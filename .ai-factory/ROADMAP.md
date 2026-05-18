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
- [x] **Multi-User Auth** — Per-user accounts with isolated session storage, replacing the single shared password
- [x] **Observability & Tests** — End-to-end smoke tests for the chat flow, error toasts in the UI, structured logs from the backend

### 🔴 High Priority

- [x] **Stop Generation** — Cancel a running request mid-stream; Stop button replaces Send while streaming, backend terminates the subprocess
- [x] **Message Retry & Edit** — Regenerate assistant response with one click; edit a sent user message and rerun from that point
- [x] **Paste Images from Clipboard** — Ctrl+V in the composer pastes screenshots directly as attachments (extends existing drag-and-drop)
- [ ] **Prompt Templates** — Save frequently used prompts as reusable templates; one-click insert into composer with a dedicated picker
- [ ] **Git Panel** — Sidebar panel showing `git status`/`diff` of the workspace; commit, stage, and view log without going through chat
- [ ] **Context Window Indicator** — Progress bar showing how much of the session context window is consumed
- [ ] **MCP Servers Panel** — View installed MCP servers, their available tools, enable/disable them, and inspect tool schemas

### 🟡 Medium Priority

- [x] **Full-Text Session Search** — Search message content across all sessions, not just session titles
- [x] **Tool Use Progress Cards** — Show inline "⚡ Using tool: bash" cards in chat while Claude is executing tools, not just in the terminal panel
- [x] **Workspace ZIP Export** — Download the entire `/home/node/workspace` as a zip archive with one button click
- [ ] **Session Folders & Tags** — Organise sessions into folders or attach tags; filter sidebar by tag
- [ ] **Pinned Sessions** — Pin important sessions to the top of the sidebar list, persisted across reloads
- [ ] **Webhook Notifications** — Notify an external endpoint (Telegram bot, Slack, email) when a long-running task completes
- [ ] **Voice Input** — Speech-to-text in the composer via the Web Speech API; push-to-talk button next to the input field
- [ ] **Session Import** — Import sessions from previously exported JSON files to restore history (complements existing export)
- [ ] **Split View** — Display two sessions side by side for comparison or parallel work

### 🟢 Nice to Have

- [x] **Keyboard Shortcuts** — Ctrl+K new chat, Ctrl+/ skills browser, arrow keys to navigate session list
- [x] **Auto-Scroll Toggle** — Pin/unpin scroll so you can read the beginning of a long response while it's still streaming
- [x] **Message Full-Text Export** — Export a single session as JSON (in addition to existing markdown export)
- [ ] **Custom System Prompt** — Set a per-session or global system prompt from the UI without editing files
- [ ] **TTS Output** — Text-to-speech playback of assistant responses via the Web Speech API
- [ ] **Session Sharing** — Generate a read-only shareable link to a session for others to view
- [ ] **API Access** — Expose a simple REST/WebSocket endpoint so external tools can send tasks to the same running instance

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
| Multi-User Auth | 2026-05-18 |
