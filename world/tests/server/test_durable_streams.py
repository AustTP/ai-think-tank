"""Durable event streams: registry, Redis Streams, and SQS."""

from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import json

import pytest

import server.durable_streams as durable_streams


class FakeRedis:
    def __init__(self):
        self.acked = []
        self.created = False
        self.closed = False

    def xgroup_create(self, *args, **kwargs):
        self.created = True

    def xreadgroup(self, group, consumer, streams, count=10, block=None):
        return [('think-tank-events', [
            ('1-0', {'type': 'event', 'payload': '{"x": 1}'}),
        ])]

    def xack(self, stream, group, *receipts):
        self.acked.extend(receipts)
        return len(receipts)

    def close(self):
        self.closed = True


class FakeSqs:
    def __init__(self):
        self.deleted = []

    def receive_message(self, **kwargs):
        return {'Messages': [{
            'MessageId': 'm1',
            'ReceiptHandle': 'r1',
            'Body': '{"x": 2}',
            'Attributes': {'SentTimestamp': '1'},
        }]}

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)


@pytest.fixture
def streams(tmp_path, monkeypatch):
    config = {
        'streams': {
            'redis': {
                'name': 'Redis test',
                'backend': 'redis_stream',
                'enabled': True,
                'url': 'redis://example.invalid/0',
                'stream': 'think-tank-events',
                'group': 'think-tank',
                'consumer': 'agent',
            },
            'sqs': {
                'name': 'SQS test',
                'backend': 'sqs',
                'enabled': True,
                'queue_url': 'https://sqs.us-east-1.amazonaws.com/000000000000/q',
                'region': 'us-east-1',
            },
            'off': {
                'name': 'Disabled test',
                'backend': 'redis_stream',
                'enabled': False,
                'url': 'redis://example.invalid/0',
            },
        },
    }
    path = tmp_path / 'event_streams.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(durable_streams, 'REGISTRY_PATH', str(path))
    durable_streams.reload_streams()
    durable_streams.close_all()
    fake_redis = FakeRedis()
    fake_sqs = FakeSqs()
    monkeypatch.setattr(durable_streams, '_redis_client', lambda spec: fake_redis)
    monkeypatch.setattr(durable_streams, '_sqs_client', lambda spec: fake_sqs)
    yield fake_redis, fake_sqs
    durable_streams.close_all()


def test_streams_text_lists_backends(streams):
    text = durable_streams.streams_text()
    assert 'Redis test' in text
    assert 'SQS test' in text
    assert 'Disabled test' in text
    assert 'redis stream=think-tank-events' in text


def test_redis_read_does_not_ack(streams):
    fake_redis, _ = streams
    out = durable_streams.read_stream('redis', count=5)
    assert out['ok'] is True
    assert out['messages'][0]['receipt'] == '1-0'
    assert out['messages'][0]['fields']['payload'] == {'x': 1}
    assert fake_redis.acked == []
    assert 'ack_event_stream' in out['text']


def test_redis_ack(streams):
    fake_redis, _ = streams
    out = durable_streams.ack_stream('redis', ['1-0'])
    assert out['ok'] is True
    assert fake_redis.acked == ['1-0']


def test_sqs_read_and_ack(streams):
    _, fake_sqs = streams
    out = durable_streams.read_stream('sqs', count=5)
    assert out['ok'] is True
    assert out['messages'][0]['receipt'] == 'r1'
    ack = durable_streams.ack_stream('sqs', ['r1'])
    assert ack['ok'] is True
    assert fake_sqs.deleted == ['r1']


def test_disabled_stream_refuses(streams):
    out = durable_streams.read_stream('off')
    assert out['ok'] is False
    assert 'disabled' in out['error']


def test_unknown_stream_is_an_error(streams):
    with pytest.raises(durable_streams.EventStreamError):
        durable_streams.get_stream('missing')


def test_empty_ack_is_refused(streams):
    out = durable_streams.ack_stream('redis', [])
    assert out['ok'] is False
    assert 'non-empty' in out['error']
