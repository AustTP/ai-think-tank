"""STM + LTM for the player's ongoing conversation with the think tank admin.

The Telegram bridge used to answer every message with a stateless prompt ("a
fresh question"): a follow-up like "and the second one?" had no referent, so
the admin failed to answer. This module ports the memory architecture from the
magi framework wheel (centene_agents/agents/memory) -- the short-term store of
the conversation plus a long-term store of durable facts extracted from it --
onto the think tank's own SQLite DB.

STM (short-term memory): each (user question, assistant reply) is stored per
session_id (the Telegram chat id). On the next ask the recent turns and a
rolling summary are injected ahead of the current question, so a follow-up is
answered in the context of what came before. When a session exceeds the turn
or token budget, the oldest turns are pruned and folded into the rolling
summary -- a rule-based recap is written immediately (guaranteed), then an LLM
upgrade is attempted in a background thread (same guaranteed-then-upgraded
discipline as magi's _enforce_session_limits).

LTM (long-term memory): typed memories (preference / fact / decision /
unresolved_question) are distilled from the conversation by an LLM at a
bounded cadence and stored in ltm_memories. On each ask the most relevant
memories (keyword-overlap scored) are injected as long-term context -- so the
admin remembers the player's standing preferences and earlier decisions across
turns, and even across separate conversation threads. LTM is global to the
player (never deleted by session expiry) -- that is the point of long-term.

All serve.py state is reached through `_serve.X` at call time (the lazy pattern
the other extracted modules use), so the env knobs resolve fresh on every call
and a test patching `serve.DB_PATH` isolates these tables to a temp database.
"""

import json
import re
import threading
import time

# The typed memories LTM distills conversations into.
LTM_TYPES = frozenset({'preference', 'fact', 'decision', 'unresolved_question'})

_LTM_EXTRACT_SYSTEM = (
    'Extract durable, factual memories about the user and the project from this '
    'conversation. Respond with ONLY valid JSON, no other text and no markdown '
    'fences, in exactly this shape: '
    '{"memories": [{"type": "preference|fact|decision|unresolved_question", "content": "..."}]}. '
    'Types: "preference" = a standing user preference or communication style; '
    '"fact" = a stable fact about the user or project that would still be true later; '
    '"decision" = a decision made or agreed in this conversation; '
    '"unresolved_question" = a question left open that the user may follow up on. '
    'Only include items useful in a LATER conversation. Skip small talk, one-off '
    'details, and anything already obvious. Return {"memories": []} if nothing durable.'
)

_STOPWORDS = frozenset({
    'the', 'a', 'an', 'and', 'or', 'of', 'to', 'for', 'with', 'on', 'in', 'at',
    'is', 'are', 'was', 'were', 'be', 'been', 'being', 'it', 'its', 'that',
    'this', 'these', 'those', 'i', 'you', 'we', 'they', 'he', 'she', 'me', 'my',
    'your', 'our', 'their', 'his', 'her', 'what', 'which', 'who', 'whom', 'how',
    'when', 'where', 'why', 'do', 'does', 'did', 'will', 'would', 'can', 'could',
    'should', 'shall', 'not', 'no', 'so', 'but', 'as', 'if', 'then', 'than',
    'about', 'from', 'by', 'up', 'down', 'out', 'over', 'under', 'again', 'more',
    'most', 'some', 'any', 'all', 'each', 'both', 'too', 'very', 'just', 'like',
    'there', 'here', 'have', 'has', 'had', 'am', 'into', 'onto', 'off', 'also',
})

_SUMMARY_CAP_CHARS = 2000


def _ensure_tables():
    """Create the memory tables if they do not exist. Idempotent, and safe
    against the real think_tank.db -- same CREATE TABLE IF NOT EXISTS discipline
    as the other extracted modules."""
    import serve as _serve
    try:
        with _serve._db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS stm_sessions (
                session_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                turn_count INTEGER NOT NULL DEFAULT 0,
                token_count INTEGER NOT NULL DEFAULT 0,
                last_extract_turn INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS stm_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                turn INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                token_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_stm_messages_session_turn '
                         'ON stm_messages(session_id, turn)')
            conn.execute('''CREATE TABLE IF NOT EXISTS ltm_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                mem_type TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_ltm_active ON ltm_memories(active)')
    except Exception:
        pass


def _enabled():
    import serve as _serve
    return bool(getattr(_serve, 'MEMORY_ENABLED', True))


def _token_count(text):
    # Cheap deterministic estimate (magi's crude fallback): ~1.3 tokens/word.
    return max(1, int(len((text or '').split()) * 1.3))


def _words(text):
    tokens = re.findall(r"[a-z0-9']+", (text or '').lower())
    return {w for w in tokens if len(w) > 2 and w not in _STOPWORDS}


def _normalize(text):
    return ' '.join(str(text or '').lower().split())


def _session_row(conn, session_id):
    conn.execute(
        'INSERT OR IGNORE INTO stm_sessions (session_id, summary, turn_count, '
        'token_count, last_extract_turn, created_at, updated_at) VALUES (?, ?, 0, 0, 0, ?, ?)',
        (session_id, '', time.time(), time.time()),
    )
    return conn.execute(
        'SELECT summary, turn_count, token_count, last_extract_turn FROM stm_sessions WHERE session_id = ?',
        (session_id,),
    ).fetchone()


def stm_history(session_id):
    """(summary, history) for a session: the rolling summary string and the
    stored turns (oldest first) capped at the STM_MAX_TURNS most recent. Empty
    strings/lists when the session has nothing stored or memory is disabled."""
    import serve as _serve
    if not session_id or not _enabled():
        return '', []
    max_turns = max(1, int(getattr(_serve, 'STM_MAX_TURNS', 40) or 40))
    _ensure_tables()
    try:
        with _serve._db() as conn:
            row = _session_row(conn, session_id)
            summary = row[0] or ''
            rows = conn.execute(
                'SELECT role, content FROM stm_messages '
                'WHERE session_id = ? ORDER BY turn ASC', (session_id,),
            ).fetchall()
        history = [{'role': r[0], 'content': r[1]} for r in rows][-max_turns:]
        return summary, history
    except Exception:
        return '', []


def stm_append(session_id, role, content):
    """Store one conversation turn for a session and enforce the turn/token
    budget (pruning the oldest turns into the rolling summary when exceeded).
    Best-effort: a memory failure must never break an ask."""
    import serve as _serve
    if not session_id or not _enabled():
        return None
    _ensure_tables()
    try:
        tokens = _token_count(content)
        now = time.time()
        with _serve._db() as conn:
            row = _session_row(conn, session_id)
            turn = row[1] + 1
            conn.execute(
                'INSERT INTO stm_messages (session_id, turn, role, content, token_count, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (session_id, turn, role, content, tokens, now),
            )
            conn.execute(
                'UPDATE stm_sessions SET turn_count = turn_count + 1, '
                'token_count = token_count + ?, updated_at = ? WHERE session_id = ?',
                (tokens, now, session_id),
            )
        _enforce_limits(session_id)
    except Exception as e:
        print(f'[memory] stm store failed: {e}', flush=True)
    return None


def _enforce_limits(session_id):
    """Prune the oldest stored turns once the session exceeds its turn or token
    budget, folding the pruned text into the rolling summary. The rule-based
    recap is written immediately; a background thread may upgrade it with an LLM
    summary later (best-effort, never blocking the ask)."""
    import serve as _serve
    max_turns = max(1, int(getattr(_serve, 'STM_MAX_TURNS', 40) or 40))
    max_tokens = max(1, int(getattr(_serve, 'STM_MAX_TOKENS', 8000) or 8000))
    try:
        with _serve._db() as conn:
            row = _session_row(conn, session_id)
            turn_count, token_count, summary = row[1], row[2], row[0] or ''
            turns_exceeded = turn_count > max_turns
            tokens_exceeded = token_count > max_tokens
            if not turns_exceeded and not tokens_exceeded:
                return
            rows = conn.execute(
                'SELECT id, turn, role, content, token_count FROM stm_messages '
                'WHERE session_id = ? ORDER BY turn ASC', (session_id,),
            ).fetchall()
            if not rows:
                return
            prune = max(0, turn_count - max_turns)
            if tokens_exceeded:
                excess = token_count - max_tokens
                acc, cnt = 0, 0
                for r in rows:
                    acc += r[4]
                    cnt += 1
                    if acc >= excess:
                        break
                prune = max(prune, cnt)
            if prune <= 0:
                return
            pruned = rows[:prune]
            pruned_tokens = sum(r[4] for r in pruned)
            recap = _rule_based_summary(
                '\n'.join(f"{r[2]}: {r[3]}" for r in pruned))
            merged = f"{summary}\n{recap}".strip() if summary else recap
            if len(merged) > _SUMMARY_CAP_CHARS:
                merged = merged[-_SUMMARY_CAP_CHARS:]
            conn.execute(
                'UPDATE stm_sessions SET summary = ?, turn_count = turn_count - ?, '
                'token_count = token_count - ?, updated_at = ? WHERE session_id = ?',
                (merged, prune, pruned_tokens, time.time(), session_id),
            )
            conn.execute(
                'DELETE FROM stm_messages WHERE id IN (%s)' % ','.join('?' * len(pruned)),  # nosec B608 -- placeholder count only, values are bound parameters
                tuple(r[0] for r in pruned),
            )
    except Exception as e:
        print(f'[memory] stm prune failed: {e}', flush=True)
        return
    _spawn_summary_upgrade(session_id)


def _rule_based_summary(text):
    """Cheap deterministic recap of pruned turns (magi's rule-based fallback):
    the first user message, any decision lines, and the last assistant reply.
    Guaranteed to return something, so context is never lost to a failing LLM."""
    lines = (text or '').strip().split('\n')
    if not lines:
        return 'Context continues from earlier in this conversation.'
    first_user = None
    last_assistant = None
    decision_parts = []
    for line in lines:
        if line.lower().startswith('user:') and first_user is None:
            first_user = line[5:].strip()[:200]
        elif line.lower().startswith('assistant:'):
            last_assistant = line[10:].strip()[:200]
        lower = line.lower()
        if any(k in lower for k in ('decided', 'agreed', 'confirmed', 'will do',
                                    'plan is', 'solution', 'conclusion', 'answer',
                                    'result', 'prefer', 'preference')):
            decision_parts.append(line.strip()[:150])
    parts = []
    if first_user:
        parts.append(f'User asked: {first_user}')
    if decision_parts:
        parts.append('Key points: ' + '; '.join(decision_parts[:3]))
    if last_assistant:
        parts.append(f'Last response: {last_assistant}')
    return ' | '.join(parts) if parts else 'Context continues from earlier in this conversation.'


def _spawn_summary_upgrade(session_id):
    """Fire-and-forget LLM upgrade of a session's rolling summary. The rule-based
    recap is already persisted; this only makes it better, so a failure is a
    silent no-op."""
    try:
        t = threading.Thread(target=_summary_upgrade_worker, args=(session_id,), daemon=True)
        t.start()
    except Exception:
        pass


def _summary_upgrade_worker(session_id):
    try:
        import serve as _serve
        _ensure_tables()
        with _serve._db() as conn:
            row = conn.execute(
                'SELECT summary FROM stm_sessions WHERE session_id = ?', (session_id,),
            ).fetchone()
        if not row or not row[0]:
            return
        summary = _chat_call(
            'Condense the following recap of an earlier conversation into one '
            'concise paragraph (3-5 sentences). Focus on the user intent, any '
            'decisions, and what was resolved. Keep the user\'s phrasing for '
            'preferences.',
            row[0],
            max_tokens=300,
        )
        if not summary:
            return
        with _serve._db() as conn:
            conn.execute(
                'UPDATE stm_sessions SET summary = ?, updated_at = ? WHERE session_id = ?',
                (summary.strip()[:_SUMMARY_CAP_CHARS], time.time(), session_id),
            )
    except Exception as e:
        print(f'[memory] summary upgrade failed: {e}', flush=True)


def ltm_recall(question, limit=None):
    """The most relevant long-term memories for `question`, as a formatted
    string (or None). Relevance is keyword-overlap scored with recency as the
    tiebreak -- a cheap, dependency-free stand-in for magi's vector search."""
    import serve as _serve
    if not question or not _enabled():
        return None
    limit = int(limit if limit is not None else getattr(_serve, 'LTM_MAX_RECALL', 5) or 5)
    if limit <= 0:
        return None
    qwords = _words(question)
    if not qwords:
        return None
    _ensure_tables()
    try:
        with _serve._db() as conn:
            rows = conn.execute(
                'SELECT mem_type, content, created_at FROM ltm_memories '
                'WHERE active = 1 ORDER BY created_at DESC LIMIT ?', (limit * 30,),
            ).fetchall()
    except Exception:
        return None
    scored = []
    for mem_type, content, created_at in rows:
        overlap = len(qwords & _words(content))
        if overlap >= 1:
            scored.append((overlap, created_at, mem_type, content))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    top = scored[:limit]
    if not top:
        return None
    return '\n'.join(f"- [{mt}] {c}" for _, _, mt, c in top)


def ltm_maybe_extract(session_id):
    """Start a background LTM extraction when the session has accumulated enough
    new turns since the last one. Best-effort and fire-and-forget -- extraction
    is bonus context, never a gate on an ask."""
    import serve as _serve
    if not session_id or not _enabled():
        return None
    every = int(getattr(_serve, 'LTM_EXTRACT_EVERY_TURNS', 6) or 0)
    if every <= 0:
        return None
    _ensure_tables()
    try:
        with _serve._db() as conn:
            row = _session_row(conn, session_id)
            last_extract, turn_count = row[3], row[1]
        if turn_count - last_extract < every:
            return None
        t = threading.Thread(target=_ltm_extract_worker, args=(session_id,), daemon=True)
        t.start()
    except Exception as e:
        print(f'[memory] ltm extract launch failed: {e}', flush=True)
    return None


def _ltm_extract_worker(session_id):
    """Distill new turns into typed LTM memories. The cooldown is re-checked
    inside the worker so two racing triggers cannot double-extract."""
    try:
        import serve as _serve
        _ensure_tables()
        with _serve._db() as conn:
            row = _session_row(conn, session_id)
            last_extract, turn_count = row[3], row[1]
            if turn_count <= last_extract:
                return
            rows = conn.execute(
                'SELECT role, content FROM stm_messages '
                'WHERE session_id = ? AND turn > ? ORDER BY turn ASC',
                (session_id, last_extract),
            ).fetchall()
        if not rows:
            with _serve._db() as conn:
                conn.execute(
                    'UPDATE stm_sessions SET last_extract_turn = ?, updated_at = ? '
                    'WHERE session_id = ?', (turn_count, time.time(), session_id))
            return
        text = '\n'.join(f'{r[0]}: {r[1]}' for r in rows)
        reply = _chat_call(_LTM_EXTRACT_SYSTEM, text, max_tokens=500)
        memories = _parse_memories(reply) if reply else []
        if memories:
            _insert_memories(session_id, memories)
        with _serve._db() as conn:
            conn.execute(
                'UPDATE stm_sessions SET last_extract_turn = ?, updated_at = ? '
                'WHERE session_id = ?', (turn_count, time.time(), session_id))
    except Exception as e:
        print(f'[memory] ltm extract failed: {e}', flush=True)


def _parse_memories(reply):
    """Parse the extraction reply into [(type, content), ...]. Fails closed to
    [] on anything that does not parse cleanly -- a bad reply never fabricates
    memory."""
    if not reply:
        return []
    cleaned = re.sub(r'^```json\s*|^```\s*|```\s*$', '', str(reply).strip())
    try:
        data = json.loads(cleaned)
    except Exception:
        return []
    if isinstance(data, dict):
        items = data.get('memories') or []
    elif isinstance(data, list):
        items = data
    else:
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        mem_type = str(it.get('type') or 'fact').strip().lower()
        if mem_type not in LTM_TYPES:
            mem_type = 'fact'
        content = str(it.get('content') or '').strip()
        if content:
            out.append((mem_type, content))
    return out


def _insert_memories(session_id, memories):
    """Insert new LTM memories, deduplicated against the active set, and cap the
    session's store so an aging conversation cannot grow the table without
    bound. Duplicates are skipped silently -- a re-extracted fact must not
    accumulate."""
    import serve as _serve
    cap = int(getattr(_serve, 'LTM_MAX_PER_SESSION', 200) or 200)
    try:
        now = time.time()
        with _serve._db() as conn:
            existing = conn.execute(
                'SELECT content FROM ltm_memories WHERE session_id = ? AND active = 1',
                (session_id,),
            ).fetchall()
            seen = {_normalize(e[0]) for e in existing}
            for mem_type, content in memories:
                if _normalize(content) in seen:
                    continue
                seen.add(_normalize(content))
                conn.execute(
                    'INSERT INTO ltm_memories (session_id, mem_type, content, created_at, active) '
                    'VALUES (?, ?, ?, ?, 1)',
                    (session_id, mem_type, content, now),
                )
            rows = conn.execute(
                'SELECT id FROM ltm_memories WHERE session_id = ? AND active = 1 '
                'ORDER BY created_at ASC, id ASC', (session_id,),
            ).fetchall()
            over = len(rows) - cap
            if over > 0:
                conn.execute(
                    'UPDATE ltm_memories SET active = 0 WHERE id IN (%s)'  # nosec B608 -- placeholder count only, values are bound parameters
                    % ','.join('?' * over),
                    tuple(r[0] for r in rows[:over]),
                )
    except Exception as e:
        print(f'[memory] ltm insert failed: {e}', flush=True)


def memory_context(session_id, question):
    """Everything an ask needs to answer a follow-up in context: the rolling
    summary, the recent turns, and the relevant long-term memories. Returns None
    when memory is disabled or the caller has no session (stateless ask)."""
    if not session_id or not _enabled():
        return None
    import serve as _serve
    summary, history = stm_history(session_id)
    ltm = ltm_recall(question, getattr(_serve, 'LTM_MAX_RECALL', 5))
    return {'summary': summary, 'history': history, 'ltm': ltm}


def cleanup_expired_sessions():
    """Delete STM sessions (and their messages) not touched within
    STM_TTL_HOURS. LTM is deliberately untouched -- it is long-term and global.
    Best-effort; runs on the log-prune cadence."""
    import serve as _serve
    if not _enabled():
        return 0
    ttl_hours = int(getattr(_serve, 'STM_TTL_HOURS', 168) or 168)
    if ttl_hours <= 0:
        return 0
    cutoff = time.time() - ttl_hours * 3600
    _ensure_tables()
    deleted = 0
    try:
        with _serve._db() as conn:
            stale = conn.execute(
                'SELECT session_id FROM stm_sessions WHERE updated_at < ?', (cutoff,),
            ).fetchall()
            if stale:
                ids = tuple(r[0] for r in stale)
                conn.execute(
                    'DELETE FROM stm_messages WHERE session_id IN (%s)'  # nosec B608 -- placeholder count only, values are bound parameters
                    % ','.join('?' * len(ids)), ids)
                cur = conn.execute(
                    'DELETE FROM stm_sessions WHERE session_id IN (%s)'  # nosec B608 -- placeholder count only, values are bound parameters
                    % ','.join('?' * len(ids)), ids)
                deleted = cur.rowcount
    except Exception as e:
        print(f'[memory] cleanup failed: {e}', flush=True)
    return deleted


def _chat_call(system_prompt, user_text, max_tokens=400):
    """One loopback /api/chat call as the admin (so spend accrues to the Bank
    and every model call goes through the choke point). Returns the reply text
    or None -- fails closed on any missing config, auth, or network failure."""
    try:
        import serve as _serve
        if not getattr(_serve, 'OPENROUTER_API_KEY', None):
            return None
        state = _serve.get_state_from_db()
        admin_id = _serve._admin_agent_id(state) if state else None
        if not admin_id:
            return None
        key = _serve.get_or_create_agent_key(admin_id)
        model = _serve._resolve_model_tier('Extract and condense conversation memory')
        if not model:
            return None
        result = _serve._http_json(
            'POST', _serve.SELF_BASE_URL, '/api/chat',
            {'model': model,
             'messages': [{'role': 'system', 'content': system_prompt},
                          {'role': 'user', 'content': user_text}],
             'max_tokens': max_tokens, 'agentId': admin_id,
             'service': '__memory__', 'plain': False},
            key, timeout=30)
        if isinstance(result, dict) and result.get('reply'):
            return result['reply']
    except Exception as e:
        print(f'[memory] chat call failed: {e}', flush=True)
    return None