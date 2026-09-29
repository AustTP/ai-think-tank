# Laya Standby on Google Colab (the sentinel notebook)

Paste each fenced block into its own notebook cell in
[colab.research.google.com](colab.research.google.com) and run them in order.
**Runtime > Change runtime type > Hardware accelerator > GPU (T4)** — free tier
is fine; the model real ~808MB on a GPU.

When Jev (the remote decisions model) degrades, the village's 15-minute
standby loop tells this notebook to stand Laya up; when Jev is healthy again
for two cycles it tells the notebook to stand down. This notebook never asks
Google's APIs for anything — the runtime is your own session, and only the
tunnel is used.

---

## Cell 1 — install

```python
!pip install -q "laya[serve]" cloudflared
```

## Cell 2 — start Laya (detached, preloaded on the GPU)

```python
import os, subprocess, secrets

KEY = "VJ-Laya-" + secrets.token_hex(12)
with open("/content/laya_key.txt", "w") as f:
    f.write(KEY)

env = {
    **os.environ,
    "LAYA_DEVICE": "cuda",
    "LAYA_PRELOAD": "1",
    "LAYEA_API_KEY": KEY,
    "LAYA_API_KEY": KEY,
}
log = open("/content/laya.log", "wb")
subprocess.Popen(["laya-serve"], env=env, stdout=log, stderr=subprocess.STDOUT)
print("laya-serve starting on :8000")
```

## Cell 3 — write the proxy/control server

The proxy owns the public port (:8080): it fronts Laya's Jev-compatible
`/v1/systemone`, pings Laya's `/health`, and honors `/control` stand
up/tear-down commands from the village. It runs detached so later cells don't
block.

```python
%%writefile /content/standby_proxy.py
import json, os, subprocess, urllib.request
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

KEY = open("/content/laya_key.txt").read().strip()
app = FastAPI()

def _laya_ready():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False

@app.post("/v1/systemone")
async def systemone(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "malformed body"}, status_code=400)
    req = urllib.request.Request(
        "http://127.0.0.1:8000/v1/systemone",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return JSONResponse(json.loads(resp.read().decode()))
    except Exception as e:
        return JSONResponse({"detail": f"laya unavailable: {e}"}, status_code=503)

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "laya": _laya_ready(), "serving": True})

@app.get("/control")
async def control(action: str = "", key: str = ""):
    if key != KEY:
        return JSONResponse({"error": "bad key"}, status_code=401)
    if action == "standup":
        if _laya_ready():
            return {"ok": True, "standing": True, "laya": "already up"}
        return {"ok": False, "laya": "preloading (check /health shortly)"}
    if action == "teardown":
        subprocess.run(["pkill", "-f", "laya-serve"], capture_output=True)
        return {"ok": True, "standing": False}
    return JSONResponse({"error": "unknown action"}, status_code=400)
```

## Cell 4 — start the proxy on :8080 (detached)

```python
import subprocess
log = open("/content/proxy.log", "wb")
subprocess.Popen(
    ["nohup", "python3", "/content/standby_proxy.py"],
    stdout=log, stderr=subprocess.STDOUT,
)
print("proxy starting on :8080")
```

## Cell 5 — open the public tunnel

```python
import subprocess, time, re

tunnel = subprocess.Popen(
    ["cloudflared", "tunnel", "--url", "http://127.0.0.1:8080", "--no-autoupdate", "--no-autoupgrade"],
    stdout=open("/content/tunnel.log", "wb"), stderr=subprocess.STDOUT,
)
url = None
for _ in range(120):
    time.sleep(1)
    log = open("/content/tunnel.log").read()
    m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", log)
    if m:
        url = m.group(0)
        break
with open("/content/tunnel_url.txt", "w") as f:
    f.write(url or "")
print("TUNNEL URL:", url)
print("LAYA KEY:", open("/content/laya_key.txt").read().strip())
```

## Cell 6 — generate the pairing command (run on the Mac, not here)

Colab runs on Google's cloud, so it can't reach the village on `127.0.0.1`
(the Mac). Paste the Device Key (from `world/.env` →
`DEVICE_API_KEY=...`) plus Cells 5's URL and key into this cell, run it, and
it prints the one command to paste into the Mac's terminal. That command
registers this session with `POST /api/colab/register`, and the village then
owns everything: stand up/tear down via `/control` and failover routing.

```python
DEVICE_KEY = "PASTE_DEVICE_API_KEY"  # from .env on the Mac
TUNNEL_URL = "https://PASTE-TRUNNEL-URL.trycloudflare.com"
LAYA_KEY = "VJ-Laya-PASTE-KEY"

print(f'curl -s -X POST http://127.0.0.1:8936/api/colab/register '
      f'-H "X-Device-Key: {DEVICE_KEY}" -H "Content-Type: application/json" '
      f'-d \'{{"url":"{TUNNEL_URL}","key":"{LAYA_KEY}"}}\'')
```

---

## Keeping it alive (free tier reality)

- Free Colab kills the runtime after ~90 min of idle or ~12 h of continuous
  use. When it resets, re-run Cells 2-6 (Cell 5-6 give you the new URL/key).
- Colab Pro/Pro+ background execution keeps a session alive up to 24 h without
  you watching.
- Each new session gets a fresh tunnel URL and a fresh Laya key — that is why
  re-pairing (Cell 6's curl, run on the Mac) happens per session.
- The village side needs nothing from Google: no OAuth, no Colab API, no
  cookies. If the sentinel is not running, the village just stays on
  deterministic fallbacks and you keep the normal health alert.