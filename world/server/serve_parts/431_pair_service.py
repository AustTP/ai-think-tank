# Pairing mode (2026-11-XX): the player sits down with one agent (Theo) in a
# conversational thread. The agent may talk, but the work-request lane is
# deliberately separated: when the conversation produces a concrete work item,
# the agent does NOT file a card -- it emits a `HANDOFF: <request>` line and
# this service mails that request to the coordinator (ruth), who owns the
# filing pipeline. This keeps the interactive lane free of the story-gate
# bureaucracy while making sure nothing actionable is lost: every handoff is a
# durable mailbox entry with a full audit trail in pair_messages.
#
# Auth: POST /api/pair/* requires a player session OR a PAIR_KEY from .env
# (the CLI scripts/pair_chat.py uses the key path). The agent identity for the
# model call is fixed here (PAIR_AGENT_ID), and its key is resolved server-side
# so the player can never act as the agent or forge its mail.
#
# Fail-closed: if PAIR_KEY is not configured, the key path is refused (503)
# rather than open.

import os
import re
import time
import sqlite3
import contextlib
import json as _json

_PAIR_AGENT_ID = os.environ.get('PAIR_AGENT_ID') or 'theo'
_PAIR_COORDINATOR_ID = os.environ.get('PAIR_COORDINATOR_ID') or 'ruth'
_PAIR_HISTORY_TURNS = 12          # messages of context sent to the model
_PAIR_MAX_TEXT = 4000             # player message cap
_PAIR_MAX_TOKENS = 800            # roomy conversational reply

_PAIR_HANDOFF_RE = re.compile(r'^\s*HANDOFF:\s*(.+?)\s*$', re.MULTILINE)

_PAIR_SCHEMA = '''
CREATE TABLE IF NOT EXISTS pair_threads (
    id        TEXT PRIMARY KEY,
    title     TEXT NOT NULL DEFAULT '',
    agent_id  TEXT NOT NULL,
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pair_messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL REFERENCES pair_threads(id),
    role      TEXT NOT NULL CHECK (role IN ('player', 'agent', 'note')),
    text      TEXT NOT NULL,
    handoff_request TEXT,
    handoff_mailed INTEGER NOT NULL DEFAULT 0,
    ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pair_messages_thread
    ON pair_messages(thread_id, ts);
'''


@contextlib.contextmanager
def _pair_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_PAIR_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _pair_key_ok(request):
    # Player session OR the dedicated operator key. Never an arbitrary agent
    # key: pairing is an operator surface, and an agent key would let any
    # roster agent open a thread claiming to be the player.
    if verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return 'player'
    presented = request.headers.get('X-Pair-Key')
    if not presented:
        return None
    expected = _load_env().get('PAIR_KEY')
    if not expected:
        return None   # fail-closed when unconfigured
    import secrets as _secrets
    return 'player' if _secrets.compare_digest(presented, str(expected)) else None


def _pair_agent_key():
    # Resolved server-side every call: the player's key material never
    # determines the agent's identity or reach.
    return get_or_create_agent_key(_PAIR_AGENT_ID)


def _pair_chat_model():
    # The low band's RESOLVED pick carries the free-pool overlay
    # ('@freepool:low:...'), and a circuit-broken free endpoint surfaces as a
    # model error mid-conversation. A live pairing session should not ride
    # the free pool: read the band's PAID slug straight from model_tiers and
    # fall back to mid, then to the literal band name (which /api/chat
    # resolves).
    try:
        with _pair_db() as conn:
            row = conn.execute(
                "SELECT slug FROM model_tiers WHERE band = 'low'").fetchone()
        if row and row['slug'] and not str(row['slug']).startswith('@'):
            return row['slug']
    except Exception:
        pass
    try:
        with _pair_db() as conn:
            row = conn.execute(
                "SELECT slug FROM model_tiers WHERE band = 'mid'").fetchone()
        if row and row['slug'] and not str(row['slug']).startswith('@'):
            return row['slug']
    except Exception:
        pass
    return 'low'


def _pair_model_reply(thread_id, player_text):
    # Loopback to /api/chat as the pair agent: same endpoint every other
    # agent uses, same attribution, no separate key handling.
    with _pair_db() as conn:
        rows = conn.execute(
            'SELECT role, text FROM pair_messages WHERE thread_id = ? '
            'ORDER BY ts DESC, id DESC LIMIT ?',
            (thread_id, _PAIR_HISTORY_TURNS)).fetchall()
    history = [{'role': 'user' if r['role'] == 'player' else 'assistant',
                'content': r['text']}
               for r in reversed(rows)]
    messages = [{'role': 'system', 'content': _pair_system_prompt()}] + history
    messages.append({'role': 'user', 'content': player_text})
    result = _http_json(
        'POST', SELF_BASE_URL, '/api/chat',
        {'model': _pair_chat_model(), 'messages': messages,
         'max_tokens': _PAIR_MAX_TOKENS, 'agentId': _PAIR_AGENT_ID,
         'service': 'pairing'},
        _pair_agent_key(), timeout=120)
    if not isinstance(result, dict) or result.get('error') or not result.get('reply'):
        return None, (result.get('error') if isinstance(result, dict) else result)
    return result['reply'].strip(), None


def _pair_system_prompt():
    return (
        'You are Theo in a live pairing session with the user.\n'
        'Ground rules:\n'
        '- Converse naturally: clarify requirements, discuss design, ask '
        'questions.\n'
        '- You do NOT file cards; you pass work items on.\n'
        '- When the conversation produces a concrete work request (a feature, '
        'a bug, a refactor, anything that should enter the task pipeline), '
        'you pass it on by ending your reply with exactly:\n'
        'HANDOFF: <one clear sentence describing the work request>\n'
        '- Keep HANDOFF to a single line; anything after it is ignored.\n'
        '- Do not promise the work is done or will be done by you; say it has '
        'been passed on.')


def _pair_send_handoff(request_text):
    # Theo's key forwards the mail so the send is attributed to the agent,
    # not to the player. Ruth owns filing; the mail text is the durable record.
    try:
        from core.style import sanitize_text
        text = sanitize_text(
            f"Pairing handoff from the user's session: {request_text}"
        ).strip()[:2000]
    except Exception:
        text = f"Pairing handoff from the user's session: {request_text}".strip()[:2000]
    result = _http_json('POST', SELF_BASE_URL, '/api/mail/send',
                        {'toId': _PAIR_COORDINATOR_ID, 'text': text},
                        _pair_agent_key(), timeout=30)
    ok = isinstance(result, dict) and result.get('ok')
    err = None if ok else ((result.get('error') if isinstance(result, dict)
                            else None) or str(result))
    return ok, err


@app.post('/api/pair/message')
async def api_pair_message(request: Request):
    actor = _pair_key_ok(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    text = body.get('text')
    if not isinstance(text, str) or not text.strip():
        return JSONResponse({'error': 'text is required'}, status_code=400)
    text = text.strip()[:_PAIR_MAX_TEXT]
    thread_id = (body.get('threadId') or '').strip()

    now = time.time()
    with _pair_db() as conn:
        if thread_id:
            row = conn.execute('SELECT id FROM pair_threads WHERE id = ?',
                               (thread_id,)).fetchone()
            if not row:
                return JSONResponse({'error': 'thread not found'}, status_code=404)
        else:
            thread_id = 'pair-%d' % int(now * 1000)
            title = text[:60]
            conn.execute(
                'INSERT INTO pair_threads (id, title, agent_id, created_ts, '
                'updated_ts) VALUES (?, ?, ?, ?, ?)',
                (thread_id, title, _PAIR_AGENT_ID, now, now))
        conn.execute(
            'INSERT INTO pair_messages (thread_id, role, text, ts) '
            'VALUES (?, ?, ?, ?)', (thread_id, 'player', text, now))
        conn.execute('UPDATE pair_threads SET updated_ts = ? WHERE id = ?',
                     (now, thread_id))

    reply, model_err = await asyncio.to_thread(
        _pair_model_reply, thread_id, text)
    if not reply:
        return JSONResponse({
            'ok': True, 'threadId': thread_id,
            'reply': '(model error -- your message is saved; retry.)',
            'modelError': str(model_err)[:300] if model_err else None,
        }, status_code=200)

    handoff = None
    m = _PAIR_HANDOFF_RE.search(reply)
    if m:
        request_text = m.group(1).strip()[:1000]
        mailed, mail_err = await asyncio.to_thread(
            _pair_send_handoff, request_text)
        handoff = {'to': _PAIR_COORDINATOR_ID, 'request': request_text,
                   'mailed': mailed}
        if not mailed:
            handoff['error'] = mail_err
        reply = _PAIR_HANDOFF_RE.sub('', reply).rstrip()

    now = time.time()
    with _pair_db() as conn:
        conn.execute(
            'INSERT INTO pair_messages (thread_id, role, text, '
            'handoff_request, handoff_mailed, ts) VALUES (?, ?, ?, ?, ?, ?)',
            (thread_id, 'agent', reply,
             (handoff or {}).get('request'),
             1 if handoff and handoff.get('mailed') else 0, now))
        conn.execute('UPDATE pair_threads SET updated_ts = ? WHERE id = ?',
                     (now, thread_id))

    return JSONResponse({'ok': True, 'threadId': thread_id,
                         'reply': reply, 'handoff': handoff})


@app.get('/api/pair/history')
async def api_pair_history(request: Request):
    if not _pair_key_ok(request):
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(str(request.url)).query)
    thread_id = (qs.get('threadId') or [''])[0].strip()
    if not thread_id:
        return JSONResponse({'error': 'threadId is required'}, status_code=400)
    with _pair_db() as conn:
        thread = conn.execute('SELECT * FROM pair_threads WHERE id = ?',
                              (thread_id,)).fetchone()
        if not thread:
            return JSONResponse({'error': 'thread not found'}, status_code=404)
        rows = conn.execute(
            'SELECT role, text, handoff_request, handoff_mailed, ts '
            'FROM pair_messages WHERE thread_id = ? ORDER BY ts, id',
            (thread_id,)).fetchall()
    return JSONResponse({
        'ok': True, 'threadId': thread_id,
        'title': thread['title'], 'agentId': thread['agent_id'],
        'messages': [{'role': r['role'], 'text': r['text'],
                      'handoffRequest': r['handoff_request'],
                      'handoffMailed': bool(r['handoff_mailed']),
                      'ts': r['ts']} for r in rows]})


@app.get('/api/pair/threads')
async def api_pair_threads(request: Request):
    if not _pair_key_ok(request):
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    with _pair_db() as conn:
        rows = conn.execute(
            'SELECT id, title, agent_id, created_ts, updated_ts '
            'FROM pair_threads ORDER BY updated_ts DESC LIMIT 50').fetchall()
    return JSONResponse({'ok': True, 'threads': [dict(r) for r in rows]})
