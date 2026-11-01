"""MCP edge (2026-11-01): POST /mcp -- the board as an MCP server.

The other kanzo idea worth stealing: let agents OUTSIDE the think tank (a
Claude Code session, a scratch CLI agent) read the board through the Model
Context Protocol instead of scraping the House UI. The think tank already
SPEAKS MCP as a client (integrations/mcp_client.py); this is the same
protocol, server side, hand-rolled to match the repo's no-SDK discipline.

Posture (read-only v1, fail closed):
- POST /mcp answers initialize / ping / tools/list / tools/call, stateless:
  every request is independent, no session id is issued or required.
- The ONLY tools are read-only projections of the same state the House board
  shows (board_overview, list_cards, card_detail). Nothing here moves a
  card: mutating paths stay behind the sim's guards, ceremonies and
  peer-review gates. An external agent can SEE the board, not steer it.
- Auth is a bearer token from THINK_TANK_MCP_TOKEN. If the token is unset
  or empty the surface is DISABLED (503) -- no token, no edge, no partial
  exposure. Every accepted call is logged to the action log so the edge is
  auditable like every other entry point.
"""
from __future__ import annotations

import hmac
import json
import os

from fastapi import Request
from fastapi.responses import JSONResponse

_JSONRPC_OK = 200


def _mcp_token():
    return (os.environ.get('THINK_TANK_MCP_TOKEN') or '').strip()


def _card_projection(task, include_note=False):
    out = {
        'taskId': task.get('id') or '',
        'title': task.get('title') or '',
        'status': task.get('status') or '',
        'assignedTo': task.get('assignedTo') or '',
        'teamId': task.get('teamId') or '',
        'room': task.get('room') or '',
        'taskType': task.get('taskType') or '',
        'lane': task.get('lane') or '',
        'phase': task.get('phase') or '',
        'priority': task.get('priority') or '',
    }
    if include_note:
        out['note'] = (task.get('note') or '')[:400]
        out['acceptanceCriteria'] = task.get('acceptanceCriteria') or ''
        out['reportPath'] = task.get('reportPath') or ''
    return out


def _open_set(state):
    return [t for t in (state.get('tasks') or {}).values()
            if isinstance(t, dict) and str(t.get('status') or '')
            not in ('done', 'cancelled', 'abandoned')]


def _tool_board_overview(state, _args):
    tasks = state.get('tasks') or {}
    open_tasks = _open_set(state)
    counts = {
        'inProgress': sum(1 for t in open_tasks
                          if t.get('status') in ('walking', 'working', 'repairing')),
        'awaitingReview': sum(1 for t in open_tasks if t.get('status') == 'needs_review'),
        'parked': sum(1 for t in open_tasks if (t.get('lane') or '') == 'parking-lot'),
        'done': sum(1 for t in tasks.values()
                    if isinstance(t, dict) and t.get('status') == 'done'),
        'queuedCards': len([i for i in (state.get('workQueue') or [])
                            if (i.get('lane') or '') != 'parking-lot']),
        'parkedCards': len([i for i in (state.get('workQueue') or [])
                            if (i.get('lane') or '') == 'parking-lot']),
    }
    pending = [r for r in (state.get('backlogRequests') or [])
               if isinstance(r, dict) and r.get('status') == 'pending']
    return {
        'counts': counts,
        'pendingLargeRequests': [{'id': r.get('id'), 'goal': r.get('goal'),
                                  'teamId': r.get('teamId')} for r in pending[:10]],
    }


def _tool_list_cards(state, args):
    status = (args.get('status') or 'open').strip().lower()
    team = (args.get('team') or '').strip()
    limit = max(1, min(int(args.get('limit') or 25), 100))
    if status == 'open':
        rows = _open_set(state)
    elif status == 'done':
        rows = [t for t in (state.get('tasks') or {}).values()
                if isinstance(t, dict) and t.get('status') == 'done']
    else:
        rows = [t for t in (state.get('tasks') or {}).values()
                if isinstance(t, dict) and t.get('status') == status]
    if team:
        rows = [t for t in rows
                if team in (str(t.get('teamId') or ''), str(t.get('assignedTo') or ''))]
    rows.sort(key=lambda t: int(t.get('updatedAt') or t.get('createdAt') or 0),
              reverse=True)
    return {'count': len(rows),
            'cards': [_card_projection(t) for t in rows[:limit]]}


def _tool_card_detail(state, args):
    task_id = (args.get('taskId') or '').strip()
    task = (state.get('tasks') or {}).get(task_id)
    if not isinstance(task, dict):
        return {'error': f'no task {task_id}'}
    return _card_projection(task, include_note=True)


_MCP_TOOLS = [
    {'name': 'board_overview',
     'description': 'Counts of in-progress, awaiting-review, parked and done cards, '
                    'plus pending large requests. Read-only view of the think tank board.',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'list_cards',
     'description': 'List board cards with filters. status: open (default) | done | '
                    'needs_review | working; optional team filter (team id or agent id). '
                    'Read-only.',
     'inputSchema': {'type': 'object',
                     'properties': {'status': {'type': 'string'},
                                    'team': {'type': 'string'},
                                    'limit': {'type': 'integer', 'minimum': 1,
                                              'maximum': 100}}}},
    {'name': 'card_detail',
     'description': 'One card by taskId, including the worker note, acceptance '
                    'criteria and report path. Read-only.',
     'inputSchema': {'type': 'object', 'required': ['taskId'],
                     'properties': {'taskId': {'type': 'string'}}}},
]

_MCP_DISPATCH = {
    'board_overview': _tool_board_overview,
    'list_cards': _tool_list_cards,
    'card_detail': _tool_card_detail,
}


def _mcp_result(call_id, payload):
    return {'jsonrpc': '2.0', 'id': call_id,
            'result': {'content': [{'type': 'text', 'text': json.dumps(payload)}]}}


def _mcp_error(call_id, code, message):
    return {'jsonrpc': '2.0', 'id': call_id,
            'error': {'code': code, 'message': message}}


def _mcp_handle(body):
    method = body.get('method') or ''
    call_id = body.get('id')
    params = body.get('params') or {}
    if method == 'initialize':
        # initialize and tools/list return STRUCTURED results (the object
        # itself, not a content block) per the MCP spec; only tools/call
        # wraps its payload in content[] text.
        return JSONResponse({'jsonrpc': '2.0', 'id': call_id, 'result': {
            'protocolVersion': params.get('protocolVersion') or '2024-11-05',
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'think-tank-board', 'version': '1.0.0'},
        }}, status_code=_JSONRPC_OK)
    if method == 'ping':
        return JSONResponse(_mcp_result(call_id, {}), status_code=_JSONRPC_OK)
    if method == 'tools/list':
        return JSONResponse({'jsonrpc': '2.0', 'id': call_id,
                             'result': {'tools': _MCP_TOOLS}},
                            status_code=_JSONRPC_OK)
    if method == 'tools/call':
        name = params.get('name') or ''
        handler = _MCP_DISPATCH.get(name)
        if handler is None:
            return JSONResponse(_mcp_error(call_id, -32602, f'unknown tool {name}'),
                                status_code=_JSONRPC_OK)
        try:
            payload = handler(get_state_from_db() or {},
                              params.get('arguments') or {})
        except Exception as exc:  # a read tool must never 500 the edge
            payload = {'error': str(exc)}
        return JSONResponse(_mcp_result(call_id, payload), status_code=_JSONRPC_OK)
    if method.startswith('notifications/'):
        # notifications/initialized and friends: no response body per spec.
        return JSONResponse({}, status_code=202)
    return JSONResponse(_mcp_error(call_id if call_id is not None else 0,
                                    -32601, 'method not supported'),
                        status_code=_JSONRPC_OK)


@app.post('/mcp')
async def api_mcp_edge(request: Request):
    token = _mcp_token()
    if not token:
        return JSONResponse({'error': 'MCP edge disabled (THINK_TANK_MCP_TOKEN unset)'},
                            status_code=503)
    supplied = (request.headers.get('authorization') or '').removeprefix('Bearer ').strip()
    if not supplied or not hmac.compare_digest(supplied, token):
        log_action(None, 'mcp_edge_auth_failed', {'ip': request.client.host if request.client else ''})
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'invalid JSON body'}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({'error': 'invalid JSON-RPC body'}, status_code=400)
    method = str(body.get('method') or '')
    if method == 'tools/call':
        log_action(None, 'mcp_edge_call',
                   {'tool': (body.get('params') or {}).get('name'),
                    'ip': request.client.host if request.client else ''})
    return _mcp_handle(body)
