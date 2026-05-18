# Token-by-Token Streaming

**Branch:** `feature/token-streaming`
**Created:** 2026-05-18
**Milestone:** Token-by-Token Streaming (ROADMAP.md)

## Settings

- **Testing:** yes — pytest with mocked subprocess
- **Logging:** verbose — DEBUG logs for every stream event, INFO for session saves
- **Docs:** yes — update README after implementation

## Roadmap Linkage

**Milestone:** "Token-by-Token Streaming"
**Rationale:** This is the first unchecked milestone and the foundational UX change — without it, every future feature (file upload, slash commands) lands on top of a 30-second blank wait.

## Problem

Currently `--output-format json` makes the CLI buffer ALL output before writing stdout once. The server reads it with `await proc.stdout.read()` and emits one giant SSE text chunk at the end. The blinking cursor in the UI is decorative only.

## Solution

Switch to `--output-format stream-json`. The CLI emits NDJSON to stdout — one JSON event per line. Events:

| type | payload we use |
|---|---|
| `system` | `session_id` (arrives first, before any text) |
| `assistant` | `message.content[0].text` — full text accumulated so far |
| `result` | `result` (full text), `session_id`, `is_error` |

Text delta = `full_text[prev_len:]` after each `assistant` event.

## Tasks

### Phase 1 — Backend core (Task #1, #2)

- [x] **Task #1** — Replace `_stdout_reader()` with `_stdout_line_reader()` in `app.py`
  - Change flag: `--output-format stream-json`
  - Line-by-line parse of NDJSON events
  - Queue: `sid`, `delta`, `result` messages
  - DEBUG log every parsed event + delta length

- [x] **Task #2** — Robust session storage (blocked by #1)
  - Use `result.text` as authoritative final text
  - Prefix error sessions with `[ERROR] `
  - Fallback to accumulated deltas on crash
  - INFO log after `_upsert_session`

### Phase 2 — Frontend (Task #3)

- [x] **Task #3** — Streaming UX edge-cases (blocked by #1)
  - Add `finally` block to remove `.streaming` on network drop
  - Add `console.debug` for deltas and session_id events
  - Verify cursor blink disappears cleanly

### Phase 3 — Tests (Task #4)

- [x] **Task #4** — `tests/test_streaming.py` (blocked by #1, #2)
  - 4 scenarios: happy path, delta computation, is_error, proc crash
  - Mock `asyncio.create_subprocess_exec`

### Phase 4 — Docs (Task #5)

- [x] **Task #5** — README update (blocked by #1–3)
  - Add streaming to Features
  - Remove "single chunk" from Known gaps
  - Update routes table

## Commit Plan

| Commit | Tasks | Message |
|---|---|---|
| 1 | #1, #2 | `feat: switch to stream-json for token-by-token streaming` |
| 2 | #3 | `fix(ui): harden streaming cursor and network-drop recovery` |
| 3 | #4 | `test: add pytest suite for stream-json backend` |
| 4 | #5 | `docs: update README — streaming now live, remove known gap` |
