import json
import types

import server.durable_streams as ds


def _redis(**kw):
    s = {'id': 'r', 'name': 'R', 'description': '', 'backend': 'redis_stream',
         'enabled': True, 'max_message_bytes': 2048, 'url': 'redis://x',
         'url_env': '', 'stream': 's', 'group': 'g', 'consumer': 'c',
         'block_ms': 0, 'max_batch': 20}
    s.update(kw); return s


def _sqs(**kw):
    s = {'id': 'q', 'name': 'Q', 'description': '', 'backend': 'sqs', 'enabled': True,
         'max_message_bytes': 2048, 'queue_url': 'https://q/123', 'region': 'r',
         'profile': None, 'endpoint_url': None, 'visibility_timeout_s': 60,
         'wait_time_s': 0, 'max_messages': 10}
    s.update(kw); return s


def test_normalize_registry(tmp_path, monkeypatch):
    assert ds._clamp_int('x', 3, 1, 5) == 3
    for raw in ('x', {'backend': 'x'}, {'backend': 'redis_stream'},
                {'backend': 'sqs'}):
        try:
            ds._normalize_stream('s', raw)
        except ds.EventStreamError:
            pass
    assert ds._normalize_stream('r', {'url': 'redis://x'})['backend'] == 'redis_stream'
    assert ds._normalize_stream('q', {'backend': 'sqs', 'queue_url': 'u'})['backend'] == 'sqs'
    p = tmp_path / 'event_streams.json'; monkeypatch.setattr(ds, 'REGISTRY_PATH', str(p))
    monkeypatch.setattr(ds, '_CACHE', None)
    assert ds._load_registry() == {}
    p.write_text(json.dumps({'streams': {'r': {'url': 'redis://x', 'enabled': True}}}))
    assert ds.get_stream('r')['id'] == 'r'
    assert 'R' in ds.streams_text()
    p.write_text(json.dumps({'streams': {'q': {'backend': 'sqs', 'queue_url': 'u'}}}))
    ds.reload_streams()
    assert 'sqs queue=u' in ds.streams_text()
    try:
        ds.get_stream('missing')
    except ds.EventStreamError:
        pass
    p.write_text(json.dumps({'streams': {'../bad': {}}}))
    try:
        ds._load_registry()
    except ds.EventStreamError:
        pass
    p.write_text(json.dumps({'streams': []}))
    try:
        ds._load_registry()
    except ds.EventStreamError:
        pass
    p.write_text('x')
    try:
        ds._load_registry()
    except ds.EventStreamError:
        pass


def test_clients_and_parsing(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'redis', None)
    try:
        ds._redis_client(_redis())
    except ds.EventStreamError:
        pass
    monkeypatch.setitem(sys.modules, 'boto3', None)
    try:
        ds._sqs_client(_sqs())
    except ds.EventStreamError:
        pass
    fake_redis = types.SimpleNamespace(Redis=types.SimpleNamespace(from_url=lambda *a, **k: 'rc'))
    monkeypatch.setitem(sys.modules, 'redis', fake_redis)
    assert ds._redis_client(_redis()) == 'rc'
    monkeypatch.setenv('TT_REDIS_URL', 'redis://env')
    assert ds._redis_client(_redis(url='', url_env='TT_REDIS_URL')) == 'rc'
    try:
        ds._redis_client(_redis(url='', url_env='TT_MISSING_URL'))
    except ds.EventStreamError:
        pass
    class Sess:
        def __init__(self, profile_name=None): pass
        def client(self, *a, **k): return 'sqsc'
    monkeypatch.setitem(sys.modules, 'boto3', types.SimpleNamespace(session=types.SimpleNamespace(Session=Sess)))
    assert ds._sqs_client(_sqs()) == 'sqsc'
    assert ds._parse_fields({'a': '1', 'b': 'x'}) == {'a': 1, 'b': 'x'}
    assert ds._truncate('x', 10) == 'x'
    assert 'truncated' in ds._truncate('x' * 100, 5)
    assert ds._message_text({'a': 1}, 100)


class RC:
    def xgroup_create(self, *a, **k): raise RuntimeError('BUSYGROUP')
    def xreadgroup(self, *a, **k): return [('s', [('1', {'a': '1'})])]
    def xack(self, *a): return 1
    def close(self): raise RuntimeError('x')


def test_redis(monkeypatch):
    r = ds._RedisStream(_redis()); r.client = RC()
    assert r.read(1)[0]['fields']['a'] == 1
    assert r.ack(['1']) == 1
    r.close()
    class Bad(RC):
        def xreadgroup(self, *a, **k): raise RuntimeError('x')
        def xack(self, *a): raise RuntimeError('x')
    r.client = Bad()
    for fn in (lambda: r.read(1), lambda: r.ack(['1'])):
        try:
            fn()
        except ds.EventStreamError:
            pass


class SC:
    def receive_message(self, **k):
        return {'Messages': [{'Body': '{"a":1}', 'ReceiptHandle': 'r', 'MessageId': 'i'}]}
    def delete_message(self, **k):
        if k['ReceiptHandle'] == 'bad': raise RuntimeError('x')


def test_sqs_and_actions(monkeypatch):
    s = ds._SqsStream(_sqs()); s.client = SC()
    msgs = s.read(1)
    assert msgs[0]['body']['a'] == 1
    assert s.ack(['r'])['deleted'] == 1
    assert s.ack(['bad'])['errors']
    s.close()
    class BadS(SC):
        def receive_message(self, **k): raise RuntimeError('x')
    s.client = BadS()
    try:
        s.read(1)
    except ds.EventStreamError:
        pass
    monkeypatch.setattr(ds, 'get_stream', lambda i: _redis(enabled=True))
    monkeypatch.setattr(ds, '_stream_for', lambda spec: type('S', (), {'read': lambda self, c, **k: [{'id': 1}], 'ack': lambda self, r: 1})())
    assert ds.read_stream('r', count=1)['ok']
    assert ds.ack_stream('r', ['a'])['acked'] == 1
    monkeypatch.setattr(ds, 'get_stream', lambda i: _redis(enabled=False))
    assert 'disabled' in ds.read_stream('r')['error']
    assert 'disabled' in ds.ack_stream('r', ['a'])['error']
    monkeypatch.setattr(ds, 'get_stream', lambda i: _redis())
    assert 'receipts' in ds.ack_stream('r', 'x')['error']
    monkeypatch.setattr(ds, 'get_stream', lambda i: _redis())
    monkeypatch.setattr(ds, '_stream_for', lambda spec: (_ for _ in ()).throw(ds.EventStreamError('read fail')))
    assert 'read fail' in ds.read_stream('r')['error']
    monkeypatch.setattr(ds, '_stream_for', lambda spec: (_ for _ in ()).throw(ds.EventStreamError('ack fail')))
    assert 'ack fail' in ds.ack_stream('r', ['a'])['error']
    monkeypatch.setattr(ds, '_stream_for', lambda spec: type('S2', (), {'ack': lambda self, r: {'deleted': 1, 'errors': ['e']}})())
    assert ds.ack_stream('r', ['a'])['ok'] is False
    monkeypatch.setattr(ds, '_stream_for', lambda spec: type('S3', (), {'ack': lambda self, r: {'deleted': 1, 'errors': []}})())
    assert ds.ack_stream('r', ['a'])['ok'] is True
    monkeypatch.setattr(ds, '_STREAMS', {'r': type('C', (), {'close': lambda self: None})()})
    ds.close_all()
    assert ds._STREAMS == {}


def test_durable_final(monkeypatch, tmp_path):
    p = tmp_path / 'e.json'; p.write_text(json.dumps({'streams': {}}))
    monkeypatch.setattr(ds, 'REGISTRY_PATH', str(p)); ds.reload_streams()
    assert 'No event streams' in ds.streams_text()
    p.write_text(json.dumps({'streams': {'r': {'url': 'redis://x', 'description': 'd'}}}))
    ds.reload_streams()
    assert 'd' in ds.streams_text()
    # client caches
    r = ds._RedisStream(_redis())
    class RC2:
        def xgroup_create(self, *a, **k): pass
        def xreadgroup(self, *a, **k): return []
        def xack(self, *a): return 1
    r.client = RC2(); assert r.read(1) == []; assert r.ack([]) == 1
    s = ds._SqsStream(_sqs())
    class SC2:
        def receive_message(self, **k): return {'Messages': [{'Body': 'bad'}]}
        def delete_message(self, **k): pass
    s.client = SC2(); assert s.read(1)[0]['body'] == 'bad'; assert s.ack([]) == {'deleted': 0, 'errors': []}
    # stream_for branches and cache
    ds._STREAMS.clear()
    rspec = _redis(); qspec = _sqs()
    assert ds._stream_for(rspec) is ds._stream_for(rspec)
    assert ds._stream_for(qspec)
    # close_all exception
    ds._STREAMS.clear()
    ds._STREAMS['x'] = type('C', (), {'close': lambda self: (_ for _ in ()).throw(RuntimeError('x'))})()
    ds.close_all()
    # message fallback
    monkeypatch.setattr(ds.json, 'dumps', lambda *a, **k: (_ for _ in ()).throw(TypeError('x')))
    assert ds._message_text({'a': 1}, 100)
    monkeypatch.undo()
    # sqs read path
    monkeypatch.setattr(ds, 'get_stream', lambda i: _sqs())
    class SS:
        def read(self, c, **k): return []
        def ack(self, r): return {'deleted': 0, 'errors': []}
    monkeypatch.setattr(ds, '_stream_for', lambda spec: SS())
    assert ds.read_stream('q')['ok']
    assert 'non-empty' in ds.ack_stream('q', [])['error']
