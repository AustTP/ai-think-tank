"""Fast path: dedicated always-on lanes outside the tank's sim loop.

Design (2026-10-28, after the tape-race and runner-kill fixes):

- SEPARATE PROCESS. The main tank's ceiling is one contended interpreter:
  sim tick, watches, API and model calls all share it, and a stalled tick
  costs minutes (the watchdog recycles at 180s). Latency-sensitive work gets
  its own process with no sim, no watches, no shared GIL contention.

- LANES ARE CONFIGURED, NOT CODED. `fast_lanes.json` declares any number of
  lane instances; each has a `type` from a small, safety-audited registry:

      http_watch    GET-only observation of declared targets, rate-limited,
                    optional LLM analysis. Use it for bug-bounty recon
                    (declare `program` + in-scope `targets`), surface
                    monitoring, or any watch-shaped job.
      paper_markets Simulated fills against public spot prices. There is NO
                    order-placement code path to any exchange anywhere in
                    this file. Caps and the kill switch bound it.

    JSON tunes and adds lane instances freely; a NEW kind of behavior is a
    code change and gets reviewed like one. That boundary is deliberate:
    config changes behavior, code changes capability.

- OWN AUDIT CHAIN, SINGLE WRITER. The 39 decision-tape breaks were two
  writers interleaving on one hash chain. Here every lane event goes through
  a queue to ONE flusher thread that is the only writer of fast_lane.db, so
  the chain race is impossible by construction. The chain is HMAC-SHA256,
  keyed by .secret_keys/fast_lane.key (independent of the tank's boundary
  secret). A later tank-side watch can anchor lane head hashes into the main
  decision_tape as checkpoints without any cross-process write contention.

- BLAST RADIUS IS STRUCTURAL, NOT HOPED FOR.
    * http_watch never issues anything but GET, never touches a host outside
      its declared targets, and idles loudly when unconfigured.
    * paper_markets caps trades/day and notional in code; the kill-switch
      file (data/fast-kill) pauses every lane.
    * The service binds 127.0.0.1 by default; it is reachable only on this
      machine unless the operator explicitly rebinds it.

- FAIL FAST, DEGRADE HONESTLY. One pinned model with hard deadlines
  (connect 2s, read 8s). A slow or missing model never stalls a lane: the
  lane logs a degraded event and keeps running on its rule layer. Provider
  roulette is a background-lane luxury; a fast lane fails visibly instead.

Run:  python3 fast_path.py          (env: FAST_PORT, FAST_HOST, FAST_MODEL)
Stop: launchctl kickstart -k gui/501/com.austtp.tank-fast  (or SIGTERM)
"""

import asyncio
import hashlib
import hmac
import json
import os
import queue
import subprocess
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / 'fast_lanes.json'
LANE_DB_PATH = REPO_ROOT / 'fast_lane.db'
KILL_SWITCH_PATH = REPO_ROOT / 'data' / 'fast-kill'
LANE_KEY_PATH = REPO_ROOT / '.secret_keys' / 'fast_lane.key'

FAST_PORT = int(os.environ.get('FAST_PORT', '8938'))
FAST_HOST = os.environ.get('FAST_HOST', '127.0.0.1')
FAST_MODEL = os.environ.get('FAST_MODEL', 'meta-llama/llama-3.3-70b-instruct:free')
MAX_TRADES_PER_DAY = int(os.environ.get('FAST_MAX_TRADES_PER_DAY', '10'))
MAX_NOTIONAL_PER_TRADE = float(os.environ.get('FAST_MAX_NOTIONAL', '500'))
MAX_TRADES_PER_DAY = max(1, MAX_TRADES_PER_DAY)
MAX_NOTIONAL_PER_TRADE = max(1.0, MAX_NOTIONAL_PER_TRADE)
HTTP_TIMEOUT = httpx.Timeout(8.0, connect=2.0)
CYCLE_TIMEOUT_FRACTION = 0.9


def _load_env_file(path):
    env = {}
    p = Path(path)
    if p.is_file():
        for line in p.read_text(errors='replace').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV_FILE = _load_env_file(REPO_ROOT / '.env')
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY') or ENV_FILE.get('OPENROUTER_API_KEY', '')


def _lane_key():
    """Dedicated HMAC key for the lane chain, separate from the tank's
    boundary secret: the fast path must not need (or leak) the main
    server's trust root, and the two chains stay independently verifiable."""
    key_path = Path(LANE_KEY_PATH)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.exists():
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(os.urandom(32).hex())
    return key_path.read_text().strip()


def _kill_switch_active():
    return Path(KILL_SWITCH_PATH).exists()


class LaneLog:
    """Append-only, hash-chained event log. ALL writes go through one
    flusher thread; callers only enqueue. verify() walks the chain."""

    def __init__(self, db_path=LANE_DB_PATH):
        self.db_path = Path(db_path)
        self.key = _lane_key()
        self._q = queue.Queue()
        # Dedicated connection for the flusher thread ONLY (writes).
        # check_same_thread=False is safe here because the single-writer
        # discipline is enforced by construction: nothing but _flush_loop
        # ever issues a statement on _conn. Every read path (head, verify,
        # recent) opens its own short-lived connection instead.
        self._conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.execute('''CREATE TABLE IF NOT EXISTS lane_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, lane TEXT,
            agent_id TEXT, kind TEXT, payload TEXT,
            prev_hash TEXT, hash TEXT)''')
        self._conn.commit()
        self._writer = threading.Thread(target=self._flush_loop, name='lane-writer', daemon=True)
        self._writer.start()

    def _reader(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    def _row_hash(self, prev_hash, ts, lane, agent_id, kind, payload):
        blob = json.dumps([ts, lane, agent_id, kind, payload], sort_keys=True, default=str)
        return hmac.new(self.key.encode(), f'{prev_hash or ""}|{blob}'.encode('utf-8', errors='replace'),
                        hashlib.sha256).hexdigest()

    def emit(self, lane, agent_id, kind, payload):
        self._q.put({'ts': time.time(), 'lane': lane, 'agent_id': agent_id,
                     'kind': kind, 'payload': json.dumps(payload, default=str)})

    def _flush_loop(self):
        while True:
            batch = [self._q.get()]
            while True:
                try:
                    batch.append(self._q.get(timeout=0.5))
                except queue.Empty:
                    break
            try:
                cur = self._conn.execute(
                    'SELECT hash FROM lane_events ORDER BY id DESC LIMIT 1').fetchone()
                prev_hash = cur[0] if cur else ''
                for ev in batch:
                    h = self._row_hash(prev_hash, ev['ts'], ev['lane'],
                                       ev['agent_id'], ev['kind'], ev['payload'])
                    self._conn.execute(
                        'INSERT INTO lane_events (ts, lane, agent_id, kind, payload, prev_hash, hash) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (ev['ts'], ev['lane'], ev['agent_id'], ev['kind'], ev['payload'], prev_hash, h))
                    prev_hash = h
                self._conn.commit()
            except Exception as e:
                print(f'[lane-log] flush error: {e}', flush=True)

    def flush_sync(self, timeout=5):
        """Test/ops sync point: emit a marker event and return once it is
        VISIBLE in the DB. Because the single writer commits each batch in
        FIFO order, the marker's commit proves every earlier event landed.
        Returns False on timeout (the writer is stuck or erroring)."""
        marker = f'sync-{time.time()}-{os.getpid()}'
        self.emit('__sync__', 'fast-path', 'sync', {'marker': marker})
        deadline = time.time() + timeout
        while time.time() < deadline:
            conn = sqlite3.connect(self.db_path, timeout=10)
            try:
                row = conn.execute(
                    'SELECT 1 FROM lane_events WHERE payload LIKE ? LIMIT 1',
                    (f'%{marker}%',)).fetchone()
            finally:
                conn.close()
            if row:
                return True
            time.sleep(0.02)
        return False

    def head(self):
        with self._reader() as conn:
            cur = conn.execute(
                'SELECT id, hash FROM lane_events ORDER BY id DESC LIMIT 1').fetchone()
        return {'id': cur[0], 'hash': cur[1]} if cur else {'id': 0, 'hash': ''}

    def verify(self):
        with self._reader() as conn:
            rows = conn.execute(
                'SELECT id, prev_hash, hash, ts, lane, agent_id, kind, payload '
                'FROM lane_events ORDER BY id ASC').fetchall()
        problems, prev_hash = [], ''
        for i, (rid, row_prev, row_hash, ts, lane, agent_id, kind, payload) in enumerate(rows):
            if i > 0 and row_prev != prev_hash:
                problems.append({'id': rid, 'issue': 'chain break'})
            expected = self._row_hash(row_prev, ts, lane, agent_id, kind, payload)
            if expected != row_hash:
                problems.append({'id': rid, 'issue': 'hash mismatch (content tampered)'})
            prev_hash = row_hash
        return problems

    def recent(self, lane=None, limit=50):
        with self._reader() as conn:
            if lane:
                rows = conn.execute(
                    'SELECT ts, lane, agent_id, kind, payload FROM lane_events '
                    'WHERE lane = ? ORDER BY id DESC LIMIT ?', (lane, limit)).fetchall()
            else:
                rows = conn.execute(
                    'SELECT ts, lane, agent_id, kind, payload FROM lane_events '
                    'ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        return [{'ts': r[0], 'lane': r[1], 'agent_id': r[2], 'kind': r[3],
                 'payload': json.loads(r[4]) if r[4] else {}} for r in rows]


class FastModel:
    """Pinned-provider client with hard deadlines. No failover roulette:
    a slow model logs degraded and the lane's rule layer carries on."""

    def __init__(self, log):
        self.log = log
        self.client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

    async def ask(self, lane, agent_id, system, user, max_tokens=400):
        if not OPENROUTER_API_KEY:
            return None
        try:
            resp = await self.client.post(
                'https://openrouter.ai/api/v1/chat/completions',
                headers={'Authorization': f'Bearer {OPENROUTER_API_KEY}'},
                json={'model': FAST_MODEL,
                      'messages': [{'role': 'system', 'content': system},
                                   {'role': 'user', 'content': user}],
                      'max_tokens': max_tokens})
            resp.raise_for_status()
            data = resp.json()
            text = data['choices'][0]['message']['content']
            cost = (data.get('usage') or {}).get('cost') or 0
            self.log.emit(lane, agent_id, 'model_call',
                          {'model': FAST_MODEL, 'cost': cost, 'chars': len(text)})
            return text
        except Exception as e:
            self.log.emit(lane, agent_id, 'model_degraded', {'error': str(e)[:200]})
            return None


class VpsExecutor:
    """Dispatch declared work to a dedicated VPS over SSH.

    The bounty lane's work always runs here, never in a local Docker
    container and never from the operator's home IP: the VPS is the
    attributable, disposable workhorse. Discipline:

    - Only commands declared in the lane's config `work` list ever run.
      LLM output is analyzed, never executed; there is no code path from
      model text to a shell command.
    - Host key pinning is mandatory (StrictHostKeyChecking=yes against an
      operator-provided known_hosts file); an unpinned host fails closed.
    - BatchMode (key auth only), bounded connect/command timeouts, and a
      bounded output capture keep a hung VPS from stalling the lane.
    """

    def __init__(self, cfg, log, lane_name):
        self.log = log
        self.lane_name = lane_name
        self.host = cfg.get('host') or ''
        self.user = cfg.get('user') or ''
        self.port = str(cfg.get('port', 22))
        self.key_path = cfg.get('key_path') or ''
        self.known_hosts = cfg.get('known_hosts') or ''
        self.command_timeout_s = float(cfg.get('command_timeout_s', 120))

    @property
    def configured(self):
        return bool(self.host and self.user and self.key_path and self.known_hosts
                    and Path(self.key_path).is_file()
                    and Path(self.known_hosts).is_file())

    def missing(self):
        need = []
        if not (self.host and self.user):
            need.append('host/user')
        if not self.key_path or not Path(self.key_path).is_file():
            need.append('key_path (must exist on this machine)')
        if not self.known_hosts or not Path(self.known_hosts).is_file():
            need.append('known_hosts (pin it: ssh-keyscan -H <host> >> <file>)')
        return need

    def argv(self, command):
        return ['ssh', '-i', self.key_path, '-p', self.port,
                '-o', 'BatchMode=yes',
                '-o', 'ConnectTimeout=8',
                '-o', 'StrictHostKeyChecking=yes',
                '-o', 'UserKnownHostsFile=' + self.known_hosts,
                f'{self.user}@{self.host}', command]

    def run(self, command, timeout_s=None):
        started = time.monotonic()
        try:
            proc = subprocess.run(
                self.argv(command), capture_output=True, text=True,
                timeout=timeout_s or self.command_timeout_s)
            return {'exit_code': proc.returncode,
                    'stdout': proc.stdout[:20000],
                    'stderr': proc.stderr[:8000],
                    'duration_s': round(time.monotonic() - started, 2)}
        except subprocess.TimeoutExpired:
            return {'exit_code': None, 'stdout': '', 'stderr': '',
                    'duration_s': round(time.monotonic() - started, 2),
                    'error': f'timeout after {timeout_s or self.command_timeout_s}s'}
        except Exception as e:
            return {'exit_code': None, 'stdout': '', 'stderr': '',
                    'duration_s': round(time.monotonic() - started, 2),
                    'error': str(e)[:200]}


class BaseLane:
    """Common lane plumbing: the engine owns cadence; a lane only cycles."""

    def __init__(self, cfg, log, model):
        self.cfg = cfg or {}
        self.log = log
        self.model = model
        self.name = self.cfg.get('name') or self.__class__.__name__
        self.cadence_s = max(1.0, float(self.cfg.get('cadence_s', 60)))

    def _paused_event(self):
        self.log.emit(self.name, 'fast-path', 'paused', {'reason': 'kill switch'})


class HttpWatchLane(BaseLane):
    """GET-only observation of declared targets. Recon semantics when a
    `program` is declared; plain surface-watch otherwise. It never touches
    a host outside `targets` and it never issues anything but GET."""

    def __init__(self, cfg, log, model):
        super().__init__(cfg, log, model)
        self.program = self.cfg.get('program') or ''
        self.targets = [t for t in (self.cfg.get('targets') or [])
                        if isinstance(t, str) and t.strip()]
        self.rate_limit_s = max(0.0, float(self.cfg.get('rate_limit_s', 2.0)))
        self.analysis_prompt = self.cfg.get('analysis_prompt') or (
            'You are a recon analyst. Work only within the declared scope. '
            'Be concise: list anything unusual worth a human look, or say '
            '"nothing notable".')
        self._last = {}
        self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False)
        self.work = self.cfg.get('work') or []
        self.work_timeout_s = float(self.cfg.get('work_timeout_s', 120))
        self.executor = None
        exec_cfg = self.cfg.get('executor') or {}
        if exec_cfg.get('kind') == 'vps':
            self.executor = VpsExecutor(exec_cfg, log, self.name)

    @property
    def configured(self):
        return bool(self.targets)

    async def _vps_cycle(self):
        """Work happens on the VPS; this machine only dispatches, observes,
        and keeps the receipts. Every dispatch is a declared work item; the
        lane never runs anything the config did not name."""
        if not self.executor.configured:
            self.log.emit(self.name, 'fast-path', 'executor_unconfigured',
                          {'missing': self.executor.missing()})
            return
        if not self.work:
            self.log.emit(self.name, 'fast-path', 'unconfigured',
                          {'note': 'declare the work list (declared commands only); '
                                   'the VPS runs nothing the config did not name'})
            return
        results = []
        for item in self.work:
            cmd_name = (item or {}).get('name') or ''
            command = (item or {}).get('command') or ''
            if not cmd_name or not command:
                continue
            self.log.emit(self.name, 'lane-dispatcher', 'dispatch',
                          {'work': cmd_name, 'host': self.executor.host})
            result = await asyncio.to_thread(self.executor.run, command,
                                             self.work_timeout_s)
            self.log.emit(self.name, 'lane-dispatcher', 'result',
                          {'work': cmd_name, 'exit_code': result['exit_code'],
                           'duration_s': result['duration_s'],
                           'stdout_tail': result['stdout'][-3000:],
                           'stderr_tail': result['stderr'][-1500:],
                           **({'error': result['error']} if result.get('error') else {})})
            results.append({'work': cmd_name, **result})
        if results:
            summary = await self.model.ask(
                self.name, 'lane-analyst',
                self.analysis_prompt + ' Never propose executing anything; '
                'you are read-only analysis over returned output.',
                json.dumps({'program': self.program,
                            'results': [{'work': r['work'],
                                         'exit': r['exit_code'],
                                         'stdout_tail': r['stdout'][-2500:]}
                                        for r in results]}))
            if summary:
                self.log.emit(self.name, 'lane-analyst', 'analysis',
                              {'summary': summary[:2000]})

    async def cycle(self):
        if _kill_switch_active():
            self._paused_event()
            return
        if not self.configured:
            self.log.emit(self.name, 'fast-path', 'unconfigured',
                          {'note': 'declare targets in fast_lanes.json; '
                                   'no requests run without an explicit scope'})
            return
        if self.executor is not None:
            return await self._vps_cycle()
        findings = []
        for target in self.targets:
            url = target if target.startswith('http') else f'https://{target}'
            try:
                if self.rate_limit_s:
                    await asyncio.sleep(self.rate_limit_s)
                r = await self._client.get(url)
                hdrs = {k.lower(): v for k, v in r.headers.items()}
                obs = {'url': url, 'status': r.status_code,
                       'server': hdrs.get('server'),
                       'csp': bool(hdrs.get('content-security-policy')),
                       'hsts': bool(hdrs.get('strict-transport-security'))}
                prev = self._last.get(url)
                self._last[url] = obs
                if prev and prev != obs:
                    findings.append({'type': 'surface_changed', **obs, 'prev': prev})
                elif not prev:
                    findings.append({'type': 'surface_recorded', **obs})
                if r.status_code >= 400:
                    findings.append({'type': 'error_surface', **obs})
            except Exception as e:
                findings.append({'type': 'fetch_failed', 'url': url, 'error': str(e)[:120]})
        self.log.emit(self.name, 'lane-observer', 'cycle',
                      {'program': self.program, 'findings': findings,
                       'targets': len(self.targets)})
        summary = await self.model.ask(
            self.name, 'lane-analyst',
            self.analysis_prompt + ' Never act outside the declared scope.',
            json.dumps({'program': self.program, 'observations': findings}))
        if summary:
            self.log.emit(self.name, 'lane-analyst', 'analysis', {'summary': summary[:2000]})


class PaperMarketsLane(BaseLane):
    """Simulated fills against public spot prices. There is NO order-placement
    code path in this file. Caps are structural; the kill switch pauses."""

    PRICE_SOURCES = {'coinbase_spot': 'https://api.coinbase.com/v2/prices/{symbol}/spot'}

    def __init__(self, cfg, log, model):
        super().__init__(cfg, log, model)
        self.price_source = self.cfg.get('price_source', 'coinbase_spot')
        self.symbols = [s for s in (self.cfg.get('symbols') or []) if isinstance(s, str) and s]
        self.notional = min(float(self.cfg.get('notional', 100)), MAX_NOTIONAL_PER_TRADE)
        self.band_pct = float(self.cfg.get('band_pct', 2.0))
        self.analysis_prompt = self.cfg.get('analysis_prompt') or (
            'You are a trading analyst on a PAPER trading lane. No real money. '
            'One short paragraph on price action and positions. Never give '
            'financial advice; this is system telemetry commentary.')
        self.window = int(self.cfg.get('window', 30))
        self._prices = {s: deque(maxlen=self.window) for s in self.symbols}
        self._portfolio = {s: {'units': 0.0, 'cash': 1000.0} for s in self.symbols}
        self._trades_today = {}
        self._cycles = 0
        self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

    @property
    def configured(self):
        return bool(self.symbols) and self.price_source in self.PRICE_SOURCES

    async def _spot(self, symbol):
        r = await self._client.get(self.PRICE_SOURCES[self.price_source].format(symbol=symbol))
        r.raise_for_status()
        return float(r.json()['data']['amount'])

    def _today(self):
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')

    def _decide(self, symbol, price):
        win = self._prices[symbol]
        win.append(price)
        if len(win) < 10:
            return 'hold', 'warming up'
        sma = sum(win) / len(win)
        drift = (price - sma) / sma * 100
        if drift <= -self.band_pct:
            return 'buy', f'{drift:.2f}% below {len(win)}-tick SMA'
        if drift >= self.band_pct:
            return 'sell', f'{drift:.2f}% above {len(win)}-tick SMA'
        return 'hold', 'inside band'

    def _paper_fill(self, symbol, action, price):
        day = self._today()
        if self._trades_today.get(day, 0) >= MAX_TRADES_PER_DAY:
            return None, 'daily trade cap reached'
        pos = self._portfolio[symbol]
        units = self.notional / price
        if action == 'buy':
            if pos['cash'] < self.notional:
                return None, 'insufficient paper cash'
            pos['cash'] -= self.notional
            pos['units'] += units
        else:
            if pos['units'] < units:
                units = pos['units']
                if units <= 0:
                    return None, 'no position to sell'
            pos['units'] -= units
            pos['cash'] += units * price
        self._trades_today[day] = self._trades_today.get(day, 0) + 1
        return {'symbol': symbol, 'action': action, 'price': price,
                'units': round(units, 8), 'notional': round(units * price, 2),
                'portfolio': {k: round(v, 4) for k, v in pos.items()}}, None

    async def cycle(self):
        self._cycles += 1
        if _kill_switch_active():
            self._paused_event()
            return
        if not self.configured:
            self.log.emit(self.name, 'fast-path', 'unconfigured',
                          {'note': 'declare symbols and a supported price_source '
                                   'in fast_lanes.json'})
            return
        ticks = {}
        for symbol in self.symbols:
            try:
                ticks[symbol] = await self._spot(symbol)
            except Exception as e:
                self.log.emit(self.name, 'lane-markets', 'price_degraded',
                              {'symbol': symbol, 'error': str(e)[:120]})
        actions = []
        for symbol, price in ticks.items():
            action, why = self._decide(symbol, price)
            if action in ('buy', 'sell'):
                fill, blocked = self._paper_fill(symbol, action, price)
                if fill:
                    actions.append(fill)
                    self.log.emit(self.name, 'lane-markets', 'paper_fill', fill)
                elif blocked:
                    self.log.emit(self.name, 'lane-markets', 'fill_blocked',
                                  {'symbol': symbol, 'reason': blocked})
            else:
                self.log.emit(self.name, 'lane-markets', 'tick',
                              {'symbol': symbol, 'price': price, 'note': why})
        if self._cycles % 5 == 0 and ticks:
            summary = await self.model.ask(
                self.name, 'lane-analyst', self.analysis_prompt,
                json.dumps({'prices': ticks, 'actions': actions,
                            'portfolio': self._portfolio}))
            if summary:
                self.log.emit(self.name, 'lane-analyst', 'analysis', {'summary': summary[:2000]})


LANE_TYPES = {'http_watch': HttpWatchLane, 'paper_markets': PaperMarketsLane}


def build_lanes(cfg, log, model):
    """Instantiate every declared lane; unknown types fail visibly and are
    skipped rather than crashing the service."""
    lanes = []
    for i, lane_cfg in enumerate(cfg.get('lanes') or []):
        lane_type = (lane_cfg or {}).get('type')
        cls = LANE_TYPES.get(lane_type)
        if not cls:
            log.emit('system', 'fast-path', 'lane_rejected',
                     {'index': i, 'type': lane_type,
                      'known_types': sorted(LANE_TYPES)})
            continue
        try:
            lanes.append(cls(lane_cfg, log, model))
        except Exception as e:
            log.emit('system', 'fast-path', 'lane_error',
                     {'index': i, 'error': str(e)[:300]})
    return lanes


app = FastAPI()
LOG = MODEL = None
LANES = []


@app.on_event('startup')
async def _startup():
    # All construction happens at startup, never at import: importing this
    # module (as the tests do) must not open the real lane DB, create the
    # key file, or start threads.
    global LOG, MODEL
    LOG = LaneLog()
    MODEL = FastModel(LOG)
    cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.is_file() else {}
    LANES[:] = build_lanes(cfg, LOG, MODEL)
    LOG.emit('system', 'fast-path', 'boot',
             {'model': FAST_MODEL, 'port': FAST_PORT,
              'kill_switch': str(KILL_SWITCH_PATH),
              'lanes': [l.name for l in LANES]})
    for lane in LANES:
        asyncio.create_task(_lane_loop(lane))


async def _lane_loop(lane):
    while True:
        started = time.monotonic()
        budget = lane.cadence_s * CYCLE_TIMEOUT_FRACTION
        try:
            await asyncio.wait_for(lane.cycle(), timeout=budget)
        except asyncio.TimeoutError:
            LOG.emit(lane.name, 'fast-path', 'cycle_overrun', {'budget_s': budget})
        except Exception as e:
            LOG.emit(lane.name, 'fast-path', 'cycle_error', {'error': str(e)[:300]})
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, lane.cadence_s - elapsed))


@app.get('/status')
async def status():
    return JSONResponse({
        'model': FAST_MODEL,
        'kill_switch': _kill_switch_active(),
        'lanes': [{'name': l.name, 'type': type(l).__name__,
                   'configured': l.configured,
                   'cadence_s': l.cadence_s,
                   'symbols': getattr(l, 'symbols', None),
                   'program': getattr(l, 'program', None),
                   'executor': (None if not getattr(l, 'executor', None)
                                else {'kind': 'vps', 'host': l.executor.host,
                                      'configured': l.executor.configured,
                                      'missing': l.executor.missing()}),
                   'portfolio': getattr(l, '_portfolio', None)} for l in LANES],
        'chain_head': LOG.head(),
        'chain_problems': LOG.verify(),
        'recent': LOG.recent(limit=12),
    })


@app.get('/health')
async def health():
    return {'ok': True}


def main():
    uvicorn.run(app, host=FAST_HOST, port=FAST_PORT, log_level='warning')


if __name__ == '__main__':
    main()
