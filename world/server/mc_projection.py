"""Tank Mission Control projection: relational mirrors of the live state.

The tank's whole state lives in one kv_state JSONB blob, which SQL panels
cannot query cheaply. These mc_* tables are a read-only-facing projection:
the tank's writers stay untouched (sync runs after a committed save, fails
silently and is counted, and never raises into the save path -- Mission
Control's invariant). Every change notifies the 'mc_changed' channel so
the realtime proxy re-runs exactly the affected live queries.
"""
import json

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS mc_tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT,
    assigned_to TEXT,
    task_type TEXT,
    deliverable TEXT,
    priority INTEGER,
    feature TEXT,
    created_ms REAL,
    updated_ms REAL,
    blob TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mc_agents (
    id TEXT PRIMARY KEY,
    name TEXT,
    status TEXT,
    role TEXT,
    team_id TEXT,
    blob TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mc_programs (
    id TEXT PRIMARY KEY,
    title TEXT,
    owner_agent_id TEXT,
    status TEXT,
    planning_card_minted INTEGER,
    meeting_card_minted INTEGER,
    pieces_minted INTEGER,
    cr_requested INTEGER,
    piece_status TEXT,
    blob TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mc_work_queue (
    id TEXT PRIMARY KEY,
    title TEXT,
    status TEXT,
    assigned_to TEXT,
    not_before REAL,
    blob TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mc_inbox (
    id TEXT PRIMARY KEY,
    agent_id TEXT,
    question TEXT,
    status TEXT,
    created_ms REAL,
    blob TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mc_issues (
    id TEXT PRIMARY KEY,
    title TEXT,
    status TEXT,
    created_ms REAL,
    updated_ms REAL,
    blob TEXT NOT NULL
);
'''

def _safe_items(d):
    while True:
        try:
            return list(d.items())
        except RuntimeError:
            continue


def _safe_json(obj) -> str:
    while True:
        try:
            return json.dumps(obj, sort_keys=True, default=str)
        except RuntimeError:
            continue


def ensure_schema(conn):
    import db_backend
    conn.executescript(db_backend.translate_ddl(_SCHEMA))
    conn.commit()


def _upsert_tasks(conn, tasks) -> bool:
    changed = False
    existing = {r[0]: r[1] for r in conn.execute(
        'SELECT id, blob FROM mc_tasks').fetchall()}
    wanted = {}
    for tid, t in _safe_items(tasks or {}):
        if not isinstance(t, dict):
            continue
        blob = _safe_json(t)
        wanted[tid] = blob
        row = (t.get('title') or '', t.get('status'),
               t.get('assignedTo'), t.get('taskType'),
               t.get('deliverable'), t.get('priority'),
               t.get('feature'), t.get('createdAt'),
               t.get('updatedAt') or t.get('createdAt'), blob)
        if existing.get(tid) != blob:
            conn.execute(
                'INSERT OR REPLACE INTO mc_tasks (id, title, status, '
                'assigned_to, task_type, deliverable, priority, feature, '
                'created_ms, updated_ms, blob) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (tid,) + row)
            changed = True
    gone = set(existing) - set(wanted)
    for tid in gone:
        conn.execute('DELETE FROM mc_tasks WHERE id = ?', (tid,))
        changed = True
    return changed


def _upsert_agents(conn, agents) -> bool:
    changed = False
    existing = {r[0]: r[1] for r in conn.execute(
        'SELECT id, blob FROM mc_agents').fetchall()}
    wanted = {}
    for aid, a in _safe_items(agents or {}):
        if not isinstance(a, dict):
            continue
        blob = _safe_json(a)
        wanted[aid] = blob
        if existing.get(aid) != blob:
            conn.execute(
                'INSERT OR REPLACE INTO mc_agents (id, name, status, role, '
                'team_id, blob) VALUES (?,?,?,?,?,?)',
                (aid, a.get('name'), a.get('status'), a.get('role'),
                 a.get('teamId') or a.get('team'), blob))
            changed = True
    gone = set(existing) - set(wanted)
    for aid in gone:
        conn.execute('DELETE FROM mc_agents WHERE id = ?', (aid,))
        changed = True
    return changed


def _upsert_programs(conn, programs) -> bool:
    changed = False
    existing = {r[0]: r[1] for r in conn.execute(
        'SELECT id, blob FROM mc_programs').fetchall()}
    wanted = {}
    for p in (programs or []):
        if not isinstance(p, dict):
            continue
        pid = p.get('id')
        if not pid:
            continue
        blob = _safe_json(p)
        wanted[pid] = blob
        if existing.get(pid) != blob:
            piece_status = p.get('pieceStatus') or {}
            conn.execute(
                'INSERT OR REPLACE INTO mc_programs (id, title, '
                'owner_agent_id, status, planning_card_minted, '
                'meeting_card_minted, pieces_minted, cr_requested, '
                'piece_status, blob) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (pid, p.get('title'), p.get('ownerAgentId'), p.get('status'),
                 1 if p.get('planningCardMinted') else 0,
                 1 if p.get('meetingCardMinted') else 0,
                 1 if p.get('piecesMinted') else 0,
                 1 if p.get('crRequested') else 0,
                 json.dumps(piece_status, default=str), blob))
            changed = True
    gone = set(existing) - set(wanted)
    for pid in gone:
        conn.execute('DELETE FROM mc_programs WHERE id = ?', (pid,))
        changed = True
    return changed


def _upsert_queue(conn, queue) -> bool:
    changed = False
    existing = {r[0]: r[1] for r in conn.execute(
        'SELECT id, blob FROM mc_work_queue').fetchall()}
    wanted = {}
    for i, item in enumerate(queue or []):
        if not isinstance(item, dict):
            continue
        iid = item.get('id') or f'q{i}'
        blob = _safe_json(item)
        wanted[iid] = blob
        if existing.get(iid) != blob:
            conn.execute(
                'INSERT OR REPLACE INTO mc_work_queue (id, title, status, '
                'assigned_to, not_before, blob) VALUES (?,?,?,?,?,?)',
                (iid, item.get('title'), item.get('status'),
                 item.get('assignedTo'), item.get('notBefore'), blob))
            changed = True
    gone = set(existing) - set(wanted)
    for iid in gone:
        conn.execute('DELETE FROM mc_work_queue WHERE id = ?', (iid,))
        changed = True
    return changed


def _upsert_inbox(conn, messages) -> bool:
    changed = False
    existing = {r[0]: r[1] for r in conn.execute(
        'SELECT id, blob FROM mc_inbox').fetchall()}
    wanted = {}
    for m in (messages or []):
        if not isinstance(m, dict):
            continue
        mid = str(m.get('id') or m.get('createdAt'))
        if not mid or mid == 'None':
            continue
        blob = _safe_json(m)
        wanted[mid] = blob
        if existing.get(mid) != blob:
            conn.execute(
                'INSERT OR REPLACE INTO mc_inbox (id, agent_id, question, '
                'status, created_ms, blob) VALUES (?,?,?,?,?,?)',
                (mid, m.get('agentId') or m.get('from'),
                 m.get('title') or m.get('question') or m.get('body'),
                 m.get('status') or ('answered' if m.get('closeReason')
                                     else 'awaiting_input'),
                 m.get('createdAt'), blob))
            changed = True
    gone = set(existing) - set(wanted)
    for mid in gone:
        conn.execute('DELETE FROM mc_inbox WHERE id = ?', (mid,))
        changed = True
    return changed


def _upsert_issues(conn, issues) -> bool:
    changed = False
    existing = {r[0]: r[1] for r in conn.execute(
        'SELECT id, blob FROM mc_issues').fetchall()}
    wanted = {}
    if isinstance(issues, dict):
        issue_iter = [(k, v) for k, v in _safe_items(issues)
                      if isinstance(v, dict)]
    else:
        issue_iter = [(i.get('id'), i) for i in (issues or [])
                      if isinstance(i, dict)]
    for iid, issue in issue_iter:
        if not iid:
            continue
        blob = _safe_json(issue)
        wanted[iid] = blob
        if existing.get(iid) != blob:
            conn.execute(
                'INSERT OR REPLACE INTO mc_issues (id, title, status, '
                'created_ms, updated_ms, blob) VALUES (?,?,?,?,?,?)',
                (iid, issue.get('title'), issue.get('status'),
                 issue.get('createdAt'), issue.get('updatedAt'), blob))
            changed = True
    gone = set(existing) - set(wanted)
    for iid in gone:
        conn.execute('DELETE FROM mc_issues WHERE id = ?', (iid,))
        changed = True
    return changed


def notify(conn, tables):
    """Tell the realtime proxy which projection tables changed."""
    import json as _json
    try:
        raw = conn.execute(
            "SELECT pg_notify('mc_changed', ?)",
            (_json.dumps({'tables': sorted(tables)}),))
        raw.fetchall()
    except Exception:
        pass


def sync(state, conn=None, notify_tables=True) -> list:
    """Mirror the live state into the projection. Returns the changed
    table names. NEVER raises: a failed sync is counted and skipped, and
    the next committed save retries with fresher data."""
    changed = []
    own = conn is None
    try:
        if own:
            import db_backend
            conn = db_backend.connect()
        ensure_schema(conn)
        if _upsert_tasks(conn, state.get('tasks') or {}):
            changed.append('mc_tasks')
        if _upsert_agents(conn, state.get('agents') or {}):
            changed.append('mc_agents')
        if _upsert_programs(conn, state.get('programs') or []):
            changed.append('mc_programs')
        if _upsert_queue(conn, state.get('workQueue') or []):
            changed.append('mc_work_queue')
        if _upsert_issues(conn, state.get('issues') or {}):
            changed.append('mc_issues')
        if _upsert_inbox(conn, state.get('playerInbox') or []):
            changed.append('mc_inbox')
        if changed:
            conn.commit()
            if notify_tables:
                notify(conn, changed)
    except Exception as exc:
        count_failure()
        try:
            print(f'[mc-projection] sync failed (counted, silent): '
                  f'{type(exc).__name__}: {exc}', flush=True)
        except Exception:
            pass
        if own and conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return changed


_MC_FAILURES = {'count': 0}


def count_failure():
    _MC_FAILURES['count'] += 1
    return _MC_FAILURES['count']
