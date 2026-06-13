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
# Multi-user: USERS=alice:pass1,bob:pass2  (overrides APP_PASSWORD when set)
_USERS: dict = {}
_raw_users = os.environ.get("USERS", "").strip()
if _raw_users:
    for _entry in _raw_users.split(","):
        _entry = _entry.strip()
        if ":" in _entry:
            _u, _, _p = _entry.partition(":")
            if _u.strip():
                _USERS[_u.strip()] = _p.strip()
    print(f"[INFO auth] multi-user mode: {list(_USERS.keys())}", flush=True)
UPLOAD_DIR       = Path(os.environ.get("UPLOAD_DIR", "/home/node/workspace/.uploads"))
WORKSPACE_DIR    = Path(os.environ.get("WORKSPACE_DIR", "/home/node/workspace"))
COMMANDS_DIR     = Path(os.environ.get("COMMANDS_DIR", "/home/node/.claude/commands"))
TEMPLATES_FILE   = Path(os.environ.get("TEMPLATES_FILE", "/home/node/templates.json"))
# Colon-separated list of skill directories to scan
SKILLS_DIRS      = [Path(p) for p in os.environ.get(
    "SKILLS_DIRS",
    "/home/node/.claude/skills:/app/.claude/skills"
).split(":") if p]
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
_CACHE_TTL = 30  # seconds — commands/skills cache TTL
_commands_cache: tuple = ([], 0.0)
_skills_cache: tuple    = ([], 0.0)
# Active subprocess registry: stream_id → asyncio.subprocess.Process
_active_streams: dict = {}


def _parse_skill_frontmatter(text: str) -> dict:
    """Extract YAML-like frontmatter from skill SKILL.md."""
    meta: dict = {}
    if not text.startswith("---"):
        # No frontmatter — try to grab first heading as description
        for line in text.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                meta["description"] = line[:200]
                break
        return meta
    end = text.find("\n---", 3)
    block = text[3:end] if end != -1 else text[3:]
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip().strip('"').strip("'")
        if key in ("name", "description", "argument-hint", "trigger"):
            meta[key] = val[:300]
    return meta


def _load_skills() -> list:
    """Scan SKILLS_DIRS for skill subdirectories with SKILL.md files. Results cached for _CACHE_TTL seconds."""
    import time
    global _skills_cache
    cached, ts = _skills_cache
    if cached and (time.monotonic() - ts) < _CACHE_TTL:
        return cached
    seen: set = set()
    result = []
    for base in SKILLS_DIRS:
        if not base.exists():
            print(f"[DEBUG skills] dir not found: {base}", flush=True)
            continue
        try:
            for skill_dir in sorted(base.iterdir()):
                if not skill_dir.is_dir():
                    continue
                skill_md = skill_dir / "SKILL.md"
                if not skill_md.exists():
                    continue
                try:
                    text = skill_md.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                meta = _parse_skill_frontmatter(text)
                name = meta.get("name") or skill_dir.name
                if name in seen:
                    continue
                seen.add(name)
                result.append({
                    "name": name,
                    "trigger": meta.get("trigger") or f"/{name}",
                    "description": meta.get("description") or "",
                    "argument_hint": meta.get("argument-hint") or "",
                    "source": str(base.name),
                })
        except Exception as e:
            print(f"[WARN skills] failed to scan {base}: {e}", flush=True)
    print(f"[DEBUG skills] found {len(result)} skill(s) across {len(SKILLS_DIRS)} dir(s)", flush=True)
    import time
    _skills_cache = (result, time.monotonic())
    return result

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
    """Return built-in commands + custom commands from COMMANDS_DIR. Results cached for _CACHE_TTL seconds."""
    import time
    global _commands_cache
    cached, ts = _commands_cache
    if cached and (time.monotonic() - ts) < _CACHE_TTL:
        return cached
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
    import time
    _commands_cache = (result, time.monotonic())
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

def _load_sessions(sf: Optional[Path] = None) -> list:
    f = sf or SESSIONS_FILE
    try:
        return json.loads(f.read_text()) if f.exists() else []
    except Exception:
        return []

def _write_sessions(sessions: list, sf: Optional[Path] = None):
    f = sf or SESSIONS_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(sessions, ensure_ascii=False, indent=2))

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
                    output_files: Optional[list] = None,
                    sf: Optional[Path] = None,
                    usage: Optional[dict] = None):
    sessions = _load_sessions(sf)
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
            if usage:
                s["last_usage"] = usage
            _write_sessions(sessions, sf)
            return
    title = user_msg[:60] + ("…" if len(user_msg) > 60 else "")
    entry: dict = {
        "session_id": session_id,
        "title":      title,
        "created_at": now,
        "updated_at": now,
        "messages": [user_record, assistant_record],
    }
    if usage:
        entry["last_usage"] = usage
    sessions.insert(0, entry)
    _write_sessions(sessions, sf)


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

def _make_user_token(username: str, password: str) -> str:
    return hmac.new(b"claude-ui-user", f"{username}:{password}".encode(), hashlib.sha256).hexdigest()


def _authorized_user(request: Request) -> Optional[str]:
    """Return username (str) if authorized, None otherwise."""
    tok = request.headers.get("X-Token", "")
    if not tok:
        return None
    if _USERS:
        # Multi-user: token = "username:hmachex"
        if ":" not in tok:
            return None
        username, _, tok_hex = tok.partition(":")
        pwd = _USERS.get(username)
        if pwd is None:
            return None
        expected = _make_user_token(username, pwd)
        return username if hmac.compare_digest(tok_hex, expected) else None
    # Single-user fallback
    if _TOKEN and hmac.compare_digest(tok, _TOKEN):
        return "default"
    return None


def _authorized(request: Request) -> bool:
    return _authorized_user(request) is not None


def _sessions_file(username: str = "default") -> Path:
    """Return the sessions file path for the given user."""
    if not _USERS or username == "default":
        return SESSIONS_FILE
    safe = re.sub(r"[^\w\-]", "_", username)
    return SESSIONS_FILE.parent / f"sessions_{safe}.json"


def _templates_file(username: str = "default") -> Path:
    if not _USERS or username == "default":
        return TEMPLATES_FILE
    safe = re.sub(r"[^\w\-]", "_", username)
    return TEMPLATES_FILE.parent / f"templates_{safe}.json"


def _load_templates(tf: Optional[Path] = None) -> list:
    f = tf or TEMPLATES_FILE
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    except Exception:
        return []


def _write_templates(templates: list, tf: Optional[Path] = None):
    f = tf or TEMPLATES_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(templates, ensure_ascii=False, indent=2))


# ── HTML ──────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Claude</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/styles/github-dark.min.css">
  <script src="https://cdn.jsdelivr.net/npm/marked@9/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/highlight.min.js" defer></script>
  <script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js" defer></script>
  <script>
    (function(){
      const t = localStorage.getItem('theme') ||
        (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
      document.documentElement.setAttribute('data-theme', t);
    })();
  </script>
  <style>
    /* ── Theme variables ─────────────────────────────── */
    :root,[data-theme="dark"]{
      --bg:#0f0f0f;--bg2:#141414;--bg3:#1a1a1a;--bg4:#212121;--bg5:#2a2a2a;
      --border:#1e1e1e;--border2:#2a2a2a;
      --text:#e5e5e5;--text2:#9ca3af;--text3:#6b7280;--text4:#4b5563;
      --accent:#4f46e5;--accent-h:#4338ca;--accent-glow:rgba(79,70,229,.18);
      --user-bg:#4f46e5;--user-text:#fff;
      --code-bg:#0d1117;--inline-code:#2d2d2d;--inline-code-text:#e2e8f0;
      --shadow:rgba(0,0,0,.5);--shadow-sm:rgba(0,0,0,.3);
      --term-bg:#0a0a0a;--term-text:#6ee7b7;
      --green:#22c55e;--red:#f87171;--amber:#f5a623;--purple:#818cf8;
      color-scheme:dark;
    }
    [data-theme="light"]{
      --bg:#f5f5f7;--bg2:#fff;--bg3:#f0f0f2;--bg4:#e8e8ec;--bg5:#d8d8de;
      --border:#e0e0e6;--border2:#d0d0d8;
      --text:#1a1a2e;--text2:#4a4a6a;--text3:#7a7a9a;--text4:#ababc0;
      --accent:#4f46e5;--accent-h:#4338ca;--accent-glow:rgba(79,70,229,.1);
      --user-bg:#4f46e5;--user-text:#fff;
      --code-bg:#1e1e2e;--inline-code:#e8e8f0;--inline-code-text:#3a3a5a;
      --shadow:rgba(0,0,0,.12);--shadow-sm:rgba(0,0,0,.06);
      --term-bg:#1a1a2e;--term-text:#6ee7b7;
      --green:#16a34a;--red:#dc2626;--amber:#d97706;--purple:#6d28d9;
      color-scheme:light;
    }

    /* ── Base ────────────────────────────────────────── */
    *{box-sizing:border-box;margin:0;padding:0}
    html,body{height:100%;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text)}
    body{display:flex;height:100dvh;overflow:hidden}
    *::-webkit-scrollbar{width:4px;height:4px}
    *::-webkit-scrollbar-track{background:transparent}
    *::-webkit-scrollbar-thumb{background:var(--bg5);border-radius:4px}
    *::-webkit-scrollbar-thumb:hover{background:var(--text4)}

    /* ── Auth ────────────────────────────────────────── */
    #auth{position:fixed;inset:0;background:var(--bg);display:flex;align-items:center;justify-content:center;z-index:100}
    #auth.hidden{display:none}
    .auth-card{background:var(--bg3);border:1px solid var(--border2);border-radius:20px;padding:36px 28px;width:360px;display:flex;flex-direction:column;gap:16px;box-shadow:0 20px 60px var(--shadow)}
    .auth-card h2{font-size:20px;font-weight:700;text-align:center;color:var(--text)}
    .auth-card input{background:var(--bg);border:1px solid var(--border2);color:var(--text);padding:14px 16px;border-radius:12px;font-size:15px;outline:none;transition:border-color .15s,box-shadow .15s;font-family:inherit}
    .auth-card input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-glow)}
    .auth-card button{background:var(--accent);color:#fff;border:none;padding:14px;border-radius:12px;font-size:15px;font-weight:600;cursor:pointer;transition:background .15s,transform .1s;font-family:inherit}
    .auth-card button:hover{background:var(--accent-h)}
    .auth-card button:active{transform:scale(.98)}
    .auth-card .err{color:var(--red);font-size:13px;text-align:center;min-height:18px}

    /* ── Sidebar ─────────────────────────────────────── */
    #sidebar{width:260px;flex-shrink:0;background:var(--bg2);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;transition:width .2s cubic-bezier(.4,0,.2,1)}
    #sidebar-header{padding:12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px}
    #new-chat-btn{flex:1;display:flex;align-items:center;justify-content:center;gap:6px;background:var(--accent);color:#fff;border:none;padding:9px 14px;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;transition:background .15s,transform .1s;font-family:inherit}
    #new-chat-btn:hover{background:var(--accent-h)}
    #new-chat-btn:active{transform:scale(.97)}
    #new-chat-btn .icon{font-size:14px;line-height:1}
    #sidebar-toggle{width:32px;height:32px;flex-shrink:0;background:var(--bg3);border:1px solid var(--border2);color:var(--text3);border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s}
    #sidebar-toggle:hover{background:var(--bg4);color:var(--text)}
    #sidebar-toggle svg{width:14px;height:14px;transition:transform .2s ease}
    #sidebar-search{padding:8px 10px 4px;position:relative}
    #sidebar.collapsed #sidebar-search{display:none}
    #session-search{width:100%;background:var(--bg);border:1px solid var(--border2);color:var(--text2);padding:7px 28px 7px 10px;border-radius:8px;font-size:12px;outline:none;transition:border-color .15s;font-family:inherit}
    #session-search:focus{border-color:var(--accent)}
    #session-search::placeholder{color:var(--text4)}
    #search-clear{position:absolute;right:16px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--text4);cursor:pointer;font-size:16px;line-height:1;display:none;transition:color .15s}
    #search-clear:hover{color:var(--text2)}
    #search-clear.visible{display:block}
    #session-list{flex:1;overflow-y:auto;padding:6px}
    .session-item{display:flex;align-items:center;gap:6px;padding:9px 10px;border-radius:9px;cursor:pointer;transition:background .15s;margin-bottom:2px;position:relative}
    .session-item:hover{background:var(--bg3)}
    .session-item.active{background:var(--accent-glow);border:1px solid rgba(79,70,229,.3)}
    .session-item.kbd-focus{background:var(--bg4);outline:2px solid var(--accent);outline-offset:-2px}
    .session-item.active .session-title{color:var(--text);font-weight:500}
    .session-info{flex:1;overflow:hidden}
    .session-title{font-size:12px;color:var(--text2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .session-date{font-size:10px;color:var(--text4);margin-top:2px}
    .session-snippet{font-size:10px;color:var(--text3);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-style:italic}
    .session-match-count{font-size:9px;background:var(--accent-glow);color:var(--accent);border-radius:8px;padding:0 5px;flex-shrink:0}
    #search-status{padding:4px 12px;font-size:11px;color:var(--text4);text-align:center;display:none}
    #search-status.visible{display:block}
    .session-del{background:none;border:none;color:var(--bg5);cursor:pointer;padding:2px 6px;border-radius:4px;font-size:13px;flex-shrink:0;line-height:1;transition:color .15s}
    .session-del:hover{color:var(--amber)}
    .session-export{background:none;border:none;color:var(--bg5);cursor:pointer;padding:2px 6px;border-radius:4px;font-size:13px;flex-shrink:0;line-height:1;transition:color .15s}
    .session-export:hover{color:var(--purple)}

    /* Archive */
    #archive-section{border-top:1px solid var(--border);flex-shrink:0}
    #archive-header{padding:8px 10px;display:flex;align-items:center;gap:4px;cursor:pointer;font-size:11px;color:var(--text4);user-select:none;transition:color .15s}
    #archive-header:hover{color:var(--text3)}
    #archive-count{background:var(--bg4);border-radius:10px;padding:0 5px;font-size:10px;color:var(--text4);margin-left:4px}
    #archive-arrow{font-size:9px;margin-left:auto;transition:transform .15s}
    #archive-arrow.open{transform:rotate(90deg)}
    #archive-list{display:none;overflow-y:auto;max-height:200px;padding:4px 6px}
    #archive-list.open{display:block}
    #sidebar.collapsed #archive-section{display:none}
    .session-restore{background:none;border:none;color:var(--text4);cursor:pointer;padding:2px 4px;font-size:12px;flex-shrink:0;transition:color .15s}
    .session-restore:hover{color:var(--green)}
    .session-perm-del{background:none;border:none;color:var(--bg5);cursor:pointer;padding:2px 5px;border-radius:4px;font-size:14px;flex-shrink:0;line-height:1;transition:color .15s}
    .session-perm-del:hover{color:var(--red)}

    /* Sidebar collapsed */
    #sidebar.collapsed{width:56px}
    #sidebar.collapsed #sidebar-header{padding:10px 6px;flex-direction:column;gap:6px}
    #sidebar.collapsed #new-chat-btn{flex:none;width:36px;height:36px;padding:0}
    #sidebar.collapsed #new-chat-btn .label{display:none}
    #sidebar.collapsed #new-chat-btn .icon{font-size:18px}
    #sidebar.collapsed #sidebar-toggle svg{transform:rotate(180deg)}
    #sidebar.collapsed #session-list{display:none}

    /* ── Main ────────────────────────────────────────── */
    #main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
    #header{padding:10px 20px;border-bottom:1px solid var(--border);background:var(--bg2);display:flex;align-items:center;gap:10px;flex-shrink:0}
    #header .dot{width:8px;height:8px;border-radius:50%;background:var(--green);flex-shrink:0;box-shadow:0 0 6px var(--green)}
    #header-title{font-size:15px;font-weight:600;color:var(--text)}
    #model{margin-left:auto;background:var(--bg3);border:1px solid var(--border2);color:var(--text2);padding:6px 10px;border-radius:8px;font-size:13px;outline:none;cursor:pointer;transition:border-color .15s;font-family:inherit}
    #model:hover{border-color:var(--border2)}
    #model:focus{border-color:var(--accent)}
    #theme-toggle{width:32px;height:32px;flex-shrink:0;background:var(--bg3);border:1px solid var(--border2);color:var(--text3);border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s;margin-left:6px}
    #theme-toggle:hover{background:var(--bg4);color:var(--text)}
    #theme-toggle svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}

    /* ── Messages ────────────────────────────────────── */
    @keyframes msgIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
    @keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
    #main-messages-wrap{position:relative;flex:1;overflow:hidden;display:flex;flex-direction:column;min-height:0}
    #messages{flex:1;overflow-y:auto;padding:24px 20px;display:flex;flex-direction:column;gap:14px}
    .msg{max-width:82%;animation:msgIn .22s ease both}
    .msg.user{align-self:flex-end}
    .msg.assistant{align-self:flex-start}
    .bubble{padding:12px 16px;border-radius:18px;line-height:1.6;white-space:pre-wrap;word-break:break-word;font-size:14px}
    .msg.user .bubble{background:var(--user-bg);color:var(--user-text);border-bottom-right-radius:5px;box-shadow:0 2px 8px var(--accent-glow)}
    .msg.assistant .bubble{background:var(--bg3);color:var(--text);border:1px solid var(--border2);border-bottom-left-radius:5px}
    .bubble.streaming::after{content:'▋';animation:blink .7s infinite;margin-left:2px;opacity:.7}
    .msg-actions{display:flex;gap:4px;margin-top:4px;opacity:0;transition:opacity .15s;justify-content:flex-end}
    .msg:hover .msg-actions{opacity:1}
    .msg.assistant .msg-actions{justify-content:flex-start}
    .msg-action-btn{background:none;border:1px solid var(--border2);color:var(--text3);border-radius:6px;padding:2px 8px;font-size:11px;cursor:pointer;transition:background .12s,color .12s,border-color .12s;font-family:inherit;white-space:nowrap}
    .msg-action-btn:hover{background:var(--bg4);color:var(--text);border-color:var(--text4)}

    /* ── Terminal ────────────────────────────────────── */
    #term-panel{flex-shrink:0;border-top:1px solid var(--border);background:var(--term-bg)}
    #term-header{padding:5px 14px;display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none;transition:background .15s}
    #term-header:hover{background:var(--bg2)}
    #term-label{font-size:11px;color:var(--text4);font-family:'Menlo','Monaco','Courier New',monospace;flex:1;transition:color .2s}
    #term-label.active{color:var(--green)}
    #term-arrow{font-size:10px;color:var(--text4);transition:transform .15s}
    #term-body{height:150px;overflow-y:auto;padding:6px 14px 8px;font-family:'Menlo','Monaco','Courier New',monospace;font-size:11px;color:var(--term-text);display:none;line-height:1.5}
    #term-body.open{display:block}
    .tl-tool{color:var(--purple)}
    .tl-result{color:var(--text4)}
    .tool-cards{display:flex;flex-direction:column;gap:3px;margin-bottom:6px}
    .tool-card{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text3);background:var(--bg4);border:1px solid var(--border2);border-radius:6px;padding:3px 9px;width:fit-content;max-width:100%;overflow:hidden}
    .tool-card.pending .tc-icon{animation:spin .8s linear infinite;display:inline-block}
    .tool-card.done .tc-icon{color:var(--green)}
    .tool-card.done{opacity:.65}
    .tc-name{font-family:monospace;color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:280px}
    @keyframes spin{to{transform:rotate(360deg)}}
    .tl-other{color:var(--bg5)}

    /* ── Composer ────────────────────────────────────── */
    #footer{padding:12px 20px 16px;border-top:1px solid var(--border);flex-shrink:0;position:relative;background:var(--bg2)}
    #slash-picker{position:absolute;bottom:100%;left:20px;right:20px;background:var(--bg3);border:1px solid var(--border2);border-radius:12px;margin-bottom:6px;max-height:280px;overflow-y:auto;display:none;z-index:50;box-shadow:0 -8px 30px var(--shadow)}
    #slash-picker.open{display:block}
    .slash-item{display:flex;align-items:center;gap:10px;padding:8px 12px;cursor:pointer;border-radius:8px;margin:3px;transition:background .1s}
    .slash-item:hover,.slash-item.active{background:var(--bg4)}
    .slash-cmd{font-weight:600;font-size:13px;color:var(--accent);font-family:monospace;flex-shrink:0}
    [data-theme="light"] .slash-cmd{color:var(--accent)}
    .slash-desc{font-size:12px;color:var(--text3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
    .slash-badge{font-size:10px;padding:1px 5px;border-radius:4px;background:rgba(79,70,229,.15);color:var(--purple);flex-shrink:0}
    #form{display:flex;gap:8px;align-items:flex-end}
    #input{flex:1;background:var(--bg3);border:1px solid var(--border2);color:var(--text);padding:11px 15px;border-radius:14px;font-size:14px;resize:none;outline:none;min-height:46px;max-height:160px;line-height:1.5;font-family:inherit;transition:border-color .15s,box-shadow .15s}
    #input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-glow)}
    #input::placeholder{color:var(--text4)}
    #send{background:var(--accent);border:none;color:#fff;width:44px;height:44px;border-radius:12px;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;transition:background .15s,transform .1s,box-shadow .15s}
    #send:hover{background:var(--accent-h);transform:scale(1.05);box-shadow:0 4px 12px var(--accent-glow)}
    #send:active{transform:scale(.97)}
    #send:disabled{opacity:.4;cursor:not-allowed;transform:none;box-shadow:none}
    #send svg{width:20px;height:20px;fill:none;stroke:#fff;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
    #stop-btn{background:#dc2626;border:none;color:#fff;width:44px;height:44px;border-radius:12px;cursor:pointer;flex-shrink:0;display:none;align-items:center;justify-content:center;transition:background .15s,transform .1s}
    #stop-btn.visible{display:flex}
    #stop-btn:hover{background:#b91c1c;transform:scale(1.05)}
    #stop-btn svg{width:16px;height:16px;fill:#fff}

    /* Attachments */
    #attach-btn{background:var(--bg3);border:1px solid var(--border2);color:var(--text3);width:44px;height:44px;border-radius:12px;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s}
    #attach-btn:hover{background:var(--bg4);color:var(--text)}
    #attach-btn svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
    #attach-preview{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
    #attach-preview:empty{display:none}
    .attach-chip{display:flex;align-items:center;gap:4px;background:var(--bg3);border:1px solid var(--border2);border-radius:8px;padding:3px 8px 3px 4px;font-size:12px;color:var(--text2);max-width:180px}
    .attach-chip img{width:36px;height:36px;object-fit:cover;border-radius:5px;flex-shrink:0}
    .attach-chip .chip-icon{font-size:18px;flex-shrink:0;line-height:1}
    .attach-chip .chip-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:110px}
    .attach-chip .chip-rm{background:none;border:none;color:var(--text4);cursor:pointer;padding:0 2px;font-size:14px;line-height:1;margin-left:2px;flex-shrink:0;transition:color .15s}
    .attach-chip .chip-rm:hover{color:var(--red)}
    .attach-chip.uploading{opacity:.55}
    .attach-chip.error{border-color:#7f1d1d;color:var(--red)}

    /* Attachment bubbles */
    .bubble-attachments{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}
    .bubble-attachments img{max-width:200px;max-height:200px;border-radius:10px;display:block;object-fit:cover}
    .bubble-file-chip{display:inline-flex;align-items:center;gap:4px;background:rgba(255,255,255,.08);border-radius:6px;padding:3px 8px;font-size:12px;color:var(--text2)}
    [data-theme="light"] .bubble-file-chip{background:rgba(0,0,0,.06)}

    /* Output files tray */
    .output-files-tray{border-top:1px solid var(--border2);margin-top:10px;padding-top:8px}
    .output-files-tray .tray-label{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px;font-weight:600}
    .output-image-row{margin:6px 0}
    .output-image-row img{max-width:100%;border-radius:8px;display:block;box-shadow:0 2px 8px var(--shadow-sm)}
    .file-row{display:flex;align-items:center;gap:8px;padding:3px 0;font-size:13px;color:var(--text3)}
    .file-row .file-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text2)}
    .file-row a.dl-btn{color:var(--purple);text-decoration:none;white-space:nowrap;flex-shrink:0;font-size:12px}
    .file-row a.dl-btn:hover{text-decoration:underline}
    .file-row .del-btn{background:none;border:none;cursor:pointer;color:var(--text4);padding:0;font-size:13px;flex-shrink:0;transition:color .15s}
    .file-row .del-btn:hover{color:var(--red)}

    /* Drag-over */
    body.drag-over #messages{outline:2px dashed var(--accent);outline-offset:-4px}
    #scroll-to-bottom{position:absolute;bottom:14px;right:14px;background:var(--bg3);border:1px solid var(--border2);color:var(--text2);border-radius:20px;padding:5px 12px;font-size:12px;cursor:pointer;display:none;align-items:center;gap:5px;box-shadow:0 2px 12px var(--shadow);transition:background .15s,transform .1s;z-index:10;font-family:inherit}
    #scroll-to-bottom.visible{display:flex}
    #scroll-to-bottom:hover{background:var(--bg4);transform:translateY(-1px)}
    #main-messages-wrap{position:relative;flex:1;overflow:hidden;display:flex;flex-direction:column}

    /* ── Markdown ─────────────────────────────────────── */
    .bubble.rendered{white-space:normal}
    .bubble.rendered p{margin:0 0 8px}.bubble.rendered p:last-child{margin-bottom:0}
    .bubble.rendered h1,.bubble.rendered h2,.bubble.rendered h3,.bubble.rendered h4{margin:14px 0 6px;font-weight:700;line-height:1.3;color:var(--text)}
    .bubble.rendered h1{font-size:1.3em}.bubble.rendered h2{font-size:1.15em}.bubble.rendered h3{font-size:1.05em}
    .bubble.rendered ul,.bubble.rendered ol{margin:4px 0 8px 18px;padding:0}
    .bubble.rendered li{margin:3px 0}
    .bubble.rendered code:not(pre code){background:var(--inline-code);border-radius:5px;padding:1px 6px;font-family:'Menlo','Monaco','Courier New',monospace;font-size:.86em;color:var(--inline-code-text)}
    .bubble.rendered pre{position:relative;margin:10px 0;border-radius:10px;overflow:hidden;border:1px solid var(--border2)}
    .bubble.rendered pre code{display:block;overflow-x:auto;padding:14px 16px;font-size:12px;line-height:1.6;background:var(--code-bg)}
    .bubble.rendered blockquote{border-left:3px solid var(--accent);margin:8px 0;padding:4px 12px;color:var(--text2)}
    .bubble.rendered table{border-collapse:collapse;margin:8px 0;width:100%;font-size:13px}
    .bubble.rendered th,.bubble.rendered td{border:1px solid var(--border2);padding:6px 12px;text-align:left}
    .bubble.rendered th{background:var(--bg4);font-weight:600;color:var(--text)}
    .bubble.rendered a{color:var(--purple);text-decoration:none}.bubble.rendered a:hover{text-decoration:underline}
    .bubble.rendered hr{border:none;border-top:1px solid var(--border2);margin:12px 0}
    .bubble.rendered strong{font-weight:700;color:var(--text)}
    .bubble.rendered em{font-style:italic}
    .copy-btn{position:absolute;top:8px;right:8px;background:var(--bg4);border:1px solid var(--border2);color:var(--text3);border-radius:6px;padding:3px 10px;font-size:11px;cursor:pointer;opacity:0;transition:opacity .15s,color .15s;font-family:inherit}
    .bubble.rendered pre:hover .copy-btn{opacity:1}
    .copy-btn:hover{color:var(--text);border-color:var(--text3)}
    .copy-btn.copied{color:var(--green);border-color:var(--green)}
    .mermaid-block{background:var(--bg4);border-radius:10px;padding:12px;margin:10px 0;overflow-x:auto;text-align:center;border:1px solid var(--border2)}
    .mermaid-block svg{max-width:100%;height:auto}

    /* ── Workspace panel ──────────────────────────── */
    #workspace-panel{width:280px;flex-shrink:0;background:var(--bg2);border-left:1px solid var(--border);display:none;flex-direction:column;overflow:hidden}
    #workspace-panel.open{display:flex}
    #ws-header{padding:10px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:6px;flex-shrink:0}
    #ws-header span{font-size:13px;font-weight:600;color:var(--text);flex:1}
    #ws-refresh,#ws-close,#ws-zip{background:none;border:none;color:var(--text3);cursor:pointer;font-size:13px;padding:2px 7px;border-radius:5px;line-height:1.4;transition:color .15s,background .15s;font-family:inherit}
    #ws-refresh:hover,#ws-close:hover,#ws-zip:hover{color:var(--text);background:var(--bg4)}
    #ws-refresh{font-size:16px}
    #ws-close{font-size:18px}
    #ws-upload-zone{margin:8px 10px;border:1.5px dashed var(--border2);border-radius:10px;padding:10px;font-size:12px;color:var(--text3);text-align:center;cursor:pointer;transition:border-color .15s,background .15s;flex-shrink:0}
    #ws-upload-zone:hover,#ws-upload-zone.drag-over{border-color:var(--accent);background:var(--accent-glow);color:var(--text2)}
    #ws-upload-zone label{color:var(--accent);cursor:pointer;text-decoration:underline}
    #ws-file-input{display:none}
    #ws-tree{flex:1;overflow-y:auto;padding:4px 6px 8px}
    .ws-node{font-size:12px;line-height:1.5}
    .ws-row{display:flex;align-items:center;gap:5px;padding:3px 6px;border-radius:6px;cursor:pointer;transition:background .1s;color:var(--text2);white-space:nowrap;overflow:hidden}
    .ws-row:hover{background:var(--bg3);color:var(--text)}
    .ws-icon{font-size:13px;flex-shrink:0;width:16px;text-align:center}
    .ws-name{flex:1;overflow:hidden;text-overflow:ellipsis}
    .ws-size{font-size:10px;color:var(--text4);flex-shrink:0}
    .ws-children{display:none;padding-left:14px}
    .ws-children.open{display:block}
    .ws-dir-row .ws-arrow{font-size:9px;color:var(--text4);flex-shrink:0;transition:transform .15s}
    .ws-dir-row.open .ws-arrow{transform:rotate(90deg)}
    #workspace-toggle{width:32px;height:32px;flex-shrink:0;background:var(--bg3);border:1px solid var(--border2);color:var(--text3);border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s;margin-left:4px}
    #workspace-toggle:hover,#workspace-toggle.active{background:var(--bg4);color:var(--text)}
    #workspace-toggle svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}

    /* ── Shortcuts modal ─────────────────────────────── */
    #shortcuts-modal{position:fixed;inset:0;z-index:95;display:none;align-items:center;justify-content:center}
    #shortcuts-modal.open{display:flex}
    #shortcuts-backdrop2{position:absolute;inset:0;background:rgba(0,0,0,.5);backdrop-filter:blur(3px)}
    #shortcuts-sheet{position:relative;background:var(--bg2);border:1px solid var(--border2);border-radius:16px;padding:20px 24px;min-width:320px;max-width:420px;z-index:1;box-shadow:0 8px 40px var(--shadow)}
    #shortcuts-sheet h3{font-size:15px;font-weight:700;margin-bottom:14px;color:var(--text)}
    .kbd-row{display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);font-size:13px;color:var(--text2)}
    .kbd-row:last-child{border-bottom:none}
    kbd{background:var(--bg4);border:1px solid var(--border2);border-radius:5px;padding:2px 7px;font-size:11px;font-family:monospace;color:var(--text);white-space:nowrap}
    #shortcuts-close2{position:absolute;top:12px;right:14px;background:none;border:none;color:var(--text3);cursor:pointer;font-size:20px;padding:0 4px;line-height:1}
    #shortcuts-close2:hover{color:var(--text)}
    #shortcuts-btn:hover{background:var(--bg4);color:var(--text)}

    /* ── Toast notifications ────────────────────────── */
    #toast-container{position:fixed;bottom:80px;right:20px;z-index:200;display:flex;flex-direction:column;gap:8px;pointer-events:none}
    @media(max-width:768px){#toast-container{bottom:70px;right:12px;left:12px}}
    .toast{display:flex;align-items:center;gap:10px;padding:10px 14px;border-radius:10px;font-size:13px;font-weight:500;min-width:240px;max-width:380px;box-shadow:0 4px 20px var(--shadow);pointer-events:auto;animation:toastIn .25s ease both}
    @keyframes toastIn{from{opacity:0;transform:translateY(10px) scale(.95)}to{opacity:1;transform:none}}
    .toast.toast-out{animation:toastOut .2s ease forwards}
    @keyframes toastOut{to{opacity:0;transform:translateY(8px) scale(.96)}}
    .toast-error{background:#3b1515;border:1px solid #7f1d1d;color:#fca5a5}
    .toast-success{background:#0f2e1a;border:1px solid #14532d;color:#86efac}
    .toast-info{background:var(--bg3);border:1px solid var(--border2);color:var(--text2)}
    .toast-warn{background:#2d1f00;border:1px solid #713f12;color:#fcd34d}
    .toast-icon{font-size:15px;flex-shrink:0}
    .toast-msg{flex:1}
    .toast-close{background:none;border:none;cursor:pointer;font-size:16px;opacity:.6;line-height:1;padding:0 2px;color:inherit;transition:opacity .15s}
    .toast-close:hover{opacity:1}
    [data-theme="light"] .toast-error{background:#fef2f2;color:#991b1b}
    [data-theme="light"] .toast-success{background:#f0fdf4;color:#166534}
    [data-theme="light"] .toast-info{background:var(--bg3);color:var(--text2)}

    /* ── Skills browser modal ───────────────────────── */
    #skills-modal{position:fixed;inset:0;z-index:95;display:none;align-items:flex-end;justify-content:center;padding:0}
    #skills-modal.open{display:flex}
    #skills-backdrop{position:absolute;inset:0;background:rgba(0,0,0,.5);backdrop-filter:blur(3px)}
    #skills-sheet{position:relative;width:100%;max-width:640px;max-height:70vh;background:var(--bg2);border-radius:16px 16px 0 0;display:flex;flex-direction:column;z-index:1;box-shadow:0 -8px 40px var(--shadow)}
    @media(min-width:640px){#skills-sheet{border-radius:16px;margin-bottom:60px;max-height:75vh}}
    #skills-sheet-header{padding:16px 18px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;flex-shrink:0}
    #skills-sheet-header h3{font-size:15px;font-weight:700;color:var(--text);flex:1}
    #skills-search-input{flex:1;background:var(--bg3);border:1px solid var(--border2);color:var(--text);padding:7px 12px;border-radius:8px;font-size:13px;outline:none;font-family:inherit}
    #skills-search-input:focus{border-color:var(--accent)}
    #skills-sheet-close{background:none;border:none;color:var(--text3);cursor:pointer;font-size:20px;padding:0 4px;line-height:1;transition:color .15s}
    #skills-sheet-close:hover{color:var(--text)}
    #skills-list{overflow-y:auto;padding:8px}
    .skill-item{display:flex;align-items:flex-start;gap:12px;padding:10px 12px;border-radius:10px;cursor:pointer;transition:background .1s;border:1px solid transparent}
    .skill-item:hover{background:var(--bg3);border-color:var(--border2)}
    .skill-trigger{font-family:monospace;font-size:13px;font-weight:700;color:var(--accent);white-space:nowrap;flex-shrink:0;min-width:120px}
    .skill-info{flex:1;min-width:0}
    .skill-desc{font-size:12px;color:var(--text2);line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
    .skill-hint{font-size:11px;color:var(--text4);margin-top:3px;font-family:monospace}
    .skill-source{font-size:10px;padding:1px 6px;border-radius:4px;background:var(--bg4);color:var(--text3);flex-shrink:0;align-self:flex-start;margin-top:2px}
    .skills-section-title{font-size:10px;color:var(--text4);text-transform:uppercase;letter-spacing:.08em;padding:6px 12px 2px;font-weight:600}
    #skills-toggle{width:32px;height:32px;flex-shrink:0;background:var(--bg3);border:1px solid var(--border2);color:var(--text3);border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s;margin-left:4px}
    #skills-toggle:hover,#skills-toggle.active{background:var(--bg4);color:var(--text)}
    #skills-toggle svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}

    /* ── Templates modal ───────────────────────────── */
    #tpl-modal{position:fixed;inset:0;z-index:95;display:none;align-items:flex-end;justify-content:center;padding:0}
    #tpl-modal.open{display:flex}
    #tpl-backdrop{position:absolute;inset:0;background:rgba(0,0,0,.5);backdrop-filter:blur(3px)}
    #tpl-sheet{position:relative;width:100%;max-width:640px;max-height:75vh;background:var(--bg2);border-radius:16px 16px 0 0;display:flex;flex-direction:column;z-index:1;box-shadow:0 -8px 40px var(--shadow)}
    @media(min-width:640px){#tpl-sheet{border-radius:16px;margin-bottom:60px}}
    #tpl-sheet-header{padding:14px 16px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;flex-shrink:0}
    #tpl-sheet-header h3{font-size:14px;font-weight:700;color:var(--text);flex:1}
    #tpl-search{flex:1;background:var(--bg3);border:1px solid var(--border2);color:var(--text);padding:7px 12px;border-radius:8px;font-size:13px;outline:none;font-family:inherit}
    #tpl-search:focus{border-color:var(--accent)}
    #tpl-sheet-close{background:none;border:none;color:var(--text3);cursor:pointer;font-size:20px;padding:0 4px;line-height:1}
    #tpl-sheet-close:hover{color:var(--text)}
    #tpl-list{overflow-y:auto;padding:6px}
    .tpl-item{display:flex;align-items:flex-start;gap:10px;padding:10px 12px;border-radius:10px;cursor:pointer;transition:background .1s;border:1px solid transparent}
    .tpl-item:hover{background:var(--bg3);border-color:var(--border2)}
    .tpl-info{flex:1;min-width:0}
    .tpl-name{font-size:13px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .tpl-preview{font-size:12px;color:var(--text3);margin-top:2px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;white-space:pre-wrap}
    .tpl-actions{display:flex;gap:4px;flex-shrink:0;opacity:0;transition:opacity .15s}
    .tpl-item:hover .tpl-actions{opacity:1}
    .tpl-act{background:var(--bg4);border:1px solid var(--border2);color:var(--text3);border-radius:6px;padding:3px 8px;font-size:11px;cursor:pointer;transition:all .15s;font-family:inherit}
    .tpl-act:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
    .tpl-act.del:hover{background:#ef4444;border-color:#ef4444;color:#fff}
    #tpl-save-btn{background:var(--accent);border:none;color:#fff;padding:6px 14px;border-radius:8px;font-size:13px;cursor:pointer;font-weight:600;font-family:inherit;white-space:nowrap;transition:opacity .15s}
    #tpl-save-btn:hover{opacity:.85}
    #tpl-empty{padding:24px;text-align:center;color:var(--text3);font-size:13px}
    #tpl-toggle{width:32px;height:32px;flex-shrink:0;background:var(--bg3);border:1px solid var(--border2);color:var(--text3);border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s;margin-left:4px}
    #tpl-toggle:hover,#tpl-toggle.active{background:var(--bg4);color:var(--text)}
    #tpl-toggle svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}

    /* ── Context window bar ─────────────────────────── */
    #ctx-bar-wrap{display:none;align-items:center;gap:8px;padding:5px 14px 0;flex-shrink:0}
    #ctx-bar-wrap.visible{display:flex}
    #ctx-bar-track{flex:1;height:3px;background:var(--bg4);border-radius:2px;overflow:hidden}
    #ctx-bar-fill{height:100%;width:0%;background:var(--accent);border-radius:2px;transition:width .4s ease,background .4s ease}
    #ctx-bar-fill.warn{background:#f59e0b}
    #ctx-bar-fill.danger{background:#ef4444}
    #ctx-label{font-size:11px;color:var(--text4);white-space:nowrap;font-variant-numeric:tabular-nums}

    /* ── Mobile drawer ─────────────────────────────── */
    #mobile-menu-btn{display:none;width:36px;height:36px;flex-shrink:0;background:var(--bg3);border:1px solid var(--border2);color:var(--text3);border-radius:8px;cursor:pointer;align-items:center;justify-content:center;transition:background .15s,color .15s;order:-1}
    #mobile-menu-btn:hover{background:var(--bg4);color:var(--text)}
    #mobile-menu-btn svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round}
    #sidebar-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:89;backdrop-filter:blur(2px)}
    #sidebar-backdrop.open{display:block}

    @media(max-width:768px){
      /* Sidebar → overlay drawer */
      #sidebar{position:fixed;top:0;left:0;bottom:0;z-index:90;transform:translateX(-100%);transition:transform .25s cubic-bezier(.4,0,.2,1);box-shadow:4px 0 30px var(--shadow)}
      #sidebar.mobile-open{transform:translateX(0)}
      #sidebar.collapsed{width:260px;transform:translateX(-100%)}
      #sidebar.collapsed.mobile-open{transform:translateX(0)}
      /* Show hamburger, hide desktop toggle */
      #mobile-menu-btn{display:flex}
      #sidebar-toggle{display:none}
      /* Header */
      #header{padding:8px 12px;gap:8px}
      #header-title{font-size:14px}
      #model{font-size:12px;padding:5px 8px;max-width:110px}
      /* Messages */
      #messages{padding:16px 12px;gap:10px}
      .msg{max-width:92%}
      /* Composer */
      #footer{padding:10px 12px calc(12px + env(safe-area-inset-bottom,0px))}
      #input{font-size:16px}
      #slash-picker{left:10px;right:10px}
      /* Bubbles */
      .bubble{font-size:14px;padding:10px 14px}
      /* Workspace panel overlay on mobile */
      #workspace-panel{position:fixed;top:0;right:-280px;bottom:0;z-index:90;width:280px;transition:right .25s cubic-bezier(.4,0,.2,1);box-shadow:-4px 0 30px var(--shadow)}
      #workspace-panel.open{right:0;display:flex}
    }
  </style>
</head>
<body>

  <div id="auth">
    <div class="auth-card">
      <h2>⚡ Claude</h2>
      <input type="text" id="uname" placeholder="Имя пользователя" style="display:none" autocomplete="username">
      <input type="password" id="pwd" placeholder="Пароль" autofocus autocomplete="current-password">
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
    <div id="search-status"></div>
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
    <div id="sidebar-backdrop"></div>

    <div id="header">
      <button id="mobile-menu-btn" aria-label="Открыть меню">
        <svg viewBox="0 0 24 24"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
      </button>
      <div class="dot"></div>
      <span id="header-title">Claude</span>
      <select id="model">
        <option value="claude-fable-5">Fable 5</option>
        <option value="claude-opus-4-8">Opus 4.8</option>
        <option value="claude-sonnet-4-6">Sonnet 4.6</option>
        <option value="claude-opus-4-7">Opus 4.7</option>
        <option value="claude-haiku-4-5-20251001">Haiku 4.5</option>
      </select>
      <button id="theme-toggle" title="Переключить тему" aria-label="Переключить тему">
        <svg id="theme-icon-moon" viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
        <svg id="theme-icon-sun" viewBox="0 0 24 24" style="display:none"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
      </button>
      <button id="workspace-toggle" title="Файлы workspace" aria-label="Файлы workspace">
        <svg viewBox="0 0 24 24"><path d="M3 3h18v18H3z" stroke-width="1.5"/><path d="M3 9h18M9 21V9"/></svg>
      </button>
      <button id="skills-toggle" title="Скиллы и команды" aria-label="Скиллы">
        <svg viewBox="0 0 24 24"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
      </button>
      <button id="tpl-toggle" title="Шаблоны промптов (Ctrl+T)" aria-label="Шаблоны">
        <svg viewBox="0 0 24 24"><path d="M19 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2z"/><path d="M9 9h6M9 12h6M9 15h4"/></svg>
      </button>
      <button id="shortcuts-btn" title="Горячие клавиши" aria-label="Горячие клавиши" style="width:32px;height:32px;flex-shrink:0;background:var(--bg3);border:1px solid var(--border2);color:var(--text3);border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;margin-left:4px;transition:background .15s,color .15s;font-family:inherit">?</button>
    </div>

    <div id="main-messages-wrap">
      <div id="messages">
        <div class="msg assistant"><div class="bubble">Привет! Чем могу помочь?</div></div>
      </div>
      <button id="scroll-to-bottom">↓ Вниз</button>
    </div>

    <div id="term-panel">
      <div id="term-header">
        <span id="term-label">// terminal</span>
        <span id="term-arrow">▶</span>
      </div>
      <div id="term-body"></div>
    </div>

    <div id="footer">
      <div id="ctx-bar-wrap">
        <div id="ctx-bar-track"><div id="ctx-bar-fill"></div></div>
        <span id="ctx-label">0 / 200k</span>
      </div>
      <div id="slash-picker"></div>
      <input type="file" id="file-input" multiple style="display:none">
      <div id="attach-preview"></div>
      <form id="form">
        <button type="button" id="attach-btn" title="Прикрепить файл (или перетащи / вставь)">
          <svg viewBox="0 0 24 24"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
        </button>
        <textarea id="input" rows="1" placeholder="Напиши сообщение... (Ctrl+V для вставки скриншота)"></textarea>
        <button id="send" type="submit">
          <svg viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
        </button>
        <button id="stop-btn" type="button" title="Остановить генерацию">
          <svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>
        </button>
      </form>
    </div>
  </div>

  <!-- Templates modal -->
  <div id="tpl-modal" role="dialog" aria-label="Шаблоны промптов">
    <div id="tpl-backdrop"></div>
    <div id="tpl-sheet">
      <div id="tpl-sheet-header">
        <h3>Шаблоны</h3>
        <input id="tpl-search" type="text" placeholder="Поиск…" autocomplete="off">
        <button id="tpl-save-btn" type="button">+ Сохранить текущий</button>
        <button id="tpl-sheet-close" aria-label="Закрыть">×</button>
      </div>
      <div id="tpl-list"></div>
    </div>
  </div>

  <div id="shortcuts-modal">
    <div id="shortcuts-backdrop2"></div>
    <div id="shortcuts-sheet">
      <button id="shortcuts-close2">×</button>
      <h3>⌨️ Горячие клавиши</h3>
      <div class="kbd-row"><span>Новый чат</span><kbd>Ctrl+K</kbd></div>
      <div class="kbd-row"><span>Свернуть сайдбар</span><kbd>Ctrl+B</kbd></div>
      <div class="kbd-row"><span>Скиллы</span><kbd>Ctrl+/</kbd></div>
      <div class="kbd-row"><span>Шаблоны</span><kbd>Ctrl+T</kbd></div>
      <div class="kbd-row"><span>Файлы workspace</span><kbd>Ctrl+.</kbd></div>
      <div class="kbd-row"><span>Отправить сообщение</span><kbd>Enter</kbd></div>
      <div class="kbd-row"><span>Новая строка</span><kbd>Shift+Enter</kbd></div>
      <div class="kbd-row"><span>Вставить скриншот</span><kbd>Ctrl+V</kbd></div>
      <div class="kbd-row"><span>Навигация по сессиям</span><kbd>↑</kbd> <kbd>↓</kbd> <kbd>Enter</kbd></div>
      <div class="kbd-row"><span>Закрыть панель</span><kbd>Esc</kbd></div>
    </div>
  </div>

  <div id="skills-modal">
    <div id="skills-backdrop"></div>
    <div id="skills-sheet">
      <div id="skills-sheet-header">
        <h3>⚡ Скиллы и команды</h3>
        <input id="skills-search-input" placeholder="Поиск..." type="text">
        <button id="skills-sheet-close">×</button>
      </div>
      <div id="skills-list"></div>
    </div>
  </div>

  <div id="workspace-panel">
    <div id="ws-header">
      <span>📁 Workspace</span>
      <button id="ws-refresh" title="Обновить">↻</button>
      <button id="ws-zip" title="Скачать workspace как ZIP">⬇ ZIP</button>
      <button id="ws-close" title="Закрыть">×</button>
    </div>
    <div id="ws-upload-zone">
      Перетащи файлы или <label for="ws-file-input">выбери</label>
      <input type="file" id="ws-file-input" multiple>
    </div>
    <div id="ws-tree"></div>
  </div>

  <script>
    // ── Toast notifications ────────────────────────────
    const toastContainer = document.getElementById('toast-container');
    const ICONS = {error:'❌', success:'✅', info:'ℹ️', warn:'⚠️'};
    function showToast(msg, type = 'info', duration = 4000) {
      const t = document.createElement('div');
      t.className = `toast toast-${type}`;
      t.innerHTML = `<span class="toast-icon">${ICONS[type]||'ℹ️'}</span><span class="toast-msg">${msg}</span><button class="toast-close" aria-label="Закрыть">×</button>`;
      const close = () => {
        t.classList.add('toast-out');
        setTimeout(() => t.remove(), 220);
      };
      t.querySelector('.toast-close').addEventListener('click', close);
      toastContainer.appendChild(t);
      if (duration > 0) setTimeout(close, duration);
      console.debug('[toast]', type, msg);
      return close;
    }

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

    // ── Theme ──────────────────────────────────────────
    const THEME_KEY = 'theme';
    function applyTheme(t) {
      document.documentElement.setAttribute('data-theme', t);
      localStorage.setItem(THEME_KEY, t);
      const moon = document.getElementById('theme-icon-moon');
      const sun  = document.getElementById('theme-icon-sun');
      if (moon && sun) { moon.style.display = t === 'dark' ? '' : 'none'; sun.style.display = t === 'light' ? '' : 'none'; }
      console.debug('[theme] switched to', t);
    }
    document.getElementById('theme-toggle').addEventListener('click', () => {
      const cur = document.documentElement.getAttribute('data-theme');
      applyTheme(cur === 'dark' ? 'light' : 'dark');
    });
    // Sync icon with initial theme
    applyTheme(document.documentElement.getAttribute('data-theme') || 'dark');

    // ── App state ──────────────────────────────────────
    const TOKEN_KEY   = 'claude_token';
    const SESSION_KEY = 'claude_session_id';
    const SIDEBAR_KEY = 'claude_sidebar_collapsed';
    let token     = localStorage.getItem(TOKEN_KEY) || '';
    let sessionId = localStorage.getItem(SESSION_KEY) || '';

    const authEl   = document.getElementById('auth');
    const pwdEl    = document.getElementById('pwd');
    const unameEl  = document.getElementById('uname');
    const authErr  = document.getElementById('auth-err');
    let _multiUser = false;

    // Detect multi-user mode and show/hide username field
    fetch('/claude/auth/config').then(r => r.json()).then(cfg => {
      _multiUser = cfg.multi_user;
      if (_multiUser) {
        unameEl.style.display = '';
        unameEl.placeholder = 'Имя пользователя';
        pwdEl.removeAttribute('autofocus');
        unameEl.setAttribute('autofocus', '');
        unameEl.focus();
      }
    }).catch(() => {});
    const messages    = document.getElementById('messages');
    const input       = document.getElementById('input');
    const send        = document.getElementById('send');
    const form        = document.getElementById('form');
    const sesList     = document.getElementById('session-list');
    const scrollBtn   = document.getElementById('scroll-to-bottom');
    let _autoScroll   = true;

    function scrollToBottom() {
      messages.scrollTop = messages.scrollHeight;
      _autoScroll = true;
      scrollBtn.classList.remove('visible');
    }

    function maybeScroll() {
      if (_autoScroll) messages.scrollTop = messages.scrollHeight;
    }

    messages.addEventListener('scroll', () => {
      const atBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 60;
      _autoScroll = atBottom;
      scrollBtn.classList.toggle('visible', !atBottom);
    });

    scrollBtn.addEventListener('click', scrollToBottom);
    let searchQuery = '';
    let _searchTimer = null;
    let _inFullTextSearch = false;
    const searchStatus = document.getElementById('search-status');

    function filterSessions() {
      const q = searchQuery.toLowerCase().trim();
      document.getElementById('search-clear').classList.toggle('visible', q.length > 0);
      if (_inFullTextSearch) return; // full-text results override client filter
      document.querySelectorAll('#session-list .session-item').forEach(item => {
        const title = (item.dataset.title || '').toLowerCase();
        item.style.display = (!q || title.includes(q)) ? '' : 'none';
      });
    }

    function renderSearchResults(results, q) {
      sesList.innerHTML = '';
      _inFullTextSearch = true;
      if (!results.length) {
        searchStatus.textContent = `Ничего не найдено по «${q}»`;
        searchStatus.classList.add('visible');
        return;
      }
      searchStatus.textContent = `${results.length} сессий содержат «${q}»`;
      searchStatus.classList.add('visible');
      results.forEach(r => {
        const item = document.createElement('div');
        item.className = 'session-item';
        item.dataset.sid = r.session_id;
        item.dataset.title = r.title;
        const info = document.createElement('div');
        info.className = 'session-info';
        const title = document.createElement('div');
        title.className = 'session-title';
        title.textContent = r.title || 'Без названия';
        info.appendChild(title);
        if (r.snippet) {
          const snip = document.createElement('div');
          snip.className = 'session-snippet';
          snip.textContent = r.snippet;
          info.appendChild(snip);
        }
        const badge = document.createElement('span');
        badge.className = 'session-match-count';
        badge.textContent = r.match_count + ' совп.';
        item.appendChild(info);
        item.appendChild(badge);
        item.addEventListener('click', () => openSession(r.session_id));
        sesList.appendChild(item);
      });
      console.debug('[search] rendered', results.length, 'results for', q);
    }

    function clearSearchResults() {
      _inFullTextSearch = false;
      searchStatus.textContent = '';
      searchStatus.classList.remove('visible');
    }

    async function runFullTextSearch(q) {
      if (q.length < 2) {
        clearSearchResults();
        await loadSessions();
        filterSessions();
        return;
      }
      try {
        const r = await fetch(`/claude/sessions/search?q=${encodeURIComponent(q)}`, { headers: {'X-Token': token} });
        if (!r.ok) return;
        const data = await r.json();
        renderSearchResults(data.results || [], q);
      } catch(e) { console.debug('[search] error', e.message); }
    }

    document.addEventListener('DOMContentLoaded', () => {
      const searchInput = document.getElementById('session-search');
      const searchClear = document.getElementById('search-clear');
      if (searchInput) {
        searchInput.addEventListener('input', e => {
          searchQuery = e.target.value;
          filterSessions();
          clearTimeout(_searchTimer);
          _searchTimer = setTimeout(() => runFullTextSearch(searchQuery.trim()), 350);
        });
      }
      if (searchClear) {
        searchClear.addEventListener('click', async () => {
          searchQuery = '';
          if (searchInput) searchInput.value = '';
          clearSearchResults();
          await loadSessions();
          filterSessions();
        });
      }
    });

    const termBody = document.getElementById('term-body');
    const termLbl  = document.getElementById('term-label');
    const termArrow= document.getElementById('term-arrow');
    const sidebar  = document.getElementById('sidebar');
    const sidebarToggle = document.getElementById('sidebar-toggle');
    const backdrop = document.getElementById('sidebar-backdrop');
    const isMobile = () => window.innerWidth <= 768;

    // ── Sidebar collapse (desktop) / drawer (mobile) ───
    function applySidebarState(collapsed) {
      sidebar.classList.toggle('collapsed', collapsed);
      if (sidebarToggle) {
        sidebarToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        sidebarToggle.title = collapsed ? 'Развернуть (Ctrl/Cmd+B)' : 'Свернуть (Ctrl/Cmd+B)';
      }
    }

    function openMobileDrawer() {
      sidebar.classList.add('mobile-open');
      backdrop.classList.add('open');
      document.body.style.overflow = 'hidden';
      console.debug('[sidebar] mobile drawer open');
    }

    function closeMobileDrawer() {
      sidebar.classList.remove('mobile-open');
      backdrop.classList.remove('open');
      document.body.style.overflow = '';
      console.debug('[sidebar] mobile drawer closed');
    }

    function toggleSidebar() {
      if (isMobile()) {
        sidebar.classList.contains('mobile-open') ? closeMobileDrawer() : openMobileDrawer();
        return;
      }
      const collapsed = !sidebar.classList.contains('collapsed');
      applySidebarState(collapsed);
      localStorage.setItem(SIDEBAR_KEY, collapsed ? '1' : '0');
      console.debug('[sidebar] toggled', collapsed);
    }

    backdrop.addEventListener('click', closeMobileDrawer);
    applySidebarState(localStorage.getItem(SIDEBAR_KEY) === '1');
    if (sidebarToggle) sidebarToggle.addEventListener('click', toggleSidebar);
    document.getElementById('mobile-menu-btn').addEventListener('click', toggleSidebar);

    // ── Keyboard shortcuts ─────────────────────────────
    // Ctrl/Cmd+B  → sidebar toggle
    // Ctrl/Cmd+K  → new chat
    // Ctrl/Cmd+/  → skills browser
    // Ctrl/Cmd+.  → workspace browser
    // ↑/↓         → navigate session list (when input not focused)
    // Escape      → close open panels/modals
    let _sessionNavIdx = -1;

    function getVisibleSessions() {
      return Array.from(sesList.querySelectorAll('.session-item'))
        .filter(el => el.style.display !== 'none');
    }

    function highlightSessionNav(idx) {
      const items = getVisibleSessions();
      items.forEach((el, i) => el.classList.toggle('kbd-focus', i === idx));
      if (items[idx]) items[idx].scrollIntoView({block: 'nearest'});
    }

    window.addEventListener('keydown', e => {
      if (authEl && !authEl.classList.contains('hidden')) return;
      const mod = e.ctrlKey || e.metaKey;
      const inputFocused = document.activeElement === input;

      // Ctrl/Cmd + key shortcuts
      if (mod && !e.shiftKey && !e.altKey) {
        const k = e.key.toLowerCase();
        if (k === 'b') { e.preventDefault(); toggleSidebar(); return; }
        if (k === 'k') {
          e.preventDefault();
          document.getElementById('new-chat-btn').click();
          input.focus();
          return;
        }
        if (k === '/') {
          e.preventDefault();
          skillsModal.classList.contains('open') ? closeSkillsModal() : openSkillsModal();
          return;
        }
        if (k === '.') {
          e.preventDefault();
          wsTogBtn.click();
          return;
        }
        if (k === 't') {
          e.preventDefault();
          tplModal.classList.contains('open') ? closeTplModal() : openTplModal();
          return;
        }
      }

      // Escape — close open panels
      if (e.key === 'Escape') {
        if (tplModal.classList.contains('open')) { closeTplModal(); return; }
        if (shortcutsModal && shortcutsModal.classList.contains('open')) { closeShortcuts(); return; }
        if (skillsModal.classList.contains('open')) { closeSkillsModal(); return; }
        if (wsPanel.classList.contains('open')) { wsPanel.classList.remove('open'); wsTogBtn.classList.remove('active'); return; }
        if (isMobile() && sidebar.classList.contains('mobile-open')) { closeMobileDrawer(); return; }
      }

      // ↑/↓ session navigation (only when input/search not focused)
      const searchEl = document.getElementById('session-search');
      if (!inputFocused && document.activeElement !== searchEl) {
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          const items = getVisibleSessions();
          _sessionNavIdx = Math.min(_sessionNavIdx + 1, items.length - 1);
          highlightSessionNav(_sessionNavIdx);
          return;
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          _sessionNavIdx = Math.max(_sessionNavIdx - 1, 0);
          highlightSessionNav(_sessionNavIdx);
          return;
        }
        if (e.key === 'Enter' && _sessionNavIdx >= 0) {
          const items = getVisibleSessions();
          if (items[_sessionNavIdx]) {
            items[_sessionNavIdx].click();
            _sessionNavIdx = -1;
            input.focus();
          }
          return;
        }
      }
    });

    // Reset session nav index when sessions reload
    const _origLoadSessions = loadSessions;
    async function loadSessions() {
      _sessionNavIdx = -1;
      return _origLoadSessions();
    }

    // Close mobile drawer when screen resizes to desktop
    window.addEventListener('resize', () => { if (!isMobile()) closeMobileDrawer(); });

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
        const d = await r.json();
        renderSessions(Array.isArray(d) ? d : (d.sessions || []));
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
        exp.textContent = '↓md';
        exp.addEventListener('click', async e => {
          e.stopPropagation();
          await exportSession(s.session_id, 'md');
        });

        const expJson = document.createElement('button');
        expJson.className = 'session-export';
        expJson.title = 'Скачать как JSON';
        expJson.textContent = '↓{}';
        expJson.addEventListener('click', async e => {
          e.stopPropagation();
          await exportSession(s.session_id, 'json');
        });

        item.append(info, exp, expJson, del);
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
      if (isMobile()) closeMobileDrawer();
      try {
        const r = await fetch(`/claude/sessions/${sid}`, { headers: {'X-Token': token} });
        if (!r.ok) return;
        const s = await r.json();
        sessionId = sid;
        localStorage.setItem(SESSION_KEY, sid);

        document.querySelectorAll('.session-item').forEach(el =>
          el.classList.toggle('active', el.dataset.sid === sid));

        messages.innerHTML = '';
        for (let i = 0; i < (s.messages || []).length; i++) {
          const m = s.messages[i];
          addMsg(m.role, m.text, m.attachments || [], m.output_files || [], i);
        }
        if (!s.messages?.length) {
          messages.innerHTML = '<div class="msg assistant"><div class="bubble">Привет! Чем могу помочь?</div></div>';
        }
        _autoScroll = true;
        scrollBtn.classList.remove('visible');
        messages.scrollTop = messages.scrollHeight;
        termClear();
        if (s.last_usage) updateCtxBar(s.last_usage.input_tokens);
        else resetCtxBar();
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
        const raw = await r.json();
        const list = Array.isArray(raw) ? raw : (raw.sessions || []);
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

    async function exportSession(sid, fmt = 'md') {
      try {
        const r = await fetch(`/claude/sessions/${sid}/export?format=${fmt}`, {headers:{'X-Token': token}});
        if (!r.ok) throw new Error(r.statusText);
        const blob = await r.blob();
        const cd = r.headers.get('Content-Disposition') || '';
        const match = cd.match(/filename="([^"]+)"/);
        const filename = match ? match[1] : `claude_${sid.slice(0,8)}.${fmt}`;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = filename;
        document.body.appendChild(a); a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        console.debug('[session] exported', sid, 'as', filename);
      } catch(e) {
        console.debug('[session] export error', e.message);
        showToast('Ошибка экспорта: ' + e.message, 'error');
      }
    }

    // ── Auth ───────────────────────────────────────────
    async function tryLogin() {
      authErr.textContent = '';
      const pwd = pwdEl.value.trim();
      if (!pwd) return;
      const uname = unameEl.value.trim();
      const body = _multiUser && uname
        ? {username: uname, password: pwd}
        : {password: pwd};
      try {
        const r = await fetch('/claude/auth', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body),
        });
        const d = await r.json();
        if (r.ok && d.token) {
          token = d.token;
          localStorage.setItem(TOKEN_KEY, token);
          if (d.username) localStorage.setItem('claude_username', d.username);
          authEl.classList.add('hidden');
          await afterAuth();
        } else {
          authErr.textContent = _multiUser ? 'Неверный логин или пароль' : 'Неверный пароль';
          pwdEl.value = ''; pwdEl.focus();
        }
      } catch(e) { authErr.textContent = 'Ошибка соединения'; }
    }

    // ── Skills Browser ────────────────────────────────
    const skillsModal   = document.getElementById('skills-modal');
    const skillsList    = document.getElementById('skills-list');
    const skillsSearch  = document.getElementById('skills-search-input');
    const skillsTogBtn  = document.getElementById('skills-toggle');
    let allSkills = [];

    async function loadSkills() {
      try {
        const r = await fetch('/claude/skills', { headers: {'X-Token': token} });
        if (r.ok) { allSkills = (await r.json()).skills || []; console.debug('[skills] loaded', allSkills.length); }
      } catch(e) { console.debug('[skills] load error', e.message); }
    }

    function renderSkills(query) {
      const q = (query || '').toLowerCase().trim();
      const filtered = q ? allSkills.filter(s =>
        s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q)
      ) : allSkills;

      skillsList.innerHTML = '';
      if (!filtered.length) {
        skillsList.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text3);font-size:13px">Ничего не найдено</div>';
        return;
      }

      const bySource = {};
      filtered.forEach(s => { (bySource[s.source] = bySource[s.source] || []).push(s); });
      Object.entries(bySource).forEach(([src, items]) => {
        const title = document.createElement('div');
        title.className = 'skills-section-title';
        title.textContent = src;
        skillsList.appendChild(title);
        items.forEach(skill => {
          const item = document.createElement('div');
          item.className = 'skill-item';
          const trigEl = document.createElement('span');
          trigEl.className = 'skill-trigger';
          trigEl.textContent = skill.trigger;
          const info = document.createElement('div');
          info.className = 'skill-info';
          const desc = document.createElement('div');
          desc.className = 'skill-desc';
          desc.textContent = skill.description || '—';
          info.appendChild(desc);
          if (skill.argument_hint) {
            const hint = document.createElement('div');
            hint.className = 'skill-hint';
            hint.textContent = skill.argument_hint;
            info.appendChild(hint);
          }
          item.appendChild(trigEl);
          item.appendChild(info);
          if (src !== 'skills') {
            const badge = document.createElement('span');
            badge.className = 'skill-source';
            badge.textContent = src;
            item.appendChild(badge);
          }
          item.addEventListener('click', () => {
            const ins = skill.trigger + (skill.argument_hint ? ' ' : '');
            const cur = input.value;
            input.value = cur ? cur + '\n' + ins : ins;
            input.style.height = 'auto';
            input.style.height = Math.min(input.scrollHeight, 160) + 'px';
            closeSkillsModal();
            input.focus();
            console.debug('[skills] inserted', skill.trigger);
          });
          skillsList.appendChild(item);
        });
      });
    }

    function openSkillsModal() {
      skillsModal.classList.add('open');
      skillsTogBtn.classList.add('active');
      if (!allSkills.length) loadSkills().then(() => renderSkills(skillsSearch.value));
      else renderSkills(skillsSearch.value);
      skillsSearch.focus();
    }

    function closeSkillsModal() {
      skillsModal.classList.remove('open');
      skillsTogBtn.classList.remove('active');
    }

    skillsTogBtn.addEventListener('click', () => skillsModal.classList.contains('open') ? closeSkillsModal() : openSkillsModal());
    document.getElementById('skills-sheet-close').addEventListener('click', closeSkillsModal);
    document.getElementById('skills-backdrop').addEventListener('click', closeSkillsModal);
    skillsSearch.addEventListener('input', () => renderSkills(skillsSearch.value));

    // ── Prompt Templates ──────────────────────────────────
    const tplModal   = document.getElementById('tpl-modal');
    const tplList    = document.getElementById('tpl-list');
    const tplSearch  = document.getElementById('tpl-search');
    const tplTogBtn  = document.getElementById('tpl-toggle');
    let allTemplates = [];

    async function loadTemplates() {
      try {
        const r = await fetch('/claude/templates', { headers: {'X-Token': token} });
        if (r.ok) { allTemplates = (await r.json()).templates || []; }
      } catch(e) {}
    }

    function renderTemplates(query) {
      const q = (query || '').toLowerCase().trim();
      const items = q ? allTemplates.filter(t =>
        t.name.toLowerCase().includes(q) || t.text.toLowerCase().includes(q)
      ) : allTemplates;
      tplList.innerHTML = '';
      if (!items.length) {
        const empty = document.createElement('div');
        empty.id = 'tpl-empty';
        empty.textContent = q ? 'Ничего не найдено' : 'Нет шаблонов. Напишите промпт и нажмите «+ Сохранить текущий»';
        tplList.appendChild(empty);
        return;
      }
      items.forEach(t => {
        const item = document.createElement('div');
        item.className = 'tpl-item';
        const info = document.createElement('div');
        info.className = 'tpl-info';
        const name = document.createElement('div');
        name.className = 'tpl-name';
        name.textContent = t.name;
        const prev = document.createElement('div');
        prev.className = 'tpl-preview';
        prev.textContent = t.text;
        info.append(name, prev);
        const acts = document.createElement('div');
        acts.className = 'tpl-actions';
        const editBtn = document.createElement('button');
        editBtn.className = 'tpl-act';
        editBtn.textContent = 'Изменить';
        editBtn.addEventListener('click', e => { e.stopPropagation(); editTemplate(t); });
        const delBtn = document.createElement('button');
        delBtn.className = 'tpl-act del';
        delBtn.textContent = 'Удалить';
        delBtn.addEventListener('click', async e => {
          e.stopPropagation();
          if (!confirm(`Удалить шаблон «${t.name}»?`)) return;
          await fetch(`/claude/templates/${t.id}`, { method: 'DELETE', headers: {'X-Token': token} });
          allTemplates = allTemplates.filter(x => x.id !== t.id);
          renderTemplates(tplSearch.value);
        });
        acts.append(editBtn, delBtn);
        item.append(info, acts);
        item.addEventListener('click', () => {
          input.value = t.text;
          input.dispatchEvent(new Event('input'));
          closeTplModal();
          input.focus();
        });
        tplList.appendChild(item);
      });
    }

    async function editTemplate(t) {
      const newName = prompt('Название шаблона:', t.name);
      if (newName === null) return;
      const newText = prompt('Текст шаблона:', t.text);
      if (newText === null) return;
      const r = await fetch(`/claude/templates/${t.id}`, {
        method: 'PATCH', headers: {'Content-Type':'application/json','X-Token':token},
        body: JSON.stringify({name: newName.trim(), text: newText}),
      });
      if (r.ok) {
        const updated = await r.json();
        const idx = allTemplates.findIndex(x => x.id === t.id);
        if (idx !== -1) allTemplates[idx] = updated;
        renderTemplates(tplSearch.value);
      }
    }

    function openTplModal() {
      loadTemplates().then(() => renderTemplates(''));
      tplModal.classList.add('open');
      tplTogBtn.classList.add('active');
      tplSearch.value = '';
      setTimeout(() => tplSearch.focus(), 80);
    }

    function closeTplModal() {
      tplModal.classList.remove('open');
      tplTogBtn.classList.remove('active');
    }

    tplTogBtn.addEventListener('click', () => tplModal.classList.contains('open') ? closeTplModal() : openTplModal());
    document.getElementById('tpl-sheet-close').addEventListener('click', closeTplModal);
    document.getElementById('tpl-backdrop').addEventListener('click', closeTplModal);
    tplSearch.addEventListener('input', () => renderTemplates(tplSearch.value));

    document.getElementById('tpl-save-btn').addEventListener('click', async () => {
      const text = input.value.trim();
      if (!text) { showToast('Напишите промпт, чтобы сохранить его как шаблон', 'info'); return; }
      const name = prompt('Название шаблона:');
      if (!name || !name.trim()) return;
      const r = await fetch('/claude/templates', {
        method: 'POST', headers: {'Content-Type':'application/json','X-Token':token},
        body: JSON.stringify({name: name.trim(), text}),
      });
      if (r.ok) {
        const tpl = await r.json();
        allTemplates.unshift(tpl);
        renderTemplates(tplSearch.value);
        showToast('Шаблон сохранён', 'success');
      }
    });

    // ── Context Window Indicator ──────────────────────────
    const ctxBarWrap  = document.getElementById('ctx-bar-wrap');
    const ctxBarFill  = document.getElementById('ctx-bar-fill');
    const ctxLabel    = document.getElementById('ctx-label');
    const CTX_LIMIT   = 200000;

    function updateCtxBar(inputTokens) {
      if (!inputTokens) return;
      const pct = Math.min(100, (inputTokens / CTX_LIMIT) * 100);
      ctxBarWrap.classList.add('visible');
      ctxBarFill.style.width = pct + '%';
      ctxBarFill.classList.toggle('warn', pct >= 50 && pct < 80);
      ctxBarFill.classList.toggle('danger', pct >= 80);
      ctxBarFill.classList.toggle('accent', pct < 50);
      const kIn = Math.round(inputTokens / 100) / 10;
      ctxLabel.textContent = `${kIn}k / 200k`;
    }

    function resetCtxBar() {
      ctxBarWrap.classList.remove('visible');
      ctxBarFill.style.width = '0%';
      ctxBarFill.className = '';
      ctxLabel.textContent = '0 / 200k';
    }

    // ── Shortcuts modal ───────────────────────────────────
    const shortcutsModal = document.getElementById('shortcuts-modal');
    function openShortcuts() { shortcutsModal.classList.add('open'); }
    function closeShortcuts() { shortcutsModal.classList.remove('open'); }
    document.getElementById('shortcuts-btn').addEventListener('click', () =>
      shortcutsModal.classList.contains('open') ? closeShortcuts() : openShortcuts());
    document.getElementById('shortcuts-close2').addEventListener('click', closeShortcuts);
    document.getElementById('shortcuts-backdrop2').addEventListener('click', closeShortcuts);

    // ── Workspace File Browser ─────────────────────────
    const wsPanel   = document.getElementById('workspace-panel');
    const wsTree    = document.getElementById('ws-tree');
    const wsUpload  = document.getElementById('ws-upload-zone');
    const wsInput   = document.getElementById('ws-file-input');
    const wsTogBtn  = document.getElementById('workspace-toggle');
    let wsLoaded = false;

    function fileIcon(ext, isImage) {
      if (isImage) return '🖼';
      const m = {'.pdf':'📄','.md':'📝','.txt':'📄','.js':'📜','.ts':'📜','.py':'🐍','.json':'{}',
                 '.html':'🌐','.css':'🎨','.sh':'⚙','.zip':'🗜','.tar':'🗜','.gz':'🗜'};
      return m[ext] || '📄';
    }

    function renderTree(items, container, depth) {
      items.forEach(node => {
        const wrap = document.createElement('div');
        wrap.className = 'ws-node';
        const row = document.createElement('div');
        row.className = 'ws-row' + (node.type === 'dir' ? ' ws-dir-row' : '');
        row.style.paddingLeft = (depth * 10) + 'px';

        if (node.type === 'dir') {
          const arrow = document.createElement('span');
          arrow.className = 'ws-arrow';
          arrow.textContent = '▶';
          const icon = document.createElement('span');
          icon.className = 'ws-icon';
          icon.textContent = '📁';
          const name = document.createElement('span');
          name.className = 'ws-name';
          name.textContent = node.name;
          row.appendChild(arrow);
          row.appendChild(icon);
          row.appendChild(name);

          const children = document.createElement('div');
          children.className = 'ws-children';
          if (node.children && node.children.length) renderTree(node.children, children, depth + 1);

          row.addEventListener('click', () => {
            const open = children.classList.toggle('open');
            row.classList.toggle('open', open);
            console.debug('[ws] dir', open ? 'expanded' : 'collapsed', node.rel_path);
          });
          wrap.appendChild(row);
          wrap.appendChild(children);
        } else {
          const icon = document.createElement('span');
          icon.className = 'ws-icon';
          icon.textContent = fileIcon(node.ext, node.is_image);
          const name = document.createElement('span');
          name.className = 'ws-name';
          name.title = node.rel_path;
          name.textContent = node.name;
          const sz = document.createElement('span');
          sz.className = 'ws-size';
          sz.textContent = formatBytes(node.size || 0);
          row.appendChild(icon);
          row.appendChild(name);
          row.appendChild(sz);
          row.addEventListener('click', () => {
            const url = `/claude/workspace/file/${node.rel_path}`;
            if (node.is_image) { window.open(url, '_blank'); }
            else {
              const a = document.createElement('a');
              a.href = url; a.download = node.name; a.click();
            }
            console.debug('[ws] file accessed', node.rel_path);
          });
          wrap.appendChild(row);
        }
        container.appendChild(wrap);
      });
    }

    async function loadWorkspaceTree() {
      wsTree.innerHTML = '<div style="padding:12px;font-size:12px;color:var(--text3)">Загрузка...</div>';
      try {
        const r = await fetch('/claude/workspace/tree', { headers: {'X-Token': token} });
        if (!r.ok) { wsTree.innerHTML = '<div style="padding:12px;font-size:12px;color:var(--red)">Ошибка загрузки</div>'; return; }
        const data = await r.json();
        wsTree.innerHTML = '';
        if (!data.tree || !data.tree.length) {
          wsTree.innerHTML = '<div style="padding:12px;font-size:12px;color:var(--text3)">Workspace пуст</div>';
          return;
        }
        renderTree(data.tree, wsTree, 0);
        wsLoaded = true;
        console.debug('[ws] tree loaded', data.tree.length, 'root items');
      } catch(e) { wsTree.innerHTML = '<div style="padding:12px;font-size:12px;color:var(--red)">Ошибка: ' + e.message + '</div>'; }
    }

    async function wsUploadFiles(files, dir) {
      const fd = new FormData();
      for (const f of files) fd.append('files', f);
      if (dir) fd.append('dir', dir);
      try {
        const r = await fetch('/claude/workspace/upload', { method:'POST', headers:{'X-Token':token}, body:fd });
        const data = await r.json();
        if (r.ok) {
          showToast(`Загружено ${data.files.length} файл(а)`, 'success', 3000);
          console.debug('[ws] uploaded', data.files.length, 'file(s)');
          loadWorkspaceTree();
        } else {
          showToast('Ошибка загрузки: ' + (data.error || r.status), 'error');
          console.debug('[ws] upload error', data.error);
        }
      } catch(e) { console.debug('[ws] upload exception', e.message); }
    }

    wsTogBtn.addEventListener('click', () => {
      const open = wsPanel.classList.toggle('open');
      wsTogBtn.classList.toggle('active', open);
      if (open && !wsLoaded) loadWorkspaceTree();
      console.debug('[ws] panel', open ? 'opened' : 'closed');
    });

    document.getElementById('ws-close').addEventListener('click', () => {
      wsPanel.classList.remove('open');
      wsTogBtn.classList.remove('active');
    });

    document.getElementById('ws-refresh').addEventListener('click', loadWorkspaceTree);

    document.getElementById('ws-zip').addEventListener('click', async () => {
      const btn = document.getElementById('ws-zip');
      btn.textContent = '⏳';
      btn.disabled = true;
      try {
        const r = await fetch('/claude/workspace/zip', { headers: {'X-Token': token} });
        if (!r.ok) { showToast('Ошибка создания ZIP', 'error'); return; }
        const blob = await r.blob();
        const fname = (r.headers.get('content-disposition') || '').match(/filename="([^"]+)"/)?.[1] || 'workspace.zip';
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = fname;
        a.click();
        URL.revokeObjectURL(a.href);
        showToast('ZIP скачан', 'success', 2500);
        console.debug('[ws-zip] downloaded', fname);
      } catch(e) { showToast('Ошибка: ' + e.message, 'error'); }
      finally { btn.textContent = '⬇ ZIP'; btn.disabled = false; }
    });

    wsInput.addEventListener('change', () => { if (wsInput.files.length) wsUploadFiles(wsInput.files, ''); wsInput.value = ''; });
    wsUpload.addEventListener('click', () => wsInput.click());
    wsUpload.addEventListener('dragover', e => { e.preventDefault(); wsUpload.classList.add('drag-over'); });
    wsUpload.addEventListener('dragleave', () => wsUpload.classList.remove('drag-over'));
    wsUpload.addEventListener('drop', e => {
      e.preventDefault(); wsUpload.classList.remove('drag-over');
      if (e.dataTransfer.files.length) wsUploadFiles(e.dataTransfer.files, '');
    });

    async function afterAuth() {
      await loadSessions();
      await loadArchivedSessions();
      loadCommands();
      loadTemplates();
      if (sessionId) await openSession(sessionId);
      input.focus();
    }

    document.getElementById('login-btn').addEventListener('click', tryLogin);
    unameEl.addEventListener('keydown', e => { if (e.key === 'Enter') pwdEl.focus(); });
    pwdEl.addEventListener('keydown', e => { if (e.key === 'Enter') tryLogin(); });

    document.getElementById('new-chat-btn').addEventListener('click', () => {
      sessionId = '';
      localStorage.removeItem(SESSION_KEY);
      messages.innerHTML = '<div class="msg assistant"><div class="bubble">Привет! Чем могу помочь?</div></div>';
      termClear();
      resetCtxBar();
      document.querySelectorAll('.session-item').forEach(el => el.classList.remove('active'));
      input.focus();
    });

    if (token) {
      fetch('/claude/auth', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token}),
      }).then(async r => {
        if (r.ok) {
          const d = await r.json().catch(() => ({}));
          if (d.username) localStorage.setItem('claude_username', d.username);
          authEl.classList.add('hidden');
          afterAuth();
        } else {
          localStorage.removeItem(TOKEN_KEY);
          localStorage.removeItem('claude_username');
          token = '';
        }
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

    function addMsg(role, text = '', attachments = [], outputFiles = [], msgIndex = -1) {
      const div = document.createElement('div');
      div.className = `msg ${role}`;
      if (msgIndex >= 0) div.dataset.msgIndex = msgIndex;
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
      } else {
        b.textContent = text;
      }
      div.appendChild(b);
      if (role === 'assistant' && outputFiles.length) {
        renderOutputFiles(div, outputFiles);
      }
      // Action buttons (shown on hover)
      if (msgIndex >= 0 && sessionId) {
        const actions = document.createElement('div');
        actions.className = 'msg-actions';
        if (role === 'user') {
          const editBtn = document.createElement('button');
          editBtn.className = 'msg-action-btn';
          editBtn.textContent = '✏ Редактировать';
          editBtn.addEventListener('click', () => editMessage(msgIndex, text));
          actions.appendChild(editBtn);
        } else if (role === 'assistant') {
          const retryBtn = document.createElement('button');
          retryBtn.className = 'msg-action-btn';
          retryBtn.textContent = '↩ Повторить';
          retryBtn.addEventListener('click', () => retryMessage(msgIndex));
          actions.appendChild(retryBtn);
        }
        div.appendChild(actions);
      }
      messages.appendChild(div);
      maybeScroll();
      return b;
    }

    async function truncateSession(keepCount) {
      if (!sessionId) return false;
      try {
        const r = await fetch(`/claude/sessions/${sessionId}/truncate`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json', 'X-Token': token},
          body: JSON.stringify({keep: keepCount}),
        });
        return r.ok;
      } catch(e) { return false; }
    }

    async function editMessage(msgIndex, originalText) {
      if (send.disabled) return;
      // Keep messages before this user message, remove from msgIndex onwards
      const ok = await truncateSession(msgIndex);
      if (!ok) { showToast('Не удалось усечь сессию', 'error'); return; }
      // Remove from DOM: all messages from this index onwards
      document.querySelectorAll('.msg[data-msg-index]').forEach(el => {
        if (parseInt(el.dataset.msgIndex) >= msgIndex) el.remove();
      });
      input.value = originalText;
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 160) + 'px';
      input.focus();
      console.debug('[edit] truncated to', msgIndex, 'messages, text loaded');
    }

    async function retryMessage(assistantMsgIndex) {
      if (send.disabled) return;
      // assistantMsgIndex is the assistant msg — keep everything before it (msgIndex - 1 user msgs)
      const keepCount = assistantMsgIndex; // keep 0..assistantMsgIndex-1
      // Find the preceding user message text
      let userText = '';
      document.querySelectorAll('.msg[data-msg-index]').forEach(el => {
        if (parseInt(el.dataset.msgIndex) === assistantMsgIndex - 1) {
          userText = el.querySelector('.bubble')?.textContent || '';
        }
      });
      const ok = await truncateSession(keepCount);
      if (!ok) { showToast('Не удалось усечь сессию', 'error'); return; }
      document.querySelectorAll('.msg[data-msg-index]').forEach(el => {
        if (parseInt(el.dataset.msgIndex) >= keepCount) el.remove();
      });
      if (userText) {
        input.value = userText;
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 160) + 'px';
        form.dispatchEvent(new Event('submit'));
      }
      console.debug('[retry] retrying from index', assistantMsgIndex);
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
          showToast('Ошибка загрузки файла: ' + err.message, 'error');
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
      // Handle files copied from filesystem (have real paths)
      if (e.clipboardData.files.length) {
        e.preventDefault();
        handleFiles(e.clipboardData.files);
        return;
      }
      // Handle screenshots and image blobs from clipboard items
      const imageFiles = [];
      for (const item of (e.clipboardData.items || [])) {
        if (item.kind === 'file' && item.type.startsWith('image/')) {
          const blob = item.getAsFile();
          if (!blob) continue;
          const ext = item.type.split('/')[1] || 'png';
          const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
          // Rename blob to a meaningful filename since screenshots lack one
          const named = new File([blob], `screenshot-${ts}.${ext}`, {type: item.type});
          imageFiles.push(named);
        }
      }
      if (imageFiles.length) {
        e.preventDefault();
        handleFiles(imageFiles);
        showToast(`Вставлено ${imageFiles.length} изображение(й) из буфера`, 'success', 2500);
        console.debug('[paste] pasted', imageFiles.length, 'image(s) from clipboard');
      }
    });

    // ── Tool Use Progress Cards ────────────────────────
    let _lastToolCard = null;

    function getOrCreateToolCards(msgEl) {
      let tc = msgEl.querySelector('.tool-cards');
      if (!tc) {
        tc = document.createElement('div');
        tc.className = 'tool-cards';
        const bubble = msgEl.querySelector('.bubble');
        if (bubble) bubble.before(tc);
        else msgEl.prepend(tc);
      }
      return tc;
    }

    function parseToolName(text) {
      // "⚡ Bash(command=...)" → "Bash"
      // "⚡ Read(path=...)" → "Read"
      const m = text.match(/^⚡\s+([A-Za-z_]\w*)/);
      return m ? m[1] : text.slice(2, 30).trim();
    }

    function addToolCard(msgEl, toolText) {
      // Mark previous pending card as done
      if (_lastToolCard && _lastToolCard.classList.contains('pending')) {
        _lastToolCard.classList.remove('pending');
        _lastToolCard.classList.add('done');
        _lastToolCard.querySelector('.tc-icon').textContent = '✓';
      }
      const tc = getOrCreateToolCards(msgEl);
      const card = document.createElement('div');
      card.className = 'tool-card pending';
      const icon = document.createElement('span');
      icon.className = 'tc-icon';
      icon.textContent = '⟳';
      const name = document.createElement('span');
      name.className = 'tc-name';
      name.textContent = parseToolName(toolText);
      card.appendChild(icon);
      card.appendChild(name);
      tc.appendChild(card);
      _lastToolCard = card;
      console.debug('[tool-card] added', name.textContent);
    }

    function finishToolCard() {
      if (_lastToolCard && _lastToolCard.classList.contains('pending')) {
        _lastToolCard.classList.remove('pending');
        _lastToolCard.classList.add('done');
        _lastToolCard.querySelector('.tc-icon').textContent = '✓';
        _lastToolCard = null;
      }
    }

    function clearToolCards() {
      _lastToolCard = null;
    }

    // ── Chat ───────────────────────────────────────────
    const stopBtn = document.getElementById('stop-btn');
    let activeStreamId = null;
    let activeReader = null;

    function setStreaming(active) {
      send.disabled = active;
      stopBtn.classList.toggle('visible', active);
      console.debug('[stream] streaming state:', active);
    }

    stopBtn.addEventListener('click', async () => {
      console.debug('[stop] requested, stream_id=', activeStreamId);
      if (activeReader) { try { activeReader.cancel(); } catch(_) {} }
      if (activeStreamId) {
        try {
          await fetch('/claude/stop', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-Token': token},
            body: JSON.stringify({stream_id: activeStreamId}),
          });
        } catch(_) {}
        activeStreamId = null;
      }
      setStreaming(false);
    });

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
      setStreaming(true);
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
          setStreaming(false);
          return;
        }

        const reader = res.body.getReader();
        activeReader = reader;
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
                if (data.stream_id) {
                  activeStreamId = data.stream_id;
                  console.debug('[stream] got stream_id', data.stream_id);
                }
                if (data.session_id) {
                  console.debug('[stream] got session_id', data.session_id);
                  sessionId = data.session_id;
                  localStorage.setItem(SESSION_KEY, sessionId);
                }
                if (data.text) {
                  rawText += data.text;
                  bubble.textContent += data.text;
                  maybeScroll();
                }
                if (data.terminal) {
                  const cls = data.terminal.startsWith('⚡') ? 'tool'
                            : data.terminal.startsWith('←') ? 'result' : 'other';
                  termAppend(data.terminal, cls);
                  if (cls === 'tool') addToolCard(bubble.parentElement, data.terminal);
                  else if (cls === 'result') finishToolCard();
                }
                if (data.output_files && data.output_files.length) {
                  renderOutputFiles(bubble.parentElement, data.output_files);
                }
                if (data.usage) {
                  updateCtxBar(data.usage.input_tokens);
                }
              } catch(_) {}
            }
          }
        } finally {
          finishToolCard();
          clearToolCards();
          bubble.classList.remove('streaming');
          if (rawText) {
            applyMarkdown(bubble, rawText);
          }
          activeReader = null;
          activeStreamId = null;
        }
      } catch(err) {
        if (err.name !== 'AbortError') {
          bubble.textContent = '❌ Ошибка: ' + err.message;
          bubble.classList.remove('streaming');
          showToast('Ошибка запроса: ' + err.message, 'error');
        }
        activeReader = null;
        activeStreamId = null;
      }

      setStreaming(false);
      input.focus();
      await loadSessions();
    });
  </script>
  <div id="toast-container"></div>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────

@app.get("/claude/health")
async def health(request: Request):
    user = _authorized_user(request)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sf = _sessions_file(user)
    sessions = _load_sessions(sf)
    active = sum(1 for s in sessions if not s.get("archived"))
    archived = sum(1 for s in sessions if s.get("archived"))
    return JSONResponse({
        "status": "ok",
        "user": user if _USERS else None,
        "multi_user": bool(_USERS),
        "sessions": {"active": active, "archived": archived, "total": len(sessions)},
        "workspace_exists": WORKSPACE_DIR.exists(),
        "commands_dir_exists": COMMANDS_DIR.exists(),
        "skills_dirs": [str(d) for d in SKILLS_DIRS if d.exists()],
    })


@app.post("/claude/stop")
async def stop_stream(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    stream_id = (body.get("stream_id") or "").strip()
    if not stream_id:
        return JSONResponse({"error": "missing stream_id"}, status_code=400)
    proc = _active_streams.get(stream_id)
    if proc is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        proc.terminate()
        print(f"[INFO stop] terminated stream {stream_id}", flush=True)
    except Exception as e:
        print(f"[WARN stop] terminate failed for {stream_id}: {e}", flush=True)
    _active_streams.pop(stream_id, None)
    return JSONResponse({"stopped": stream_id})


@app.get("/claude")
@app.get("/claude/")
async def index():
    return HTMLResponse(HTML)


@app.get("/claude/auth/config")
async def auth_config():
    return JSONResponse({"multi_user": bool(_USERS)})


@app.post("/claude/auth")
async def auth(request: Request):
    body = await request.json()

    # ── Multi-user mode ───────────────────────────────────────────
    if _USERS:
        if "username" in body and "password" in body:
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            pwd = _USERS.get(username)
            if pwd is not None and hmac.compare_digest(pwd, password):
                token = f"{username}:{_make_user_token(username, pwd)}"
                print(f"[INFO auth] user '{username}' logged in", flush=True)
                return JSONResponse({"token": token, "username": username})
            return JSONResponse({"error": "wrong credentials"}, status_code=401)
        if "token" in body:
            tok = body.get("token") or ""
            if ":" in tok:
                username, _, tok_hex = tok.partition(":")
                pwd = _USERS.get(username)
                if pwd and hmac.compare_digest(tok_hex, _make_user_token(username, pwd)):
                    return JSONResponse({"ok": True, "username": username})
            return JSONResponse({"error": "invalid token"}, status_code=401)
        return JSONResponse({"error": "bad request"}, status_code=400)

    # ── Single-user mode (backward compat) ───────────────────────
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
    user = _authorized_user(request)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sf = _sessions_file(user)
    sessions = _load_sessions(sf)
    filtered = [s for s in sessions if bool(s.get("archived", False)) == archived]
    print(f"[DEBUG sessions] list user={user} archived={archived}: {len(filtered)} sessions", flush=True)
    return JSONResponse({"sessions": [{k: v for k, v in s.items() if k != "messages"} for s in filtered]})


@app.get("/claude/sessions/search")
async def search_sessions(request: Request, q: str = ""):
    user = _authorized_user(request)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sf = _sessions_file(user)
    q = q.strip()
    if not q:
        return JSONResponse({"results": []})
    ql = q.lower()
    results = []
    for s in _load_sessions(sf):
        if s.get("archived"):
            continue
        matches = 0
        snippet = ""
        for msg in s.get("messages", []):
            text = msg.get("text") or ""
            idx = text.lower().find(ql)
            if idx != -1:
                matches += 1
                if not snippet:
                    start = max(0, idx - 40)
                    end = min(len(text), idx + len(q) + 60)
                    raw = text[start:end].replace("\n", " ").strip()
                    snippet = ("…" if start > 0 else "") + raw + ("…" if end < len(text) else "")
        if matches:
            results.append({
                "session_id": s["session_id"],
                "title": s.get("title", ""),
                "updated_at": s.get("updated_at", ""),
                "match_count": matches,
                "snippet": snippet,
            })
    results.sort(key=lambda x: x["match_count"], reverse=True)
    print(f"[DEBUG search] q={q!r} → {len(results)} sessions", flush=True)
    return JSONResponse({"results": results[:50]})


@app.get("/claude/sessions/{session_id}")
async def get_session(session_id: str, request: Request):
    user = _authorized_user(request)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sf = _sessions_file(user)
    for s in _load_sessions(sf):
        if s["session_id"] == session_id:
            return JSONResponse(s)
    return JSONResponse({"error": "not found"}, status_code=404)


@app.delete("/claude/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    user = _authorized_user(request)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sf = _sessions_file(user)
    sessions = _load_sessions(sf)
    now = datetime.utcnow().isoformat()
    for s in sessions:
        if s["session_id"] == session_id:
            s["archived"] = True
            s["updated_at"] = now
            _write_sessions(sessions, sf)
            print(f"[INFO sessions] archived sid={session_id} user={user}", flush=True)
            return JSONResponse({"ok": True, "archived": True})
    return JSONResponse({"error": "not found"}, status_code=404)


@app.delete("/claude/sessions/{session_id}/permanent")
async def permanent_delete_session(session_id: str, request: Request):
    user = _authorized_user(request)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sf = _sessions_file(user)
    sessions = _load_sessions(sf)
    new_sessions = [s for s in sessions if s["session_id"] != session_id]
    if len(new_sessions) == len(sessions):
        return JSONResponse({"error": "not found"}, status_code=404)
    _write_sessions(new_sessions, sf)
    print(f"[INFO sessions] permanently deleted sid={session_id} user={user}", flush=True)
    return JSONResponse({"ok": True})


@app.patch("/claude/sessions/{session_id}")
async def update_session(session_id: str, request: Request):
    user = _authorized_user(request)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sf = _sessions_file(user)
    body = await request.json()
    sessions = _load_sessions(sf)
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
            _write_sessions(sessions, sf)
            return JSONResponse({k: v for k, v in s.items() if k != "messages"})
    return JSONResponse({"error": "not found"}, status_code=404)


@app.post("/claude/sessions/{session_id}/truncate")
async def truncate_session(session_id: str, request: Request):
    user = _authorized_user(request)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sf = _sessions_file(user)
    body = await request.json()
    keep = int(body.get("keep", 0))
    sessions = _load_sessions(sf)
    for s in sessions:
        if s["session_id"] == session_id:
            original = len(s.get("messages", []))
            s["messages"] = s.get("messages", [])[:keep]
            s["updated_at"] = datetime.utcnow().isoformat()
            _write_sessions(sessions, sf)
            print(f"[INFO sessions] truncated sid={session_id} kept={keep} removed={original - keep}", flush=True)
            return JSONResponse({"kept": keep, "removed": original - keep})
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/claude/sessions/{session_id}/export")
async def export_session(session_id: str, request: Request, format: str = "md"):
    user = _authorized_user(request)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sf = _sessions_file(user)
    from fastapi.responses import Response as FResponse
    for s in _load_sessions(sf):
        if s["session_id"] == session_id:
            safe_title = re.sub(r"[^\w\s-]", "", s.get("title", "session"))[:40].strip().replace(" ", "_")
            base = f"claude_{safe_title or session_id[:8]}"
            if format == "json":
                payload = json.dumps(s, ensure_ascii=False, indent=2)
                filename = base + ".json"
                print(f"[DEBUG sessions] export JSON sid={session_id} file={filename}", flush=True)
                return FResponse(
                    content=payload,
                    media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'},
                )
            # default: markdown
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
            filename = base + ".md"
            print(f"[DEBUG sessions] export MD sid={session_id} messages={len(s.get('messages', []))} file={filename}", flush=True)
            return FResponse(
                content=md,
                media_type="text/markdown",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/claude/templates")
async def get_templates(request: Request):
    user = _authorized_user(request)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    tf = _templates_file(user)
    return JSONResponse({"templates": _load_templates(tf)})


@app.post("/claude/templates")
async def create_template(request: Request):
    user = _authorized_user(request)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    name = (body.get("name") or "").strip()
    text = (body.get("text") or "").strip()
    if not name or not text:
        return JSONResponse({"error": "name and text required"}, status_code=400)
    tf = _templates_file(user)
    templates = _load_templates(tf)
    tpl = {"id": uuid.uuid4().hex, "name": name, "text": text,
           "created_at": datetime.utcnow().isoformat()}
    templates.insert(0, tpl)
    _write_templates(templates, tf)
    return JSONResponse(tpl)


@app.patch("/claude/templates/{tid}")
async def update_template(request: Request, tid: str):
    user = _authorized_user(request)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    tf = _templates_file(user)
    templates = _load_templates(tf)
    for t in templates:
        if t["id"] == tid:
            if "name" in body:
                t["name"] = (body["name"] or "").strip()
            if "text" in body:
                t["text"] = body["text"]
            _write_templates(templates, tf)
            return JSONResponse(t)
    return JSONResponse({"error": "not found"}, status_code=404)


@app.delete("/claude/templates/{tid}")
async def delete_template(request: Request, tid: str):
    user = _authorized_user(request)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    tf = _templates_file(user)
    templates = _load_templates(tf)
    new = [t for t in templates if t["id"] != tid]
    if len(new) == len(templates):
        return JSONResponse({"error": "not found"}, status_code=404)
    _write_templates(new, tf)
    return JSONResponse({"ok": True})


@app.get("/claude/commands")
async def get_commands(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    commands = _load_commands()
    print(f"[DEBUG commands] serving {len(commands)} commands", flush=True)
    return JSONResponse({"commands": commands})


@app.get("/claude/skills")
async def get_skills(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    skills = _load_skills()
    print(f"[DEBUG skills] serving {len(skills)} skills", flush=True)
    return JSONResponse({"skills": skills})


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


_MAX_TREE_CHILDREN = 200  # max items per directory level in tree response

def _workspace_tree(base: Path, rel: str = "", depth: int = 0, max_depth: int = 6) -> dict:
    """Recursively build workspace directory tree."""
    name = base.name
    node: dict = {"name": name, "rel_path": rel, "type": "dir", "children": []}
    if depth >= max_depth:
        return node
    try:
        entries = sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        count = 0
        for p in entries:
            if p.name.startswith("."):
                continue
            if count >= _MAX_TREE_CHILDREN:
                node["truncated"] = True
                print(f"[WARN tree] {rel or '/'}: truncated at {_MAX_TREE_CHILDREN} items", flush=True)
                break
            child_rel = (rel + "/" + p.name).lstrip("/")
            if p.is_dir():
                node["children"].append(_workspace_tree(p, child_rel, depth + 1, max_depth))
            else:
                ext = p.suffix.lower()
                try:
                    size = p.stat().st_size
                except Exception:
                    size = 0
                node["children"].append({
                    "name": p.name,
                    "rel_path": child_rel,
                    "type": "file",
                    "ext": ext,
                    "is_image": ext in _IMAGE_EXTS,
                    "size": size,
                })
            count += 1
    except PermissionError:
        pass
    files = sum(1 for c in node["children"] if c["type"] == "file")
    dirs  = sum(1 for c in node["children"] if c["type"] == "dir")
    print(f"[DEBUG tree] {rel or '/'}: {dirs} dirs, {files} files", flush=True)
    return node


@app.get("/claude/workspace/zip")
async def workspace_zip(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import io, zipfile
    from datetime import datetime as _dt
    from fastapi.responses import StreamingResponse as _SR
    buf = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if WORKSPACE_DIR.exists():
            for p in WORKSPACE_DIR.rglob("*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(WORKSPACE_DIR)
                parts = rel.parts
                if parts and (parts[0] == ".uploads" or parts[0].startswith(".")):
                    continue
                try:
                    zf.write(p, rel)
                    total += 1
                except Exception as e:
                    print(f"[WARN zip] skipped {p}: {e}", flush=True)
    buf.seek(0)
    fname = f"workspace-{_dt.utcnow().strftime('%Y%m%d-%H%M%S')}.zip"
    print(f"[INFO zip] created {fname} with {total} files ({buf.getbuffer().nbytes} bytes)", flush=True)
    return _SR(
        content=buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/claude/workspace/tree")
async def workspace_tree(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not WORKSPACE_DIR.exists():
        return JSONResponse({"tree": []})
    root = _workspace_tree(WORKSPACE_DIR)
    print(f"[DEBUG tree] root children={len(root['children'])}", flush=True)
    return JSONResponse({"tree": root["children"]})


@app.post("/claude/workspace/upload")
async def workspace_upload(request: Request,
                           files: list[UploadFile] = File(default=[]),
                           dir: str = ""):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not files:
        return JSONResponse({"error": "no files"}, status_code=400)
    # Sanitize target directory
    safe_dir = Path(dir.lstrip("/")) if dir else Path(".")
    target = (WORKSPACE_DIR / safe_dir).resolve()
    try:
        target.relative_to(WORKSPACE_DIR.resolve())
    except ValueError:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    target.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        if not f.filename:
            continue
        data = await f.read()
        if len(data) > MAX_UPLOAD_BYTES:
            return JSONResponse({"error": f"{f.filename} too large"}, status_code=413)
        fname = _safe_filename(f.filename)
        dest = target / fname
        dest.write_bytes(data)
        rel = str(dest.relative_to(WORKSPACE_DIR))
        ext = dest.suffix.lower()
        saved.append({"name": fname, "rel_path": rel, "is_image": ext in _IMAGE_EXTS, "size": len(data)})
        print(f"[DEBUG ws-upload] saved {rel} ({len(data)}b)", flush=True)
    print(f"[INFO ws-upload] uploaded {len(saved)} file(s) to {target}", flush=True)
    return JSONResponse({"files": saved})


async def _anthropic_stream(prompt: str, augmented: str, image_attachments: list,
                            model: str, session_id: str, attachments: list,
                            sf: Optional[Path] = None):
    """Async SSE generator for multimodal messages.
    Uses claude CLI with --input-format stream-json so it can use its own credentials.
    """
    ws_before = await asyncio.to_thread(_snapshot_workspace)

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

    stream_id = uuid.uuid4().hex[:16]
    _active_streams[stream_id] = proc
    yield f"data: {json.dumps({'stream_id': stream_id})}\n\n"
    print(f"[DEBUG multimodal] registered stream_id={stream_id}", flush=True)

    final_sid = session_id
    parts: list[str] = []
    result_text: str | None = None  # type: ignore[assignment]
    is_error = False
    final_usage: Optional[dict] = None
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
                    "usage": ev.get("usage"),
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
            if val.get("usage"):
                final_usage = val["usage"]
                yield f"data: {json.dumps({'usage': final_usage})}\n\n"
            if val["sid"] and not final_sid:
                final_sid = val["sid"]
                yield f"data: {json.dumps({'session_id': val['sid']})}\n\n"
        elif kind == "out_done":
            out_done = True

    await proc.wait()

    ws_after = await asyncio.to_thread(_snapshot_workspace)
    output_files = _diff_workspace(ws_before, ws_after)
    if output_files:
        yield f"data: {json.dumps({'output_files': output_files})}\n\n"
        print(f"[INFO multimodal] {len(output_files)} output file(s) detected", flush=True)

    yield f"data: {json.dumps({'done': True})}\n\n"

    assistant_text = (("[ERROR] " if is_error else "") + result_text) if result_text is not None else "".join(parts)
    if not final_sid:
        final_sid = f"img-{uuid.uuid4().hex[:12]}"
    _active_streams.pop(stream_id, None)
    _upsert_session(final_sid, prompt, assistant_text, attachments=attachments,
                    output_files=output_files or None, sf=sf, usage=final_usage)
    print(f"[INFO multimodal] session saved sid={final_sid} text_len={len(assistant_text)}", flush=True)


@app.post("/claude/ask")
async def ask(request: Request):
    user = _authorized_user(request)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sf = _sessions_file(user)

    body        = await request.json()
    prompt      = (body.get("prompt") or "").strip()
    model       = (body.get("model") or "claude-sonnet-4-6").strip()
    session_id  = (body.get("session_id") or "").strip()
    attachments = body.get("attachments") or []
    if model not in {"claude-fable-5", "claude-opus-4-8", "claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5-20251001"}:
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

        ws_before = await asyncio.to_thread(_snapshot_workspace)

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

        stream_id = uuid.uuid4().hex[:16]
        _active_streams[stream_id] = proc
        yield f"data: {json.dumps({'stream_id': stream_id})}\n\n"
        print(f"[DEBUG stream] registered stream_id={stream_id}", flush=True)

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
            prev_full_text = ""
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
                            full_text += c.get("text", "")
                    # New assistant turn after tool call: text doesn't continue from previous
                    if prev_len > 0 and not full_text.startswith(prev_full_text):
                        prev_len = 0
                    if len(full_text) < prev_len:
                        prev_len = 0
                    delta = full_text[prev_len:]
                    if delta:
                        print(f"[DEBUG stream] delta len={len(delta)}", flush=True)
                        await q.put(("delta", delta))
                        prev_len = len(full_text)
                        prev_full_text = full_text
                elif etype == "result":
                    await q.put(("result", {
                        "text":     event.get("result", ""),
                        "sid":      event.get("session_id", ""),
                        "is_error": event.get("is_error", False),
                        "usage":    event.get("usage"),
                    }))
            await q.put(("out_done", ""))

        asyncio.create_task(_stderr_reader())
        asyncio.create_task(_stdout_line_reader())

        t_done = False
        out_done = False
        final_usage: Optional[dict] = None
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
                if val.get("usage"):
                    final_usage = val["usage"]
                    yield f"data: {json.dumps({'usage': final_usage})}\n\n"
                if val["sid"] and not final_sid:
                    final_sid = val["sid"]
                    yield f"data: {json.dumps({'session_id': val['sid']})}\n\n"
            elif kind == "out_done":
                out_done = True

        await proc.wait()

        ws_after = await asyncio.to_thread(_snapshot_workspace)
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
                            output_files=output_files or None, sf=sf, usage=final_usage)
            print(f"[INFO stream] session saved sid={final_sid} text_len={len(assistant_text)} is_error={is_error} attachments={len(attachments)}", flush=True)
        _active_streams.pop(stream_id, None)
        asyncio.create_task(_git_push(env))

    if image_attachments:
        return StreamingResponse(
            _anthropic_stream(prompt, augmented, image_attachments, model,
                              session_id, attachments, sf=sf),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
