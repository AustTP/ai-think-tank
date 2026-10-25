"""Durable event streams for the think tank.

Two operator-registered backends cover the practical queue gap:
- Redis Streams with consumer groups (`XREADGROUP` / `XACK`).
- AWS SQS with visibility timeouts (`ReceiveMessage` / `DeleteMessage`).

The registry lives at `event_streams.json` in the repo root, outside the
statically served world/ tree. Reads do not acknowledge. The agent must call
`ack_event_stream` after it has used the message, so a crash or a failed task
re-delivers instead of losing the event.
"""
from __future__ import annotations

import atexit
import json
import os
import threading

WORLD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_PATH = os.path.join(os.path.dirname(WORLD_DIR), 'event_streams.json')
MAX_TOTAL_BYTES = 2_000_000


class EventStreamError(Exception):
    """A configuration, backend, or message error."""


_CACHE = None
_CACHE_KEY = None
_CACHE_LOCK = threading.Lock()


def _clamp_int(value, default, low, high):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _normalize_stream(stream_id, raw):
    if not isinstance(raw, dict):
        raise EventStreamError(f'event stream {stream_id!r} must be an object')
    spec = dict(raw)
    spec['id'] = stream_id
    spec['name'] = str(raw.get('name') or stream_id)
    spec['description'] = str(raw.get('description') or '')
    spec['backend'] = str(raw.get('backend') or 'redis_stream').strip().lower()
    spec['enabled'] = bool(raw.get('enabled', False))
    spec['max_message_bytes'] = _clamp_int(
        raw.get('max_message_bytes'), 1_000_000, 1024, 8_000_000)
    if spec['backend'] == 'redis_stream':
        url = str(raw.get('url') or '').strip()
        url_env = str(raw.get('url_env') or '').strip()
        if not url and not url_env:
            raise EventStreamError(
                f'event stream {stream_id!r}: redis_stream needs url or url_env')
        spec['url'] = url
        spec['url_env'] = url_env
        spec['stream'] = str(raw.get('stream') or 'think-tank-events').strip()
        spec['group'] = str(raw.get('group') or 'think-tank').strip()
        spec['consumer'] = str(raw.get('consumer') or 'agent').strip()
        spec['block_ms'] = _clamp_int(raw.get('block_ms'), 0, 0, 30000)
        spec['max_batch'] = _clamp_int(raw.get('max_batch'), 20, 1, 100)
    elif spec['backend'] == 'sqs':
        queue_url = str(raw.get('queue_url') or '').strip()
        if not queue_url:
            raise EventStreamError(
                f'event stream {stream_id!r}: sqs needs queue_url')
        spec['queue_url'] = queue_url
        spec['region'] = str(raw.get('region') or 'us-east-1')
        spec['profile'] = str(raw.get('profile') or '') or None
        spec['endpoint_url'] = str(raw.get('endpoint_url') or '') or None
        spec['visibility_timeout_s'] = _clamp_int(
            raw.get('visibility_timeout_s'), 60, 0, 43200)
        spec['wait_time_s'] = _clamp_int(raw.get('wait_time_s'), 0, 0, 20)
        spec['max_messages'] = _clamp_int(raw.get('max_messages'), 10, 1, 10)
    else:
        raise EventStreamError(
            f'event stream {stream_id!r}: unsupported backend {spec["backend"]!r}')
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
        raise EventStreamError(f'could not read event_streams.json: {exc}') from exc
    if not isinstance(data, dict) or not isinstance(data.get('streams'), dict):
        raise EventStreamError('event_streams.json must contain a "streams" object')
    streams = {}
    for stream_id, raw in data['streams'].items():
        stream_id = str(stream_id).strip()
        if not stream_id or '/' in stream_id or '\\' in stream_id or stream_id.startswith('.'):
            raise EventStreamError(f'invalid event stream id {stream_id!r}')
        streams[stream_id] = _normalize_stream(stream_id, raw)
    with _CACHE_LOCK:
        _CACHE = streams
        _CACHE_KEY = key
    return streams


def reload_streams():
    global _CACHE, _CACHE_KEY
    with _CACHE_LOCK:
        _CACHE = None
        _CACHE_KEY = None


def get_stream(stream_id):
    stream_id = str(stream_id or '').strip()
    streams = _load_registry()
    spec = streams.get(stream_id)
    if spec is None:
        known = ', '.join(sorted(streams)) or '(none)'
        raise EventStreamError(f'unknown event stream {stream_id!r}; registered: {known}')
    return spec


def streams_text():
    streams = _load_registry()
    if not streams:
        return ('No event streams are registered. Add one to event_streams.json '
                'at the repo root.')
    lines = ['Registered durable event streams:']
    for stream_id in sorted(streams):
        spec = streams[stream_id]
        state = 'enabled' if spec['enabled'] else 'disabled'
        if spec['backend'] == 'redis_stream':
            target = f"redis stream={spec['stream']} group={spec['group']}"
        else:
            queue = spec['queue_url'].rstrip('/').rsplit('/', 1)[-1]
            target = f'sqs queue={queue} region={spec["region"]}'
        lines.append(f'- {spec["name"]} [id: {stream_id}] ({state}, {spec["backend"]})')
        if spec['description']:
            lines.append(f'    {spec["description"]}')
        lines.append(f'    {target}')
    lines.append('')
    lines.append('Read with read_event_stream. Acknowledge with ack_event_stream '
                 'after the message has been used.')
    return '\n'.join(lines)


def _redis_client(spec):
    try:
        import redis
    except ImportError as exc:
        raise EventStreamError('redis package is not installed') from exc
    url = spec.get('url') or ''
    if spec.get('url_env'):
        url = os.environ.get(spec['url_env'], '')
    if not url:
        raise EventStreamError(
            f'event stream {spec["id"]!r}: redis url is not configured')
    return redis.Redis.from_url(url, decode_responses=True)


def _sqs_client(spec):
    try:
        import boto3
    except ImportError as exc:
        raise EventStreamError('boto3 package is not installed') from exc
    session = boto3.session.Session(profile_name=spec.get('profile'))
    return session.client('sqs', region_name=spec['region'],
                          endpoint_url=spec.get('endpoint_url'))


def _parse_fields(fields):
    parsed = {}
    for key, value in (fields or {}).items():
        if isinstance(value, str):
            try:
                parsed[key] = json.loads(value)
                continue
            except ValueError:
                pass
        parsed[key] = value
    return parsed


class _RedisStream:
    def __init__(self, spec):
        self.spec = spec
        self.client = None

    def read(self, count, block_ms=None):
        if self.client is None:
            self.client = _redis_client(self.spec)
        stream = self.spec['stream']
        group = self.spec['group']
        try:
            try:
                self.client.xgroup_create(stream, group, id='0', mkstream=True)
            except Exception as exc:
                if 'BUSYGROUP' not in str(exc):
                    raise
            block = block_ms if block_ms is not None else self.spec['block_ms']
            rows = self.client.xreadgroup(
                group, self.spec['consumer'], {stream: '>'},
                count=count, block=(block or None))
        except Exception as exc:
            raise EventStreamError(
                f'redis stream {self.spec["id"]!r} read failed: {exc}') from exc
        messages = []
        for _stream, entries in rows or []:
            for entry_id, fields in entries:
                messages.append({
                    'receipt': entry_id,
                    'id': entry_id,
                    'fields': _parse_fields(fields),
                })
        return messages

    def ack(self, receipts):
        if self.client is None:
            self.client = _redis_client(self.spec)
        try:
            return int(self.client.xack(
                self.spec['stream'], self.spec['group'], *receipts))
        except Exception as exc:
            raise EventStreamError(
                f'redis stream {self.spec["id"]!r} ack failed: {exc}') from exc

    def close(self):
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None


class _SqsStream:
    def __init__(self, spec):
        self.spec = spec
        self.client = None

    def read(self, count, wait_s=None):
        if self.client is None:
            self.client = _sqs_client(self.spec)
        try:
            response = self.client.receive_message(
                QueueUrl=self.spec['queue_url'],
                MaxNumberOfMessages=min(count, self.spec['max_messages']),
                WaitTimeSeconds=(self.spec['wait_time_s']
                                 if wait_s is None else wait_s),
                VisibilityTimeout=self.spec['visibility_timeout_s'],
                AttributeNames=['All'],
                MessageAttributeNames=['All'],
            )
        except Exception as exc:
            raise EventStreamError(
                f'sqs stream {self.spec["id"]!r} read failed: {exc}') from exc
        messages = []
        for message in response.get('Messages') or []:
            body = message.get('Body', '')
            try:
                body = json.loads(body)
            except (TypeError, ValueError):
                pass
            messages.append({
                'receipt': message.get('ReceiptHandle', ''),
                'id': message.get('MessageId', ''),
                'body': body,
                'attributes': message.get('Attributes') or {},
                'messageAttributes': message.get('MessageAttributes') or {},
            })
        return messages

    def ack(self, receipts):
        if self.client is None:
            self.client = _sqs_client(self.spec)
        deleted = 0
        errors = []
        for receipt in receipts:
            try:
                self.client.delete_message(
                    QueueUrl=self.spec['queue_url'], ReceiptHandle=receipt)
                deleted += 1
            except Exception as exc:
                errors.append(str(exc)[:200])
        return {'deleted': deleted, 'errors': errors}

    def close(self):
        self.client = None


_STREAMS = {}
_STREAMS_LOCK = threading.Lock()


def _stream_for(spec):
    with _STREAMS_LOCK:
        stream = _STREAMS.get(spec['id'])
        if stream is not None:
            return stream
        stream = (_RedisStream(spec) if spec['backend'] == 'redis_stream'
                  else _SqsStream(spec))
        _STREAMS[spec['id']] = stream
        return stream


def close_all():
    with _STREAMS_LOCK:
        streams = list(_STREAMS.values())
        _STREAMS.clear()
    for stream in streams:
        try:
            stream.close()
        except Exception:
            pass


atexit.register(close_all)


def _truncate(text, max_bytes):
    raw = text.encode('utf-8', errors='replace')
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode('utf-8', errors='ignore') + '\n...[truncated]'


def _message_text(message, max_bytes):
    try:
        text = json.dumps(message, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(message)
    return _truncate(text, max_bytes)


def read_stream(stream_id, count=None, block_ms=None, wait_s=None, max_bytes=None):
    spec = get_stream(stream_id)
    if not spec['enabled']:
        return {'ok': False, 'error': f'event stream {stream_id!r} is disabled'}
    count = _clamp_int(count, spec.get('max_batch') or spec.get('max_messages') or 10,
                       1, 100)
    total_cap = _clamp_int(max_bytes, MAX_TOTAL_BYTES, 1024, MAX_TOTAL_BYTES)
    try:
        stream = _stream_for(spec)
        if spec['backend'] == 'redis_stream':
            block_ms = _clamp_int(block_ms, spec['block_ms'], 0, 30000)
            messages = stream.read(count, block_ms=block_ms)
        else:
            wait_s = _clamp_int(wait_s, spec['wait_time_s'], 0, 20)
            messages = stream.read(count, wait_s=wait_s)
    except EventStreamError as exc:
        return {'ok': False, 'error': str(exc)}
    lines = [f'{len(messages)} message(s) from {spec["name"]} [{stream_id}]:']
    for message in messages:
        lines.append(_message_text(message, spec['max_message_bytes']))
    lines.append('Call ack_event_stream with the receipts after the message is used. '
                 'Unacknowledged messages re-deliver.')
    return {'ok': True, 'stream': stream_id, 'messages': messages,
            'text': _truncate('\n'.join(lines), total_cap)}


def ack_stream(stream_id, receipts):
    spec = get_stream(stream_id)
    if not spec['enabled']:
        return {'ok': False, 'error': f'event stream {stream_id!r} is disabled'}
    if not isinstance(receipts, list):
        return {'ok': False, 'error': 'receipts must be a non-empty list'}
    receipts = [str(r) for r in receipts if str(r).strip()][:100]
    if not receipts:
        return {'ok': False, 'error': 'receipts must be a non-empty list'}
    try:
        result = _stream_for(spec).ack(receipts)
    except EventStreamError as exc:
        return {'ok': False, 'error': str(exc)}
    if isinstance(result, dict):
        return {'ok': not result.get('errors'), 'stream': stream_id,
                'deleted': result.get('deleted', 0),
                'errors': result.get('errors') or [],
                'text': f"acknowledged {result.get('deleted', 0)} message(s)"}
    return {'ok': True, 'stream': stream_id, 'acked': result,
            'text': f'acknowledged {result} message(s)'}


DURABLE_STREAM_TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'list_event_streams',
            'description': (
                'List operator-registered durable event streams (Redis Streams or '
                'SQS), their backend, enabled state, and target. Use this before '
                'read_event_stream. Does not connect to a backend or return '
                'credentials.'
            ),
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'read_event_stream',
            'description': (
                'Read new messages from a registered durable event stream. Reads '
                'do NOT acknowledge; call ack_event_stream with the returned '
                'receipts after the work is done. Unacknowledged messages '
                're-deliver. Use this for queues and event logs, not for '
                'read-only feeds such as RSS or WebSocket buffers.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'stream': {'type': 'string', 'description': 'Event stream id.'},
                    'max': {'type': 'integer',
                            'description': 'Max messages, 1-100.'},
                    'blockMs': {'type': 'integer',
                                'description': 'Redis block time in ms, 0-30000.'},
                    'waitS': {'type': 'integer',
                              'description': 'SQS long-poll seconds, 0-20.'},
                    'maxBytes': {'type': 'integer',
                                 'description': 'Optional total result cap.'},
                },
                'required': ['stream'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'ack_event_stream',
            'description': (
                'Acknowledge durable event messages after they have been used. '
                'Redis Streams uses XACK; SQS deletes the message. Pass the exact '
                'receipts returned by read_event_stream. This is a prepare-tier '
                'housekeeping action, not a business commit.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'stream': {'type': 'string', 'description': 'Event stream id.'},
                    'receipts': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'Receipts from read_event_stream, max 100.',
                    },
                },
                'required': ['stream', 'receipts'],
            },
        },
    },
]
