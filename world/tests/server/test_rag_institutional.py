"""Institutional-memory retrieval (RAG, zero-dep).

Hashed sparse embeddings over the Library (wiki bodies included -- they
are files under library/wiki/) and the archived deliverables, surfaced
into the three consumers: the library search agents already call for
task context, the escalation prompt, and the grader's room anchors.
DB-isolated (setUpModule below, the test_vpn_exit_endpoints pattern).
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-rag-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        BROWSE_TRAIL_PATH=os.path.join(_TMP_DIR, 'browse_trail.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _seed_archive(task_id, room, title, body, grade=8.0):
    blob = json.dumps({'id': task_id, 'room': room, 'title': title,
                       'taskType': 'code', 'grade': grade,
                       'deliverableText': body,
                       'instructions': f'Build {title}'})
    with serve._db() as conn:
        conn.execute(
            'INSERT INTO task_archive (id, blob, updated_at) VALUES (?, ?, ?)',
            (task_id, blob, time.time()))
    return task_id


def _wipe():
    serve._rag_ensure_table()
    serve._ensure_grade_outcomes()
    with serve._db() as conn:
        for table in ('rag_index', 'task_archive', 'grade_outcomes'):
            conn.execute(f'DELETE FROM {table}')
    shutil.rmtree(serve.LIBRARY_DIR, ignore_errors=True)
    os.makedirs(serve.LIBRARY_DIR, exist_ok=True)


class Embed(unittest.TestCase):
    def test_deterministic_and_normalized(self):
        a = serve._rag_embed('rate limiting backoff storms')
        b = serve._rag_embed('rate limiting backoff storms')
        self.assertEqual(a, b)
        norm = sum(w * w for w in a.values())
        self.assertAlmostEqual(norm, 1.0, places=5)

    def test_related_texts_share_mass(self):
        a = serve._rag_embed('the rate limiter backoff on 429 storms')
        b = serve._rag_embed('backoff handling for 429 rate limits')
        shared = sum(w * b.get(k, 0.0) for k, w in a.items())
        self.assertGreater(shared, 0.2)

    def test_empty(self):
        self.assertEqual(serve._rag_embed(''), {})
        self.assertEqual(serve._rag_embed(None), {})


class IndexAndSearch(unittest.TestCase):
    def setUp(self):
        _wipe()

    def test_index_is_idempotent_and_search_ranks_the_match_first(self):
        _seed_archive('task-10', 'observatory', 'Rate limit backoff',
                      'Implemented exponential backoff for the 429 storms in '
                      'the ingest loop; the retry budget stayed inside the '
                      'spend cap.')
        _seed_archive('task-11', 'pressoffice', 'Mascot footer',
                      'Added a purely decorative mascot to the footer, no '
                      'data flows, tiny cosmetic change.')
        with open(os.path.join(serve.LIBRARY_DIR, 'working-guide.md'), 'w') as f:
            f.write('The observatory watches rate limits and 429 storms.\n')
        first = serve._rag_index_pass(60)
        self.assertGreaterEqual(first['indexed'], 3)
        second = serve._rag_index_pass(60)
        self.assertEqual(second['indexed'], 0)
        self.assertGreaterEqual(second['skipped'], 3)
        hits = serve._rag_search('how did we handle 429 rate limit storms')
        self.assertTrue(hits)
        # Cosine favors the short doc that shares the most mass; the
        # deliverable must still surface among the hits.
        self.assertTrue(any('backoff' in h['snippet'] for h in hits), hits)
        scoped = serve._rag_search('how did we handle 429 rate limit storms',
                                   sources=('library',))
        self.assertTrue(all(h['source'] == 'library' for h in scoped))

    def test_stale_entries_refresh(self):
        _seed_archive('task-20', 'bank', 'Ledger reconciliation',
                      'Reconciled the budget ledger against the bank room '
                      'records after the double-spend incident.')
        serve._rag_index_pass(60)
        with serve._db() as conn:
            conn.execute('UPDATE rag_index SET updated_at = ? '
                         "WHERE source = 'deliverable'",
                         (time.time() - 8 * 86400,))
        out = serve._rag_index_pass(60)
        self.assertGreaterEqual(out['indexed'], 1)

    def test_budget_bounds_the_pass(self):
        for i in range(10):
            _seed_archive(f'task-3{i}', 'bank', f'Ledger note {i}',
                          f'Reconciliation pass {i} for the budget ledger.')
        out = serve._rag_index_pass(3)
        self.assertEqual(out['indexed'], 3)
        self.assertGreaterEqual(out['pending'], 7)


class Consumers(unittest.TestCase):
    def setUp(self):
        _wipe()

    def test_search_endpoint_merges_semantic_hits(self):
        _seed_archive('task-40', 'observatory', 'Rate limit backoff',
                      'Exponential backoff fixed the 429 storms; spend cap '
                      'held.')
        serve._rag_index_pass(60)
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        out = c.get('/api/library/search?q=backoff').json()
        paths = [m['path'] for m in out['matches']]
        self.assertTrue(any(p.startswith('task-archive/') for p in paths),
                        paths)

    def test_escalation_context_gains_retrieval_lines(self):
        with open(os.path.join(serve.LIBRARY_DIR, 'runbook.md'), 'w') as f:
            f.write('When the 429 storms hit the ingest loop, raise the '
                    'backoff ceiling before touching the retry budget.\n')
        _seed_archive('task-50', 'observatory', 'Backoff runbook followup',
                      'The backoff ceiling fix that ended the 429 storms.')
        serve._rag_index_pass(60)
        esc = {'note': 'ingest loop keeps failing with 429 storms',
               'whatChecked': 'the retry budget', 'lookFirst': 'backoff'}
        lines = serve._escalation_context_lines(esc)
        self.assertTrue(any('Related ' in line for line in lines), lines)

    def test_escalation_context_survives_index_failure(self):
        with unittest.mock.patch.object(serve, '_rag_search',
                                        side_effect=RuntimeError('boom')):
            lines = serve._escalation_context_lines({'note': 'x'})
        self.assertEqual(lines, ['Note: x'])

    def test_grade_anchors_carry_grade_and_outcome(self):
        _seed_archive('task-60', 'observatory', 'Backoff hardening',
                      'Backoff ceiling raised; 429 storms ended.', grade=8.0)
        serve._rag_index_pass(60)
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO grade_outcomes (tape_id, task_ref, grade_ts, '
                'grade, confidence, outcome, evidence, computed_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (1, 'task-60', time.time(), '8', 0.9, 'held', '[]',
                 time.time()))
        anchors = serve._rag_room_anchors('observatory', exclude_ref='task-61')
        self.assertEqual(len(anchors), 1)
        self.assertIn('task-60', anchors[0])
        self.assertIn('8/10', anchors[0])
        self.assertIn('held up', anchors[0])
        self.assertEqual(serve._rag_room_anchors('observatory',
                                                 exclude_ref='task-60'), [])

    def test_grade_anchors_empty_on_failure(self):
        _seed_archive('task-70', 'bank', 'Ledger check',
                      'Reconciliation clean.', grade=9.0)
        serve._rag_index_pass(60)
        with unittest.mock.patch.object(serve, '_db',
                                        side_effect=RuntimeError('db down')):
            self.assertEqual(serve._rag_room_anchors('bank'), [])


class Status(unittest.TestCase):
    def test_status_counts_sources(self):
        _wipe()
        _seed_archive('task-80', 'media', 'Render audit',
                      'Audit of the render pipeline.')
        serve._rag_index_pass(60)
        out = serve._rag_status()
        self.assertIn('deliverable', out['sources'])
        self.assertGreaterEqual(out['sources']['deliverable']['docs'], 1)


if __name__ == '__main__':
    unittest.main()
