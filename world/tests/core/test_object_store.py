"""S3-compatible object store: registry, keys, reads, writes, agent filespace."""
from __future__ import annotations

import io
import json

import pytest

import core.object_store as object_store


class FakeBody(io.BytesIO):
    pass


class FakeS3:
    def __init__(self):
        self.objects = {
            'shared/a.txt': b'hello',
            'shared/sub/b.bin': b'\x00\x01\x02',
        }
        self.puts = []
        self.deletes = []

    def list_objects_v2(self, Bucket, Prefix='', MaxKeys=100):
        contents = [
            {'Key': key, 'Size': len(value), 'ETag': '"e"',
             'LastModified': '2026-01-01'}
            for key, value in sorted(self.objects.items())
            if key.startswith(Prefix)
        ][:MaxKeys]
        return {'Contents': contents, 'IsTruncated': False}

    def head_object(self, Bucket, Key):
        return {'ContentLength': len(self.objects[Key]),
                'ContentType': 'text/plain',
                'LastModified': '2026-01-01', 'ETag': '"e"'}

    def get_object(self, Bucket, Key):
        return {'Body': FakeBody(self.objects[Key]),
                'ContentLength': len(self.objects[Key]),
                'ContentType': 'text/plain',
                'LastModified': '2026-01-01', 'ETag': '"e"'}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.puts.append((Key, bytes(Body), ContentType))
        self.objects[Key] = bytes(Body)

    def delete_object(self, Bucket, Key):
        self.deletes.append(Key)
        self.objects.pop(Key, None)


@pytest.fixture
def stores(tmp_path, monkeypatch):
    agents = tmp_path / 'agents'
    monkeypatch.setattr(object_store, 'AGENTS_DIR', str(agents))
    config = {
        'stores': {
            'readonly': {
                'name': 'Read only',
                'backend': 's3',
                'enabled': True,
                'bucket': 'bucket',
                'prefix': 'shared',
                'allow_write': False,
            },
            'writable': {
                'name': 'Writable',
                'backend': 's3',
                'enabled': True,
                'bucket': 'bucket',
                'prefix': 'shared',
                'allow_write': True,
            },
            'off': {
                'name': 'Disabled',
                'backend': 's3',
                'enabled': False,
                'bucket': 'bucket',
            },
        },
    }
    path = tmp_path / 'object_stores.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(object_store, 'REGISTRY_PATH', str(path))
    object_store.reload_stores()
    fake = FakeS3()
    monkeypatch.setattr(object_store, '_s3_client', lambda spec: fake)
    object_store._STORES.clear()
    yield fake, agents
    object_store._STORES.clear()


def test_stores_text_lists_policy(stores):
    text = object_store.stores_text()
    assert 'Read only' in text
    assert 'Writable' in text
    assert 'read-only' in text
    assert 'read-write' in text


def test_list_returns_relative_keys(stores):
    out = object_store.read_store('readonly', 'list', {'prefix': 'sub'}, 'ada')
    assert out['ok'] is True
    assert out['items'][0]['key'] == 'sub/b.bin'


def test_get_text(stores):
    out = object_store.read_store('readonly', 'get_text', {'key': 'a.txt'}, 'ada')
    assert out['ok'] is True
    assert out['text'] == 'hello'


def test_get_base64(stores):
    out = object_store.read_store('readonly', 'get_base64', {'key': 'sub/b.bin'}, 'ada')
    assert out['ok'] is True
    assert out['text'] == 'AAEC'


def test_get_file_writes_agent_filespace(stores):
    _, agents = stores
    out = object_store.read_store(
        'readonly', 'get_file',
        {'key': 'a.txt', 'destPath': 'downloads/a.txt'}, 'ada')
    assert out['ok'] is True
    assert (agents / 'ada' / 'downloads' / 'a.txt').read_bytes() == b'hello'


def test_get_file_rejects_traversal(stores):
    out = object_store.read_store(
        'readonly', 'get_file',
        {'key': 'a.txt', 'destPath': '../escape.txt'}, 'ada')
    assert out['ok'] is False
    assert '..' in out['error']


def test_get_file_cannot_escape_via_downloads(stores):
    out = object_store.read_store(
        'readonly', 'get_file',
        {'key': 'a.txt', 'destPath': 'downloads/../agent.json'}, 'ada')
    assert out['ok'] is False
    assert '..' in out['error']


def test_get_file_cannot_overwrite_agent_metadata(stores):
    out = object_store.read_store(
        'readonly', 'get_file',
        {'key': 'a.txt', 'destPath': 'agent.json'}, 'ada')
    assert out['ok'] is False
    assert 'agents/<id>/downloads/' in out['error']


def test_put_text_requires_write(stores):
    out = object_store.write_store(
        'readonly', 'put_text', {'key': 'new.txt', 'text': 'x'}, 'ada')
    assert out['ok'] is False
    assert 'read-only' in out['error']


def test_put_text_writes(stores):
    fake, _ = stores
    out = object_store.write_store(
        'writable', 'put_text',
        {'key': 'new.txt', 'text': 'new', 'contentType': 'text/plain'}, 'ada')
    assert out['ok'] is True
    assert fake.objects['shared/new.txt'] == b'new'


def test_put_file_reads_agent_filespace(stores):
    fake, agents = stores
    source = agents / 'ada' / 'uploads' / 'x.txt'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'from file')
    out = object_store.write_store(
        'writable', 'put_file',
        {'key': 'from-file.txt', 'sourcePath': 'uploads/x.txt'}, 'ada')
    assert out['ok'] is True
    assert fake.objects['shared/from-file.txt'] == b'from file'


def test_delete_writes(stores):
    fake, _ = stores
    out = object_store.write_store(
        'writable', 'delete', {'key': 'a.txt'}, 'ada')
    assert out['ok'] is True
    assert fake.deletes == ['shared/a.txt']


def test_disabled_store_refuses(stores):
    out = object_store.read_store('off', 'list', {}, 'ada')
    assert out['ok'] is False
    assert 'disabled' in out['error']


def test_unknown_store_is_an_error(stores):
    with pytest.raises(object_store.ObjectStoreError):
        object_store.get_store('missing')


def test_key_traversal_is_rejected(stores):
    out = object_store.read_store(
        'readonly', 'get_text', {'key': '../a.txt'}, 'ada')
    assert out['ok'] is False
    assert '..' in out['error']
