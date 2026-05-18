"""Tests for session management endpoints (PATCH, export, archive, permanent delete)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

os.environ.setdefault("APP_PASSWORD", "test-secret")
os.environ.setdefault("UPLOAD_DIR", "/tmp/claude_test_uploads2")

import importlib
app_module = importlib.import_module("app")


def _make_session(sid="sess-1", title="Test Session", archived=False):
    return {
        "session_id": sid,
        "title": title,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "archived": archived,
        "messages": [
            {"role": "user", "text": "Hello"},
            {"role": "assistant", "text": "Hi there!"},
        ],
    }


class TestSessionManagement(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        sessions = [_make_session("sess-active"), _make_session("sess-archived", archived=True)]
        self._tmp.write(json.dumps(sessions).encode())
        self._tmp.close()
        self._orig = app_module.SESSIONS_FILE
        app_module.SESSIONS_FILE = Path(self._tmp.name)

    def tearDown(self):
        app_module.SESSIONS_FILE = self._orig
        Path(self._tmp.name).unlink(missing_ok=True)

    def _c(self):
        return TestClient(app_module.app)

    def _tok(self):
        return {"X-Token": app_module._TOKEN}

    # ── LIST ────────────────────────────────────────────────────────────

    def test_list_active_excludes_archived(self):
        r = self._c().get("/claude/sessions", headers=self._tok())
        self.assertEqual(r.status_code, 200)
        sids = [s["session_id"] for s in r.json()["sessions"]]
        self.assertIn("sess-active", sids)
        self.assertNotIn("sess-archived", sids)

    def test_list_archived_only(self):
        r = self._c().get("/claude/sessions?archived=true", headers=self._tok())
        self.assertEqual(r.status_code, 200)
        sids = [s["session_id"] for s in r.json()["sessions"]]
        self.assertIn("sess-archived", sids)
        self.assertNotIn("sess-active", sids)

    # ── SOFT DELETE (ARCHIVE) ────────────────────────────────────────────

    def test_delete_soft_archives(self):
        r = self._c().delete("/claude/sessions/sess-active", headers=self._tok())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("archived"))
        # Session still in file
        data = json.loads(Path(self._tmp.name).read_text())
        by_id = {s["session_id"]: s for s in data}
        self.assertIn("sess-active", by_id)
        self.assertTrue(by_id["sess-active"]["archived"])

    def test_delete_not_in_active_list_after_archive(self):
        self._c().delete("/claude/sessions/sess-active", headers=self._tok())
        r = self._c().get("/claude/sessions", headers=self._tok())
        sids = [s["session_id"] for s in r.json()["sessions"]]
        self.assertNotIn("sess-active", sids)

    # ── PATCH (RENAME + RESTORE) ─────────────────────────────────────────

    def test_patch_rename(self):
        r = self._c().patch("/claude/sessions/sess-active",
                            json={"title": "New Name"}, headers=self._tok())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["title"], "New Name")

    def test_patch_restore(self):
        r = self._c().patch("/claude/sessions/sess-archived",
                            json={"archived": False}, headers=self._tok())
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["archived"])
        # Should appear in active list now
        r2 = self._c().get("/claude/sessions", headers=self._tok())
        sids = [s["session_id"] for s in r2.json()["sessions"]]
        self.assertIn("sess-archived", sids)

    def test_patch_unauthorized(self):
        r = self._c().patch("/claude/sessions/sess-active",
                            json={"title": "x"}, headers={"X-Token": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_patch_not_found(self):
        r = self._c().patch("/claude/sessions/nonexistent",
                            json={"title": "x"}, headers=self._tok())
        self.assertEqual(r.status_code, 404)

    # ── EXPORT ──────────────────────────────────────────────────────────

    def test_export_returns_markdown(self):
        r = self._c().get("/claude/sessions/sess-active/export", headers=self._tok())
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/markdown", r.headers.get("content-type", ""))
        self.assertIn("attachment", r.headers.get("content-disposition", ""))
        body = r.text
        self.assertIn("# Test Session", body)
        self.assertIn("Hello", body)
        self.assertIn("Hi there!", body)

    def test_export_not_found(self):
        r = self._c().get("/claude/sessions/nonexistent/export", headers=self._tok())
        self.assertEqual(r.status_code, 404)

    # ── PERMANENT DELETE ─────────────────────────────────────────────────

    def test_permanent_delete(self):
        r = self._c().delete("/claude/sessions/sess-archived/permanent", headers=self._tok())
        self.assertEqual(r.status_code, 200)
        data = json.loads(Path(self._tmp.name).read_text())
        sids = [s["session_id"] for s in data]
        self.assertNotIn("sess-archived", sids)

    def test_permanent_delete_not_found(self):
        r = self._c().delete("/claude/sessions/nonexistent/permanent", headers=self._tok())
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
