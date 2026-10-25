import asyncio
import os
import sys
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402


def _state(role):
    rec = {'id': 'nora', 'name': 'Nora', 'role': role,
           'isDirector': role == 'director'}
    return {
        'agentRoster': [rec],
        'agents': {'nora': {'name': 'Nora', 'busy': False, 'offDuty': False,
                            'role': role, 'profile': {'mission': 'review the code'}}},
    }


def _tools_for(role):
    seen = {}
    def fake_loop(model, messages, tools, execute_tool, *args, **kwargs):
        seen['tools'] = [t['function']['name'] for t in tools]
        return 'ok', []
    with unittest.mock.patch.object(serve, '_resolve_model_tier', return_value='m'), \
         unittest.mock.patch.object(serve, 'get_or_create_agent_key', return_value='k'), \
         unittest.mock.patch.object(serve, '_make_web_tools_executor', return_value=lambda *a: ''), \
         unittest.mock.patch.object(serve, '_call_agent_tool_loop', side_effect=fake_loop), \
         unittest.mock.patch.object(serve, 'memory_context', return_value=None), \
         unittest.mock.patch.object(sim, '_eligible_candidates', return_value=['nora']):
        seen['result'] = asyncio.run(serve._ask_core(_state(role), 'review this', 'nora'))
    assert 'tools' in seen, seen.get('result')
    return seen['tools']


def test_director_ask_can_read_live_project():
    assert 'live_read' in _tools_for('director')


def test_worker_ask_cannot_read_live_project():
    assert 'live_read' not in _tools_for('worker')
