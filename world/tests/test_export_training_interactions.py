"""Tests for the training-interaction export tool.

Hermetic: a throwaway SQLite database stands in for the real
recording store, and the recorder module's ROOT is pointed at a
temp dir whose .env holds a made-up secret, so credential
redaction is exercised without ever touching the repo's real
credentials.
"""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_WORLD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_WORLD)
sys.path.insert(0, os.path.join(_REPO, 'scripts'))
sys.path.insert(0, _WORLD)

import training_capture  # noqa: E402
import export_training_interactions as export  # noqa: E402

_TMP_DIR = None
_ROOT_PATCHER = None


def setUpModule():
    global _TMP_DIR, _ROOT_PATCHER
    _TMP_DIR = Path(tempfile.mkdtemp(prefix='think tank-export-test-'))
    # A fake .env secret: redaction is exercised without ever
    # touching the repo's real credentials.
    (_TMP_DIR / '.env').write_text('TEST_API_KEY=fake-secret-value-123\n')
    _ROOT_PATCHER = unittest.mock.patch.object(training_capture, 'ROOT', _TMP_DIR)
    _ROOT_PATCHER.start()


def tearDownModule():
    _ROOT_PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


_SCHEMA = '''
CREATE TABLE IF NOT EXISTS interactions (
  id TEXT PRIMARY KEY, ts REAL NOT NULL, trace_id TEXT NOT NULL,
  agent_id TEXT, task_id TEXT, group_id TEXT NOT NULL, provider TEXT,
  model TEXT, seed INTEGER, duration_ms INTEGER, ok INTEGER NOT NULL,
  payload TEXT NOT NULL, flags TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'pending', review_note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, trace_id TEXT, task_id TEXT,
  kind TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_outcomes (
  task_id TEXT PRIMARY KEY, updated_at REAL NOT NULL, payload TEXT NOT NULL
);
'''

_INSERT = ('INSERT INTO interactions (id,ts,trace_id,agent_id,task_id,group_id,provider,'
           'model,seed,duration_ms,ok,payload,flags,review_status,review_note) '
           'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)')


def _payload(messages=None, content='hello world', tools=None, refusal=None,
             finish_reason='stop', tool_calls=None):
    request = {'model': 'test-model',
               'messages': messages if messages is not None else [{'role': 'user', 'content': 'hi there'}]}
    if tools is not None:
        request['tools'] = tools
    message = {'role': 'assistant'}
    if content is not None:
        message['content'] = content
    if tool_calls is not None:
        message['tool_calls'] = tool_calls
    if refusal is not None:
        message['refusal'] = refusal
    return {'request': request,
            'response': {'choices': [{'message': message, 'finish_reason': finish_reason}]}}


def _insert_row(conn, row_id, *, ok=1, payload=None, flags=None, review_status='approved',
                group_id='g1', task_id='t1', agent_id='agent-1'):
    conn.execute(_INSERT, (row_id, 1.0, 'trace-1', agent_id, task_id, group_id, 'openrouter',
                           'test-model', 7, 1, ok,
                           json.dumps(payload if payload is not None else _payload()),
                           json.dumps(flags if flags is not None else []), review_status, ''))


class ExportCase(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix='think tank-export-out-'))
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        self.db = self.out / 'recordings.sqlite3'
        self.conn = sqlite3.connect(str(self.db))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.addCleanup(self.conn.close)

    def _insert(self, row_id, **kwargs):
        _insert_row(self.conn, row_id, **kwargs)

    def _add_outcome(self, task_id, data):
        self.conn.execute('INSERT INTO task_outcomes VALUES (?,?,?)',
                          (task_id, 1.0, json.dumps(data)))


class ExportRows(ExportCase):
    def test_export_classifies_every_rejection_and_skip(self):
        messages = [{'role': 'user', 'content': 'hi there'}]
        other = [{'role': 'user', 'content': 'a different question entirely'}]
        tools = [{'type': 'function', 'function': {'name': 'f', 'description': 'do f'}}]
        # Rejected before candidacy: failed, empty, or contentless.
        self._insert('r-failed', ok=0)
        self._insert('r-empty-message',
                     payload={'request': {'model': 'm', 'messages': messages},
                              'response': {'choices': [{'message': {}, 'finish_reason': 'stop'}]}})
        self._insert('r-no-content', payload=_payload(content=None))
        self._insert('r-no-request', payload={'response': {}})
        self._insert('r-messages-not-list', payload=_payload(messages='not-a-list'))
        self._insert('r-message-no-role', payload=_payload(messages=[{'content': 'x'}]))
        # Candidates: accepted, grouped, or stopped at the review gates.
        self._insert('c-tools', payload=_payload(tools=tools), group_id='g1', task_id='task-tools')
        self._insert('c-same-input', payload=_payload(), group_id='g2')
        self._insert('c-distinct', payload=_payload(messages=other, content='another answer'),
                     group_id='g3')
        self._insert('c-dup', payload=_payload(tools=tools), group_id='g4')
        self._insert('c-redacted', payload=_payload(content='key is fake-secret-value-123'))
        self._insert('c-flags', flags=['diagnostic_interaction'])
        self._insert('c-pending', review_status='pending')
        self._insert('c-long', payload=_payload(content='x' * 1000))
        self._insert('c-refusal', payload=_payload(refusal='no'))
        self._insert('c-length', payload=_payload(finish_reason='length'))
        self._add_outcome('task-tools', {'status': 'done'})
        summary = export.export(self.conn, self.out, fraction=0.1, max_chars=400)
        self.assertEqual(summary['candidates'], 10)
        self.assertEqual(summary['groups'], 2)
        self.assertEqual(summary['train'] + summary['eval'], 3)
        self.assertEqual(summary['skipped'], {
            'failed_or_empty': 4, 'invalid_messages': 2, 'redacted_or_omitted': 2,
            'not_approved': 1, 'over_character_limit': 1, 'incomplete_or_refused': 2,
            'duplicate': 1})
        candidates = [json.loads(line)
                      for line in (self.out / 'candidates.jsonl').read_text(encoding='utf-8').splitlines()]
        by_id = {row['id']: row for row in candidates}
        self.assertEqual(by_id['c-tools']['outcome'], {'status': 'done'})
        self.assertIsNone(by_id['c-same-input']['outcome'])
        manifest = [json.loads(line)
                    for line in (self.out / 'manifest.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(manifest), 3)
        for name in ('candidates.jsonl', 'manifest.jsonl', 'train.jsonl', 'eval.jsonl', 'summary.json'):
            self.assertTrue((self.out / name).is_file(), name)
        text = (self.out / 'candidates.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('fake-secret-value-123', text)
        self.assertIn('[REDACTED]', text)

    def test_export_accepts_tool_calls_without_content(self):
        self._insert('c1', payload=_payload(
            content=None,
            tool_calls=[{'id': 'call-1', 'type': 'function',
                         'function': {'name': 'f', 'arguments': '{}'}}]))
        summary = export.export(self.conn, self.out)
        self.assertEqual(summary['train'] + summary['eval'], 1)
        self.assertEqual(summary['skipped'], {})

    def test_export_single_group_has_no_eval_split(self):
        self._insert('c1')
        self._insert('c2', payload=_payload(content='another answer'))
        summary = export.export(self.conn, self.out)
        self.assertEqual(summary['groups'], 1)
        self.assertEqual(summary['eval'], 0)
        self.assertEqual(summary['train'], 2)

    def test_export_eval_fraction_splits_multiple_groups(self):
        for index in range(3):
            self._insert(f'c{index}', group_id=f'g{index}',
                         payload=_payload(messages=[{'role': 'user', 'content': f'question {index}'}],
                                          content=f'answer {index}'))
        summary = export.export(self.conn, self.out, fraction=0.5)
        self.assertEqual(summary['groups'], 3)
        self.assertEqual(summary['eval'], 2)
        self.assertEqual(summary['train'], 1)


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='think tank-export-main-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = self.tmp / 'recordings.sqlite3'
        conn = sqlite3.connect(str(self.db))
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()

    def _insert(self, row_id, **kwargs):
        conn = sqlite3.connect(str(self.db))
        conn.row_factory = sqlite3.Row
        try:
            _insert_row(conn, row_id, **kwargs)
            conn.commit()
        finally:
            conn.close()

    def _run(self, *argv):
        with unittest.mock.patch('sys.argv', ['export_training_interactions.py',
                                              '--db', str(self.db), *argv]):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                export.main()
        return out.getvalue()

    def _run_error(self, *argv):
        with unittest.mock.patch('sys.argv', ['export_training_interactions.py',
                                              '--db', str(self.db), *argv]):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    export.main()

    def test_stats_reports_counts(self):
        self._insert('r1', ok=0, review_status='rejected')
        self._insert('r2')
        stats = json.loads(self._run('stats'))
        self.assertEqual(stats['interactions'], 2)
        self.assertEqual(stats['events'], 0)
        self.assertEqual(stats['reviews'], {'approved': 1, 'rejected': 1})

    def test_list_limits_rows(self):
        for index in range(3):
            self._insert(f'r{index}')
        lines = [line for line in self._run('list', '--limit', '2').splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)

    def test_show_prints_payload_and_recorded_outcome(self):
        self._insert('r1', task_id='task-1')
        conn = sqlite3.connect(str(self.db))
        conn.execute('INSERT INTO task_outcomes VALUES (?,?,?)',
                     ('task-1', 1.0, json.dumps({'status': 'done'})))
        conn.commit()
        conn.close()
        output = self._run('show', 'r1')
        self.assertIn('"role": "assistant"', output)
        self.assertIn('Recorded outcome: {"status": "done"}', output)

    def test_show_without_outcome_omits_outcome_line(self):
        self._insert('r1', task_id='task-9')
        self.assertNotIn('Recorded outcome', self._run('show', 'r1'))

    def test_show_unknown_id_exits(self):
        self._run_error('show', 'nope')

    def test_review_approves_clean_row(self):
        self._insert('r1', review_status='pending')
        self.assertIn('Review saved.',
                      self._run('review', 'r1', '--status', 'approved', '--note', 'looks good'))
        conn = sqlite3.connect(str(self.db))
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT review_status, review_note FROM interactions WHERE id=?',
                           ('r1',)).fetchone()
        events = conn.execute("SELECT COUNT(*) FROM events WHERE kind='training_review'").fetchone()[0]
        conn.close()
        self.assertEqual((row['review_status'], row['review_note']), ('approved', 'looks good'))
        self.assertEqual(events, 1)

    def test_review_redacts_note(self):
        self._insert('r1', review_status='pending')
        self._run('review', 'r1', '--status', 'rejected', '--note', 'key is fake-secret-value-123')
        conn = sqlite3.connect(str(self.db))
        note = conn.execute('SELECT review_note FROM interactions WHERE id=?', ('r1',)).fetchone()[0]
        conn.close()
        self.assertEqual(note, 'key is [REDACTED]')

    def test_review_unknown_id_exits(self):
        self._run_error('review', 'nope', '--status', 'approved', '--note', 'x')

    def test_review_cannot_approve_failed_row(self):
        self._insert('r1', ok=0)
        self._run_error('review', 'r1', '--status', 'approved', '--note', 'x')

    def test_review_cannot_approve_flagged_row(self):
        self._insert('r1', flags=['diagnostic_interaction'])
        self._run_error('review', 'r1', '--status', 'approved', '--note', 'x')

    def test_export_prints_summary(self):
        self._insert('r1')
        out_dir = self.tmp / 'export'
        summary = json.loads(self._run('export', '--out', str(out_dir),
                                       '--eval-fraction', '0.1', '--max-chars', '40000'))
        self.assertEqual(summary['candidates'], 1)
        self.assertTrue((out_dir / 'summary.json').is_file())

    def test_export_rejects_bad_fraction(self):
        self._run_error('export', '--out', str(self.tmp / 'x'), '--eval-fraction', '0')

    def test_export_rejects_bad_max_chars(self):
        self._run_error('export', '--out', str(self.tmp / 'x'), '--max-chars', '0')

    def test_missing_database_exits(self):
        with unittest.mock.patch('sys.argv', ['export_training_interactions.py',
                                              '--db', str(self.tmp / 'absent.sqlite3'), 'stats']):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    export.main()


if __name__ == '__main__':
    unittest.main()
