import asyncio
import json
import time

import server.feedback_api as feedback_api
import server.webhooks as webhooks


class Req:
    cookies = {}
    headers = {}
    query_params = {}
    async def json(self): return {}
class BadReq(Req):
    async def json(self): raise ValueError('bad')


def test_feedback_helpers(monkeypatch):
    assert feedback_api._task_map({'tasks': {'a': {}}}) == {'a': {}}
    assert feedback_api._task_map({'tasks': [{'id': 'a'}, 'x']}) == {'a': {'id': 'a'}}
    assert feedback_api._task_map({}) == {}
    assert feedback_api._text({'summary': 's'}) == 's'
    now = time.time() * 1000
    state = {'tasks': {'a': {'status': 'working'}, 'b': {'status': 'done', 'updatedAt': now},
                       'c': {'status': 'done', 'updatedAt': 0}, 'd': 'x'}}
    items = feedback_api._work_items(state)
    assert [i['id'] for i in items] == ['a', 'b']


def test_feedback_endpoints(monkeypatch):
    import serve
    monkeypatch.setattr(serve, 'verify_session', lambda c: False)
    assert asyncio.run(feedback_api.work_items(Req())).status_code == 401
    assert asyncio.run(feedback_api.list_feedback(Req())).status_code == 401
    assert asyncio.run(feedback_api.add_feedback(Req())).status_code == 401
    assert asyncio.run(feedback_api.update_feedback_status(1, Req())).status_code == 401
    assert asyncio.run(feedback_api.get_taste(Req())).status_code == 401
    assert asyncio.run(feedback_api.put_taste(Req())).status_code == 401
    monkeypatch.setattr(serve, 'verify_session', lambda c: True)
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: {'tasks': {'a': {'status': 'working'}}})
    assert asyncio.run(feedback_api.work_items(Req())).status_code == 200
    class Store:
        def list(self, **k): return []
        def add(self, *a, **k): return 1
        def set_status(self, *a): return None
        def get_taste(self, p): return {'weights': {'bold_calm': 70}}
        def set_taste(self, *a): return {'weights': {'bold_calm': 70}}
    monkeypatch.setattr(feedback_api, '_store', lambda: Store())
    assert asyncio.run(feedback_api.list_feedback(Req())).status_code == 200
    assert asyncio.run(feedback_api.add_feedback(Req())).status_code == 200
    assert asyncio.run(feedback_api.add_feedback(BadReq())).status_code == 400
    assert asyncio.run(feedback_api.update_feedback_status(1, Req())).status_code == 404
    assert asyncio.run(feedback_api.get_taste(Req())).status_code == 200
    assert asyncio.run(feedback_api.put_taste(Req())).status_code == 200


def test_webhook_pure(tmp_path, monkeypatch):
    import serve
    monkeypatch.setattr(serve, 'X_HANDLE', '')
    assert webhooks._webhook_channel_enabled('nope') is False
    assert webhooks._webhook_channel_enabled('x-mention') is False
    monkeypatch.setattr(serve, 'X_HANDLE', 'me')
    assert webhooks.active_webhook_channels() == ('x-mention',)
    assert webhooks._webhook_seen_key({'channel': 'c', 'payload': {'id': 1}}) == 'c:1'
    assert webhooks._webhook_seen_key({'channel': 'c', 'payload': {}}) is None
    assert webhooks._webhook_goal_text({'author': 'a', 'text': 't', 'url': 'u'}) == 'X mention from a -- t [u]'
    assert webhooks._webhook_goal_text({}, 'other') == 'other'
    assert webhooks._webhook_self_mention({'author': 'ME'}) is True
    assert webhooks._webhook_self_mention({'author': 'other'}) is False
    p = tmp_path / 'w.jsonl'
    p.write_text('not json\n' + json.dumps({'channel': 'x-mention', 'payload': {'id': '1'}}) + '\n' + 'partial')
    monkeypatch.setattr(serve, 'WEBHOOKS_PATH', str(p))
    assert webhooks._webhook_seed_seen() == {'x-mention:1'}
    found, offset = webhooks._webhook_read_new(0)
    assert len(found) == 1 and offset < len(p.read_bytes())
    assert webhooks._webhook_read_new(offset)[0] == []


def test_webhook_handle(monkeypatch):
    import serve
    monkeypatch.setattr(serve, 'X_HANDLE', 'me')
    assert asyncio.run(webhooks._webhook_handle_entry({})) == 'dropped'
    assert asyncio.run(webhooks._webhook_handle_entry({'channel': 'x-mention', 'payload': {}})) == 'dropped'
    webhooks._WEBHOOK_SEEN.clear()
    assert asyncio.run(webhooks._webhook_handle_entry({'channel': 'x-mention', 'payload': {'id': '1', 'author': 'me'}})) == 'self'
    webhooks._WEBHOOK_SEEN.clear()
    monkeypatch.setattr(serve, '_state_begin', lambda: None)
    assert asyncio.run(webhooks._webhook_handle_entry({'channel': 'x-mention', 'payload': {'id': '2'}})) == 'no-state'
    webhooks._WEBHOOK_SEEN.clear()
    async def dispatch(state, goal, source):
        return 'staffed', {'teamName': 't', 'request': 'r'}
    monkeypatch.setattr(serve, '_state_begin', lambda: {'tasks': {}})
    monkeypatch.setattr(serve, '_dispatch_big_task', dispatch)
    assert asyncio.run(webhooks._webhook_handle_entry({'channel': 'x-mention', 'payload': {'id': '3'}})) == 'staffed'


def test_feedback_more(monkeypatch):
    # page
    assert asyncio.run(feedback_api.feedback_page()).status_code == 200
    import serve
    monkeypatch.setattr(serve, 'verify_session', lambda c: True)
    class Store:
        def add(self, *a, **k): raise ValueError('bad')
        def set_status(self, *a): raise ValueError('bad')
        def put_taste(self, *a): return {}
        def set_taste(self, *a): raise ValueError('bad')
    monkeypatch.setattr(feedback_api, '_store', lambda: Store())
    assert asyncio.run(feedback_api.add_feedback(Req())).status_code == 400
    assert asyncio.run(feedback_api.update_feedback_status(1, BadReq())).status_code == 400
    assert asyncio.run(feedback_api.update_feedback_status(1, Req())).status_code == 400
    assert asyncio.run(feedback_api.put_taste(BadReq())).status_code == 400


def test_webhook_more(tmp_path, monkeypatch):
    import serve
    monkeypatch.setattr(serve, 'X_HANDLE', 'me')
    assert webhooks._webhook_channel_spec('x-mention')
    assert webhooks._webhook_goal_text({}, 'x-mention') == 'X mention (no text captured)'
    # unknown channel goal
    assert webhooks._webhook_goal_text({'author': 'a'}, 'nope') == 'nope'
    # no own attr
    monkeypatch.setattr(webhooks, 'CHANNELS', {'c': {'goal': {}}})
    assert webhooks._webhook_channel_enabled('c') is True
    assert webhooks._webhook_self_mention({}, 'c') is False
    # no handle
    monkeypatch.setattr(webhooks, 'CHANNELS', {'c': {'own_handle_attr': 'X_HANDLE', 'own_handle_fields': ('a',)}})
    monkeypatch.setattr(serve, 'X_HANDLE', '')
    assert webhooks._webhook_self_mention({'a': 'x'}, 'c') is True
    monkeypatch.undo()
    # seed blank and errors
    monkeypatch.setattr(serve, 'WEBHOOKS_PATH', str(tmp_path / 'none'))
    assert webhooks._webhook_seed_seen() == set()
    p = tmp_path / 'w'; p.write_text('\nnot json\n')
    monkeypatch.setattr(serve, 'WEBHOOKS_PATH', str(p))
    assert webhooks._webhook_seed_seen() == set()
    # read no file
    assert webhooks._webhook_read_new(0)[0] == []
    # seen and dispatch exception
    webhooks._WEBHOOK_SEEN.clear()
    webhooks._WEBHOOK_SEEN.add('x-mention:1')
    assert asyncio.run(webhooks._webhook_handle_entry({'channel': 'x-mention', 'payload': {'id': '1'}})) == 'seen'
    webhooks._WEBHOOK_SEEN.clear()
    async def boom(*a):
        raise RuntimeError('x')
    monkeypatch.setattr(serve, '_state_begin', lambda: {'tasks': {}})
    monkeypatch.setattr(serve, 'X_HANDLE', 'me')
    monkeypatch.setattr(serve, '_dispatch_big_task', boom)
    assert asyncio.run(webhooks._webhook_handle_entry({'channel': 'x-mention', 'payload': {'id': '2'}})) == 'failed'
    # poll loop one iteration
    monkeypatch.setattr(webhooks, '_webhook_read_new', lambda o: ([], o))
    async def stop(_): raise KeyboardInterrupt
    monkeypatch.setattr(webhooks.asyncio, 'sleep', stop)
    try:
        asyncio.run(webhooks._webhook_poll_loop())
    except KeyboardInterrupt:
        pass


def test_feedback_store_default(tmp_path, monkeypatch):
    import serve
    monkeypatch.setattr(serve, 'DB_PATH', str(tmp_path / 'f.db'))
    assert feedback_api._store().path == str(tmp_path / 'f.db')


def test_webhook_final(tmp_path, monkeypatch):
    import serve
    monkeypatch.setattr(serve, 'X_HANDLE', 'me')
    monkeypatch.setattr(webhooks, 'open', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')), raising=False)
    monkeypatch.setattr(serve, 'WEBHOOKS_PATH', str(tmp_path / 'x'))
    assert webhooks._webhook_seed_seen() == set()
    monkeypatch.undo()
    monkeypatch.setattr(serve, 'WEBHOOKS_PATH', str(tmp_path / 'missing'))
    assert webhooks._webhook_read_new(0) == ([], 0)
    # outcomes
    monkeypatch.setattr(serve, 'X_HANDLE', 'me')
    monkeypatch.setattr(serve, '_state_begin', lambda: {'tasks': {}})
    for outcome, data in (('backlogged', {'feature': {'id': 'f'}}),
                          ('new_team', {'id': 't'}), ('other', {})):
        async def dispatch(state, goal, source, o=outcome, d=data):
            return o, d
        monkeypatch.setattr(serve, '_dispatch_big_task', dispatch)
        webhooks._WEBHOOK_SEEN.clear()
        assert asyncio.run(webhooks._webhook_handle_entry({'channel': 'x-mention', 'payload': {'id': outcome}})) == outcome
    # poll loop with one entry
    entry = {'channel': 'x-mention', 'payload': {'id': 'p'}}
    monkeypatch.setattr(webhooks, '_webhook_read_new', lambda o: ([{'entry': entry, 'end': 1}], 1))
    async def stop(_): raise KeyboardInterrupt
    monkeypatch.setattr(webhooks.asyncio, 'sleep', stop)
    try:
        asyncio.run(webhooks._webhook_poll_loop())
    except KeyboardInterrupt:
        pass


def test_feedback_not_found(monkeypatch):
    import serve
    monkeypatch.setattr(serve, 'verify_session', lambda c: True)
    class Store:
        def set_status(self, *a): return None
    monkeypatch.setattr(feedback_api, '_store', lambda: Store())
    assert asyncio.run(feedback_api.update_feedback_status(9, Req())).status_code == 404


def test_webhook_loop_final(monkeypatch):
    import serve
    monkeypatch.setattr(serve, 'X_HANDLE', 'me')
    entries = [{'entry': {'channel': 'other'}, 'end': 1},
               {'entry': {'channel': 'x-mention'}, 'end': 2}]
    monkeypatch.setattr(webhooks, '_webhook_read_new', lambda o: (entries, 2))
    monkeypatch.setattr(webhooks, '_webhook_channel_enabled', lambda c: False)
    calls = {'n': 0}
    async def sleep(_):
        calls['n'] += 1
        if calls['n'] > 1: raise KeyboardInterrupt
    monkeypatch.setattr(webhooks.asyncio, 'sleep', sleep)
    try:
        asyncio.run(webhooks._webhook_poll_loop())
    except KeyboardInterrupt:
        pass
    monkeypatch.setattr(webhooks, '_webhook_read_new', lambda o: (_ for _ in ()).throw(RuntimeError('x')))
    async def sleep2(_): raise KeyboardInterrupt
    monkeypatch.setattr(webhooks.asyncio, 'sleep', sleep2)
    try:
        asyncio.run(webhooks._webhook_poll_loop())
    except KeyboardInterrupt:
        pass
