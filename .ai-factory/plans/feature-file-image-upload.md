# File & Image Upload

**Branch:** `feature/file-image-upload`
**Created:** 2026-05-18
**Milestone:** File & Image Upload (ROADMAP.md)

## Settings

- **Testing:** yes — pytest for upload endpoint and prompt augmentation
- **Logging:** verbose — DEBUG for each file received/saved, INFO for batch saves
- **Docs:** yes — update README after implementation

## Roadmap Linkage

**Milestone:** "File & Image Upload"
**Rationale:** Second unchecked milestone; enables attaching code files and screenshots to messages — the most requested capability after streaming.

## Architecture

The implementation follows a two-endpoint design within the existing single-file architecture:

1. `POST /claude/upload` — accepts multipart/form-data, writes files to workspace, returns paths
2. `POST /claude/ask` — unchanged JSON API, gains optional `attachments: [{path, name, is_image}]` field

Files land in `/home/node/workspace/.uploads/<batch_uuid>/<safe_filename>`.
Claude CLI receives an augmented prompt that lists file paths; Claude reads them via its file tools.

## Constraints

- 20 MB per file limit
- Needs `python-multipart` in Dockerfile and local install
- Images approach: write to workspace + reference in prompt (no base64 stdin)
- File serving for history: expose `GET /claude/files/{path}` proxy route

## Tasks

### Phase 1 — Backend upload (Task #6, #7)

- [x] **Task #6** — `POST /claude/upload` endpoint + `python-multipart` dep
  - `_safe_filename()` helper, `UPLOAD_DIR` constant, 20 MB validation
  - Write files to `UPLOAD_DIR/<batch_uuid>/<safe_name>`
  - Return `{files: [{id,name,path,mime_type,is_image,size}]}`
  - DEBUG log each file received/saved

- [x] **Task #7** — Augment prompt + store attachments in session (blocked by #6)
  - `_build_prompt(prompt, attachments)` helper
  - Add `attachments` to JSON body of `/claude/ask`
  - Update `_upsert_session` signature to accept attachments
  - Store `attachments` in user message record

### Phase 2 — Frontend (Task #8, #9)

- [x] **Task #8** — Attachment composer UI (blocked by #6)
  - Paperclip button, hidden file input, drag-drop, paste
  - Upload-on-pick: POST to `/claude/upload`, show chip with × to remove
  - Include `attachments` in form submit JSON

- [x] **Task #9** — Render attachments in message bubbles (blocked by #7, #8)
  - `addMsg(role, text, attachments=[])` signature update
  - Images: `<img>` with objectURL or `/claude/files/` proxy
  - Files: `📄 name` chip
  - Add `GET /claude/files/{path}` proxy route to backend
  - History: render attachments when loading sessions

### Phase 3 — Tests + Docs (Task #10, #11)

- [x] **Task #10** — `tests/test_uploads.py` (blocked by #6, #7)
  - 6 scenarios: single image, text file, too large, unauthenticated, augmentation, empty attachments

- [x] **Task #11** — README update (blocked by #6–9)
  - Features, Routes, Env vars, local install, Known gaps

## Commit Plan

| Commit | Tasks | Message |
|---|---|---|
| 1 | #6, #7 | `feat: add /claude/upload endpoint and attachment prompt augmentation` |
| 2 | #8, #9 | `feat(ui): attachment composer, drag-drop, paste, bubble previews` |
| 3 | #10, #11 | `test: upload test suite; docs: README update` |
