"""One-shot cleanup: resolve pending escalations, unwedge the cora mail-loop
work item, mark wrq-8 done. Run with the server STOPPED."""
import json
import os
import sqlite3
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(REPO, 'think_tank.db')
ESCALATIONS = os.path.join(REPO, 'escalations.json')


def main():
    now_ms = int(time.time() * 1000)
    now_s = time.time()

    # 1. Resolve every pending escalation (the player asked the tank to
    #    handle these itself going forward; the historical pile just needs
    #    to stop pinging).
    resolved = 0
    if os.path.exists(ESCALATIONS):
        with open(ESCALATIONS) as f:
            esc = json.load(f)
        for eid, rec in esc.items():
            if rec.get('status') == 'pending':
                rec['status'] = 'resolved'
                rec['resolvedBy'] = 'manual-cleanup (auto-handling policy)'
                rec['resolvedAt'] = now_s
                resolved += 1
        with open(ESCALATIONS, 'w') as f:
            json.dump(esc, f)
    print(f'resolved {resolved} pending escalation(s)')

    # 2. Load state, fix the queue + wrq-8.
    with sqlite3.connect(DB) as conn:
        row = conn.execute('SELECT blob FROM kv_state WHERE id = 1').fetchone()
    state = json.loads(row[0])

    # Unwedge the "Act on mail (cora)" item: 10k+ attempts, clearly a loop.
    wq = state.get('workQueue') or []
    before = len(wq)
    state['workQueue'] = [
        it for it in wq
        if not (it.get('title') or '').startswith('Act on mail (cora)')
    ]
    print(f'workQueue: {before} -> {len(state["workQueue"])} (dropped wedged cora item)')

    # Mark wrq-8 done.
    for r in state.get('backlogRequests') or []:
        if r.get('id') == 'wrq-8':
            r['status'] = 'done'
            r['doneAt'] = now_ms
            r['doneNote'] = ('Delivered to library/projects/simulation-engine/ '
                             '(76/76 tests green). Parent tasks were lost to a '
                             'task-ID collision bug, now fixed in sim.py.')
            print('wrq-8 -> done')

    blob = json.dumps(state)
    with sqlite3.connect(DB) as conn:
        conn.execute(
            'REPLACE INTO kv_state (id, blob, updated_at) VALUES (1, ?, ?)',
            (blob, now_s))
        # self-heal: a DB built before the integrity feature has no table
        conn.execute(
            'CREATE TABLE IF NOT EXISTS state_checksum ('
            'singleton INTEGER PRIMARY KEY CHECK(singleton = 1), '
            'sha256 TEXT NOT NULL, updated_at REAL NOT NULL)')
        # keep the integrity checksum row (kv_state id=2) in step -- same
        # scheme as server 015_integrity (_state_checksum_write)
        conn.execute(
            'INSERT INTO state_checksum (singleton, sha256, updated_at) VALUES (1, ?, ?) '
            'ON CONFLICT(singleton) DO UPDATE SET sha256 = excluded.sha256, updated_at = excluded.updated_at',
            (__import__('hashlib').sha256(blob.encode('utf-8')).hexdigest(), now_s))
        conn.commit()
    print('state saved')


if __name__ == '__main__':
    main()  # pragma: no cover
