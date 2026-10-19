"""Local training review API, sharing the CLI's dataset exporter."""
import importlib.util
import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
import training_capture as capture
from training_roles import worker_interaction

router = APIRouter(prefix='/api/training')


def approval_blockers(row, payload, flags):
    reasons = []
    if not worker_interaction(payload, row['agent_id']):
        reasons.append('Only worker interactions are eligible for training approval.')
    if not row['ok']:
        reasons.append('This model call failed or did not return a usable response.')
    if flags:
        reasons.append('This recording has capture flags: ' + ', '.join(flags) +
                       '. Redacted, omitted, or unattributed recordings are excluded from training approval.')
    return reasons


@router.get('/interactions')
def interactions(status: str = 'pending', offset: int = 0, limit: int = 25):
    if status not in ('pending', 'approved', 'rejected', 'all'):
        raise HTTPException(400, 'Invalid review status')
    offset, limit = max(0, offset), min(100, max(1, limit))
    with capture.database() as conn:
        workers = []
        for row in conn.execute('SELECT id,ts,agent_id,task_id,model,ok,flags,review_status,payload FROM interactions WHERE ok=1 ORDER BY ts DESC,id'):
            flags = json.loads(row['flags'])
            if flags:
                continue
            payload = json.loads(row['payload'])
            if not worker_interaction(payload, row['agent_id']):
                continue
            payload, extra = capture.sanitize(payload)
            if approval_blockers(row, payload, sorted(set(flags + extra))):
                continue
            item = dict(row)
            item.pop('payload')
            workers.append(item)
        counts = {s: sum(r['review_status'] == s for r in workers) for s in ('pending', 'approved', 'rejected')}
        matching = [r for r in workers if status == 'all' or r['review_status'] == status]
        total = len(matching)
        rows = matching[offset:offset + limit]
    return {'counts': counts, 'total': total, 'items': [dict(r) for r in rows]}


@router.get('/interactions/{interaction_id}')
def detail(interaction_id: str):
    with capture.database() as conn:
        row = conn.execute('SELECT * FROM interactions WHERE id=?', (interaction_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Recording not found')
        result = dict(row)
        result['payload'], extra = capture.sanitize(json.loads(result['payload']))
        result['flags'] = sorted(set(json.loads(result['flags']) + extra))
        result['approval_blockers'] = approval_blockers(row, result['payload'], result['flags'])
        result['can_approve'] = not result['approval_blockers']
        outcome = conn.execute('SELECT payload FROM task_outcomes WHERE task_id=?', (row['task_id'],)).fetchone()
        result['outcome'] = capture.sanitize(json.loads(outcome[0]))[0] if outcome else None
    return result


@router.post('/interactions/{interaction_id}/review')
async def review(interaction_id: str, request: Request):
    body = await request.json()
    status, note = body.get('status'), body.get('note')
    if status not in ('pending', 'approved', 'rejected') or not isinstance(note, str) or not note.strip() or len(note) > 6000:
        raise HTTPException(400, 'Choose a status and supply a review note (up to 6000 characters)')
    with capture.database() as conn:
        row = conn.execute('SELECT * FROM interactions WHERE id=?', (interaction_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Recording not found')
        payload, extra = capture.sanitize(json.loads(row['payload']))
        blockers = approval_blockers(row, payload, sorted(set(json.loads(row['flags']) + extra)))
        if status == 'approved' and blockers:
            raise HTTPException(400, ' '.join(blockers))
        note = capture.sanitize(note.strip())[0]
        conn.execute('UPDATE interactions SET review_status=?,review_note=? WHERE id=?', (status, note, interaction_id))
        conn.execute('INSERT INTO events(ts,kind,payload) VALUES (?,?,?)', (time.time(), 'training_review', json.dumps({'id': interaction_id, 'status': status, 'note': note})))
    return {'status': status}


@router.post('/export')
def export_dataset(max_tokens: int = 16384):
    if not 256 <= max_tokens <= 32768:
        raise HTTPException(400, 'Token limit must be between 256 and 32768')
    path = capture.ROOT / 'scripts/export_training_interactions.py'
    spec = importlib.util.spec_from_file_location('training_export', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = capture.ROOT / 'datasets/local/interactions/export'
    with capture.database() as conn:
        summary = module.export(conn, out, max_tokens=max_tokens)
    return {'summary': summary, 'path': summary.get('dated_path') or str(out)}
