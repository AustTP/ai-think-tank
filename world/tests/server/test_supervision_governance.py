
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import asyncio
import types

import core.governance as governance
import server.supervision_api as supervision_api


class Req:
    cookies = {}
    headers = {}
    async def json(self): return {}
class BadReq(Req):
    async def json(self): raise ValueError('bad')
class Body(Req):
    def __init__(self, d): self.d = d
    async def json(self): return self.d


def test_supervision(monkeypatch):
    import serve
    monkeypatch.setattr(serve, 'verify_session', lambda c: False)
    monkeypatch.setattr(serve, '_resolve_requester', lambda r: 'a')
    monkeypatch.setattr(serve, 'verify_agent_key', lambda a, k: False)
    assert supervision_api._caller(Req()) is None
    monkeypatch.setattr(serve, 'verify_agent_key', lambda a, k: True)
    assert supervision_api._caller(Req()) == 'a'
    assert asyncio.run(supervision_api.get_supervision(Req())).status_code == 200
    assert asyncio.run(supervision_api.put_supervision(BadReq())).status_code == 400
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: {'agentRoster': [{'id': 'a'}]})
    monkeypatch.setattr(governance, 'is_admin', lambda s, a: False)
    monkeypatch.setattr(governance, 'is_director', lambda s, a: False)
    assert asyncio.run(supervision_api.put_supervision(Body({'level': 2}))).status_code == 400
    monkeypatch.setattr(governance, 'is_director', lambda s, a: True)
    monkeypatch.setattr(governance, 'set_supervision_level', lambda *a: (_ for _ in ()).throw(ValueError('bad')))
    assert asyncio.run(supervision_api.put_supervision(Body({'level': 2}))).status_code == 400
    monkeypatch.setattr(governance, 'set_supervision_level', lambda *a: {'_default': 2})
    monkeypatch.setattr(serve, 'save_state_to_db', lambda s: None)
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: None)
    assert asyncio.run(supervision_api.put_supervision(Body({'level': 2}))).status_code == 200
    monkeypatch.setattr(supervision_api, '_caller', lambda r: None)
    assert asyncio.run(supervision_api.get_supervision(Req())).status_code == 401
    assert asyncio.run(supervision_api.put_supervision(Req())).status_code == 401


def test_governance_missing():
    # import failure path
    import sys
    old = sys.modules.get('sim')
    sys.modules['sim'] = None
    try:
        assert governance.supervisor_tick({'tasks': {'a': {'id': 'a', 'status': 'failed'}},
                                           'automation': {'mode': 'run'}}, now_ms=1) == 0
    finally:
        if old is None: sys.modules.pop('sim', None)
        else: sys.modules['sim'] = old
