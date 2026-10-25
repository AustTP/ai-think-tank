#!/usr/bin/env python3
"""Public, read-only fishtank view of the think tank floor.

This is deliberately a separate HTTP service from serve.py:

  * no login, no session cookie, no auth surface
  * only a sanitized position snapshot (opaque tokens, x/y, direction)
  * no agent names, tasks, chats, reports, or any writable endpoint
  * serves only the background, the four default character sprites, and the
    per-agent avatar images addressed by opaque token

Run it on loopback and open http://localhost:8938/ on the display device.
Using localhost (not the main server's host/IP) also keeps the main server's
session cookie out of this origin, so a person with devtools cannot copy it
from the fishtank tab.

Devtools cannot be disabled on a device the viewer controls. The guarantee
here is data minimization: devtools can only reveal the same positions and
public art that are already on screen.
"""
import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

WORLD_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = WORLD_DIR.parent
DB_PATH = ROOT_DIR / 'think_tank.db'
ASSETS_DIR = WORLD_DIR / 'assets'
AGENTS_DIR = ROOT_DIR / 'agents'
ORIENTATIONS = ('south', 'north', 'east', 'west')
ASSET_WHITELIST = {
    'think_tank_background.png',
    'character_south.png',
    'character_north.png',
    'character_east.png',
    'character_west.png',
}

_TOKEN_SECRET = secrets.token_bytes(32)
_TOKENS = {}
_TOKENS_LOCK = threading.Lock()


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<meta name="robots" content="noindex,nofollow">
<title>Think Tank Floor</title>
<style>
  html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#05070d}
  body{position:fixed;inset:0;cursor:none;user-select:none;-webkit-user-select:none;touch-action:none}
  #view{position:absolute;inset:0;width:100%;height:100%;display:block;image-rendering:pixelated}
  #water,#vignette{position:absolute;inset:0;pointer-events:none}
  #water{background:linear-gradient(180deg,rgba(92,198,224,.13),transparent 34%),linear-gradient(0deg,rgba(10,28,44,.30),transparent 55%);mix-blend-mode:screen;opacity:.5}
  #vignette{background:radial-gradient(ellipse 100% 85% at 50% 45%,transparent 58%,rgba(2,6,12,.55) 100%)}
</style>
</head>
<body>
<canvas id="view" aria-label="Live view of the think tank floor"></canvas>
<div id="water"></div>
<div id="vignette"></div>
<script>
(() => {
  'use strict';
  const WORLD_W = 1376, WORLD_H = 768;
  const canvas = document.getElementById('view');
  const ctx = canvas.getContext('2d', { alpha: false });
  const view = { w: 0, h: 0, scale: 1, ox: 0, oy: 0 };

  const bg = new Image();
  let bgReady = false;
  bg.onload = () => { bgReady = true; };
  bg.src = '/assets/think_tank_background.png';

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, window.innerWidth), h = Math.max(1, window.innerHeight);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    view.w = canvas.width;
    view.h = canvas.height;
    view.scale = Math.max(canvas.width / WORLD_W, canvas.height / WORLD_H);
    view.ox = (canvas.width - WORLD_W * view.scale) / 2;
    view.oy = (canvas.height - WORLD_H * view.scale) / 2;
  }
  window.addEventListener('resize', resize, { passive: true });
  resize();

  const agents = new Map();
  const spriteCache = new Map();
  const defaultSprites = {};
  for (const d of ['south', 'north', 'east', 'west']) {
    const img = new Image();
    img.src = '/assets/character_' + d + '.png';
    defaultSprites[d] = img;
  }

  function spriteFor(token, dir) {
    const key = token + ':' + dir;
    let entry = spriteCache.get(key);
    if (entry) return entry;
    const img = new Image();
    entry = { img, ok: false, failed: false };
    img.onload = () => { entry.ok = true; };
    img.onerror = () => { entry.failed = true; };
    img.src = '/avatar/' + encodeURIComponent(token) + '/' + dir + '.png';
    spriteCache.set(key, entry);
    return entry;
  }

  function drawAgent(a) {
    const entry = a.a ? spriteFor(a.token, a.dir) : null;
    const img = (!entry || entry.failed) ? defaultSprites[a.dir]
      : (entry.ok ? entry.img : defaultSprites[a.dir]);
    if (!img || !img.complete || !img.naturalWidth) return;
    const sw = img.naturalWidth, sh = img.naturalHeight;
    const x = view.ox + (a.x - (sw - 20) / 2) * view.scale;
    const y = view.oy + (a.y - (sh - 16)) * view.scale;
    ctx.globalAlpha = a.alpha;
    ctx.drawImage(img, x, y, sw * view.scale, sh * view.scale);
    ctx.globalAlpha = 1;
  }

  let last = performance.now();
  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    ctx.fillStyle = '#05070d';
    ctx.fillRect(0, 0, view.w, view.h);
    if (bgReady) {
      ctx.drawImage(bg, view.ox, view.oy, WORLD_W * view.scale, WORLD_H * view.scale);
    }
    const ease = 1 - Math.exp(-dt * 9);
    const order = [];
    for (const a of agents.values()) {
      a.x += (a.tx - a.x) * ease;
      a.y += (a.ty - a.y) * ease;
      if (a.alpha < 1) a.alpha = Math.min(1, a.alpha + dt * 4);
      order.push(a);
    }
    order.sort((p, q) => p.y - q.y);
    for (const a of order) drawAgent(a);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  let backoff = 1000;
  async function poll() {
    try {
      const res = await fetch('/api/fishtank/state', { cache: 'no-store' });
      if (!res.ok) throw new Error('state ' + res.status);
      const data = await res.json();
      const seen = new Set();
      for (const p of data.agents || []) {
        const token = String(p.t || '');
        if (!token) continue;
        seen.add(token);
        let a = agents.get(token);
        if (!a) {
          a = { token, x: Number(p.x) || 0, y: Number(p.y) || 0,
                tx: Number(p.x) || 0, ty: Number(p.y) || 0,
                dir: p.d || 'south', alpha: 0, a: p.a ? 1 : 0 };
          agents.set(token, a);
        } else {
          a.tx = Number(p.x) || 0;
          a.ty = Number(p.y) || 0;
          a.dir = p.d || a.dir;
          a.a = p.a ? 1 : 0;
        }
      }
      for (const token of agents.keys()) {
        if (!seen.has(token)) agents.delete(token);
      }
      backoff = 1000;
    } catch (e) {
      backoff = Math.min(5000, backoff + 500);
    }
    setTimeout(poll, backoff);
  }
  poll();

  // Cosmetic only. Devtools cannot be disabled on a device the viewer owns;
  // the real boundary is the read-only, sanitized service and separate origin.
  document.addEventListener('contextmenu', e => e.preventDefault());
  document.addEventListener('dragstart', e => e.preventDefault());
  document.addEventListener('keydown', e => {
    const k = e.key.toLowerCase();
    const block = e.key === 'F12'
      || (e.ctrlKey && e.shiftKey && ['i', 'j', 'c'].includes(k))
      || (e.metaKey && e.altKey && ['i', 'j', 'c'].includes(k))
      || (e.ctrlKey && k === 'u')
      || (e.metaKey && e.altKey && k === 'u');
    if (block) e.preventDefault();
  }, { capture: true });
})();
</script>
</body>
</html>
'''


def _token(agent_id):
    return hmac.new(_TOKEN_SECRET, agent_id.encode('utf-8'), hashlib.sha256).hexdigest()[:20]


def _load_snapshot():
    """Read the current positions and return only what the renderer needs."""
    con = sqlite3.connect('file:%s?mode=ro' % DB_PATH, uri=True, timeout=2.0)
    try:
        row = con.execute('SELECT blob FROM kv_state WHERE id = 1').fetchone()
    finally:
        con.close()
    if not row:
        return {'tick': 0, 'agents': []}
    state = json.loads(row[0])
    sim = state.get('sim') or {}
    snapshot = sim.get('agents') or {}
    roster = state.get('agents') or {}
    tokens = {}
    agents = []
    for agent_id, pos in snapshot.items():
        if not isinstance(pos, dict):
            continue
        if not pos.get('visible') or pos.get('inRoom'):
            continue
        info = roster.get(agent_id) or {}
        if (info.get('villageId') or 'main') != 'main':
            continue
        try:
            x = float(pos.get('x'))
            y = float(pos.get('y'))
        except (TypeError, ValueError):
            continue
        direction = pos.get('dir')
        if direction not in ORIENTATIONS:
            direction = 'south'
        token = _token(str(agent_id))
        tokens[token] = str(agent_id)
        avatar_dir = AGENTS_DIR / str(agent_id) / 'avatars'
        has_avatar = avatar_dir.is_dir() and any(
            (avatar_dir / (orientation + '.png')).is_file()
            for orientation in ORIENTATIONS)
        agents.append({'t': token, 'x': round(x, 2), 'y': round(y, 2),
                       'd': direction, 'a': 1 if has_avatar else 0})
    agents.sort(key=lambda a: (a['y'], a['x']))
    with _TOKENS_LOCK:
        _TOKENS.clear()
        _TOKENS.update(tokens)
    return {'tick': sim.get('tick', 0), 'agents': agents}


class Handler(BaseHTTPRequestHandler):
    server_version = 'Fishtank/1.0'

    def log_message(self, fmt, *args):
        # No access log: this display runs unattended and the paths are public.
        return

    def _send(self, status, content_type, body, cache='no-store', extra=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _file(self, path, content_type=None, cache='public, max-age=3600'):
        try:
            body = path.read_bytes()
        except OSError:
            self._send(404, 'text/plain; charset=utf-8', b'not found')
            return
        if content_type is None:
            content_type = mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
        self._send(200, content_type, body, cache=cache)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/fishtank', '/fishtank.html'):
            self._send(
                200, 'text/html; charset=utf-8', PAGE.encode('utf-8'),
                extra={'Content-Security-Policy': (
                    "default-src 'none'; img-src 'self' data:; connect-src 'self'; "
                    "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")})
            return
        if path == '/api/fishtank/state':
            try:
                payload = _load_snapshot()
            except Exception:
                self._send(503, 'application/json; charset=utf-8',
                           b'{"ok":false,"agents":[]}')
                return
            body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
            self._send(200, 'application/json; charset=utf-8', body)
            return
        if path.startswith('/assets/'):
            name = unquote(path[len('/assets/'):])
            if name not in ASSET_WHITELIST:
                self._send(404, 'text/plain; charset=utf-8', b'not found')
                return
            self._file(ASSETS_DIR / name)
            return
        if path.startswith('/avatar/'):
            parts = path.split('/')
            if len(parts) == 4:
                token = unquote(parts[2])
                filename = unquote(parts[3])
                if filename.endswith('.png'):
                    orientation = filename[:-4]
                    with _TOKENS_LOCK:
                        agent_id = _TOKENS.get(token)
                    if agent_id and orientation in ORIENTATIONS:
                        self._file(AGENTS_DIR / agent_id / 'avatars' / (orientation + '.png'))
                        return
            self._send(404, 'text/plain; charset=utf-8', b'not found')
            return
        if path == '/favicon.ico':
            self._send(204, 'image/x-icon', b'')
            return
        self._send(404, 'text/plain; charset=utf-8', b'not found')


def main():
    parser = argparse.ArgumentParser(description='Public read-only think tank fishtank view.')
    parser.add_argument('--host', default='127.0.0.1',
                        help='bind address (default 127.0.0.1; use localhost in the browser)')
    parser.add_argument('--port', default=8938, type=int)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print('Fishtank view on http://%s:%s/ (read-only, no auth, no session)' %
          (args.host, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':  # pragma: no cover
    main()
