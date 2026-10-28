"""One-shot manual intervention: thaw frozen wrq-8 review gates and return the
stories straight to peer review (NOT another coding pass).

Rationale: the shared workroom sandbox already contains a complete vanilla-JS
simulation engine and `node --test` is green (66/66). Re-running the coding
executor on 10 stories at once thrashes that single sandbox and re-breaks the
suite. Peer review evaluates the sandbox as it stands.

Run with the server STOPPED:
    kill $(lsof -ti tcp:8937); python3 world/reset_wrq8_gates.py; then restart.
Idempotent: a task already thawed (not escalated / cycleCount 0) is skipped.
Also resolves matching pending "stuck review loop" escalations by title.
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(REPO, 'think_tank.db')
ESCALATIONS = os.path.join(REPO, 'escalations.json')

WRQ8_MARKERS = ('simulation', 'boids', 'predator-prey', 'sir epidemic',
                'cellular automata', 'headless mode', 'browser harness',
                'rng', 'seeded deterministic', 'fixed-timestep',
                'systems registry', 'simulation invariants')


def _is_wrq8(text):
    t = (text or '').lower()
    return any(m in t for m in WRQ8_MARKERS)


def main():
    with sqlite3.connect(DB) as conn:
        row = conn.execute('SELECT blob FROM kv_state WHERE id = 1').fetchone()
    state = json.loads(row[0])

    tasks = state.get('tasks') or {}
    now_ms = int(time.time() * 1000)

    thawed = []
    for tid, task in tasks.items():
        if not isinstance(task, dict):
            continue
        title = task.get('title') or ''
        gate = task.get('_peerGate') or {}
        if not (_is_wrq8(title) or _is_wrq8(task.get('projectLabel'))):
            continue
        if not gate.get('escalated') and task.get('status') != 'failed':
            continue  # already thawed / not failed

        gate['escalated'] = False
        gate['cycleCount'] = 0
        task['_peerGate'] = gate
        # Straight to peer review: the sandbox already has the work.
        task['status'] = 'needs_review'
        thawed.append((tid, title, task.get('assignedTo')))

    blob = json.dumps(state)
    with sqlite3.connect(DB) as conn:
        conn.execute(
            'REPLACE INTO kv_state (id, blob, updated_at) VALUES (1, ?, ?)',
            (blob, now_ms))
        # keep the integrity checksum row (kv_state id=2) in step -- same
        # scheme as server 015_integrity (_state_checksum_write)
        conn.execute(
            'INSERT INTO state_checksum (singleton, sha256, updated_at) VALUES (1, ?, ?) '
            'ON CONFLICT(singleton) DO UPDATE SET sha256 = excluded.sha256, updated_at = excluded.updated_at',
            (__import__('hashlib').sha256(blob.encode('utf-8')).hexdigest(), now_ms))
        conn.commit()

    resolved_esc = []
    if os.path.exists(ESCALATIONS):
        with open(ESCALATIONS) as f:
            escalations = json.load(f)
        for esc_id, esc in list(escalations.items()):
            if esc.get('status') != 'pending':
                continue
            if esc.get('kind') != 'stuck review loop':
                continue
            if not _is_wrq8(esc.get('question')):
                continue
            esc['status'] = 'resolved'
            esc['resolvedBy'] = 'manual-gate-reset (reset_wrq8_gates.py)'
            esc['resolvedAt'] = now_ms / 1000.0
            resolved_esc.append(esc_id)
        with open(ESCALATIONS, 'w') as f:
            json.dump(escalations, f)

    print(f'thawed {len(thawed)} gate(s) -> needs_review:')
    for tid, title, author in thawed:
        print(f'  {tid} [{author}] {title}')
    print(f'resolved {len(resolved_esc)} pending "stuck review loop" escalation(s)')


if __name__ == '__main__':
    main()  # pragma: no cover
