# Project Roadmap

> A beautiful, internet-accessible web UI for Claude Code with full feature parity (files, images, slash commands, skills) and persistent sessions across visits.

## Milestones

- [x] **Public Deployment** — Single-server FastAPI app behind k8s ingress with TLS, reachable from anywhere
- [x] **Auth Gate** — Password + HMAC token in localStorage, blocks unauthenticated requests
- [x] **Persistent Sessions** — All chats stored in `SESSIONS_FILE`, restored on reload, listed in collapsible sidebar
- [x] **Model Picker & Live Terminal** — Switch between Sonnet/Opus/Haiku, watch `claude` CLI stderr stream live
- [x] **Token-by-Token Streaming** — Replace single-chunk reply with real-time streaming using `claude --output-format stream-json`, so messages appear as they are generated
- [x] **File & Image Upload** — Drag-and-drop and paste attachments into the composer; forward them to the `claude` CLI via stdin/temp files so the model can read images and source files
- [x] **File & Image Download** — Detect files Claude wrote or modified inside the workspace, render images inline, expose a per-message download tray
- [x] **Slash Command Picker** — Typing `/` opens an autocomplete menu of available slash commands (skills) with descriptions, inserts the chosen command into the composer
- [x] **Skills & Agents Browser** — A dedicated panel listing installed skills/subagents with descriptions, one-click insert into the prompt
- [x] **Markdown & Code Rendering** — Proper markdown rendering of replies with syntax-highlighted code blocks, copy-code buttons, tables, and Mermaid diagrams
- [x] **Session Management Upgrades** — Rename sessions, full-text search across history, export to markdown, archive instead of delete
- [x] **Workspace File Browser** — Side panel showing the contents of `/home/node/workspace`, view/download/upload files without going through chat
- [x] **Mobile & Responsive Layout** — Sidebar becomes a drawer, composer is thumb-friendly, attachments work from the camera roll
- [x] **UI Polish & Theming** — Light/dark toggle, refined typography, motion design, "beautiful UI" pass that matches the project goal
- [ ] **Multi-User Auth** — Per-user accounts with isolated session storage, replacing the single shared password
- [x] **Observability & Tests** — End-to-end smoke tests for the chat flow, error toasts in the UI, structured logs from the backend

### 🔴 High Priority

- [ ] **Stop Generation** — Cancel a running request mid-stream; Stop button replaces Send while streaming, backend terminates the subprocess
- [ ] **Message Retry & Edit** — Regenerate assistant response with one click; edit a sent user message and rerun from that point
- [ ] **Paste Images from Clipboard** — Ctrl+V in the composer pastes screenshots directly as attachments (extends existing drag-and-drop)

### 🟡 Medium Priority

- [ ] **Full-Text Session Search** — Search message content across all sessions, not just session titles
- [ ] **Tool Use Progress Cards** — Show inline "⚡ Using tool: bash" cards in chat while Claude is executing tools, not just in the terminal panel
- [ ] **Workspace ZIP Export** — Download the entire `/home/node/workspace` as a zip archive with one button click

### 🟢 Nice to Have

- [ ] **Keyboard Shortcuts** — Ctrl+K new chat, Ctrl+/ skills browser, arrow keys to navigate session list
- [ ] **Auto-Scroll Toggle** — Pin/unpin scroll so you can read the beginning of a long response while it's still streaming
- [ ] **Message Full-Text Export** — Export a single session as JSON (in addition to existing markdown export)

## Completed

| Milestone | Date |
|-----------|------|
| Public Deployment | 2026-05-18 |
| Auth Gate | 2026-05-18 |
| Persistent Sessions | 2026-05-18 |
| Model Picker & Live Terminal | 2026-05-18 |
| Token-by-Token Streaming | 2026-05-18 |
| File & Image Upload | 2026-05-18 |
| Markdown & Code Rendering | 2026-05-18 |
| Session Management Upgrades | 2026-05-18 |
| File & Image Download | 2026-05-18 |
| Slash Command Picker | 2026-05-18 |
| UI Polish & Theming | 2026-05-18 |
| Mobile & Responsive Layout | 2026-05-18 |
| Workspace File Browser | 2026-05-18 |
| Skills & Agents Browser | 2026-05-18 |
| Observability & Tests | 2026-05-18 |
