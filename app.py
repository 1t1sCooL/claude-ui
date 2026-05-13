import asyncio, json, os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse

app = FastAPI()

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
    #header{padding:16px 20px;border-bottom:1px solid #1e1e1e;display:flex;align-items:center;gap:10px;flex-shrink:0}
    #header span{font-size:18px;font-weight:600}
    #header .dot{width:8px;height:8px;border-radius:50%;background:#22c55e}
    #messages{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:16px}
    .msg{max-width:80%;display:flex;flex-direction:column;gap:4px}
    .msg.user{align-self:flex-end}
    .msg.assistant{align-self:flex-start}
    .bubble{padding:12px 16px;border-radius:16px;line-height:1.55;white-space:pre-wrap;word-break:break-word;font-size:14px}
    .msg.user .bubble{background:#4f46e5;color:#fff;border-bottom-right-radius:4px}
    .msg.assistant .bubble{background:#1a1a1a;color:#e5e5e5;border-bottom-left-radius:4px}
    .bubble.streaming::after{content:'▋';animation:blink .7s infinite;margin-left:2px}
    @keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
    code{background:#2a2a2a;padding:1px 5px;border-radius:4px;font-size:13px;font-family:'Fira Code',monospace}
    pre{background:#1e1e1e;border-radius:10px;padding:14px;overflow-x:auto;margin:6px 0}
    pre code{background:none;padding:0}
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
  <div id="header">
    <div class="dot"></div>
    <span>Claude</span>
  </div>
  <div id="messages">
    <div class="msg assistant"><div class="bubble">Привет! Чем могу помочь?</div></div>
  </div>
  <div id="footer">
    <form id="form">
      <textarea id="input" rows="1" placeholder="Напиши сообщение..." autofocus></textarea>
      <button id="send" type="submit">
        <svg viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
      </button>
    </form>
  </div>

  <script>
    const messages = document.getElementById('messages');
    const input    = document.getElementById('input');
    const send     = document.getElementById('send');
    const form     = document.getElementById('form');

    // Auto-resize textarea
    input.addEventListener('input', () => {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 160) + 'px';
    });

    // Submit on Enter (Shift+Enter = newline)
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
        const res = await fetch('/claude/ask', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt }),
        });

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop();
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            const data = JSON.parse(line.slice(6));
            if (data.done) break;
            if (data.text) bubble.textContent += data.text;
            messages.scrollTop = messages.scrollHeight;
          }
        }
      } catch (err) {
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


@app.post("/claude/ask")
async def ask(request: Request):
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return {"error": "empty prompt"}

    async def stream():
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            "--dangerously-skip-permissions",
            "--max-turns", "20",
            "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "HOME": "/home/node"},
        )
        stdout, _ = await proc.communicate()
        try:
            data = json.loads(stdout.decode("utf-8", errors="replace"))
            text = data.get("result", "")
        except Exception:
            text = stdout.decode("utf-8", errors="replace")
        yield f"data: {json.dumps({'text': text})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
