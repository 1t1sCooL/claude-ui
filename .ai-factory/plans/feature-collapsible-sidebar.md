# Collapsible Sidebar + README

- **Branch:** `feature/collapsible-sidebar`
- **Base:** `main`
- **Created:** 2026-05-15
- **Repo:** https://github.com/1t1sCooL/claude-ui

## Settings

- **Testing:** no — repo has no test infra (single-file FastAPI, embedded HTML)
- **Logging:** minimal — one `console.debug('[sidebar] toggled', collapsed)` on each toggle, no backend changes
- **Docs:** yes — README is part of the scope (no separate docs checkpoint, README is delivered as task #5)
- **Scope guardrail:** only the sidebar collapse feature + README. No refactor of `app.py` into multi-file, no streaming-text fix, no session search.

## Roadmap Linkage

- **Milestone:** none
- **Rationale:** project has no `.ai-factory/ROADMAP.md`

## Context Snapshot

`app.py` (634 lines) is the whole project: FastAPI server + HTML/CSS/JS as one big triple-quoted string `HTML = r"""..."""`. Sidebar is `#sidebar` (fixed 260px). All UI changes happen inside that string.

Relevant anchors in `app.py`:

- `/* Sidebar */` CSS block — around line 114-127
- `<div id="sidebar">` markup — around line 183-188
- `// ── Session list ──` JS section — around line 265
- `loadSessions` / `renderSessions` — already iterates sessions and creates `.session-item` nodes; safe place to add `title` attr for tooltip behaviour when collapsed.

## Tasks

### Phase 1 — Sidebar feature

1. ✅ **Add collapsed-state CSS** (`app.py` HTML `<style>`)
   - `#sidebar.collapsed { width: 56px; transition: width .18s ease; }`
   - Hide `.session-title`, `.session-date`, `.session-del`, `#new-chat-btn .label` when collapsed
   - Style `#sidebar-toggle` (28x28, top-right of header, rotates chevron via `transform`)
   - Keep `#session-list` items clickable; rely on `title` attr for tooltip

2. ✅ **Add toggle button markup** (`app.py` HTML body) — blocked by #1
   - New `<button id="sidebar-toggle" aria-label="Свернуть боковую панель" title="Свернуть (Ctrl/Cmd+B)">` with inline SVG chevron
   - Split `#new-chat-btn` into `<span class="icon">+</span><span class="label">Новый чат</span>`

3. ✅ **Toggle JS + persistence** (`app.py` HTML `<script>`) — blocked by #2
   - `const SIDEBAR_KEY = 'claude_sidebar_collapsed';`
   - On boot: read `localStorage[SIDEBAR_KEY]`, apply `.collapsed` and `aria-expanded`
   - Click handler on `#sidebar-toggle`: toggle class, persist '1'/'0', update `aria-expanded` and `title` text (Свернуть / Развернуть)
   - Window keydown: `Ctrl/Cmd+B` triggers toggle, but only when `#auth.hidden` (skip on login screen)
   - In `renderSessions`: `item.title = s.title || 'Без названия'` for hover tooltip in collapsed mode
   - Add `console.debug('[sidebar] toggled', collapsed)` once per toggle

4. ✅ **Smoke-test in browser** — blocked by #3 (server-side smoke check done: app boots, `/claude` returns 200, new markers present, `/claude/auth` works; full UI interactivity test must be done manually in a browser)
   ```bash
   cd /Users/worker/Work/claude-ui
   pip install fastapi uvicorn
   APP_PASSWORD=test python3 -m uvicorn app:app --reload --port 8080
   ```
   Verify: collapse to 56px, chevron rotates, refresh persists state, `Ctrl/Cmd+B` works, tooltip shows full title, no layout regressions in `#main` / `#term-panel` / `#footer`, login screen ignores the shortcut.

### Phase 2 — Documentation

5. **Write `README.md`** (new file at repo root)
   - What it is — FastAPI wrapper around `claude` CLI, single-page chat UI at `/claude`
   - Stack: Python 3 + FastAPI + Uvicorn, vanilla HTML/CSS/JS in `app.py`, calls `@anthropic-ai/claude-code` CLI, optional git auto-push of an Obsidian vault
   - Env vars: `APP_PASSWORD` (required), `OBSIDIAN_PATH` (default `/home/node/obsidian`), `SESSIONS_FILE` (default `/home/node/sessions.json`), plus `claude` CLI vars (`ANTHROPIC_API_KEY`, etc.)
   - Routes: `GET /claude`, `POST /claude/auth`, `GET /claude/sessions`, `GET/DELETE /claude/sessions/{id}`, `POST /claude/ask` (SSE)
   - Run locally / Run in Docker (references existing `Dockerfile`) / Kubernetes (references `k8s/*.yaml`)
   - UI features (including new collapsible sidebar + `Ctrl/Cmd+B`)
   - Security note: HMAC token in localStorage, `--dangerously-skip-permissions` is passed to `claude` CLI — keep behind TLS, never expose without `APP_PASSWORD`
   - Roadmap / Known gaps: no session rename, no search, no mobile layout, response arrives as a single chunk (no token-by-token streaming)

## Commit Plan

5 tasks → use checkpoints:

- **Checkpoint A** (after task #4): `feat(ui): collapsible sidebar with persisted state and Ctrl+B shortcut`
- **Checkpoint B** (after task #5): `docs: add README with stack, env, routes, and deploy notes`

## Out of Scope (explicit)

- Splitting `app.py` into separate Python/HTML/JS files
- Token-by-token streaming in `/claude/ask`
- Mobile/responsive sidebar (drawer)
- Session rename, search, pinning
- Tests (no infrastructure in repo)
- CI workflow changes (existing `.github/workflows/` left untouched)
