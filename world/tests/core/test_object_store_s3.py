import base64
import json
from pathlib import Path

import core.object_store as os_


def _spec(**kw):
    s = {'id': 's', 'name': 'S', 'description': 'd', 'backend': 's3', 'enabled': True,
         'allow_write': True, 'bucket': 'b', 'prefix': '', 'region': 'r',
         'profile': None, 'endpoint_url': None, 'max_read_bytes': 2048,
         'max_write_bytes': 2048}
    s.update(kw); return s


def test_normalize_and_registry(tmp_path, monkeypatch):
    assert os_._clamp_int('x', 3, 1, 5) == 3
    for raw in ('x', {'backend': 'gcs'}, {}):
        try:
            os_._normalize_store('s', raw)
        except os_.ObjectStoreError:
            pass
    spec = os_._normalize_store('s', {'bucket': 'b'})
    assert spec['backend'] == 's3'
    p = tmp_path / 'object_stores.json'
    monkeypatch.setattr(os_, 'REGISTRY_PATH', str(p))
    monkeypatch.setattr(os_, '_CACHE', None)
    assert os_._load_registry() == {}
    p.write_text(json.dumps({'stores': {'s': {'bucket': 'b', 'enabled': True}}}))
    assert os_.get_store('s')['bucket'] == 'b'
    assert '[id: s]' in os_.stores_text()
    p.write_text(json.dumps({'stores': {}}))
    os_.reload_stores()
    assert 'No object stores' in os_.stores_text()
    try:
        os_.get_store('x')
    except os_.ObjectStoreError:
        pass
    p.write_text('x')
    try:
        os_._load_registry()
    except os_.ObjectStoreError:
        pass


def test_keys_and_paths(tmp_path, monkeypatch):
    s = _spec(prefix='pre')
    assert os_._key(s, '/a/b') == 'pre/a/b'
    assert os_._relative(s, 'pre/a') == 'a'
    for bad in ('', 'a\\b', '../a', '.'):
        try:
            os_._key(s, bad)
        except os_.ObjectStoreError:
            pass
    monkeypatch.setattr(os_, 'AGENTS_DIR', str(tmp_path))
    assert os_._agent_path('a', 'downloads/x', create=True)
    for bad in ('', 'x', '../x', 'other/x'):
        try:
            os_._agent_path('a', bad)
        except os_.ObjectStoreError:
            pass
    try:
        os_._agent_path('../a', 'downloads/x')
    except os_.ObjectStoreError:
        pass


class FakeStore:
    def list(self, prefix, n): return ([{'key': 'a'}], True)
    def head(self, key): return {'key': key}
    def get_bytes(self, key, n):
        if key == 'bad': return b'\xff', {}
        return b'hello', {'key': key}
    def put_bytes(self, *a, **k): self.put = a
    def delete(self, key): self.deleted = key


def test_read_write(tmp_path, monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(os_, '_store_for', lambda s: fake)
    monkeypatch.setattr(os_, 'get_store', lambda i: _spec(allow_write=True))
    assert os_.read_store('s', 'list', {}, 'a')['ok']
    assert os_.read_store('s', 'head', {'key': 'k'}, 'a')['ok']
    assert os_.read_store('s', 'get_text', {'key': 'k'}, 'a')['text'] == 'hello'
    assert 'not UTF-8' in os_.read_store('s', 'get_text', {'key': 'bad'}, 'a')['error']
    assert base64.b64decode(os_.read_store('s', 'get_base64', {'key': 'k'}, 'a')['text']) == b'hello'
    monkeypatch.setattr(os_, 'AGENTS_DIR', str(tmp_path))
    assert os_.read_store('s', 'get_file', {'key': 'k', 'destPath': 'downloads/x'}, 'a')['ok']
    assert 'must be' in os_.read_store('s', 'nope', {}, 'a')['error']
    assert os_.write_store('s', 'put_text', {'key': 'k', 'text': 'x'}, 'a')['ok']
    (tmp_path / 'a' / 'downloads').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'a' / 'downloads' / 'f').write_text('data')
    assert os_.write_store('s', 'put_file', {'key': 'k', 'sourcePath': 'downloads/f'}, 'a')['ok']
    assert os_.write_store('s', 'delete', {'key': 'k'}, 'a')['ok']
    assert 'must be' in os_.write_store('s', 'nope', {}, 'a')['error']
    monkeypatch.setattr(os_, 'get_store', lambda i: _spec(allow_write=False))
    assert 'read-only' in os_.write_store('s', 'put_text', {}, 'a')['error']


def test_disabled_and_s3(monkeypatch):
    monkeypatch.setattr(os_, 'get_store', lambda i: _spec(enabled=False))
    assert 'disabled' in os_.read_store('s', 'list', {}, 'a')['error']
    assert 'disabled' in os_.write_store('s', 'put_text', {}, 'a')['error']
    s3 = os_._S3Store(_spec())
    class Client:
        def list_objects_v2(self, **k): return {'Contents': [{'Key': 'pre/a', 'Size': 1}], 'IsTruncated': True}
        def head_object(self, **k): return {'ContentLength': 1}
        def get_object(self, **k):
            class B:
                def read(self, n): return b'x'
                def close(self): pass
            return {'Body': B()}
        def put_object(self, **k): pass
        def delete_object(self, **k): pass
    s3.client = Client()
    assert s3.list('a', 1)[1] is True
    assert s3.head('a')['size'] == 1
    assert s3.get_bytes('a', 10)[0] == b'x'
    s3.put_bytes('a', b'x')
    s3.delete('a')


def test_object_store_more(tmp_path, monkeypatch):
    assert os_._clamp_int('x', 3, 1, 5) == 3
    p = tmp_path / 'r.json'; monkeypatch.setattr(os_, 'REGISTRY_PATH', str(p))
    monkeypatch.setattr(os_, '_CACHE', None)
    for data in ('x', {'stores': []}, {'stores': {'../x': {'bucket': 'b'}}}):
        p.write_text(data if isinstance(data, str) else json.dumps(data))
        try:
            os_._load_registry()
        except os_.ObjectStoreError:
            pass
    p.write_text(json.dumps({'stores': {'s': {'bucket': 'b', 'description': 'd', 'prefix': 'p'}}}))
    os_.reload_stores()
    assert 'prefix=p/' in os_.stores_text()
    # s3 client construction
    import sys, types
    boto = types.ModuleType('boto3')
    boto.session = types.SimpleNamespace(Session=lambda profile_name=None: types.SimpleNamespace(client=lambda *a, **k: 'c'))
    monkeypatch.setitem(sys.modules, 'boto3', boto)
    assert os_._s3_client(_spec()) == 'c'
    # path escape
    monkeypatch.setattr(os_, 'AGENTS_DIR', str(tmp_path / 'agents'))
    (tmp_path / 'agents' / 'a').mkdir(parents=True)
    (tmp_path / 'agents' / 'a' / 'downloads').symlink_to(tmp_path)
    try:
        os_._agent_path('a', 'downloads/x', create=True)
    except os_.ObjectStoreError:
        pass
    # store cache
    spec = _spec(); os_._STORES.clear()
    assert os_._store_for(spec) is os_._store_for(spec)
    # truncate
    assert 'truncated' in os_._truncate('x' * 100, 10)
    # write caps
    fake = FakeStore(); monkeypatch.setattr(os_, '_store_for', lambda s: fake)
    monkeypatch.setattr(os_, 'get_store', lambda i: _spec(allow_write=True, max_write_bytes=1024))
    assert 'write cap' in os_.write_store('s', 'put_text', {'key': 'k', 'text': 'x' * 2000}, 'a')['error']
    (tmp_path / 'agents' / 'a' / 'uploads').mkdir(exist_ok=True)
    (tmp_path / 'agents' / 'a' / 'uploads' / 'big').write_bytes(b'x' * 2000)
    monkeypatch.setattr(os_, 'AGENTS_DIR', str(tmp_path / 'agents'))
    assert 'write cap' in os_.write_store('s', 'put_file', {'key': 'k', 'sourcePath': 'uploads/big'}, 'a')['error']


def test_s3_errors(monkeypatch):
    s3 = os_._S3Store(_spec())
    class Bad:
        def __getattr__(self, name):
            return lambda **k: (_ for _ in ()).throw(RuntimeError('x'))
    s3.client = Bad()
    for fn, args in ((s3.list, ('a', 1)), (s3.head, ('a',)), (s3.get_bytes, ('a', 1)),
                     (s3.put_bytes, ('a', b'x')), (s3.delete, ('a',))):
        try:
            fn(*args)
        except os_.ObjectStoreError:
            pass
    # cap and close exception
    class B:
        def read(self, n): return b'x' * 2
        def close(self): raise RuntimeError('x')
    class C:
        def get_object(self, **k): return {'Body': B()}
        def put_object(self, **k): pass
    s3.client = C()
    try:
        s3.get_bytes('a', 1)
    except os_.ObjectStoreError:
        pass


def test_object_store_final(monkeypatch):
    assert os_._clamp_int(object(), 3, 1, 5) == 3
    import sys
    monkeypatch.setitem(sys.modules, 'boto3', None)
    try:
        os_._s3_client(_spec())
    except os_.ObjectStoreError:
        pass
    s3 = os_._S3Store(_spec())
    class C:
        def put_object(self, **k): self.kw = k
    c = C(); s3.client = c
    s3.put_bytes('a', b'x', 'text/plain')
    assert c.kw['ContentType'] == 'text/plain'
    assert os_._truncate('x', 10) == 'x'


def test_s3_client_cache(monkeypatch):
    s3 = os_._S3Store(_spec())
    monkeypatch.setattr(os_, '_s3_client', lambda spec: 'client')
    assert s3._client() == 'client'
    assert s3._client() == 'client'
