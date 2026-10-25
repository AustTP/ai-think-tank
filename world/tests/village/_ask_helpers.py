"""Shared fixtures for the player-ask test modules."""
import json


def _roster():
    return [
        {'id': 'maya', 'name': 'Maya', 'role': 'admin', 'isAdmin': True},
        {'id': 'ben', 'name': 'Ben', 'role': 'engineer'},
        {'id': 'cora', 'name': 'Cora', 'role': 'engineer'},
        {'id': 'dax', 'name': 'Dax', 'role': 'engineer'},
    ]


def _state(**over):
    roster = _roster()
    state = {
        'agentRoster': roster,
        'agents': {a['id']: {'id': a['id'], 'name': a['name'], 'role': a['role'],
                             'offDuty': False, 'busy': False, 'task': None,
                             'pairWith': None, 'profile': {'mission': 'help the think tank'}}
                   for a in roster},
        'workQueue': [],
        'tasks': {},
        'products': {},
        'completedDeliverables': [],
    }
    state.update(over)
    return state


def _search_tool_call(call_id='call_1', query='current facts'):
    return {'id': call_id, 'type': 'function',
            'function': {'name': 'search_web', 'arguments': json_dumps({'query': query})}}


def json_dumps(obj):
    import json
    return json.dumps(obj)
