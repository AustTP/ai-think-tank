"""Coverage-gap tests for the large shared web-tools executor factory.

Only new tests. Every tool call is invoked directly with serve._http_json (and
a few local helpers) patched, so no network, browser, DB, or model call runs.
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


def _exec(**kwargs):
    return serve._make_web_tools_executor('ada', 'key-123', **kwargs)


def _http(fake):
    return mock.patch.object(serve, '_http_json', side_effect=fake)


class DispatchGapTests(unittest.TestCase):
    def test_family_dispatch_lines(self):
        tool = _exec()
        cases = (
            ('mcp_list_servers', '_mcp_tool_text'),
            ('read_event_stream', '_durable_tool_text'),
            ('list_object_stores', '_object_tool_text'),
            ('list_cli_tools', '_cli_tool_text'),
        )
        for name, target in cases:
            with mock.patch.object(serve, target,
                                   return_value=f'called:{name}') as patched:
                self.assertEqual(f'called:{name}', tool(name, {}))
            self.assertEqual(name, patched.call_args.args[0])


class CredentialAccessGapTests(unittest.TestCase):
    def test_needs_active_task(self):
        tool = _exec(task_id=None)
        text = tool('request_credential_access', {'service': 'github'})
        self.assertIn('needs the active taskId', text)

    def test_granted(self):
        calls = []

        def fake(method, base, path, body=None, header=None, timeout=None):
            calls.append((method, path, body, header, timeout))
            return {'ok': True, 'service': 'github', 'handle': 'h1',
                    'hosts': ['api.github.com', 'github.com'],
                    'expiresInSec': 120}

        tool = _exec()
        with _http(fake):
            text = tool('request_credential_access',
                        {'service': 'GitHub', 'taskId': 't1',
                         'purpose': 'read issues'})
        self.assertIn('Granted read-only github access', text)
        self.assertIn('api.github.com, github.com', text)
        self.assertEqual('t1', calls[0][2]['taskId'])

    def test_refused(self):
        def fake(*args, **kwargs):
            return {'error': 'not allowed'}

        tool = _exec(task_id='t1')
        with _http(fake):
            text = tool('request_credential_access', {'service': 'x'})
        self.assertIn('Credential access refused: not allowed', text)


class LiveCoverageGapTests(unittest.TestCase):
    def test_read_bad_max_and_error(self):
        def fake(method, base, path, body=None, header=None, timeout=None):
            self.assertEqual('GET', method)
            self.assertIn('/api/live-coverage/status', path)
            return {'error': 'status down'}

        tool = _exec()
        with _http(fake):
            text = tool('read_live_coverage', {'max': 'not-a-number'})
        self.assertIn('Could not read live coverage: status down', text)

    def test_read_run_with_run_id_and_no_observations(self):
        seen = {}

        def fake(method, base, path, body=None, header=None, timeout=None):
            seen['path'] = path
            return {'run': {'id': 'r1', 'status': 'active', 'label': 'cam',
                            'stream': 'u', 'frames': 1, 'maxFrames': 10,
                            'spentUsd': 0.1, 'observations': []}}

        tool = _exec()
        with _http(fake):
            text = tool('read_live_coverage', {'run_id': 'run/1', 'max': 5})
        self.assertIn('run_id=run/1', seen['path'])
        self.assertIn('no observations yet', text)

    def test_read_without_a_run(self):
        def fake(*args, **kwargs):
            return {'run': None}

        tool = _exec()
        with _http(fake):
            text = tool('read_live_coverage', {})
        self.assertIn('No live coverage runs yet', text)

    def test_start_success_and_failure(self):
        def success(*args, **kwargs):
            return {'ok': True, 'run': {'id': 'r2', 'intervalS': 5,
                                        'maxSeconds': 60, 'maxFrames': 12,
                                        'maxUsd': 1.5}}

        tool = _exec()
        with _http(success):
            text = tool('start_live_coverage',
                        {'url': 'https://example.test/s', 'label': 'l',
                         'interval_s': 5, 'max_seconds': 60,
                         'max_frames': 12, 'max_usd': 1.5})
        self.assertIn('Live coverage started: run r2', text)
        self.assertIn('$1.5', text)

        with _http(lambda *a, **k: {'error': 'no budget'}):
            text = tool('start_live_coverage', {})
        self.assertIn('Could not start live coverage: no budget', text)

    def test_stop_paths(self):
        tool = _exec()
        self.assertIn('needs a run_id', tool('stop_live_coverage', {}))
        with _http(lambda *a, **k: {'ok': True}):
            self.assertIn('r3 is stopping',
                          tool('stop_live_coverage', {'run_id': 'r3'}))
        with _http(lambda *a, **k: {'error': 'unknown run'}):
            self.assertIn('Could not stop live coverage: unknown run',
                          tool('stop_live_coverage', {'run_id': 'r3'}))


class SelfEditToolGapTests(unittest.TestCase):
    def test_self_edit_paths(self):
        tool = _exec()
        self.assertIn('self_edit requires',
                      tool('self_edit', {}))
        body = {'path': 'p', 'find': 'f', 'replace': 'r', 'summary': 'why'}
        with _http(lambda *a, **k: {'error': 'anchor missing'}):
            self.assertIn('self_edit not filed: anchor missing',
                          tool('self_edit', body))
        with _http(lambda *a, **k: {'other': 'shape'}):
            self.assertIn('unexpected response', tool('self_edit', body))

    def test_status_empty_and_rich(self):
        tool = _exec()
        with _http(lambda *a, **k: {'edits': []}):
            self.assertIn('No self-edits have been proposed',
                          tool('self_edit_status', {'limit': 'bad'}))
        rich = {
            'edits': [{
                'id': 'e1', 'status': 'applied', 'path': 'world/x.py',
                'agent': 'ada', 'jevConfidence': 0.9, 'summary': 's',
                'validator': 'validator-note', 'note': 'edit-note',
            }],
            'windows': [{
                'id': 'w1', 'status': 'pending', 'date': '2026-11-01',
                'start': '01:00', 'durationMin': 30, 'agent': 'ada',
                'validator': 'v', 'validatorStatus': 'ok',
                'pushStatus': 'pushed', 'pushCategory': 'cat',
                'pushSha': 'abc123', 'reason': 'r', 'note': 'n',
            }],
        }
        with _http(lambda *a, **k: rich):
            text = tool('self_edit_status', {'limit': 5})
        self.assertIn('SELF-EDIT LEDGER', text)
        self.assertIn('DORMANCY WINDOWS', text)
        self.assertIn('push: pushed / cat abc123', text)
        with _http(lambda *a, **k: {'error': 'ledger down'}):
            self.assertIn('Could not read the self-edit ledger: ledger down',
                          tool('self_edit_status', {}))

    def test_schedule_paths(self):
        tool = _exec()
        self.assertIn('requires date', tool('schedule_dormant_apply', {}))
        body = {'date': '2026-11-01', 'start': '01:00', 'reason': 'r',
                'validator': 'v', 'durationMin': 'bad', 'title': 't'}
        with _http(lambda *a, **k: {'other': 'shape'}):
            self.assertIn('unexpected response',
                          tool('schedule_dormant_apply', body))

    def test_fix_paths(self):
        tool = _exec()
        self.assertIn('selfedit_fix requires', tool('selfedit_fix', {}))
        body = {'path': 'p', 'find': 'f', 'replace': 'r', 'summary': 's'}
        with _http(lambda *a, **k: {'error': 'session closed'}):
            self.assertIn('selfedit_fix not applied: session closed',
                          tool('selfedit_fix', body))
        with _http(lambda *a, **k: {'other': 'shape'}):
            self.assertIn('unexpected response', tool('selfedit_fix', body))

    def test_recheck_paths(self):
        tool = _exec()
        with _http(lambda *a, **k: {'error': 'checks unavailable'}):
            self.assertIn('selfedit_recheck: checks unavailable',
                          tool('selfedit_recheck', {}))
        with _http(lambda *a, **k: {'tests': 'fail', 'tail': 'x' * 1500}):
            text = tool('selfedit_recheck', {})
        self.assertIn('still FAILING', text)
        with _http(lambda *a, **k: 'not-a-dict'):
            self.assertIn('unexpected response',
                          tool('selfedit_recheck', {}))

    def test_read_paths(self):
        tool = _exec()
        with _http(lambda *a, **k: {'error': 'read denied'}):
            self.assertIn('selfedit_read: read denied',
                          tool('selfedit_read', {}))
        modules = {'modules': [{'path': f'm{i}.py', 'bytes': i}
                               for i in range(61)]}
        with _http(lambda *a, **k: modules):
            text = tool('selfedit_read', {})
        self.assertIn('READABLE self-edit surface', text)
        self.assertIn('1 more; use selfedit_read with a path', text)
        text_result = {
            'text': 'body', 'sha256': 'abc', 'totalBytes': 4,
            'truncated': True, 'matchLine': 3, 'startLine': 1,
            'endLine': 5, 'totalLines': 9, 'path': 'world/x.py',
        }
        with _http(lambda *a, **k: text_result):
            text = tool('selfedit_read', {'path': 'world/x.py'})
        self.assertIn('[lines 1-5 of 9; match at 3]', text)
        self.assertIn('truncated', text)
        with _http(lambda *a, **k: {'other': 'shape'}):
            self.assertIn('unexpected response', tool('selfedit_read', {}))

    def test_validate_paths(self):
        tool = _exec()
        self.assertIn('requires windowId', tool('selfedit_validate', {}))
        body = {'windowId': 'w1', 'approved': True}
        with _http(lambda *a, **k: {'error': 'wrong validator'}):
            self.assertIn('selfedit_validate refused: wrong validator',
                          tool('selfedit_validate', body))
        with _http(lambda *a, **k: {'other': 'shape'}):
            self.assertIn('unexpected response',
                          tool('selfedit_validate', body))

    def test_veto_paths(self):
        tool = _exec()
        self.assertIn('requires windowId', tool('veto_change_request', {}))
        body = {'windowId': 'w1', 'reason': 'bad change'}
        with _http(lambda *a, **k: {'error': 'not the player'}):
            self.assertIn('veto_change_request refused: not the player',
                          tool('veto_change_request', body))
        with _http(lambda *a, **k: {'other': 'shape'}):
            self.assertIn('unexpected response',
                          tool('veto_change_request', body))

    def test_policy_edit_paths(self):
        tool = _exec()
        self.assertIn('requires key and value', tool('policy_edit', {}))
        self.assertIn('requires reason',
                      tool('policy_edit', {'key': 'K', 'value': 1}))
        body = {'key': 'K', 'value': 2, 'reason': 'because'}
        with _http(lambda *a, **k: {'error': 'reviewer said no'}):
            self.assertIn('policy_edit: reviewer said no',
                          tool('policy_edit', body))
        with _http(lambda *a, **k: {'ok': True, 'key': 'K',
                                    'previous': 1, 'value': 2}):
            text = tool('policy_edit', body)
        self.assertIn('Policy applied after review', text)
        self.assertIn("'K' 1 -> 2", text)
        with _http(lambda *a, **k: {'other': 'shape'}):
            self.assertIn('unexpected response', tool('policy_edit', body))


if __name__ == '__main__':
    unittest.main()
