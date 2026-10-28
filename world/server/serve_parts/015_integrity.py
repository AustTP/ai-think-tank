# Integrity primitives: a checksum over the whole-village state blob and
# parity rows over the append-only action log. The tank runs semi-autonomously
# and spends real money, so a silently torn or edited record must become a
# LOUD event, not a wrong answer discovered weeks later.
#
# Design decisions:
# - The whole-state checksum lives in its own singleton table
#   (state_checksum), updated in the same transaction as every blob write,
#   so the pair is always consistent as of any commit.
# - A checksum MISMATCH alerts and stashes the suspect blob beside the DB for
#   forensics but does NOT auto-restore from backup: auto-restore can destroy
#   newer valid state written after the backup. The operator decides.
# - Action-log parity: every 64 unsummed rows a 'log_parity' row carries a
#   sha256 over the covered window's canonical rows, so truncation or
#   retro-editing of the audit trail (which holds spend and safety decisions)
#   is detectable by a cheap verification of the trailing windows.

import hashlib
import json
import os
import time

ACTION_LOG_PARITY_WINDOW = 64


def _state_checksum_for(blob_text):
    return hashlib.sha256(blob_text.encode('utf-8')).hexdigest()


def _state_checksum_write(conn, blob_text):
    """Update the singleton checksum row inside the writer's transaction."""
    conn.execute(
        'INSERT INTO state_checksum (singleton, sha256, updated_at) VALUES (1, ?, ?) '
        'ON CONFLICT(singleton) DO UPDATE SET sha256 = excluded.sha256, updated_at = excluded.updated_at',
        (_state_checksum_for(blob_text), time.time()),
    )


def _verify_state_checksum_at_boot():
    """Boot-time integrity gate for the whole-village blob. Returns a small
    dict: {'ok': True}, {'seeded': True} (first boot after this feature), or
    {'ok': False, 'stashedTo': path}. A fresh DB (no blob yet) returns None."""
    try:
        with _db() as conn:
            row = conn.execute('SELECT blob FROM kv_state WHERE id = 1').fetchone()
            if not row:
                return None  # nothing seeded yet -- nothing to verify
            blob = row[0]
            want = _state_checksum_for(blob)
            got_row = conn.execute('SELECT sha256 FROM state_checksum WHERE singleton = 1').fetchone()
            got = got_row[0] if got_row else None
            if got is None:
                _state_checksum_write(conn, blob)
                return {'seeded': True}
            if got == want:
                return {'ok': True}
            stash = None
            try:
                stash = os.path.join(THINK_TANK_DIR, f'state.corrupt-{int(time.time())}.json')
                with open(stash, 'w') as f:
                    f.write(blob)
            except OSError:
                stash = None
            try:
                conn.execute(
                    'INSERT INTO action_log (agent_id, action, details, authorized, ts) '
                    "VALUES (NULL, 'state_checksum_mismatch', ?, NULL, ?)",
                    (json.dumps({'stored': got, 'computed': want, 'stashedTo': stash}),
                     time.time()))
            except Exception:
                pass
            # Re-anchor to what we are actually running with: the mismatch has
            # been reported, and the next legitimate save rewrites both rows.
            _state_checksum_write(conn, blob)
            return {'ok': False, 'stashedTo': stash}
    except Exception:
        return None


def _canonical_action_row(row):
    rid, agent_id, action, details, authorized, ts, trace_id = row
    return '|'.join('' if x is None else str(x) for x in
                    (rid, agent_id, action, details, authorized, ts, trace_id))


def _action_log_parity_maybe():
    """Called at the end of log_action. When >= ACTION_LOG_PARITY_WINDOW
    unsummed rows have piled up since the last parity row, append one covering
    them. A duplicate parity row from a concurrent append is harmless: the
    verifier trusts the LAST row covering a window, and overlapping windows
    still verify against their recorded ranges."""
    try:
        with _db() as conn:
            last = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM action_log WHERE action = 'log_parity'").fetchone()[0]
            cnt, top = conn.execute(
                'SELECT COUNT(*), COALESCE(MAX(id), 0) FROM action_log WHERE id > ?',
                (last,)).fetchone()
            if cnt < ACTION_LOG_PARITY_WINDOW:
                return
            rows = conn.execute(
                'SELECT id, agent_id, action, details, authorized, ts, trace_id '
                'FROM action_log WHERE id > ? AND id <= ? ORDER BY id',
                (last, top)).fetchall()
            digest = hashlib.sha256(
                '\n'.join(_canonical_action_row(r) for r in rows)
                .encode('utf-8', errors='replace')).hexdigest()
            conn.execute(
                'INSERT INTO action_log (agent_id, action, details, authorized, ts, trace_id) '
                "VALUES (NULL, 'log_parity', ?, NULL, ?, NULL)",
                (json.dumps({'afterId': last, 'throughId': top,
                             'rows': len(rows), 'sha256': digest}), time.time()))
    except Exception:
        pass  # parity is best-effort; it must never break logging itself


def _verify_action_log_parity(windows=2):
    """Recompute the last `windows` parity windows against the live rows.
    Returns (ok, problems) where problems is a list of {id, issue}. Rows
    legitimately deleted after a parity row (operator cleanup) WILL fail the
    covering window -- that is the feature: deletions are visible."""
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT id, details FROM action_log WHERE action = 'log_parity' "
                'ORDER BY id DESC LIMIT ?', (windows,)).fetchall()
            if not rows:
                return True, []
            problems = []
            for pid, det in rows:
                try:
                    spec = json.loads(det or '{}')
                except ValueError:
                    problems.append({'id': pid, 'issue': 'malformed parity row'})
                    continue
                after, through, want = spec.get('afterId', 0), spec.get('throughId'), spec.get('sha256')
                if through is None or not want:
                    problems.append({'id': pid, 'issue': 'malformed parity row'})
                    continue
                cur = conn.execute(
                    'SELECT id, agent_id, action, details, authorized, ts, trace_id '
                    'FROM action_log WHERE id > ? AND id <= ? ORDER BY id',
                    (after, through)).fetchall()
                got = hashlib.sha256(
                    '\n'.join(_canonical_action_row(r) for r in cur)
                    .encode('utf-8', errors='replace')).hexdigest()
                if got != want:
                    problems.append({'id': pid,
                                     'issue': f'window {after + 1}..{through} hash mismatch'})
            return (not problems), problems
    except Exception as e:
        return None, [{'issue': str(e)}]
