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
    '{"memories": [{"type": "preference|fact|decision|unresolved_question", '
    '"content": "...", "confidence": 0.0-1.0, "source": "user_stated|agent_inferred"}]}. '
    'Types: "preference" = a standing user preference or communication style; '
    '"fact" = a stable fact about the user or project that would still be true later; '
    '"decision" = a decision made or agreed in this conversation; '
    '"unresolved_question" = a question left open that the user may follow up on. '
    '"confidence" is how sure you are the memory is true and stable (1.0 = certain). '
    '"source" is "user_stated" when the user said it directly, else "agent_inferred". '
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

# Write-side credential redaction for persisted memory (ported from the magi
# framework's memory_write_filter.py). A player texting a password, API key,
# token, or connection string must never have it distilled into LTM and
# re-injected into a later ask -- REDACT instead of block, so the memory keeps
# its context ("user asked about a password reset") while the secret value is
# scrubbed. Also applied to the rolling STM summary so pruned turns don't bake
# a secret into the recap.
_CREDENTIAL_PATTERNS = (
    ('PASSWORD_STATEMENT', re.compile(
        r"\b(my\s+)?password\s*(is|was|:)\s*[\"']?[\w@#$%^&*!]+[\"']?", re.IGNORECASE)),
    ('API_KEY', re.compile(
        r"\b(api[\s_-]?key|apikey|api[\s_-]?token)\s*(:|=|is)\s+[\"']?[a-zA-Z0-9_\-]{16,}[\"']?",
        re.IGNORECASE)),
    ('BEARER_TOKEN', re.compile(r"\bbearer\s+[a-zA-Z0-9_\-\.]+", re.IGNORECASE)),
    ('AWS_KEY', re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{12,20}\b")),
    ('SECRET', re.compile(
        r"\b(secret|credential|private[_-]?key)\s*(:|=|is)\s*[\"']?[^\s\"']{8,}[\"']?",
        re.IGNORECASE)),
    ('CONN_STRING', re.compile(r"(password|pwd|passwd)\s*=\s*[^\s;]+", re.IGNORECASE)),
    ('JWT', re.compile(r"eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+")),
    ('GIT_TOKEN', re.compile(r"\b(ghp_|gho_|ghu_|ghs_|ghr_|glpat-)[a-zA-Z0-9_]{20,}\b")),
    ('DAPI_TOKEN', re.compile(r"\bdapi[a-f0-9]{32,}\b", re.IGNORECASE)),
)


def _redact_secrets(text):
    """Scrub credential values from text before it is persisted to memory.
    Every pattern match becomes [REDACTED:TYPE] so the context survives and
    the secret does not."""
    out = str(text or '')
    for name, pattern in _CREDENTIAL_PATTERNS:
        out = pattern.sub(f'[REDACTED:{name}]', out)
    return out

# Read-side trust for LTM recall (ported from the magi framework's
# memory_trust.py): every memory carries a confidence + source, and recall
# filters by minimum confidence, decays stale memories, and boosts memories
# the user stated directly. A false fact the player mentioned once in passing
# must not carry the same weight as something stable and repeated.
_TRUST_SOURCE_WEIGHTS = {
    'user_stated': 0.9,
    'agent_inferred': 0.6,
    'system_generated': 0.8,
    'unknown': 0.4,
}
_TRUST_SOURCE_BOOST = {
    'user_stated': 0.1,
    'system_generated': 0.05,
    'agent_inferred': 0.0,
    'unknown': -0.15,
}
_TRUST_DECAY_DAYS = 90  # beyond this age, memories lose half their trust


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
                last_consolidate_turn INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )''')
            # Migration: stm_sessions predates the consolidation cadence column.
            existing_s = {r[1] for r in conn.execute('PRAGMA table_info(stm_sessions)')}
            if 'last_consolidate_turn' not in existing_s:
                conn.execute(
                    'ALTER TABLE stm_sessions ADD COLUMN last_consolidate_turn '
                    'INTEGER NOT NULL DEFAULT 0')
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
                active INTEGER NOT NULL DEFAULT 1,
                confidence REAL NOT NULL DEFAULT 0.5,
                source_type TEXT NOT NULL DEFAULT 'unknown',
                fact_checked INTEGER NOT NULL DEFAULT 0
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_ltm_active ON ltm_memories(active)')
            # Migration: ltm_memories predates the trust columns (confidence /
            # source_type / fact_checked), so an existing DB needs the columns
            # added idempotently -- CREATE TABLE IF NOT EXISTS alone cannot.
            existing_l = {r[1] for r in conn.execute('PRAGMA table_info(ltm_memories)')}
            for col, ddl in (
                    ('confidence', 'REAL NOT NULL DEFAULT 0.5'),
                    ('source_type', "TEXT NOT NULL DEFAULT 'unknown'"),
                    ('fact_checked', 'INTEGER NOT NULL DEFAULT 0')):
                if col not in existing_l:
                    conn.execute(f'ALTER TABLE ltm_memories ADD COLUMN {col} {ddl}')
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
            merged = _redact_secrets(f"{summary}\n{recap}".strip()) if summary else recap
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
    recap = ' | '.join(parts) if parts else 'Context continues from earlier in this conversation.'
    return _redact_secrets(recap)


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
                (_redact_secrets(summary.strip())[:_SUMMARY_CAP_CHARS], time.time(), session_id),
            )
    except Exception as e:
        print(f'[memory] summary upgrade failed: {e}', flush=True)


def ltm_recall(question, limit=None):
    """The most relevant long-term memories for `question`, as a formatted
    string (or None). Relevance is keyword-overlap scored, then ranked by a
    trust factor -- confidence, source priority (user-stated > inferred), and
    staleness decay -- a cheap, dependency-free stand-in for magi's vector
    search + trust filter. Memories below the minimum confidence or older than
    the max age are not recalled at all (poisoning defense: an offhand false
    fact the user stated once does not outrank a stable, repeated one)."""
    import serve as _serve
    if not question or not _enabled():
        return None
    limit = int(limit if limit is not None else getattr(_serve, 'LTM_MAX_RECALL', 5) or 5)
    if limit <= 0:
        return None
    min_conf = float(getattr(_serve, 'LTM_MIN_CONFIDENCE', 0.3) or 0.0)
    max_age_days = int(getattr(_serve, 'LTM_MAX_AGE_DAYS', 180) or 0)
    qwords = _words(question)
    if not qwords:
        return None
    _ensure_tables()
    now = time.time()
    try:
        with _serve._db() as conn:
            rows = conn.execute(
                'SELECT mem_type, content, created_at, confidence, source_type FROM ltm_memories '
                'WHERE active = 1 ORDER BY created_at DESC LIMIT ?', (limit * 60,),
            ).fetchall()
    except Exception:
        return None
    scored = []
    for mem_type, content, created_at, confidence, source_type in rows:
        overlap = len(qwords & _words(content))
        if overlap < 1:
            continue
        confidence = max(0.0, min(1.0, float(confidence or 0.0)))
        if confidence < min_conf:
            continue
        age_days = max(0.0, (now - created_at) / 86400.0)
        if max_age_days > 0 and age_days > max_age_days:
            continue
        # Trust factor: source priority + staleness decay (half-life).
        trust = _TRUST_SOURCE_BOOST.get(source_type or 'unknown', -0.15) \
            - (age_days / _TRUST_DECAY_DAYS) * 0.2
        scored.append((overlap + trust, overlap, created_at, mem_type, content))
    scored.sort(key=lambda x: (-x[0], -x[2]))
    top = scored[:limit]
    if not top:
        return None
    return '\n'.join(f"- [{mt}] {c}" for _, _, _, mt, c in top)


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
        # Consolidation runs on its own (slower) cadence; check whether it is
        # due and fire it in a background thread too. Never blocks the ask.
        if ltm_consolidate_due(session_id):
            c = threading.Thread(target=_ltm_consolidate_worker, args=(session_id,), daemon=True)
            c.start()
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
    """Parse the extraction reply into [(type, content, confidence, source), ...].
    Fails closed to [] on anything that does not parse cleanly -- a bad reply
    never fabricates memory. Confidence defaults to the source's base weight;
    source defaults to 'agent_inferred' (the reply is LLM-distilled)."""
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
        if not content:
            continue
        source = str(it.get('source') or 'agent_inferred').strip().lower()
        if source not in _TRUST_SOURCE_WEIGHTS:
            source = 'agent_inferred'
        try:
            confidence = float(it.get('confidence'))
        except (TypeError, ValueError):
            confidence = _TRUST_SOURCE_WEIGHTS[source]
        confidence = max(0.0, min(1.0, confidence))
        out.append((mem_type, content, confidence, source))
    return out


def _insert_memories(session_id, memories):
    """Insert new LTM memories, deduplicated against the active set, and cap the
    session's store so an aging conversation cannot grow the table without
    bound. Duplicates are skipped silently -- a re-extracted fact must not
    accumulate. Content is redacted for secrets before it is stored (write-side
    credential filter), so a password or API key a player texts can never be
    re-injected into a later ask."""
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
            for mem_type, content, confidence, source in memories:
                content = _redact_secrets(content).strip()
                if not content or _normalize(content) in seen:
                    continue
                seen.add(_normalize(content))
                conn.execute(
                    'INSERT INTO ltm_memories (session_id, mem_type, content, created_at, active, '
                    'confidence, source_type, fact_checked) VALUES (?, ?, ?, ?, 1, ?, ?, 0)',
                    (session_id, mem_type, content, now, confidence, source),
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


_LTM_CONSOLIDATE_SYSTEM = (
    'You are merging redundant long-term memories. Below is a list of memories '
    'about the same user/project that may describe the same underlying fact in '
    'different words. If they are near-duplicates of ONE underlying fact, merge '
    'them into a single richer memory. If they are genuinely different facts, '
    'do not merge. Respond with ONLY valid JSON: '
    '{"merged": {"type": "preference|fact|decision|unresolved_question", "content": "...", '
    '"confidence": 0.0-1.0}} for a merge, or {"merged": false} to keep them separate. '
    'The merged content should keep the durable, still-true details and drop '
    'contradicted or one-off specifics.'
)

_LTM_CONSOLIDATE_ALPHA = 0.4  # min Jaccard similarity for a merge candidate pair


def _memory_words(content):
    return _words(content)


def _find_consolidation_clusters(memories, max_clusters=4):
    """Group memories into near-duplicate candidate clusters by keyword overlap
    (Jaccard on significant words). Returns a list of lists of
    (id, mem_type, content) -- each cluster has >= 2 members -- capped to the
    max clusters per run so consolidation spend stays bounded."""
    clusters = []
    n = len(memories)
    word_sets = [_memory_words(c) for _, _, c in memories]
    consumed = set()
    for i in range(n):
        if i in consumed:
            continue
        group = [i]
        for j in range(i + 1, n):
            if j in consumed:
                continue
            a, b = word_sets[i], word_sets[j]
            if not a or not b:
                continue
            inter = len(a & b)
            union = len(a | b)
            if union and inter >= 2 and inter / union >= _LTM_CONSOLIDATE_ALPHA:
                group.append(j)
        if len(group) >= 2:
            consumed.update(group)
            clusters.append([memories[k] for k in group])
            if len(clusters) >= max_clusters:
                break
    return clusters


def ltm_consolidate_due(session_id):
    """Cheap pre-check: is the session past its consolidation cadence? Used to
    decide whether to spawn a consolidation worker; the worker re-checks the
    cadence under the DB before merging anything."""
    import serve as _serve
    if not session_id or not _enabled():
        return False
    if not getattr(_serve, 'LTM_CONSOLIDATE_ENABLED', True):
        return False
    every = int(getattr(_serve, 'LTM_CONSOLIDATE_EVERY_TURNS', 12) or 0)
    if every <= 0:
        return False
    _ensure_tables()
    try:
        with _serve._db() as conn:
            row = conn.execute(
                'SELECT last_consolidate_turn, turn_count FROM stm_sessions WHERE session_id = ?',
                (session_id,),
            ).fetchone()
        return bool(row and row[1] - row[0] >= every)
    except Exception:
        return False


def _ltm_consolidate_worker(session_id):
    """Best-effort background consolidation. Fails closed to a no-op; a merge
    error never blocks or corrupts an ask."""
    try:
        ltm_consolidate(session_id)
    except Exception as e:
        print(f'[memory] ltm consolidate worker failed: {e}', flush=True)


def ltm_consolidate(session_id):
    """Batch consolidation (ported from the magi framework's
    memory_consolidation.py): scan the session's active memories for
    near-duplicates and merge each cluster into one richer memory via a
    bounded LLM call. Runs on its own cadence (LTM_CONSOLIDATE_EVERY_TURNS),
    never blocks an ask, and fails closed -- a bad merge reply keeps the
    cluster untouched. Merged memories inherit the cluster's max confidence
    and a 'system_generated' source."""
    import serve as _serve
    if not session_id or not _enabled():
        return 0
    if not getattr(_serve, 'LTM_CONSOLIDATE_ENABLED', True):
        return 0
    every = int(getattr(_serve, 'LTM_CONSOLIDATE_EVERY_TURNS', 12) or 0)
    if every <= 0:
        return 0
    max_clusters = int(getattr(_serve, 'LTM_CONSOLIDATE_MAX_CLUSTERS', 4) or 4)
    _ensure_tables()
    try:
        with _serve._db() as conn:
            row = conn.execute(
                'SELECT last_consolidate_turn, turn_count FROM stm_sessions WHERE session_id = ?',
                (session_id,),
            ).fetchone()
            if not row:
                return 0
            last_consolidate, turn_count = row
            if turn_count - last_consolidate < every:
                return 0
            memories = conn.execute(
                'SELECT id, mem_type, content FROM ltm_memories '
                'WHERE session_id = ? AND active = 1 ORDER BY created_at DESC, id DESC LIMIT 100',
                (session_id,),
            ).fetchall()
            conn.execute(
                'UPDATE stm_sessions SET last_consolidate_turn = ?, updated_at = ? '
                'WHERE session_id = ?', (turn_count, time.time(), session_id))
    except Exception as e:
        print(f'[memory] consolidate scan failed: {e}', flush=True)
        return 0
    clusters = _find_consolidation_clusters(memories, max_clusters=max_clusters)
    merged_count = 0
    for cluster in clusters:
        try:
            ids = [r[0] for r in cluster]
            snippet = '\n'.join(f'- [{mt}] {c}' for _, mt, c in cluster)
            reply = _chat_call(_LTM_CONSOLIDATE_SYSTEM, snippet, max_tokens=300)
            if not reply:
                continue
            cleaned = re.sub(r'^```json\s*|^```\s*|```\s*$', '', reply.strip())
            data = json.loads(cleaned)
            if not isinstance(data, dict) or data.get('merged') in (None, False):
                continue
            m = data['merged']
            if not isinstance(m, dict):
                continue
            mem_type = str(m.get('type') or 'fact').strip().lower()
            if mem_type not in LTM_TYPES:
                mem_type = 'fact'
            content = str(m.get('content') or '').strip()
            if not content:
                continue
            try:
                confidence = float(m.get('confidence'))
            except (TypeError, ValueError):
                confidence = _TRUST_SOURCE_WEIGHTS['system_generated']
            confidence = max(0.0, min(1.0, confidence))
            _insert_memories(session_id, [(mem_type, content, confidence, 'system_generated')])
            with _serve._db() as conn:
                conn.execute(
                    'UPDATE ltm_memories SET active = 0 WHERE id IN (%s)'  # nosec B608 -- placeholder count only, values are bound parameters
                    % ','.join('?' * len(ids)), tuple(ids))
            merged_count += 1
        except Exception as e:
            print(f'[memory] consolidate merge failed: {e}', flush=True)
    if merged_count:
        print(f'[memory] consolidated {merged_count} cluster(s) for session {session_id}', flush=True)
    return merged_count


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