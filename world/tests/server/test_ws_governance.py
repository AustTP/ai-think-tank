
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import asyncio
import threading
import types

import core.governance as governance
import server.ws as ws


def test_ws_ingest_errors(monkeypatch):
    import serve
    monkeypatch.setattr(serve, '_WS_BUFFERS', {})
    monkeypatch.setattr(serve, '_WS_FEEDS', {})
    monkeypatch.setattr(serve, '_WS_LOCK', threading.RLock())
    monkeypatch.setattr(ws, '_WS_CHECKPOINT_AT', {})
    fake = types.ModuleType('pulse')
    fake.observe = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x'))
    monkeypatch.setitem(__import__('sys').modules, 'pulse', fake)
    ws._ws_ingest('s', 't', {'x': 1}, 'r')
    monkeypatch.setattr(serve, 'set_collector_checkpoint', lambda *a: (_ for _ in ()).throw(RuntimeError('x')))
    ws._WS_CHECKPOINT_AT.clear()
    ws._ws_ingest('s', 't', {'x': 2}, 'r')


def test_ws_collector_checkpoint(monkeypatch):
    import serve
    monkeypatch.setattr(serve, '_WS_BUFFERS', {})
    monkeypatch.setattr(serve, '_WS_FEEDS', {'s': {'ws': {'url': 'u'}}})
    monkeypatch.setattr(serve, '_WS_LOCK', threading.RLock())
    monkeypatch.setattr(serve, 'get_collector_checkpoint', lambda s: {'ts': 0})
    monkeypatch.setattr(serve, '_api_credential_value', lambda spec: ('', None, 'bad'))
    async def sleep(_): raise asyncio.CancelledError
    monkeypatch.setattr(ws.asyncio, 'sleep', sleep)
    try:
        asyncio.run(ws._ws_collector_loop('s'))
    except (asyncio.CancelledError, Exception):
        pass
    # exception path
    monkeypatch.setattr(serve, 'get_collector_checkpoint', lambda s: (_ for _ in ()).throw(RuntimeError('x')))
    try:
        asyncio.run(ws._ws_collector_loop('s'))
    except (asyncio.CancelledError, Exception):
        pass


def test_governance_tail(monkeypatch):
    # import failure via builtins
    import builtins
    real = builtins.__import__
    def bad(name, *a, **k):
        if name == 'sim': raise ImportError('x')
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', bad)
    assert governance.supervisor_tick({'tasks': {'a': {'id': 'a', 'status': 'failed'}},
                                       'automation': {'mode': 'run'}}, now_ms=1) == 0


def test_governance_branches(monkeypatch):
    import sys, types
    fake = types.ModuleType('sim')
    fake._task_owner_director = lambda s, t: t.get('_owner', '')
    fake.on_call_agent = lambda *a: 'w'
    fake.queue_work = lambda s, q: None
    monkeypatch.setitem(sys.modules, 'sim', fake)
    def tick(task, levels):
        state = {'tasks': {'a': dict(task, id='a')}, 'automation': {'mode': 'run'},
                 'lastSupervisorScanAt': 0, 'directorSupervision': levels}
        return governance.supervisor_tick(state, now_ms=100000)
    assert tick({'status': 'failed', 'updatedAt': 95000, 'taskType': 'bug'}, {}) == 0
    assert tick({'status': 'failed', 'updatedAt': 95000, 'blockerBugId': 'x'}, {}) == 0
    assert tick({'status': 'failed', 'updatedAt': 95000, '_owner': ''}, {}) == 0
    assert tick({'status': 'failed', 'updatedAt': 95000, 'teamId': 't'}, {'t': {'_default': 2}}) == 0
    assert tick({'status': 'failed', 'updatedAt': 95000, 'teamId': 't'}, {'t': {'_default': 3}}) == 0
