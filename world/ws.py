"""Generic WebSocket feed collector.

Extracted from serve.py. One loop interprets a declarative ws_spec descriptor
(url, subscribe template with credential/variable substitution, frame filter,
field paths, buffer kind) for ANY "subscribe then stream JSON" feed -- Kraken
and AISStream are just two registered instances (ws_feeds DB table, seeded on
first boot). A supervisor reconciles running collector tasks with the live
registry every few seconds, so a director/admin adding, editing, or removing a
feed via /api/ws/* takes effect with no restart. HTTP's universal contract is
what makes api_call generic; WebSocket has no such contract, so the descriptor
language IS the abstraction. Hard boundary: binary frames, protobuf, and
multi-round handshake protocols cannot be described and stay as code.

Everything that tests or other modules patch on the serve module is reached
through `_serve.X` at call time (the same lazy pattern auth.py documents), so:
  - a test patching `serve._WS_FEEDS` / `serve._WS_BUFFERS` / `serve._ws_ingest`
    is honored by the functions below, and
  - serve.py re-imports these names unchanged, so no call site moved.
"""

import asyncio
import json
import re
import sqlite3
import threading
import time
import urllib.parse

WS_FEED_SEED = {
    'kraken': {
        'name': 'Kraken public crypto ticker',
        'credential': {'type': 'none'},
        'ws': {
            'url': 'wss://ws.kraken.com',
            'subscribe': {'event': 'subscribe', 'pair': ['$PAIRS'],
                          'subscription': {'name': 'ticker'}},
            'vars': {'$PAIRS': ['XBT/USD', 'ETH/USD', 'XRP/USD', 'SOL/USD', 'XDG/USD']},
            'open_timeout_s': 15,
            'stall_timeout_s': 90,
            'reconnect_backoff_s': 15,
            'frame_filter': {
                'data_kind': 'list',
                'data_index': 1,
                # Ticker frames are [channelID, data, channelName, pair].
                'topic_path': '[3]',
                'fields': {'price': 'c[0]', 'bid': 'b[0]', 'ask': 'a[0]', 'volume': 'v[0]'},
            },
            'buffer': {'kind': 'latest_per_topic', 'max_topics': 100, 'history': 200},
        },
    },
    'aisstream': {
        'name': 'AISStream live maritime positions',
        'credential': {'type': 'env', 'key': 'AISSTREAM_API_KEY'},
        'ws': {
            'url': 'wss://stream.aisstream.io/v0/stream',
            'subscribe': {'APIKey': '$CRED',
                          'BoundingBoxes': [[[36.9, -76.3], [33.3, -74.8]]],
                          'FilterMessageTypes': ['PositionReport']},
            'vars': {},
            'open_timeout_s': 20,
            'stall_timeout_s': 120,
            'reconnect_backoff_s': 15,
            'frame_filter': {
                'data_kind': 'dict',
                'topic_path': 'MetaData.ShipName',
                'skip_on_key': 'MessageType',
                'skip_values': ['SubscriptionConfirmation'],
                'fields': {'lat': 'MetaData.latitude', 'lon': 'MetaData.longitude',
                           'time': 'MetaData.time_utc', 'mmsi': 'MetaData.MMSI_String'},
            },
            'buffer': {'kind': 'ring', 'history': 300},
        },
    },
}

# Public aliases used by serve.py's routes and lifespan wiring. The underscore
# names below are the originals the tests and the rest of serve.py already use.
_WS_FEED_SEED = WS_FEED_SEED
_WS_FEEDS: dict = {}  # live registry: sid -> {'name', 'credential', 'ws': descriptor} (DB-backed)
_WS_FEEDS_LOAD_ERROR = None
_WS_BUFFERS: dict = {}  # sid -> {'latest': {topic: rec}, 'history': [...], 'status': {...}}
_WS_LOCK = threading.Lock()
_WS_TASKS: dict = {}  # sid -> (spec_fingerprint, asyncio.Task)


def _ws_feed_spec_to_row(spec, status='active', proposed_by=None):
    now = time.time()
    cred = spec.get('credential') or {}
    return {
        'id': spec.get('id'),
        'name': spec.get('name') or spec.get('id'),
        'credential_type': cred.get('type') or 'none',
        'credential_key': cred.get('key') if cred.get('type') in ('env', 'vault') else None,
        'credential_key2': cred.get('key2'),
        'ws_spec': json.dumps(spec.get('ws') or {}, sort_keys=True),
        'status': status,
        'proposed_by': proposed_by,
        'created_at': now,
        'updated_at': now,
    }


def _ws_feed_row_to_spec(row):
    try:
        ws = json.loads(row.get('ws_spec') or '{}')
    except ValueError:
        ws = {}
    return {'id': row['id'], 'name': row['name'],
            'credential': {'type': row.get('credential_type') or 'none',
                           'key': row.get('credential_key'),
                           'key2': row.get('credential_key2')},
            'ws': ws, 'status': row['status'], 'proposed_by': row.get('proposed_by')}


def _load_ws_feeds():
    """Load the active ws_feeds rows into the live _WS_FEEDS registry. Fail
    closed: on DB error the registry stays empty (no collectors start)."""
    import serve as _serve
    _serve._WS_FEEDS = {}
    _serve._WS_FEEDS_LOAD_ERROR = None
    try:
        with _serve._db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, name, credential_type, credential_key, credential_key2, ws_spec, status, proposed_by "
                "FROM ws_feeds WHERE status = 'active'"  # nosec B608 -- fixed internal columns
            ).fetchall()
    except Exception as e:
        _serve._WS_FEEDS_LOAD_ERROR = f'db unavailable: {e}'
        return
    feeds = {}
    for row in rows:
        spec = _serve._ws_feed_row_to_spec(dict(row))
        feeds[spec['id']] = spec
    _serve._WS_FEEDS = feeds


def _seed_ws_feeds_if_empty():
    """Seed the ws_feeds table from _WS_FEED_SEED the first time it comes up
    empty, mirroring _seed_api_services_if_empty."""
    import serve as _serve
    try:
        with _serve._db() as conn:
            count = conn.execute('SELECT COUNT(*) FROM ws_feeds').fetchone()[0]
            if count:
                return
            for sid, feed in _serve._WS_FEED_SEED.items():
                spec = {'id': sid, 'name': feed.get('name'),
                        'credential': feed.get('credential') or {},
                        'ws': feed.get('ws') or {}}
                row = _serve._ws_feed_spec_to_row(spec, status='active', proposed_by='seed')
                conn.execute(
                    'INSERT INTO ws_feeds (id, name, credential_type, credential_key, '
                    'credential_key2, ws_spec, status, proposed_by, created_at, updated_at) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    [row[c] for c in ('id', 'name', 'credential_type', 'credential_key',
                                      'credential_key2', 'ws_spec', 'status', 'proposed_by',
                                      'created_at', 'updated_at')])
    except Exception as e:
        print(f'[ws] seed failed: {e}', flush=True)


def _ws_feed_upsert(spec, status='active', proposed_by=None):
    """Persist a feed spec as a row and refresh the live registry. Used by the
    propose flow (status='proposed'), approval flow ('active'/'denied'), and
    remove flow ('removed')."""
    import serve as _serve
    row = _serve._ws_feed_spec_to_row(spec, status=status, proposed_by=proposed_by)
    with _serve._db() as conn:
        conn.execute(
            'INSERT INTO ws_feeds (id, name, credential_type, credential_key, '
            'credential_key2, ws_spec, status, proposed_by, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(id) DO UPDATE SET name=excluded.name, '
            'credential_type=excluded.credential_type, credential_key=excluded.credential_key, '
            'credential_key2=excluded.credential_key2, ws_spec=excluded.ws_spec, '
            'status=excluded.status, proposed_by=excluded.proposed_by, updated_at=excluded.updated_at',
            [row[c] for c in ('id', 'name', 'credential_type', 'credential_key',
                              'credential_key2', 'ws_spec', 'status', 'proposed_by',
                              'created_at', 'updated_at')])
    _serve._load_ws_feeds()


def _validate_ws_feed_spec(spec):
    """Validate a proposed websocket feed descriptor. Returns (sid, normalized,
    error) -- error is None when valid. Security constraints mirror the other
    registry validators: a public wss:// URL only (the collector connects to
    it), a known credential source, a describable frame filter, and non-empty
    field paths. Re-validated at approval time so a forged proposal cannot
    grant more than this allows."""
    import serve as _serve
    spec = spec or {}
    sid = str(spec.get('id') or '').strip().lower()
    if not sid or not sid.replace('_', '').replace('-', '').isalnum():
        return None, None, 'feed id must be a simple alphanumeric slug'
    ws = spec.get('ws') or {}
    url = str(ws.get('url') or '').strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'wss' or not parsed.hostname or not _serve._is_safe_public_host(parsed.hostname):
        return None, None, 'ws.url must be a public wss:// URL'
    if not isinstance(ws.get('subscribe'), dict):
        return None, None, 'ws.subscribe must be a JSON object (the subscription template)'
    cred = spec.get('credential') or {}
    if cred.get('type') not in ('none', 'env', 'vault'):
        return None, None, 'credential.type must be "none", "env", or "vault"'
    if cred.get('type') in ('env', 'vault') and not (cred.get('key') or '').strip():
        return None, None, 'credential.key is required for env/vault credentials'
    ff = ws.get('frame_filter') or {}
    if ff.get('data_kind') not in ('list', 'dict'):
        return None, None, 'frame_filter.data_kind must be "list" or "dict"'
    fields = ff.get('fields') or {}
    if not isinstance(fields, dict) or not fields:
        return None, None, 'frame_filter.fields must be a non-empty mapping of name -> path'
    buf = ws.get('buffer') or {}
    if buf.get('kind') not in ('latest_per_topic', 'ring'):
        return None, None, 'buffer.kind must be "latest_per_topic" or "ring"'
    # Clamp every numeric knob so a bad proposal cannot DoS the collector.
    ws['open_timeout_s'] = max(5, min(int(ws.get('open_timeout_s') or 15), 60))
    ws['stall_timeout_s'] = max(15, min(int(ws.get('stall_timeout_s') or 90), 600))
    ws['reconnect_backoff_s'] = max(5, min(int(ws.get('reconnect_backoff_s') or 15), 300))
    return sid, {'id': sid, 'name': str(spec.get('name') or sid)[:120],
                 'credential': {'type': cred.get('type') or 'none',
                                'key': cred.get('key') if cred.get('type') in ('env', 'vault') else None,
                                'key2': cred.get('key2')},
                 'ws': ws}, None


def _ws_resolve_path(obj, path):
    """Resolve a tiny path language against a parsed frame: bare segments are
    dict keys, bracketed numbers are list indices. 'MetaData.ShipName',
    'c[0]', '[3]'. Returns None when any step is missing or the wrong type."""
    tokens = re.findall(r'[^.\[\]]+|\[\d+\]', path or '')
    cur = obj
    for tok in tokens:
        if tok.startswith('[') and tok.endswith(']'):
            idx = int(tok[1:-1])
            if not isinstance(cur, (list, tuple)) or idx >= len(cur):
                return None
            cur = cur[idx]
        else:
            if not isinstance(cur, dict) or tok not in cur:
                return None
            cur = cur[tok]
    return cur


def _ws_extract_frame(ws_cfg, data):
    """Extract (topic, fields, raw) from one frame per the feed's frame_filter.
    Returns (None, None, None) for control frames (heartbeats, subscription
    confirmations, malformed shapes)."""
    import serve as _serve
    ff = ws_cfg.get('frame_filter') or {}
    kind = ff.get('data_kind', 'dict')
    if kind == 'list':
        # List-framed feeds: data frames are [..., {data}, ...]; any dict is a
        # control frame and any list without a dict payload is skipped.
        if not isinstance(data, list):
            return None, None, None
        idx = int(ff.get('data_index') or 1)
        if len(data) <= idx or not isinstance(data[idx], dict):
            return None, None, None
        payload = data[idx]
        topic = _serve._ws_resolve_path(data, ff.get('topic_path'))
    else:
        if not isinstance(data, dict):
            return None, None, None
        skip_key = ff.get('skip_on_key')
        skip_vals = ff.get('skip_values') or []
        if skip_key and data.get(skip_key) in skip_vals:
            return None, None, None
        payload = data
        topic = _serve._ws_resolve_path(data, ff.get('topic_path'))
    if not topic:
        return None, None, None
    fields = {}
    for fname, path in (ff.get('fields') or {}).items():
        try:
            val = _serve._ws_resolve_path(payload, path)
        except Exception:  # noqa: BLE001 -- a missing field is skipped, never a dropped frame
            val = None
        if val is not None:
            fields[fname] = val
    return topic, fields, data


def _ws_substitute(node, token, var_map):
    """Recursively substitute $CRED (the resolved credential) and $VARS entries
    into a subscription template. A var whose value is a LIST splices into an
    enclosing list (Kraken's 'pair': ['$PAIRS'] must become a flat pair array,
    not a nested one)."""
    if isinstance(node, dict):
        return {k: _ws_substitute(v, token, var_map) for k, v in node.items()}
    if isinstance(node, list):
        out = []
        for v in node:
            if isinstance(v, str) and v in var_map and isinstance(var_map[v], list):
                out.extend(var_map[v])
            else:
                out.append(_ws_substitute(v, token, var_map))
        return out
    if isinstance(node, str):
        if node == '$CRED' and token is not None:
            return token
        if node in var_map:
            return var_map[node]
        return node
    return node


def _ws_subscribe_payload(ws_cfg, token):
    return _ws_substitute(ws_cfg.get('subscribe') or {}, token, ws_cfg.get('vars') or {})


def _ws_ingest(sid, topic, fields, raw):
    import serve as _serve
    buf = _serve._WS_BUFFERS.setdefault(sid, {'latest': {}, 'history': [],
                                              'status': {'connected': False, 'last_error': '',
                                                         'connected_at': 0, 'last_tick_at': 0}})
    spec = _serve._WS_FEEDS.get(sid) or {}
    cfg = (spec.get('ws') or {}).get('buffer') or {}
    now = int(time.time() * 1000)
    rec = {'ts': now, 'topic': topic, **fields, 'raw': raw}
    kind = cfg.get('kind', 'latest_per_topic')
    with _serve._WS_LOCK:
        buf['status']['last_tick_at'] = time.time()
        if kind == 'ring':
            buf['history'].append(rec)
            cap = int(cfg.get('history') or 300)
            if len(buf['history']) > cap:
                del buf['history'][:len(buf['history']) - cap]
        else:
            buf['latest'][topic] = rec
            max_topics = int(cfg.get('max_topics') or 100)
            if len(buf['latest']) > max_topics:
                oldest = min(buf['latest'], key=lambda t: buf['latest'][t]['ts'])
                del buf['latest'][oldest]
            buf['history'].append(rec)
            cap = int(cfg.get('history') or 200)
            if len(buf['history']) > cap:
                del buf['history'][:len(buf['history']) - cap]


def _ws_status(sid):
    import serve as _serve
    return dict((_serve._WS_BUFFERS.get(sid) or {}).get('status') or {})


def _ws_latest(sid, topics=None):
    import serve as _serve
    buf = _serve._WS_BUFFERS.get(sid) or {}
    with _serve._WS_LOCK:
        latest = buf.get('latest') or {}
        wanted = topics or list(latest)
        return {t: dict(latest[t]) for t in wanted if t in latest}


def _ws_recent(sid, max_items=20, window_s=1800, topic=None):
    import serve as _serve
    buf = _serve._WS_BUFFERS.get(sid) or {}
    cutoff = time.time() - window_s
    with _serve._WS_LOCK:
        items = [dict(r) for r in (buf.get('history') or [])
                 if r['ts'] / 1000 >= cutoff and (topic is None or r.get('topic') == topic)]
    return items[-max_items:]


def _kraken_normalize_pair(pair):
    """Normalize a user-supplied pair to Kraken's canonical symbol set:
    uppercase, '-'/'_' -> '/', and the common aliases BTC->XBT and DOGE->XDG
    (Kraken's ticker symbols for bitcoin and dogecoin)."""
    p = (pair or '').strip().upper().replace('-', '/').replace('_', '/')
    p = p.replace('BTC', 'XBT').replace('DOGE', 'XDG')
    return p


async def _ws_collector_loop(sid):
    """One generic collector per registered feed. Reads the spec fresh from the
    live registry on every reconnect, resolves the credential (if any),
    subscribes, and ingests extracted frames into the service buffer. The stall
    watchdog forces a reconnect when a subscribed-but-silent connection sits
    idle (observed live after rapid reconnects throttled the IP)."""
    import websockets
    import serve as _serve
    while True:
        spec = _serve._WS_FEEDS.get(sid) or {}
        ws = spec.get('ws') or {}
        url = ws.get('url') or ''
        stall = float(ws.get('stall_timeout_s') or 90)
        backoff = float(ws.get('reconnect_backoff_s') or 15)
        open_timeout = float(ws.get('open_timeout_s') or 15)
        buf = _serve._WS_BUFFERS.setdefault(sid, {'latest': {}, 'history': [],
                                                  'status': {'connected': False, 'last_error': '',
                                                             'connected_at': 0, 'last_tick_at': 0}})
        try:
            token, _t2, err = _serve._api_credential_value(spec)
            if err:
                buf['status'].update(connected=False, last_error=err)
                print(f'[ws:{sid}] credential error: {err}', flush=True)
                await asyncio.sleep(backoff)
                continue
            payload = _serve._ws_subscribe_payload(ws, token)
            async with websockets.connect(
                    url, max_size=2 ** 20,
                    open_timeout=open_timeout, ping_interval=20, ping_timeout=20) as conn:
                await conn.send(json.dumps(payload))
                buf['status'].update(connected=True, last_error='', connected_at=time.time())
                print(f'[ws:{sid}] connected', flush=True)
                while True:
                    try:
                        raw = await asyncio.wait_for(conn.recv(), timeout=stall)
                    except asyncio.TimeoutError:
                        print(f'[ws:{sid}] no data for {stall}s; reconnecting', flush=True)
                        break
                    except Exception:  # noqa: BLE001 -- ConnectionClosed et al: reconnect
                        break
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        continue
                    topic, fields, full = _serve._ws_extract_frame(ws, data)
                    if topic is None:
                        continue
                    _serve._ws_ingest(sid, topic, fields, full)
        except Exception as e:  # noqa: BLE001 -- a drop/backoff and retry is the whole point
            buf['status'].update(connected=False, last_error=str(e)[:200])
            print(f'[ws:{sid}] collector error: {e}', flush=True)
            await asyncio.sleep(backoff)


async def _ws_supervisor_loop():
    """Reconcile running collector tasks with the live _WS_FEEDS registry every
    few seconds: spawn collectors for newly-active feeds, cancel collectors for
    removed/denied feeds, and restart a collector whose spec fingerprint changed
    (an operator's edit goes live without a restart)."""
    import serve as _serve
    while True:
        feeds = _serve._WS_FEEDS
        tasks = _serve._WS_TASKS
        for sid, spec in list(feeds.items()):
            fp = json.dumps(spec, sort_keys=True)
            entry = tasks.get(sid)
            if entry is None or entry[0] != fp or entry[1].done():
                if entry is not None:
                    entry[1].cancel()
                tasks[sid] = (fp, asyncio.create_task(_serve._ws_collector_loop(sid)))
        for sid, entry in list(tasks.items()):
            if sid not in feeds:
                entry[1].cancel()
                tasks.pop(sid, None)
        await asyncio.sleep(10)
