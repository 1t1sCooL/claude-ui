import asyncio, json, os, hmac, hashlib
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse

app = FastAPI()

APP_PASSWORD  = os.environ.get("APP_PASSWORD", "")
OBSIDIAN_PATH = os.environ.get("OBSIDIAN_PATH", "/home/node/obsidian")
SESSIONS_FILE = Path(os.environ.get("SESSIONS_FILE", "/home/node/sessions.json"))
_TOKEN = hmac.new(b"claude-ui", APP_PASSWORD.encode(), hashlib.sha256).hexdigest() if APP_PASSWORD else ""


# ── Sessions ──────────────────────────────────────────────────────

def _load_sessions() -> list:
    try:
        return json.loads(SESSIONS_FILE.read_text()) if SESSIONS_FILE.exists() else []
    except Exception:
        return []

def _write_sessions(sessions: list):
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSIONS_FILE.write_text(json.dumps(sessions, ensure_ascii=False, indent=2))

def _upsert_session(session_id: str, user_msg: str, assistant_msg: str):
    sessions = _load_sessions()
    now = datetime.utcnow().isoformat()
    for s in sessions:
        if s["session_id"] == session_id:
            s["updated_at"] = now
            s["messages"].extend([
                {"role": "user",      "text": user_msg},
                {"role": "assistant", "text": assistant_msg},
            ])
            _write_sessions(sessions)
            return
    title = user_msg[:60] + ("…" if len(user_msg) > 60 else "")
    sessions.insert(0, {
        "session_id": session_id,
        "title":      title,
        "created_at": now,
        "updated_at": now,
        "messages": [
            {"role": "user",      "text": user_msg},
            {"role": "assistant", "text": assistant_msg},
        ],
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
    #session-list{flex:1;overflow-y:auto;padding:6px}
    .session-item{display:flex;align-items:center;gap:6px;padding:9px 10px;border-radius:9px;cursor:pointer;transition:background .15s;margin-bottom:2px}
    .session-item:hover{background:#1a1a1a}
    .session-item.active{background:#1e1a2e;border:1px solid #2d2060}
    .session-info{flex:1;overflow:hidden}
    .session-title{font-size:12px;color:#ccc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .session-date{font-size:10px;color:#555;margin-top:2px}
    .session-del{background:none;border:none;color:#3a3a3a;cursor:pointer;padding:2px 6px;border-radius:4px;font-size:16px;flex-shrink:0;line-height:1;transition:color .15s}
    .session-del:hover{color:#f87171}

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
    #footer{padding:12px 20px;border-top:1px solid #1e1e1e;flex-shrink:0}
    #form{display:flex;gap:10px;align-items:flex-end}
    #input{flex:1;background:#1a1a1a;border:1px solid #2a2a2a;color:#e5e5e5;padding:12px 16px;border-radius:14px;font-size:15px;resize:none;outline:none;min-height:48px;max-height:160px;line-height:1.4;font-family:inherit;transition:border-color .15s}
    #input:focus{border-color:#4f46e5}
    #input::placeholder{color:#555}
    #send{background:#4f46e5;border:none;color:#fff;width:44px;height:44px;border-radius:12px;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;transition:background .15s}
    #send:hover{background:#4338ca}
    #send:disabled{opacity:.4;cursor:not-allowed}
    #send svg{width:20px;height:20px;fill:none;stroke:#fff;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
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
    <div id="session-list"></div>
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
      <form id="form">
        <textarea id="input" rows="1" placeholder="Напиши сообщение..."></textarea>
        <button id="send" type="submit">
          <svg viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
        </button>
      </form>
    </div>
  </div>

  <script>
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

    function renderSessions(list) {
      sesList.innerHTML = '';
      for (const s of list) {
        const item = document.createElement('div');
        item.className = 'session-item' + (s.session_id === sessionId ? ' active' : '');
        item.dataset.sid = s.session_id;
        item.title = s.title || 'Без названия';

        const info = document.createElement('div');
        info.className = 'session-info';
        const title = document.createElement('div');
        title.className = 'session-title';
        title.textContent = s.title || 'Без названия';
        const date = document.createElement('div');
        date.className = 'session-date';
        date.textContent = fmtDate(s.updated_at);
        info.append(title, date);

        const del = document.createElement('button');
        del.className = 'session-del';
        del.title = 'Удалить';
        del.textContent = '×';
        del.addEventListener('click', async e => {
          e.stopPropagation();
          await deleteSession(s.session_id);
        });

        item.append(info, del);
        item.addEventListener('click', () => openSession(s.session_id));
        sesList.appendChild(item);
      }
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
          const div = document.createElement('div');
          div.className = `msg ${m.role}`;
          const b = document.createElement('div');
          b.className = 'bubble';
          b.textContent = m.text;
          div.appendChild(b);
          messages.appendChild(div);
        }
        if (!s.messages?.length) {
          messages.innerHTML = '<div class="msg assistant"><div class="bubble">Привет! Чем могу помочь?</div></div>';
        }
        messages.scrollTop = messages.scrollHeight;
        termClear();
      } catch(e) {}
    }

    async function deleteSession(sid) {
      try {
        await fetch(`/claude/sessions/${sid}`, { method: 'DELETE', headers: {'X-Token': token} });
        if (sessionId === sid) {
          sessionId = '';
          localStorage.removeItem(SESSION_KEY);
          messages.innerHTML = '<div class="msg assistant"><div class="bubble">Привет! Чем могу помочь?</div></div>';
          termClear();
        }
        await loadSessions();
      } catch(e) {}
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

    // ── Chat ───────────────────────────────────────────
    input.addEventListener('input', () => {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 160) + 'px';
    });
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.dispatchEvent(new Event('submit')); }
    });

    function addMsg(role, text = '') {
      const div = document.createElement('div');
      div.className = `msg ${role}`;
      const b = document.createElement('div');
      b.className = 'bubble';
      b.textContent = text;
      div.appendChild(b);
      messages.appendChild(div);
      messages.scrollTop = messages.scrollHeight;
      return b;
    }

    form.addEventListener('submit', async e => {
      e.preventDefault();
      const prompt = input.value.trim();
      if (!prompt || send.disabled) return;

      addMsg('user', prompt);
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
          body: JSON.stringify({prompt, model, session_id: sessionId}),
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

        while (true) {
          const {done, value} = await reader.read();
          if (done) break;
          buf += dec.decode(value, {stream: true});
          const lines = buf.split('\n');
          buf = lines.pop();
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const data = JSON.parse(line.slice(6));
              if (data.done) break;
              if (data.session_id) {
                sessionId = data.session_id;
                localStorage.setItem(SESSION_KEY, sessionId);
              }
              if (data.text) {
                bubble.textContent += data.text;
                messages.scrollTop = messages.scrollHeight;
              }
              if (data.terminal) {
                const cls = data.terminal.startsWith('⚡') ? 'tool'
                          : data.terminal.startsWith('←') ? 'result' : 'other';
                termAppend(data.terminal, cls);
              }
            } catch(_) {}
          }
        }
      } catch(err) {
        bubble.textContent = '❌ Ошибка: ' + err.message;
      }

      bubble.classList.remove('streaming');
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
async def list_sessions(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sessions = _load_sessions()
    return JSONResponse([{k: v for k, v in s.items() if k != "messages"} for s in sessions])


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
    sessions = [s for s in _load_sessions() if s["session_id"] != session_id]
    _write_sessions(sessions)
    return JSONResponse({"ok": True})


@app.post("/claude/ask")
async def ask(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body       = await request.json()
    prompt     = (body.get("prompt") or "").strip()
    model      = (body.get("model") or "claude-sonnet-4-6").strip()
    session_id = (body.get("session_id") or "").strip()
    if model not in {"claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5-20251001"}:
        model = "claude-sonnet-4-6"
    if not prompt:
        return JSONResponse({"error": "empty prompt"})

    async def stream():
        env = {**os.environ, "HOME": "/home/node"}
        cmd = ["claude", "-p", prompt, "--model", model,
               "--dangerously-skip-permissions", "--max-turns", "20",
               "--output-format", "json"]
        if session_id:
            cmd += ["--resume", session_id]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        final_sid = session_id
        parts: list[str] = []
        q: asyncio.Queue = asyncio.Queue()

        async def _stderr_reader():
            async for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    await q.put(("t", line))
            await q.put(("t_done", ""))

        async def _stdout_reader():
            data = await proc.stdout.read()
            await q.put(("out", data.decode("utf-8", errors="replace")))

        asyncio.create_task(_stderr_reader())
        asyncio.create_task(_stdout_reader())

        t_done = False
        out_done = False
        while not (t_done and out_done):
            kind, val = await q.get()
            if kind == "t":
                yield f"data: {json.dumps({'terminal': val})}\n\n"
            elif kind == "t_done":
                t_done = True
            elif kind == "out":
                out_done = True
                try:
                    data = json.loads(val)
                    text = data.get("result", "")
                    sid  = data.get("session_id", "")
                except Exception:
                    text = val
                    sid  = ""
                if sid:
                    final_sid = sid
                    yield f"data: {json.dumps({'session_id': sid})}\n\n"
                if text:
                    parts.append(text)
                    yield f"data: {json.dumps({'text': text})}\n\n"

        await proc.wait()
        yield f"data: {json.dumps({'done': True})}\n\n"

        if final_sid:
            _upsert_session(final_sid, prompt, "".join(parts))
        asyncio.create_task(_git_push(env))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
