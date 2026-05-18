"""
Tests for stream-json backend logic in /claude/ask.

Mocks asyncio.create_subprocess_exec so no real `claude` CLI is needed.
"""
import asyncio
import io
import json
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Make sure app module is importable even without the real `claude` binary.
os.environ.setdefault("APP_PASSWORD", "test-secret")

import importlib
app_module = importlib.import_module("app")


# ── Helpers ──────────────────────────────────────────────────────────────────

def ndjson(*events: dict) -> bytes:
    """Encode a sequence of dicts as NDJSON bytes (one JSON per line)."""
    return b"\n".join(json.dumps(e).encode() for e in events) + b"\n"


def _make_stream(stdout_bytes: bytes, stderr_lines: list[str] = None):
    """Return a fake asyncio.create_subprocess_exec coroutine.

    stdout_bytes is returned line-by-line via AsyncIterator.
    stderr_lines (list of str) is returned via AsyncIterator.
    """
    stderr_lines = stderr_lines or []

    class FakeStream:
        def __init__(self, lines: list[bytes]):
            self._lines = lines
            self._index = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._index >= len(self._lines):
                raise StopAsyncIteration
            line = self._lines[self._index]
            self._index += 1
            return line

    stdout_lines = [l + b"\n" for l in stdout_bytes.split(b"\n") if l]
    stderr_bytes = [s.encode() + b"\n" for s in stderr_lines]

    proc = MagicMock()
    proc.stdout = FakeStream(stdout_lines)
    proc.stderr = FakeStream(stderr_bytes)
    proc.returncode = 0
    proc.wait = AsyncMock(return_value=0)

    async def fake_exec(*args, **kwargs):
        return proc

    return fake_exec, proc


async def _collect_sse(gen) -> list[dict]:
    """Drain an async generator and return parsed SSE data payloads."""
    events = []
    async for chunk in gen:
        for line in chunk.split("\n"):
            line = line.strip()
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
    return events


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestStreamJson(unittest.IsolatedAsyncioTestCase):

    async def _run_ask(self, stdout_bytes: bytes, stderr_lines: list[str] = None,
                       prompt: str = "hello", session_id: str = ""):
        """Invoke the stream generator directly and return (sse_events, saved_sessions)."""
        fake_exec, _ = _make_stream(stdout_bytes, stderr_lines)
        saved = []

        def fake_upsert(sid, user_msg, assistant_msg, attachments=None):
            saved.append({"sid": sid, "user": user_msg, "assistant": assistant_msg})

        with patch("asyncio.create_subprocess_exec", new=fake_exec), \
             patch.object(app_module, "_upsert_session", side_effect=fake_upsert), \
             patch.object(app_module, "_git_push", new=AsyncMock()):
            # Reconstruct a minimal request context so ask() can build its inner stream()
            request = MagicMock()
            request.headers = {"X-Token": app_module._TOKEN}

            # Directly grab the stream() generator via the ask() handler
            # We patch the body to inject our prompt / session_id
            async def fake_json():
                return {"prompt": prompt, "model": "claude-sonnet-4-6", "session_id": session_id}
            request.json = fake_json

            response = await app_module.ask(request)
            events = await _collect_sse(response.body_iterator)

        return events, saved

    # ── 1. Happy path ─────────────────────────────────────────────────────────

    async def test_happy_path(self):
        """System → assistant (x2) → result should yield sid, deltas, done."""
        stdout = ndjson(
            {"type": "system", "session_id": "sess-abc"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello World"}]}},
            {"type": "result", "result": "Hello World", "session_id": "sess-abc", "is_error": False},
        )
        events, saved = await self._run_ask(stdout, prompt="hi")

        sid_events = [e for e in events if "session_id" in e]
        text_events = [e for e in events if "text" in e]
        done_events = [e for e in events if e.get("done")]

        self.assertTrue(len(sid_events) >= 1, "Should emit at least one session_id event")
        self.assertEqual(sid_events[0]["session_id"], "sess-abc")
        self.assertTrue(len(done_events) == 1, "Should emit exactly one done event")
        self.assertTrue(len(text_events) >= 1, "Should emit at least one text delta")

    # ── 2. Delta computation ──────────────────────────────────────────────────

    async def test_delta_computation(self):
        """Two assistant events: first 'Hello', second 'Hello World'. Deltas: 'Hello' and ' World'."""
        stdout = ndjson(
            {"type": "system", "session_id": "sess-delta"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello World"}]}},
            {"type": "result", "result": "Hello World", "session_id": "sess-delta", "is_error": False},
        )
        events, _ = await self._run_ask(stdout, prompt="delta-test")

        text_events = [e for e in events if "text" in e]
        texts = [e["text"] for e in text_events]

        self.assertIn("Hello", texts, "First delta should be 'Hello'")
        self.assertIn(" World", texts, "Second delta should be ' World'")
        self.assertNotIn("Hello World", texts, "Should NOT emit full text twice")

    # ── 3. is_error result ────────────────────────────────────────────────────

    async def test_is_error_result(self):
        """is_error=True result should prefix saved text with [ERROR] and still emit done."""
        stdout = ndjson(
            {"type": "system", "session_id": "sess-err"},
            {"type": "result", "result": "Something went wrong", "session_id": "sess-err", "is_error": True},
        )
        events, saved = await self._run_ask(stdout, prompt="bad-prompt")

        done_events = [e for e in events if e.get("done")]
        self.assertEqual(len(done_events), 1, "Should emit done even on error")
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0]["assistant"].startswith("[ERROR] "),
                        "Error session text must be prefixed with [ERROR] ")
        self.assertIn("Something went wrong", saved[0]["assistant"])

    # ── 4. Process crash (no result event) ───────────────────────────────────

    async def test_proc_crash_no_result(self):
        """If proc closes stdout without a result event, accumulated deltas are used as fallback."""
        stdout = ndjson(
            {"type": "system", "session_id": "sess-crash"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Partial"}]}},
            # No result event — simulates crash
        )
        events, saved = await self._run_ask(stdout, prompt="crash-test")

        done_events = [e for e in events if e.get("done")]
        self.assertEqual(len(done_events), 1, "Should still emit done after crash")
        self.assertEqual(len(saved), 1, "Should still save session with fallback text")
        self.assertEqual(saved[0]["assistant"], "Partial",
                         "Fallback text should equal accumulated deltas")


if __name__ == "__main__":
    unittest.main()
