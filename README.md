# claude-ui

A tiny FastAPI wrapper around the `claude` CLI that exposes a single-page chat UI at `/claude`. Persists sessions to disk, streams the CLI's stderr as a live terminal log, and (optionally) auto-pushes an Obsidian vault after every reply.

## Stack

- **Backend:** Python 3, [FastAPI](https://fastapi.tiangolo.com/), Uvicorn (ASGI server)
- **Frontend:** vanilla HTML + CSS + JS, all embedded as a single string inside `app.py`
- **Engine:** [`@anthropic-ai/claude-code`](https://github.com/anthropics/claude-code) CLI, invoked as a subprocess for every prompt
- **Persistence:** plain JSON file (`SESSIONS_FILE`), one document holds every session
- **Optional:** git auto-push of `OBSIDIAN_PATH` after each successful reply

The whole project is one file (`app.py`, ~700 lines) plus a `Dockerfile`, three k8s manifests under `k8s/`, and a `tests/` suite.

## Features

- Password gate with HMAC token in `localStorage`
- Session list with delete, persistent across reloads, tooltip on hover
- **Collapsible sidebar** — click the chevron in the header or hit `Ctrl/Cmd + B`; collapsed state is remembered in `localStorage` (`claude_sidebar_collapsed`)
- Model picker — Sonnet 4.6, Opus 4.7, Haiku 4.5
- **Token-by-token streaming** — assistant replies appear word-by-word as Claude generates them (uses `--output-format stream-json`)
- Live terminal panel showing `claude` CLI stderr (collapsible)
- Dark theme

## Routes

| Method | Path | Description |
|--------|------|-------------|
| GET    | `/claude` | Serve the chat UI |
| POST   | `/claude/auth` | Login with `{password}` or re-validate `{token}` |
| GET    | `/claude/sessions` | List sessions (metadata only) |
| GET    | `/claude/sessions/{id}` | Get full session with messages |
| DELETE | `/claude/sessions/{id}` | Delete a session |
| POST   | `/claude/ask` | Send a prompt; returns an SSE stream of `text` (streaming deltas), `terminal`, `session_id`, `done` events |

All `/claude/*` routes except `GET /claude` and `POST /claude/auth` require the HMAC token in the `X-Token` header.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `APP_PASSWORD` | _(empty)_ | Required. Plaintext password used to derive the HMAC token. With an empty value the app rejects every authenticated request. |
| `OBSIDIAN_PATH` | `/home/node/obsidian` | Working directory for the post-reply `git add/commit/pull/push` cycle. Set to a vault you want auto-synced, or leave alone — failures are swallowed silently. |
| `SESSIONS_FILE` | `/home/node/sessions.json` | Path to the JSON file that stores chat history. Created on first write. |

The underlying `claude` CLI reads its own variables (most importantly `ANTHROPIC_API_KEY`) — see the [Claude Code docs](https://docs.anthropic.com/en/docs/claude-code) for the full list. `app.py` only forwards the existing process environment, plus `HOME=/home/node`.

## Run locally

Prerequisite: install the `claude` CLI globally.

```bash
npm install -g @anthropic-ai/claude-code
pip install fastapi uvicorn
APP_PASSWORD=secret \
ANTHROPIC_API_KEY=sk-ant-... \
python3 -m uvicorn app:app --port 8080
```

Then open <http://localhost:8080/claude>.

## Run in Docker

The provided `Dockerfile` installs Node 20, Python 3, the `claude` CLI, FastAPI, and `kubectl` (used so the in-container `claude` can talk to a cluster when needed). To build and run:

```bash
docker build -t claude-ui .
docker run --rm -p 8080:8080 \
  -e APP_PASSWORD=secret \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  claude-ui
```

## Kubernetes

Manifests live under `k8s/`:

- `deployment.yaml` — single replica, mounts `/home/node/.claude`, `/home/node/obsidian`, `/home/node/.ssh`, `/home/node/workspace` as hostPaths; reads `APP_PASSWORD` from the `claude-ui-secret` secret
- `service.yaml` — `ClusterIP` on port 8080
- `ingress.yaml` — HTTPS ingress in front of the service

Quick apply:

```bash
kubectl create secret generic claude-ui-secret --from-literal=password=secret
kubectl apply -f k8s/
```

Replace `IMAGE_TAG` in `k8s/deployment.yaml` with the image tag you pushed to GHCR.

## Security

- The HMAC token is stored in `localStorage` — anyone with access to the user's browser session can use it
- `/claude/ask` invokes `claude` with `--dangerously-skip-permissions` and `--max-turns 20`, so the CLI can run any tool the host environment allows
- **Never expose this service without `APP_PASSWORD` set and TLS in front** (the ingress manifest assumes you bring your own TLS)
- The optional git auto-push will commit and push **all** uncommitted changes inside `OBSIDIAN_PATH` after every reply — point it at a directory you actually want auto-synced

## Known gaps

- Sessions can't be renamed or searched
- No mobile / responsive layout
- No file/image upload or download
- No slash-command picker or skills browser
- Single-file architecture is intentional but constrains how much UI complexity is sensible
