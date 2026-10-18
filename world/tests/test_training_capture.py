"""Tests for the local training-interaction recorder.

Hermetic: the module ROOT is pointed at a throwaway temp dir whose
.env holds a made-up secret, so the repo's real credentials are
never read, and TRAINING_CAPTURE_* env knobs point the recorder at
a temp SQLite file. Recording is fail-open by design, so every
failure path is exercised without breaking the caller.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import training_capture  # noqa: E402

_TMP_DIR = None
_ROOT_PATCHER = None
_ENV_PATCHER = None


def setUpModule():
    global _TMP_DIR, _ROOT_PATCHER, _ENV_PATCHER
    _TMP_DIR = Path(tempfile.mkdtemp(prefix='think tank-training-test-'))
    # A fake .env secret: redaction is exercised without ever
    # touching the repo's real credentials.
    (_TMP_DIR / '.env').write_text('TEST_API_KEY=fake-secret-value-123\n')
    _ROOT_PATCHER = unittest.mock.patch.object(training_capture, 'ROOT', _TMP_DIR)
    _ROOT_PATCHER.start()
    _ENV_PATCHER = unittest.mock.patch.dict(os.environ, {
        'TRAINING_CAPTURE_ENABLED': 'true',
        'TRAINING_CAPTURE_PATH': str(_TMP_DIR / 'recordings.sqlite3'),
    })
    _ENV_PATCHER.start()


def tearDownModule():
    _ENV_PATCHER.stop()
    _ROOT_PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class Config(unittest.TestCase):
    def test_config_reads_env_overrides(self):
        values = training_capture.config()
        self.assertEqual(values['TRAINING_CAPTURE_ENABLED'], 'true')
        self.assertIn('recordings.sqlite3', values['TRAINING_CAPTURE_PATH'])
        # The fake .env secret is parsed as a plain key/value pair.
        self.assertEqual(values['TEST_API_KEY'], 'fake-secret-value-123')

    def test_enabled_accepts_affirmative_values(self):
        self.assertTrue(training_capture.enabled())
        with unittest.mock.patch.dict(os.environ, {'TRAINING_CAPTURE_ENABLED': 'no'}):
            self.assertFalse(training_capture.enabled())

    def test_database_resolves_a_relative_path_under_root(self):
        with unittest.mock.patch.dict(os.environ, {'TRAINING_CAPTURE_PATH': 'rel-recordings.sqlite3'}):
            with training_capture.database() as conn:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('interactions', tables)
        self.assertTrue((_TMP_DIR / 'rel-recordings.sqlite3').exists())

    def test_database_creates_the_default_schema(self):
        with training_capture.database() as conn:
            self.assertIs(conn.row_factory, sqlite3.Row)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {'interactions', 'events', 'task_outcomes'})


class Sanitize(unittest.TestCase):
    def test_sensitive_keys_are_redacted(self):
        result, flags = training_capture.sanitize({'api_key': 'abcdefghijklmnop'})
        self.assertEqual(result, {'api_key': '[REDACTED]'})
        self.assertIn('credentials_redacted', flags)

    def test_nested_containers_are_cleaned(self):
        result, _ = training_capture.sanitize(
            {'outer': [{'api_key': 'abcdefghijklmnop'}], 'keep': 5})
        self.assertEqual(result['outer'][0], {'api_key': '[REDACTED]'})
        self.assertEqual(result['keep'], 5)

    def test_json_string_payloads_are_parsed_and_cleaned(self):
        result, flags = training_capture.sanitize(
            '{"note": "the token is fake-secret-value-123"}')
        self.assertEqual(result, '{"note": "the token is [REDACTED]"}')
        self.assertIn('credentials_redacted', flags)

    def test_broken_json_string_is_left_alone(self):
        result, flags = training_capture.sanitize('{not json')
        self.assertEqual(result, '{not json')
        self.assertEqual(flags, [])

    def test_known_secret_values_are_replaced(self):
        result, flags = training_capture.sanitize('token is fake-secret-value-123')
        self.assertEqual(result, 'token is [REDACTED]')
        self.assertIn('credentials_redacted', flags)

    def test_private_keys_are_redacted(self):
        pem = ('-----BEGIN RSA PRIVATE KEY-----\nabc\n'
               '-----END RSA PRIVATE KEY-----')
        result, flags = training_capture.sanitize(pem)
        self.assertEqual(result, '[REDACTED]')
        self.assertIn('credentials_redacted', flags)

    def test_assignments_are_redacted(self):
        result, flags = training_capture.sanitize('api_key = "abcdefghijklmnop"')
        self.assertEqual(result, '[REDACTED]"')
        self.assertIn('credentials_redacted', flags)

    def test_bearer_and_sk_tokens_are_redacted(self):
        result, _ = training_capture.sanitize(
            'Bearer abcdefghijklmnop and sk-or-v1-abcdefghijklmnop')
        self.assertEqual(result, '[REDACTED] and [REDACTED]')

    def test_binary_content_is_omitted(self):
        result, flags = training_capture.sanitize('data:image/png;base64,AAAA')
        self.assertEqual(result, '[BINARY CONTENT OMITTED]')
        self.assertIn('binary_content_omitted', flags)

    def test_scalars_pass_through(self):
        for value in (None, 5, 2.5, True):
            self.assertEqual(training_capture.sanitize(value)[0], value)

    def test_other_types_are_stringified(self):
        result, _ = training_capture.sanitize(object())
        self.assertIsInstance(result, str)

    def test_oversize_payloads_are_replaced_by_digest(self):
        with unittest.mock.patch.dict(os.environ, {'TRAINING_CAPTURE_MAX_BYTES': '64'}):
            result, flags = training_capture.sanitize({'x': 'a' * 100})
        self.assertTrue(result['omitted'])
        self.assertGreater(result['bytes'], 64)
        self.assertIn('sha256', result)
        self.assertIn('oversize_payload_omitted', flags)


class RootStory(unittest.TestCase):
    def test_walks_review_links_to_the_root(self):
        state = {'tasks': {
            't1': {'id': 't1', 'reviewOf': 't2'},
            't2': {'id': 't2', 'investigationOf': 't3'},
            't3': {'id': 't3'},
        }}
        root = training_capture._root_story(state, state['tasks']['t1'])
        self.assertEqual(root['id'], 't3')

    def test_breaks_cycles(self):
        state = {'tasks': {
            't1': {'id': 't1', 'reviewOf': 't2'},
            't2': {'id': 't2', 'reviewOf': 't1'},
        }}
        root = training_capture._root_story(state, state['tasks']['t1'])
        # The walk stops at the first task it sees a second time.
        self.assertEqual(root['id'], 't1')

    def test_non_dict_task_yields_empty(self):
        self.assertEqual(training_capture._root_story({'tasks': {}}, 't1'), {})


class Metadata(unittest.TestCase):
    def test_metadata_attributes_the_call(self):
        state = {
            'agents': {'maya': {'role': 'admin', 'task': 't1'}},
            'tasks': {
                't1': {'id': 't1', 'requestId': 'r1', 'taskType': 'ask',
                       'title': 'How tall is Everest?', 'reviewOf': 't0'},
                't0': {'id': 't0', 'requestId': 'r0'},
            },
        }
        meta = training_capture.metadata(state, 'maya', 't1')
        self.assertEqual(meta['agent_id'], 'maya')
        self.assertEqual(meta['role'], 'admin')
        self.assertEqual(meta['task_id'], 't1')
        self.assertEqual(meta['group_id'], 'r1')
        self.assertEqual(meta['task_type'], 'ask')
        self.assertEqual(meta['title'], 'How tall is Everest?')
        self.assertEqual(meta['root_story_id'], 't0')
        self.assertEqual(meta['request_id'], 'r1')
        self.assertEqual(meta['review_of'], 't0')
        self.assertIsNone(meta['purpose'])

    def test_metadata_resolves_a_string_task(self):
        state = {
            'agents': {'maya': {'role': 'admin', 'task': 't1'}},
            'tasks': {'t1': {'id': 't1', 'requestId': 'r1'}},
        }
        meta = training_capture.metadata(state, 'maya')
        self.assertEqual(meta['task_id'], 't1')
        self.assertEqual(meta['root_story_id'], 't1')

    def test_metadata_with_no_task_yields_empty_fields(self):
        meta = training_capture.metadata({'agents': {'maya': {'role': 'admin'}}}, 'maya')
        self.assertIsNone(meta['task_id'])
        self.assertIsNone(meta['group_id'])
        self.assertIsNone(meta['root_story_id'])

    def test_metadata_with_a_non_dict_task(self):
        state = {'agents': {'maya': {'role': 'admin', 'task': ['t1']}}}
        meta = training_capture.metadata(state, 'maya')
        self.assertIsNone(meta['task_id'])


class Context(unittest.TestCase):
    def test_context_sets_and_restores_the_contextvar(self):
        self.assertIsNone(training_capture.CURRENT.get())
        with training_capture.context({'agent_id': 'maya', 'task_id': 't1'}):
            info = training_capture.CURRENT.get()
            self.assertEqual(info['agent_id'], 'maya')
            self.assertEqual(info['task_id'], 't1')
            self.assertIn('trace_id', info)
        self.assertIsNone(training_capture.CURRENT.get())

    def test_context_inherits_only_the_trace_id(self):
        with training_capture.context({'agent_id': 'maya'}):
            outer = training_capture.CURRENT.get()['trace_id']
            with training_capture.context({'task_id': 't1'}):
                inner = training_capture.CURRENT.get()
            self.assertEqual(inner['trace_id'], outer)
            self.assertEqual(inner['task_id'], 't1')
            self.assertNotIn('agent_id', inner)


class Record(unittest.TestCase):
    def setUp(self):
        with training_capture.database() as conn:
            conn.execute('DELETE FROM interactions')
            conn.execute('DELETE FROM events')
            conn.execute('DELETE FROM task_outcomes')
        training_capture._TRACKED.clear()
        training_capture._OUTCOMES.clear()

    def _rows(self, table):
        with training_capture.database() as conn:
            return [dict(r) for r in conn.execute(f'SELECT * FROM {table}')]

    def test_record_stores_a_successful_interaction(self):
        with training_capture.context({'agent_id': 'maya', 'task_id': 't1',
                                       'group_id': 'g1'}):
            training_capture.record(
                'openrouter', {'model': 'test-model', 'seed': 42},
                {'choices': [{'message': 'hi'}]}, duration_ms=12)
        rows = self._rows('interactions')
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['provider'], 'openrouter')
        self.assertEqual(row['agent_id'], 'maya')
        self.assertEqual(row['task_id'], 't1')
        self.assertEqual(row['group_id'], 'g1')
        self.assertEqual(row['model'], 'test-model')
        self.assertEqual(row['seed'], 42)
        self.assertEqual(row['duration_ms'], 12)
        self.assertEqual(row['ok'], 1)
        payload = json.loads(row['payload'])
        self.assertEqual(payload['request']['model'], 'test-model')
        self.assertEqual(json.loads(row['flags']), [])

    def test_record_flags_a_failed_interaction(self):
        with training_capture.context({'agent_id': 'maya'}):
            training_capture.record(
                'openrouter', {'model': 'test-model'}, {'error': 'boom'})
        rows = self._rows('interactions')
        self.assertEqual(rows[0]['ok'], 0)
        self.assertEqual(json.loads(rows[0]['payload'])['response'],
                         {'error': 'boom'})

    def test_record_stores_the_error_string(self):
        with training_capture.context({'agent_id': 'maya'}):
            training_capture.record(
                'openrouter', {'model': 'test-model'}, None,
                error=RuntimeError('boom'))
        payload = json.loads(self._rows('interactions')[0]['payload'])
        self.assertEqual(payload['error'], 'boom')
        self.assertEqual(self._rows('interactions')[0]['ok'], 0)

    def test_record_without_context_is_unattributed(self):
        training_capture.record('openrouter', {'model': 'test-model'},
                                {'choices': []})
        rows = self._rows('interactions')
        payload = json.loads(rows[0]['payload'])
        self.assertEqual(payload['context']['purpose'], 'unattributed')
        self.assertIn('unattributed_call', json.loads(rows[0]['flags']))

    def test_record_marks_diagnostic_interactions(self):
        with training_capture.context({'purpose': 'health_check'}):
            training_capture.record('openrouter', {'model': 'm'},
                                    {'choices': []})
        rows = self._rows('interactions')
        self.assertIn('diagnostic_interaction', json.loads(rows[0]['flags']))

    def test_record_updates_the_task_tracking_set(self):
        with training_capture.context({'agent_id': 'maya', 'task_id': 't1',
                                       'root_story_id': 't0',
                                       'review_of': 't9'}):
            training_capture.record('openrouter', {'model': 'm'},
                                    {'choices': []})
            training_capture.record('openrouter', {'model': 'm'},
                                    {'choices': []})
        rows = self._rows('interactions')
        self.assertEqual(len(rows), 2)

    def test_record_fails_open(self):
        with unittest.mock.patch.object(training_capture, 'database',
                                        side_effect=RuntimeError('boom')):
            training_capture.record('openrouter', {'model': 'm'},
                                    {'choices': []})

    def test_record_is_a_noop_when_disabled(self):
        with unittest.mock.patch.dict(os.environ,
                                      {'TRAINING_CAPTURE_ENABLED': 'false'}):
            training_capture.record('openrouter', {'model': 'm'},
                                    {'choices': []})
        self.assertEqual(len(self._rows('interactions')), 0)


class Event(unittest.TestCase):
    def setUp(self):
        with training_capture.database() as conn:
            conn.execute('DELETE FROM events')
        training_capture._TRACKED.clear()
        training_capture._OUTCOMES.clear()

    def _count(self, table):
        with training_capture.database() as conn:
            return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def test_event_stores_a_kind_and_payload(self):
        with training_capture.context({'agent_id': 'maya'}):
            training_capture.event('note', 't1', {'note': 'hello'})
        with training_capture.database() as conn:
            rows = [dict(r) for r in conn.execute('SELECT * FROM events')]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'note')
        self.assertEqual(rows[0]['task_id'], 't1')
        self.assertIn('hello', rows[0]['payload'])

    def test_event_fails_open(self):
        with unittest.mock.patch.object(training_capture, 'database',
                                        side_effect=RuntimeError('boom')):
            training_capture.event('note', 't1', {'note': 'hello'})

    def test_event_is_a_noop_when_disabled(self):
        with unittest.mock.patch.dict(os.environ,
                                      {'TRAINING_CAPTURE_ENABLED': 'false'}):
            training_capture.event('note', 't1', {'note': 'hello'})
        self.assertEqual(self._count('events'), 0)


class SyncOutcomes(unittest.TestCase):
    def setUp(self):
        with training_capture.database() as conn:
            conn.execute('DELETE FROM interactions')
            conn.execute('DELETE FROM events')
            conn.execute('DELETE FROM task_outcomes')
        training_capture._TRACKED.clear()
        training_capture._OUTCOMES.clear()

    def _count(self, table):
        with training_capture.database() as conn:
            return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def test_sync_outcomes_links_task_state(self):
        with training_capture.context({'agent_id': 'maya', 'task_id': 't1'}):
            training_capture.record('openrouter', {'model': 'm'},
                                    {'choices': []})
        state = {'tasks': {'t1': {'id': 't1', 'status': 'done',
                                  'pipelineOk': True}}}
        training_capture.sync_outcomes(state)
        with training_capture.database() as conn:
            outcomes = [dict(r) for r in conn.execute('SELECT * FROM task_outcomes')]
            events = [dict(r) for r in conn.execute(
                "SELECT * FROM events WHERE kind = 'task_state'")]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]['task_id'], 't1')
        payload = json.loads(outcomes[0]['payload'])
        self.assertEqual(payload['data']['status'], 'done')
        self.assertEqual(payload['data']['pipelineOk'], True)
        self.assertEqual(len(events), 1)

    def test_sync_outcomes_skips_unknown_tasks(self):
        with training_capture.context({'agent_id': 'maya', 'task_id': 't1'}):
            training_capture.record('openrouter', {'model': 'm'},
                                    {'choices': []})
        training_capture.sync_outcomes({'tasks': {}})
        self.assertEqual(self._count('task_outcomes'), 0)

    def test_sync_outcomes_is_idempotent(self):
        with training_capture.context({'agent_id': 'maya', 'task_id': 't1'}):
            training_capture.record('openrouter', {'model': 'm'},
                                    {'choices': []})
        state = {'tasks': {'t1': {'id': 't1', 'status': 'done'}}}
        training_capture.sync_outcomes(state)
        training_capture.sync_outcomes(state)
        self.assertEqual(self._count('task_outcomes'), 1)

    def test_sync_outcomes_populates_a_fresh_database(self):
        fresh = _TMP_DIR / 'fresh.sqlite3'
        with unittest.mock.patch.dict(os.environ,
                                      {'TRAINING_CAPTURE_PATH': str(fresh)}):
            training_capture.sync_outcomes({'tasks': {}})
        self.assertTrue(fresh.exists())

    def test_sync_outcomes_fails_open(self):
        with unittest.mock.patch.object(training_capture, 'database',
                                        side_effect=RuntimeError('boom')):
            training_capture.sync_outcomes({'tasks': {}})

    def test_sync_outcomes_is_a_noop_when_disabled(self):
        with unittest.mock.patch.dict(os.environ,
                                      {'TRAINING_CAPTURE_ENABLED': 'false'}):
            training_capture.sync_outcomes({'tasks': {}})
        self.assertEqual(self._count('task_outcomes'), 0)


if __name__ == '__main__':
    unittest.main()
