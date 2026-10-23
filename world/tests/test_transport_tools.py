"""Wiring for durable streams, object storage, CLI, and interactive web."""
from __future__ import annotations

import os
from unittest import mock

import cli_tools
import content
import durable_streams
import object_store
import serve
import tool_policy


def _tool_names():
    return [t['function']['name'] for t in serve.AGENT_ASK_TOOLS]


def test_transport_registries_are_outside_the_static_served_tree():
    for module in (durable_streams, object_store, cli_tools):
        assert not module.REGISTRY_PATH.startswith(module.WORLD_DIR + os.sep)
        assert os.path.exists(module.REGISTRY_PATH)


def test_transport_tools_are_offered():
    names = _tool_names()
    for name in ('list_event_streams', 'read_event_stream', 'ack_event_stream',
                 'list_object_stores', 'read_object_store', 'write_object_store',
                 'list_cli_tools', 'read_cli', 'run_cli'):
        assert name in names


def test_transport_trust_tiers():
    assert tool_policy.tool_trust_tier('list_event_streams') == 'research'
    assert tool_policy.tool_trust_tier('read_event_stream') == 'prepare'
    assert tool_policy.tool_trust_tier('ack_event_stream') == 'prepare'
    assert tool_policy.tool_trust_tier('list_object_stores') == 'research'
    assert tool_policy.tool_trust_tier('read_object_store') == 'prepare'
    assert tool_policy.tool_trust_tier('write_object_store') == 'commit'
    assert tool_policy.tool_trust_tier('list_cli_tools') == 'research'
    assert tool_policy.tool_trust_tier('read_cli') == 'research'
    assert tool_policy.tool_trust_tier('run_cli') == 'commit'
    assert tool_policy.tool_replay_safety('read_event_stream') == 'unsafe'
    assert tool_policy.tool_replay_safety('read_object_store') == 'unsafe'
    assert tool_policy.tool_replay_safety('run_cli') == 'unsafe'


def test_research_policy_allows_reads_not_writes():
    policy = tool_policy.task_tool_policy({'taskType': 'research'})
    assert policy('read_event_stream') is True
    assert policy('read_object_store') is True
    assert policy('read_cli') is True
    assert policy('write_object_store') is False
    assert policy('run_cli') is False


def test_durable_read_is_wrapped():
    with mock.patch.object(serve, 'log_action'), \
            mock.patch.object(serve.durable_streams, 'read_stream', return_value={
                'ok': True, 'text': 'message payload', 'messages': [{}]}):
        text = serve._durable_tool_text(
            'read_event_stream', {'stream': 'redis'}, agent_id='ada')
    assert '<<<EXTERNAL_DATA' in text
    assert 'message payload' in text


def test_object_read_is_wrapped():
    with mock.patch.object(serve, 'log_action'), \
            mock.patch.object(serve.object_store, 'read_store', return_value={
                'ok': True, 'text': 'object text'}):
        text = serve._object_tool_text(
            'read_object_store',
            {'store': 's3', 'action': 'get_text', 'key': 'a.txt'}, agent_id='ada')
    assert '<<<EXTERNAL_DATA' in text
    assert 'object text' in text


def test_cli_output_is_wrapped():
    with mock.patch.object(serve, 'log_action'), \
            mock.patch.object(serve.cli_tools, 'run_tool', return_value={
                'ok': True, 'text': 'status output', 'exitCode': 0,
                'truncated': False}):
        text = serve._cli_tool_text(
            'read_cli', {'tool': 'git_status'}, agent_id='ada')
    assert '<<<EXTERNAL_DATA' in text
    assert 'status output' in text


def test_web_executor_dispatches_browser_act():
    with mock.patch.object(content, '_make_browser_act_executor',
                           return_value=lambda name, args: 'browser ok') as factory:
        executor = serve._make_web_tools_executor('ada', 'key')
        assert executor('browser_act', {'op': 'read'}) == 'browser ok'
    factory.assert_called_once()


def test_web_executor_dispatches_captcha_and_handles():
    with mock.patch.object(content, '_make_captcha_solve_executor',
                           return_value=lambda name, args: 'captcha ok'):
        executor = serve._make_web_tools_executor('ada', 'key')
        assert executor('solve_captcha', {'requestId': 'r'}) == 'captcha ok'
    with mock.patch.object(content, '_make_list_handles_executor',
                           return_value=lambda name, args: 'handles ok'):
        executor = serve._make_web_tools_executor('ada', 'key')
        assert executor('list_capability_handles', {}) == 'handles ok'
