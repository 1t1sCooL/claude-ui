# Project Roadmap

> A beautiful, internet-accessible web UI for Claude Code with full feature parity (files, images, slash commands, skills) and persistent sessions across visits.

## Milestones

- [x] **Public Deployment** — Single-server FastAPI app behind k8s ingress with TLS, reachable from anywhere
- [x] **Auth Gate** — Password + HMAC token in localStorage, blocks unauthenticated requests
- [x] **Persistent Sessions** — All chats stored in `SESSIONS_FILE`, restored on reload, listed in collapsible sidebar
- [x] **Model Picker & Live Terminal** — Switch between Sonnet/Opus/Haiku, watch `claude` CLI stderr stream live
- [x] **Token-by-Token Streaming** — Replace single-chunk reply with real-time streaming using `claude --output-format stream-json`, so messages appear as they are generated
- [x] **File & Image Upload** — Drag-and-drop and paste attachments into the composer; forward them to the `claude` CLI via stdin/temp files so the model can read images and source files
- [ ] **File & Image Download** — Detect files Claude wrote or modified inside the workspace, render images inline, expose a per-message download tray
- [ ] **Slash Command Picker** — Typing `/` opens an autocomplete menu of available slash commands (skills) with descriptions, inserts the chosen command into the composer
- [ ] **Skills & Agents Browser** — A dedicated panel listing installed skills/subagents with descriptions, one-click insert into the prompt
- [ ] **Markdown & Code Rendering** — Proper markdown rendering of replies with syntax-highlighted code blocks, copy-code buttons, tables, and Mermaid diagrams
- [ ] **Session Management Upgrades** — Rename sessions, full-text search across history, export to markdown, archive instead of delete
- [ ] **Workspace File Browser** — Side panel showing the contents of `/home/node/workspace`, view/download/upload files without going through chat
- [ ] **Mobile & Responsive Layout** — Sidebar becomes a drawer, composer is thumb-friendly, attachments work from the camera roll
- [ ] **UI Polish & Theming** — Light/dark toggle, refined typography, motion design, "beautiful UI" pass that matches the project goal
- [ ] **Multi-User Auth** — Per-user accounts with isolated session storage, replacing the single shared password
- [ ] **Observability & Tests** — End-to-end smoke tests for the chat flow, error toasts in the UI, structured logs from the backend

## Completed

| Milestone | Date |
|-----------|------|
| Public Deployment | 2026-05-18 |
| Auth Gate | 2026-05-18 |
| Persistent Sessions | 2026-05-18 |
| Model Picker & Live Terminal | 2026-05-18 |
| Token-by-Token Streaming | 2026-05-18 |
| File & Image Upload | 2026-05-18 |
