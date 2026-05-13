import asyncio, json, os, hmac, hashlib, shlex
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse

app = FastAPI()

APP_PASSWORD  = os.environ.get("APP_PASSWORD", "")
OBSIDIAN_PATH = os.environ.get("OBSIDIAN_PATH", "/home/node/obsidian")
_TOKEN = hmac.new(b"claude-ui", APP_PASSWORD.encode(), hashlib.sha256).hexdigest() if APP_PASSWORD else ""


async def _git_push(env: dict):
    try:
        r = await asyncio.create_subprocess_exec(
            "git", "-C", OBSIDIAN_PATH, "status", "--porcelain",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env,
        )
        out, _ = await r.communicate()
        if not out.strip():
            return  # nothing changed
        for cmd in [
            ["git", "-C", OBSIDIAN_PATH, "add", "-A"],
            ["git", "-C", OBSIDIAN_PATH, "commit", "-m", "claude: auto-update"],
            ["git", "-C", OBSIDIAN_PATH, "push"],
        ]:
            p = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, env=env,
            )
            await p.communicate()
    except Exception:
        pass


def _authorized(request: Request) -> bool:
    token = request.headers.get("X-Token", "")
    return bool(_TOKEN) and hmac.compare_digest(token, _TOKEN)


HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Claude</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    html,body{height:100%;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f0f;color:#e5e5e5}
    body{display:flex;flex-direction:column;height:100vh}

    /* ── Auth screen ─────────────────────────────────────────────── */
    #auth{position:fixed;inset:0;background:#0f0f0f;display:flex;align-items:center;justify-content:center;z-index:100}
    #auth.hidden{display:none}
    .auth-card{background:#1a1a1a;border-radius:20px;padding:36px 28px;width:100%;max-width:360px;display:flex;flex-direction:column;gap:16px}
    .auth-card h2{font-size:20px;font-weight:700;text-align:center}
    .auth-card input{background:#0f0f0f;border:1px solid #2a2a2a;color:#e5e5e5;padding:14px 16px;border-radius:12px;font-size:16px;outline:none;transition:border-color .15s}
    .auth-card input:focus{border-color:#4f46e5}
    .auth-card button{background:#4f46e5;color:#fff;border:none;padding:14px;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer;transition:background .15s}
    .auth-card button:active{background:#3730a3}
    .auth-card .err{color:#f87171;font-size:13px;text-align:center;min-height:18px}

    /* ── Chat ────────────────────────────────────────────────────── */
    #header{padding:16px 20px;border-bottom:1px solid #1e1e1e;display:flex;align-items:center;gap:10px;flex-shrink:0}
    #header span{font-size:18px;font-weight:600}
    #header .dot{width:8px;height:8px;border-radius:50%;background:#22c55e}
    #model{margin-left:auto;background:#1a1a1a;border:1px solid #2a2a2a;color:#aaa;padding:6px 10px;border-radius:8px;font-size:13px;outline:none;cursor:pointer}
    #model:focus{border-color:#4f46e5}
    #messages{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:16px}
    .msg{max-width:80%;display:flex;flex-direction:column;gap:4px}
    .msg.user{align-self:flex-end}
    .msg.assistant{align-self:flex-start}
    .bubble{padding:12px 16px;border-radius:16px;line-height:1.55;white-space:pre-wrap;word-break:break-word;font-size:14px}
    .msg.user .bubble{background:#4f46e5;color:#fff;border-bottom-right-radius:4px}
    .msg.assistant .bubble{background:#1a1a1a;color:#e5e5e5;border-bottom-left-radius:4px}
    .bubble.streaming::after{content:'▋';animation:blink .7s infinite;margin-left:2px}
    @keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
    #footer{padding:16px 20px;border-top:1px solid #1e1e1e;flex-shrink:0}
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

  <!-- Auth screen -->
  <div id="auth">
    <div class="auth-card">
      <h2>⚡ Claude</h2>
      <input type="password" id="pwd" placeholder="Пароль" autofocus>
      <button id="login-btn">Войти</button>
      <div class="err" id="auth-err"></div>
    </div>
  </div>

  <!-- Chat -->
  <div id="header">
    <div class="dot"></div>
    <span>Claude</span>
    <select id="model" title="Модель">
      <option value="claude-sonnet-4-6">Sonnet 4.6</option>
      <option value="claude-opus-4-7">Opus 4.7</option>
      <option value="claude-haiku-4-5-20251001">Haiku 4.5</option>
    </select>
  </div>
  <div id="messages">
    <div class="msg assistant"><div class="bubble">Привет! Чем могу помочь?</div></div>
  </div>
  <div id="footer">
    <form id="form">
      <textarea id="input" rows="1" placeholder="Напиши сообщение..."></textarea>
      <button id="send" type="submit">
        <svg viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
      </button>
    </form>
  </div>

  <script>
    const TOKEN_KEY = 'claude_token';
    let token = sessionStorage.getItem(TOKEN_KEY) || '';

    const authEl    = document.getElementById('auth');
    const pwdEl     = document.getElementById('pwd');
    const loginBtn  = document.getElementById('login-btn');
    const authErr   = document.getElementById('auth-err');
    const messages  = document.getElementById('messages');
    const input     = document.getElementById('input');
    const send      = document.getElementById('send');
    const form      = document.getElementById('form');

    // ── Auth ──────────────────────────────────────────────────────
    async function tryLogin() {
      authErr.textContent = '';
      const pwd = pwdEl.value.trim();
      if (!pwd) return;
      try {
        const r = await fetch('/claude/auth', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({password: pwd}),
        });
        const d = await r.json();
        if (r.ok && d.token) {
          token = d.token;
          sessionStorage.setItem(TOKEN_KEY, token);
          authEl.classList.add('hidden');
          input.focus();
        } else {
          authErr.textContent = 'Неверный пароль';
          pwdEl.value = '';
          pwdEl.focus();
        }
      } catch(e) {
        authErr.textContent = 'Ошибка соединения';
      }
    }

    loginBtn.addEventListener('click', tryLogin);
    pwdEl.addEventListener('keydown', e => { if (e.key === 'Enter') tryLogin(); });

    if (token) {
      // Validate stored token silently
      fetch('/claude/auth', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({token}),
      }).then(r => {
        if (r.ok) authEl.classList.add('hidden');
        else { sessionStorage.removeItem(TOKEN_KEY); token = ''; }
      }).catch(() => {});
    }

    // ── Chat ──────────────────────────────────────────────────────
    input.addEventListener('input', () => {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 160) + 'px';
    });
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.dispatchEvent(new Event('submit')); }
    });

    function addMessage(role, text) {
      const div = document.createElement('div');
      div.className = `msg ${role}`;
      const bubble = document.createElement('div');
      bubble.className = 'bubble';
      bubble.textContent = text;
      div.appendChild(bubble);
      messages.appendChild(div);
      messages.scrollTop = messages.scrollHeight;
      return bubble;
    }

    form.addEventListener('submit', async e => {
      e.preventDefault();
      const prompt = input.value.trim();
      if (!prompt || send.disabled) return;

      addMessage('user', prompt);
      input.value = '';
      input.style.height = 'auto';
      send.disabled = true;

      const bubble = addMessage('assistant', '');
      bubble.classList.add('streaming');

      try {
        const model = document.getElementById('model').value;
        const res = await fetch('/claude/ask', {
          method: 'POST',
          headers: {'Content-Type': 'application/json', 'X-Token': token},
          body: JSON.stringify({prompt, model}),
        });
        if (res.status === 401) {
          bubble.textContent = '🔒 Сессия истекла, перезагрузи страницу';
          sessionStorage.removeItem(TOKEN_KEY);
          bubble.classList.remove('streaming');
          send.disabled = false;
          return;
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';
        while (true) {
          const {done, value} = await reader.read();
          if (done) break;
          buf += decoder.decode(value, {stream: true});
          const lines = buf.split('\n');
          buf = lines.pop();
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            const data = JSON.parse(line.slice(6));
            if (data.done) break;
            if (data.text) { bubble.textContent += data.text; messages.scrollTop = messages.scrollHeight; }
          }
        }
      } catch(err) {
        bubble.textContent = '❌ Ошибка: ' + err.message;
      }
      bubble.classList.remove('streaming');
      send.disabled = false;
      input.focus();
    });
  </script>
</body>
</html>"""


@app.get("/claude")
@app.get("/claude/")
async def index():
    return HTMLResponse(HTML)


@app.post("/claude/auth")
async def auth(request: Request):
    body = await request.json()
    # Validate by password
    if "password" in body:
        if APP_PASSWORD and hmac.compare_digest(body["password"], APP_PASSWORD):
            return JSONResponse({"token": _TOKEN})
        return JSONResponse({"error": "wrong password"}, status_code=401)
    # Validate by existing token
    if "token" in body:
        if _TOKEN and hmac.compare_digest(body["token"], _TOKEN):
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "invalid token"}, status_code=401)
    return JSONResponse({"error": "bad request"}, status_code=400)


@app.post("/claude/ask")
async def ask(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    model = (body.get("model") or "claude-sonnet-4-6").strip()
    allowed_models = {"claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5-20251001"}
    if model not in allowed_models:
        model = "claude-sonnet-4-6"
    if not prompt:
        return {"error": "empty prompt"}

    async def stream():
        env = {**os.environ, "HOME": "/home/node"}
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            "--model", model,
            "--dangerously-skip-permissions",
            "--max-turns", "20",
            "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        stdout, _ = await proc.communicate()
        try:
            data = json.loads(stdout.decode("utf-8", errors="replace"))
            text = data.get("result", "")
        except Exception:
            text = stdout.decode("utf-8", errors="replace")
        yield f"data: {json.dumps({'text': text})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"

        # Auto-push to GitHub if anything was written to obsidian
        asyncio.create_task(_git_push(env))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
