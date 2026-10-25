import json
import os
import time

import integrations.notify as notify
import core.theo_policy as theo_policy


def test_notify_escalation_file_edges(tmp_path, monkeypatch):
    import serve
    p = tmp_path / 'esc.json'
    monkeypatch.setattr(serve, 'ESCALATIONS_PATH', str(p))
    p.write_text('not json')
    assert notify._load_escalations() == {}
    p.write_text(json.dumps({'e1': {'status': 'pending'}}))
    assert notify._load_escalations()['e1']['status'] == 'pending'
    # merge missing + disk decision over pending
    p.write_text(json.dumps({'a': {'status': 'pending'}, 'b': {'status': 'approved'}}))
    data = {'b': {'status': 'pending'}}
    notify._save_escalations(data)
    assert data['a']['status'] == 'pending' and data['b']['status'] == 'approved'
    # cleanup failure
    real_remove = os.remove
    monkeypatch.setattr(os, 'remove', lambda p: (_ for _ in ()).throw(OSError('x')))
    notify._save_escalations({'x': {}})
    monkeypatch.setattr(os, 'remove', real_remove)


def test_notify_chunks_and_agent_id(monkeypatch):
    assert notify._telegram_chunks('x' * 9000) == ['x' * 4000, 'x' * 4000, 'x' * 1000]
    import serve
    seen = {}
    monkeypatch.setattr(serve, '_load_escalations', lambda: {})
    monkeypatch.setattr(serve, '_save_escalations', lambda d: seen.update(d))
    monkeypatch.setattr(notify, 'notify_human_of_escalation', lambda *a, **k: None)
    monkeypatch.setattr(notify, '_ESCALATION_KIND_GUIDANCE', {})
    notify.create_escalation('kind', 'q', agent_id='a')
    assert next(iter(seen.values()))['agentId'] == 'a'


def test_theo_persist_edges(tmp_path):
    p = tmp_path / 't.json'
    policy = theo_policy.TheoPolicy(storage_path=str(p), clock=lambda: 10**9)
    a = theo_policy.PendingApproval('a', 'r', 'owner', 'x', {}, 0)
    a.status = 'approved'
    policy._approvals['a'] = a
    policy._approved.add('a')
    policy._persist()
    assert policy._approvals == {}
    # no path and invalid JSON
    assert theo_policy.TheoPolicy().load_persisted() is None
    p.write_text('bad')
    policy.load_persisted()
    # invalid row
    p.write_text(json.dumps({'approvals': [{'not': 'valid'}]}))
    policy.load_persisted()
    # persist failure
    monkeypatch = __import__('pytest').MonkeyPatch()
    monkeypatch.setattr(theo_policy, 'open', lambda *a, **k: (_ for _ in ()).throw(OSError('x')), raising=False)
    policy._persist()
    monkeypatch.undo()


def test_notify_more(tmp_path, monkeypatch):
    import serve
    monkeypatch.setattr(serve, 'ESCALATIONS_PATH', str(tmp_path / 'nope.json'))
    assert notify._load_escalations() == {}
    p = tmp_path / 'e.json'; p.write_text('bad')
    monkeypatch.setattr(serve, 'ESCALATIONS_PATH', str(p))
    notify._save_escalations({'x': {}})
    # remove failure
    monkeypatch.setattr(os.path, 'exists', lambda p: True)
    monkeypatch.setattr(os, 'remove', lambda p: (_ for _ in ()).throw(OSError('x')))
    notify._save_escalations({'y': {}})
    monkeypatch.undo()
    # dedupe
    monkeypatch.setattr(serve, '_load_escalations', lambda: {'e': {'status': 'pending', 'kind': 'k', 'question': 'q'}})
    monkeypatch.setattr(serve, '_save_escalations', lambda d: None)
    assert notify.create_escalation('k', 'q') == 'e'


def test_theo_more(tmp_path, monkeypatch):
    p = tmp_path / 't.json'
    policy = theo_policy.TheoPolicy(storage_path=str(p))
    policy._persist()
    p.write_text(json.dumps({'approvals': [
        {'approval_id': 'a', 'requester_id': 'r', 'role': 'owner', 'action': 'x',
         'status': 'approved'}]}))
    policy.load_persisted()
    assert 'a' in policy._approved
    # remove failure on persist
    monkeypatch.setattr(theo_policy.os.path, 'exists', lambda p: True)
    monkeypatch.setattr(theo_policy.os, 'remove', lambda p: (_ for _ in ()).throw(OSError('x')))
    policy._persist()
