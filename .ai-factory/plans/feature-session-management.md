# Session Management Upgrades

**Branch:** `feature/session-management`
**Created:** 2026-05-18
**Milestone:** Session Management Upgrades (ROADMAP.md)

## Settings

- **Testing:** yes — pytest for new backend routes
- **Logging:** verbose — console.debug for all session actions + print() for backend
- **Docs:** yes — update README after implementation

## Roadmap Linkage

**Milestone:** "Session Management Upgrades"
**Rationale:** Addresses all three known gaps at once: no rename, no search, no export. Archive replaces accidental-delete risk.

## Architecture

All changes stay within the single-file `app.py` pattern:
- Backend: new PATCH + export + permanent-delete routes; soft-archive via `archived: bool` flag in JSON
- Frontend: search input, inline rename (dblclick), archive button replacing ×, archive drawer, export button

Session JSON schema gains one optional field: `"archived": false` (absent = not archived, backwards compat).

## Tasks

### Phase 1 — Backend (Tasks #16, #17, #18)

- [x] **Task #16** — Add `archived` support to list/delete; `GET /sessions?archived=true`
- [x] **Task #17** — `PATCH /claude/sessions/{id}` for rename + restore (blocked by #16)
- [x] **Task #18** — `GET /claude/sessions/{id}/export` markdown download (blocked by #16)

### Phase 2 — Frontend (Tasks #19, #20, #21, #22)

- [x] **Task #19** — Search input in sidebar (independent)
- [x] **Task #20** — Inline rename on double-click (blocked by #17)
- [x] **Task #21** — Archive button + drawer with restore and permanent delete (blocked by #16, #17)
- [x] **Task #22** — Export button per session item (blocked by #18)

### Phase 3 — Tests + Docs (Tasks #23, #24)

- [x] **Task #23** — `tests/test_sessions.py` (blocked by #16, #17, #18)
- [x] **Task #24** — README update (blocked by #16–22)

## Commit Plan

| Commit | Tasks | Message |
|---|---|---|
| 1 | #16, #17, #18 | `feat: session archive, rename, and markdown export endpoints` |
| 2 | #19, #20, #21, #22 | `feat(ui): session search, rename, archive drawer, export button` |
| 3 | #23, #24 | `test: session management test suite; docs: README update` |
