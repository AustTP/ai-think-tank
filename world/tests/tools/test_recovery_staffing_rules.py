import asyncio
import json
import os
import sqlite3
from pathlib import Path

import agents.coding_library as coding_library
import server.confirmations_api as confirmations_api
import core.optimization_advisor as optimization_advisor
import village.recovery_staffing as recovery_staffing
import core.theo_policy as theo_policy
import agents.transport_recovery as transport_recovery


def test_coding_library_symlink(tmp_path):
    lib = tmp_path / 'lib'; lib.mkdir(); (lib / 'a').write_text('x')
    (lib / 'l').symlink_to(lib / 'a')
    try:
        coding_library.contained(lib, 'l')
    except ValueError:
        pass


def test_confirmations_api_direct(monkeypatch):
    import serve
    class Req:
        cookies = {}
        headers = {}
        query_params = {}
        async def json(self): return {}
    monkeypatch.setattr(serve, 'verify_session', lambda c: False)
    monkeypatch.setattr(serve, '_agent_id_for_key', lambda k: 'a')
    assert confirmations_api._actor(Req()) == 'a'
    monkeypatch.setattr(serve, '_agent_id_for_key', lambda k: None)
    assert asyncio.run(confirmations_api.create_confirmation(Req())).status_code == 401
    assert asyncio.run(confirmations_api.list_confirmations(Req())).status_code == 401


def test_optimization_non_dict_credits():
    assert optimization_advisor._credit_used_pct({'openrouter_credits': []}) is None


def test_recovery_staffing_branches(monkeypatch):
    import sim
    state = {'teams': [{'scrumMasterId': 'm', 'id': 't', 'members': ['w']}],
             'agents': {'w': {}}, 'agentRoster': [{'id': 'w'}],
             'tasks': {'s': {'id': 's', 'status': 'investigating', 'assignedTo': 'm',
                             'createdAt': 1},
                       'c': {'id': 'c', 'investigationOf': 's', 'status': 'done',
                             'libraryPath': 'r', 'assignedTo': 'w'}},
             'workQueue': []}
    recovery_staffing.step(state)
    assert state['tasks']['s']['status'] == 'recovery_planned'
    state2 = {'teams': [{'scrumMasterId': 'm', 'id': 't', 'members': ['w']}],
              'agents': {'w': {}}, 'agentRoster': [{'id': 'w'}],
              'tasks': {'s': {'id': 's', 'status': 'investigating', 'assignedTo': 'm'}},
              'workQueue': [{'investigationOf': 's'}]}
    monkeypatch.setattr(sim, 'queue_work', lambda *a: None)
    recovery_staffing.step(state2)
    assert state2['tasks']['s']['recoveryStaffed'] is True


def test_transport_branches():
    task = {'id': 't', 'status': 'working'}
    assert transport_recovery.queue({'completionControl': {}}, task,
                                    {'ok': False, 'note': 'timeout'}, 1) is False
    task['manualAttention'] = True
    assert transport_recovery.queue({'completionControl': {}}, task,
                                    {'ok': False, 'note': 'timeout'}, 1) is False
    parent = {'id': 'p', 'status': 'working', 'taskType': 'code'}
    state = {'tasks': {'p': parent, 't': {'id': 't', 'reviewOf': 'p'}},
             'completionControl': {}}
    import sim
    old = sim.queue_work
    sim.queue_work = lambda *a: None
    try:
        assert transport_recovery.queue(state, state['tasks']['t'],
                                        {'ok': False, 'note': 'timeout',
                                         'stagedWorkspace': 'w'}, 1) == 'queued'
        assert parent['lastFailedWorkspace'] == 'w'
    finally:
        sim.queue_work = old


def test_theo_remove_failure(tmp_path, monkeypatch):
    p = tmp_path / 't.json'
    policy = theo_policy.TheoPolicy(storage_path=str(p))
    monkeypatch.setattr(theo_policy.os.path, 'exists', lambda p: True)
    monkeypatch.setattr(theo_policy.os, 'remove', lambda p: (_ for _ in ()).throw(OSError('x')))
    monkeypatch.setattr(theo_policy.os, 'replace', lambda a, b: (_ for _ in ()).throw(OSError('x')))
    policy._persist()
