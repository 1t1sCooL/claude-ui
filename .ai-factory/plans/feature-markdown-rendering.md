# Markdown & Code Rendering

**Branch:** `feature/markdown-rendering`
**Created:** 2026-05-18
**Milestone:** Markdown & Code Rendering (ROADMAP.md)

## Settings

- **Testing:** no (pure frontend; not suited for unit tests)
- **Logging:** verbose — console.debug for each render step
- **Docs:** yes — update README after implementation

## Roadmap Linkage

**Milestone:** "Markdown & Code Rendering"
**Rationale:** Most impactful visible improvement — turns raw text walls into readable, structured responses with code highlighting.

## Architecture

Single-file approach: all libraries loaded from jsDelivr CDN (deferred). No npm, no build step.

Libraries:
- `marked@13` — markdown parser with custom renderer
- `highlight.js@11.9.0` — syntax highlighting for code blocks
- `mermaid@11` — diagram rendering for ```mermaid fences

Rendering strategy:
- **During streaming:** raw text appended as `textContent` (fast, no flicker)
- **After `done` event:** replace with `innerHTML = renderMarkdown(rawText)` + inject copy buttons + run mermaid
- **History / addMsg:** render markdown immediately on load

Key CSS override: `.bubble.rendered { white-space: normal }` removes `pre-wrap` for rendered markdown.

## Tasks

### Phase 1 — CDN + CSS (Task #12)

- [x] **Task #12** — Add CDN links to `<head>`, add markdown CSS block

### Phase 2 — JS utilities (Task #13)

- [x] **Task #13** — `initMarkdown()`, `renderMarkdown()`, `injectCopyButtons()`, `applyMermaid()` (blocked by #12)

### Phase 3 — Wiring (Task #14)

- [x] **Task #14** — Update streaming + `addMsg` + `openSession` to use markdown (blocked by #13)

### Phase 4 — Docs (Task #15)

- [x] **Task #15** — README update (blocked by #14)

## Commit Plan

| Commit | Tasks | Message |
|---|---|---|
| 1 | #12, #13 | `feat: add marked.js + highlight.js + mermaid + rendering utilities` |
| 2 | #14 | `feat(ui): render markdown in assistant messages with copy buttons` |
| 3 | #15 | `docs: update README — markdown rendering now live` |
