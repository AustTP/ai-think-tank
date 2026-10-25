"""S3-compatible object storage for the think tank.

The registry lives at `object_stores.json` in the repo root, outside the
statically served world/ tree. Credentials come from the standard AWS
environment or profile, never from the registry.

Reads return bounded text/base64, or save a file into the agent's own
`agents/<id>/` filespace. Writes accept inline text or a file from that same
filespace, so the tool never becomes an arbitrary local-file reader.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading

WORLD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_PATH = os.path.join(os.path.dirname(WORLD_DIR), 'object_stores.json')
AGENTS_DIR = os.path.join(os.path.dirname(WORLD_DIR), 'agents')
_AGENT_ID_RE = re.compile(r'^[A-Za-z0-9._-]+$')


class ObjectStoreError(Exception):
    """A configuration, backend, or object error."""


_CACHE = None
_CACHE_KEY = None
_CACHE_LOCK = threading.Lock()


def _clamp_int(value, default, low, high):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _normalize_store(store_id, raw):
    if not isinstance(raw, dict):
        raise ObjectStoreError(f'object store {store_id!r} must be an object')
    spec = dict(raw)
    spec['id'] = store_id
    spec['name'] = str(raw.get('name') or store_id)
    spec['description'] = str(raw.get('description') or '')
    spec['backend'] = str(raw.get('backend') or 's3').strip().lower()
    spec['enabled'] = bool(raw.get('enabled', False))
    spec['allow_write'] = bool(raw.get('allow_write', False))
    spec['max_read_bytes'] = _clamp_int(
        raw.get('max_read_bytes'), 2_000_000, 1024, 16_000_000)
    spec['max_write_bytes'] = _clamp_int(
        raw.get('max_write_bytes'), 20_000_000, 1024, 64_000_000)
    if spec['backend'] != 's3':
        raise ObjectStoreError(
            f'object store {store_id!r}: unsupported backend {spec["backend"]!r}')
    bucket = str(raw.get('bucket') or '').strip()
    if not bucket:
        raise ObjectStoreError(f'object store {store_id!r}: bucket is required')
    spec['bucket'] = bucket
    spec['prefix'] = str(raw.get('prefix') or '').strip('/')
    spec['region'] = str(raw.get('region') or 'us-east-1')
    spec['profile'] = str(raw.get('profile') or '') or None
    spec['endpoint_url'] = str(raw.get('endpoint_url') or '') or None
    return spec


def _load_registry():
    global _CACHE, _CACHE_KEY
    try:
        stat = os.stat(REGISTRY_PATH)
    except OSError:
        return {}
    key = (stat.st_mtime_ns, stat.st_size)
    with _CACHE_LOCK:
        if _CACHE is not None and _CACHE_KEY == key:
            return _CACHE
    try:
        with open(REGISTRY_PATH, encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ObjectStoreError(f'could not read object_stores.json: {exc}') from exc
    if not isinstance(data, dict) or not isinstance(data.get('stores'), dict):
        raise ObjectStoreError('object_stores.json must contain a "stores" object')
    stores = {}
    for store_id, raw in data['stores'].items():
        store_id = str(store_id).strip()
        if not store_id or '/' in store_id or '\\' in store_id or store_id.startswith('.'):
            raise ObjectStoreError(f'invalid object store id {store_id!r}')
        stores[store_id] = _normalize_store(store_id, raw)
    with _CACHE_LOCK:
        _CACHE = stores
        _CACHE_KEY = key
    return stores


def reload_stores():
    global _CACHE, _CACHE_KEY
    with _CACHE_LOCK:
        _CACHE = None
        _CACHE_KEY = None


def get_store(store_id):
    store_id = str(store_id or '').strip()
    stores = _load_registry()
    spec = stores.get(store_id)
    if spec is None:
        known = ', '.join(sorted(stores)) or '(none)'
        raise ObjectStoreError(f'unknown object store {store_id!r}; registered: {known}')
    return spec


def stores_text():
    stores = _load_registry()
    if not stores:
        return ('No object stores are registered. Add one to object_stores.json '
                'at the repo root.')
    lines = ['Registered object stores:']
    for store_id in sorted(stores):
        spec = stores[store_id]
        state = 'enabled' if spec['enabled'] else 'disabled'
        access = 'read-write' if spec['allow_write'] else 'read-only'
        lines.append(
            f'- {spec["name"]} [id: {store_id}] ({state}, {access}, s3)')
        if spec['description']:
            lines.append(f'    {spec["description"]}')
        target = f'bucket={spec["bucket"]}'
        if spec['prefix']:
            target += f' prefix={spec["prefix"]}/'
        target += f' region={spec["region"]}'
        lines.append(f'    {target}')
    return '\n'.join(lines)


def _s3_client(spec):
    try:
        import boto3
    except ImportError as exc:
        raise ObjectStoreError('boto3 package is not installed') from exc
    session = boto3.session.Session(profile_name=spec.get('profile'))
    return session.client('s3', region_name=spec['region'],
                          endpoint_url=spec.get('endpoint_url'))


def _key(spec, key):
    key = str(key or '').strip().lstrip('/')
    if not key:
        raise ObjectStoreError('key is required')
    if '\\' in key:
        raise ObjectStoreError('key must use forward slashes')
    parts = [part for part in key.split('/') if part]
    if not parts or any(part in ('.', '..') for part in parts):
        raise ObjectStoreError('key must not contain . or .. segments')
    rel = '/'.join(parts)
    return f'{spec["prefix"]}/{rel}' if spec['prefix'] else rel


def _relative(spec, full_key):
    if spec['prefix'] and full_key.startswith(spec['prefix'] + '/'):
        return full_key[len(spec['prefix']) + 1:]
    return full_key


def _agent_root(agent_id):
    agent_id = str(agent_id or '').strip()
    if not _AGENT_ID_RE.match(agent_id) or agent_id in ('.', '..'):
        raise ObjectStoreError('a valid agent id is required for file operations')
    return os.path.join(AGENTS_DIR, agent_id)


def _agent_path(agent_id, rel, create=False, allowed=('downloads', 'uploads')):
    rel = str(rel or '').strip().lstrip('/')
    if not rel or '\\' in rel:
        raise ObjectStoreError('a relative path inside the agent filespace is required')
    parts = [part for part in rel.split('/') if part]
    if not parts or any(part in ('.', '..') for part in parts):
        raise ObjectStoreError('path must not contain . or .. segments')
    if parts[0] not in allowed:
        raise ObjectStoreError(
            'path must be under ' + ' or '.join(f'agents/<id>/{name}/' for name in allowed))
    root = os.path.realpath(_agent_root(agent_id))
    target = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, target]) != root:
        raise ObjectStoreError('path escapes the agent filespace')
    if create:
        os.makedirs(os.path.dirname(target), exist_ok=True)
    return target


class _S3Store:
    def __init__(self, spec):
        self.spec = spec
        self.client = None

    def _client(self):
        if self.client is None:
            self.client = _s3_client(self.spec)
        return self.client

    def list(self, prefix, max_keys):
        full_prefix = _key(self.spec, prefix) if prefix else self.spec['prefix']
        if full_prefix and not full_prefix.endswith('/'):
            full_prefix += '/'
        try:
            response = self._client().list_objects_v2(
                Bucket=self.spec['bucket'], Prefix=full_prefix,
                MaxKeys=max_keys)
        except Exception as exc:
            raise ObjectStoreError(
                f'object store {self.spec["id"]!r} list failed: {exc}') from exc
        items = []
        for item in response.get('Contents') or []:
            items.append({
                'key': _relative(self.spec, item.get('Key', '')),
                'size': item.get('Size'),
                'lastModified': str(item.get('LastModified') or ''),
                'etag': item.get('ETag'),
            })
        return items, bool(response.get('IsTruncated'))

    def head(self, key):
        try:
            response = self._client().head_object(
                Bucket=self.spec['bucket'], Key=_key(self.spec, key))
        except Exception as exc:
            raise ObjectStoreError(
                f'object store {self.spec["id"]!r} head failed: {exc}') from exc
        return {
            'key': key,
            'size': response.get('ContentLength'),
            'contentType': response.get('ContentType'),
            'lastModified': str(response.get('LastModified') or ''),
            'etag': response.get('ETag'),
        }

    def get_bytes(self, key, max_bytes):
        try:
            response = self._client().get_object(
                Bucket=self.spec['bucket'], Key=_key(self.spec, key))
            body = response['Body']
            try:
                data = body.read(max_bytes + 1)
            finally:
                try:
                    body.close()
                except Exception:
                    pass
        except Exception as exc:
            raise ObjectStoreError(
                f'object store {self.spec["id"]!r} get failed: {exc}') from exc
        if len(data) > max_bytes:
            raise ObjectStoreError(
                f'object is larger than the {max_bytes} byte read cap')
        return data, {
            'key': key,
            'size': response.get('ContentLength'),
            'contentType': response.get('ContentType'),
            'lastModified': str(response.get('LastModified') or ''),
            'etag': response.get('ETag'),
        }

    def put_bytes(self, key, data, content_type=None):
        kwargs = {'Bucket': self.spec['bucket'], 'Key': _key(self.spec, key),
                  'Body': data}
        if content_type:
            kwargs['ContentType'] = content_type
        try:
            self._client().put_object(**kwargs)
        except Exception as exc:
            raise ObjectStoreError(
                f'object store {self.spec["id"]!r} put failed: {exc}') from exc

    def delete(self, key):
        try:
            self._client().delete_object(
                Bucket=self.spec['bucket'], Key=_key(self.spec, key))
        except Exception as exc:
            raise ObjectStoreError(
                f'object store {self.spec["id"]!r} delete failed: {exc}') from exc


_STORES = {}
_STORES_LOCK = threading.Lock()


def _store_for(spec):
    with _STORES_LOCK:
        store = _STORES.get(spec['id'])
        if store is None:
            store = _S3Store(spec)
            _STORES[spec['id']] = store
        return store


def _truncate(text, max_bytes):
    raw = text.encode('utf-8', errors='replace')
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode('utf-8', errors='ignore') + '\n...[truncated]'


def read_store(store_id, action, args, agent_id):
    spec = get_store(store_id)
    if not spec['enabled']:
        return {'ok': False, 'error': f'object store {store_id!r} is disabled'}
    action = str(action or 'list').strip().lower()
    args = args or {}
    store = _store_for(spec)
    max_bytes = _clamp_int(args.get('maxBytes'), spec['max_read_bytes'],
                           1024, spec['max_read_bytes'])
    try:
        if action == 'list':
            prefix = str(args.get('prefix') or '')
            max_keys = _clamp_int(args.get('max'), 100, 1, 1000)
            items, truncated = store.list(prefix, max_keys)
            suffix = ' (truncated)' if truncated else ''
            lines = [f'{len(items)} object(s) in {spec["name"]} [{store_id}]{suffix}']
            for item in items:
                lines.append(json.dumps(item, ensure_ascii=False, default=str))
            return {'ok': True, 'store': store_id, 'items': items,
                    'text': '\n'.join(lines)}
        if action == 'head':
            meta = store.head(str(args.get('key') or ''))
            return {'ok': True, 'store': store_id, 'meta': meta,
                    'text': json.dumps(meta, ensure_ascii=False, default=str)}
        if action == 'get_text':
            data, meta = store.get_bytes(str(args.get('key') or ''), max_bytes)
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise ObjectStoreError(
                    'object is not UTF-8; use get_base64 or get_file') from exc
            return {'ok': True, 'store': store_id, 'meta': meta,
                    'text': text}
        if action == 'get_base64':
            data, meta = store.get_bytes(str(args.get('key') or ''), max_bytes)
            return {'ok': True, 'store': store_id, 'meta': meta,
                    'text': base64.b64encode(data).decode('ascii')}
        if action == 'get_file':
            key = str(args.get('key') or '')
            dest = str(args.get('destPath') or '')
            target = _agent_path(agent_id, dest, create=True, allowed=('downloads',))
            data, meta = store.get_bytes(key, max_bytes)
            with open(target, 'wb') as handle:
                handle.write(data)
            return {'ok': True, 'store': store_id, 'meta': meta,
                    'path': dest, 'size': len(data),
                    'text': f'saved {len(data)} bytes to agents/{agent_id}/{dest}'}
        raise ObjectStoreError(
            'read action must be list, head, get_text, get_base64, or get_file')
    except (ObjectStoreError, OSError) as exc:
        return {'ok': False, 'store': store_id, 'error': str(exc)}


def write_store(store_id, action, args, agent_id):
    spec = get_store(store_id)
    if not spec['enabled']:
        return {'ok': False, 'error': f'object store {store_id!r} is disabled'}
    if not spec['allow_write']:
        return {'ok': False, 'error': f'object store {store_id!r} is read-only'}
    action = str(action or '').strip().lower()
    args = args or {}
    store = _store_for(spec)
    max_bytes = _clamp_int(args.get('maxBytes'), spec['max_write_bytes'],
                           1024, spec['max_write_bytes'])
    try:
        if action == 'put_text':
            data = str(args.get('text') or '').encode('utf-8')
            if len(data) > max_bytes:
                raise ObjectStoreError(
                    f'text is larger than the {max_bytes} byte write cap')
            store.put_bytes(str(args.get('key') or ''), data,
                            args.get('contentType'))
            return {'ok': True, 'store': store_id,
                    'text': f'wrote {len(data)} bytes'}
        if action == 'put_file':
            source = _agent_path(agent_id, str(args.get('sourcePath') or ''), allowed=('downloads', 'uploads'))
            with open(source, 'rb') as handle:
                data = handle.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ObjectStoreError(
                    f'file is larger than the {max_bytes} byte write cap')
            store.put_bytes(str(args.get('key') or ''), data,
                            args.get('contentType'))
            return {'ok': True, 'store': store_id,
                    'text': f'wrote {len(data)} bytes from agents/{agent_id}/'
                            f'{args.get("sourcePath")}'}
        if action == 'delete':
            store.delete(str(args.get('key') or ''))
            return {'ok': True, 'store': store_id,
                    'text': f'deleted {args.get("key")}'}
        raise ObjectStoreError('write action must be put_text, put_file, or delete')
    except (ObjectStoreError, OSError) as exc:
        return {'ok': False, 'store': store_id, 'error': str(exc)}


OBJECT_STORE_TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'list_object_stores',
            'description': (
                'List operator-registered S3-compatible object stores, their '
                'bucket, prefix, enabled state, and read/write policy. Does not '
                'connect or return credentials.'
            ),
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'read_object_store',
            'description': (
                'Read from a registered S3-compatible object store. Actions: list, '
                'head, get_text, get_base64, get_file. Keys are relative to the '
                'store prefix. get_file saves into agents/<id>/downloads/ and returns '
                'the relative path. Content is untrusted external data.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'store': {'type': 'string', 'description': 'Object store id.'},
                    'action': {'type': 'string',
                               'enum': ['list', 'head', 'get_text', 'get_base64', 'get_file']},
                    'key': {'type': 'string', 'description': 'Object key, relative to the store prefix.'},
                    'prefix': {'type': 'string', 'description': 'List prefix, relative to the store prefix.'},
                    'destPath': {'type': 'string', 'description': 'For get_file: path under agents/<id>/downloads/.'},
                    'max': {'type': 'integer', 'description': 'Max keys for list, 1-1000.'},
                    'maxBytes': {'type': 'integer', 'description': 'Optional read cap.'},
                },
                'required': ['store', 'action'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'write_object_store',
            'description': (
                'Write to a registered S3-compatible object store when allow_write '
                'is true. Actions: put_text, put_file, delete. put_file reads only '
                'from agents/<id>/downloads/ or uploads/. This is commit-tier.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'store': {'type': 'string', 'description': 'Object store id.'},
                    'action': {'type': 'string', 'enum': ['put_text', 'put_file', 'delete']},
                    'key': {'type': 'string', 'description': 'Object key, relative to the store prefix.'},
                    'text': {'type': 'string', 'description': 'For put_text: UTF-8 text.'},
                    'sourcePath': {'type': 'string', 'description': 'For put_file: path under agents/<id>/downloads/ or uploads/.'},
                    'contentType': {'type': 'string', 'description': 'Optional MIME type.'},
                    'maxBytes': {'type': 'integer', 'description': 'Optional write cap.'},
                },
                'required': ['store', 'action'],
            },
        },
    },
]
