

def external_write_once(kind, key, fn, ttl_s=86400, pending_s=180):
    """Exactly-once convention for a side-effectful external call.

    Returns (result, error, replayed). `fn` must return (result, error) and
    result must be JSON serializable. A completed write is reused for ttl_s.
    A concurrent duplicate sees an in-progress row and is refused rather than
    starting a second side effect; a stale in-progress row (crashed process)
    is taken over after pending_s.
    """
    now = time.time()
    try:
        # The ledger table is created in init_db(), but a copied/older DB may
        # predate it; make the helper self-sufficient.
        with _db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS external_writes (
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (kind, key)
            )''')
        with _db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                'SELECT result, created_at FROM external_writes WHERE kind=? AND key=?',
                (kind, key)).fetchone()
            if row:
                result_text, created_at = row
                if result_text and now - float(created_at or 0) < ttl_s:
                    return json.loads(result_text), None, True
                if not result_text and now - float(created_at or 0) < pending_s:
                    return None, 'duplicate request is already in progress', False
            conn.execute(
                'INSERT OR REPLACE INTO external_writes (kind, key, result, created_at) '
                'VALUES (?, ?, ?, ?)', (kind, key, '', now))
    except Exception as e:
        return None, f'idempotency ledger unavailable: {e}', False

    try:
        result, error = fn()
    except Exception as e:
        result, error = None, str(e)
    if error:
        try:
            with _db() as conn:
                conn.execute(
                    'DELETE FROM external_writes WHERE kind=? AND key=? AND result=\'\'',
                    (kind, key))
        except Exception:
            pass
        return None, error, False
    try:
        with _db() as conn:
            conn.execute(
                'UPDATE external_writes SET result=?, created_at=? WHERE kind=? AND key=?',
                (json.dumps(result, sort_keys=True), time.time(), kind, key))
    except Exception as e:
        return None, f'write completed but the idempotency ledger failed: {e}', False
    return result, None, False


def _tool_output_is_error(text):
    s = str(text or '').strip().lower()
    return s.startswith(('error', 'could not', 'failed', 'unable', 'invalid',
                         'policy', '__tool_error__', 'not found', 'requires '))


def idempotent_tool_call(agent_id, task_id, raw_execute, name, args, ttl_s=3600):
    """Exactly-once convention for one commit-tier tool call.

    Read-only and prepare tools pass straight through. A commit tool gets a
    stable request id: the caller's explicit `requestId` when present, else a
    digest of the conversation, tool, and arguments. A retry inside the TTL
    returns the original output instead of repeating the side effect. Error
    outputs are not cached, so a transient failure can be retried with the same
    call."""
    if tool_trust_tier(name) != 'commit' or not is_known_tool(name):
        # Read-only/prepare tools never need the ledger. An unknown name also
        # bypasses it so its real error propagates instead of being turned
        # into a cached tool string.
        return raw_execute(name, args)
    args = args or {}
    request_id = str(args.get('requestId') or '').strip()
    if not request_id:
        request_id = external_write_key(
            task_id or agent_id or 'tank', name,
            json.dumps(args, sort_keys=True, default=str))[:32]
    key = f'{task_id or agent_id or "tank"}:{name}:{request_id}'

    def run():
        output = raw_execute(name, args)
        if _tool_output_is_error(output):
            return None, output
        return output, None

    result, error, replayed = external_write_once(
        'tool:' + str(name), key, run, ttl_s=ttl_s)
    if error:
        return error
    if replayed:
        return f'{result}\n\n[replayed from requestId {request_id}; the side effect was not repeated]'
    return result


def set_collector_checkpoint(collector, cursor):
    """Persist the last processed cursor for one live feed. Best effort: a
    checkpoint failure must never stop ingestion."""
    if not collector:
        return False
    try:
        with _db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS collector_checkpoints (
                collector TEXT PRIMARY KEY,
                cursor TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
            conn.execute(
                'INSERT OR REPLACE INTO collector_checkpoints (collector, cursor, updated_at) '
                'VALUES (?, ?, ?)',
                (str(collector), json.dumps(cursor, sort_keys=True), time.time()))
        return True
    except Exception:
        return False


def get_collector_checkpoint(collector):
    if not collector:
        return None
    try:
        with _db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS collector_checkpoints (
                collector TEXT PRIMARY KEY,
                cursor TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
            row = conn.execute(
                'SELECT cursor, updated_at FROM collector_checkpoints WHERE collector=?',
                (str(collector),)).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        if isinstance(data, dict):
            data['checkpoint_at'] = row[1]
        return data
    except Exception:
        return None


def save_task_checkpoint(task_id, phase, payload):
    if not task_id:
        return False
    try:
        with _db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS task_checkpoints (
                task_id TEXT PRIMARY KEY,
                phase TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
            conn.execute(
                'INSERT OR REPLACE INTO task_checkpoints (task_id, phase, payload, updated_at) '
                'VALUES (?, ?, ?, ?)',
                (str(task_id), str(phase), json.dumps(payload, sort_keys=True), time.time()))
        return True
    except Exception:
        return False


def load_task_checkpoint(task_id, max_age_s=86400):
    if not task_id:
        return None
    try:
        with _db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS task_checkpoints (
                task_id TEXT PRIMARY KEY,
                phase TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
            row = conn.execute(
                'SELECT phase, payload, updated_at FROM task_checkpoints WHERE task_id=?',
                (str(task_id),)).fetchone()
        if not row:
            return None
        phase, payload_text, updated_at = row
        if max_age_s and time.time() - float(updated_at or 0) > max_age_s:
            return None
        data = json.loads(payload_text)
        if not isinstance(data, dict):
            return None
        data['phase'] = phase
        data['checkpoint_at'] = updated_at
        return data
    except Exception:
        return None


def clear_task_checkpoint(task_id):
    if not task_id:
        return False
    try:
        with _db() as conn:
            conn.execute('DELETE FROM task_checkpoints WHERE task_id=?', (str(task_id),))
        return True
    except Exception:
        return False


def init_db():
    # Replaces state.json (whole-think tank state) and the separate
    # browse_log.jsonl/execute_log.jsonl files (scattered, per-feature
    # logs) with one real database -- you want a single
    # SQLite instance for both activity and state, not files that only
    # cover whichever feature happened to add one.
    with _db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS kv_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            blob TEXT NOT NULL,
            updated_at REAL NOT NULL
        )''')
        # The Bank spend ledger lives in its OWN row/table, separate from the
        # whole-think tank blob: a model call's accrual must never read-modify-write
        # the entire kv_state blob (the sim owns that, and a stale read+write
#         there is the blob-clobber class that has already caused data loss). Spending is
        # accounted independently so the ledger can't race the sim's saves.
        conn.execute('''CREATE TABLE IF NOT EXISTS kv_spend (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            blob TEXT NOT NULL,
            updated_at REAL NOT NULL
        )''')
        # Durable per-call usage receipts. kv_spend is the read projection;
        # a receipt written before an interrupted projection is replayed on
        # the next process. Unpriced receipts stay unresolved and hold the
        # task budget by default.
        conn.execute('''CREATE TABLE IF NOT EXISTS usage_receipts (
            id TEXT PRIMARY KEY,
            service TEXT NOT NULL,
            village_id TEXT,
            agent_id TEXT,
            task_id TEXT,
            cost_usd TEXT NOT NULL,
            status TEXT NOT NULL,
            idempotency_key TEXT UNIQUE,
            applied INTEGER NOT NULL DEFAULT 0,
            resolved INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            applied_at REAL
        )''')
        conn.execute('''CREATE INDEX IF NOT EXISTS idx_usage_receipts_task
            ON usage_receipts(task_id, status, resolved)''')
        conn.execute('''CREATE INDEX IF NOT EXISTS idx_usage_receipts_applied
            ON usage_receipts(applied)''')
        # Page-request budget: the think tank has a MONTHLY allowance
        # of external page requests (browse_page fetches + search_web calls) --
        # a count-based quota, not a dollar cap. Mirrors kv_spend's own-table
        # independence so the accounting never depends on the whole-think tank blob.
        conn.execute('''CREATE TABLE IF NOT EXISTS kv_pagebudget (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            blob TEXT NOT NULL,
            updated_at REAL NOT NULL
        )''')
        # The Research Desk's claim-level evidence ledger and review-gated
        # coverage ledger (world/evidence.py). Like kv_spend, each lives in
        # its OWN row/table -- a research run recording claims must never
        # read-modify-write the whole kv_state blob. One single-row blob per
        # ledger (claim id -> record, source identity -> coverage record) is
        # enough: claims are low-volume (a handful per scheduled run) and the
        # whole-ledger read/write keeps the accounting atomic.
        conn.execute('''CREATE TABLE IF NOT EXISTS evidence_records (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            blob TEXT NOT NULL,
            updated_at REAL NOT NULL
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS coverage_ledger (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            blob TEXT NOT NULL,
            updated_at REAL NOT NULL
        )''')
        # Completed tasks live here, NOT in the whole-blob kv_state. The blob
        # had grown to multi-MB of `done` tasks (thousands of them) that were
        # json.dumps'd and json.loads'd on EVERY tick and every state write,
        # while only a handful of tasks were ever active. get_state_from_db
        # merges this table back into state['tasks'] so every existing reader
        # keeps its exact shape; save_state_to_db writes only the live (non-
        # done) tasks into the blob. Per-task rows (not one blob) so a save
        # upserts only the tasks that actually changed.
        conn.execute('''CREATE TABLE IF NOT EXISTS task_archive (
            id TEXT PRIMARY KEY,
            blob TEXT NOT NULL,
            updated_at REAL NOT NULL
        )''')
        # Idempotency ledger for external writes. A retry with the same
        # requestId returns the original result instead of starting a second
        # Apify run or creating a second calendar event.
        conn.execute('''CREATE TABLE IF NOT EXISTS external_writes (
            kind TEXT NOT NULL,
            key TEXT NOT NULL,
            result TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (kind, key)
        )''')
        # Durable cursor for a live feed collector. AISStream is live-only, so
        # this records the last ingested frame and the reconnect coverage gap;
        # it is a checkpoint, not a replay buffer.
        conn.execute('''CREATE TABLE IF NOT EXISTS collector_checkpoints (
            collector TEXT PRIMARY KEY,
            cursor TEXT NOT NULL,
            updated_at REAL NOT NULL
        )''')
        # Executor-level task checkpoint. The spike executor saves its plan at
        # the plan/execute boundary; a restart can reuse that plan instead of
        # paying for it again. Rows are cleared on successful close.
        conn.execute('''CREATE TABLE IF NOT EXISTS task_checkpoints (
            task_id TEXT PRIMARY KEY,
            phase TEXT NOT NULL,
            payload TEXT NOT NULL,
            updated_at REAL NOT NULL
        )''')
        # Measurement-only: a hash of every Jev decision REQUEST, so we can
        # see whether the same decision recurs often enough to be worth
        # caching before building a cache. Two keys per call -- 'q:' (model +
        # questions) and 'r:' (model + state + questions) -- so the report
        # shows both the optimistic (question-only) and exact (full-request)
        # repeat rates. Never read by any decision path; safe to drop.
        conn.execute('''CREATE TABLE IF NOT EXISTS decision_key_stats (
            key TEXT PRIMARY KEY,
            count INTEGER NOT NULL,
            first_ts REAL NOT NULL,
            last_ts REAL NOT NULL
        )''')
        # Cache of Jev decision RESPONSES keyed by the full request hash. The
        # measured exact-repeat rate justified it: identical (model, state,
        # questions) requests are common, and the answer to an identical
        # request is stable, so a repeat can be served from here with no
        # network call and no spend. TTL-bounded (DECISION_CACHE_TTL_S, 0
        # disables) so a decision never goes stale indefinitely.
        conn.execute('''CREATE TABLE IF NOT EXISTS decision_cache (
            key TEXT PRIMARY KEY,
            response TEXT NOT NULL,
            created_at REAL NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0
        )''')
        # Runtime-switchable settings: a small key/value table
        # for values that must change without a server restart -- currently
        # just the Jev decisions-model slug (see _jev_model). Deliberately
        # NOT auto-refreshed like model_tiers: a new decision model appearing
        # on OpenRouter is a deliberate operator decision, not something the
        # think tank should re-pick for itself on the tier-refresh cadence.
        conn.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS agent_keys (
            agent_id TEXT PRIMARY KEY,
            secret_key TEXT NOT NULL,
            created_at REAL NOT NULL
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT,
            action TEXT NOT NULL,
            details TEXT,
            authorized INTEGER,
            ts REAL NOT NULL,
            trace_id TEXT
        )''')
        # Audit trace linkage: trace_id ties a JEV decision
        # (decision_tape) to the resulting tool outcomes (action_log) in a
        # single queryable chain, so "what decision led to this action" is
        # explicit rather than relying on a loose (ts, agent_id) heuristic.
        try:
            conn.execute('ALTER TABLE action_log ADD COLUMN trace_id TEXT')
        except Exception:
            pass  # column already exists (idempotent)
        # index drug: per-agent lookups (peer review, firing
        # review, passport audit) scan the whole table otherwise -- with no
        # index action_log grew to 295MB/216k rows and every agent query was a
        # full b-tree scan + temp-sort, pinning a CPU core.
        conn.execute('CREATE INDEX IF NOT EXISTS idx_action_log_agent_ts ON action_log(agent_id, ts)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_action_log_agent ON action_log(agent_id)')
        # Action-kind reads (allowlist history, a few audits) filter by action
        # and time. Without this they full-scan a 67k+ row table.
        conn.execute('CREATE INDEX IF NOT EXISTS idx_action_log_action_ts ON action_log(action, ts)')
        # Decision tape: a raw, model-facing record of every Jev decision call the
        # chokepoint (_call_openrouter_decision_sync) makes -- prompt, candidates,
        # parsed choice/confidence/cost, and the full response. Complements the
        # state-side audit (action_log + passport hash-chain) with the
        # observed-answer side those don't keep: what Jev was asked, what it
        # answered, and how certain/costly it was. Outcomes stay in action_log/
        # passport; correlate by ts + agent_id.
        conn.execute('''CREATE TABLE IF NOT EXISTS decision_tape (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            kind TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt TEXT NOT NULL,
            criteria TEXT NOT NULL,
            choice TEXT,
            confidence REAL,
            cost REAL,
            raw TEXT NOT NULL,
            ok INTEGER NOT NULL,
            trace_id TEXT,
            prev_hash TEXT,
            hash TEXT
        )''')
        # Same unindexed-scan class as action_log above: decision_tape grew to
        # 641MB/223k rows with no index, so any time-windowed read full-scans
        # it. Index on ts (primary ordering key).
        conn.execute('CREATE INDEX IF NOT EXISTS idx_decision_tape_ts ON decision_tape(ts)')
        try:
            conn.execute('ALTER TABLE decision_tape ADD COLUMN trace_id TEXT')
        except Exception:
            pass
        # Tamper-evident chain: every row carries prev_hash + hash (HMAC-SHA256
        # under the server secret over prev_hash + the row's content), so a
        # modified or reordered row breaks the chain (see _verify_decision_tape).
        # Backfilled idempotently for rows written before this existed -- the
        # backfill only fills NULL-hash rows and never re-signs a hashed one.
        for _col in ('prev_hash', 'hash'):
            try:
                conn.execute(f'ALTER TABLE decision_tape ADD COLUMN {_col} TEXT')
            except Exception:
                pass
        _backfill_decision_tape_hashes(conn)
        # The permanent decision ARCHIVE. _prune_logs distills old decision_tape
        # rows (prompt/raw are the expensive part) into this append-only table
        # before deleting them, so the think tank's institutional memory --
        # what was decided, by which model, how confident, at what cost -- is
        # NEVER lost to retention pruning. The noisy full prompt/response stays
        # on the 7-day tape; the distilled decision survives forever and is
        # folded into the weekly decision-archive wiki page (_archive_distill_step).
        # Each archive row carries the tape hash of the row it distilled from,
        # so an archived decision can be cross-checked against the tape's chain.
        conn.execute('''CREATE TABLE IF NOT EXISTS decision_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            kind TEXT NOT NULL,
            model TEXT NOT NULL,
            choice TEXT,
            confidence REAL,
            cost REAL,
            ok INTEGER NOT NULL,
            trace_id TEXT,
            tape_hash TEXT
        )''')
        try:
            conn.execute('ALTER TABLE decision_archive ADD COLUMN tape_hash TEXT')
        except Exception:
            pass
        conn.execute('CREATE INDEX IF NOT EXISTS idx_decision_archive_ts ON decision_archive(ts)')
        conn.execute('''CREATE TABLE IF NOT EXISTS model_tiers (
            band TEXT PRIMARY KEY,
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            price_per_m REAL NOT NULL,
            chosen_at REAL NOT NULL
        )''')
        # Ordered alternates for each tier: when a band's selected model stops
        # answering (delisted / no endpoints), a real call retries down this
        # list immediately instead of waiting for the minute-scale re-pick
        # loop. Filled from the same candidate pool as the pick; not verified
        # up front, because an alternate is only tried after a real failure
        # and a successful answer is its proof.
        conn.execute('''CREATE TABLE IF NOT EXISTS model_tier_fallbacks (
            band TEXT NOT NULL,
            rank INTEGER NOT NULL,
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            price_per_m REAL NOT NULL,
            chosen_at REAL NOT NULL,
            PRIMARY KEY (band, rank)
        )''')
        # Daily tier price/score log: one row per (date, band) recording
        # which model the tier picked, its blended price, and the benchmark
        # score the pick was grounded on. model_tiers keeps only the CURRENT
        # pick; this table is the append-only history the scheduling logic
        # reads to see how each band's price and quality moved day over day
        # (e.g. "coding tier was $0.26 yesterday, $1.30 today"). Written on
        # every refresh_model_tiers run; same-day re-runs overwrite rather
        # than duplicate (PK on log_date+band).
        conn.execute('''CREATE TABLE IF NOT EXISTS tier_daily_log (
            log_date TEXT NOT NULL,
            band TEXT NOT NULL,
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            price_per_m REAL NOT NULL,
            benchmark TEXT,
            score REAL,
            logged_at REAL NOT NULL,
            median_latency_ms REAL,
            latency_samples INTEGER,
            speed_picked INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (log_date, band)
        )''')
        # BP-63 audit columns: when a band's pick was latency-decided (a
        # pricier qualifier inside the price parity band won on observed
        # speed), the daily row records the median ms and the sample count
        # that grounded it, plus an explicit speed_picked flag. NULL latency
        # fields mean the pick was NOT latency-decided (cheapest-first,
        # unsampled, or a classifier fallback), so absence stays honest.
        # Column-added migration keeps existing databases working.
        _tier_log_cols = {r[1] for r in conn.execute('PRAGMA table_info(tier_daily_log)')}
        for _col_type in ('median_latency_ms REAL', 'latency_samples INTEGER', 'speed_picked INTEGER NOT NULL DEFAULT 0'):
            if _col_type.split()[0] not in _tier_log_cols:
                conn.execute(f'ALTER TABLE tier_daily_log ADD COLUMN {_col_type}')
        # Full-catalog daily price snapshot: one row per (date, model) with
        # the blended price. model_catalog overwrites prices in place on
        # every sync, so without this a model's price history is lost the
        # moment it changes. This table is the market-wide series the
        # scheduling logic reads -- when DeepSeek V4 Flash is \$0.26 one day
        # and \$1.30 the next, both data points exist even if it never won
        # a tier pick. Written by _sync_model_catalog on every refresh;
        # same-day re-runs overwrite (PK log_date+model_id).
        conn.execute('''CREATE TABLE IF NOT EXISTS catalog_price_daily (
            log_date TEXT NOT NULL,
            model_id TEXT NOT NULL,
            price_per_m REAL NOT NULL,
            logged_at REAL NOT NULL,
            PRIMARY KEY (log_date, model_id)
        )''')
        # Daily OpenRouter catalog snapshot -- the persistent record of "what
        # models existed, at what price, on which day" that the live fetch
        # (_fetch_openrouter_catalog_sync) otherwise leaves nowhere in the DB.
        # model_tiers holds the DECISION; this table holds the market data the
        # decision was grounded in. Synced by _sync_model_catalog() on every
        # refresh: new models are inserted (first_seen set), known models get
        # refreshed prices + carried-over benchmark scores, and models that
        # vanished from the catalog are purged -- from BOTH model_catalog and
        # model_benchmark_scores, so a removed model's stale score record can't
        # linger and quietly mislead a future tier pick. `scores` mirrors the
        # player-entered benchmark scores (JSON: benchmark -> score), so the
        # snapshot carries the research context next to the price instead of
        # only a bare slug.
        conn.execute('''CREATE TABLE IF NOT EXISTS model_catalog (
            model_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            prompt_price REAL NOT NULL,
            completion_price REAL NOT NULL,
            price_per_m REAL NOT NULL,
            image_capable INTEGER NOT NULL DEFAULT 0,
            scores TEXT,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL
        )''')
        # BP-63 observed-latency samples: one row per successful OpenRouter
        # POST, wall-clock ms written by _post_once_tracked through
        # _record_model_latency. The tier pick reads a per-model median as a
        # speed tie-break inside the cheapest qualifier's price parity band
        # (OpenRouter's own latency numbers are not exposed anywhere we can
        # pull -- only through request-time routing prefs -- so the tank
        # measures its own). Rows older than the retention window are pruned
        # opportunistically, at most once per hour. Failed calls are never
        # recorded: a failed POST's duration is queue noise, not model speed.
        conn.execute('''CREATE TABLE IF NOT EXISTS model_latency_samples (
            model TEXT NOT NULL,
            ts REAL NOT NULL,
            duration_ms REAL NOT NULL
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_latency_model_ts ON model_latency_samples(model, ts)')
        # Real login sessions -- replaces the old "anyone who loads the
        # page gets the same baked-in key" model (see the removed
        # SERVER_ACCESS_KEY) now that you're considering a public
        # deployment. A session is only ever created by a verified
        # username+password POST to /login; the id itself is the only
        # thing the browser holds (as an HttpOnly cookie -- unlike the old
        # key, page JS can't read it, so an XSS bug can't exfiltrate it).
        conn.execute('''CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )''')
        # Model-tier selection for EVERY band -- not just
        # 'high' (coding) -- needs to be grounded in real, published
        # benchmark scores across the WHOLE real candidate pool, every
        # vendor in the current OpenRouter catalog, not just whichever
        # names happen to be familiar. This table is that real, persistent
        # record: one row per (model, benchmark) actually looked up, so the
        # research doesn't have to be redone from scratch (or drift toward
        # only the models someone already recognizes) every time tiers are
        # refreshed. `source_url` keeps every score traceable back to
        # where it came from, since a benchmark number with no citation is
        # not verifiable later. Originally named model_coding_scores when
        # it only held SWE-bench Verified numbers for the coding band --
        # renamed once 'low' (MMLU) and 'mid' (MMLU-Pro) started using it
        # too, since a table full of non-coding scores called "coding
        # scores" would mislead the next person who reads the schema.
        conn.execute('''CREATE TABLE IF NOT EXISTS model_benchmark_scores (
            model_id TEXT NOT NULL,
            benchmark TEXT NOT NULL,
            score REAL NOT NULL,
            source_url TEXT,
            evaluator TEXT,
            checked_at REAL NOT NULL,
            PRIMARY KEY (model_id, benchmark)
        )''')
        # Migration: the evaluator column (who actually measured the score --
        # 'artificial_analysis' for the OpenRouter/AA indices, 'huggingface_open_llm'
        # for the Open LLM Leaderboard fallback, 'player' for hand-entered research)
        # was added after the table first shipped. Back-fill it for the AA-index
        # rows already in the table so an existing DB carries real evaluator data.
        bench_cols = {r[1] for r in conn.execute('PRAGMA table_info(model_benchmark_scores)')}
        if 'evaluator' not in bench_cols:
            conn.execute('ALTER TABLE model_benchmark_scores ADD COLUMN evaluator TEXT')
            conn.execute(
                "UPDATE model_benchmark_scores SET evaluator = 'artificial_analysis' "
                "WHERE evaluator IS NULL AND benchmark IN ('intelligence_index', 'coding_index', 'agentic_index')")
        # One-time migration for any think_tank.db created before the rename
        # above -- preserves real, already-cited research instead of
        # silently losing it the first time this runs against an existing
        # database.
        old_table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_coding_scores'"
        ).fetchone()
        if old_table_exists:
            conn.execute(
                'INSERT OR IGNORE INTO model_benchmark_scores (model_id, benchmark, score, source_url, checked_at) '
                'SELECT model_id, benchmark, score, source_url, checked_at FROM model_coding_scores'
            )
            conn.execute('DROP TABLE model_coding_scores')
        # An agent without standing access to
        # something (e.g. an agent with no Weather Station curl access)
        # should be able to ask their supervisor for it, and get REAL,
        # TEMPORARY access if the reason is legitimate -- not permanent,
        # not self-granted. One active grant per (agent, capability); a
        # fresh approval overwrites/extends it rather than stacking rows.
        # Real expiry, checked server-side on every use,
        # same trust boundary as the room check this sits alongside.
        conn.execute('''CREATE TABLE IF NOT EXISTS temp_access_grants (
            agent_id TEXT NOT NULL,
            capability TEXT NOT NULL,
            granted_by TEXT NOT NULL,
            reason TEXT,
            granted_at REAL NOT NULL,
            expires_at REAL,
            task_id TEXT,
            PRIMARY KEY (agent_id, capability)
        )''')
        # Migration for DBs created before the per-story grant existed: the
        # grant carries the task_id (the specific story it was granted for), so
        # it can be revoked the moment that story ships instead of riding a
        # timer alone.
        _cols = {r[1] for r in conn.execute('PRAGMA table_info(temp_access_grants)')}
        if 'task_id' not in _cols:
            conn.execute('ALTER TABLE temp_access_grants ADD COLUMN task_id TEXT')
        # Migration for DBs created before story-scoped grants stopped riding a
        # clock: SQLite can't relax a NOT NULL column in place, so rebuild the
        # table with a nullable expires_at. NULL = "no timer -- this grant lives
        # until its story ships" (revoke_task_access on completion); only a
        # grant with no story keeps the finite window.
        _exp_notnull = any(r[1] == 'expires_at' and bool(r[3])
                           for r in conn.execute('PRAGMA table_info(temp_access_grants)'))
        if _exp_notnull:
            conn.execute('ALTER TABLE temp_access_grants RENAME TO temp_access_grants_old')
            conn.execute('''CREATE TABLE temp_access_grants (
                agent_id TEXT NOT NULL,
                capability TEXT NOT NULL,
                granted_by TEXT NOT NULL,
                reason TEXT,
                granted_at REAL NOT NULL,
                expires_at REAL,
                task_id TEXT,
                PRIMARY KEY (agent_id, capability)
            )''')
            conn.execute('''INSERT INTO temp_access_grants (agent_id, capability, granted_by, reason, granted_at, expires_at, task_id)
                            SELECT agent_id, capability, granted_by, reason, granted_at, expires_at, task_id FROM temp_access_grants_old''')
            conn.execute('DROP TABLE temp_access_grants_old')
        # Phase D external-credential vault (confused-deputy). Two tables:
        #   external_credentials -- the real secrets, encrypted at rest with a
        #     server-side Fernet key; never readable back, only decrypted in
        #     process at use time. Each entry is one of two generic kinds: a
        #     'token' (an API key injected into a request) or a 'login'
        #     (username + password for an interactive account). 'status' is the
        #     registry lifecycle: agents propose (encrypted immediately,
        #     status='proposed'), a director/admin or the player approves or
        #     denies, and removed rows are unusable -- the whole vault is
        #     runtime-administered, no restart needed.
        #   capability_handles    -- opaque nonces an agent presents to USE a
        #     credential for a scoped purpose (allowed hosts/methods/expiry).
        #     The agent never holds the credential, only a handle; the server
        #     resolves the handle to the real secret and injects it, so a
        #     persuaded/compromised agent still can't exfiltrate the key.
        conn.execute('''CREATE TABLE IF NOT EXISTS external_credentials (
            name            TEXT PRIMARY KEY,
            service         TEXT NOT NULL,
            kind            TEXT NOT NULL DEFAULT 'token',
            encrypted_value TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'active',
            proposed_by     TEXT,
            created_at      REAL NOT NULL,
            updated_at      REAL
        )''')
        try:
            cols = [r[1] for r in conn.execute('PRAGMA table_info(external_credentials)')]
            for col, col_type in (('kind', "TEXT NOT NULL DEFAULT 'token'"),
                                  ('status', "TEXT NOT NULL DEFAULT 'active'"),
                                  ('proposed_by', 'TEXT'),
                                  ('updated_at', 'REAL')):
                if col not in cols:
                    conn.execute(f'ALTER TABLE external_credentials ADD COLUMN {col} {col_type}')  # nosec B608 -- fixed internal column names, never user input
        except Exception:
            pass
        conn.execute('''CREATE TABLE IF NOT EXISTS capability_handles (
            handle          TEXT PRIMARY KEY,
            agent_id        TEXT NOT NULL,
            credential_name TEXT NOT NULL,
            purpose         TEXT NOT NULL,
            allowed_hosts   TEXT NOT NULL,
            allowed_methods TEXT NOT NULL,
            granted_by      TEXT NOT NULL,
            expires_at      REAL NOT NULL,
            created_at      REAL NOT NULL
        )''')
        # Inbound SMS inbox (receive-only by design -- nothing here can send a
        # text). Messages enter ONLY via /api/sms/inbound, which requires a
        # shared forward secret, so a text arriving means a real forwarder
        # delivered it. Reads are gated per-message through sms_read_grants:
        # a director/admin/player mints one short-lived, single-use grant for
        # ONE agent + ONE message, choosing whether the OTP code is revealed.
        # Agents never browse the inbox; every read is an ask.
        conn.execute('''CREATE TABLE IF NOT EXISTS sms_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sender      TEXT NOT NULL,
            body        TEXT NOT NULL,
            otp_code    TEXT,
            received_at REAL NOT NULL,
            consumed_at REAL
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS sms_read_grants (
            grant_id    TEXT PRIMARY KEY,
            agent_id    TEXT NOT NULL,
            message_id  INTEGER NOT NULL,
            granted_by  TEXT NOT NULL,
            otp_allowed INTEGER NOT NULL DEFAULT 0,
            created_at  REAL NOT NULL,
            expires_at  REAL NOT NULL
        )''')
        # Real, persistent record of health anomalies (see
        # compute_health_snapshot) -- a table, not just a print, so an
        # alert survives even if nobody's watching stdout when it fires.
        # Built to close the "no monitoring layer -- everything gets
        # caught reactively" gap.
        conn.execute('''CREATE TABLE IF NOT EXISTS health_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            severity TEXT NOT NULL,
            message TEXT NOT NULL,
            ts REAL NOT NULL
        )''')
        # Judge-vs-anchor calibration samples (self-evolve
        # selfdeception.py -- "judge the judge"). One row per review-checklist
        # grade where Jev's SUBJECTIVE verdict (meets/fails) can be checked
        # against the MECHANICAL code-pipeline verdict (the anchor) for the same
        # requirement section; agree=1 when they match. The report aggregates
        # these into an agreement rate that drives the review-grade confidence
        # bar (_review_grade_calibration_pass) -- an overconfident grader shows
        # up here as a low agreement rate, not as confidence that was never
        # checked. Indexed on ts because the report is a time-windowed scan.
        conn.execute('''CREATE TABLE IF NOT EXISTS review_judge_calibration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            section TEXT NOT NULL,
            judge_verdict TEXT NOT NULL,
            judge_confidence REAL NOT NULL,
            anchor_verdict TEXT NOT NULL,
            agree INTEGER NOT NULL
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_review_calibration_ts ON review_judge_calibration(ts)')
        # Bot Ops / weekly review. A recurring diff-against-expectation report:
        # every real change the think tank made in a window (from action_log +
        # decision_tape -- GROUND TRUTH, never an agent's self-reported summary)
        # plus the prior-week comparison, so the player reviews what actually
        # happened vs. what was asked, rather than trusting a bot's write-up.
        # `period_start` is the week's UTC start (epoch ms); one row per week.
        conn.execute('''CREATE TABLE IF NOT EXISTS weekly_reviews (
            period_start INTEGER PRIMARY KEY,
            generated_at REAL NOT NULL,
            digest TEXT NOT NULL,
            markdown TEXT NOT NULL
        )''')
        # Generic api_call service registry. This is the DB-backed replacement
        # for world/api_services.json -- a service is added/extended by a row
        # here, not by editing serve.py. status='active' rows are what
        # _load_api_services() exposes to the generic api_call path;
        # 'pending'/'denied' rows are proposal/approval state (see the
        # /api/api-service/request flow). JSON columns hold the lists/dicts a
        # service entry needs (methods, path_rules, default_headers,
        # workflow). Seeded from world/api_services.json the first time the
        # table comes up empty, so the JSON file stays the canonical seed and
        # a fresh install gets the five built-ins for free.
        conn.execute('''CREATE TABLE IF NOT EXISTS api_services (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            credential_type TEXT NOT NULL DEFAULT 'env',
            credential_key TEXT,
            credential_key2 TEXT,
            credential_name TEXT,
            auth_type TEXT NOT NULL DEFAULT 'header_bearer',
            auth_value_format TEXT,
            auth_field TEXT,
            auth_token_url TEXT,
            auth_header_name TEXT,
            default_headers TEXT,
            methods TEXT,
            path_rules TEXT,
            spend_kind TEXT NOT NULL DEFAULT 'none',
            spend_note TEXT,
            spend_cap REAL,
            workflow TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            proposed_by TEXT,
            created_at REAL,
            updated_at REAL
        )''')
        # Migration for DBs created before auth_token_url (the OAuth2
        # client-credentials token endpoint a registered service declares) or
        # spend_cap (the daily-call budget cap): CREATE TABLE IF NOT EXISTS
        # cannot add a column to an existing table, so ALTER it in place when
        # missing. New DBs get the columns from the DDL above and this is a
        # no-op.
        try:
            cols = [r[1] for r in conn.execute('PRAGMA table_info(api_services)')]
            for col, col_type in (('auth_token_url', 'TEXT'), ('spend_cap', 'REAL'), ('auth_header_name', 'TEXT')):
                if col not in cols:
                    conn.execute(f'ALTER TABLE api_services ADD COLUMN {col} {col_type}')  # nosec B608 -- fixed internal column names, never user input
        except Exception:
            pass

        # RSS aggregator feed registry -- the curated feed list lives HERE, not
        # in code, so a director/admin can add / tweak / remove feeds at
        # runtime with no restart. Seeded from the module-level _RSS_FEED_SEED
        # on first boot, exactly like api_services seeds from
        # world/api_services.json.
        conn.execute('''CREATE TABLE IF NOT EXISTS rss_feeds (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            interval_s INTEGER NOT NULL DEFAULT 600,
            status TEXT NOT NULL DEFAULT 'active',
            proposed_by TEXT,
            created_at REAL,
            updated_at REAL
        )''')

        # WebSocket feed registry -- same operator-managed registry idea as
        # rss_feeds, for subscribe-and-stream feeds. Each row carries a
        # declarative ws_spec descriptor (url, subscribe template, frame
        # filter, field paths) that the one generic collector interprets.
        # Seeded with the two built-ins (kraken, aisstream) on first boot.
        conn.execute('''CREATE TABLE IF NOT EXISTS ws_feeds (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            credential_type TEXT NOT NULL DEFAULT 'none',
            credential_key TEXT,
            credential_key2 TEXT,
            ws_spec TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            proposed_by TEXT,
            created_at REAL,
            updated_at REAL
        )''')

        # Benchmark data-source registry -- the URLs the model-tier refresh
        # fetches external evaluator data from (the Artificial Analysis
        # LLM leaderboard page, the HF Open LLM Leaderboard parquet, ...). These
        # links drift over time, so they live HERE, not in code: a player or a
        # director/admin agent PUTs a fresh JSON document to /api/data-sources
        # and the next refresh picks it up with no restart. Deliberately NOT
        # seeded -- an unset source is skipped with a log line until an
        # operator configures it.
        conn.execute('''CREATE TABLE IF NOT EXISTS data_sources (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_by TEXT,
            updated_at REAL
        )''')
        tank_accounts.ensure_schema(conn)
        captcha.ensure_schema(conn)
        import server.migrations as _migrations
        _migrations.apply(conn)

    _seed_api_services_if_empty()
    _seed_rss_feeds_if_empty()
    _seed_ws_feeds_if_empty()
    _load_api_services()
    _load_rss_feeds()
    _load_ws_feeds()
    _harden_db_perms(DB_PATH)


def _harden_db_perms(path):
    """Keep state/audit DB files private. The DB and its WAL/SHM siblings
    (plus the timestamped snapshots) hold full prompts, raw model output, and
    plaintext agent keys; a default-umask 0644 leaves them readable by any
    local account. Best-effort, a chmod failure never takes the server down."""
    try:
        os.chmod(path, 0o600)
        for suffix in ('-wal', '-shm'):
            sibling = path + suffix
            if os.path.exists(sibling):
                os.chmod(sibling, 0o600)
    except OSError:
        pass
