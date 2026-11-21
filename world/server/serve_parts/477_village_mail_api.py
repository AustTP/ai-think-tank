"""Cross-village mail API: send mail into another village's durable queue.

Operator/agent-facing write path for the village sharding seam
(118_village_shards). Delivery is NOT immediate: the mail rides
state['villageMail'] until the global tick's relay pass claims it for the
target village and files it into the recipient's mailbox there. That is the
whole point -- the sender never touches another village's agents directly,
which is the boundary a process-per-village split needs.

Auth: _operator_gate (a valid player session, or a director/admin agent) --
cross-village writes are a leadership act, not worker chatter. Worker
cross-village requests should go through their director.
"""
import time

from fastapi import Request
from fastapi.responses import JSONResponse


@app.post('/api/villages/mail')
async def villages_mail_endpoint(request: Request):
    """Send one cross-village mail. Body: {toVillage, toAgent?, kind?,
    text?|mail?}. Returns {id, status:'queued'} when accepted; 404 for an
    unknown target village; 400 for missing fields."""
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'body must be JSON'}, status_code=400)
    body = body if isinstance(body, dict) else {}
    to_village = str(body.get('toVillage') or '').strip()
    if not to_village:
        return JSONResponse({'error': 'toVillage is required'}, status_code=400)
    try:
        state = get_state_from_db()
    except Exception:
        state = None
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    from village.sim_helpers import village_of_agent
    actor_village = village_of_agent(state, actor or 'player') if actor \
        and actor != 'player' else 'main'
    mail_id = _village_mail_enqueue(state, actor_village, to_village,
                                    {'toAgent': str(body.get('toAgent') or ''),
                                     'kind': str(body.get('kind') or 'village_mail'),
                                     'about': str(body.get('about') or ''),
                                     'mail': {'kind': str(body.get('kind') or 'village_mail'),
                                              'text': str(body.get('text') or ''),
                                              'fromVillage': actor_village,
                                              'fromAgent': actor or 'player'}},
                                    int(time.time() * 1000))
    if not mail_id:
        return JSONResponse({'error': f'unknown village: {to_village}'},
                            status_code=404)
    save_state_to_db(state)
    log_action(actor or 'player', 'village_mail_sent',
               {'id': mail_id, 'toVillage': to_village,
                'toAgent': body.get('toAgent')}, authorized=True)
    return JSONResponse({'id': mail_id, 'status': 'queued'})


@app.get('/api/villages/mail')
async def villages_mail_list_endpoint(request: Request):
    """The cross-village mail queue: counts by status, plus recent entries.
    Query: ?village=<id>&limit=N (default 50, cap 200)."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        state = get_state_from_db()
    except Exception:
        state = None
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    village = str(request.query_params.get('village') or '').strip()
    try:
        limit = max(1, min(int(request.query_params.get('limit') or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    queue = _village_mail_queue(state)
    counts = {'queued': 0, 'claimed': 0, 'done': 0}
    for m in queue:
        counts[m.get('status')] = counts.get(m.get('status'), 0) + 1
    rows = [m for m in queue
            if not village or m.get('toVillage') == village
            or m.get('fromVillage') == village]
    recent = rows[-limit:]
    recent.reverse()
    return JSONResponse({'counts': counts, 'total': len(rows),
                         'mail': [{'id': m.get('id'),
                                   'from': m.get('fromVillage'),
                                   'to': m.get('toVillage'),
                                   'status': m.get('status'),
                                   'attempts': m.get('attempts'),
                                   'outcome': m.get('outcome'),
                                   'toAgent': (m.get('entry') or {}).get('toAgent')}
                                  for m in recent]})
