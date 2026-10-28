"""Fast path: dedicated always-on lanes outside the tank's sim loop.

Design (2026-10-28, second revision after the tape-race and runner-kill
fixes):

- SEPARATE PROCESS. The main tank's ceiling is one contended interpreter:
  sim tick, watches, API and model calls all share it, and a stalled tick
  costs minutes (the watchdog recycles at 180s). Latency-sensitive work
  gets its own process with no sim, no watches, no shared GIL contention.

- ONE GENERIC LANE ENGINE, FULLY JSON-PARAMETERIZED. There are no
  per-use-case lane classes. A lane is a pipeline of stages declared in
  fast_lanes.json; the stage vocabulary is small and safety-audited:

      observe.http_get      GET-only observation of declared targets,
                            rate-limited. No redirects followed.
      observe.vps_dispatch  Declared work runs on a dedicated VPS over
                            pinned-host-key ssh. Bug-bounty work lives
                            here: never in Docker, never from the home IP.
                            Only the config-declared work list ever runs;
                            there is no code path from model output to a
                            shell command.
      observe.price_spot    Public spot prices (coinbase_spot only).
      decide.sma_band       Rule decision over price ticks.
      act.paper_fill        Simulated fills with hard structural caps.
                            There is NO order-placement code path to any
                            exchange in this file.
      analyze.llm           Read-only model commentary, cadence-throttled.

    Unknown stage kinds and impossible compositions are rejected at build
    and never run. The boundary: stages define what is possible (audited
    code); JSON decides what runs (hot-reloaded).

- IDLE BY DEFAULT. A lane with `enabled: false` (or an incomplete config)
  emits one transition event and then goes silent: no polling, no
  probing, no noise. The service itself stays up as a cheap watcher and
  hot-reloads fast_lanes.json on change, so flipping a lane on is a JSON
  edit, not a deployment.

- OWN AUDIT CHAIN, SINGLE WRITER. The 39 decision-tape breaks were two
  writers interleaving on one hash chain. Lane events go through a queue
  to ONE flusher thread that is the only writer of the events table, so
  the chain race is impossible by construction. HMAC-SHA256, keyed by
  .secret_keys/fast_lane.key, independent of the tank's boundary secret.
  Mutable lane state (paper portfolios) lives in a separate unchained
  state table so rebuilds and reloads never touch the audit chain.

- FAIL FAST, DEGRADE HONESTLY. One pinned model with hard deadlines
  (connect 2s, read 8s). A slow or missing model never stalls a lane: the
  lane logs a degraded event and keeps running on its rule layer.

Run:  python3 fast_path.py          (env: FAST_PORT, FAST_HOST, FAST_MODEL)
Stop: launchctl kickstart -k gui/501/com.austtp.tank-fast  (or SIGTERM)
Kill: touch data/fast-kill         (pauses every lane within one cycle)
"""

import asyncio
import hashlib
import hmac
import json
import os
import queue
import sqlite3
import subprocess
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
MAX_TRADES_PER_DAY = max(1, int(os.environ.get('FAST_MAX_TRADES_PER_DAY', '10')))
MAX_NOTIONAL_PER_TRADE = max(1.0, float(os.environ.get('FAST_MAX_NOTIONAL', '500')))
HTTP_TIMEOUT = httpx.Timeout(8.0, connect=2.0)
CYCLE_TIMEOUT_FRACTION = 0.9
RELOAD_POLL_S = 10.0


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
    """Append-only, hash-chained event log plus a separate unchained state
    table. Events: ALL writes go through one flusher thread (callers only
    enqueue). State: short-lived connections, plain key/value, never part
    of the chain."""

    def __init__(self, db_path=LANE_DB_PATH):
        self.db_path = Path(db_path)
        self.key = _lane_key()
        self._q = queue.Queue()
        # Dedicated connection for the flusher thread ONLY (writes).
        # check_same_thread=False is safe here because the single-writer
        # discipline is enforced by construction: nothing but _flush_loop
        # ever issues a statement on _conn. Every read path opens its own
        # short-lived connection instead.
        self._conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.execute('''CREATE TABLE IF NOT EXISTS lane_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, lane TEXT,
            agent_id TEXT, kind TEXT, payload TEXT,
            prev_hash TEXT, hash TEXT)''')
        self._conn.execute('''CREATE TABLE IF NOT EXISTS lane_state (
            lane TEXT PRIMARY KEY, state TEXT, updated_at REAL)''')
        self._conn.commit()
        self._writer = threading.Thread(target=self._flush_loop, name='lane-writer', daemon=True)
        self._writer.start()

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
            if self._read(lambda conn: conn.execute(
                    'SELECT 1 FROM lane_events WHERE payload LIKE ? LIMIT 1',
                    (f'%{marker}%',)).fetchone()):
                return True
            time.sleep(0.02)
        return False

    def _read(self, fn):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute('PRAGMA journal_mode=WAL')
        try:
            return fn(conn)
        finally:
            conn.close()

    def head(self):
        cur = self._read(lambda conn: conn.execute(
            'SELECT id, hash FROM lane_events ORDER BY id DESC LIMIT 1').fetchone())
        return {'id': cur[0], 'hash': cur[1]} if cur else {'id': 0, 'hash': ''}

    def verify(self):
        def walk(conn):
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
        return self._read(walk)

    def recent(self, lane=None, limit=50):
        def query(conn):
            if lane:
                return conn.execute(
                    'SELECT ts, lane, agent_id, kind, payload FROM lane_events '
                    'WHERE lane = ? ORDER BY id DESC LIMIT ?', (lane, limit)).fetchall()
            return conn.execute(
                'SELECT ts, lane, agent_id, kind, payload FROM lane_events '
                'ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        rows = self._read(query)
        return [{'ts': r[0], 'lane': r[1], 'agent_id': r[2], 'kind': r[3],
                 'payload': json.loads(r[4]) if r[4] else {}} for r in rows]

    def load_state(self, lane, default=None):
        row = self._read(lambda conn: conn.execute(
            'SELECT state FROM lane_state WHERE lane = ?', (lane,)).fetchone())
        if not row:
            return default if default is not None else {}
        try:
            return json.loads(row[0])
        except Exception:
            return default if default is not None else {}

    def save_state(self, lane, state):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute('PRAGMA journal_mode=WAL')
        try:
            conn.execute(
                'INSERT INTO lane_state (lane, state, updated_at) VALUES (?, ?, ?) '
                'ON CONFLICT(lane) DO UPDATE SET state = excluded.state, '
                'updated_at = excluded.updated_at',
                (lane, json.dumps(state, default=str), time.time()))
            conn.commit()
        finally:
            conn.close()


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

    Bug-bounty work always runs here, never in a local Docker container
    and never from the operator's home IP: the VPS is the attributable,
    disposable workhorse. Discipline:

    - Only commands declared in the stage's `work` list ever run. Model
      output is analyzed, never executed; there is no code path from model
      text to a shell command.
    - Host key pinning is mandatory (StrictHostKeyChecking=yes against an
      operator-provided known_hosts file); an unpinned host fails closed.
    - BatchMode (key auth only), bounded connect/command timeouts, and a
      bounded output capture keep a hung VPS from stalling the lane.
    """

    def __init__(self, cfg):
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


class StageError(ValueError):
    """A stage declaration the engine refuses to run."""


class Lane:
    """A generic pipeline lane: observe -> decide -> act -> analyze.

    Built entirely from JSON. Stages run in declared order each cycle;
    facts flow through a per-cycle dict; mutable lane state persists in
    the lane DB (unchained) so rebuilds and reloads keep paper positions.
    """

    def __init__(self, cfg, log, model):
        self.cfg = cfg or {}
        self.log = log
        self.model = model
        self.name = self.cfg.get('name') or ''
        if not self.name:
            raise StageError('lane needs a name')
        self.enabled = bool(self.cfg.get('enabled', False))
        self.cadence_s = max(1.0, float(self.cfg.get('cadence_s', 60)))
        self.pipeline = self.cfg.get('pipeline') or []
        if not isinstance(self.pipeline, list) or not self.pipeline:
            raise StageError(f'lane {self.name!r} needs a non-empty pipeline list')
        self.stages = []
        seen_kinds = []
        for i, s in enumerate(self.pipeline):
            kind = (s or {}).get('kind')
            factory = STAGE_REGISTRY.get(kind)
            if not factory:
                raise StageError(
                    f'lane {self.name!r} stage {i}: unknown kind {kind!r} '
                    f'(known: {sorted(STAGE_REGISTRY)})')
            stage = factory(s, self)
            stage.validate(seen_kinds)
            seen_kinds.append(kind)
            self.stages.append(stage)
        self.mode = 'idle'
        self._inhibited = False
        self.last_cycle_ts = None

    # -- lifecycle ------------------------------------------------------
    def note(self, agent, kind, payload):
        self.log.emit(self.name, agent, kind, payload)

    def enter_mode(self, mode, payload):
        """Emit only on TRANSITION. Idle and paused lanes are silent."""
        if self.mode != mode:
            self.mode = mode
            self.note('fast-path', mode, payload)

    def inhibit(self, payload):
        """A stage found the lane unable to work (no scope, unpinned VPS).
        Demote to idle ONCE; stay silent on later cycles until a reload
        rebuilds this lane object."""
        self._inhibited = True
        self.enter_mode('idle', payload)

    async def cycle(self):
        if _kill_switch_active():
            self.enter_mode('paused', {'reason': 'kill switch'})
            return
        if not self.enabled:
            self.enter_mode('idle', {'note': 'enabled=false in fast_lanes.json'})
            return
        if self._inhibited:
            return  # already announced why; silence until reload
        self.enter_mode('running', {'cadence_s': self.cadence_s})
        facts = {}
        for stage in self.stages:
            try:
                await stage.run(facts)
            except Exception as e:
                self.note(stage.AGENT, 'stage_error',
                          {'stage': stage.kind, 'error': str(e)[:300]})
        self.last_cycle_ts = time.time()


class HttpGetStage:
    """GET-only observation of declared targets. Recon semantics when the
    lane declares `program`; plain surface-watch otherwise. Never touches
    a host outside `targets`, never issues anything but GET."""
    KIND = 'http_get'
    AGENT = 'lane-observer'

    def __init__(self, cfg, lane):
        self.kind = self.KIND
        self.lane = lane
        self.program = cfg.get('program') or ''
        self.targets = [t for t in (cfg.get('targets') or [])
                        if isinstance(t, str) and t.strip()]
        self.rate_limit_s = max(0.0, float(cfg.get('rate_limit_s', 2.0)))
        self._last = {}
        self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False)

    def validate(self, seen_kinds):
        pass

    async def run(self, facts):
        lane = self.lane
        if not self.targets:
            lane.inhibit({'note': 'declare targets; no requests run '
                                  'without an explicit scope'})
            return
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
        facts['observations'] = findings
        lane.note(self.AGENT, 'cycle', {'program': self.program,
                                        'findings': findings,
                                        'targets': len(self.targets)})


class VpsDispatchStage:
    """Work happens on the VPS; this machine only dispatches, observes,
    and keeps the receipts. Every dispatch is a declared work item."""
    KIND = 'vps_dispatch'
    AGENT = 'lane-dispatcher'

    def __init__(self, cfg, lane):
        self.kind = self.KIND
        self.lane = lane
        self.work = [{'name': (w or {}).get('name') or '',
                      'command': (w or {}).get('command') or ''}
                     for w in (cfg.get('work') or [])
                     if isinstance(w, dict) and (w or {}).get('command')]
        self.work_timeout_s = float(cfg.get('work_timeout_s', 120))
        self.executor = VpsExecutor(cfg.get('executor') or {})

    def validate(self, seen_kinds):
        pass

    @property
    def configured(self):
        return self.executor.configured

    async def run(self, facts):
        lane = self.lane
        if not self.executor.configured:
            lane.inhibit({'missing': self.executor.missing(),
                          'note': 'executor fails closed until the VPS is '
                                  'declared and pinned'})
            return
        if not self.work:
            lane.inhibit({'note': 'declare the work list; the VPS runs '
                                  'nothing the config did not name'})
            return
        results = []
        for item in self.work:
            lane.note(self.AGENT, 'dispatch',
                      {'work': item['name'], 'host': self.executor.host})
            result = await asyncio.to_thread(
                self.executor.run, item['command'], self.work_timeout_s)
            lane.note(self.AGENT, 'result',
                      {'work': item['name'], 'exit_code': result['exit_code'],
                       'duration_s': result['duration_s'],
                       'stdout_tail': result['stdout'][-3000:],
                       'stderr_tail': result['stderr'][-1500:],
                       **({'error': result['error']} if result.get('error') else {})})
            results.append({'work': item['name'], **result})
        facts['work_results'] = results


class PriceSpotStage:
    """Public spot prices from a whitelisted source. No keys, no orders."""
    KIND = 'price_spot'
    AGENT = 'lane-markets'
    SOURCES = {'coinbase_spot': 'https://api.coinbase.com/v2/prices/{symbol}/spot'}

    def __init__(self, cfg, lane):
        self.kind = self.KIND
        self.lane = lane
        self.source = cfg.get('source', 'coinbase_spot')
        if self.source not in self.SOURCES:
            raise StageError(f'unknown price source {self.source!r} '
                             f'(known: {sorted(self.SOURCES)})')
        self.symbols = [s for s in (cfg.get('symbols') or [])
                        if isinstance(s, str) and s]
        if not self.symbols:
            raise StageError('price_spot needs symbols')
        self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

    def validate(self, seen_kinds):
        pass

    async def run(self, facts):
        lane = self.lane
        ticks = {}
        for symbol in self.symbols:
            try:
                r = await self._client.get(self.SOURCES[self.source].format(symbol=symbol))
                r.raise_for_status()
                ticks[symbol] = float(r.json()['data']['amount'])
            except Exception as e:
                lane.note(self.AGENT, 'price_degraded',
                          {'symbol': symbol, 'error': str(e)[:120]})
        facts['prices'] = ticks


class SmaBandStage:
    """Rule decision over price ticks: buy below the band, sell above,
    hold inside. Needs no model; works when the model is degraded."""
    KIND = 'sma_band'
    AGENT = 'lane-markets'

    def __init__(self, cfg, lane):
        self.kind = self.KIND
        self.lane = lane
        self.window = max(2, int(cfg.get('window', 30)))
        self.band_pct = float(cfg.get('band_pct', 2.0))
        self._prices = {}

    def validate(self, seen_kinds):
        if 'price_spot' not in seen_kinds:
            raise StageError('sma_band needs a price_spot stage before it')

    async def run(self, facts):
        decisions = {}
        for symbol, price in (facts.get('prices') or {}).items():
            win = self._prices.setdefault(symbol, deque(maxlen=self.window))
            win.append(price)
            if len(win) < 10:
                decisions[symbol] = {'action': 'hold', 'why': 'warming up'}
                continue
            sma = sum(win) / len(win)
            drift = (price - sma) / sma * 100
            if drift <= -self.band_pct:
                decisions[symbol] = {'action': 'buy',
                                     'why': f'{drift:.2f}% below {len(win)}-tick SMA'}
            elif drift >= self.band_pct:
                decisions[symbol] = {'action': 'sell',
                                     'why': f'{drift:.2f}% above {len(win)}-tick SMA'}
            else:
                decisions[symbol] = {'action': 'hold', 'why': 'inside band'}
        facts['decisions'] = decisions
        for symbol, d in decisions.items():
            if d['action'] == 'hold':
                self.lane.note(self.AGENT, 'tick',
                               {'symbol': symbol,
                                'price': facts['prices'].get(symbol),
                                'note': d['why']})


class PaperFillStage:
    """Simulated fills with structural caps. State persists in the lane DB
    (unchained) so reloads and restarts keep positions. There is NO
    order-placement code path to any exchange anywhere in this file."""
    KIND = 'paper_fill'
    AGENT = 'lane-markets'

    def __init__(self, cfg, lane):
        self.kind = self.KIND
        self.lane = lane
        self.notional = min(float(cfg.get('notional', 100)), MAX_NOTIONAL_PER_TRADE)

    def validate(self, seen_kinds):
        if 'price_spot' not in seen_kinds:
            raise StageError('paper_fill needs a price_spot stage before it')
        if 'sma_band' not in seen_kinds:
            raise StageError('paper_fill needs a decide stage (sma_band) before it')

    def _today(self):
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')

    async def run(self, facts):
        lane = self.lane
        state = lane.log.load_state(lane.name, {}) or {}
        portfolio = state.get('portfolio') or {}
        for s in facts.get('prices', {}):
            portfolio.setdefault(s, {'units': 0.0, 'cash': 1000.0})
        trades_today = state.get('trades_today') or {}
        if trades_today.get('day') != self._today():
            trades_today = {'day': self._today(), 'count': 0}
        for symbol, decision in (facts.get('decisions') or {}).items():
            action = decision.get('action')
            if action not in ('buy', 'sell'):
                continue
            price = facts['prices'][symbol]
            if trades_today['count'] >= MAX_TRADES_PER_DAY:
                lane.note(self.AGENT, 'fill_blocked',
                          {'symbol': symbol, 'reason': 'daily trade cap reached'})
                continue
            pos = portfolio.setdefault(symbol, {'units': 0.0, 'cash': 1000.0})
            units = self.notional / price
            if action == 'buy':
                if pos['cash'] < self.notional:
                    lane.note(self.AGENT, 'fill_blocked',
                              {'symbol': symbol, 'reason': 'insufficient paper cash'})
                    continue
                pos['cash'] -= self.notional
                pos['units'] += units
            else:
                if pos['units'] < units:
                    units = pos['units']
                if units <= 0:
                    lane.note(self.AGENT, 'fill_blocked',
                              {'symbol': symbol, 'reason': 'no position to sell'})
                    continue
                pos['units'] -= units
                pos['cash'] += units * price
            trades_today['count'] += 1
            fill = {'symbol': symbol, 'action': action, 'price': price,
                    'units': round(units, 8), 'notional': round(units * price, 2),
                    'portfolio': {k: round(v, 4) for k, v in pos.items()}}
            facts.setdefault('fills', []).append(fill)
            lane.note(self.AGENT, 'paper_fill', fill)
        lane.log.save_state(lane.name, {'portfolio': portfolio,
                                        'trades_today': trades_today})


class LlmAnalyzeStage:
    """Read-only model commentary. Cadence-throttled (every N cycles);
    a degraded model costs the lane nothing but one logged event. Model
    output is recorded as text and NEVER executed."""
    KIND = 'llm'
    AGENT = 'lane-analyst'

    def __init__(self, cfg, lane):
        self.kind = self.KIND
        self.lane = lane
        self.prompt = cfg.get('prompt') or (
            'You are an analyst. Be concise: list anything unusual worth a '
            'human look, or say "nothing notable".')
        self.every = max(1, int(cfg.get('every', 1)))
        self.max_input_chars = int(cfg.get('max_input_chars', 6000))
        self._cycles = 0

    def validate(self, seen_kinds):
        pass

    async def run(self, facts):
        self._cycles += 1
        if self._cycles % self.every != 0:
            return
        if not any(k in facts for k in ('observations', 'work_results', 'prices', 'fills')):
            return
        payload = {k: facts[k] for k in
                   ('observations', 'work_results', 'prices', 'fills', 'decisions')
                   if k in facts}
        summary = await self.lane.model.ask(
            self.lane.name, self.AGENT,
            self.prompt + ' You are read-only analysis over returned data; '
            'never propose executing anything.',
            json.dumps(payload, default=str)[:self.max_input_chars])
        if summary:
            self.lane.note(self.AGENT, 'analysis', {'summary': summary[:2000]})


class LogFactsStage:
    """Record the cycle's facts as one lane event (for lanes that only
    watch and report)."""
    KIND = 'log'
    AGENT = 'lane-observer'

    def __init__(self, cfg, lane):
        self.kind = self.KIND
        self.lane = lane

    def validate(self, seen_kinds):
        pass

    async def run(self, facts):
        self.lane.note(self.AGENT, 'facts', facts)


STAGE_REGISTRY = {
    HttpGetStage.KIND: HttpGetStage,
    VpsDispatchStage.KIND: VpsDispatchStage,
    PriceSpotStage.KIND: PriceSpotStage,
    SmaBandStage.KIND: SmaBandStage,
    PaperFillStage.KIND: PaperFillStage,
    LlmAnalyzeStage.KIND: LlmAnalyzeStage,
    LogFactsStage.KIND: LogFactsStage,
}


def build_lanes(cfg, log, model):
    """Instantiate every declared lane; bad declarations fail visibly and
    are skipped rather than crashing the service."""
    lanes, rejected = [], []
    names = set()
    for i, lane_cfg in enumerate(cfg.get('lanes') or []):
        name = (lane_cfg or {}).get('name')
        if name in names:
            rejected.append({'index': i, 'name': name,
                             'error': 'duplicate lane name'})
            continue
        names.add(name)
        try:
            lanes.append(Lane(lane_cfg, log, model))
        except StageError as e:
            rejected.append({'index': i,
                             'name': (lane_cfg or {}).get('name'),
                             'error': str(e)})
        except Exception as e:
            rejected.append({'index': i,
                             'name': (lane_cfg or {}).get('name'),
                             'error': str(e)[:300]})
    for r in rejected:
        log.emit('system', 'fast-path', 'lane_rejected', r)
    return lanes


class LaneSupervisor:
    """Owns the lane tasks; hot-reloads the config file on change. Reloads
    rebuild lane objects (paper state survives in the lane DB) and restart
    their loops."""

    def __init__(self, log, model):
        self.log = log
        self.model = model
        self.lanes = []
        self.tasks = {}
        self._mtime = None
        self._stop = asyncio.Event()

    def _load_config(self):
        if not Path(CONFIG_PATH).is_file():
            return {}
        return json.loads(Path(CONFIG_PATH).read_text())

    async def start(self):
        await self.reload(force=True)
        asyncio.create_task(self._reload_loop())

    async def reload(self, force=False):
        try:
            mtime = (Path(CONFIG_PATH).stat().st_mtime
                     if Path(CONFIG_PATH).is_file() else None)
        except OSError:
            mtime = None
        if not force and mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            cfg = self._load_config()
        except Exception as e:
            self.log.emit('system', 'fast-path', 'config_error',
                          {'error': str(e)[:300]})
            return
        old = {l.name: l for l in self.lanes}
        lanes = build_lanes(cfg, self.log, self.model)
        # Stop loops for lanes that disappeared or changed definition.
        new_by_name = {l.name: l for l in lanes}
        for name, task in list(self.tasks.items()):
            if name not in new_by_name or \
                    json.dumps(new_by_name[name].cfg, sort_keys=True, default=str) != \
                    json.dumps(old.get(name).cfg if old.get(name) else None,
                               sort_keys=True, default=str):
                task.cancel()
                self.tasks.pop(name, None)
        self.log.emit('system', 'fast-path', 'reload',
                      {'lanes': [l.name for l in lanes]})
        for lane in lanes:
            if lane.name not in self.tasks:
                self.tasks[lane.name] = asyncio.create_task(_lane_loop(lane))
        self.lanes = lanes

    async def _reload_loop(self):
        while True:
            await asyncio.sleep(RELOAD_POLL_S)
            try:
                await self.reload()
            except Exception as e:
                self.log.emit('system', 'fast-path', 'reload_error',
                              {'error': str(e)[:200]})


def rejected_names(cfg, lanes, log):
    declared = {(l.get('name') if isinstance(l, dict) else None)
                for l in (cfg.get('lanes') or [])}
    built = {l.name for l in lanes}
    return sorted(n for n in declared - built if n)


async def _lane_loop(lane):
    while True:
        started = time.monotonic()
        budget = lane.cadence_s * CYCLE_TIMEOUT_FRACTION
        try:
            await asyncio.wait_for(lane.cycle(), timeout=budget)
        except asyncio.TimeoutError:
            lane.note('fast-path', 'cycle_overrun', {'budget_s': budget})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            lane.note('fast-path', 'cycle_error', {'error': str(e)[:300]})
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, lane.cadence_s - elapsed))


app = FastAPI()
LOG = MODEL = SUPERVISOR = None


@app.on_event('startup')
async def _startup():
    # All construction happens at startup, never at import: importing this
    # module (as the tests do) must not open the real lane DB, create the
    # key file, or start threads.
    global LOG, MODEL, SUPERVISOR
    LOG = LaneLog()
    MODEL = FastModel(LOG)
    SUPERVISOR = LaneSupervisor(LOG, MODEL)
    await SUPERVISOR.start()


@app.get('/status')
async def status():
    return JSONResponse({
        'model': FAST_MODEL,
        'kill_switch': _kill_switch_active(),
        'config_mtime': (Path(CONFIG_PATH).stat().st_mtime
                         if Path(CONFIG_PATH).is_file() else None),
        'lanes': [{'name': l.name, 'enabled': l.enabled,
                   'mode': l.mode, 'cadence_s': l.cadence_s,
                   'stages': [s.kind for s in l.stages],
                   'last_cycle_ts': l.last_cycle_ts,
                   'portfolio': (l.log.load_state(l.name, {}) or {}).get('portfolio')}
                  for l in SUPERVISOR.lanes],
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
