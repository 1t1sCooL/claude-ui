import asyncio, json, os, hmac, hashlib, re, uuid, base64
from datetime import datetime
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request, File, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse

app = FastAPI()

APP_PASSWORD     = os.environ.get("APP_PASSWORD", "")
OBSIDIAN_PATH    = os.environ.get("OBSIDIAN_PATH", "/home/node/obsidian")
SESSIONS_FILE    = Path(os.environ.get("SESSIONS_FILE", "/home/node/sessions.json"))
UPLOAD_DIR       = Path(os.environ.get("UPLOAD_DIR", "/home/node/workspace/.uploads"))
WORKSPACE_DIR    = Path(os.environ.get("WORKSPACE_DIR", "/home/node/workspace"))
COMMANDS_DIR     = Path(os.environ.get("COMMANDS_DIR", "/home/node/.claude/commands"))
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB

_BUILTIN_COMMANDS = [
    {"name": "/clear",   "description": "Clear conversation history",          "category": "builtin"},
    {"name": "/compact", "description": "Summarize history to save context",    "category": "builtin"},
    {"name": "/help",    "description": "Show available commands and shortcuts","category": "builtin"},
    {"name": "/model",   "description": "Switch the AI model",                 "category": "builtin"},
    {"name": "/doctor",  "description": "Check Claude Code setup and health",   "category": "builtin"},
    {"name": "/mcp",     "description": "Manage MCP servers",                  "category": "builtin"},
    {"name": "/review",  "description": "Review staged git changes",            "category": "builtin"},
    {"name": "/memory",  "description": "Edit Claude memory files",             "category": "builtin"},
    {"name": "/init",    "description": "Initialize CLAUDE.md for this project","category": "builtin"},
    {"name": "/pr_comments", "description": "View GitHub PR comments",          "category": "builtin"},
    {"name": "/cost",    "description": "Show token usage and cost for session","category": "builtin"},
    {"name": "/logout",  "description": "Log out from Claude account",          "category": "builtin"},
]


def _load_commands() -> list:
    """Return built-in commands + custom commands from COMMANDS_DIR."""
    result = list(_BUILTIN_COMMANDS)
    if COMMANDS_DIR.exists() and COMMANDS_DIR.is_dir():
        custom = []
        try:
            for p in sorted(COMMANDS_DIR.glob("*.md")):
                name = "/" + p.stem
                description = ""
                try:
                    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                        line = line.strip().lstrip("#").strip()
                        if line:
                            description = line[:100]
                            break
                except Exception as e:
                    print(f"[WARN commands] failed to read {p}: {e}", flush=True)
                custom.append({"name": name, "description": description, "category": "custom"})
            print(f"[DEBUG commands] found {len(custom)} custom command(s) in {COMMANDS_DIR}", flush=True)
        except Exception as e:
            print(f"[WARN commands] failed to scan COMMANDS_DIR: {e}", flush=True)
        result.extend(custom)
    else:
        print(f"[DEBUG commands] COMMANDS_DIR not found: {COMMANDS_DIR}", flush=True)
    print(f"[DEBUG commands] total {len(result)} commands ({len(_BUILTIN_COMMANDS)} builtin)", flush=True)
    return result

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}


def _snapshot_workspace() -> dict:
    """Return {rel_path: mtime} for all files in WORKSPACE_DIR, excluding .uploads/."""
    snapshot: dict = {}
    if not WORKSPACE_DIR.exists():
        return snapshot
    try:
        for p in WORKSPACE_DIR.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(WORKSPACE_DIR)
            parts = rel.parts
            # skip .uploads and hidden top-level dirs
            if parts and (parts[0] == ".uploads" or parts[0].startswith(".")):
                continue
            snapshot[str(rel)] = p.stat().st_mtime
    except Exception as e:
        print(f"[WARN snapshot] failed to snapshot workspace: {e}", flush=True)
    print(f"[DEBUG snapshot] {len(snapshot)} files in workspace", flush=True)
    return snapshot


def _diff_workspace(before: dict, after: dict) -> list:
    """Return list of {name, rel_path, is_image, size} for new or modified files."""
    result = []
    for rel_path, mtime in after.items():
        if rel_path not in before or before[rel_path] != mtime:
            full = WORKSPACE_DIR / rel_path
            try:
                size = full.stat().st_size
            except Exception:
                size = 0
            ext = Path(rel_path).suffix.lower()
            result.append({
                "name": Path(rel_path).name,
                "rel_path": rel_path,
                "is_image": ext in _IMAGE_EXTS,
                "size": size,
            })
            print(f"[DEBUG diff] {'new' if rel_path not in before else 'modified'}: {rel_path} ({size}b)", flush=True)
    print(f"[DEBUG diff] {len(result)} output files detected", flush=True)
    return result
_TOKEN = hmac.new(b"claude-ui", APP_PASSWORD.encode(), hashlib.sha256).hexdigest() if APP_PASSWORD else ""


def _safe_filename(name: str) -> str:
    name = os.path.basename(name)
    if "." in name:
        stem, ext = name.rsplit(".", 1)
        ext = "." + re.sub(r"[^\w]", "", ext)[:10]
    else:
        stem, ext = name, ""
    stem = re.sub(r"[^\w\-]", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_") or "file"
    return (stem[:116] + ext)[:120]


# ── Sessions ──────────────────────────────────────────────────────

def _load_sessions() -> list:
    try:
        return json.loads(SESSIONS_FILE.read_text()) if SESSIONS_FILE.exists() else []
    except Exception:
        return []

def _write_sessions(sessions: list):
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSIONS_FILE.write_text(json.dumps(sessions, ensure_ascii=False, indent=2))

_MAX_INLINE_BYTES = 50_000  # embed up to 50 KB of text file content inline


def _build_prompt(prompt: str, attachments: list[dict]) -> str:
    if not attachments:
        return prompt
    parts = [prompt, "", "---"]
    for a in attachments:
        if a.get("is_image"):
            parts.append(f"[Image '{a['name']}' attached above as visual content]")
            continue
        # Text / code file: embed content directly so Claude can read it without tools
        fpath = Path(a.get("path", ""))
        if fpath.exists() and fpath.is_file():
            try:
                raw = fpath.read_bytes()
                # Try to decode as UTF-8; if that fails, treat as binary
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    parts.append(f"[Binary file '{a['name']}' at {fpath} — cannot display inline]")
                    continue
                if len(content) > _MAX_INLINE_BYTES:
                    content = content[:_MAX_INLINE_BYTES] + "\n… (truncated)"
                ext = fpath.suffix.lstrip(".") or "text"
                parts.append(f"\n**File: {a['name']}**\n```{ext}\n{content}\n```")
                print(f"[DEBUG build_prompt] embedded {a['name']} ({len(content)} chars)", flush=True)
            except Exception as e:
                parts.append(f"[Could not read '{a['name']}': {e}]")
                print(f"[WARN build_prompt] failed to read {fpath}: {e}", flush=True)
        else:
            parts.append(f"[File '{a['name']}' not found at {a.get('path')}]")
    return "\n".join(parts)


def _upsert_session(session_id: str, user_msg: str, assistant_msg: str,
                    attachments: Optional[list] = None,
                    output_files: Optional[list] = None):
    sessions = _load_sessions()
    now = datetime.utcnow().isoformat()
    user_record: dict = {"role": "user", "text": user_msg}
    if attachments:
        user_record["attachments"] = attachments
    assistant_record: dict = {"role": "assistant", "text": assistant_msg}
    if output_files:
        assistant_record["output_files"] = output_files
        print(f"[DEBUG upsert] saving {len(output_files)} output_files in session {session_id}", flush=True)
    for s in sessions:
        if s["session_id"] == session_id:
            s["updated_at"] = now
            s["messages"].extend([user_record, assistant_record])
            _write_sessions(sessions)
            return
    title = user_msg[:60] + ("…" if len(user_msg) > 60 else "")
    sessions.insert(0, {
        "session_id": session_id,
        "title":      title,
        "created_at": now,
        "updated_at": now,
        "messages": [user_record, assistant_record],
    })
    _write_sessions(sessions)


# ── Git push ──────────────────────────────────────────────────────

async def _git_push(env: dict):
    try:
        git_env = {**env, "HOME": "/home/node", "GIT_AUTHOR_NAME": "Claude VPS",
                   "GIT_AUTHOR_EMAIL": "claude@vps", "GIT_COMMITTER_NAME": "Claude VPS",
                   "GIT_COMMITTER_EMAIL": "claude@vps"}

        async def run(*cmd):
            p = await asyncio.create_subprocess_exec(
                *cmd, cwd=OBSIDIAN_PATH,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                env=git_env,
            )
            _, err = await p.communicate()
            return p.returncode, err.decode()

        p = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain", cwd=OBSIDIAN_PATH,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=git_env,
        )
        out, _ = await p.communicate()
        if not out.strip():
            return
        await run("git", "add", "-A")
        await run("git", "commit", "-m", "claude: auto-update")
        await run("git", "pull", "--rebase")
        await run("git", "push")
    except Exception:
        pass


# ── Auth ──────────────────────────────────────────────────────────

def _authorized(request: Request) -> bool:
    return bool(_TOKEN) and hmac.compare_digest(request.headers.get("X-Token", ""), _TOKEN)


# ── HTML ──────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Claude</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/styles/github-dark.min.css">
  <script src="https://cdn.jsdelivr.net/npm/marked@9/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/highlight.min.js" defer></script>
  <script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js" defer></script>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    html,body{height:100%;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f0f;color:#e5e5e5}
    body{display:flex;height:100vh;overflow:hidden}

    /* Auth */
    #auth{position:fixed;inset:0;background:#0f0f0f;display:flex;align-items:center;justify-content:center;z-index:100}
    #auth.hidden{display:none}
    .auth-card{background:#1a1a1a;border-radius:20px;padding:36px 28px;width:360px;display:flex;flex-direction:column;gap:16px}
    .auth-card h2{font-size:20px;font-weight:700;text-align:center}
    .auth-card input{background:#0f0f0f;border:1px solid #2a2a2a;color:#e5e5e5;padding:14px 16px;border-radius:12px;font-size:16px;outline:none;transition:border-color .15s}
    .auth-card input:focus{border-color:#4f46e5}
    .auth-card button{background:#4f46e5;color:#fff;border:none;padding:14px;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer}
    .auth-card .err{color:#f87171;font-size:13px;text-align:center;min-height:18px}

    /* Sidebar */
    #sidebar{width:260px;flex-shrink:0;background:#111;border-right:1px solid #1e1e1e;display:flex;flex-direction:column;overflow:hidden;transition:width .18s ease}
    #sidebar-header{padding:14px;border-bottom:1px solid #1e1e1e;display:flex;align-items:center;gap:8px}
    #new-chat-btn{flex:1;display:flex;align-items:center;justify-content:center;gap:6px;background:#4f46e5;color:#fff;border:none;padding:10px 14px;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;transition:background .15s}
    #new-chat-btn:hover{background:#4338ca}
    #new-chat-btn .icon{font-size:14px;line-height:1}
    #sidebar-toggle{width:32px;height:32px;flex-shrink:0;background:#1a1a1a;border:1px solid #2a2a2a;color:#aaa;border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s}
    #sidebar-toggle:hover{background:#222;color:#fff}
    #sidebar-toggle svg{width:14px;height:14px;transition:transform .18s ease}
    #sidebar-search{padding:6px 10px 2px;position:relative}
    #sidebar.collapsed #sidebar-search{display:none}
    #session-search{width:100%;background:#0f0f0f;border:1px solid #2a2a2a;color:#ccc;padding:7px 28px 7px 10px;border-radius:8px;font-size:12px;outline:none;transition:border-color .15s}
    #session-search:focus{border-color:#4f46e5}
    #session-search::placeholder{color:#444}
    #search-clear{position:absolute;right:16px;top:50%;transform:translateY(-50%);background:none;border:none;color:#555;cursor:pointer;font-size:16px;line-height:1;display:none;transition:color .15s}
    #search-clear:hover{color:#aaa}
    #search-clear.visible{display:block}
    #session-list{flex:1;overflow-y:auto;padding:6px}
    .session-item{display:flex;align-items:center;gap:6px;padding:9px 10px;border-radius:9px;cursor:pointer;transition:background .15s;margin-bottom:2px}
    .session-item:hover{background:#1a1a1a}
    .session-item.active{background:#1e1a2e;border:1px solid #2d2060}
    .session-info{flex:1;overflow:hidden}
    .session-title{font-size:12px;color:#ccc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .session-date{font-size:10px;color:#555;margin-top:2px}
    .session-del{background:none;border:none;color:#3a3a3a;cursor:pointer;padding:2px 6px;border-radius:4px;font-size:13px;flex-shrink:0;line-height:1;transition:color .15s}
    .session-del:hover{color:#f5a623}
    .session-export{background:none;border:none;color:#3a3a3a;cursor:pointer;padding:2px 6px;border-radius:4px;font-size:13px;flex-shrink:0;line-height:1;transition:color .15s}
    .session-export:hover{color:#818cf8}

    /* Archive section */
    #archive-section{border-top:1px solid #1e1e1e;flex-shrink:0}
    #archive-header{padding:8px 10px;display:flex;align-items:center;gap:4px;cursor:pointer;font-size:11px;color:#444;user-select:none;transition:color .15s}
    #archive-header:hover{color:#666}
    #archive-count{background:#2a2a2a;border-radius:10px;padding:0 5px;font-size:10px;color:#555;margin-left:4px}
    #archive-arrow{font-size:9px;margin-left:auto;transition:transform .15s}
    #archive-arrow.open{transform:rotate(90deg)}
    #archive-list{display:none;overflow-y:auto;max-height:200px;padding:4px 6px}
    #archive-list.open{display:block}
    #sidebar.collapsed #archive-section{display:none}
    .session-restore{background:none;border:none;color:#555;cursor:pointer;padding:2px 4px;font-size:12px;flex-shrink:0;transition:color .15s}
    .session-restore:hover{color:#6ee7b7}
    .session-perm-del{background:none;border:none;color:#3a3a3a;cursor:pointer;padding:2px 5px;border-radius:4px;font-size:14px;flex-shrink:0;line-height:1;transition:color .15s}
    .session-perm-del:hover{color:#f87171}

    /* Sidebar collapsed state */
    #sidebar.collapsed{width:56px}
    #sidebar.collapsed #sidebar-header{padding:10px 6px;flex-direction:column;gap:6px}
    #sidebar.collapsed #new-chat-btn{flex:none;width:36px;height:36px;padding:0}
    #sidebar.collapsed #new-chat-btn .label{display:none}
    #sidebar.collapsed #new-chat-btn .icon{font-size:18px}
    #sidebar.collapsed #sidebar-toggle svg{transform:rotate(180deg)}
    #sidebar.collapsed #session-list{display:none}

    /* Main */
    #main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
    #header{padding:12px 20px;border-bottom:1px solid #1e1e1e;display:flex;align-items:center;gap:10px;flex-shrink:0}
    #header .dot{width:8px;height:8px;border-radius:50%;background:#22c55e;flex-shrink:0}
    #header span{font-size:16px;font-weight:600}
    #model{margin-left:auto;background:#1a1a1a;border:1px solid #2a2a2a;color:#aaa;padding:6px 10px;border-radius:8px;font-size:13px;outline:none;cursor:pointer}

    /* Messages */
    #messages{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:12px}
    .msg{max-width:80%}
    .msg.user{align-self:flex-end}
    .msg.assistant{align-self:flex-start}
    .bubble{padding:12px 16px;border-radius:16px;line-height:1.55;white-space:pre-wrap;word-break:break-word;font-size:14px}
    .msg.user .bubble{background:#4f46e5;color:#fff;border-bottom-right-radius:4px}
    .msg.assistant .bubble{background:#1a1a1a;color:#e5e5e5;border-bottom-left-radius:4px}
    .bubble.streaming::after{content:'▋';animation:blink .7s infinite;margin-left:2px}
    @keyframes blink{0%,100%{opacity:1}50%{opacity:0}}

    /* Terminal panel */
    #term-panel{flex-shrink:0;border-top:1px solid #1e1e1e;background:#0a0a0a}
    #term-header{padding:5px 14px;display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none}
    #term-header:hover{background:#111}
    #term-label{font-size:11px;color:#3a3a3a;font-family:monospace;flex:1;transition:color .2s}
    #term-label.active{color:#6ee7b7}
    #term-arrow{font-size:10px;color:#333;transition:transform .15s}
    #term-body{height:150px;overflow-y:auto;padding:6px 14px 8px;font-family:'Menlo','Monaco','Courier New',monospace;font-size:11px;color:#6ee7b7;display:none;line-height:1.5}
    #term-body.open{display:block}
    .tl-tool{color:#818cf8}
    .tl-result{color:#4b5563}
    .tl-other{color:#374151}

    /* Input */
    #footer{padding:12px 20px;border-top:1px solid #1e1e1e;flex-shrink:0;position:relative}
    #slash-picker{position:absolute;bottom:100%;left:20px;right:20px;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;margin-bottom:4px;max-height:260px;overflow-y:auto;display:none;z-index:50;box-shadow:0 -4px 20px rgba(0,0,0,.4)}
    #slash-picker.open{display:block}
    .slash-item{display:flex;align-items:center;gap:10px;padding:8px 12px;cursor:pointer;border-radius:6px;margin:3px;transition:background .1s}
    .slash-item:hover,.slash-item.active{background:#2a2a2a}
    .slash-cmd{font-weight:600;font-size:13px;color:#a5b4fc;font-family:monospace;flex-shrink:0}
    .slash-desc{font-size:12px;color:#6b7280;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
    .slash-badge{font-size:10px;padding:1px 5px;border-radius:4px;background:#1e3a5f;color:#93c5fd;flex-shrink:0}
    #form{display:flex;gap:10px;align-items:flex-end}
    #input{flex:1;background:#1a1a1a;border:1px solid #2a2a2a;color:#e5e5e5;padding:12px 16px;border-radius:14px;font-size:15px;resize:none;outline:none;min-height:48px;max-height:160px;line-height:1.4;font-family:inherit;transition:border-color .15s}
    #input:focus{border-color:#4f46e5}
    #input::placeholder{color:#555}
    #send{background:#4f46e5;border:none;color:#fff;width:44px;height:44px;border-radius:12px;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;transition:background .15s}
    #send:hover{background:#4338ca}
    #send:disabled{opacity:.4;cursor:not-allowed}
    #send svg{width:20px;height:20px;fill:none;stroke:#fff;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}

    /* Attachments */
    #attach-btn{background:#1a1a1a;border:1px solid #2a2a2a;color:#888;width:44px;height:44px;border-radius:12px;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s}
    #attach-btn:hover{background:#222;color:#e5e5e5}
    #attach-btn svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
    #attach-preview{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
    #attach-preview:empty{display:none}
    .attach-chip{display:flex;align-items:center;gap:4px;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:8px;padding:3px 8px 3px 4px;font-size:12px;color:#aaa;max-width:180px}
    .attach-chip img{width:36px;height:36px;object-fit:cover;border-radius:5px;flex-shrink:0}
    .attach-chip .chip-icon{font-size:18px;flex-shrink:0;line-height:1}
    .attach-chip .chip-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:110px}
    .attach-chip .chip-rm{background:none;border:none;color:#555;cursor:pointer;padding:0 2px;font-size:14px;line-height:1;margin-left:2px;flex-shrink:0;transition:color .15s}
    .attach-chip .chip-rm:hover{color:#f87171}
    .attach-chip.uploading{opacity:.55}
    .attach-chip.error{border-color:#7f1d1d;color:#f87171}

    /* Attachment display in bubbles */
    .bubble-attachments{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}
    .bubble-attachments img{max-width:200px;max-height:200px;border-radius:8px;display:block;object-fit:cover}
    .bubble-file-chip{display:inline-flex;align-items:center;gap:4px;background:rgba(255,255,255,.07);border-radius:6px;padding:3px 8px;font-size:12px;color:#aaa}

    /* Output files tray */
    .output-files-tray{border-top:1px solid #2a2a2a;margin-top:8px;padding-top:8px}
    .output-files-tray .tray-label{font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}
    .output-image-row{margin:4px 0}
    .output-image-row img{max-width:100%;border-radius:6px;display:block}
    .file-row{display:flex;align-items:center;gap:8px;padding:3px 0;font-size:13px;color:#9ca3af}
    .file-row .file-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#d1d5db}
    .file-row a.dl-btn{color:#818cf8;text-decoration:none;white-space:nowrap;flex-shrink:0}
    .file-row a.dl-btn:hover{color:#a5b4fc;text-decoration:underline}
    .file-row .del-btn{background:none;border:none;cursor:pointer;color:#4b5563;padding:0;font-size:13px;flex-shrink:0;transition:color .15s}
    .file-row .del-btn:hover{color:#f87171}

    /* Drag-over highlight */
    body.drag-over #messages{outline:2px dashed #4f46e5;outline-offset:-4px}

    /* Markdown rendering */
    .bubble.rendered{white-space:normal}
    .bubble.rendered p{margin:0 0 8px}.bubble.rendered p:last-child{margin-bottom:0}
    .bubble.rendered h1,.bubble.rendered h2,.bubble.rendered h3,.bubble.rendered h4{margin:12px 0 6px;font-weight:700;line-height:1.3}
    .bubble.rendered h1{font-size:1.3em}.bubble.rendered h2{font-size:1.15em}.bubble.rendered h3{font-size:1.05em}
    .bubble.rendered ul,.bubble.rendered ol{margin:4px 0 8px 18px;padding:0}
    .bubble.rendered li{margin:2px 0}
    .bubble.rendered code:not(pre code){background:#2d2d2d;border-radius:4px;padding:1px 5px;font-family:'Menlo','Monaco','Courier New',monospace;font-size:.88em;color:#e2e8f0}
    .bubble.rendered pre{position:relative;margin:8px 0;border-radius:8px;overflow:hidden}
    .bubble.rendered pre code{display:block;overflow-x:auto;padding:12px 14px;font-size:12px;line-height:1.5;background:#0d1117}
    .bubble.rendered blockquote{border-left:3px solid #4f46e5;margin:8px 0;padding:4px 12px;color:#aaa}
    .bubble.rendered table{border-collapse:collapse;margin:8px 0;width:100%;font-size:13px}
    .bubble.rendered th,.bubble.rendered td{border:1px solid #2a2a2a;padding:5px 10px;text-align:left}
    .bubble.rendered th{background:#1e1e1e;font-weight:600}
    .bubble.rendered a{color:#818cf8;text-decoration:none}.bubble.rendered a:hover{text-decoration:underline}
    .bubble.rendered hr{border:none;border-top:1px solid #2a2a2a;margin:10px 0}
    .copy-btn{position:absolute;top:6px;right:6px;background:#2a2a2a;border:1px solid #3a3a3a;color:#aaa;border-radius:5px;padding:2px 8px;font-size:11px;cursor:pointer;opacity:0;transition:opacity .15s,color .15s,border-color .15s}
    .bubble.rendered pre:hover .copy-btn{opacity:1}
    .copy-btn.copied{color:#6ee7b7;border-color:#6ee7b7}
    .mermaid-block{background:#111;border-radius:8px;padding:10px;margin:8px 0;overflow-x:auto;text-align:center}
    .mermaid-block svg{max-width:100%;height:auto}
  </style>
</head>
<body>

  <div id="auth">
    <div class="auth-card">
      <h2>⚡ Claude</h2>
      <input type="password" id="pwd" placeholder="Пароль" autofocus>
      <button id="login-btn">Войти</button>
      <div class="err" id="auth-err"></div>
    </div>
  </div>

  <div id="sidebar">
    <div id="sidebar-header">
      <button id="new-chat-btn" title="Новый чат">
        <span class="icon">+</span><span class="label">Новый чат</span>
      </button>
      <button id="sidebar-toggle" aria-label="Свернуть боковую панель" aria-expanded="true" title="Свернуть (Ctrl/Cmd+B)">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"></polyline></svg>
      </button>
    </div>
    <div id="sidebar-search">
      <input type="text" id="session-search" placeholder="Поиск сессий...">
      <button type="button" id="search-clear" title="Очистить">×</button>
    </div>
    <div id="session-list"></div>
    <div id="archive-section">
      <div id="archive-header">
        <span>Архив</span>
        <span id="archive-count"></span>
        <span id="archive-arrow">▶</span>
      </div>
      <div id="archive-list"></div>
    </div>
  </div>

  <div id="main">
    <div id="header">
      <div class="dot"></div>
      <span>Claude</span>
      <select id="model">
        <option value="claude-sonnet-4-6">Sonnet 4.6</option>
        <option value="claude-opus-4-7">Opus 4.7</option>
        <option value="claude-haiku-4-5-20251001">Haiku 4.5</option>
      </select>
    </div>

    <div id="messages">
      <div class="msg assistant"><div class="bubble">Привет! Чем могу помочь?</div></div>
    </div>

    <div id="term-panel">
      <div id="term-header">
        <span id="term-label">// terminal</span>
        <span id="term-arrow">▶</span>
      </div>
      <div id="term-body"></div>
    </div>

    <div id="footer">
      <div id="slash-picker"></div>
      <input type="file" id="file-input" multiple style="display:none">
      <div id="attach-preview"></div>
      <form id="form">
        <button type="button" id="attach-btn" title="Прикрепить файл (или перетащи / вставь)">
          <svg viewBox="0 0 24 24"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
        </button>
        <textarea id="input" rows="1" placeholder="Напиши сообщение..."></textarea>
        <button id="send" type="submit">
          <svg viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
        </button>
      </form>
    </div>
  </div>

  <script>
    // ── Markdown rendering ─────────────────────────────
    let _mdReady = false;

    function initMarkdown() {
      if (_mdReady) return;
      if (typeof marked === 'undefined') {
        console.debug('[md] marked not loaded yet');
        return;
      }
      const renderer = new marked.Renderer();
      // marked@9: code(code, language, isEscaped)
      renderer.code = function(code, lang) {
        if (lang === 'mermaid') {
          return `<div class="mermaid-block">${code}</div>`;
        }
        if (lang && typeof hljs !== 'undefined' && hljs.getLanguage(lang)) {
          const highlighted = hljs.highlight(code, {language: lang, ignoreIllegals: true}).value;
          return `<pre><code class="hljs language-${lang}">${highlighted}</code></pre>`;
        }
        const highlighted = typeof hljs !== 'undefined'
          ? hljs.highlightAuto(code).value
          : code.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        return `<pre><code class="hljs">${highlighted}</code></pre>`;
      };
      marked.setOptions({ renderer, breaks: true, gfm: true });
      if (typeof mermaid !== 'undefined') {
        mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'loose' });
      }
      _mdReady = true;
      console.debug('[md] markdown renderer initialized');
    }

    // Try on DOMContentLoaded; if CDN was slow, retry lazily in renderMarkdown
    document.addEventListener('DOMContentLoaded', initMarkdown);

    function renderMarkdown(text) {
      if (!_mdReady) initMarkdown();
      if (typeof marked === 'undefined') {
        return text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      }
      try {
        const html = marked.parse(text);
        console.debug('[md] rendered', text.length, '→', html.length, 'chars');
        return html;
      } catch(e) {
        console.debug('[md] render error', e.message, '— falling back to plain text');
        return text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      }
    }

    function injectCopyButtons(container) {
      container.querySelectorAll('pre').forEach(pre => {
        if (pre.querySelector('.copy-btn')) return;
        const code = pre.querySelector('code');
        if (!code) return;
        const btn = document.createElement('button');
        btn.className = 'copy-btn';
        btn.textContent = 'Copy';
        btn.addEventListener('click', async () => {
          try {
            await navigator.clipboard.writeText(code.textContent);
            btn.textContent = '✓ Copied';
            btn.classList.add('copied');
            setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
            console.debug('[md] code copied', code.textContent.length, 'chars');
          } catch(e) {
            btn.textContent = 'Error';
            console.debug('[md] clipboard error', e.message);
          }
        });
        pre.appendChild(btn);
      });
    }

    async function applyMermaid(container) {
      if (typeof mermaid === 'undefined') return;
      const blocks = container.querySelectorAll('.mermaid-block');
      if (!blocks.length) return;
      console.debug('[md] rendering', blocks.length, 'mermaid diagram(s)');
      for (const block of blocks) {
        try {
          const id = 'mermaid-' + Math.random().toString(36).slice(2);
          const src = block.textContent.trim();
          const { svg } = await mermaid.render(id, src);
          block.innerHTML = svg;
        } catch(e) {
          console.debug('[md] mermaid error', e.message);
          block.style.color = '#f87171';
          block.textContent = 'Diagram error: ' + e.message;
        }
      }
    }

    function applyMarkdown(bubble, rawText) {
      if (!rawText) return;
      bubble.innerHTML = renderMarkdown(rawText);
      bubble.classList.add('rendered');
      injectCopyButtons(bubble);
      applyMermaid(bubble);
      console.debug('[md] applyMarkdown done', rawText.length, 'chars');
    }

    // ── App state ──────────────────────────────────────
    const TOKEN_KEY   = 'claude_token';
    const SESSION_KEY = 'claude_session_id';
    const SIDEBAR_KEY = 'claude_sidebar_collapsed';
    let token     = localStorage.getItem(TOKEN_KEY) || '';
    let sessionId = localStorage.getItem(SESSION_KEY) || '';

    const authEl   = document.getElementById('auth');
    const pwdEl    = document.getElementById('pwd');
    const authErr  = document.getElementById('auth-err');
    const messages = document.getElementById('messages');
    const input    = document.getElementById('input');
    const send     = document.getElementById('send');
    const form     = document.getElementById('form');
    const sesList  = document.getElementById('session-list');
    let searchQuery = '';

    function filterSessions() {
      const q = searchQuery.toLowerCase().trim();
      document.getElementById('search-clear').classList.toggle('visible', q.length > 0);
      document.querySelectorAll('#session-list .session-item').forEach(item => {
        const title = (item.dataset.title || '').toLowerCase();
        item.style.display = (!q || title.includes(q)) ? '' : 'none';
      });
      console.debug('[session] filter query=', q);
    }

    document.addEventListener('DOMContentLoaded', () => {
      const searchInput = document.getElementById('session-search');
      const searchClear = document.getElementById('search-clear');
      if (searchInput) {
        searchInput.addEventListener('input', e => { searchQuery = e.target.value; filterSessions(); });
      }
      if (searchClear) {
        searchClear.addEventListener('click', () => {
          searchQuery = '';
          if (searchInput) searchInput.value = '';
          filterSessions();
        });
      }
    });

    const termBody = document.getElementById('term-body');
    const termLbl  = document.getElementById('term-label');
    const termArrow= document.getElementById('term-arrow');
    const sidebar  = document.getElementById('sidebar');
    const sidebarToggle = document.getElementById('sidebar-toggle');

    // ── Sidebar collapse ───────────────────────────────
    function applySidebarState(collapsed) {
      sidebar.classList.toggle('collapsed', collapsed);
      sidebarToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      sidebarToggle.setAttribute('aria-label', collapsed ? 'Развернуть боковую панель' : 'Свернуть боковую панель');
      sidebarToggle.title = collapsed ? 'Развернуть (Ctrl/Cmd+B)' : 'Свернуть (Ctrl/Cmd+B)';
    }

    function toggleSidebar() {
      const collapsed = !sidebar.classList.contains('collapsed');
      applySidebarState(collapsed);
      localStorage.setItem(SIDEBAR_KEY, collapsed ? '1' : '0');
      console.debug('[sidebar] toggled', collapsed);
    }

    applySidebarState(localStorage.getItem(SIDEBAR_KEY) === '1');
    sidebarToggle.addEventListener('click', toggleSidebar);
    window.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'b') {
        if (!authEl.classList.contains('hidden')) return;
        e.preventDefault();
        toggleSidebar();
      }
    });

    // ── Terminal panel ─────────────────────────────────
    document.getElementById('term-header').addEventListener('click', () => {
      const open = termBody.classList.toggle('open');
      termArrow.textContent = open ? '▼' : '▶';
    });

    function termAppend(text, cls) {
      const d = document.createElement('div');
      d.className = 'tl-' + cls;
      d.textContent = text;
      termBody.appendChild(d);
      termBody.scrollTop = termBody.scrollHeight;
      if (!termBody.classList.contains('open')) {
        termBody.classList.add('open');
        termArrow.textContent = '▼';
      }
      termLbl.classList.add('active');
    }

    function termClear() {
      termBody.innerHTML = '';
      termLbl.classList.remove('active');
    }

    // ── Session list ───────────────────────────────────
    async function loadSessions() {
      try {
        const r = await fetch('/claude/sessions', { headers: {'X-Token': token} });
        if (!r.ok) return;
        renderSessions(await r.json());
      } catch(e) {}
    }

    function startRename(sid, titleEl) {
      if (titleEl.querySelector('input')) return;
      const current = titleEl.textContent;
      titleEl.textContent = '';
      const inp = document.createElement('input');
      inp.value = current;
      inp.style.cssText = 'width:100%;background:#0f0f0f;border:1px solid #4f46e5;color:#e5e5e5;border-radius:4px;padding:1px 5px;font-size:12px;outline:none';
      titleEl.appendChild(inp);
      inp.focus();
      inp.select();

      async function commit() {
        const newTitle = inp.value.trim();
        titleEl.textContent = newTitle || current;
        if (newTitle && newTitle !== current) {
          try {
            await fetch(`/claude/sessions/${sid}`, {
              method: 'PATCH',
              headers: {'Content-Type': 'application/json', 'X-Token': token},
              body: JSON.stringify({title: newTitle}),
            });
            titleEl.closest('.session-item').dataset.title = newTitle;
            console.debug('[session] renamed', sid, '→', newTitle);
          } catch(e) {
            titleEl.textContent = current;
            console.debug('[session] rename error', e.message);
          }
        }
      }

      inp.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); commit(); }
        if (e.key === 'Escape') { titleEl.textContent = current; }
      });
      inp.addEventListener('blur', commit);
      inp.addEventListener('click', e => e.stopPropagation());
    }

    function renderSessions(list) {
      sesList.innerHTML = '';
      for (const s of list) {
        const item = document.createElement('div');
        item.className = 'session-item' + (s.session_id === sessionId ? ' active' : '');
        item.dataset.sid   = s.session_id;
        item.dataset.title = s.title || '';
        item.title = s.title || 'Без названия';

        const info = document.createElement('div');
        info.className = 'session-info';
        const title = document.createElement('div');
        title.className = 'session-title';
        title.textContent = s.title || 'Без названия';
        title.addEventListener('dblclick', e => { e.stopPropagation(); startRename(s.session_id, title); });
        const date = document.createElement('div');
        date.className = 'session-date';
        date.textContent = fmtDate(s.updated_at);
        info.append(title, date);

        const del = document.createElement('button');
        del.className = 'session-del';
        del.title = 'Архивировать';
        del.textContent = '🗄';
        del.addEventListener('click', async e => {
          e.stopPropagation();
          await archiveSession(s.session_id);
        });

        const exp = document.createElement('button');
        exp.className = 'session-export';
        exp.title = 'Скачать как Markdown';
        exp.textContent = '↓';
        exp.addEventListener('click', async e => {
          e.stopPropagation();
          await exportSession(s.session_id);
        });

        item.append(info, exp, del);
        item.addEventListener('click', () => openSession(s.session_id));
        sesList.appendChild(item);
      }
      filterSessions();
    }

    function fmtDate(iso) {
      if (!iso) return '';
      const d = new Date(iso + (iso.endsWith('Z') ? '' : 'Z'));
      const diff = Date.now() - d.getTime();
      if (diff < 60000)    return 'только что';
      if (diff < 3600000)  return Math.floor(diff / 60000) + ' мин назад';
      if (diff < 86400000) return Math.floor(diff / 3600000) + ' ч назад';
      return d.toLocaleDateString('ru');
    }

    async function openSession(sid) {
      try {
        const r = await fetch(`/claude/sessions/${sid}`, { headers: {'X-Token': token} });
        if (!r.ok) return;
        const s = await r.json();
        sessionId = sid;
        localStorage.setItem(SESSION_KEY, sid);

        document.querySelectorAll('.session-item').forEach(el =>
          el.classList.toggle('active', el.dataset.sid === sid));

        messages.innerHTML = '';
        for (const m of (s.messages || [])) {
          addMsg(m.role, m.text, m.attachments || [], m.output_files || []);
        }
        if (!s.messages?.length) {
          messages.innerHTML = '<div class="msg assistant"><div class="bubble">Привет! Чем могу помочь?</div></div>';
        }
        messages.scrollTop = messages.scrollHeight;
        termClear();
      } catch(e) {}
    }

    async function archiveSession(sid) {
      try {
        await fetch(`/claude/sessions/${sid}`, { method: 'DELETE', headers: {'X-Token': token} });
        if (sessionId === sid) {
          sessionId = '';
          localStorage.removeItem(SESSION_KEY);
          messages.innerHTML = '<div class="msg assistant"><div class="bubble">Привет! Чем могу помочь?</div></div>';
          termClear();
        }
        console.debug('[session] archived', sid);
        await loadSessions();
        await loadArchivedSessions();
      } catch(e) { console.debug('[session] archive error', e.message); }
    }

    async function loadArchivedSessions() {
      try {
        const r = await fetch('/claude/sessions?archived=true', {headers:{'X-Token':token}});
        if (!r.ok) return;
        const list = await r.json();
        const countEl = document.getElementById('archive-count');
        if (countEl) countEl.textContent = list.length || '';
        renderArchivedSessions(list);
        console.debug('[session] loaded', list.length, 'archived sessions');
      } catch(e) {}
    }

    function renderArchivedSessions(list) {
      const archList = document.getElementById('archive-list');
      if (!archList) return;
      archList.innerHTML = '';
      for (const s of list) {
        const item = document.createElement('div');
        item.className = 'session-item';
        item.dataset.sid = s.session_id;

        const info = document.createElement('div');
        info.className = 'session-info';
        const title = document.createElement('div');
        title.className = 'session-title';
        title.textContent = s.title || 'Без названия';
        const date = document.createElement('div');
        date.className = 'session-date';
        date.textContent = fmtDate(s.updated_at);
        info.append(title, date);

        const restore = document.createElement('button');
        restore.className = 'session-restore';
        restore.title = 'Восстановить';
        restore.textContent = '↺';
        restore.addEventListener('click', async e => {
          e.stopPropagation();
          try {
            await fetch(`/claude/sessions/${s.session_id}`, {
              method: 'PATCH',
              headers: {'Content-Type': 'application/json', 'X-Token': token},
              body: JSON.stringify({archived: false}),
            });
            console.debug('[session] restored', s.session_id);
            await loadSessions();
            await loadArchivedSessions();
          } catch(err) { console.debug('[session] restore error', err.message); }
        });

        const permDel = document.createElement('button');
        permDel.className = 'session-perm-del';
        permDel.title = 'Удалить навсегда';
        permDel.textContent = '×';
        permDel.addEventListener('click', async e => {
          e.stopPropagation();
          if (!confirm('Удалить сессию навсегда?')) return;
          try {
            await fetch(`/claude/sessions/${s.session_id}/permanent`, {method:'DELETE', headers:{'X-Token':token}});
            console.debug('[session] permanently deleted', s.session_id);
            await loadArchivedSessions();
          } catch(err) { console.debug('[session] perm delete error', err.message); }
        });

        item.append(info, restore, permDel);
        archList.appendChild(item);
      }
    }

    document.addEventListener('DOMContentLoaded', () => {
      const archHeader = document.getElementById('archive-header');
      if (archHeader) {
        archHeader.addEventListener('click', () => {
          const archList = document.getElementById('archive-list');
          const archArrow = document.getElementById('archive-arrow');
          const isOpen = archList.classList.toggle('open');
          archArrow.classList.toggle('open', isOpen);
          console.debug('[session] archive drawer toggled', isOpen);
        });
      }
    });

    async function exportSession(sid) {
      try {
        const r = await fetch(`/claude/sessions/${sid}/export`, {headers:{'X-Token': token}});
        if (!r.ok) throw new Error(r.statusText);
        const blob = await r.blob();
        const cd = r.headers.get('Content-Disposition') || '';
        const match = cd.match(/filename="([^"]+)"/);
        const filename = match ? match[1] : `claude_${sid.slice(0,8)}.md`;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = filename;
        document.body.appendChild(a); a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        console.debug('[session] exported', sid, 'as', filename);
      } catch(e) {
        console.debug('[session] export error', e.message);
      }
    }

    // ── Auth ───────────────────────────────────────────
    async function tryLogin() {
      authErr.textContent = '';
      const pwd = pwdEl.value.trim();
      if (!pwd) return;
      try {
        const r = await fetch('/claude/auth', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({password: pwd}),
        });
        const d = await r.json();
        if (r.ok && d.token) {
          token = d.token;
          localStorage.setItem(TOKEN_KEY, token);
          authEl.classList.add('hidden');
          await afterAuth();
        } else {
          authErr.textContent = 'Неверный пароль';
          pwdEl.value = ''; pwdEl.focus();
        }
      } catch(e) { authErr.textContent = 'Ошибка соединения'; }
    }

    async function afterAuth() {
      await loadSessions();
      await loadArchivedSessions();
      loadCommands();
      if (sessionId) await openSession(sessionId);
      input.focus();
    }

    document.getElementById('login-btn').addEventListener('click', tryLogin);
    pwdEl.addEventListener('keydown', e => { if (e.key === 'Enter') tryLogin(); });

    document.getElementById('new-chat-btn').addEventListener('click', () => {
      sessionId = '';
      localStorage.removeItem(SESSION_KEY);
      messages.innerHTML = '<div class="msg assistant"><div class="bubble">Привет! Чем могу помочь?</div></div>';
      termClear();
      document.querySelectorAll('.session-item').forEach(el => el.classList.remove('active'));
      input.focus();
    });

    if (token) {
      fetch('/claude/auth', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token}),
      }).then(r => {
        if (r.ok) { authEl.classList.add('hidden'); afterAuth(); }
        else { localStorage.removeItem(TOKEN_KEY); token = ''; }
      }).catch(() => {});
    }

    // ── Slash command picker ───────────────────────────
    const slashPicker = document.getElementById('slash-picker');
    let slashCommands = [];
    let slashActive = -1;
    let slashMatches = [];

    async function loadCommands() {
      try {
        const r = await fetch('/claude/commands', { headers: {'X-Token': token} });
        if (r.ok) {
          const data = await r.json();
          slashCommands = data.commands || [];
          console.debug('[slash] loaded', slashCommands.length, 'commands');
        }
      } catch(e) { console.debug('[slash] failed to load commands', e.message); }
    }

    function renderSlashPicker(matches) {
      slashPicker.innerHTML = '';
      matches.forEach((cmd, i) => {
        const item = document.createElement('div');
        item.className = 'slash-item' + (i === slashActive ? ' active' : '');
        item.dataset.idx = i;
        const nm = document.createElement('span');
        nm.className = 'slash-cmd';
        nm.textContent = cmd.name;
        const desc = document.createElement('span');
        desc.className = 'slash-desc';
        desc.textContent = cmd.description || '';
        item.appendChild(nm);
        item.appendChild(desc);
        if (cmd.category === 'custom') {
          const badge = document.createElement('span');
          badge.className = 'slash-badge';
          badge.textContent = 'custom';
          item.appendChild(badge);
        }
        item.addEventListener('mousedown', e => {
          e.preventDefault();
          insertSlash(cmd.name);
        });
        slashPicker.appendChild(item);
      });
    }

    function showSlashPicker(matches) {
      slashMatches = matches;
      slashActive = matches.length ? 0 : -1;
      renderSlashPicker(matches);
      slashPicker.classList.add('open');
      console.debug('[slash] open with', matches.length, 'matches');
    }

    function hideSlashPicker() {
      slashPicker.classList.remove('open');
      slashActive = -1;
      slashMatches = [];
      console.debug('[slash] closed');
    }

    function setSlashActive(idx) {
      slashActive = idx;
      renderSlashPicker(slashMatches);
      const activeEl = slashPicker.querySelector('.slash-item.active');
      if (activeEl) activeEl.scrollIntoView({block: 'nearest'});
    }

    function insertSlash(cmdName) {
      const val = input.value;
      const pos = input.selectionStart;
      // Find the start of the current /word at cursor
      let start = pos;
      while (start > 0 && val[start - 1] !== ' ' && val[start - 1] !== '\n') start--;
      const newVal = val.slice(0, start) + cmdName + ' ' + val.slice(pos);
      input.value = newVal;
      const newPos = start + cmdName.length + 1;
      input.setSelectionRange(newPos, newPos);
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 160) + 'px';
      hideSlashPicker();
      console.debug('[slash] inserted', cmdName);
    }

    function updateSlashPicker(val, cursorPos) {
      // Find word at cursor
      let start = cursorPos;
      while (start > 0 && val[start - 1] !== ' ' && val[start - 1] !== '\n') start--;
      const word = val.slice(start, cursorPos);
      if (word.startsWith('/')) {
        const query = word.slice(1).toLowerCase();
        const matches = slashCommands.filter(c => c.name.toLowerCase().startsWith('/' + query));
        if (matches.length) { showSlashPicker(matches); return; }
      }
      hideSlashPicker();
    }

    // Hide picker when clicking outside
    document.addEventListener('click', e => {
      if (!slashPicker.contains(e.target) && e.target !== input) hideSlashPicker();
    });

    // ── Chat ───────────────────────────────────────────
    input.addEventListener('input', () => {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 160) + 'px';
      updateSlashPicker(input.value, input.selectionStart);
    });
    input.addEventListener('keydown', e => {
      if (slashPicker.classList.contains('open')) {
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          setSlashActive((slashActive + 1) % slashMatches.length);
          return;
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          setSlashActive((slashActive - 1 + slashMatches.length) % slashMatches.length);
          return;
        }
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          if (slashActive >= 0 && slashMatches[slashActive]) insertSlash(slashMatches[slashActive].name);
          else hideSlashPicker();
          return;
        }
        if (e.key === 'Escape') { e.preventDefault(); hideSlashPicker(); return; }
        if (e.key === 'Tab') {
          e.preventDefault();
          if (slashActive >= 0 && slashMatches[slashActive]) insertSlash(slashMatches[slashActive].name);
          return;
        }
      }
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.dispatchEvent(new Event('submit')); }
    });

    function formatBytes(n) {
      if (n < 1024) return n + ' B';
      if (n < 1048576) return (n/1024).toFixed(1) + ' KB';
      return (n/1048576).toFixed(1) + ' MB';
    }

    function renderOutputFiles(msgEl, outputFiles) {
      if (!outputFiles || !outputFiles.length) return;
      console.debug('[output] rendering', outputFiles.length, 'output files');
      const tray = document.createElement('div');
      tray.className = 'output-files-tray';
      const label = document.createElement('div');
      label.className = 'tray-label';
      label.textContent = 'Созданные файлы';
      tray.appendChild(label);

      outputFiles.forEach(f => {
        const url = `/claude/workspace/file/${f.rel_path}`;
        if (f.is_image) {
          const row = document.createElement('div');
          row.className = 'output-image-row';
          const img = document.createElement('img');
          img.src = url;
          img.alt = f.name;
          img.loading = 'lazy';
          row.appendChild(img);
          tray.appendChild(row);
        }
        const row = document.createElement('div');
        row.className = 'file-row';
        const nm = document.createElement('span');
        nm.className = 'file-name';
        nm.title = f.rel_path;
        nm.textContent = f.name;
        const sz = document.createElement('span');
        sz.textContent = formatBytes(f.size || 0);
        const dl = document.createElement('a');
        dl.className = 'dl-btn';
        dl.href = url;
        dl.download = f.name;
        dl.textContent = '⬇ Скачать';
        const del = document.createElement('button');
        del.className = 'del-btn';
        del.title = 'Удалить файл';
        del.textContent = '🗑';
        del.addEventListener('click', async () => {
          try {
            const r = await fetch(`/claude/workspace/file/${f.rel_path}`, {
              method: 'DELETE', headers: {'X-Token': token}
            });
            if (r.ok) {
              row.style.opacity = '0.4';
              row.style.textDecoration = 'line-through';
              del.disabled = true;
              console.debug('[output] deleted', f.rel_path);
            }
          } catch(e) { console.debug('[output] delete error', e.message); }
        });
        row.appendChild(nm);
        row.appendChild(sz);
        row.appendChild(dl);
        row.appendChild(del);
        tray.appendChild(row);
      });

      const bubble = msgEl.querySelector('.bubble');
      if (bubble) bubble.after(tray);
      else msgEl.appendChild(tray);
    }

    function addMsg(role, text = '', attachments = [], outputFiles = []) {
      const div = document.createElement('div');
      div.className = `msg ${role}`;
      if (attachments.length) {
        console.debug('[msg] rendering', attachments.length, 'attachments');
        const row = document.createElement('div');
        row.className = 'bubble-attachments';
        attachments.forEach(a => {
          if (a.is_image && (a.localUrl || a.id)) {
            const img = document.createElement('img');
            img.src = a.localUrl || `/claude/files/${a.id}`;
            img.alt = a.name;
            img.loading = 'lazy';
            row.appendChild(img);
          } else {
            const chip = document.createElement('div');
            chip.className = 'bubble-file-chip';
            chip.textContent = '📄 ' + a.name;
            row.appendChild(chip);
          }
        });
        div.appendChild(row);
      }
      const b = document.createElement('div');
      b.className = 'bubble';
      if (role === 'assistant' && text) {
        applyMarkdown(b, text);
        console.debug('[md] addMsg rendered', text.length, 'chars');
      } else {
        b.textContent = text;
      }
      div.appendChild(b);
      if (role === 'assistant' && outputFiles.length) {
        renderOutputFiles(div, outputFiles);
      }
      messages.appendChild(div);
      messages.scrollTop = messages.scrollHeight;
      return b;
    }

    // ── Attachments ───────────────────────────────────────
    let pendingAttachments = [];
    const attachPreview = document.getElementById('attach-preview');
    const fileInput     = document.getElementById('file-input');

    function renderAttachChip(a) {
      const chip = document.createElement('div');
      chip.className = 'attach-chip uploading';
      chip.dataset.id = a.clientId;
      if (a.is_image && a.localUrl) {
        const img = document.createElement('img');
        img.src = a.localUrl;
        chip.appendChild(img);
      } else {
        const ic = document.createElement('span');
        ic.className = 'chip-icon';
        ic.textContent = '📄';
        chip.appendChild(ic);
      }
      const nm = document.createElement('span');
      nm.className = 'chip-name';
      nm.textContent = a.name;
      chip.appendChild(nm);
      const rm = document.createElement('button');
      rm.className = 'chip-rm';
      rm.type = 'button';
      rm.textContent = '×';
      rm.onclick = () => removeAttachment(a.clientId);
      chip.appendChild(rm);
      attachPreview.appendChild(chip);
      return chip;
    }

    function removeAttachment(clientId) {
      pendingAttachments = pendingAttachments.filter(a => a.clientId !== clientId);
      const chip = attachPreview.querySelector(`[data-id="${clientId}"]`);
      if (chip) chip.remove();
      console.debug('[attach] removed', clientId, 'remaining', pendingAttachments.length);
    }

    function clearAttachments() {
      pendingAttachments = [];
      attachPreview.innerHTML = '';
    }

    async function handleFiles(fileList) {
      const files = Array.from(fileList);
      if (!files.length) return;
      console.debug('[attach] uploading', files.length, 'files');
      for (const file of files) {
        const clientId = Math.random().toString(36).slice(2);
        const isImage  = file.type.startsWith('image/');
        const localUrl = isImage ? URL.createObjectURL(file) : null;
        const pending  = { clientId, name: file.name, is_image: isImage, localUrl, path: null, id: null };
        pendingAttachments.push(pending);
        const chip = renderAttachChip(pending);
        try {
          const fd = new FormData();
          fd.append('files', file);
          const r = await fetch('/claude/upload', { method: 'POST', headers: { 'X-Token': token }, body: fd });
          if (!r.ok) throw new Error(await r.text());
          const d = await r.json();
          const saved = d.files[0];
          pending.path = saved.path;
          pending.id   = saved.id;
          chip.classList.remove('uploading');
          console.debug('[attach] uploaded', saved.id);
        } catch(err) {
          chip.classList.remove('uploading');
          chip.classList.add('error');
          chip.title = 'Ошибка загрузки: ' + err.message;
          pending.error = true;
          console.debug('[attach] upload error', err.message);
        }
      }
    }

    document.getElementById('attach-btn').addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', e => { handleFiles(e.target.files); e.target.value = ''; });

    document.addEventListener('dragover', e => { e.preventDefault(); document.body.classList.add('drag-over'); });
    document.addEventListener('dragleave', e => { if (!e.relatedTarget) document.body.classList.remove('drag-over'); });
    document.addEventListener('drop', e => {
      e.preventDefault();
      document.body.classList.remove('drag-over');
      if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
    });
    document.addEventListener('paste', e => {
      if (e.clipboardData.files.length) { e.preventDefault(); handleFiles(e.clipboardData.files); }
    });

    // ── Chat ───────────────────────────────────────────
    form.addEventListener('submit', async e => {
      e.preventDefault();
      const prompt = input.value.trim();
      const readyAttachments = pendingAttachments.filter(a => a.path && !a.error);
      if (!prompt && !readyAttachments.length || send.disabled) return;

      const sentAttachments = [...readyAttachments];
      addMsg('user', prompt, sentAttachments);
      clearAttachments();
      input.value = '';
      input.style.height = 'auto';
      send.disabled = true;
      termClear();

      const bubble = addMsg('assistant', '');
      bubble.classList.add('streaming');

      try {
        const model = document.getElementById('model').value;
        const res = await fetch('/claude/ask', {
          method: 'POST',
          headers: {'Content-Type': 'application/json', 'X-Token': token},
          body: JSON.stringify({
            prompt,
            model,
            session_id: sessionId,
            attachments: sentAttachments.map(a => ({path: a.path, name: a.name, is_image: a.is_image})),
          }),
        });
        if (res.status === 401) {
          bubble.textContent = '🔒 Сессия истекла, перезагрузи страницу';
          bubble.classList.remove('streaming');
          send.disabled = false;
          return;
        }

        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        let streamDone = false;
        let rawText = '';

        try {
          while (!streamDone) {
            const {done, value} = await reader.read();
            if (done) break;
            buf += dec.decode(value, {stream: true});
            const lines = buf.split('\n');
            buf = lines.pop();
            for (const line of lines) {
              if (!line.startsWith('data: ')) continue;
              try {
                const data = JSON.parse(line.slice(6));
                if (data.done) { streamDone = true; break; }
                if (data.session_id) {
                  console.debug('[stream] got session_id', data.session_id);
                  sessionId = data.session_id;
                  localStorage.setItem(SESSION_KEY, sessionId);
                }
                if (data.text) {
                  console.debug('[stream] delta', data.text.length, 'chars');
                  rawText += data.text;
                  bubble.textContent += data.text;
                  messages.scrollTop = messages.scrollHeight;
                }
                if (data.terminal) {
                  const cls = data.terminal.startsWith('⚡') ? 'tool'
                            : data.terminal.startsWith('←') ? 'result' : 'other';
                  termAppend(data.terminal, cls);
                }
                if (data.output_files && data.output_files.length) {
                  console.debug('[stream] output_files', data.output_files.length);
                  renderOutputFiles(bubble.parentElement, data.output_files);
                }
              } catch(_) {}
            }
          }
        } finally {
          bubble.classList.remove('streaming');
          if (rawText) {
            applyMarkdown(bubble, rawText);
            console.debug('[md] post-stream render applied');
          }
        }
      } catch(err) {
        bubble.textContent = '❌ Ошибка: ' + err.message;
        bubble.classList.remove('streaming');
      }

      send.disabled = false;
      input.focus();
      await loadSessions();
    });
  </script>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────

@app.get("/claude")
@app.get("/claude/")
async def index():
    return HTMLResponse(HTML)


@app.post("/claude/auth")
async def auth(request: Request):
    body = await request.json()
    if "password" in body:
        if APP_PASSWORD and hmac.compare_digest(body["password"], APP_PASSWORD):
            return JSONResponse({"token": _TOKEN})
        return JSONResponse({"error": "wrong password"}, status_code=401)
    if "token" in body:
        if _TOKEN and hmac.compare_digest(body["token"], _TOKEN):
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "invalid token"}, status_code=401)
    return JSONResponse({"error": "bad request"}, status_code=400)


@app.get("/claude/sessions")
async def list_sessions(request: Request, archived: bool = False):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sessions = _load_sessions()
    filtered = [s for s in sessions if bool(s.get("archived", False)) == archived]
    print(f"[DEBUG sessions] list archived={archived}: {len(filtered)} sessions", flush=True)
    return JSONResponse([{k: v for k, v in s.items() if k != "messages"} for s in filtered])


@app.get("/claude/sessions/{session_id}")
async def get_session(session_id: str, request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    for s in _load_sessions():
        if s["session_id"] == session_id:
            return JSONResponse(s)
    return JSONResponse({"error": "not found"}, status_code=404)


@app.delete("/claude/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sessions = _load_sessions()
    now = datetime.utcnow().isoformat()
    for s in sessions:
        if s["session_id"] == session_id:
            s["archived"] = True
            s["updated_at"] = now
            _write_sessions(sessions)
            print(f"[INFO sessions] archived sid={session_id}", flush=True)
            return JSONResponse({"ok": True, "archived": True})
    return JSONResponse({"error": "not found"}, status_code=404)


@app.delete("/claude/sessions/{session_id}/permanent")
async def permanent_delete_session(session_id: str, request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sessions = _load_sessions()
    new_sessions = [s for s in sessions if s["session_id"] != session_id]
    if len(new_sessions) == len(sessions):
        return JSONResponse({"error": "not found"}, status_code=404)
    _write_sessions(new_sessions)
    print(f"[INFO sessions] permanently deleted sid={session_id}", flush=True)
    return JSONResponse({"ok": True})


@app.patch("/claude/sessions/{session_id}")
async def update_session(session_id: str, request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    sessions = _load_sessions()
    now = datetime.utcnow().isoformat()
    for s in sessions:
        if s["session_id"] == session_id:
            if "title" in body:
                s["title"] = (str(body["title"]) or "")[:80]
                s["updated_at"] = now
                print(f"[INFO sessions] renamed sid={session_id} title={s['title'][:30]}", flush=True)
            if "archived" in body:
                s["archived"] = bool(body["archived"])
                s["updated_at"] = now
                print(f"[INFO sessions] set archived={s['archived']} sid={session_id}", flush=True)
            _write_sessions(sessions)
            return JSONResponse({k: v for k, v in s.items() if k != "messages"})
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/claude/sessions/{session_id}/export")
async def export_session(session_id: str, request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from fastapi.responses import Response as FResponse
    for s in _load_sessions():
        if s["session_id"] == session_id:
            lines = [f"# {s.get('title', 'Session')}", ""]
            lines.append(f"_Exported: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}_")
            lines.append("")
            for m in s.get("messages", []):
                lines.append("---")
                lines.append("")
                label = "**You:**" if m["role"] == "user" else "**Claude:**"
                lines.append(f"{label} {m.get('text', '')}")
                for a in m.get("attachments", []):
                    lines.append(f"  - Attachment: {a.get('name', '')} (`{a.get('path', '')}`)")
                lines.append("")
            md = "\n".join(lines)
            safe_title = re.sub(r"[^\w\s-]", "", s.get("title", "session"))[:40].strip().replace(" ", "_")
            filename = f"claude_{safe_title or session_id[:8]}.md"
            print(f"[DEBUG sessions] export sid={session_id} messages={len(s.get('messages', []))} file={filename}", flush=True)
            return FResponse(
                content=md,
                media_type="text/markdown",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/claude/commands")
async def get_commands(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    commands = _load_commands()
    print(f"[DEBUG commands] serving {len(commands)} commands", flush=True)
    return JSONResponse({"commands": commands})


@app.post("/claude/upload")
async def upload_files(request: Request, files: list[UploadFile] = File(default=[])):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not files:
        return JSONResponse({"error": "no files provided"}, status_code=400)

    batch_id = uuid.uuid4().hex[:12]
    batch_dir = UPLOAD_DIR / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    print(f"[DEBUG upload] batch={batch_id} files={len(files)}", flush=True)

    saved = []
    for f in files:
        data = await f.read()
        print(f"[DEBUG upload] received file={f.filename} size={len(data)} mime={f.content_type}", flush=True)
        if len(data) > MAX_UPLOAD_BYTES:
            return JSONResponse(
                {"error": f"{f.filename}: exceeds 20 MB limit"},
                status_code=413,
            )
        safe_name = _safe_filename(f.filename or "upload")
        dest = batch_dir / safe_name
        dest.write_bytes(data)
        print(f"[DEBUG upload] saved to {dest}", flush=True)
        is_image = (f.content_type or "").startswith("image/")
        saved.append({
            "id":        f"{batch_id}/{safe_name}",
            "name":      f.filename or safe_name,
            "path":      str(dest),
            "mime_type": f.content_type or "application/octet-stream",
            "is_image":  is_image,
            "size":      len(data),
        })

    print(f"[INFO upload] batch={batch_id} saved {len(saved)} file(s)", flush=True)
    return JSONResponse({"files": saved})


@app.get("/claude/files/{file_path:path}")
async def serve_file(file_path: str, request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    dest = UPLOAD_DIR / file_path
    if not dest.exists() or not dest.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    # Prevent path traversal
    try:
        dest.resolve().relative_to(UPLOAD_DIR.resolve())
    except ValueError:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    print(f"[DEBUG files] serving {dest}", flush=True)
    return FileResponse(str(dest))


@app.get("/claude/workspace/file/{file_path:path}")
async def serve_workspace_file(file_path: str, request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    dest = (WORKSPACE_DIR / file_path).resolve()
    try:
        dest.relative_to(WORKSPACE_DIR.resolve())
    except ValueError:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not dest.exists() or not dest.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    print(f"[DEBUG workspace] serving {dest}", flush=True)
    return FileResponse(str(dest))


@app.delete("/claude/workspace/file/{file_path:path}")
async def delete_workspace_file(file_path: str, request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    dest = (WORKSPACE_DIR / file_path).resolve()
    try:
        dest.relative_to(WORKSPACE_DIR.resolve())
    except ValueError:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not dest.exists() or not dest.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    size = dest.stat().st_size
    dest.unlink()
    print(f"[DEBUG workspace] deleted {dest} ({size}b)", flush=True)
    return JSONResponse({"deleted": file_path})


async def _anthropic_stream(prompt: str, augmented: str, image_attachments: list,
                            model: str, session_id: str, attachments: list):
    """Async SSE generator for multimodal messages.
    Uses claude CLI with --input-format stream-json so it can use its own credentials.
    """
    ws_before = _snapshot_workspace()

    content: list = [{"type": "text", "text": augmented}]
    for a in image_attachments:
        fpath = Path(a.get("path", ""))
        if fpath.exists() and fpath.is_file():
            try:
                b64 = base64.standard_b64encode(fpath.read_bytes()).decode()
                mime = a.get("mime_type", "image/jpeg")
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": mime, "data": b64,
                }})
                print(f"[DEBUG multimodal] encoded {fpath.name} ({len(b64)} b64 chars)", flush=True)
            except Exception as e:
                print(f"[WARN multimodal] failed to encode {fpath}: {e}", flush=True)

    # Pass message as stream-json event via stdin; claude CLI uses its own credentials
    stdin_data = (json.dumps({
        "type": "user",
        "message": {"role": "user", "content": content},
    }) + "\n").encode()

    env = {**os.environ, "HOME": "/home/node"}
    cmd = ["claude", "--model", model,
           "--dangerously-skip-permissions", "--max-turns", "20",
           "--output-format", "stream-json", "--verbose",
           "--input-format", "stream-json"]
    if session_id:
        cmd += ["--resume", session_id]

    print(f"[DEBUG multimodal] launching CLI content_parts={len(content)}", flush=True)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    proc.stdin.write(stdin_data)
    await proc.stdin.drain()
    proc.stdin.close()

    final_sid = session_id
    parts: list[str] = []
    result_text: str | None = None  # type: ignore[assignment]
    is_error = False
    q: asyncio.Queue = asyncio.Queue()

    async def _stderr():
        async for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                await q.put(("t", line))
        await q.put(("t_done", ""))

    async def _stdout():
        prev_len = 0
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            etype = ev.get("type", "")
            if etype == "system":
                sid = ev.get("session_id", "")
                if sid:
                    await q.put(("sid", sid))
            elif etype == "assistant":
                txt = ""
                for c in ev.get("message", {}).get("content", []):
                    if isinstance(c, dict) and c.get("type") == "text":
                        txt = c.get("text", "")
                delta = txt[prev_len:]
                if delta:
                    await q.put(("delta", delta))
                    prev_len = len(txt)
            elif etype == "result":
                await q.put(("result", {
                    "text": ev.get("result", ""),
                    "sid": ev.get("session_id", ""),
                    "is_error": ev.get("is_error", False),
                }))
        await q.put(("out_done", ""))

    asyncio.create_task(_stderr())
    asyncio.create_task(_stdout())

    t_done = out_done = False
    while not (t_done and out_done):
        kind, val = await q.get()
        if kind == "t":
            yield f"data: {json.dumps({'terminal': val})}\n\n"
        elif kind == "t_done":
            t_done = True
        elif kind == "sid":
            final_sid = val
            yield f"data: {json.dumps({'session_id': val})}\n\n"
        elif kind == "delta":
            parts.append(val)
            yield f"data: {json.dumps({'text': val})}\n\n"
        elif kind == "result":
            result_text = val["text"]
            is_error = val.get("is_error", False)
            if val["sid"] and not final_sid:
                final_sid = val["sid"]
                yield f"data: {json.dumps({'session_id': val['sid']})}\n\n"
        elif kind == "out_done":
            out_done = True

    await proc.wait()

    ws_after = _snapshot_workspace()
    output_files = _diff_workspace(ws_before, ws_after)
    if output_files:
        yield f"data: {json.dumps({'output_files': output_files})}\n\n"
        print(f"[INFO multimodal] {len(output_files)} output file(s) detected", flush=True)

    yield f"data: {json.dumps({'done': True})}\n\n"

    assistant_text = (("[ERROR] " if is_error else "") + result_text) if result_text is not None else "".join(parts)
    if not final_sid:
        final_sid = f"img-{uuid.uuid4().hex[:12]}"
    _upsert_session(final_sid, prompt, assistant_text, attachments=attachments, output_files=output_files or None)
    print(f"[INFO multimodal] session saved sid={final_sid} text_len={len(assistant_text)}", flush=True)


@app.post("/claude/ask")
async def ask(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body        = await request.json()
    prompt      = (body.get("prompt") or "").strip()
    model       = (body.get("model") or "claude-sonnet-4-6").strip()
    session_id  = (body.get("session_id") or "").strip()
    attachments = body.get("attachments") or []
    if model not in {"claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5-20251001"}:
        model = "claude-sonnet-4-6"
    if not prompt:
        return JSONResponse({"error": "empty prompt"})

    # Separate image attachments to pass as multimodal base64 via stdin
    image_attachments = [a for a in attachments if a.get("is_image")]
    text_attachments  = [a for a in attachments if not a.get("is_image")]

    # For text-only: augment prompt with all file paths
    # For multimodal: augment prompt with text file paths only (images go as base64)
    augmented = _build_prompt(prompt, attachments if not image_attachments else text_attachments)
    print(f"[DEBUG ask] attachments={len(attachments)} images={len(image_attachments)} augmented_len={len(augmented)}", flush=True)

    # Build multimodal stdin payload when images are present
    async def stream():
        env = {**os.environ, "HOME": "/home/node"}

        ws_before = _snapshot_workspace()

        # ── Text-only path — use claude CLI ────────────────────────────────
        cmd = ["claude", "-p", augmented, "--model", model,
               "--dangerously-skip-permissions", "--max-turns", "20",
               "--output-format", "stream-json", "--verbose"]
        if session_id:
            cmd += ["--resume", session_id]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        final_sid = session_id
        parts: list[str] = []
        result_text: str | None = None
        is_error: bool = False
        q: asyncio.Queue = asyncio.Queue()

        async def _stderr_reader():
            async for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    await q.put(("t", line))
            await q.put(("t_done", ""))

        async def _stdout_line_reader():
            prev_len = 0
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    print(f"[DEBUG stream] stdout line unparseable: {line[:80]}", flush=True)
                    continue
                etype = event.get("type", "")
                print(f"[DEBUG stream] event type={etype}", flush=True)
                if etype == "system":
                    sid = event.get("session_id", "")
                    if sid:
                        await q.put(("sid", sid))
                elif etype == "assistant":
                    msg = event.get("message", {})
                    content = msg.get("content", [])
                    full_text = ""
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            full_text = c.get("text", "")
                    delta = full_text[prev_len:]
                    if delta:
                        print(f"[DEBUG stream] delta len={len(delta)}", flush=True)
                        await q.put(("delta", delta))
                        prev_len = len(full_text)
                elif etype == "result":
                    await q.put(("result", {
                        "text":     event.get("result", ""),
                        "sid":      event.get("session_id", ""),
                        "is_error": event.get("is_error", False),
                    }))
            await q.put(("out_done", ""))

        asyncio.create_task(_stderr_reader())
        asyncio.create_task(_stdout_line_reader())

        t_done = False
        out_done = False
        while not (t_done and out_done):
            kind, val = await q.get()
            if kind == "t":
                yield f"data: {json.dumps({'terminal': val})}\n\n"
            elif kind == "t_done":
                t_done = True
            elif kind == "sid":
                final_sid = val
                yield f"data: {json.dumps({'session_id': val})}\n\n"
            elif kind == "delta":
                parts.append(val)
                yield f"data: {json.dumps({'text': val})}\n\n"
            elif kind == "result":
                result_text = val["text"]
                is_error = val.get("is_error", False)
                if val["sid"] and not final_sid:
                    final_sid = val["sid"]
                    yield f"data: {json.dumps({'session_id': val['sid']})}\n\n"
            elif kind == "out_done":
                out_done = True

        await proc.wait()

        ws_after = _snapshot_workspace()
        output_files = _diff_workspace(ws_before, ws_after)
        if output_files:
            yield f"data: {json.dumps({'output_files': output_files})}\n\n"
            print(f"[INFO stream] {len(output_files)} output file(s) detected", flush=True)

        yield f"data: {json.dumps({'done': True})}\n\n"

        if result_text is not None:
            assistant_text = ("[ERROR] " if is_error else "") + result_text
        else:
            assistant_text = "".join(parts)

        # Fallback: if Claude crashed before system event, generate a local session_id
        # so the user's message is not completely lost
        if not final_sid and (prompt or attachments):
            final_sid = f"local-{uuid.uuid4().hex[:12]}"
            print(f"[WARN stream] no session_id received, using fallback sid={final_sid}", flush=True)

        if final_sid:
            _upsert_session(final_sid, prompt, assistant_text, attachments=attachments,
                            output_files=output_files or None)
            print(f"[INFO stream] session saved sid={final_sid} text_len={len(assistant_text)} is_error={is_error} attachments={len(attachments)}", flush=True)
        asyncio.create_task(_git_push(env))

    if image_attachments:
        return StreamingResponse(
            _anthropic_stream(prompt, augmented, image_attachments, model,
                              session_id, attachments),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
