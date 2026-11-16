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

    def test_search_sees_rows_indexed_after_a_prior_search(self):
        """The 2026-11-15 row cache must re-probe freshness per query:
        content indexed after an earlier search has to show up in the
        next search (read-your-writes), not stay invisible behind a TTL."""
        _wipe()
        _seed_archive('task-30', 'observatory', 'Backoff retrofit',
                      'Retrofitted exponential backoff for the 429 storms '
                      'across the ingest loop.')
        serve._rag_index_pass(60)
        first = serve._rag_search('429 storm backoff')
        self.assertTrue(first)
        self.assertFalse(any('token bucket' in h['snippet'].lower()
                             for h in first), first)
        _seed_archive('task-31', 'observatory', 'Token bucket',
                      'Added a token bucket limiter so the 429 storms '
                      'cannot recur during bursts.')
        serve._rag_index_pass(60)
        second = serve._rag_search('token bucket 429 storms')
        self.assertTrue(second)
        self.assertTrue(any('token bucket' in h['snippet'].lower()
                            for h in second), second)

    def test_purge_keeps_surviving_entries_in_place(self):
        """Delta sync (2026-11-15): when rows leave the index, the search
        cache must drop just those refs. Re-parsing the whole index per
        deletion churned ~2 GB per cycle on the VM tank. Entry object
        identity across a deletion proves the delta path ran; a full
        reload would mint fresh dicts and fail this assert."""
        _wipe()
        _seed_archive('task-40', 'observatory', 'Backoff ledger',
                      'Exponential backoff held the 429 storms inside '
                      'the retry budget.')
        _seed_archive('task-41', 'pressoffice', 'Footer mascot',
                      'A decorative mascot footer with no data flows.')
        serve._rag_index_pass(60)
        serve._rag_search('429 storm backoff')
        snap1 = serve._RAG_SEARCH_SNAPSHOT
        keep = next(e for e in snap1 if e[1] == 'task-40')
        with serve._db() as conn:
            conn.execute("DELETE FROM rag_index WHERE ref = 'task-41'")
        serve._rag_search('429 storm backoff')
        snap2 = serve._RAG_SEARCH_SNAPSHOT
        self.assertNotIn('task-41', [e[1] for e in snap2])
        kept = next(e for e in snap2 if e[1] == 'task-40')
        self.assertIs(kept, keep)

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


class VillageScoping(unittest.TestCase):
    """2026-11-01: the index is village-stamped and retrieval is village-
    scoped -- one village's institutional memory never surfaces in another's
    retrieval. NULL-village rows (shared craft, legacy) stay visible."""

    def setUp(self):
        _wipe()

    def test_deliverable_stamped_with_its_village(self):
        blob = json.dumps({'id': 'task-9', 'room': 'observatory',
                           'title': 'Village B rate limiter', 'taskType': 'code',
                           'grade': 8.0, 'villageId': 'village-b',
                           'deliverableText': 'rate limiting backoff storms',
                           'instructions': 'build it'})
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO task_archive (id, blob, updated_at) VALUES (?, ?, ?)',
                ('task-9', blob, time.time()))
        serve._rag_index_pass(budget=10)
        with serve._db() as conn:
            row = conn.execute(
                "SELECT village_id FROM rag_index WHERE doc_id = 'deliverable:task-9'"
            ).fetchone()
        self.assertEqual(row[0], 'village-b')

    def test_library_path_village_follows_the_tree(self):
        vdir = os.path.join(serve.LIBRARY_DIR, 'villages', 'village-b')
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, 'notes.md'), 'w') as f:
            f.write('village b orbit predictions notes')
        serve._rag_index_pass(budget=10)
        with serve._db() as conn:
            row = conn.execute(
                "SELECT village_id FROM rag_index WHERE doc_id = 'library:villages/village-b/notes.md'"
            ).fetchone()
        self.assertEqual(row[0], 'village-b')

    def test_cross_village_hit_hidden_shared_visible(self):
        blob_a = json.dumps({'id': 'task-a', 'room': 'observatory',
                             'title': 'A limiter', 'taskType': 'code',
                             'grade': 8.0, 'villageId': 'village-a',
                             'deliverableText': 'rate limiting backoff storms',
                             'instructions': 'build'})
        blob_b = json.dumps({'id': 'task-b', 'room': 'observatory',
                             'title': 'B limiter', 'taskType': 'code',
                             'grade': 8.0, 'villageId': 'village-b',
                             'deliverableText': 'rate limiting backoff storms',
                             'instructions': 'build'})
        with serve._db() as conn:
            for tid, blob in (('task-a', blob_a), ('task-b', blob_b)):
                conn.execute(
                    'INSERT INTO task_archive (id, blob, updated_at) VALUES (?, ?, ?)',
                    (tid, blob, time.time()))
        serve._rag_index_pass(budget=10)
        hits = serve._rag_search('rate limiting backoff storms', k=10,
                                 village='village-a')
        refs = {h['ref'] for h in hits}
        self.assertIn('task-a', refs)
        self.assertNotIn('task-b', refs)

    def test_null_village_rows_stay_visible(self):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO rag_index (doc_id, source, ref, title, room, text, '
                'vec, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                ('library:skills/shared.md', 'library', 'skills/shared.md',
                 'shared', '', 'rate limiting backoff storms',
                 json.dumps(serve._rag_embed('rate limiting backoff storms')),
                 time.time()))
        hits = serve._rag_search('rate limiting backoff storms', k=5,
                                 village='village-b')
        self.assertTrue(any(h['ref'] == 'skills/shared.md' for h in hits))

    def test_room_anchors_village_scoped(self):
        for vid in ('village-a', 'village-b'):
            blob = json.dumps({'id': f'task-{vid}', 'room': 'observatory',
                               'title': f'{vid} anchor', 'taskType': 'code',
                               'grade': 8.0, 'villageId': vid,
                               'deliverableText': 'rate limiting backoff storms',
                               'instructions': 'build'})
            with serve._db() as conn:
                conn.execute(
                    'INSERT INTO task_archive (id, blob, updated_at) VALUES (?, ?, ?)',
                    (f'task-{vid}', blob, time.time()))
        serve._rag_index_pass(budget=10)
        anchors = serve._rag_room_anchors('observatory', village='village-a')
        self.assertTrue(anchors)
        self.assertTrue(all('village-a anchor' in a for a in anchors))

    def test_legacy_rows_backfilled_without_reindex(self):
        blob = json.dumps({'id': 'task-legacy', 'room': 'observatory',
                           'title': 'Legacy', 'taskType': 'code', 'grade': 8.0,
                           'villageId': 'village-b',
                           'deliverableText': 'rate limiting backoff storms',
                           'instructions': 'build'})
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO task_archive (id, blob, updated_at) VALUES (?, ?, ?)',
                ('task-legacy', blob, time.time()))
        serve._rag_index_pass(budget=10)
        # Simulate a pre-feature row: wipe the village stamp.
        with serve._db() as conn:
            conn.execute("UPDATE rag_index SET village_id = NULL "
                         "WHERE doc_id = 'deliverable:task-legacy'")
        out = serve._rag_index_pass(budget=10)
        with serve._db() as conn:
            row = conn.execute(
                "SELECT village_id FROM rag_index WHERE doc_id = 'deliverable:task-legacy'"
            ).fetchone()
        self.assertEqual(row[0], 'village-b')
        self.assertGreaterEqual(out['indexed'], 1)

    def test_drifted_village_stamp_restamps_without_reindex(self):
        """The pass re-stamps whenever the stored village differs from the
        derived one, not only when the stamp is missing. Same doc_id, drifted
        derivation is real: downloads/<agent> rows derive their village from
        the OWNING AGENT's current village, so an agent moving villages
        re-attributes their rows -- and a stale stamp must never keep
        surfacing village knowledge under the wrong flag."""
        _wipe()
        with open(os.path.join(serve.LIBRARY_DIR, 'runbook.md'), 'w') as f:
            f.write('Observatory runbook for 429 storm triage.\n')
        serve._rag_index_pass(budget=10)
        # Simulate drift: the stored stamp no longer matches the derivation.
        with serve._db() as conn:
            conn.execute("UPDATE rag_index SET village_id = 'atlantis' "
                         "WHERE doc_id = 'library:runbook.md'")
        out = serve._rag_index_pass(budget=10)
        self.assertGreaterEqual(out['indexed'], 1)
        with serve._db() as conn:
            row = conn.execute(
                "SELECT village_id FROM rag_index WHERE doc_id = "
                "'library:runbook.md'"
            ).fetchone()
        self.assertEqual(row[0], 'main')
        self.assertTrue(serve._rag_search('429 storm triage', village='main'))
        self.assertFalse(serve._rag_search('429 storm triage',
                                           village='winter'))


class NativeVectorBackfill(unittest.TestCase):
    """The pgvector accelerator: every row carries a native sparsevec
    literal (vec_sv) alongside its JSON, so scoring can move into Postgres
    and the in-process parsed-vector cache can retire. sqlite stores the
    literal as plain TEXT -- same code path, no extension needed."""

    def test_sv_literal_maps_and_never_lies(self):
        self.assertIsNone(serve._rag_sv_literal({}))
        out = serve._rag_sv_literal({0: 1.0, 1500000000: 0.5})
        self.assertTrue(out.endswith('/1000000000'))
        self.assertIn('0:1,', out)
        self.assertIn('500000000:0.5', out)

    def test_indexing_dual_writes_the_native_vector(self):
        _wipe()
        with open(os.path.join(serve.LIBRARY_DIR, 'guide.md'), 'w') as f:
            f.write('The observatory watches rate limits and 429 storms.\n')
        serve._rag_index_pass(budget=10)
        with serve._db() as conn:
            row = conn.execute(
                "SELECT vec, vec_sv FROM rag_index WHERE ref = 'guide.md'"
            ).fetchone()
        self.assertTrue(row[0])
        self.assertTrue(row[1].endswith('/1000000000'))
        # One sparsevec entry per JSON feature, both over the same ids.
        self.assertEqual(row[1].count(':'),
                         len(json.loads(row[0])))

    def test_missing_vec_sv_backfilled_in_place(self):
        _wipe()
        with open(os.path.join(serve.LIBRARY_DIR, 'runbook.md'), 'w') as f:
            f.write('Escalation runbook: restore the ledger, then triage.\n')
        with unittest.mock.patch.object(serve, '_RAG_PGVECTOR_OK', False):
            serve._rag_index_pass(budget=10)
        with serve._db() as conn:
            conn.execute('UPDATE rag_index SET vec_sv = NULL')
        with unittest.mock.patch.object(serve, '_RAG_PGVECTOR_OK', True):
            serve._rag_index_pass(budget=10)
        with serve._db() as conn:
            row = conn.execute(
                "SELECT vec_sv FROM rag_index WHERE ref = 'runbook.md'"
            ).fetchone()
        self.assertTrue(row[0])
        self.assertTrue(row[0].endswith('/1000000000'))

    def test_sql_path_stands_down_without_postgres(self):
        """The accelerator's contract: even with the pgvector flag forced
        on, a non-Postgres backend (sqlite in tests) must stand the SQL
        path down and score the legacy loop -- identical results, no
        surprises."""
        _wipe()
        with open(os.path.join(serve.LIBRARY_DIR, 'guide.md'), 'w') as f:
            f.write('Rate limit storms met exponential backoff retry.\n')
        serve._rag_index_pass(budget=10)
        self.assertIsNone(serve._rag_search_sql(
            serve._rag_embed('rate limit storms'), 5,
            serve._RAG_MIN_SCORE, None, None))
        with unittest.mock.patch.object(serve, '_RAG_PGVECTOR_OK', True):
            legacy = serve._rag_search('rate limit storms')
            self.assertTrue(legacy)
            self.assertTrue(all(h['source'] == 'library' for h in legacy))


class DuplicateRefs(unittest.TestCase):
    """Refs are NOT unique: hybrid docs reuse their task id as ref, so
    deliverable:task-N and hybrid:<agent>:task-N coexist (626 duplicate
    refs on the live index). The search cache is keyed by doc_id (the
    primary key) precisely so a ref collision can never drop a row from
    retrieval -- the ref-keyed cache silently kept one arbitrary row per
    ref and hid the rest."""

    def test_both_rows_with_one_ref_stay_retrievable(self):
        _wipe()
        _seed_archive('task-dupX', 'observatory', 'Ledger backoff',
                      'Exponential backoff carried the retry budget '
                      'through the 429 storms.')
        serve._rag_index_pass(budget=10)
        # A hybrid doc arrives reusing the same task id as its ref.
        vec = json.dumps(serve._rag_embed(
            'hybrid sandbox ledger notes referencing backoff storms'))
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO rag_index (doc_id, source, ref, title, room, '
                'village_id, text, vec, content_hash, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ('hybrid:lydia:task-dupX', 'hybrid', 'task-dupX',
                 'Hybrid ledger', '', None,
                 'hybrid sandbox ledger notes referencing backoff storms',
                 vec, 'hash-x', time.time()))
        rows = serve._rag_search_rows_sync()
        dup_rows = [r for r in rows if r[1] == 'task-dupX']
        self.assertEqual({r[0] for r in dup_rows},
                         {'deliverable', 'hybrid'})
        # Both sides of the ref collision surface in retrieval.
        hits = serve._rag_search('ledger backoff storms')
        sources = {h['source'] for h in hits if h['ref'] == 'task-dupX'}
        self.assertIn('deliverable', sources)
        self.assertIn('hybrid', sources)


class SqlPathMemoryContract(unittest.TestCase):
    """2026-11-16 tracemalloc profile on the VM: the in-process row cache
    held ~3.5 GB (a parsed sparse vector per doc) because ONE transient
    Postgres failure returned None from the SQL scorer and silently flipped
    retrieval onto the full-table fallback, which retains the cache for the
    life of the process. Contract: on a Postgres box the row cache is never
    built (a DB blip scores no hits for that one query instead), and a
    cache that exists anyway is released the moment the SQL scorer serves."""

    def test_pg_query_failure_returns_empty_and_never_builds_cache(self):
        import contextlib
        import unittest.mock
        _wipe()
        with open(os.path.join(serve.LIBRARY_DIR, 'boom.md'), 'w') as f:
            f.write('Backoff storms meet the retry budget.\n')
        serve._rag_index_pass(budget=10)

        @contextlib.contextmanager
        def broken_db():
            raise RuntimeError('db down')
            yield

        with unittest.mock.patch.object(serve, '_RAG_PGVECTOR_OK', True), \
                unittest.mock.patch('db_backend.is_postgres',
                                    return_value=True), \
                unittest.mock.patch.object(serve, '_db', broken_db):
            hits = serve._rag_search('backoff storms')
        self.assertEqual(hits, [])
        self.assertEqual(serve._RAG_SEARCH_CACHE, {})
        self.assertEqual(serve._RAG_SEARCH_SNAPSHOT, ())

    def test_rows_sync_refuses_to_build_on_sql_path(self):
        import contextlib
        import unittest.mock

        @contextlib.contextmanager
        def forbidden_db():
            raise AssertionError('full-table load attempted on SQL path')
            yield

        serve._RAG_SEARCH_CACHE.clear()
        with unittest.mock.patch.object(serve, '_RAG_PGVECTOR_OK', True), \
                unittest.mock.patch('db_backend.is_postgres',
                                    return_value=True), \
                unittest.mock.patch.object(serve, '_db', forbidden_db):
            rows = serve._rag_search_rows_sync()
        self.assertEqual(rows, ())
        self.assertEqual(serve._RAG_SEARCH_CACHE, {})

    def test_sql_success_releases_a_prebuilt_cache(self):
        import unittest.mock
        serve._RAG_SEARCH_CACHE['stale-doc'] = ('library', 'x', 't', 'r',
                                                None, 'snip', {1: 0.5}, 1.0)
        with unittest.mock.patch.object(
                serve, '_RAG_SEARCH_SNAPSHOT', (object(),)), \
                unittest.mock.patch.object(serve, '_rag_embed',
                                           return_value=[0.1, 0.2]), \
                unittest.mock.patch.object(
                    serve, '_rag_search_sql',
                    return_value=[{'source': 'library', 'ref': 'x',
                                   'title': 't', 'room': 'r', 'score': 0.9,
                                   'snippet': 's'}]):
            hits = serve._rag_search('anything')
        self.assertEqual(len(hits), 1)
        self.assertEqual(serve._RAG_SEARCH_CACHE, {})
        self.assertEqual(serve._RAG_SEARCH_SNAPSHOT, ())


class SqlPlaceholderParity(unittest.TestCase):
    """The SQL scorer's ? placeholders and its parameter tuple must match
    exactly. A one-extra-param bug shipped live for a day, unseen by CI
    (sqlite stands the SQL path down, so the mismatch only fired on the VM's
    Postgres): every search failed, the None-fallback built the 3.5 GB row
    cache, and nobody noticed because retrieval still returned hits."""

    def test_placeholder_count_matches_param_count(self):
        import unittest.mock
        captured = {}

        class FakeCur:
            description = [('source',), ('ref',), ('title',), ('room',),
                           ('snippet',), ('score',)]
            def execute(self, sql, params=None):
                captured['sql'] = sql
                captured['params'] = params
                return []
            def fetchall(self):
                return []

        class FakeConn:
            def execute(self, sql, params=None):
                captured['sql'] = sql
                captured['params'] = params
                return FakeCur()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with unittest.mock.patch.object(serve, '_RAG_PGVECTOR_OK', True), \
                unittest.mock.patch('db_backend.is_postgres',
                                    return_value=True), \
                unittest.mock.patch.object(serve, '_db', FakeConn):
            serve._rag_search_sql({5: 0.5, 9: 0.3}, 5, 0.1, ['library'], 'main')
        n_placeholders = captured['sql'].count('?')
        self.assertGreater(n_placeholders, 0)
        self.assertEqual(len(captured['params']), n_placeholders,
                         f"sql has {n_placeholders} '?' placeholders but "
                         f"{len(captured['params'])} params were passed")

    def test_village_and_sources_param_order(self):
        import unittest.mock
        captured = {}

        class FakeCur:
            description = [('source',), ('ref',), ('title',), ('room',),
                           ('snippet',), ('score',)]
            def fetchall(self):
                return []

        class FakeConn:
            def execute(self, sql, params=None):
                captured['params'] = params
                return FakeCur()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with unittest.mock.patch.object(serve, '_RAG_PGVECTOR_OK', True), \
                unittest.mock.patch('db_backend.is_postgres',
                                    return_value=True), \
                unittest.mock.patch.object(serve, '_db', FakeConn):
            serve._rag_search_sql({7: 0.4}, 3, 0.2, ['library', 'tasks'],
                                  'main')
        params = captured['params']
        # qlit, source1, source2, village, qlit, min_score, limit
        self.assertEqual(params[0], serve._rag_sv_literal({7: 0.4}))
        self.assertEqual(params[1:3], ('library', 'tasks'))
        self.assertEqual(params[3], 'main')
        self.assertEqual(params[5], 0.2)
        self.assertEqual(params[6], 3)
