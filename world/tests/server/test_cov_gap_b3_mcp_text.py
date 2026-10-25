"""Coverage-gap tests for the MCP/durable/object/CLI tool wrappers.

Only new tests; production code is untouched. Every wrapper is exercised with
plain fake clients so no network, model, database, or real transport is used.
"""
import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
from test_serve_helpers import setUpModule, tearDownModule  # noqa: E402,F401


class _FakeMCP:
    def __init__(self, run_result=None, run_error=None):
        self.run_result = run_result if run_result is not None else {}
        self.run_error = run_error
        self.calls = []

    def servers_text(self):
        return 'server-list'

    def list_tools_text(self, server, refresh=False):
        self.calls.append((server, refresh))
        return f'tools-for-{server}'

    def run_tool(self, server, tool, arguments, read_only=False, max_bytes=None):
        self.calls.append((server, tool, arguments, read_only, max_bytes))
        if self.run_error:
            raise self.run_error
        return self.run_result


class _FakeDurable:
    def __init__(self, read_result=None, ack_result=None, error=None):
        self.read_result = read_result if read_result is not None else {}
        self.ack_result = ack_result if ack_result is not None else {}
        self.error = error
        self.calls = []

    def streams_text(self):
        return 'stream-list'

    def read_stream(self, stream, **kwargs):
        self.calls.append(('read', stream, kwargs))
        if self.error:
            raise self.error
        return self.read_result

    def ack_stream(self, stream, receipts):
        self.calls.append(('ack', stream, receipts))
        if self.error:
            raise self.error
        return self.ack_result


class _FakeObject:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {}
        self.error = error
        self.calls = []

    def stores_text(self):
        return 'store-list'

    def read_store(self, store, action, args, agent_id):
        self.calls.append(('read', store, action, args, agent_id))
        if self.error:
            raise self.error
        return self.result

    def write_store(self, store, action, args, agent_id):
        self.calls.append(('write', store, action, args, agent_id))
        if self.error:
            raise self.error
        return self.result


class _FakeCLI:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {}
        self.error = error
        self.calls = []

    def tools_text(self):
        return 'cli-list'

    def run_tool(self, tool, args, read_only=False, max_bytes=None):
        self.calls.append((tool, args, read_only, max_bytes))
        if self.error:
            raise self.error
        return self.result


class McpToolTextTests(unittest.TestCase):
    def test_unavailable(self):
        with mock.patch.object(serve, 'mcp_client', None):
            self.assertIn('not available',
                          serve._mcp_tool_text('mcp_list_servers', {}))

    def test_list_servers_and_tools(self):
        fake = _FakeMCP()
        with mock.patch.object(serve, 'mcp_client', fake):
            self.assertIn('server-list',
                          serve._mcp_tool_text('mcp_list_servers', {}))
            text = serve._mcp_tool_text(
                'mcp_list_tools', {'server': 's1', 'refresh': True})
        self.assertIn('tools-for-s1', text)
        self.assertEqual([('s1', True)], fake.calls)

    def test_run_tool_success_log_failure_is_ignored(self):
        fake = _FakeMCP(run_result={'ok': True, 'text': 'payload', 'tier': 'read'})
        with mock.patch.object(serve, 'mcp_client', fake), \
             mock.patch.object(serve, 'log_action',
                               side_effect=RuntimeError('db down')):
            text = serve._mcp_tool_text(
                'mcp_read', {'server': 's', 'tool': 't',
                             'arguments': {'a': 1}, 'maxBytes': 10})
        self.assertIn('payload', text)
        self.assertEqual(('s', 't', {'a': 1}, True, 10), fake.calls[0])

    def test_run_tool_failures(self):
        denied = _FakeMCP(run_result={'ok': False, 'policy_denied': True,
                                      'error': 'blocked'})
        with mock.patch.object(serve, 'mcp_client', denied):
            self.assertIn('policy denial',
                          serve._mcp_tool_text('mcp_call', {}))
        retryable = _FakeMCP(run_result={'ok': False, 'retryable': False,
                                         'error': 'nope'})
        with mock.patch.object(serve, 'mcp_client', retryable):
            self.assertIn('do not retry',
                          serve._mcp_tool_text('mcp_call', {}))
        empty = _FakeMCP(run_result={'ok': True})
        with mock.patch.object(serve, 'mcp_client', empty):
            self.assertIn('(empty MCP result)',
                          serve._mcp_tool_text('mcp_call', {}))

    def test_run_tool_exception_is_wrapped(self):
        fake = _FakeMCP(run_error=RuntimeError('transport exploded'))
        with mock.patch.object(serve, 'mcp_client', fake):
            text = serve._mcp_tool_text('mcp_call', {})
        self.assertIn('transport exploded', text)
        self.assertIn('do not loop', text)


class DurableToolTextTests(unittest.TestCase):
    def test_unavailable(self):
        with mock.patch.object(serve, 'durable_streams', None):
            self.assertIn('not available',
                          serve._durable_tool_text('list_event_streams', {}))

    def test_list_and_read(self):
        fake = _FakeDurable(read_result={'ok': True, 'text': 'messages',
                                         'messages': [{'id': 1}]})
        with mock.patch.object(serve, 'durable_streams', fake), \
             mock.patch.object(serve, 'log_action',
                               side_effect=RuntimeError('db down')):
            self.assertIn('stream-list',
                          serve._durable_tool_text('list_event_streams', {}))
            text = serve._durable_tool_text(
                'read_event_stream', {'stream': 's', 'max': 2,
                                      'blockMs': 3, 'waitS': 4,
                                      'maxBytes': 5})
        self.assertIn('messages', text)
        self.assertEqual(('read', 's',
                          {'count': 2, 'block_ms': 3, 'wait_s': 4,
                           'max_bytes': 5}), fake.calls[0])

    def test_read_failure(self):
        fake = _FakeDurable(read_result={'ok': False, 'error': 'read failed'})
        with mock.patch.object(serve, 'durable_streams', fake):
            text = serve._durable_tool_text('read_event_stream', {})
        self.assertIn('Event stream read failed', text)

    def test_ack_success_and_failure(self):
        fake = _FakeDurable(ack_result={'ok': True, 'text': 'acked 2',
                                        'acked': 2})
        with mock.patch.object(serve, 'durable_streams', fake), \
             mock.patch.object(serve, 'log_action',
                               side_effect=RuntimeError('db down')):
            self.assertIn('acked 2',
                          serve._durable_tool_text(
                              'ack_event_stream',
                              {'stream': 's', 'receipts': ['r1']}))
        bad = _FakeDurable(ack_result={'ok': False, 'text': 'bad',
                                       'errors': ['e1', 'e2']})
        with mock.patch.object(serve, 'durable_streams', bad):
            text = serve._durable_tool_text('ack_event_stream', {})
        self.assertIn('e1; e2', text)
        unknown = _FakeDurable()
        with mock.patch.object(serve, 'durable_streams', unknown):
            self.assertIn('Unknown durable',
                          serve._durable_tool_text('other', {}))

    def test_exception(self):
        fake = _FakeDurable(error=RuntimeError('boom'))
        with mock.patch.object(serve, 'durable_streams', fake):
            text = serve._durable_tool_text('read_event_stream', {})
        self.assertIn('Event stream call failed: boom', text)


class ObjectToolTextTests(unittest.TestCase):
    def test_unavailable(self):
        with mock.patch.object(serve, 'object_store', None):
            self.assertIn('not available',
                          serve._object_tool_text('list_object_stores', {}))

    def test_list_read_write(self):
        fake = _FakeObject(result={'ok': True, 'text': 'object-text'})
        with mock.patch.object(serve, 'object_store', fake), \
             mock.patch.object(serve, 'log_action',
                               side_effect=RuntimeError('db down')):
            self.assertIn('store-list',
                          serve._object_tool_text('list_object_stores', {}))
            self.assertIn('object-text',
                          serve._object_tool_text(
                              'read_object_store',
                              {'store': 's', 'action': 'get', 'key': 'k'}))
            self.assertIn('object-text',
                          serve._object_tool_text(
                              'write_object_store',
                              {'store': 's', 'action': 'put', 'key': 'k'}))
        self.assertEqual('read', fake.calls[0][0])
        self.assertEqual('write', fake.calls[1][0])

    def test_failures(self):
        failed = _FakeObject(result={'ok': False, 'error': 'denied'})
        with mock.patch.object(serve, 'object_store', failed):
            self.assertIn('Object store call failed: denied',
                          serve._object_tool_text('read_object_store', {}))
        unknown = _FakeObject()
        with mock.patch.object(serve, 'object_store', unknown):
            self.assertIn('Unknown object store tool',
                          serve._object_tool_text('delete_object_store', {}))

    def test_exception(self):
        fake = _FakeObject(error=RuntimeError('store down'))
        with mock.patch.object(serve, 'object_store', fake):
            text = serve._object_tool_text('read_object_store', {})
        self.assertIn('store down', text)


class CliToolTextTests(unittest.TestCase):
    def test_unavailable(self):
        with mock.patch.object(serve, 'cli_tools', None):
            self.assertIn('not available',
                          serve._cli_tool_text('list_cli_tools', {}))

    def test_list_and_run(self):
        fake = _FakeCLI(result={'ok': True, 'text': 'cli-output',
                                'exitCode': 0})
        with mock.patch.object(serve, 'cli_tools', fake), \
             mock.patch.object(serve, 'log_action',
                               side_effect=RuntimeError('db down')):
            self.assertIn('cli-list',
                          serve._cli_tool_text('list_cli_tools', {}))
            self.assertIn('cli-output',
                          serve._cli_tool_text(
                              'read_cli',
                              {'tool': 't', 'args': ['-v'], 'maxBytes': 9}))
        self.assertEqual(('t', ['-v'], True, 9), fake.calls[0])

    def test_failure(self):
        fake = _FakeCLI(result={'ok': False, 'text': 'bad exit'})
        with mock.patch.object(serve, 'cli_tools', fake):
            self.assertIn('CLI call failed: bad exit',
                          serve._cli_tool_text('run_cli', {}))

    def test_exception(self):
        fake = _FakeCLI(error=RuntimeError('spawn failed'))
        with mock.patch.object(serve, 'cli_tools', fake):
            text = serve._cli_tool_text('run_cli', {})
        self.assertIn('spawn failed', text)


if __name__ == '__main__':
    unittest.main()
