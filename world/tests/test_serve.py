"""Real automated tests for serve.py's pure/testable functions.

Importing serve.py directly is safe for testing -- init_db()/
ensure_sandbox_networking() (which would touch Docker/the real DB) are
gated behind `if __name__ == '__main__':`, not run at import time. The one
side effect on import, _get_or_create_server_access_key(), just reads the
existing key from .env if one's already there (idempotent).

Run: python3 tests/test_serve.py
"""
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse

import asyncio
import openpyxl

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve

# Module-wide safety net (2026-09-25): most classes below already redirect
# DB_PATH themselves in their own setUp (their own temp dir layers on top of
# this one and is torn down first, same as before). But a few classes here
# (ActivitySummary, JevSafetyGate, TempAccessGrants) call serve.log_action /
# serve.init_db directly with no isolation of their own, and were confirmed
# writing real rows (a 'test-agent' browse/escalation trail, 'some-other-agent'
# curl rows) into a live production village.db during a routine test run.
# This is the default so no class in this file can slip through that gap
# again even if a future one forgets to isolate itself.
_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='village-serve-test-module-')
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        VILLAGE_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, 'library', '.passport.json'),
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


class RedactSecrets(unittest.TestCase):
    def test_redacts_openrouter_style_key(self):
        out = serve._redact_secrets('here is my key sk-abcdefghijklmnopqrstuvwxyz123456 keep it safe')
        self.assertNotIn('sk-abcdefghijklmnopqrstuvwxyz123456', out)
        self.assertIn('[REDACTED]', out)

    def test_redacts_api_key_assignment(self):
        out = serve._redact_secrets('config: api_key=verysecretvalue123 more text')
        self.assertNotIn('verysecretvalue123', out)

    def test_redacts_aws_access_key(self):
        out = serve._redact_secrets('id AKIAABCDEFGHIJKLMNOP end')
        self.assertNotIn('AKIAABCDEFGHIJKLMNOP', out)

    def test_leaves_ordinary_text_untouched(self):
        text = 'This is just a normal note about the bank tellers.'
        self.assertEqual(serve._redact_secrets(text), text)


class SafeLibraryPath(unittest.TestCase):
    def test_rejects_path_traversal(self):
        self.assertIsNone(serve._safe_library_path('../../etc/passwd'))

    def test_rejects_absolute_escape(self):
        self.assertIsNone(serve._safe_library_path('../../../../etc/passwd'))

    def test_allows_a_normal_relative_path(self):
        target = serve._safe_library_path('notes/idea.md')
        self.assertIsNotNone(target)
        self.assertTrue(target.startswith(os.path.abspath(serve.LIBRARY_DIR)))


class LibraryTrailReinforcement(unittest.TestCase):
    """Stigmergic trail reinforcement/decay (2026-09-26), ported from real ant
    pheromone-trail biology: search_library used to rank purely by file
    mtime, so a file read/cited 40 times but written 2 weeks ago always lost
    to one written 2 minutes ago and never read at all. Usage lives in its
    own sidecar file (LIBRARY_USAGE_PATH), fully isolated per test here."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='village-library-trail-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lib_dir = os.path.join(self.tmp, 'library')
        os.makedirs(self.lib_dir, exist_ok=True)
        self.usage_path = os.path.join(self.tmp, 'library_usage.json')
        patcher = unittest.mock.patch.multiple(
            serve, LIBRARY_DIR=self.lib_dir, LIBRARY_USAGE_PATH=self.usage_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, rel_path, content, mtime=None):
        full = os.path.join(self.lib_dir, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as f:
            f.write(content)
        if mtime is not None:
            os.utime(full, (mtime, mtime))

    def test_usage_read_returns_empty_dict_when_no_file_exists_yet(self):
        self.assertEqual(serve._library_usage_read(), {})

    def test_record_library_read_persists_count_and_last_read(self):
        serve.record_library_read('notes/idea.md')
        usage = serve._library_usage_read()
        self.assertEqual(usage['notes/idea.md']['count'], 1)
        self.assertGreater(usage['notes/idea.md']['lastRead'], 0)

    def test_record_library_read_accumulates_across_calls(self):
        serve.record_library_read('notes/idea.md')
        serve.record_library_read('notes/idea.md')
        self.assertEqual(serve._library_usage_read()['notes/idea.md']['count'], 2)

    def test_record_library_read_is_a_noop_for_a_falsy_path(self):
        serve.record_library_read('')
        serve.record_library_read(None)
        self.assertEqual(serve._library_usage_read(), {})

    def test_corrupt_usage_file_reads_back_as_empty_not_a_crash(self):
        with open(self.usage_path, 'w') as f:
            f.write('not valid json{{{')
        self.assertEqual(serve._library_usage_read(), {})

    def test_trail_score_zero_for_a_never_read_path(self):
        self.assertEqual(serve._library_trail_score('never/touched.md', {}), 0.0)

    def test_trail_score_full_count_with_zero_elapsed_age(self):
        usage = {'x.md': {'count': 3, 'lastRead': 1000.0}}
        self.assertAlmostEqual(serve._library_trail_score('x.md', usage, now=1000.0), 3.0)

    def test_trail_score_decays_by_half_after_one_half_life(self):
        usage = {'x.md': {'count': 4, 'lastRead': 0.0}}
        score = serve._library_trail_score('x.md', usage, now=serve.LIBRARY_TRAIL_HALF_LIFE_S)
        self.assertAlmostEqual(score, 2.0, places=6)

    def test_search_ranks_a_heavily_reinforced_older_file_above_an_untouched_newer_one(self):
        now = time.time()
        self._write('old-but-validated.md', 'ant colony trail reinforcement', mtime=now - 200_000)
        self._write('new-but-never-read.md', 'ant colony trail reinforcement', mtime=now)
        for _ in range(10):
            serve.record_library_read('old-but-validated.md')
        matches = serve._library_search_matches('trail reinforcement')
        paths = [m['path'] for m in matches]
        self.assertEqual(paths[0], 'old-but-validated.md')

    def test_search_falls_back_to_recency_when_neither_path_was_ever_read(self):
        # Preserves the OLD pure-recency behavior exactly when trail scores
        # tie at zero (the common case for anything never read via a
        # tracked path) -- this change must not bury a fresh, unread file.
        now = time.time()
        self._write('older.md', 'stigmergy', mtime=now - 100)
        self._write('newer.md', 'stigmergy', mtime=now)
        matches = serve._library_search_matches('stigmergy')
        self.assertEqual([m['path'] for m in matches], ['newer.md', 'older.md'])

    def test_search_matches_carry_a_real_file_size(self):
        # Real gap caught live (2026-09-26): the top-ranked match isn't
        # always the most substantial one -- a cheap, real, mechanical
        # signal (file size) lets a caller judge substance directly, rather
        # than trusting rank alone or hoping a model "tries harder."
        content_text = 'stigmergy: ' + ('x' * 200)
        self._write('big.md', content_text)
        matches = serve._library_search_matches('stigmergy')
        self.assertEqual(matches[0]['size'], len(content_text))

    def test_search_and_list_skip_dotfiles(self):
        # Real gap caught while building this: neither walk skipped
        # dotfiles, so .passport.json (LIBRARY_DIR's own reserved ledger)
        # was listed and content-searchable right alongside real knowledge.
        self._write('.passport.json', '{"decision": "handle_minted"}')
        self._write('real-note.md', 'a real note mentioning decision-making')
        matches = serve._library_search_matches('decision')
        self.assertEqual([m['path'] for m in matches], ['real-note.md'])

    def test_read_library_file_endpoint_records_a_trail_read(self):
        self._write('found.md', 'a real finding')
        with unittest.mock.patch.object(serve, 'record_library_read') as record:
            result = asyncio.run(serve.read_library_file('found.md'))
        self.assertEqual(json.loads(result.body)['content'], 'a real finding')
        record.assert_called_once_with('found.md')


class SanitizeDownloadFilename(unittest.TestCase):
    def test_strips_directory_components(self):
        self.assertEqual(serve._sanitize_download_filename('../../etc/passwd'), 'passwd')

    def test_replaces_unsafe_characters(self):
        self.assertEqual(serve._sanitize_download_filename('report (final)!.pdf'), 'report__final__.pdf')

    def test_allows_ordinary_filenames_unchanged(self):
        self.assertEqual(serve._sanitize_download_filename('research-notes_v2.pdf'), 'research-notes_v2.pdf')

    def test_caps_length(self):
        self.assertEqual(len(serve._sanitize_download_filename('a' * 500)), 200)

    def test_a_filename_that_is_all_path_separators_becomes_empty(self):
        # basename() of a path ending in slashes is '' -- the caller
        # (library_download) treats an empty result as a real 400, not a
        # silently-accepted mystery filename.
        self.assertEqual(serve._sanitize_download_filename('???///'), '')


class ParseHttpDateMs(unittest.TestCase):
    # Real, if imperfect, "did this page actually change" signal for
    # date-aware incremental research (2026-09-21) -- Last-Modified is
    # standard HTTP-date format (RFC 7231), the same format email headers
    # use, which is why this reuses email.utils rather than hand-rolling
    # a parser.
    def test_parses_a_real_http_date(self):
        ms = serve._parse_http_date_ms('Wed, 21 Oct 2015 07:28:00 GMT')
        self.assertIsNotNone(ms)
        self.assertEqual(ms, 1445412480000)

    def test_a_later_date_produces_a_larger_timestamp(self):
        earlier = serve._parse_http_date_ms('Wed, 21 Oct 2015 07:28:00 GMT')
        later = serve._parse_http_date_ms('Thu, 22 Oct 2015 07:28:00 GMT')
        self.assertGreater(later, earlier)

    def test_missing_header_fails_closed_to_none(self):
        self.assertIsNone(serve._parse_http_date_ms(None))
        self.assertIsNone(serve._parse_http_date_ms(''))

    def test_garbage_value_fails_closed_to_none_rather_than_throwing(self):
        self.assertIsNone(serve._parse_http_date_ms('not a real date'))


class DownloadDestRelPath(unittest.TestCase):
    def test_personal_scope_lands_in_agents_own_directory(self):
        self.assertEqual(
            serve._download_dest_rel_path('personal', 'theo', 'report.pdf'),
            'pending_review/downloads/theo/report.pdf',
        )

    def test_shared_scope_lands_in_the_common_directory(self):
        self.assertEqual(
            serve._download_dest_rel_path('shared', 'theo', 'report.pdf'),
            'pending_review/shared/report.pdf',
        )

    def test_unrecognized_scope_falls_back_to_personal(self):
        # library_download() itself already normalizes an invalid scope to
        # 'personal' before this is called, but the function defaults safe
        # regardless -- an unrecognized value should never silently land in
        # the shared, all-agent-visible directory.
        self.assertEqual(
            serve._download_dest_rel_path('bogus', 'theo', 'report.pdf'),
            'pending_review/downloads/theo/report.pdf',
        )


class BoundaryMarkers(unittest.TestCase):
    def test_legitimate_content_verifies(self):
        wrapped, nonce, tag, instruction = serve.wrap_external_content('hello world', 'a test page')
        self.assertIn(nonce, wrapped)
        self.assertIn('not instructions', instruction)
        self.assertTrue(serve.verify_boundary_intact('hello world', nonce, tag))

    def test_tampered_content_fails_verification(self):
        _wrapped, nonce, tag, _instruction = serve.wrap_external_content('hello world', 'a test page')
        self.assertFalse(serve.verify_boundary_intact('hello WORLD (tampered)', nonce, tag))

    def test_forged_tag_fails_verification(self):
        self.assertFalse(serve.verify_boundary_intact('hello world', 'deadbeef', '0000000000000000'))

    def test_nonces_differ_per_call(self):
        _w1, nonce1, _t1, _i1 = serve.wrap_external_content('same content')
        _w2, nonce2, _t2, _i2 = serve.wrap_external_content('same content')
        self.assertNotEqual(nonce1, nonce2)


class BandConfiguration(unittest.TestCase):
    # Guards the band config itself, which is easy to half-update: the
    # 'high' band was split into 'coding' (SWE-bench Verified) + 'high'
    # (planning, HLE), and adding a band without its benchmark would
    # KeyError inside refresh_model_tiers -- at which point every tier
    # silently stops updating.
    def test_every_purpose_band_has_a_benchmark(self):
        for band in serve.MODEL_BAND_PURPOSE:
            self.assertIn(band, serve.BAND_BENCHMARK, f'band {band!r} has no benchmark mapping')

    def test_coding_and_planning_are_distinct_benchmarks(self):
        self.assertNotEqual(
            serve.BAND_BENCHMARK['coding'], serve.BAND_BENCHMARK['high'],
            'splitting coding from planning is pointless if both rank on the same benchmark',
        )

    def test_planning_band_has_a_tighter_quality_floor_than_the_default(self):
        # "Willing to spend a lot on the high tier" is expressed as less
        # tolerance for score compromise before price breaks the tie --
        # not as ignoring price entirely.
        self.assertLess(serve.BAND_QUALITY_FLOOR_GAP.get('high'), serve.BENCHMARK_QUALITY_FLOOR_GAP)

    def test_verify_probe_budget_is_large_enough_for_a_reasoning_model(self):
        # Real bug: a 5-token probe made every hidden-reasoning model look
        # broken (it spends the budget thinking, returns content: None).
        self.assertGreaterEqual(serve.MODEL_VERIFY_MAX_TOKENS, 32)
        self.assertGreater(serve.MODEL_VERIFY_ATTEMPTS, 1, 'one transient blip must not permanently disqualify a model')


class BucketModelsByPrice(unittest.TestCase):
    def _model(self, id_, prompt, completion, reasoning=None, modalities=('text',)):
        return {
            'id': id_, 'name': id_,
            'architecture': {'input_modalities': list(modalities), 'output_modalities': list(modalities)},
            'pricing': {'prompt': str(prompt), 'completion': str(completion)},
            'reasoning': reasoning,
        }

    def test_excludes_batch_variants(self):
        buckets = serve._bucket_models_by_price([self._model('vendor/model:batch', 0.000001, 0.000001)])
        self.assertTrue(all(len(v) == 0 for v in buckets.values()))

    def test_excludes_models_with_mandatory_reasoning(self):
        # Can't be turned off -- exactly the real incident (a mid-tier pick
        # that spent its whole token budget on hidden reasoning and
        # returned null content) this exclusion exists to prevent.
        buckets = serve._bucket_models_by_price([self._model('vendor/reasoner', 0.000001, 0.000001, reasoning={'mandatory': True})])
        self.assertTrue(all(len(v) == 0 for v in buckets.values()))

    def test_excludes_models_with_reasoning_enabled_by_default(self):
        # Optional, but on unless a request explicitly turns it off -- this
        # system never sends that override, so it behaves like mandatory
        # reasoning in practice.
        buckets = serve._bucket_models_by_price([self._model('vendor/reasoner-default-on', 0.000001, 0.000001, reasoning={'mandatory': False, 'default_enabled': True})])
        self.assertTrue(all(len(v) == 0 for v in buckets.values()))

    def test_includes_models_with_optional_off_by_default_reasoning(self):
        # Real gap this test guards against: earlier this excluded ANY
        # model carrying a `reasoning` key at all, which silently hid the
        # Claude 4.5/5 family, GPT-5.1, and DeepSeek V3.2 -- all of which
        # ship a `reasoning: {"mandatory": False}` block even though
        # nothing is spent unless a caller explicitly opts in.
        buckets = serve._bucket_models_by_price([self._model('vendor/reasoner-optional', 0.00000005, 0.00000005, reasoning={'mandatory': False})])
        self.assertEqual(len(buckets['low']), 1)
        self.assertEqual(buckets['low'][0]['id'], 'vendor/reasoner-optional')

    def test_excludes_free_tier(self):
        buckets = serve._bucket_models_by_price([self._model('vendor/free', 0, 0)])
        self.assertTrue(all(len(v) == 0 for v in buckets.values()))

    def test_excludes_non_text_modalities(self):
        buckets = serve._bucket_models_by_price([self._model('vendor/image', 0.000001, 0.000001, modalities=('image',))])
        self.assertTrue(all(len(v) == 0 for v in buckets.values()))

    def test_buckets_a_normal_cheap_model_as_low(self):
        # $0.10/M blended -- well inside the 'low' band (0, 0.5).
        buckets = serve._bucket_models_by_price([self._model('vendor/cheap', 0.00000005, 0.00000005)])
        self.assertEqual(len(buckets['low']), 1)
        self.assertEqual(buckets['low'][0]['id'], 'vendor/cheap')


class ModelBenchmarkScores(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        serve.init_db()

    def setUp(self):
        # Real cleanup, not mocked -- these tests write to the real
        # village.db model_benchmark_scores table, same reasoning as the
        # AuthSessions tests above (destroy_session cleans up after
        # itself). Scoped to a test-only model id so a real run never
        # collides with genuine research data.
        with serve._db() as conn:
            conn.execute("DELETE FROM model_benchmark_scores WHERE model_id LIKE 'test-vendor/%'")

    def tearDown(self):
        with serve._db() as conn:
            conn.execute("DELETE FROM model_benchmark_scores WHERE model_id LIKE 'test-vendor/%'")

    def test_set_then_get_round_trips(self):
        serve.set_model_benchmark_score('test-vendor/model-a', 'SWE-bench Verified', 61.8, 'https://example.com/a')
        rows = [r for r in serve.get_model_benchmark_scores() if r['model_id'] == 'test-vendor/model-a']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['score'], 61.8)
        self.assertEqual(rows[0]['source_url'], 'https://example.com/a')

    def test_setting_the_same_model_and_benchmark_again_updates_not_duplicates(self):
        serve.set_model_benchmark_score('test-vendor/model-b', 'SWE-bench Verified', 50.0, 'https://example.com/old')
        serve.set_model_benchmark_score('test-vendor/model-b', 'SWE-bench Verified', 55.0, 'https://example.com/new')
        rows = [r for r in serve.get_model_benchmark_scores() if r['model_id'] == 'test-vendor/model-b']
        self.assertEqual(len(rows), 1, 'a corrected score should overwrite, not add a second row')
        self.assertEqual(rows[0]['score'], 55.0)
        self.assertEqual(rows[0]['source_url'], 'https://example.com/new')

    def test_same_model_different_benchmark_is_a_separate_row(self):
        serve.set_model_benchmark_score('test-vendor/model-c', 'SWE-bench Verified', 70.0, 'https://example.com/swe')
        serve.set_model_benchmark_score('test-vendor/model-c', 'MMLU-Pro', 61.8, 'https://example.com/mmlu-pro')
        rows = [r for r in serve.get_model_benchmark_scores() if r['model_id'] == 'test-vendor/model-c']
        self.assertEqual(len(rows), 2, 'different benchmarks for the same model must not clobber each other')

    def test_migration_preserves_data_from_the_old_table_name(self):
        # Real regression guard for the model_coding_scores ->
        # model_benchmark_scores rename in init_db() -- a village.db
        # created before this rename must not silently lose its already-
        # cited research the first time the renamed server starts.
        with serve._db() as conn:
            conn.execute('DROP TABLE IF EXISTS model_coding_scores')
            conn.execute('''CREATE TABLE model_coding_scores (
                model_id TEXT NOT NULL, benchmark TEXT NOT NULL, score REAL NOT NULL,
                source_url TEXT, checked_at REAL NOT NULL, PRIMARY KEY (model_id, benchmark)
            )''')
            conn.execute(
                "INSERT INTO model_coding_scores VALUES ('test-vendor/legacy', 'SWE-bench Verified', 42.0, 'https://example.com/legacy', 0)"
            )
        serve.init_db()
        rows = [r for r in serve.get_model_benchmark_scores() if r['model_id'] == 'test-vendor/legacy']
        self.assertEqual(len(rows), 1, 'a real score present under the old table name must survive the rename')
        self.assertEqual(rows[0]['score'], 42.0)
        with serve._db() as conn:
            old_table_gone = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_coding_scores'"
            ).fetchone()
        self.assertIsNone(old_table_gone, 'the old table should be dropped once its data is migrated')


class JevTierGate(unittest.TestCase):
    # JEV-gated tier escalation (2026-09-27): everything defaults to LOW; coding
    # is deterministic; mid is lightly gated; high is heavily gated. Fails
    # CLOSED to low on any outage or low-confidence escalation.

    def _slugs(self):
        return [
            unittest.mock.patch.object(serve, '_coding_tier_slug', return_value='coding-model'),
            unittest.mock.patch.object(serve, '_low_tier_slug', return_value='low-model'),
            unittest.mock.patch.object(serve, '_mid_tier_slug', return_value='mid-model'),
            unittest.mock.patch.object(serve, '_high_tier_slug', return_value='high-model'),
        ]

    def _resolve(self, purpose, **kw):
        patchers = self._slugs() + [unittest.mock.patch.object(serve, '_tier_gate_decider', self._decider)]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        return serve._resolve_model_tier(purpose, **kw)

    def _decider(self, instructions, criteria):
        return self._decision, self._confidence

    def test_coding_task_is_deterministic_no_jev(self):
        # A code/review/qa task always uses the coding tier, no JEV consulted.
        calls = []
        def decider(i, c):
            calls.append(1)
            return 'low', 0.9
        patchers = self._slugs() + [unittest.mock.patch.object(serve, '_tier_gate_decider', decider)]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        for tt in ('code', 'review', 'qa'):
            self.assertEqual(serve._resolve_model_tier('do the thing', task_type=tt), 'coding-model')
        self.assertEqual(calls, [], 'coding must not consult JEV')

    def test_defaults_to_low(self):
        self._decision, self._confidence = 'low', 0.9
        self.assertEqual(self._resolve('routine task'), 'low-model')

    def test_mid_lightly_gated(self):
        self._decision, self._confidence = 'mid', 0.8
        self.assertEqual(self._resolve('synthesize findings'), 'mid-model')

    def test_low_confidence_mid_stays_low(self):
        self._decision, self._confidence = 'mid', 0.3
        self.assertEqual(self._resolve('judgment call'), 'low-model')

    def test_high_requires_allow_high(self):
        # JEV says high, but the call didn't opt into high -> stays low.
        self._decision, self._confidence = 'high', 0.9
        self.assertEqual(self._resolve('big task', allow_high=False), 'low-model')

    def test_high_gated_on_allow_high(self):
        self._decision, self._confidence = 'high', 0.9
        self.assertEqual(self._resolve('decompose huge request', allow_high=True), 'high-model')

    def test_low_confidence_high_fails_closed(self):
        self._decision, self._confidence = 'high', 0.4
        self.assertEqual(self._resolve('decompose huge request', allow_high=True), 'low-model')

    def test_outage_fails_closed_to_low(self):
        def boom(i, c):
            raise RuntimeError('jev down')
        patchers = self._slugs() + [unittest.mock.patch.object(serve, '_tier_gate_decider', boom)]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(serve._resolve_model_tier('anything'), 'low-model')

    def test_unknown_decision_fails_closed_to_low(self):
        self._decision, self._confidence = 'maybe', 0.9
        self.assertEqual(self._resolve('anything'), 'low-model')

    def test_high_rejected_when_monthly_high_tier_budget_spent(self):
        # Even with JEV confidently saying high, if the month's high-tier
        # allowance is consumed the gate fails CLOSED to mid -- the expensive
        # tier never overruns its budget.
        self._decision, self._confidence = 'high', 0.9
        with unittest.mock.patch.object(serve, '_high_tier_budget_exceeded', return_value=True):
            self.assertEqual(self._resolve('decompose huge request', allow_high=True), 'mid-model')

    def test_high_allowed_when_budget_fresh(self):
        self._decision, self._confidence = 'high', 0.9
        with unittest.mock.patch.object(serve, '_high_tier_budget_exceeded', return_value=False):
            self.assertEqual(self._resolve('decompose huge request', allow_high=True), 'high-model')


class HighTierPriceCeiling(unittest.TestCase):
    # The daily refresh must never OFFER the high tier a model priced above
    # HIGH_TIER_MAX_PRICE_USD -- price is a hard bound for the expensive tier,
    # not a tiebreak settled after the fact.

    def _model(self, id_, price):
        return {'id': id_, 'name': id_, 'price': price}

    def test_high_tier_price_ceiling_filters_over_priced_models(self):
        # The ceiling applies only to the high band and only when set; an
        # over-ceiling model (even a top scorer) is dropped from the pool.
        models = [
            self._model('vendor/cheap-good', 2.00),
            self._model('vendor/mid-good', 4.50),
            self._model('vendor/expensive-best', 30.00),
        ]
        with unittest.mock.patch.object(serve, 'HIGH_TIER_MAX_PRICE_USD', 5.0):
            filtered = serve._apply_band_price_ceiling('high', models)
        ids = [m['id'] for m in filtered]
        self.assertIn('vendor/cheap-good', ids)
        self.assertIn('vendor/mid-good', ids)
        self.assertNotIn('vendor/expensive-best', ids, 'over-ceiling model must never be offered for high')
        self.assertTrue(all(m['price'] <= 5.0 for m in filtered))

    def test_high_tier_price_ceiling_noop_when_disabled(self):
        models = [self._model('vendor/whatever', 999.0)]
        with unittest.mock.patch.object(serve, 'HIGH_TIER_MAX_PRICE_USD', 0.0):
            filtered = serve._apply_band_price_ceiling('high', models)
        self.assertEqual([m['id'] for m in filtered], ['vendor/whatever'])

    def test_high_tier_price_ceiling_does_not_affect_other_bands(self):
        models = [self._model('vendor/mid', 10.0)]
        with unittest.mock.patch.object(serve, 'HIGH_TIER_MAX_PRICE_USD', 5.0):
            filtered = serve._apply_band_price_ceiling('mid', models)
        self.assertEqual([m['id'] for m in filtered], ['vendor/mid'],
                         'the ceiling is high-tier-only')


class AuthSessions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # init_db() is normally gated behind `if __name__ == '__main__':`
        # (see the module docstring above) -- called explicitly here since
        # these tests need the real `sessions` table to exist. Idempotent
        # (CREATE TABLE IF NOT EXISTS), safe against the real village.db.
        serve.init_db()

    def test_password_hash_is_deterministic_for_the_same_salt(self):
        salt, digest1 = serve._hash_password('correct-horse-battery-staple')
        _salt2, digest2 = serve._hash_password('correct-horse-battery-staple', salt)
        self.assertEqual(digest1, digest2)

    def test_password_hash_differs_for_a_different_password(self):
        salt, digest1 = serve._hash_password('password-one')
        _salt2, digest2 = serve._hash_password('password-two', salt)
        self.assertNotEqual(digest1, digest2)

    def test_session_lifecycle(self):
        session_id = serve.create_session()
        try:
            self.assertTrue(serve.verify_session(session_id))
            serve.destroy_session(session_id)
            self.assertFalse(serve.verify_session(session_id))
        finally:
            serve.destroy_session(session_id)  # no-op if already gone

    def test_unknown_or_missing_session_is_rejected(self):
        self.assertFalse(serve.verify_session('not-a-real-session-id'))
        self.assertFalse(serve.verify_session(None))
        self.assertFalse(serve.verify_session(''))

    def test_expired_session_is_rejected(self):
        # Insert directly with an already-past expiry, bypassing
        # create_session()'s real (future) expiry -- this is the one case
        # that needs a fabricated row rather than the real function, since
        # nothing else can make time move backward.
        import time as _time
        session_id = 'test-expired-' + serve.secrets.token_hex(8)
        with serve._db() as conn:
            conn.execute('INSERT INTO sessions (session_id, created_at, expires_at) VALUES (?, ?, ?)',
                         (session_id, _time.time() - 10000, _time.time() - 1))
        try:
            self.assertFalse(serve.verify_session(session_id))
        finally:
            serve.destroy_session(session_id)


class LoginRateLimit(unittest.TestCase):
    def test_allows_up_to_the_cap_then_blocks(self):
        ip = f'10.0.0.{int(time.time()) % 255}-{time.time()}'  # unique per run
        for _ in range(serve._LOGIN_ATTEMPT_LIMIT):
            self.assertTrue(serve._check_login_rate_limit(ip))
        self.assertFalse(serve._check_login_rate_limit(ip))


class RateLimit(unittest.TestCase):
    def test_allows_up_to_the_cap_then_blocks(self):
        agent = f'test-agent-{time.time()}'  # unique per run, no cross-test pollution
        for _ in range(serve.RATE_LIMIT_MAX_CALLS):
            self.assertTrue(serve.check_rate_limit(agent))
        self.assertFalse(serve.check_rate_limit(agent))

    def test_a_different_identity_has_its_own_budget(self):
        agent_a = f'test-agent-a-{time.time()}'
        agent_b = f'test-agent-b-{time.time()}'
        for _ in range(serve.RATE_LIMIT_MAX_CALLS):
            serve.check_rate_limit(agent_a)
        self.assertFalse(serve.check_rate_limit(agent_a))
        self.assertTrue(serve.check_rate_limit(agent_b))


class CurlRoomGate(unittest.TestCase):
    def test_player_is_always_allowed(self):
        # The player has no `inRoom` entry at all (that field only exists
        # for NPC agents) -- this must not be read as "not in the room."
        # The real UI-level gate (openTerminal()'s room check, index.html)
        # is what actually restricts the player; the server only needs to
        # not ALSO block the one identity it can't see a location for.
        self.assertTrue(serve._agent_is_in_weatherstation('player'))

    def test_agent_actually_in_weatherstation_is_allowed(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value={'agents': {'eli': {'inRoom': 'weatherstation'}}}):
            self.assertTrue(serve._agent_is_in_weatherstation('eli'))

    def test_agent_elsewhere_is_blocked(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value={'agents': {'sam': {'inRoom': 'pressoffice'}}}):
            self.assertFalse(serve._agent_is_in_weatherstation('sam'))

    def test_unknown_agent_is_blocked(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value={'agents': {}}):
            self.assertFalse(serve._agent_is_in_weatherstation('nobody'))

    def test_no_saved_state_fails_closed(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=None):
            self.assertFalse(serve._agent_is_in_weatherstation('eli'))


class SandboxDownloadRoomGate(unittest.TestCase):
    # Mirrors CurlRoomGate exactly, for the sandbox-download endpoint's
    # own room restriction (2026-09-20, extended same-day to cover the
    # Work Room alongside the Observatory -- both are real sandboxed
    # rooms, RESEARCH_SANDBOX_ID and WORKROOM_SANDBOX_ID respectively).
    def test_player_is_always_allowed(self):
        self.assertTrue(serve._agent_is_in_sandbox_room('player'))

    def test_agent_actually_in_observatory_is_allowed(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value={'agents': {'dev': {'inRoom': 'observatory'}}}):
            self.assertTrue(serve._agent_is_in_sandbox_room('dev'))

    def test_agent_actually_in_the_work_room_is_also_allowed(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value={'agents': {'dev': {'inRoom': 'pressoffice'}}}):
            self.assertTrue(serve._agent_is_in_sandbox_room('dev'))

    def test_agent_elsewhere_is_blocked(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value={'agents': {'dev': {'inRoom': 'bank'}}}):
            self.assertFalse(serve._agent_is_in_sandbox_room('dev'))

    def test_no_saved_state_fails_closed(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=None):
            self.assertFalse(serve._agent_is_in_sandbox_room('dev'))


class TempAccessGrants(unittest.TestCase):
    # Real "ask your supervisor" flow -- an agent without standing access
    # (Sam, no Weather Station role) can be granted REAL, TEMPORARY access
    # to a gated capability. These test the grant/check mechanics directly
    # (real village.db round-trips, real cleanup), not the Jev approval
    # judgment itself, which needs a live model call like every other gate.
    @classmethod
    def setUpClass(cls):
        serve.init_db()

    def tearDown(self):
        with serve._db() as conn:
            conn.execute("DELETE FROM temp_access_grants WHERE agent_id LIKE 'test-agent-%'")

    def test_no_grant_means_no_access(self):
        self.assertFalse(serve._has_active_temp_access('test-agent-a', 'curl'))

    def test_a_fresh_grant_is_active(self):
        serve._grant_temp_access('test-agent-b', 'curl', 'faye', 'needs to check a chart API for the project')
        self.assertTrue(serve._has_active_temp_access('test-agent-b', 'curl'))

    def test_an_expired_grant_is_not_active(self):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO temp_access_grants (agent_id, capability, granted_by, reason, granted_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)',
                ('test-agent-c', 'curl', 'faye', 'old reason', time.time() - 2000, time.time() - 1000),
            )
        self.assertFalse(serve._has_active_temp_access('test-agent-c', 'curl'))

    def test_a_grant_for_one_capability_does_not_leak_to_another(self):
        serve._grant_temp_access('test-agent-d', 'curl', 'faye', 'legit reason')
        self.assertFalse(serve._has_active_temp_access('test-agent-d', 'some-other-capability'))

    def test_re_granting_extends_rather_than_duplicates(self):
        serve._grant_temp_access('test-agent-e', 'curl', 'faye', 'first reason')
        serve._grant_temp_access('test-agent-e', 'curl', 'nora', 'second, renewed reason')
        with serve._db() as conn:
            rows = conn.execute("SELECT granted_by FROM temp_access_grants WHERE agent_id = 'test-agent-e' AND capability = 'curl'").fetchall()
        self.assertEqual(len(rows), 1, 'a renewed grant should update the one row, not add a second')
        self.assertEqual(rows[0][0], 'nora')


class SandboxBackups(unittest.TestCase):
    # Real incident this exists to prevent from ever being unrecoverable
    # again: a coding task overwrote a real sandbox file with no way to
    # undo it -- no git repo, no .bak, no OS-level snapshot existed for
    # this project. Rebuilt on local git per your explicit call (weighed
    # against a GitHub remote and against the original full-directory-copy
    # version, which measured at 868KB for 8 snapshots of one real
    # sandbox -- not a problem yet, but linear in sandbox size with no way
    # to store less than a full copy). _restore_sandbox_backup() always
    # resolves the sandbox directory via the real _sandbox_dir_for() (the
    # same thing every other real sandbox endpoint uses), so these tests
    # use a real, uniquely-named test sandbox under the real sandboxes/
    # directory -- same "real path, real cleanup" discipline as the
    # LibraryIngest tests above.
    SANDBOX_ID = 'test-sandbox-backups'

    def setUp(self):
        self.sandbox_dir = serve._sandbox_dir_for(self.SANDBOX_ID)
        with open(os.path.join(self.sandbox_dir, 'index.html'), 'w') as f:
            f.write('<html>real content</html>')

    def tearDown(self):
        shutil.rmtree(self.sandbox_dir, ignore_errors=True)

    def test_snapshotting_an_empty_directory_is_a_no_op(self):
        empty_dir = tempfile.mkdtemp()
        try:
            serve._snapshot_sandbox('test-sandbox-empty', empty_dir)
            self.assertFalse(os.path.isdir(os.path.join(empty_dir, '.git')), 'nothing real existed yet, so there is nothing to version at all')
        finally:
            shutil.rmtree(empty_dir, ignore_errors=True)

    def test_a_real_snapshot_is_listed_and_restorable(self):
        serve._snapshot_sandbox(self.SANDBOX_ID, self.sandbox_dir)
        backups = serve._list_sandbox_backups(self.SANDBOX_ID)
        self.assertEqual(len(backups), 1)

        # Simulate the exact real incident: the file gets destructively overwritten.
        with open(os.path.join(self.sandbox_dir, 'index.html'), 'w') as f:
            f.write('<html>fabricated replacement, the real content is gone</html>')

        serve._restore_sandbox_backup(self.SANDBOX_ID, backups[0])
        with open(os.path.join(self.sandbox_dir, 'index.html')) as f:
            self.assertEqual(f.read(), '<html>real content</html>', 'the real, pre-overwrite content should be back')

    def test_repeated_snapshots_with_no_real_change_do_not_pile_up_empty_commits(self):
        # Real behavior difference from the old copy-based version, worth
        # locking in: git naturally no-ops a commit when nothing changed,
        # so calling this on every single /api/execute (even ones that
        # touch nothing) doesn't inflate history with empty entries.
        serve._snapshot_sandbox(self.SANDBOX_ID, self.sandbox_dir)
        serve._snapshot_sandbox(self.SANDBOX_ID, self.sandbox_dir)
        serve._snapshot_sandbox(self.SANDBOX_ID, self.sandbox_dir)
        self.assertEqual(len(serve._list_sandbox_backups(self.SANDBOX_ID)), 1)

    def test_restoring_is_itself_undoable(self):
        serve._snapshot_sandbox(self.SANDBOX_ID, self.sandbox_dir)
        original_backup = serve._list_sandbox_backups(self.SANDBOX_ID)[0]
        with open(os.path.join(self.sandbox_dir, 'index.html'), 'w') as f:
            f.write('bad overwrite')
        serve._restore_sandbox_backup(self.SANDBOX_ID, original_backup)
        # Restoring should have snapshotted the (bad) pre-restore state,
        # AND the restore itself, as two more real commits.
        backups_after = serve._list_sandbox_backups(self.SANDBOX_ID)
        self.assertEqual(len(backups_after), 3, 'the pre-restore state and the restore itself should both be real, separate commits')

    def test_restoring_an_unknown_stamp_raises_instead_of_silently_doing_nothing(self):
        with self.assertRaises(ValueError):
            serve._restore_sandbox_backup(self.SANDBOX_ID, 'not-a-real-commit-hash')

    def test_history_survives_and_accumulates_across_many_real_changes(self):
        # Real answer to the storage concern this rebuild was FOR: confirm
        # history keeps growing with real content instead of silently
        # capping/dropping old entries the way the copy-based version had
        # to (SANDBOX_BACKUPS_TO_KEEP), since git's per-commit cost is the
        # diff, not a full copy.
        for i in range(15):
            with open(os.path.join(self.sandbox_dir, 'index.html'), 'w') as f:
                f.write(f'<html>version {i}</html>')
            serve._snapshot_sandbox(self.SANDBOX_ID, self.sandbox_dir)
        self.assertEqual(len(serve._list_sandbox_backups(self.SANDBOX_ID)), 15)


class ActivitySummary(unittest.TestCase):
    # Real fix for confabulated retrospectives: an agent asked to reflect
    # on the village with nothing but a role and a vague prompt invented
    # confident-sounding specifics that don't exist in the real code. This
    # grounds that in the same action_log every real action already
    # writes into, so a retrospective prompt can include what an agent
    # actually, verifiably did.
    AGENT_ID = 'test-activity-summary-agent'

    def tearDown(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log WHERE agent_id = ?', (self.AGENT_ID,))

    def test_no_history_is_an_empty_summary(self):
        self.assertEqual(serve._activity_summary_for(self.AGENT_ID), {})

    def test_counts_are_grouped_by_action_type(self):
        serve.log_action(self.AGENT_ID, 'curl', {})
        serve.log_action(self.AGENT_ID, 'curl', {})
        serve.log_action(self.AGENT_ID, 'execute', {})
        counts = serve._activity_summary_for(self.AGENT_ID)
        self.assertEqual(counts.get('curl'), 2)
        self.assertEqual(counts.get('execute'), 1)

    def test_another_agents_actions_are_not_counted(self):
        serve.log_action(self.AGENT_ID, 'curl', {})
        serve.log_action('some-other-agent', 'curl', {})
        serve.log_action('some-other-agent', 'curl', {})
        self.assertEqual(serve._activity_summary_for(self.AGENT_ID).get('curl'), 1)


class CountDueWorkItems(unittest.TestCase):
    # _count_due_work_items is the one bit of compute_health_snapshot
    # that's a pure function safe to test directly (no real village.db
    # round-trip) -- and the one part most worth pinning down, since a
    # unit mismatch (epoch seconds vs. JS's epoch milliseconds) would
    # fail completely silently: it would just always or never count an
    # item as due, with no error anywhere.
    def test_an_item_with_no_notBefore_is_always_due(self):
        self.assertEqual(serve._count_due_work_items([{'title': 'x'}], now_ms=1000), 1)

    def test_an_item_scheduled_for_the_future_is_not_due(self):
        self.assertEqual(serve._count_due_work_items([{'notBefore': 5000}], now_ms=1000), 0)

    def test_an_item_scheduled_for_the_past_is_due(self):
        self.assertEqual(serve._count_due_work_items([{'notBefore': 500}], now_ms=1000), 1)

    def test_an_item_scheduled_for_exactly_now_is_due(self):
        self.assertEqual(serve._count_due_work_items([{'notBefore': 1000}], now_ms=1000), 1)

    def test_counts_only_the_due_ones_in_a_mixed_queue(self):
        queue = [{'notBefore': 500}, {'notBefore': 5000}, {'title': 'no notBefore at all'}]
        self.assertEqual(serve._count_due_work_items(queue, now_ms=1000), 2)

    def test_an_empty_queue_has_zero_due_items(self):
        self.assertEqual(serve._count_due_work_items([], now_ms=1000), 0)


class HealthChecks(unittest.TestCase):
    # Tests the pure alert-decision function only (_health_alerts_for_signals),
    # not compute_health_snapshot itself -- that one hits the real,
    # shared village.db, whose action_log/model_tiers content isn't
    # something a test should assume or reset. Built to close the "no
    # monitoring layer -- everything gets caught reactively" gap.
    def _signals(self, **overrides):
        base = {
            'village_active': True,
            'seconds_since_last_save': 2.0,
            'work_queue_size': 0,
            'work_queue_due_size': 0,
            'agents_count': 5,
            'work_items_abandoned_last_24h': 0,
            'login_failures_last_hour': 0,
            'blocked_or_failed_actions_last_hour': {},
            'tool_volume_by_agent_last_15m': {},
            'missing_model_tier_bands': [],
            'jev_decision_attempts_last_hour': 0,
            'jev_decision_failures_last_hour': 0,
            'self_proposed_rejected_last_24h': 0,
            'task_assigned_last_24h': 0,
            'task_completed_last_24h': 0,
            'task_median_completion_hours': None,
            'task_fast_completion_rate': None,
            'task_horizon_completion_rate': None,
            'ceremony_actions_last_24h': 0,
            'progress_actions_last_24h': 0,
            'ceremony_to_progress_ratio': None,
            'ceremony_signal': 0.0,
            'progress_signal': 0.0,
            'ceremony_imbalance_score': 0.0,
        }
        base.update(overrides)
        return base

    def test_nominal_state_raises_no_alerts(self):
        self.assertEqual(serve._health_alerts_for_signals(self._signals()), [])

    def test_agent_tool_volume_anomaly_raises_behavior_alert(self):
        # An agent firing >threshold spend-inducing tool calls in the window
        # is flagged as a likely runaway loop / tool churn.
        alerts = serve._health_alerts_for_signals(self._signals(
            tool_volume_by_agent_last_15m={'ada': serve.ANOMALY_AGENT_TOOL_THRESHOLD + 5},
        ))
        self.assertTrue(any(a['category'] == 'behavior' for a in alerts))
        self.assertIn('ada', alerts[0]['message'])

    def test_agent_tool_volume_below_threshold_no_alert(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            tool_volume_by_agent_last_15m={'ada': serve.ANOMALY_AGENT_TOOL_THRESHOLD - 1},
        ))
        self.assertFalse(any(a['category'] == 'behavior' for a in alerts))

    def test_a_stale_queue_with_due_items_raises_an_info_alert(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            work_queue_due_size=3, village_active=False, seconds_since_last_save=90.0,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'work_queue')
        self.assertEqual(alerts[0]['severity'], 'info')

    def test_a_nonempty_queue_with_an_active_tab_raises_nothing(self):
        # The queue having items is normal while something's actively
        # draining it -- only "queued AND nothing is running" is a signal.
        alerts = serve._health_alerts_for_signals(self._signals(work_queue_due_size=3, village_active=True))
        self.assertEqual(alerts, [])

    def test_a_stale_queue_of_only_future_scheduled_items_raises_nothing(self):
        # Real gap this closes: a queue can be non-empty (work_queue_size)
        # while nothing in it is actually due yet (work_queue_due_size) --
        # an agent scheduled for later, sitting with no tab open, is
        # working as designed, not stuck.
        alerts = serve._health_alerts_for_signals(self._signals(
            work_queue_size=2, work_queue_due_size=0, village_active=False, seconds_since_last_save=90.0,
        ))
        self.assertEqual(alerts, [])

    def test_abandoned_work_items_raise_a_warning(self):
        alerts = serve._health_alerts_for_signals(self._signals(work_items_abandoned_last_24h=2))
        self.assertEqual(len(alerts), 1)
        self.assertIn('2 work item', alerts[0]['message'])
        self.assertEqual(alerts[0]['severity'], 'warning')

    def test_a_handful_of_login_failures_raises_nothing(self):
        self.assertEqual(serve._health_alerts_for_signals(self._signals(login_failures_last_hour=4)), [])

    def test_five_or_more_login_failures_raises_a_security_warning(self):
        alerts = serve._health_alerts_for_signals(self._signals(login_failures_last_hour=5))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'security')

    def test_a_handful_of_blocked_calls_raises_nothing(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            blocked_or_failed_actions_last_hour={'browse': 9},
        ))
        self.assertEqual(alerts, [])

    def test_ten_or_more_blocked_calls_for_one_action_raises_a_warning(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            blocked_or_failed_actions_last_hour={'browse': 3, 'curl': 10},
        ))
        self.assertEqual(len(alerts), 1)
        self.assertIn('curl', alerts[0]['message'])

    def test_missing_model_tier_bands_are_named_in_the_alert(self):
        alerts = serve._health_alerts_for_signals(self._signals(missing_model_tier_bands=['coding', 'vision']))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'model_tiers')
        self.assertIn('coding', alerts[0]['message'])
        self.assertIn('vision', alerts[0]['message'])

    def test_jev_health_below_min_attempts_raises_nothing(self):
        # Fewer than the minimum attempts can't establish a failure rate --
        # a quiet village with 2 blips out of 3 calls isn't a Jev outage.
        alerts = serve._health_alerts_for_signals(self._signals(
            jev_decision_attempts_last_hour=3, jev_decision_failures_last_hour=3,
        ))
        self.assertEqual(alerts, [])

    def test_jev_health_below_failure_rate_raises_nothing(self):
        # A busy village blips a call or two without being down -- the RATE,
        # not the raw count, is the signal.
        alerts = serve._health_alerts_for_signals(self._signals(
            jev_decision_attempts_last_hour=100, jev_decision_failures_last_hour=30,
        ))
        self.assertEqual(alerts, [])

    def test_jev_health_majority_failures_raise_a_warning(self):
        # >=50% of Jev calls failing over the window = the decisions model is
        # likely down, and the colony is running deterministic fallbacks.
        alerts = serve._health_alerts_for_signals(self._signals(
            jev_decision_attempts_last_hour=40, jev_decision_failures_last_hour=30,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'jev')
        self.assertEqual(alerts[0]['severity'], 'warning')
        self.assertIn('30/40', alerts[0]['message'])

    def test_multiple_real_conditions_at_once_all_surface(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            work_items_abandoned_last_24h=1, login_failures_last_hour=6,
        ))
        self.assertEqual({a['category'] for a in alerts}, {'work_queue', 'security'})

    def test_self_proposed_rejections_below_threshold_raise_nothing(self):
        # A little grooming-out is refinement's normal job -- only a real
        # pattern of rejected self-proposals is worth surfacing.
        alerts = serve._health_alerts_for_signals(self._signals(
            self_proposed_rejected_last_24h=serve.SELF_PROPOSED_REJECT_THRESHOLD - 1,
        ))
        self.assertEqual(alerts, [])

    def test_self_proposed_rejection_pattern_raises_coordination_info(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            self_proposed_rejected_last_24h=serve.SELF_PROPOSED_REJECT_THRESHOLD,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'coordination')
        self.assertEqual(alerts[0]['severity'], 'info')
        self.assertIn('rejected', alerts[0]['message'])

    def test_metr_too_few_assignments_raises_nothing(self):
        # Reliability-at-horizon is only judged with enough assigned tasks --
        # a village with 3 assignments and 0 fast finishes is just quiet.
        alerts = serve._health_alerts_for_signals(self._signals(
            task_assigned_last_24h=serve.METR_MIN_ASSIGNED - 1,
            task_fast_completion_rate=0.0, task_median_completion_hours=12.0,
        ))
        self.assertEqual(alerts, [])

    def test_metr_low_fast_completion_rate_raises_throughput_warning(self):
        # Started-but-not-finished is the METR failure mode: few of the tasks
        # assigned in the window completed within the fast horizon.
        alerts = serve._health_alerts_for_signals(self._signals(
            task_assigned_last_24h=10, task_fast_completion_rate=0.2,
            task_median_completion_hours=8.0, task_horizon_completion_rate=0.4,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'throughput')
        self.assertEqual(alerts[0]['severity'], 'warning')
        self.assertIn('20%', alerts[0]['message'])

    def test_metr_slow_median_with_ok_fast_rate_raises_throughput_info(self):
        # Fast-enough start-to-finish on some, but the median is unusually
        # long: a milder (info) signal -- work is getting done but slowly.
        alerts = serve._health_alerts_for_signals(self._signals(
            task_assigned_last_24h=10, task_fast_completion_rate=0.6,
            task_median_completion_hours=serve.METR_SLOW_MEDIAN_HOURS + 2.0,
            task_horizon_completion_rate=0.9,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'throughput')
        self.assertEqual(alerts[0]['severity'], 'info')
        self.assertIn('median', alerts[0]['message'])

    def test_metr_healthy_rates_raise_nothing(self):
        alerts = serve._health_alerts_for_signals(self._signals(
            task_assigned_last_24h=20, task_fast_completion_rate=0.8,
            task_median_completion_hours=0.5, task_horizon_completion_rate=0.95,
        ))
        self.assertEqual(alerts, [])

    def test_a_little_ceremony_with_no_progress_raises_nothing(self):
        # Below the minimum-volume floor -- a quiet village with a couple of
        # escalations and 0 releases isn't a coordination pathology, it's
        # just quiet. The imbalance score is only consulted once the decayed
        # ceremony SIGNAL clears the floor.
        alerts = serve._health_alerts_for_signals(self._signals(
            ceremony_actions_last_24h=2, ceremony_signal=2.0, progress_actions_last_24h=0,
        ))
        self.assertEqual(alerts, [])

    def test_fresh_ceremony_with_zero_shipped_work_raises_an_info_alert(self):
        # 8 fresh ceremony actions vs 0 shipped: score log10(9) ~= 0.95 --
        # clear of the INFO floor (0.5) but under the WARNING floor (1.0).
        alerts = serve._health_alerts_for_signals(self._signals(
            ceremony_actions_last_24h=8, ceremony_signal=8.0, progress_actions_last_24h=0,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'coordination')
        self.assertEqual(alerts[0]['severity'], 'info')
        self.assertIn('imbalance', alerts[0]['message'])

    def test_the_sep26_restart_storm_shape_raises_a_warning(self):
        # 79 fresh ceremony actions vs 3 shipped (the exact Sep 26 restart
        # storm profile): score log10(80) - log10(4) ~= 1.30 -- WARNING.
        alerts = serve._health_alerts_for_signals(self._signals(
            ceremony_actions_last_24h=79, progress_actions_last_24h=3,
            ceremony_signal=79.0, progress_signal=3.0,
        ))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['category'], 'coordination')
        self.assertEqual(alerts[0]['severity'], 'warning')

    def test_the_same_storm_three_days_stale_reads_quiet(self):
        # The Sep 26 storm aged to 2026-09-29: 3 days = ~6 half-lives, every
        # one of the 79 actions decayed ~64x => signal 1.2, under the 3.0
        # floor. Nothing still in the last 24h's COUNT for the message. The
        # old count-ratio would've screamed forever; the decayed scalar
        # fades with its cause.
        alerts = serve._health_alerts_for_signals(self._signals(
            ceremony_actions_last_24h=0, progress_actions_last_24h=0,
            ceremony_signal=1.2, progress_signal=0.05,
        ))
        self.assertEqual(alerts, [])

    def test_heavy_ceremony_matched_by_real_progress_raises_nothing(self):
        # Lots of review activity is fine when shipped work keeps pace --
        # the imbalance score (log10(13) - log10(11) ~= 0.07), not the raw
        # count, is the signal.
        alerts = serve._health_alerts_for_signals(self._signals(
            ceremony_actions_last_24h=12, progress_actions_last_24h=6,
            ceremony_signal=12.0, progress_signal=10.0,
        ))
        self.assertEqual(alerts, [])


class CoordinationImbalanceScalar(unittest.TestCase):
    """Direct tests of the Reddit-Hot-style decayed signals + imbalance score
    (2026-09-29): the whole point of the scalar vs the old 24h COUNT ratio is
    that a burst of ceremony is loud while FRESH and fades once it's stale --
    the Sep 26 restart storm (79 escalations, 3 shipped, within an hour)
    should have read loud on day 1 and gone quiet a few days later."""

    NOW = 1_800_000_000.0  # pinned epoch for reproducible decay math

    def test_fresh_events_sum_to_their_count(self):
        self.assertEqual(serve._decayed_signal([self.NOW, self.NOW], self.NOW), 2.0)

    def test_half_life_halves_the_weight(self):
        one_half_life = serve._IMBALANCE_HALF_LIFE_S
        fresh = serve._decayed_signal([self.NOW], self.NOW)
        aged = serve._decayed_signal([self.NOW], self.NOW + one_half_life)
        self.assertAlmostEqual(aged, fresh / 2, places=4)

    def test_two_day_old_events_are_about_one_sixteenth_weight(self):
        two_days = 2 * 86400
        fresh = serve._decayed_signal([self.NOW], self.NOW)
        aged = serve._decayed_signal([self.NOW], self.NOW + two_days)
        self.assertAlmostEqual(aged / fresh, 1 / 16, places=3)

    def test_events_beyond_the_horizon_are_excluded(self):
        beyond = serve._IMBALANCE_HORIZON_S + 3600
        self.assertEqual(serve._decayed_signal([self.NOW - beyond], self.NOW), 0.0)

    def test_balanced_activity_scores_zero(self):
        self.assertAlmostEqual(serve._imbalance_score(50.0, 50.0), 0.0, places=9)
        self.assertAlmostEqual(serve._imbalance_score(0.0, 0.0), 0.0, places=9)

    def test_scores_are_log_compressed_not_count_ratios(self):
        # log10(80) - log10(4) ~= 1.30: a 26x count ratio reads as a single
        # digit score, and the same RELATIVE imbalance at lower volume is
        # smaller -- a 26x gap of 2-vs-0.08 events is noise.
        self.assertAlmostEqual(serve._imbalance_score(79.0, 3.0), 1.30, places=2)
        self.assertLess(serve._imbalance_score(2.0, 2.0 / 26), 0.5)

    def test_decay_monotonically_lowers_the_score_toward_quiet(self):
        # Two days of decay drops the storm's score from 1.30 (warning) to
        # 0.70 (below the warning floor); it reaches true silence once the
        # ceremony SIGNAL itself decays under the 3.0 floor (~3 days for the
        # 79-event storm -- asserted via the snapshot test).
        fresh = serve._imbalance_score(79.0, 3.0)
        aged = serve._imbalance_score(79.0 / 16, 3.0 / 16)  # two days stale
        self.assertGreaterEqual(fresh, serve._COORDINATION_WARNING_SCORE)
        self.assertLess(aged, serve._COORDINATION_WARNING_SCORE)
        self.assertLess(aged, fresh)

    def test_snapshot_pairs_timestamps_with_counts(self):
        # End-to-end through compute_health_snapshot: 5 FRESH ceremony rows
        # (today) vs 0 shipped must produce a coordination alert; the same
        # rows aged 3 days must not.
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')
            now = time.time()
            fresh_action = serve._CEREMONY_ACTIONS[0]
            for i in range(5):
                conn.execute(
                    'INSERT INTO action_log (agent_id, action, details, authorized, ts, trace_id) '
                    'VALUES (?, ?, ?, 1, ?, NULL)',
                    ('ben', fresh_action, '{}', now - i * 60),
                )
        snap = serve.compute_health_snapshot()
        self.assertGreaterEqual(snap['ceremony_imbalance_score'], serve._COORDINATION_INFO_SCORE)
        self.assertTrue(any(a['category'] == 'coordination' for a in snap['alerts']))

        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')
            for i in range(5):
                conn.execute(
                    'INSERT INTO action_log (agent_id, action, details, authorized, ts, trace_id) '
                    'VALUES (?, ?, ?, 1, ?, NULL)',
                    ('ben', fresh_action, '{}', now - 3 * 86400 - i * 3600),
                )
        snap = serve.compute_health_snapshot()
        self.assertLess(snap['ceremony_signal'], serve._COORDINATION_MIN_CEREMONY_SIGNAL)
        self.assertFalse(any(a['category'] == 'coordination' for a in snap['alerts']))


class TaskHorizonMetrics(unittest.TestCase):
    # Direct test of _task_horizon_metrics -- the METR reliability-at-horizon
    # pairing -- against the module's redirected DB with explicit timestamps
    # (log_action stamps time.time() itself, so insert rows directly).

    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')

    def _insert(self, action, task_id, ts):
        import json as _json
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO action_log (agent_id, action, details, authorized, ts, trace_id) '
                'VALUES (?, ?, ?, 1, ?, NULL)',
                ('ben', action, _json.dumps({'taskId': task_id}), ts),
            )

    def test_pairs_assigned_to_completed_and_computes_rates_and_median(self):
        now = 1_000_000.0
        # task-a: assigned 5k s ago, completed 4k s later (over the 1h fast
        # horizon, inside the 24h horizon).
        self._insert('task_assigned', 'task-a', now - 5000)
        self._insert('task_completed', 'task-a', now - 1000)
        # task-b: assigned 2k s ago, completed 1k s later (fast).
        self._insert('task_assigned', 'task-b', now - 2000)
        self._insert('task_completed', 'task-b', now - 1000)
        # task-c: assigned but never completed -- must count AGAINST the rates.
        self._insert('task_assigned', 'task-c', now - 500)
        m = serve._task_horizon_metrics(now)
        self.assertEqual(m['assigned'], 3)
        self.assertEqual(m['completed'], 2)
        self.assertAlmostEqual(m['fast_completion_rate'], 1 / 3, places=4)
        self.assertAlmostEqual(m['horizon_completion_rate'], 2 / 3, places=4)
        # Median of [1000, 4000] seconds = 2500s -> 0.69h (rounded to 2 dp).
        self.assertAlmostEqual(m['median_completion_hours'], 0.69, places=2)

    def test_no_completions_leaves_rates_at_zero_or_none(self):
        now = 1_000_000.0
        self._insert('task_assigned', 'task-a', now - 1000)
        m = serve._task_horizon_metrics(now)
        self.assertEqual(m['assigned'], 1)
        self.assertEqual(m['completed'], 0)
        self.assertIsNone(m['median_completion_hours'])
        self.assertEqual(m['fast_completion_rate'], 0.0)
        self.assertEqual(m['horizon_completion_rate'], 0.0)

    def test_no_rows_yields_empty_metrics(self):
        m = serve._task_horizon_metrics(1_000_000.0)
        self.assertEqual(m['assigned'], 0)
        self.assertIsNone(m['fast_completion_rate'])


class HealthAlertPersistence(unittest.TestCase):
    # The dedup/persistence half -- a real village.db round-trip (same
    # convention as TempAccessGrants below), scoped to a distinctive
    # category so cleanup can't touch a real alert this table might
    # already hold.
    CATEGORY = 'test-health-alert'

    @classmethod
    def setUpClass(cls):
        serve.init_db()

    def tearDown(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM health_alerts WHERE category = ?', (self.CATEGORY,))

    def test_a_new_alert_is_persisted(self):
        serve._persist_new_health_alerts([{'category': self.CATEGORY, 'severity': 'warning', 'message': 'm1'}])
        with serve._db() as conn:
            rows = conn.execute('SELECT message FROM health_alerts WHERE category = ?', (self.CATEGORY,)).fetchall()
        self.assertEqual([r[0] for r in rows], ['m1'])

    def test_the_same_standing_alert_is_not_duplicated_within_the_dedup_window(self):
        item = {'category': self.CATEGORY, 'severity': 'warning', 'message': 'm2'}
        serve._persist_new_health_alerts([item])
        serve._persist_new_health_alerts([item])
        with serve._db() as conn:
            rows = conn.execute('SELECT message FROM health_alerts WHERE category = ?', (self.CATEGORY,)).fetchall()
        self.assertEqual(len(rows), 1)

    def test_a_since_resolved_and_now_new_alert_is_persisted_again(self):
        item = {'category': self.CATEGORY, 'severity': 'warning', 'message': 'm3'}
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO health_alerts (category, severity, message, ts) VALUES (?, ?, ?, ?)',
                (self.CATEGORY, 'warning', 'm3', time.time() - serve.HEALTH_ALERT_DEDUP_WINDOW_S - 10),
            )
        serve._persist_new_health_alerts([item])
        with serve._db() as conn:
            rows = conn.execute('SELECT message FROM health_alerts WHERE category = ?', (self.CATEGORY,)).fetchall()
        self.assertEqual(len(rows), 2)

    def test_persist_returns_only_the_newly_persisted_alerts(self):
        # The health loop pushes what this returns, so it must return exactly
        # the alerts it just inserted -- never the standing (deduped) ones.
        fresh = {'category': self.CATEGORY, 'severity': 'warning', 'message': 'fresh'}
        standing = {'category': self.CATEGORY, 'severity': 'warning', 'message': 'standing'}
        serve._persist_new_health_alerts([standing])
        first = serve._persist_new_health_alerts([fresh, standing])
        self.assertEqual([a['message'] for a in first], ['fresh'])
        second = serve._persist_new_health_alerts([fresh, standing])
        self.assertEqual(second, [])


class HealthAlertPush(unittest.TestCase):
    # Outbound half (2026-09-28): newly-persisted warnings are pushed to the
    # player on every configured channel; info stays dashboard-only and an
    # unconfigured channel must fail closed, never raise.

    def test_warning_alert_is_pushed_to_both_channels(self):
        sent = []
        with unittest.mock.patch.object(serve, '_send_player_email_sync',
                                        side_effect=lambda s, b: sent.append(('email', s, b)) or True), \
             unittest.mock.patch.object(serve, 'send_player_telegram_sync',
                                        side_effect=lambda s, b: sent.append(('telegram', s, b)) or True):
            serve._push_new_health_alerts(
                [{'category': 'jev', 'severity': 'warning', 'message': '2/3 Jev calls failed'}])
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][0], 'email')
        self.assertEqual(sent[1][0], 'telegram')
        self.assertIn('jev', sent[0][1])
        self.assertIn('2/3 Jev calls failed', sent[0][2])

    def test_critical_alert_is_pushed(self):
        sent = []
        with unittest.mock.patch.object(serve, '_send_player_email_sync', side_effect=lambda s, b: sent.append(s) or True), \
             unittest.mock.patch.object(serve, 'send_player_telegram_sync', side_effect=lambda s, b: True):
            serve._push_new_health_alerts([{'category': 'security', 'severity': 'critical', 'message': 'intrusion'}])
        self.assertEqual(len(sent), 1)

    def test_info_alert_is_not_pushed(self):
        with unittest.mock.patch.object(serve, '_send_player_email_sync') as email, \
             unittest.mock.patch.object(serve, 'send_player_telegram_sync') as telegram:
            serve._push_new_health_alerts([{'category': 'work_queue', 'severity': 'info', 'message': 'queue stalled'}])
        email.assert_not_called()
        telegram.assert_not_called()

    def test_unconfigured_channels_fail_closed_without_raising(self):
        # No channel configured (both return False, the same as when the
        # bridge/credential is absent) -- must not raise and must log a line.
        with unittest.mock.patch.object(serve, '_send_player_email_sync', return_value=False), \
             unittest.mock.patch.object(serve, 'send_player_telegram_sync', return_value=False):
            serve._push_new_health_alerts(
                [{'category': 'jev', 'severity': 'warning', 'message': 'down'}])  # must not raise


class LibraryIngest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.results = []

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # Anything actually written into the real Library by these tests
        # needs real cleanup too -- same discipline as every other real-
        # file-touching test this project has run.
        for r in self.results:
            if r.get('ok') and r.get('path'):
                target = serve._safe_library_path(r['path'])
                if target and os.path.isfile(target):
                    os.remove(target)

    def test_python_file_ingested_as_plain_text(self):
        src = os.path.join(self.tmpdir, 'script.py')
        with open(src, 'w') as f:
            f.write('def f(): return 1\n')
        serve._ingest_one_file(src, 'test-ingest', self.results)
        self.assertTrue(self.results[0]['ok'])
        target = serve._safe_library_path(self.results[0]['path'])
        with open(target) as f:
            self.assertIn('def f()', f.read())

    def test_excel_file_extracted_to_readable_text(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['name', 'score'])
        ws.append(['Ada', 95])
        src = os.path.join(self.tmpdir, 'data.xlsx')
        wb.save(src)
        serve._ingest_one_file(src, 'test-ingest', self.results)
        self.assertTrue(self.results[0]['ok'])
        target = serve._safe_library_path(self.results[0]['path'])
        with open(target) as f:
            content = f.read()
        self.assertIn('Ada', content)
        self.assertIn('95', content)

    def test_image_copied_byte_identical(self):
        src = os.path.join(self.tmpdir, 'icon.png')
        raw = b'\x89PNG\r\n\x1a\nnot a real png but real bytes'
        with open(src, 'wb') as f:
            f.write(raw)
        serve._ingest_one_file(src, 'test-ingest', self.results)
        self.assertTrue(self.results[0]['ok'])
        target = serve._safe_library_path(self.results[0]['path'])
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), raw)

    def test_unsupported_extension_is_skipped_not_silently_copied(self):
        src = os.path.join(self.tmpdir, 'weird.xyz123')
        with open(src, 'w') as f:
            f.write('mystery content')
        serve._ingest_one_file(src, 'test-ingest', self.results)
        self.assertFalse(self.results[0]['ok'])
        self.assertIn('unsupported', self.results[0]['note'])

    def test_oversized_file_is_skipped(self):
        src = os.path.join(self.tmpdir, 'big.py')
        with open(src, 'w') as f:
            f.write('x')
        original_cap = serve.INGEST_MAX_FILE_BYTES
        serve.INGEST_MAX_FILE_BYTES = 0  # force the cap to trigger without writing a real 15MB fixture
        try:
            serve._ingest_one_file(src, 'test-ingest', self.results)
        finally:
            serve.INGEST_MAX_FILE_BYTES = original_cap
        self.assertFalse(self.results[0]['ok'])
        self.assertIn('exceeds', self.results[0]['note'])

    def test_directory_walk_mirrors_structure_and_skips_junk_dirs(self):
        os.makedirs(os.path.join(self.tmpdir, 'sub', 'node_modules'))
        with open(os.path.join(self.tmpdir, 'sub', 'a.py'), 'w') as f:
            f.write('a = 1')
        with open(os.path.join(self.tmpdir, 'sub', 'node_modules', 'skip_me.py'), 'w') as f:
            f.write('should not be ingested')
        serve._ingest_walk(self.tmpdir, 'test-ingest-dir', self.results)
        paths = [r['path'] for r in self.results if r['ok']]
        self.assertTrue(any(p.endswith('sub/a.py') for p in paths))
        self.assertFalse(any('node_modules' in p for p in paths))


class ServerOwnedSeed(unittest.TestCase):
    # Server-owned roster (2026-09-21): no static agent names live in any JS
    # file; serve.py seeds the default roster into an empty database on first
    # boot and stamps the director tier before any client reads it. These test
    # that against a THROWAWAY database (fresh temp file), never the real
    # village.db.
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='village-seed-test-')
        # Redirect all state/disk side effects into the temp sandbox.
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            VILLAGE_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        # Seed identities are config-driven (SEED_ROSTER / SEED_DIRECTOR_MAP /
        # SEED_ADMIN_IDS / SEED_SENIOR_DIRECTOR in .env, 2026-09-27), so this
        # hermetic test injects them rather than relying on a real .env.
        self._env_patch = unittest.mock.patch.object(
            serve, '_load_env', return_value={
                'SEED_ROSTER': ('ada|Ada|#e06666|Research|small,'
                                'ben|Ben|#6fa8dc|Banking|small,'
                                'cora|Cora|#93c47d|Post Office|small,'
                                'dev|Dev|#ffd966|Studio|small,'
                                'eli|Eli|#c27ba0|Weather Station|small,'
                                'theo|Theo|#b48ce0|Control Room|mid,'
                                'nora|Nora|#e69138|Personnel|mid'),
                'SEED_DIRECTOR_MAP': 'ada:theo,dev:theo,eli:theo,ben:nora,cora:nora',
                'SEED_ADMIN_IDS': 'theo',
                'SEED_SENIOR_DIRECTOR': 'nora',
            })
        self._env_patch.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        self._env_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_roster_is_typed_and_free_of_authority_fields(self):
        roster = serve._default_roster_definitions()
        self.assertEqual(len(roster), 7)
        names = {d['name'] for d in roster}
        self.assertEqual(names, {'Ada', 'Ben', 'Cora', 'Dev', 'Eli', 'Theo', 'Nora'})
        for d in roster:
            self.assertTrue(d['id'])
            self.assertTrue(d['name'])
            self.assertNotIn('isAdmin', d, 'authority graph must not be a seed literal -- stamped from the DB')
            self.assertNotIn('isDirector', d)
            self.assertNotIn('director', d)

    def test_seed_populates_an_empty_db(self):
        refreshed = serve.get_state_from_db()
        self.assertIsNone(refreshed, 'fresh temp DB starts empty')
        self.assertTrue(serve._seed_default_roster())
        state = serve.get_state_from_db()
        self.assertIsNotNone(state)
        self.assertEqual(len(state['agentRoster']), 7)
        self.assertEqual(len(state['agents']), 7)
        # Director tier stamped server-side, not carried in the seed literal.
        theo = next(d for d in state['agentRoster'] if d['id'] == 'theo')
        nora = next(d for d in state['agentRoster'] if d['id'] == 'nora')
        self.assertTrue(theo.get('isAdmin'))
        self.assertTrue(theo.get('isDirector'))
        self.assertFalse(theo.get('director'))
        self.assertTrue(nora.get('isDirector'))
        self.assertFalse(nora.get('isAdmin'))
        self.assertFalse(nora.get('director'), 'senior-most director has no own director')

    def test_seed_is_idempotent(self):
        self.assertTrue(serve._seed_default_roster())
        self.assertFalse(serve._seed_default_roster(), 'must not reseed an already-populated DB')
        self.assertEqual(len(serve.get_state_from_db()['agentRoster']), 7)

    def test_seed_and_backfill_together_stamp_full_chain(self):
        # After seeding an empty DB, the walk-the-chain pointers backfill so
        # the base roster is authoritative and connected from the first read.
        # (nadia/maya/priya are NOT part of the base seed -- they exist only
        # once real hires add them; the backfill stamps whomever IS present.)
        serve._seed_default_roster()
        state = serve.get_state_from_db()
        by_id = {d['id']: d for d in state['agentRoster']}
        self.assertEqual(by_id['dev']['director'], 'theo')
        self.assertEqual(by_id['eli']['director'], 'theo')
        self.assertEqual(by_id['ben']['director'], 'nora')
        self.assertFalse(by_id['nora'].get('director'))       # senior-most director
        self.assertFalse(by_id['theo'].get('director'))       # admin at top
        self.assertTrue(by_id['theo']['isAdmin'])
        self.assertTrue(by_id['nora']['isDirector'])          # stamped, not literal


class WalkTheChainDirectorAuth(unittest.TestCase):
    # Derive a small realistic roster and assert the walk-the-chain derivations
    # (faye admin, nora senior director, dev/sam mid-directors with direct
    # reports), using pure functions off an in-memory state dict.
    def _state(self):
        return {
            'agentRoster': [
                {'id': 'faye', 'isAdmin': True, 'isDirector': True},
                {'id': 'nora', 'isDirector': True},
                {'id': 'dev', 'director': 'faye'},
                {'id': 'sam', 'director': 'faye'},
                {'id': 'maya', 'director': 'sam'},
                {'id': 'nadia', 'director': 'dev'},
                {'id': 'zara', 'director': 'nora'},
                {'id': 'tom', 'director': 'nora'},
            ],
        }

    def test_admin_and_senior_director_detection(self):
        s = self._state()
        self.assertTrue(serve._is_admin(s, 'faye'))
        self.assertFalse(serve._is_admin(s, 'nora'))
        self.assertEqual(serve._senior_most_director_id(s), 'nora')

    def test_direct_reports_and_chain(self):
        s = self._state()
        self.assertEqual(serve._direct_reports(s, 'dev'), ['nadia'])
        self.assertEqual(serve._direct_reports(s, 'sam'), ['maya'])
        self.assertEqual(
            serve._director_chain(s, 'maya'),
            ['maya', 'sam', 'faye'],
        )
        self.assertEqual(
            serve._director_chain(s, 'nadia'),
            ['nadia', 'dev', 'faye'],
        )

    def test_can_write_self_director_above_admin(self):
        s = self._state()
        self.assertTrue(serve._can_write_agent(s, 'nadia', 'nadia'))          # self, always
        self.assertTrue(serve._can_write_agent(s, 'dev', 'nadia'))            # director writing subordinate
        self.assertTrue(serve._can_write_agent(s, 'faye', 'nadia'))           # admin to anyone
        self.assertTrue(serve._can_write_agent(s, 'faye', 'zara'))            # admin to another chain
        self.assertTrue(serve._can_write_agent(s, 'sam', 'maya'))             # mid-director to own report
        # Write authority flows DOWN the chain: a subordinate cannot write the
        # director's own files, nor a peer's across teams.
        self.assertFalse(serve._can_write_agent(s, 'nadia', 'dev'))           # subordinate -> director denied
        self.assertFalse(serve._can_write_agent(s, 'maya', 'nadia'))          # cross-chain subordinate denied

    def test_can_write_denies_cross_team_and_player(self):
        s = self._state()
        self.assertFalse(serve._can_write_agent(s, 'nadia', 'zara'))          # unrelated
        self.assertFalse(serve._can_write_agent(s, 'zara', 'maya'))           # another chain
        self.assertFalse(serve._can_write_agent(s, 'player', 'nadia'))        # player is never an agent


class DirectorOwnedTemplates(unittest.TestCase):
    # Directors + admin may author the role-template library; rank-and-file may
    # not. Director-ness is structural (has direct reports, or is admin), not
    # reliant on the under-populated isDirector boolean -- matching the
    # walk-the-chain model. Edits retroactively re-stamp live holders.
    def _state(self, agents=None, templates=None):
        roster = [
            {'id': 'faye', 'isAdmin': True, 'isDirector': True},
            {'id': 'nora', 'isDirector': True},
            {'id': 'dev', 'director': 'faye'},
            {'id': 'sam', 'director': 'faye'},
            {'id': 'maya', 'director': 'sam'},   # sam has a direct report -> director
            {'id': 'nadia', 'director': 'dev'},  # dev has a direct report -> director
            {'id': 'zara', 'director': 'nora'},
        ]
        agents = agents or {d['id']: {'profile': {}, 'role': 'Research'} for d in roster}
        state = {'agentRoster': roster, 'agents': agents}
        if templates is not None:
            state['templates'] = templates
        return state

    def test_director_is_structural_not_flag_dependent(self):
        # dev and sam carry NO isDirector flag (the backfill only stamps top
        # tier) but each has a direct report -- the walk-the-chain definition
        # still makes them directors for template authorship.
        s = self._state()
        self.assertTrue(serve._is_director(s, 'dev'))
        self.assertTrue(serve._is_director(s, 'sam'))
        self.assertTrue(serve._is_director_or_admin(s, 'dev'))
        self.assertTrue(serve._is_director_or_admin(s, 'faye'))
        self.assertTrue(serve._is_director_or_admin(s, 'nora'))

    def test_rank_and_file_is_not_a_director(self):
        s = self._state()
        self.assertFalse(serve._is_director(s, 'maya'), 'maya has no direct reports')
        self.assertFalse(serve._is_director_or_admin(s, 'nadia'))
        self.assertFalse(serve._is_director_or_admin(s, 'zara'))

    def test_profile_for_role_prefers_db_then_seed_then_default(self):
        # DB template wins.
        s = self._state(templates={'Research': {'mission': 'DB mission', 'instructions': ['db rule'], 'notes': []}})
        p = serve._profile_for_role(s, 'Research')
        self.assertEqual(p['mission'], 'DB mission')
        self.assertEqual(p['instructions'], ['db rule'])
        # No DB entry -> seed constant is the fallback.
        s2 = self._state(templates={})
        p2 = serve._profile_for_role(s2, 'Research')
        self.assertEqual(p2['mission'], serve._SEED_PROFILES['Research']['mission'])
        # Unknown role with no DB entry and no seed -> generic fallback.
        p3 = serve._profile_for_role(s2, 'Fishing Guide')
        self.assertEqual(p3['mission'], serve._DEFAULT_PROFILE['mission'])
        # Handed out by value, never by reference to the shared constant.
        p4 = serve._profile_for_role(s2, 'Research')
        p4['mission'] = 'MUTATED'
        self.assertNotEqual(p4['mission'], serve._SEED_PROFILES['Research']['mission'])
        # Calling _profile_for_role must not backfill/populate the library.
        self.assertEqual(s2['templates'], {})

    def test_init_templates_backfills_seed_once_only(self):
        s = self._state(templates=None)
        self.assertTrue(serve._init_templates_in_db(s))
        self.assertEqual(set(s['templates']), set(serve._SEED_PROFILES))
        # Second run is a no-op and never clobbers a director's edits.
        s['templates']['Research']['mission'] = 'edited by nora'
        self.assertFalse(serve._init_templates_in_db(s))
        self.assertEqual(s['templates']['Research']['mission'], 'edited by nora')

    def test_init_templates_runs_on_an_existing_db_state(self):
        # A live village.db has no 'templates' key until the one-time startup
        # migration adds it -- must backfill from seed without a seed event.
        s = self._state(templates=None)
        self.assertNotIn('templates', s)  # simulates a pre-village DB without the key
        self.assertTrue(serve._init_templates_in_db(s))
        self.assertEqual(set(s['templates']), set(serve._SEED_PROFILES))
        # And it leaves unrelated state untouched.
        self.assertEqual(len(s['agentRoster']), 7)
        self.assertEqual(len(s['agents']), 7)

    def test_init_templates_never_overwrites_existing_library(self):
        # Migration must not clobber a library that was already written.
        s = self._state(templates={'Research': {'mission': 'director-authored', 'instructions': [], 'notes': []}})
        self.assertFalse(serve._init_templates_in_db(s))
        self.assertEqual(s['templates']['Research']['mission'], 'director-authored')

    def test_autosave_merge_carries_server_owned_fields_forward(self):
        # ROOT CAUSE of the live bug: agents.js saveState POSTs a fixed field
        # list every 5s, and post_state used to save_state_to_db(data) wholesale
        # -- so the server-owned templates (and any future server-only key) were
        # dropped on the very next autosave. _merge_server_owned must carry any
        # existing key the incoming payload omits.
        existing = {
            'agents': {'a': {'role': 'Research'}},
            'agentRoster': [{'id': 'a'}],
            'templates': {'Research': {'mission': 'keep me', 'instructions': [], 'notes': []}},
            'modelTiersDirty': True,
        }
        incoming = {'agents': {'a': {'role': 'Research'}}, 'agentRoster': [{'id': 'a'}]}  # saveState payload
        merged = serve._merge_server_owned(existing, incoming)
        self.assertEqual(merged['agents'], existing['agents'], 'client-owned game fields replaced')
        self.assertEqual(merged['templates']['Research']['mission'], 'keep me', 'server-owned templates survive')
        self.assertIs(merged['modelTiersDirty'], True, 'any omitted key is carried forward')
        self.assertEqual(set(merged), set(existing), 'merge is a superset of existing')

    def test_autosave_merge_client_changes_still_win(self):
        existing = {'templates': {}, 'agents': {'a': {'role': 'Research'}}}
        incoming = {'agents': {'a': {'role': 'Banking'}}, 'templates': {'X': {'mission': 'm', 'instructions': [], 'notes': []}}}
        merged = serve._merge_server_owned(existing, incoming)
        self.assertEqual(merged['agents']['a']['role'], 'Banking', 'client wins on client-owned fields')
        self.assertEqual(merged['templates']['X']['mission'], 'm', 'client-provided templates replace')

    def test_autosave_merge_server_cadence_stamp_wins_over_stale_client(self):
        # The 2026-09-23 sentinel return: a stale /api/state POST (an old tab
        # with pre-fix clients.js) carries lastSkillReviewAt: 1e18 in its copy
        # of the blob. _merge_server_owned previously accepted the client's copy
        # (top-level keys the client SENDS win), so the server's corrected
        # now_ms was reverted to the sentinel every 5s, permanently re-disabling
        # the standing skill-review sweep. Server cadence stamps must win.
        existing = {'sim': {'owner': 'server'},
                    'lastSkillReviewAt': 1_725_000_000_000}
        stale_incoming = {'sim': {'owner': 'server'},
                          'lastSkillReviewAt': 1_000_000_000_000_000_000}
        merged = serve._merge_server_owned(existing, stale_incoming)
        self.assertEqual(merged['lastSkillReviewAt'], existing['lastSkillReviewAt'],
                         'server cadence stamp wins over a stale client copy')
        # And a client that omits it entirely must not drop the server's value.
        omitting = {'sim': {'owner': 'server'}}
        merged2 = serve._merge_server_owned(existing, omitting)
        self.assertEqual(merged2['lastSkillReviewAt'], existing['lastSkillReviewAt'],
                         'omitted cadence stamp is carried forward')

    def test_server_ownership_merge_preserves_server_positions(self):
        # Phase-2 flip defense: once sim.owner == 'server', the client's 5s
        # autosave (a renderer's saveState) must NOT clobber positions the
        # server just advanced. Non-spatial client fields still land; spatial
        # server-owned fields are carried forward verbatim.
        existing = {
            'sim': {'owner': 'server', 'tick': 12},
            'agents': {'ada': {'x': 100, 'y': 50, 'dir': 'east', 'path': [{'x': 200, 'y': 50}],
                               'busy': True, 'inRoom': 'weatherstation'}},
        }
        # Client autosave reports ada at a stale position and busy=False.
        incoming = {'agents': {'ada': {'x': 10, 'y': 10, 'dir': 'north', 'path': None,
                                       'busy': False, 'inRoom': None}}}
        merged = serve._merge_server_owned(existing, incoming)
        a = merged['agents']['ada']
        self.assertEqual(a['x'], 100, 'server x is authoritative under the flip')
        self.assertEqual(a['y'], 50, 'server y is authoritative under the flip')
        self.assertEqual(a['dir'], 'east', 'server dir is authoritative')
        self.assertEqual(a['path'], [{'x': 200, 'y': 50}], 'server path is authoritative')
        # Client non-spatial fields still land (the renderer's own state edits).
        self.assertFalse(a['busy'], 'client-owned non-spatial fields still win')
        self.assertIsNone(a['inRoom'])

    def test_client_ownership_merge_still_allows_client_positions(self):
        # Before the flip (owner != 'server'), the client is still the mover and
        # its positions must win -- only the server-ownership branch protects them.
        existing = {'sim': {'owner': 'client'}, 'agents': {'ada': {'x': 100, 'y': 50}}}
        incoming = {'agents': {'ada': {'x': 10, 'y': 10}}}
        merged = serve._merge_server_owned(existing, incoming)
        self.assertEqual(merged['agents']['ada']['x'], 10, 'client owns movement before the flip')
        self.assertEqual(merged['agents']['ada']['y'], 10)

    def test_server_ownership_merge_preserves_task_lifecycle_state(self):
        # Phase 3: under server ownership the server is the authoritative writer
        # of workQueue + the durable task mirror. A client autosave (which also
        # sends workQueue) must not clobber the server's mid-cycle requeues/
        # assignments with a stale copy.
        existing = {
            'sim': {'owner': 'server', 'tick': 12},
            'agents': {'ada': {'x': 100, 'y': 50, 'busy': True, 'task': 'task-1'}},
            'workQueue': [{'title': 'Server queued', 'room': 'pressoffice'}],
            'tasks': {'task-1': {'id': 'task-1', 'status': 'walking', 'assignedTo': 'ada'}},
        }
        # Client autosave: stale/empty queue + no tasks key at all.
        incoming = {'agents': {'ada': {'x': 10, 'y': 10, 'busy': True, 'task': 'task-1'}},
                    'workQueue': []}
        merged = serve._merge_server_owned(existing, incoming)
        self.assertEqual(merged['workQueue'], [{'title': 'Server queued', 'room': 'pressoffice'}],
                         'server queue wins under the flip')
        self.assertEqual(merged['tasks'], {'task-1': {'id': 'task-1', 'status': 'walking',
                                                      'assignedTo': 'ada'}},
                         'server task mirror is carried into the merged state')
        # Position still protected.
        self.assertEqual(merged['agents']['ada']['x'], 100)

    def test_apply_template_restamps_every_live_holder(self):
        template = {'mission': 'New mission', 'instructions': ['new rule'], 'notes': []}
        agents = {
            'ada': {'role': 'Research', 'profile': {'mission': 'old', 'instructions': [], 'notes': []}},
            'ben': {'role': 'Research', 'profile': {'mission': 'old', 'instructions': [], 'notes': []}},
            'cora': {'role': 'Banking', 'profile': {'mission': 'keep', 'instructions': [], 'notes': []}},
        }
        s = self._state(agents=agents, templates={'Research': template})
        # Patch side-effecting writes so the pure re-stamp logic can be tested.
        with unittest.mock.patch.object(serve, 'save_state_to_db'), \
             unittest.mock.patch.object(serve, 'log_action'):
            applied = serve._apply_template_to_role(s, 'Research', template, 'nora')
        self.assertEqual(set(applied), {'ada', 'ben'})
        self.assertEqual(s['agents']['ada']['profile']['mission'], 'New mission')
        self.assertEqual(s['agents']['ben']['profile']['instructions'], ['new rule'])
        # Unrelated role untouched, notes copied fresh by value.
        self.assertEqual(s['agents']['cora']['profile']['mission'], 'keep')
        self.assertEqual(s['agents']['ada']['profile']['notes'], [])

    def test_apply_template_noop_when_role_has_no_holders(self):
        template = {'mission': 'm', 'instructions': [], 'notes': []}
        s = self._state(agents={'ada': {'role': 'Research', 'profile': {}}}, templates={'Orchard': template})
        with unittest.mock.patch.object(serve, 'save_state_to_db') as save, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            applied = serve._apply_template_to_role(s, 'Orchard', template, 'nora')
        self.assertEqual(applied, [])
        save.assert_not_called()
        log.assert_not_called()


class RoomDefinitions(unittest.TestCase):
    # Phase 4/5: the planner-facing description of each room lives in DB
    # state (director-editable), not a hardcoded copy -- so a director's
    # purpose edit reaches BOTH the server intent planner and the browser
    # delegate path. Labels are locked; only purposes change.
    def _state(self):
        return {'agentRoster': [
            {'id': 'faye', 'isAdmin': True},
            {'id': 'nora', 'isDirector': True},
            {'id': 'zara', 'director': 'nora'},
        ], 'agents': {}}

    def test_room_definitions_backfills_all_seed_rooms_on_a_legacy_db(self):
        # A live village.db predates roomDefinitions -- the lazy backfill must
        # add every seed room (delegable work rooms PLUS the hangout, which is
        # enterable-but-not-delegable) without a seed event.
        s = {}
        defs = serve._room_definitions(s)
        self.assertEqual(set(defs), set(serve._DEFAULT_ROOM_DEFINITIONS))
        self.assertEqual(defs['pressoffice']['label'], 'Work Room')
        self.assertTrue(defs['pressoffice']['purpose'])
        self.assertFalse(serve._DEFAULT_ROOM_DEFINITIONS == ({}), 'seed defaults exist')

    def test_room_definitions_never_overwrites_a_director_purpose(self):
        # Backfill fills only missing rooms; an existing, director-edited
        # purpose must survive a re-read.
        s = {'roomDefinitions': {'pressoffice': {'label': 'Work Room', 'purpose': 'director rewrote this'}}}
        defs = serve._room_definitions(s)
        self.assertEqual(defs['pressoffice']['purpose'], 'director rewrote this')
        # And the rooms the director didn't touch still get defaults.
        self.assertEqual(defs['observatory']['purpose'], serve._DEFAULT_ROOM_DEFINITIONS['observatory']['purpose'])

    def test_room_definitions_fills_a_missing_label_from_seed(self):
        # Labels can't be edited, so a label hole (e.g. from a hand-edited DB)
        # is repaired from the seed; the purpose is left as-is.
        s = {'roomDefinitions': {'media': {'purpose': 'kept'}}}
        defs = serve._room_definitions(s)
        self.assertEqual(defs['media']['label'], 'Studio')
        self.assertEqual(defs['media']['purpose'], 'kept')

    def test_merge_server_owned_keeps_server_roster_over_client_copy(self):
        # ROOT CAUSE defense for Phase 4: governance (auto-hire/fire) mutates
        # the canonical roster server-side. The client's 5s autosave sends ITS
        # stale AGENT_ROSTER copy, which would revert a server hire/fire.
        existing = {
            'sim': {'owner': 'server'},
            'agentRoster': [{'id': 'faye', 'isAdmin': True}, {'id': 'NEW_HIRE', 'name': 'maya'}],
            'agents': {'faye': {}, 'maya': {}},
        }
        incoming = {'agentRoster': [{'id': 'faye', 'isAdmin': True}, {'id': 'fired_dev'}], 'agents': {'faye': {}}}
        merged = serve._merge_server_owned(existing, incoming)
        self.assertEqual([d['id'] for d in merged['agentRoster']], ['faye', 'NEW_HIRE'],
                         'server roster (with governance hire) wins over the stale client copy')


class AgentFileVisibility(unittest.TestCase):
    # Self-reports rule (2026-09-21): an agent must NOT be able to view or
    # modify its OWN reports/ directory, but can write into others'. Also: no
    # app source visible, dotfiles/hidden filtered, traversal contained.
    def test_visible_plain_path(self):
        self.assertTrue(serve._agent_rel_path_is_visible(['notes', 'todo.md']))

    def test_hidden_dotfile_and_skipped_dirs_hidden(self):
        self.assertFalse(serve._agent_rel_path_is_visible(['.git']))
        self.assertFalse(serve._agent_rel_path_is_visible(['.DS_Store']))
        self.assertFalse(serve._agent_rel_path_is_visible(['conversations']))
        self.assertFalse(serve._agent_rel_path_is_visible(['.env']))

    def test_is_under_reports_detects_reports_root(self):
        self.assertTrue(serve.is_under_reports('reports/report-1.md'))
        self.assertTrue(serve.is_under_reports('reports/sub/x.md'))
        self.assertFalse(serve.is_under_reports('agent.json'))
        self.assertFalse(serve.is_under_reports('prototypes/x.py'))

    def test_owns_reports_dir_only_applies_to_self(self):
        self.assertTrue(serve._owns_reports_dir('nadia', 'nadia'))
        self.assertFalse(serve._owns_reports_dir('nadia', 'dev'))   # someone else is fine
        self.assertFalse(serve._owns_reports_dir('nadia', 'player'))
        self.assertFalse(serve._owns_reports_dir('nadia', None))
        self.assertFalse(serve._owns_reports_dir('nadia', 'unknown'))

    def test_path_components_under_root(self):
        self.assertEqual(serve.path_components('/a/b', '/a/b'), [])
        self.assertEqual(serve.path_components('/a/b/reports', '/a/b'), ['reports'])
        self.assertEqual(serve.path_components('/a/b/reports/x', '/a/b'), ['reports', 'x'])


class AppSourceIsolation(unittest.TestCase):
    # "They should not be able to view the source code for the application
    # itself": library reads are confined to LIBRARY_DIR, agent files to
    # AGENTS_DIR (both outside world/), and the sandbox mounts only the
    # sandbox dir. Verify the confinement helpers reject a path that would
    # escape to serve.py / village.db / world2 JS.
    def setUp(self):
        self.prev_db = serve.DB_PATH
        self.prev_agents = serve.AGENTS_DIR
        self.tmp = tempfile.mkdtemp(prefix='village-appsrc-test-')
        serve.AGENTS_DIR = os.path.join(self.tmp, 'agents')

    def tearDown(self):
        serve.DB_PATH = self.prev_db
        serve.AGENTS_DIR = self.prev_agents
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_library_path_rejects_escapes_to_app_files(self):
        # A library-relative path must resolve strictly inside LIBRARY_DIR.
        self.assertIsNone(serve._safe_library_path('../../../world/serve.py'))
        self.assertIsNone(serve._safe_library_path('../../.env'))
        self.assertIsNone(serve._safe_library_path('..'))
        self.assertIsNone(serve._safe_library_path('../../village.db'))

    def test_agent_file_read_denies_traversal_to_server_source(self):
        # list/read containment is enforced via path normalization in the
        # handlers, but the visibility helper here confirms the valid-shaped
        # inputs that name app-source are not under any whitelisted agent path.
        self.assertFalse(serve._agent_rel_path_is_visible(['..', 'serve.py']))
        self.assertFalse(serve._agent_rel_path_is_visible(['..', '..', 'world2', 'index.html']))

    def test_agent_folders_empty_until_state_saved(self):
        # Nothing under AGENTS_DIR is served by any public route -- the only
        # way agent files exist is the server's own materialization, which is
        # itself confined to AGENTS_DIR (outside world/ where serve.py lives).
        self.assertFalse(os.path.commonpath([serve.AGENTS_DIR, os.path.dirname(serve.__file__)])
                         == serve.AGENTS_DIR)


class ReportCrossWrite(unittest.TestCase):
    # Agent-filed reports land in ANOTHER agent's reports/ directory; an agent
    # cannot file about itself (keeps its own off-limits reports/ dir free of
    # self-referential writes). Pure-logic verification of the gating helpers
    # plus the materialization path via a temp DB.
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='village-report-test-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            VILLAGE_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()
        serve.save_state_to_db({
            'agentRoster': [{'id': 'ada'}, {'id': 'nadia'}, {'id': 'dev'}],
            'agents': {'ada': {}, 'nadia': {}, 'dev': {}},
            'reports': [], 'nextReportId': 1,
        })

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post_report(self, about_id, from_id, quote='q', note='n', key=None):
        import json as _j
        from unittest.mock import MagicMock
        req = MagicMock()
        req.headers = {'X-Agent-Key': key} if key else {}
        req.json = unittest.mock.AsyncMock(return_value={
            'aboutId': about_id, 'fromId': from_id, 'quote': quote, 'note': note,
        })
        # Post the body through a plain wrapper -- but post_report is async;
        # run it via asyncio.
        return req

    def test_materialization_puts_report_in_target_reports_dir(self):
        # A report about 'nadia' by 'dev' is materialized into
        # agents/nadia/reports/ -- the target's own (hidden-from-her) dir.
        state = serve.get_state_from_db()
        report = {
            'id': 'report-1-1234-nadia', 'aboutId': 'nadia', 'fromId': 'dev',
            'quote': 'q', 'note': 'n', 'ts': 1, 'severity': 'minor',
        }
        state['reports'] = [report]
        serve.save_state_to_db(state)
        target_md = os.path.join(serve.AGENTS_DIR, 'nadia', 'reports', 'report-1-1234-nadia.md')
        self.assertTrue(os.path.isfile(target_md), 'report must land in the SUBJECT\'s reports dir')
        with open(target_md) as f:
            self.assertIn('filed by: dev', f.read().lower())


class PassportDecisionChain(unittest.TestCase):
    # Hash-chain covers CHAINED KEY DECISIONS (2026-09-21): consequential
    # actions (hire, fire, promote, grant, report_filed) append decision blocks
    # off the SAME head as promoted-file blocks, so the whole ledger is one
    # tamper-evident chain.
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='village-passport-test-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            VILLAGE_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()
        os.makedirs(os.path.join(self.tmp, 'library'), exist_ok=True)

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_decision_blocks_chain_off_the_same_head(self):
        # First block: from an empty chain, prev links to the empty head (None).
        b1 = serve._append_passport_decision('grant_access', 'nora', {'agent': 'nadia', 'capability': 'curl'})
        head_after_b1 = serve._load_passport()['head']
        self.assertIsNotNone(b1)
        self.assertIsNone(b1['prev'], 'first decision block links to the empty head')

        # Each subsequent block chains off the head left by the LAST append --
        # so altering or deleting any earlier block breaks every later link.
        b2 = serve._append_passport_decision('hire', 'faye', {'hired': 'zara'})
        self.assertEqual(b2['prev'], head_after_b1, 'block 2 must chain off block 1')
        head_after_b2 = serve._load_passport()['head']
        b3 = serve._append_passport_decision('report_filed', 'dev', {'about': 'nadia'})
        self.assertEqual(b3['prev'], head_after_b2, 'block 3 must chain off block 2')

        passport = serve._load_passport()
        self.assertEqual(passport['count'], 3)
        self.assertEqual(len(passport['blocks']), 3)
        # The head advances monotonically as decisions append, never stalling.
        self.assertNotEqual(head_after_b2, head_after_b1)
        self.assertIsNotNone(passport['head'])

    def test_no_decisions_reads_fresh_chain(self):
        p = serve._load_passport()
        self.assertEqual(p['count'], 0)
        self.assertEqual(p['head'], None)
        self.assertEqual(p['blocks'], [])

    def test_hashed_actions_subset_excludes_routine(self):
        self.assertIn('hire', serve._HASHED_ACTIONS)
        self.assertIn('firing_review', serve._HASHED_ACTIONS)
        self.assertIn('grant_access', serve._HASHED_ACTIONS)
        # File writes/modifies chain too (your 2026-09-21 call on hashing
        # every file write) -- both the write and its promotion.
        self.assertIn('library_write', serve._HASHED_ACTIONS)
        self.assertIn('library_promote', serve._HASHED_ACTIONS)
        self.assertIn('report_filed', serve._HASHED_ACTIONS)
        # Routine heartbeat actions must NOT be chain-worthy.
        for a in ('chat', 'browse', 'execute', 'task_completed', 'mail_sent'):
            self.assertNotIn(a, serve._HASHED_ACTIONS)

    def test_library_write_chains_when_an_agent_writes_a_file(self):
        # Regression for the added rule: an agent writing/modifying a library
        # file lands a `library_write` block on the passport chain.
        import asyncio
        from unittest.mock import MagicMock, AsyncMock
        # Give dev a real key in this isolated DB.
        dev_key = serve.get_or_create_agent_key('dev')
        os.makedirs(os.path.join(serve.LIBRARY_DIR, 'shared'), exist_ok=True)
        body = {
            'agentId': 'dev', 'path': 'shared/field-notes.md',
            'content': 'inspected the bank vault door handle', 'source': 'firsthand',
        }
        req = MagicMock(headers={'X-Agent-Key': dev_key})
        req.json = AsyncMock(return_value=body)
        resp = asyncio.run(serve.write_library_file(req))
        self.assertEqual(resp.body.decode(), 'saved')
        blocks = [b for b in serve._load_passport()['blocks'] if b.get('kind') == 'library_write']
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]['actor'], 'dev')
        self.assertEqual(blocks[0]['payload']['path'], 'shared/field-notes.md')
        # The written file exists on disk under the shared commons.
        self.assertTrue(os.path.isfile(os.path.join(serve.LIBRARY_DIR, 'shared', 'field-notes.md')))

    def test_library_write_chains_on_modify(self):
        # Modifying an existing file (second write to the same path) chains
        # another block -- each write is a distinct, hashed event.
        import asyncio
        from unittest.mock import MagicMock, AsyncMock
        dev_key = serve.get_or_create_agent_key('dev')
        os.makedirs(os.path.join(serve.LIBRARY_DIR, 'shared'), exist_ok=True)
        for content in ('draft v1', 'draft v2 -- revised'):
            req = MagicMock(headers={'X-Agent-Key': dev_key})
            req.json = AsyncMock(return_value={
                'agentId': 'dev', 'path': 'shared/field-notes.md', 'content': content, 'source': 'firsthand',
            })
            asyncio.run(serve.write_library_file(req))
        writes = [b for b in serve._load_passport()['blocks'] if b.get('kind') == 'library_write']
        self.assertEqual(len(writes), 2, 'each write/modify must be its own chained block')


class JevChoiceExtraction(unittest.TestCase):
    # Jev returns typed decisions with calibrated confidence + cost (see
    # DESIGN.md and the Jev research). Formerly every call site read only
    # `choice`, throwing away the confidence that is Jev's whole reason to
    # exist. _jev_choice extracts all three with defaults that preserve old
    # behavior when a field is absent.
    def test_extracts_choice_confidence_and_cost(self):
        data = {
            'answers': {'choice': {'choice': 'allow', 'confidence': 0.92, 'probabilities': {'allow': 0.92, 'block': 0.08}}},
            'usage': {'cost': 0.000014},
        }
        self.assertEqual(serve._jev_choice(data), ('allow', 0.92, 0.000014))

    def test_missing_confidence_defaults_to_1_0(self):
        # No/absent confidence -> behave as before (never block a field that
        # was always present). This is the "doesn't change behavior by itself"
        # guarantee.
        data = {'answers': {'choice': {'choice': 'allow'}}, 'usage': {}}
        self.assertEqual(serve._jev_choice(data), ('allow', 1.0, 0.0))

    def test_bad_confidence_defaults_to_1_0(self):
        # Out-of-range or non-numeric confidence is treated as absent.
        data = {'answers': {'choice': {'choice': 'deny', 'confidence': 'high'}}}
        self.assertEqual(serve._jev_choice(data), ('deny', 1.0, 0.0))

    def test_empty_response_is_safe(self):
        self.assertEqual(serve._jev_choice(None), (None, 1.0, 0.0))
        self.assertEqual(serve._jev_choice({}), (None, 1.0, 0.0))

    def test_answer_uses_first_question_key(self):
        data = {'answers': {'pick': {'choice': 'worker_3', 'confidence': 0.7}}}
        self.assertEqual(serve._jev_choice(data), ('worker_3', 0.7, 0.0))


class JevSafetyGate(unittest.TestCase):
    # The "act when confident, escalate when unsure" contract Jev exists to
    # provide. A confident allow passes; a LOW-confidence allow is escalated
    # to a human instead of acted on (never flat-allowed); any block denies.
    @classmethod
    def setUpClass(cls):
        serve.init_db()

    def _call(self, decision, confidence):
        # Patch the escalation side effects (email + file I/O) so a test
        # never blasts mail or mutates the real escalations.json.
        with unittest.mock.patch.object(serve, '_send_escalation_email_sync'), \
             unittest.mock.patch.object(serve, '_load_escalations', return_value={}) as mock_load, \
             unittest.mock.patch.object(serve, '_save_escalations') as mock_save:
            allowed = serve._jev_safety_gate(
                'test-agent', 'browse', 'This page', 'http://example.com/page',
                'need to check weather', decision, confidence, 0.0, None,
            )
        return allowed, mock_save

    def test_confident_allow_passes(self):
        allowed, _ = self._call('allow', 0.95)
        self.assertTrue(allowed)

    def test_confident_approve_passes(self):
        allowed, _ = self._call('approve', 0.9)
        self.assertTrue(allowed)

    def test_low_confidence_allow_escalates_and_denies(self):
        allowed, save_called = self._call('allow', 0.4)
        self.assertFalse(allowed, 'a low-confidence allow must NOT be flat-allowed')
        save_called.assert_called()

    def test_low_confidence_approve_escalates_and_denies(self):
        allowed, save_called = self._call('approve', 0.3)
        self.assertFalse(allowed)
        save_called.assert_called()

    def test_block_denies_without_escalation(self):
        # A firm block is just a block -- no need to escalate an already-clear no.
        allowed, save_called = self._call('block', 0.99)
        self.assertFalse(allowed)
        save_called.assert_not_called()

    def test_no_answer_fails_closed(self):
        allowed, save_called = self._call(None, 1.0)
        self.assertFalse(allowed)
        save_called.assert_not_called()

    def test_confident_allow_records_cost_and_confidence_in_log(self):
        with unittest.mock.patch.object(serve, 'log_action') as mock_log, \
             unittest.mock.patch.object(serve, '_send_escalation_email_sync'):
            self.assertTrue(serve._jev_safety_gate(
                'test-agent', 'download', 'This file', 'http://example.com/f', 'purpose', 'allow', 0.88, 0.00001, 'auth-ok',
            ))
        details = mock_log.call_args[0][2]
        self.assertEqual(details['confidence'], 0.88)
        self.assertEqual(details['cost'], 0.00001)


class DecideRateLimiter(unittest.TestCase):
    # Sliding-window gate on /api/decide -- the defense against the ~48k
    # unattributed Jev-call leak. Exercises _decide_allowed directly with an
    # injectable clock so bursts/throttling are deterministic, no real
    # monotonic time involved.
    def setUp(self):
        serve._decide_stamps.clear()

    # Simulate a "real time" timeline in seconds; each _t is when the next
    # call arrives. Bucket key for an agent: calls attributed to that agent
    # throttle independently.
    def test_burst_allows_up_to_cap_then_blocks(self):
        # Space calls so (cap + 1) land inside one burst window -- the last
        # one MUST be throttled. Spacing is derived from the constants so the
        # invariant holds regardless of their exact values, and any faster
        # (>= min-interval) spacing only makes the case stricter.
        spacing = serve.DECIDE_BURST_WINDOW_S / (serve.DECIDE_BURST_ALLOW + 1)
        for i in range(serve.DECIDE_BURST_ALLOW):
            allowed, reason = serve._decide_allowed('ada', now=100.0 + i * spacing)
            self.assertTrue(allowed, f'call {i} within burst should be allowed: {reason}')
        allowed, reason = serve._decide_allowed('ada', now=100.0 + serve.DECIDE_BURST_ALLOW * spacing)
        self.assertFalse(allowed, 'burst-exhausted call must be throttled')

    def test_sustained_spam_is_stopped(self):
        # Several bursts worth of calls all arriving faster than the window
        # drains: exactly one burst's worth may get through, the rest refused.
        spacing = serve.DECIDE_BURST_WINDOW_S / (serve.DECIDE_BURST_ALLOW + 1)
        hits = refuses = 0
        for i in range(3 * serve.DECIDE_BURST_ALLOW):
            ok, _ = serve._decide_allowed('ada', now=200.0 + i * spacing)
            if ok:
                hits += 1
            else:
                refuses += 1
        self.assertEqual(hits, serve.DECIDE_BURST_ALLOW, 'at most one burst of allowed calls')
        self.assertGreater(refuses, 0, 'sustained spam must get throttled')

    def test_agents_throttle_independently(self):
        # One agent exhausting its burst must not starve another.
        spacing = serve.DECIDE_BURST_WINDOW_S / (serve.DECIDE_BURST_ALLOW + 1)
        for i in range(serve.DECIDE_BURST_ALLOW):
            serve._decide_allowed('ada', now=300.0 + i * spacing)
        allowed, _ = serve._decide_allowed('nora', now=300.0)
        self.assertTrue(allowed, "a different agent's first call must not be throttled")

    def test_anonymous_calls_share_a_system_bucket(self):
        # Unknown/missing actors collapse into one "__system__" bucket so an
        # unbounded set of anonymous identities can't dodge the throttle.
        spacing = serve.DECIDE_BURST_WINDOW_S / (serve.DECIDE_BURST_ALLOW + 1)
        for i in range(serve.DECIDE_BURST_ALLOW):
            self.assertTrue(serve._decide_allowed(None, now=400.0 + i * spacing)[0])
        self.assertTrue(serve._decide_allowed('player', now=400.0 + serve.DECIDE_BURST_ALLOW * spacing)[1].startswith('decide rate limited'))
        # same system bucket is exhausted for 'unknown' too
        allowed, _ = serve._decide_allowed('unknown', now=400.0 + serve.DECIDE_BURST_ALLOW * spacing)
        self.assertFalse(allowed)

    def test_window_drains_and_allows_again(self):
        # After the burst window passes with no calls, a fresh burst is
        # allowed -- the throttle is a sliding window, not a hard lifetime cap.
        spacing = serve.DECIDE_BURST_WINDOW_S / (serve.DECIDE_BURST_ALLOW + 1)
        for i in range(serve.DECIDE_BURST_ALLOW):
            serve._decide_allowed('ada', now=500.0 + i * spacing)
        allowed, reason = serve._decide_allowed('ada', now=500.0 + serve.DECIDE_BURST_WINDOW_S + 1.0)
        self.assertTrue(allowed, f'window should drain and re-allow, got: {reason}')


class GoogleOAuth(unittest.TestCase):
    """_google_access_token / _google_call: real OAuth mechanics confirmed
    live (2026-09-26) against a real refresh token minted through a real
    installed-app consent flow (accounts.google.com -> oauth2.googleapis.com
    token exchange), before anything was built on top of it."""

    def _resp(self, body):
        class R:
            def read(self):
                return body
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
        return R()

    def setUp(self):
        serve._GOOGLE_ACCESS_TOKEN_CACHE['at'] = 0.0
        serve._GOOGLE_ACCESS_TOKEN_CACHE['token'] = None
        self.addCleanup(lambda: serve._GOOGLE_ACCESS_TOKEN_CACHE.update({'at': 0.0, 'token': None}))

    def test_no_credential_fails_closed(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value=None):
            token, error = serve._google_access_token()
        self.assertIsNone(token)
        self.assertIn('not configured', error)

    def test_missing_refresh_token_fails_closed_with_a_clear_reason(self):
        creds = json.dumps({'client_id': 'cid', 'client_secret': 'sec', 'refresh_token': None})
        with unittest.mock.patch.object(serve, '_open_secret', return_value=creds):
            token, error = serve._google_access_token()
        self.assertIsNone(token)
        self.assertIn('OAuth consent step was never completed', error)

    def test_real_refresh_request_shape(self):
        creds = json.dumps({'client_id': 'cid', 'client_secret': 'sec', 'refresh_token': 'rt-1'})
        captured = {}

        def fake_urlopen(req, timeout=20):
            captured['url'] = req.full_url
            captured['body'] = urllib.parse.parse_qs(req.data.decode())
            return self._resp(b'{"access_token": "at-1", "expires_in": 3599}')

        with unittest.mock.patch.object(serve, '_open_secret', return_value=creds), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=fake_urlopen):
            token, error = serve._google_access_token()
        self.assertIsNone(error)
        self.assertEqual(token, 'at-1')
        self.assertEqual(captured['url'], 'https://oauth2.googleapis.com/token')
        self.assertEqual(captured['body']['refresh_token'], ['rt-1'])
        self.assertEqual(captured['body']['grant_type'], ['refresh_token'])

    def test_cached_token_avoids_a_second_network_call(self):
        creds = json.dumps({'client_id': 'cid', 'client_secret': 'sec', 'refresh_token': 'rt-1'})
        with unittest.mock.patch.object(serve, '_open_secret', return_value=creds), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        return_value=self._resp(b'{"access_token": "at-1"}')) as net:
            serve._google_access_token()
            serve._google_access_token()
        net.assert_called_once()

    def test_google_call_retries_once_on_401_with_a_forced_refresh(self):
        creds = json.dumps({'client_id': 'cid', 'client_secret': 'sec', 'refresh_token': 'rt-1'})
        calls = {'n': 0}

        def fake_urlopen(req, timeout=30):
            if 'oauth2.googleapis.com' in req.full_url:
                return self._resp(b'{"access_token": "at-fresh"}')
            calls['n'] += 1
            if calls['n'] == 1:
                raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'expired'))
            return self._resp(b'{"items": []}')

        with unittest.mock.patch.object(serve, '_open_secret', return_value=creds), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=fake_urlopen):
            data, error = serve._google_call('GET', 'https://www.googleapis.com/calendar/v3/calendars/primary/events')
        self.assertIsNone(error)
        self.assertEqual(data, {'items': []})
        self.assertEqual(calls['n'], 2)  # the real endpoint was hit twice: 401, then success

    def test_google_call_surfaces_a_non_401_error_without_retrying(self):
        creds = json.dumps({'client_id': 'cid', 'client_secret': 'sec', 'refresh_token': 'rt-1'})

        def fake_urlopen(req, timeout=30):
            if 'oauth2.googleapis.com' in req.full_url:
                return self._resp(b'{"access_token": "at-1"}')
            raise urllib.error.HTTPError(req.full_url, 403, 'Forbidden', {}, io.BytesIO(b'quota exceeded'))

        with unittest.mock.patch.object(serve, '_open_secret', return_value=creds), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=fake_urlopen):
            data, error = serve._google_call('GET', 'https://www.googleapis.com/calendar/v3/calendars/primary/events')
        self.assertIsNone(data)
        self.assertIn('403', error)
        self.assertIn('quota exceeded', error)


class PixellabCall(unittest.TestCase):
    """_pixellab_call / _pixellab_poll_job: real API mechanics, matching the
    village's own already-tested spike script (scripts/pixellab_spike.py)
    -- not guessed from the public OpenAPI spec, per the same "verify,
    don't assume" discipline as every other real integration tonight."""

    def _resp(self, body):
        class R:
            def read(self):
                return body
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
        return R()

    def test_no_credential_fails_closed_without_a_network_call(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value=None), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen') as net:
            data, error = serve._pixellab_call('POST', '/create-character-with-4-directions', {})
        net.assert_not_called()
        self.assertIsNone(data)
        self.assertIn('not configured', error)

    def test_real_call_shape_matches_the_tested_spike_script(self):
        captured = {}

        def fake_urlopen(req, timeout=30):
            captured['url'] = req.full_url
            captured['method'] = req.get_method()
            captured['headers'] = {k.lower(): v for k, v in req.headers.items()}
            captured['body'] = json.loads(req.data)
            return self._resp(b'{"character_id": "c1", "background_job_id": "j1"}')

        with unittest.mock.patch.object(serve, '_open_secret', return_value='fake-pixellab-key'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=fake_urlopen):
            data, error = serve._pixellab_call('POST', '/create-character-with-4-directions', {
                'description': 'a knight', 'image_size': {'width': 48, 'height': 48},
                'view': 'high top-down', 'template_id': 'mannequin'})
        self.assertIsNone(error)
        self.assertEqual(data, {'character_id': 'c1', 'background_job_id': 'j1'})
        self.assertEqual(captured['url'], 'https://api.pixellab.ai/v2/create-character-with-4-directions')
        self.assertEqual(captured['method'], 'POST')
        self.assertEqual(captured['headers'].get('authorization'), 'Bearer fake-pixellab-key')
        self.assertEqual(captured['body']['description'], 'a knight')

    def test_http_error_is_surfaced(self):
        def raise_http_error(req, timeout=30):
            raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'bad key'))
        with unittest.mock.patch.object(serve, '_open_secret', return_value='fake-key'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=raise_http_error):
            data, error = serve._pixellab_call('GET', '/balance')
        self.assertIsNone(data)
        self.assertIn('401', error)
        self.assertIn('bad key', error)

    def test_poll_job_returns_on_completed(self):
        with unittest.mock.patch.object(serve, '_pixellab_call', return_value=({'status': 'completed'}, None)):
            data, error = serve._pixellab_poll_job('job-1', timeout=10, interval=0)
        self.assertIsNone(error)
        self.assertEqual(data['status'], 'completed')

    def test_poll_job_surfaces_a_failed_status(self):
        with unittest.mock.patch.object(serve, '_pixellab_call', return_value=({'status': 'failed'}, None)):
            data, error = serve._pixellab_poll_job('job-1', timeout=10, interval=0)
        self.assertIsNone(data)
        self.assertIn('job failed', error)

    def test_poll_job_times_out_on_a_never_completing_job(self):
        with unittest.mock.patch.object(serve, '_pixellab_call', return_value=({'status': 'processing'}, None)):
            data, error = serve._pixellab_poll_job('job-1', timeout=0.05, interval=0.01)
        self.assertIsNone(data)
        self.assertIn('timed out', error)

    def test_poll_job_surfaces_a_transient_call_error_immediately(self):
        with unittest.mock.patch.object(serve, '_pixellab_call', return_value=(None, 'PixelLab call failed: network blip')):
            data, error = serve._pixellab_poll_job('job-1', timeout=10, interval=0)
        self.assertIsNone(data)
        self.assertIn('network blip', error)


class TregCall(unittest.TestCase):
    """_treg_call: real API mechanics confirmed live against Treg's own docs
    (POST https://treg.to/call/{endpoint_id}, header X-Treg-Token) -- built
    per your explicit request for real X-trending/LinkedIn-search tools,
    2026-09-26."""

    def _resp(self, body):
        class R:
            def read(self):
                return body
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
        return R()

    def test_no_credential_fails_closed_without_a_network_call(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value=None), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen') as net:
            data, error = serve._treg_call('x.x.get-trends-by-woeid', {'woeid': 1})
        net.assert_not_called()
        self.assertIsNone(data)
        self.assertIn('not configured', error)

    def test_default_post_call_shape_matches_treg_docs(self):
        captured = {}

        def fake_urlopen(req, timeout=30):
            captured['url'] = req.full_url
            captured['method'] = req.get_method()
            captured['headers'] = {k.lower(): v for k, v in req.headers.items()}
            captured['body'] = req.data
            return self._resp(b'{"posts": []}')

        with unittest.mock.patch.object(serve, '_open_secret', return_value='fake-treg-token'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=fake_urlopen):
            data, error = serve._treg_call('moz.web.url.metrics', {'targets': ['moz.com']})
        self.assertIsNone(error)
        self.assertEqual(data, {'posts': []})
        self.assertEqual(captured['url'], 'https://treg.to/call/moz.web.url.metrics')
        self.assertEqual(captured['method'], 'POST')
        self.assertEqual(captured['headers'].get('x-treg-token'), 'fake-treg-token')
        self.assertEqual(json.loads(captured['body']), {'targets': ['moz.com']})

    def test_get_call_sends_params_as_a_query_string_not_a_body(self):
        # Confirmed LIVE (2026-09-26): a GET endpoint's params belong in the
        # URL query string, never a request body -- Treg's own real error
        # for the opposite mistake was explicit ("is GET -- add --method
        # GET", then "needs --query woeid=<value>").
        captured = {}

        def fake_urlopen(req, timeout=30):
            captured['url'] = req.full_url
            captured['method'] = req.get_method()
            captured['body'] = req.data
            return self._resp(b'{"data": [{"trend_name": "AI safety"}]}')

        with unittest.mock.patch.object(serve, '_open_secret', return_value='fake-treg-token'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=fake_urlopen):
            data, error = serve._treg_call('x.x.get-trends-by-woeid', {'woeid': 1}, method='GET')
        self.assertIsNone(error)
        self.assertEqual(captured['method'], 'GET')
        self.assertIsNone(captured['body'])
        self.assertEqual(captured['url'], 'https://treg.to/call/x.x.get-trends-by-woeid?woeid=1')

    def test_non_json_response_falls_back_to_raw_text(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value='fake-token'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        return_value=self._resp(b'not json at all')):
            data, error = serve._treg_call('some.endpoint', {})
        self.assertIsNone(error)
        self.assertEqual(data, {'_raw': 'not json at all'})

    def test_http_error_is_surfaced_with_status_and_detail(self):
        def raise_http_error(req, timeout=30):
            raise urllib.error.HTTPError(req.full_url, 402, 'Payment Required',
                                         {}, io.BytesIO(b'insufficient balance'))
        with unittest.mock.patch.object(serve, '_open_secret', return_value='fake-token'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=raise_http_error):
            data, error = serve._treg_call('x.x.get-trends-by-woeid', {'woeid': 1})
        self.assertIsNone(data)
        self.assertIn('402', error)
        self.assertIn('insufficient balance', error)

    def test_network_exception_is_surfaced_not_raised(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value='fake-token'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=OSError('timed out')):
            data, error = serve._treg_call('x.x.get-trends-by-woeid', {'woeid': 1})
        self.assertIsNone(data)
        self.assertIn('timed out', error)

    def test_known_endpoint_prices_match_what_was_verified_live(self):
        # x.x.get-trends-by-woeid: one real live call, billed exactly $0.01.
        # scrapecreators.x.v1-linkedin-search-posts: independently RE-DERIVED
        # from 2 real billed calls ($0.00376 total / 2 = $0.00188), matching
        # the catalog's own stated price exactly -- not just trusted from
        # the catalog page alone (see the Treg skill's own "Lessons learned"
        # about a spike that invented wrong prices once).
        self.assertEqual(serve.TREG_ENDPOINT_COSTS['x.x.get-trends-by-woeid'], 0.01)
        self.assertEqual(serve.TREG_ENDPOINT_COSTS['scrapecreators.x.v1-linkedin-search-posts'], 0.00188)


class JevQuorumDecision(unittest.TestCase):
    """Quorum sensing (2026-09-26), ported from real Temnothorax ant nest-site
    selection: pool multiple independent Jev samples specifically to overcome
    errors inherent in any ONE sample -- live-confirmed problem here (the
    same URL got a confident 0.87 allow one run, a low-confidence 0.56
    escalate-and-deny the next). Only re-samples on an ambiguous FIRST call
    (a low-confidence allow); a confident allow or a firm block costs one
    sample, same as before -- real money per extra sample, unlike a real ant."""

    def _mock_sample_sequence(self, choices):
        # choices: list of {'answers': {...}} dicts, one per _call_openrouter_
        # decision_sync invocation, consumed in order.
        return unittest.mock.patch.object(serve, '_call_openrouter_decision_sync', side_effect=choices)

    def _answer(self, decision, confidence, cost=0.01):
        return {'answers': {'choice': {'choice': decision, 'confidence': confidence, 'probabilities': {}}},
                'usage': {'cost': cost}}

    def test_confident_allow_takes_exactly_one_sample(self):
        with self._mock_sample_sequence([self._answer('allow', 0.9)]) as mock_call:
            decision, confidence, cost, _trace = asyncio.run(serve._jev_quorum_decision('i', {}))
        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual((decision, confidence), ('allow', 0.9))

    def test_firm_block_takes_exactly_one_sample_even_at_low_confidence(self):
        # A block is a block -- quorum sensing exists to resolve an unsure
        # ALLOW, not to second-guess a confident classifier that already
        # said no.
        with self._mock_sample_sequence([self._answer('block', 0.3)]) as mock_call:
            decision, confidence, cost, _trace = asyncio.run(serve._jev_quorum_decision('i', {}))
        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual(decision, 'block')

    def test_ambiguous_allow_resamples_up_to_quorum_sample_size(self):
        with self._mock_sample_sequence([self._answer('allow', 0.4)] * serve.QUORUM_SAMPLE_SIZE) as mock_call:
            asyncio.run(serve._jev_quorum_decision('i', {}))
        self.assertEqual(mock_call.call_count, serve.QUORUM_SAMPLE_SIZE)

    def test_two_of_three_agreeing_allow_forms_a_quorum_and_reports_the_strongest_agreeing_confidence(self):
        with self._mock_sample_sequence([
            self._answer('allow', 0.4),   # ambiguous first sample -> triggers resampling
            self._answer('allow', 0.85),  # agrees, confidently
            self._answer('block', 0.9),   # disagrees -- excluded from the agreeing set
        ]):
            decision, confidence, cost, _trace = asyncio.run(serve._jev_quorum_decision('i', {}))
        self.assertEqual(decision, 'allow')
        self.assertAlmostEqual(confidence, 0.85)  # the strongest AGREEING vote, not an average

    def test_no_quorum_reports_the_original_low_confidence_result(self):
        with self._mock_sample_sequence([
            self._answer('allow', 0.4),
            self._answer('block', 0.9),
            self._answer('block', 0.8),
        ]):
            decision, confidence, cost, _trace = asyncio.run(serve._jev_quorum_decision('i', {}))
        # Only 1 agreeing vote (< QUORUM_MIN_AGREEING) -- stays the original
        # unsure result, not silently upgraded or downgraded.
        self.assertEqual((decision, confidence), ('allow', 0.4))

    def test_total_cost_sums_every_sample_actually_taken(self):
        with self._mock_sample_sequence([
            self._answer('allow', 0.4, cost=0.01),
            self._answer('allow', 0.5, cost=0.02),
            self._answer('block', 0.9, cost=0.03),
        ]):
            _decision, _confidence, cost, _trace = asyncio.run(serve._jev_quorum_decision('i', {}))
        self.assertAlmostEqual(cost, 0.06)

    def test_a_failed_resample_is_skipped_not_fatal(self):
        with self._mock_sample_sequence([
            self._answer('allow', 0.4),
            RuntimeError('network blip'),
            self._answer('allow', 0.85),
        ]):
            decision, confidence, cost, _trace = asyncio.run(serve._jev_quorum_decision('i', {}))
        # Still reaches a quorum from the 2 real votes despite the blip.
        self.assertEqual(decision, 'allow')
        self.assertAlmostEqual(confidence, 0.85)

    def test_unreachable_classifier_on_the_first_call_fails_closed(self):
        with self._mock_sample_sequence([RuntimeError('down')]) as mock_call:
            decision, confidence, cost, _trace = asyncio.run(serve._jev_quorum_decision('i', {}))
        self.assertEqual(mock_call.call_count, 1)
        self.assertIsNone(decision)
        self.assertEqual(confidence, 1.0)

    def test_trace_id_is_propagated_from_the_decision_call(self):
        # The decision_tape row's trace_id must reach the caller so it can be
        # stamped on the resulting action_log rows (the calibration report's
        # exact join key). The real _call_openrouter_decision_sync stamps it
        # into the returned data; here we simulate that by including it.
        with self._mock_sample_sequence([
            {'answers': {'choice': {'choice': 'allow', 'confidence': 0.9, 'probabilities': {}}},
             'usage': {'cost': 0.01}, 'trace_id': 'deadbeef'},
        ]):
            decision, confidence, _cost, trace_id = asyncio.run(serve._jev_quorum_decision('i', {}))
        self.assertEqual((decision, confidence), ('allow', 0.9))
        self.assertEqual(trace_id, 'deadbeef')


class DecisionCalibration(unittest.TestCase):
    """_decision_calibration_report (2026-09-28, takeaway #1): bucket Jev's
    stated confidence against the real success rate of the actions it allowed,
    so an overconfident classifier is visible instead of quietly eroding the
    low-confidence-escalation floor. Reads only action_log (gate rows carry
    confidence+decision, outcome rows carry success/failure) -- no model
    calls, no network."""

    def setUp(self):
        # The module DB is shared across methods in this file, and the report
        # reads a 7-day window -- clear the table so each test sees only its
        # own rows.
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')

    def _gate(self, agent, action, decision, confidence, ts, trace_id=None):
        serve.log_action(agent, action, {'target': f'http://x/{ts}', 'purpose': 'p',
                                         'decision': decision, 'confidence': confidence, 'cost': 0.01},
                         authorized=True, trace_id=trace_id)
        with serve._db() as conn:
            conn.execute('UPDATE action_log SET ts = ? WHERE id = (SELECT MAX(id) FROM action_log)', (ts,))

    def _log_outcome(self, agent, action, decision, ts, trace_id=None):
        serve.log_action(agent, action, {'url': f'http://x/{ts}', 'decision': decision}, authorized=True, trace_id=trace_id)
        with serve._db() as conn:
            conn.execute('UPDATE action_log SET ts = ? WHERE id = (SELECT MAX(id) FROM action_log)', (ts,))

    def test_successful_and_failed_allows_are_scored_per_bucket(self):
        base = time.time()
        tid = 0
        for conf, outcome, n in [
            (0.95, 'allowed', 4),   # high-confidence, mostly succeeds
            (0.95, 'allowed_but_fetch_failed', 1),
            (0.55, 'allowed', 1),   # low-confidence, fails -> below the safety floor
            (0.55, 'allowed_but_fetch_failed', 1),
        ]:
            for _ in range(n):
                tid += 1
                self._gate('ada', 'browse', 'allowed', conf, base + 10 * tid, trace_id=f't{tid}')
                self._log_outcome('ada', 'browse', outcome, base + 10 * tid + 1, trace_id=f't{tid}')
        report = serve._decision_calibration_report(window_s=7 * 86400)
        buckets = {b['bin']: b for b in report['buckets']}
        hi = buckets['0.90-1.00']
        self.assertEqual(hi['n_allowed'], 5)
        self.assertEqual(hi['n_outcome'], 5)
        self.assertEqual(hi['n_success'], 4)
        self.assertEqual(hi['n_failure'], 1)
        self.assertEqual(hi['success_rate'], 0.8)
        lo = buckets['0.50-0.60']
        self.assertEqual(lo['n_outcome'], 2)
        self.assertEqual(lo['success_rate'], 0.5)

    def test_blocked_and_escalated_are_counted_but_not_scored(self):
        base = time.time()
        self._gate('ada', 'browse', 'blocked', 0.9, base + 1)
        self._gate('ada', 'download', 'escalated_unsure', 0.4, base + 2)
        self._gate('ada', 'browse', 'allowed', 0.95, base + 3)
        report = serve._decision_calibration_report()
        buckets = {b['bin']: b for b in report['buckets']}
        hi = buckets['0.90-1.00']
        self.assertEqual(hi['n_blocked'], 1)
        self.assertEqual(hi['n_allowed'], 1)
        self.assertEqual(hi['n_outcome'], 0)  # no outcome row -> not scored
        lo = buckets['0.00-0.50']
        self.assertEqual(lo['n_escalated'], 1)
        self.assertEqual(report['total_decisions'], 3)
        self.assertEqual(report['scoreable'], 0)
        self.assertEqual(report['scoreable_fraction'], 0.0)

    def test_trace_id_outcome_match_wins_over_the_heuristic(self):
        base = time.time()
        # A gate with a threaded trace_id whose real outcome row is a failure;
        # an UNRELATED later 'allowed' row (different agent) must NOT be picked.
        self._gate('ada', 'browse', 'allowed', 0.9, base + 1, trace_id='t1')
        self._log_outcome('ada', 'browse', 'allowed_but_fetch_failed', base + 2, trace_id='t1')
        self._log_outcome('bob', 'browse', 'allowed', base + 3)  # unrelated, no trace
        report = serve._decision_calibration_report()
        hi = next(b for b in report['buckets'] if b['bin'] == '0.90-1.00')
        self.assertEqual(hi['n_success'], 0)
        self.assertEqual(hi['n_failure'], 1)
        self.assertEqual(hi['success_rate'], 0.0)

    def test_overconfident_classifier_shows_large_calibration_error(self):
        base = time.time()
        # Confident allows that only succeed half the time = overconfidence.
        for i in range(6):
            self._gate('ada', 'browse', 'allowed', 0.95, base + i, trace_id=f'o{i}')
            self._log_outcome('ada', 'browse', 'allowed' if i % 2 == 0 else 'allowed_but_fetch_failed', base + i + 0.1, trace_id=f'o{i}')
        report = serve._decision_calibration_report()
        self.assertEqual(report['overall_success_rate'], 0.5)
        # Weighted mismatch against the 0.95 center -> clearly > 0.
        self.assertGreater(report['calibration_error'], 0.4)


class CalibrationLoop(unittest.TestCase):
    """_calibration_adjust_pass + _effective_safety_confidence (2026-09-29,
    feedback-loop actuator for takeaway #1): the calibration report is the
    sensor -- the escalation threshold was never adjusted by it. This pass
    moves the LIVE bar (settings row `jev_safety_confidence`) so decisions
    auto-approved at ~the current confidence bar succeed at ~the claimed
    reliability: below target -> raise the bar, above -> lower it, and never
    move on noise. Safety-critical kinds ('blocked command' at floor 1.0)
    are unaffected -- only the routine default bar moves."""

    def setUp(self):
        # Shared module DB: each test sees only its own calibration rows and no
        # leftover threshold.
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')
            conn.execute("DELETE FROM settings WHERE key = 'jev_safety_confidence'")

    def _gate(self, confidence, ts, trace_id, ok_outcome=True):
        serve.log_action('ada', 'browse', {'target': f'http://x/{ts}', 'purpose': 'p',
                                           'decision': 'allowed', 'confidence': confidence, 'cost': 0.01},
                         authorized=True, trace_id=trace_id)
        serve.log_action('ada', 'browse', {'url': f'http://x/{ts}',
                                           'decision': 'allowed' if ok_outcome else 'allowed_but_fetch_failed'},
                         authorized=True, trace_id=trace_id)
        with serve._db() as conn:
            conn.execute("UPDATE action_log SET ts = ? WHERE id IN (SELECT id FROM action_log ORDER BY id DESC LIMIT 2)", (ts,))
            # gate row must precede its outcome row
            conn.execute("UPDATE action_log SET ts = ? WHERE id = (SELECT MIN(id) FROM action_log WHERE trace_id = ?)", (ts, trace_id))
            conn.execute("UPDATE action_log SET ts = ? WHERE id = (SELECT MAX(id) FROM action_log WHERE trace_id = ?)", (ts + 0.1, trace_id))

    def _seed_bin(self, confidence, total, failures, base=None):
        base = time.time() - 3600 if base is None else base
        for i in range(total):
            self._gate(confidence, base + i, f'trace{i}', ok_outcome=(i >= failures))

    def test_effective_confidence_defaults_to_the_constant_and_reflects_the_setting(self):
        self.assertEqual(serve._effective_safety_confidence(), serve.JEV_SAFETY_CONFIDENCE)
        serve._set_setting('jev_safety_confidence', '0.73')
        self.assertEqual(serve._effective_safety_confidence(), 0.73)

    def test_effective_confidence_rejects_out_of_range_values(self):
        serve._set_setting('jev_safety_confidence', '0.99')
        self.assertEqual(serve._effective_safety_confidence(), serve.JEV_SAFETY_CONFIDENCE,
                         'above the clamp must fall back to the safe constant')
        serve._set_setting('jev_safety_confidence', 'junk')
        self.assertEqual(serve._effective_safety_confidence(), serve.JEV_SAFETY_CONFIDENCE)

    def test_raises_the_bar_when_actions_at_it_succeed_below_target(self):
        # Threshold baseline 0.6 sits in the 0.60-0.70 bin; half of the 20
        # allowed actions it auto-approved failed -> 50% success at 90% target.
        self._seed_bin(0.65, 20, failures=10)
        new = serve._calibration_adjust_pass()
        self.assertEqual(new, 0.65, 'a reliability shortfall must raise the bar one step')
        self.assertEqual(serve._get_setting('jev_safety_confidence'), '0.65')
        with serve._db() as conn:
            row = conn.execute("SELECT COUNT(*) FROM action_log WHERE action = 'jev_calibration_adjust'").fetchone()[0]
        self.assertEqual(row, 1, 'the adjust must be audit-logged')

    def test_lowers_the_bar_when_actions_at_it_succeed_well_above_target(self):
        self._seed_bin(0.65, 10, failures=0)  # 100% success = over-target
        new = serve._calibration_adjust_pass()
        self.assertEqual(new, 0.55, 'reliability above target lets the bar come down one step')
        self.assertEqual(serve._get_setting('jev_safety_confidence'), '0.55')

    def test_dead_band_does_not_churn_the_bar(self):
        # 13/15 succeed (0.867) -- within the 0.05 dead-band of the 0.9 target.
        self._seed_bin(0.65, 15, failures=2)
        new = serve._calibration_adjust_pass()
        self.assertIsNone(new)
        self.assertIsNone(serve._get_setting('jev_safety_confidence'),
                          'inside the dead-band the bar must not move at all')

    def test_no_move_without_enough_scored_decisions(self):
        self._seed_bin(0.65, 3, failures=3)  # terrible rate, but only 3 samples
        new = serve._calibration_adjust_pass()
        self.assertIsNone(new, 'a handful of decisions must never move the safety bar')
        self.assertIsNone(serve._get_setting('jev_safety_confidence'))

    def test_raises_clamp_at_the_max_threshold(self):
        serve._set_setting('jev_safety_confidence', '0.9')
        self._seed_bin(0.95, 10, failures=10)  # all failed at the top bin
        new = serve._calibration_adjust_pass()
        self.assertEqual(new, 0.95, 'the raised bar must clamp at the max')
        self.assertEqual(serve._get_setting('jev_safety_confidence'), '0.95')
        # Already at max: another pass must not move (and not log a bogus adjust).
        self.assertIsNone(serve._calibration_adjust_pass())

    def test_gate_floors_track_the_live_threshold_but_risk_kinds_stay_fixed(self):
        serve._set_setting('jev_safety_confidence', '0.75')
        self.assertEqual(serve._escalation_floor('unresolved review requirement'), 0.75,
                         'the routine floor must track the live calibration threshold')
        self.assertEqual(serve._escalation_floor('brand new kind'), 0.75)
        self.assertEqual(serve._escalation_floor('blocked command'), 1.0,
                         "a human's own blocked verdict is never auto-approved")
        self.assertEqual(serve._escalation_floor('unsure safety decision'), 0.85,
                         'the risky kind keeps its own raised floor, fixed')


class JevQuorumChoiceSync(unittest.TestCase):
    """_jev_quorum_choice_sync: real gap caught (2026-09-26) -- quorum
    sampling was only ever applied to safety gates, never to this module's
    own routine-but-consequential multi-way routing deciders (request lane,
    team, room, product, the peer-report worker picker, the escalation
    delegated-approval decision). Unlike the safety-specific async version,
    there's no 'a firm block is always trusted' asymmetry -- every option
    is equally worth getting right, so this resamples on ANY low-confidence
    result and resolves by plurality vote, not an allow/block rule."""

    def _mock_sample_sequence(self, choices):
        return unittest.mock.patch.object(serve, '_call_openrouter_decision_sync', side_effect=choices)

    def _answer(self, decision, confidence, cost=0.01):
        return {'answers': {'choice': {'choice': decision, 'confidence': confidence, 'probabilities': {}}},
                'usage': {'cost': cost}}

    def test_confident_choice_takes_exactly_one_sample(self):
        with self._mock_sample_sequence([self._answer('room_a', 0.9)]) as mock_call:
            decision, confidence, cost = serve._jev_quorum_choice_sync('i', {})
        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual((decision, confidence), ('room_a', 0.9))

    def test_low_confidence_resamples_up_to_quorum_sample_size(self):
        with self._mock_sample_sequence([self._answer('room_a', 0.4)] * serve.QUORUM_SAMPLE_SIZE) as mock_call:
            serve._jev_quorum_choice_sync('i', {})
        self.assertEqual(mock_call.call_count, serve.QUORUM_SAMPLE_SIZE)

    def test_plurality_vote_picks_the_option_most_samples_agreed_on(self):
        with self._mock_sample_sequence([
            self._answer('room_a', 0.4),  # ambiguous first sample -> triggers resampling
            self._answer('room_b', 0.85),
            self._answer('room_b', 0.5),
        ]):
            decision, confidence, cost = serve._jev_quorum_choice_sync('i', {})
        self.assertEqual(decision, 'room_b')  # 2 votes beats 1
        self.assertAlmostEqual(confidence, 0.85)  # the winning option's OWN strongest vote

    def test_three_way_tie_breaks_on_highest_confidence(self):
        with self._mock_sample_sequence([
            self._answer('room_a', 0.4),
            self._answer('room_b', 0.55),
            self._answer('room_c', 0.3),
        ]):
            decision, confidence, cost = serve._jev_quorum_choice_sync('i', {})
        self.assertEqual(decision, 'room_b')  # all 1 vote each -- highest confidence wins
        self.assertAlmostEqual(confidence, 0.55)

    def test_total_cost_sums_every_sample_actually_taken(self):
        with self._mock_sample_sequence([
            self._answer('room_a', 0.4, cost=0.01),
            self._answer('room_b', 0.5, cost=0.02),
            self._answer('room_a', 0.45, cost=0.03),
        ]):
            _decision, _confidence, cost = serve._jev_quorum_choice_sync('i', {})
        self.assertAlmostEqual(cost, 0.06)

    def test_a_failed_resample_is_skipped_not_fatal(self):
        with self._mock_sample_sequence([
            self._answer('room_a', 0.4),
            RuntimeError('network blip'),
            self._answer('room_a', 0.7),
        ]):
            decision, confidence, cost = serve._jev_quorum_choice_sync('i', {})
        self.assertEqual(decision, 'room_a')
        self.assertAlmostEqual(confidence, 0.7)

    def test_unreachable_classifier_on_the_first_call_fails_closed(self):
        with self._mock_sample_sequence([RuntimeError('down')]) as mock_call:
            decision, confidence, cost = serve._jev_quorum_choice_sync('i', {})
        self.assertEqual(mock_call.call_count, 1)
        self.assertIsNone(decision)
        self.assertEqual(confidence, 1.0)


class _ClassifyCommandJev(unittest.TestCase):
    # The shared command gate behind /api/execute and /api/pipeline. Same
    # confidence contract as the URL gates: low-confidence allow escalates.
    @classmethod
    def setUpClass(cls):
        serve.init_db()

    def _run(self, decision, confidence):
        async def go():
            return await serve._classify_command('echo hi', 'runs a test')
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'test-key'), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        return_value={'answers': {'choice': {'choice': decision, 'confidence': confidence, 'probabilities': {}}}}), \
             unittest.mock.patch.object(serve, '_send_escalation_email_sync') as mock_email, \
             unittest.mock.patch.object(serve, '_load_escalations', return_value={}), \
             unittest.mock.patch.object(serve, '_save_escalations') as mock_save:
            allowed, reason = asyncio.run(go())
        return allowed, reason, mock_save

    def test_confident_allow_runs(self):
        allowed, reason, _ = self._run('allow', 0.95)
        self.assertTrue(allowed)
        self.assertEqual(reason, 'allow')

    def test_low_confidence_allow_escalates_and_blocks(self):
        allowed, reason, save_called = self._run('allow', 0.4)
        self.assertFalse(allowed, 'low-confidence allow must not run the command')
        self.assertIn('unsure', reason)
        save_called.assert_called()

    def test_block_blocks_without_escalation(self):
        allowed, reason, save_called = self._run('block', 0.99)
        self.assertFalse(allowed)
        save_called.assert_not_called()


class ResearchContentExecutor(unittest.TestCase):
    # Phase 3 slice 2: the research content executor runs the real crawl > save
    # > chat > Library-write pipeline on a background thread, then stashes its
    # result ({note, seenUrls}) for the next sim task_cycle pass. Hermetic test:
    # patch the loopback helper to return canned endpoint responses and assert
    # the executor's store call -- no live network, no real Jev/rate-limit.

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='village-research-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            VILLAGE_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()
        self.stored = {}
        self.http_log = []

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_http(self, browse_pages, synth_reply=None, existing_skill=None):
        # Returns a mapped fake for serve._http_json.
        pages = list(browse_pages)  # [{url, text, links:[...], lastModified}]

        def fake(method, base, path, body=None, header=None):
            self.http_log.append((method, path))
            if path == '/api/browse':
                p = pages.pop(0) if pages else None
                if p is None:
                    return {'error': 'exhausted'}
                resp = {'allowed': True, 'url': p['url'], 'text': p.get('text', ''),
                        'links': p.get('links', []),
                        'lastModified': p.get('lastModified')}
                return resp
            if path == '/api/sandbox-save-page':
                return {'allowed': True, 'ok': True}
            if path == '/api/library/file' and method == 'GET':
                if existing_skill is None:
                    return {'error': 'not found'}
                return {'path': 'skills/x.md', 'content': existing_skill}
            if path == '/api/chat':
                if synth_reply is None:
                    return {'error': 'model failed'}
                return {'reply': synth_reply}
            if path == '/api/library/file' and method == 'POST':
                return '_raw = saved'
            return {'error': f'unexpected {method} {path}'}

        return fake

    def test_executor_crawl_save_chat_store(self):
        pages = [
            {'url': 'https://a.example', 'text': 'alpha unique content', 'links': [], 'lastModified': 0},
        ]
        # Seed a mid-tier so synthesis proceeds (the test DB's model_tiers is
        # empty by default; _mid_tier_slug correctly returns None otherwise).
        with serve._db() as conn:
            conn.execute("INSERT OR REPLACE INTO model_tiers (band, slug, name, price_per_m, chosen_at) "
                         "VALUES ('mid', 'test-mid-model', 'Test Mid', 0.0, 0)")
        import sim as _sim
        real_store = _sim._store_content_result
        _sim._store_content_result = lambda tid, r: self.stored.update({tid: r})
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(pages, synth_reply='# Weather\n\nFresh synthesis.'), create=False):
                topic = {'id': 't1', 'topic': 'weather data', 'startUrl': 'https://a.example',
                         'seenUrls': [], 'since': 0}
                task = {'id': 'task-1', 'room': 'observatory',
                        'research': {'topicId': 't1', 'since': 0}}
                serve._run_research_content({'researchTopics': [topic]}, 'ada', task, {})
        finally:
            _sim._store_content_result = real_store
        self.assertIn('task-1', self.stored, 'executor must store a completed result')
        result = self.stored['task-1']
        self.assertIn('collected 1 new page', result['note'])
        self.assertIn('https://a.example', result['seenUrls'])
        # The skill file write must have been attempted (library POST).
        posts = [p for (m, p) in self.http_log if m == 'POST' and p == '/api/library/file']
        self.assertEqual(len(posts), 1, 'a fresh skill synthesis must write via /api/library/file')

    def test_executor_dedups_already_collected_unchanged(self):
        # A page already in seenUrls and unchanged since `since` is NOT saved
        # again (the crawl's dedup), and with nothing new kept there is no
        # synthesis/library-write -- the note reflects "nothing new".
        pages = [
            {'url': 'https://b.example', 'text': 'beta content', 'links': [],
             'lastModified': 1000000},
        ]
        topic = {'id': 't1', 'topic': 'weather data',
                 'startUrl': 'https://b.example', 'seenUrls': ['https://b.example'],
                 'since': 2000000}  # page lastModified < since -> unchanged
        task = {'id': 'task-2', 'room': 'observatory',
                'research': {'topicId': 't1', 'since': 2000000}}
        import sim as _sim
        real_store = _sim._store_content_result
        _sim._store_content_result = lambda tid, r: self.stored.update({tid: r})
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(pages, synth_reply=None), create=False):
                serve._run_research_content({'researchTopics': [topic]}, 'ada', task, {})
        finally:
            _sim._store_content_result = real_store
        result = self.stored['task-2']
        self.assertIn('nothing new', result['note'])
        # No save of the unchanged page, no library write (synthesis skipped).
        saves = [p for (m, p) in self.http_log if p == '/api/sandbox-save-page']
        posts = [p for (m, p) in self.http_log if m == 'POST' and p == '/api/library/file']
        self.assertEqual(saves, [], 'an unchanged already-collected page must not be re-saved')
        self.assertEqual(posts, [], 'nothing kept -> no synthesis/library write')


class RemainingExecutors(unittest.TestCase):
    # Phase 3 slice 2 follow-ups: the weather / media / skill-review / research
    # branch-3 (projectLabel) and branch-4 (bare) content executors, plus the
    # _server_content_dispatcher router. Hermetic: patch serve._http_json (and
    # the Jev decision sync) to return canned endpoint responses and assert the
    # store call. No live network, no real Jev, no real rate-limiter.

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='village-exec-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            VILLAGE_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()
        self.stored = {}
        self.http_log = []
        with serve._db() as conn:
            # Any executor that models a finding or digests needs a mid-tier.
            conn.execute("INSERT OR REPLACE INTO model_tiers (band, slug, name, price_per_m, chosen_at) "
                         "VALUES ('mid', 'test-mid-model', 'Test Mid', 0.0, 0)")

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _record(self, tid, r):
        self.stored.update({tid: r})

    def _fake_http(self, **kw):
        # kw-driveable fake for serve._http_json (route-by-route overrides).
        browse = kw.get('browse')  # list of {'allowed':..., 'text':...}
        chatreply = kw.get('chat')
        library_file = kw.get('library_file')  # dict path->content for GET
        feeds_content = kw.get('feeds_content')
        pipeline_ok = kw.get('pipeline_ok', True)

        def fake(method, base, path, body=None, header=None):
            self.http_log.append((method, path))
            # library file reads carry ?path= in the query string (urllib puts it
            # in the URL, not the body); parse it for GETs and strip the query so
            # the simple string matches below work.
            if path.startswith('/api/library/file') and '?' in path:
                raw, _, query = path.partition('?')
                if method == 'GET' and body is None:
                    from urllib.parse import parse_qs
                    qs = parse_qs(query)
                    body = {'path': qs.get('path', [''])[0]}
                path = raw
            if path == '/api/browse':
                p = browse.pop(0) if browse else None
                if p is None:
                    return {'error': 'exhausted'}
                return {'allowed': p.get('allowed', True), 'text': p.get('text', ''), 'url': p.get('url', '')}
            if path == '/api/chat':
                return {'reply': chatreply} if chatreply else {'error': 'model failed'}
            if path == '/api/library/file' and method == 'GET':
                q = (body if isinstance(body, dict) else {})
                name = q.get('path', '').split('/')[-1]
                content = (library_file or {}).get(name)
                if content is None:
                    return {'error': 'not found'}
                return {'path': q.get('path'), 'content': content}
            if path == '/api/library/file' and method == 'POST':
                return '_raw = saved'
            if path == '/api/library/promote' or path == '/api/library/reject':
                return {'ok': True, 'path': (body or {}).get('path')}
            if path == '/api/library':
                return {'files': kw.get('library_files', [])}
            if path == '/api/pipeline':
                return {'ok': pipeline_ok, 'failedStep': kw.get('pipeline_failed_step')}
            return {'error': f'unexpected {method} {path}'}
        return fake

    def _restore(self):
        import sim as _sim
        _sim._store_content_result = self._real_store

    def _patch_store(self):
        import sim as _sim
        self._real_store = _sim._store_content_result
        _sim._store_content_result = self._record

    def test_weather_notes_and_recorded(self):
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(browse=[{'allowed': True, 'text': 'Weather is unpredictable today.'}]),
                                            create=False):
                serve._run_weather_content({}, 'ada', {'id': 'w1', 'room': 'weatherstation'}, {})
        finally:
            self._restore()
        self.assertIn('w1', self.stored)
        self.assertIn('weather', self.stored['w1']['note'].lower())

    def test_weather_denied(self):
        # An unapproved browse -> a soft "wasn't approved" note (no crash).
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(browse=[{'allowed': False, 'text': ''}]),
                                            create=False):
                serve._run_weather_content({}, 'ada', {'id': 'w2', 'room': 'weatherstation'}, {})
        finally:
            self._restore()
        self.assertIn('wasn\'t approved', self.stored['w2']['note'])

    def test_media_digest_no_feeds(self):
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(library_file={'feeds.md': '# feeds\n'}), create=False):
                serve._run_media_content({}, 'ada', {'id': 'm1', 'room': 'media'}, {})
        finally:
            self._restore()
        self.assertEqual([], [p for p in self.http_log if p[1] == '/api/chat'])
        self.assertIn('No feeds configured', self.stored['m1']['note'])

    def test_media_digest_fetches_summarizes_files(self):
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(
                                                library_file={'feeds.md': '# feeds\nhttps://news.example'},
                                                browse=[{'allowed': True, 'text': 'Long enough body text about the news.'}],
                                                chat='Here is a summary of the news item.'), create=False):
                serve._run_media_content({'agents': {'ada': {'name': 'Ada'}}}, 'ada', {'id': 'm2', 'room': 'media'}, {})
        finally:
            self._restore()
        posts = [p for p in self.http_log if p[1] == '/api/library/file' and p[0] == 'POST']
        self.assertEqual(1, len(posts), 'a digest must be filed to the Library')
        self.assertIn('digest', self.stored['m2']['note'].lower())

    def test_skill_review_promotes_and_rejects(self):
        # Three pending files with a fake Jev (one reject, two keep).
        self._patch_store()
        pending = [{'path': f'pending_review/skills/{n}.md'} for n in ('a', 'b', 'c')]
        libfiles = {
            'a.md': 'skill a body',
            'b.md': 'skill b body',
            'c.md': 'skill c body',
        }
        real_jev = serve._call_openrouter_decision_sync
        choices = iter(['reject', 'keep', 'keep'])
        # Shape responses to match what _jev_choice reads: data['answers'].
        serve._call_openrouter_decision_sync = lambda *a, **k: {
            'answers': {'q': {'choice': next(choices), 'confidence': 0.9}},
            'usage': {'cost': 0.005},
        }
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(library_file=libfiles, library_files=pending), create=False):
                serve._run_skill_review_content({'agents': {'ada': {'name': 'Ada'}}}, 'ada', {'id': 's1', 'room': 'observatory', 'skillReview': True}, {})
        finally:
            serve._call_openrouter_decision_sync = real_jev
            self._restore()
        promotes = [p for p in self.http_log if p[1] == '/api/library/promote']
        rejects = [p for p in self.http_log if p[1] == '/api/library/reject']
        self.assertEqual(2, len(promotes))
        self.assertEqual(1, len(rejects))
        self.assertIn('2 promoted, 1 rejected', self.stored['s1']['note'])

    def test_skill_review_none_pending(self):
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(library_files=[]), create=False):
                serve._run_skill_review_content({}, 'ada', {'id': 's2', 'room': 'observatory', 'skillReview': True}, {})
        finally:
            self._restore()
        self.assertIn('nothing waiting', self.stored['s2']['note'])

    def test_research_project_branch_logs_and_writes(self):
        # branch-3 (projectLabel): a real model finding -> pipeline log -> library write.
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(chat='A concrete finding about inference latency.'), create=False):
                serve._run_research_project_content(
                    {'agents': {'ada': {'name': 'Ada'}}, 'researchTopics': []},
                    'ada',
                    {'id': 'p1', 'room': 'observatory', 'projectLabel': 'Inference Lab',
                     'title': 'optimize speculative decoding', 'instructions': 'find the top bottleneck'}, {})
        finally:
            self._restore()
        posts = [p for p in self.http_log if p[1] == '/api/library/file' and p[0] == 'POST']
        self.assertEqual(1, len(posts), 'a real finding must be written to the Library')
        self.assertIn('optimize speculative decoding', self.stored['p1']['note'])

    def test_research_bare_branch_failure_note(self):
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(pipeline_ok=False, pipeline_failed_step='tally findings so far'),
                                            create=False):
                serve._run_research_bare_content({}, 'ada', {'id': 'b1', 'room': 'observatory'}, {})
        finally:
            self._restore()
        self.assertIn('hit a failure at "tally findings so far"', self.stored['b1']['note'])

    def test_spike_lands_findings_and_marks_ok(self):
        # A spike (Phase E2b) is room-agnostic and files a findings artifact --
        # never a product release. Route it straight through the dispatcher to
        # prove the room-agnostic dispatch happens. Hermetic against the
        # tool-loop spike executor (2026-09-27): the executor calls the model
        # DIRECTLY (_call_openrouter_sync for plan/synthesize, _post_openrouter_raw
        # for the tool loop), not through _http_json -- so both are mocked here.
        self._patch_store()
        fake_model = {'choices': [{'message': {'content': 'WebAssembly works headless via a patched GLIBC stub.'}}],
                      'usage': {'cost': 0.0}}

        def fake_raw(model, messages, tools=None, max_tokens=None, tool_choice=None):
            # Round 1: forced tool call -- have the model browse a page so a
            # real (mocked) tool runs and the spike counts as "investigated".
            # The browse_page tool is served by _http_json (mocked) below.
            if tool_choice is not None:
                return {'choices': [{'message': {'tool_calls': [
                    {'id': 'call_1', 'function': {'name': 'browse_page',
                     'arguments': '{"url": "https://example.com", "purpose": "verify WASM"}'}},
                ]}}], 'usage': {'cost': 0.0}}
            # Round 2: model settles with its investigation summary.
            return {'choices': [{'message': {'content': 'Investigated the WASM question.'}}],
                    'usage': {'cost': 0.0}}

        with unittest.mock.patch.object(serve, '_http_json',
                                        self._fake_http(chat='WebAssembly works headless via a patched GLIBC stub.'),
                                        create=False), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync', return_value=fake_model), \
             unittest.mock.patch.object(serve, '_post_openrouter_raw', side_effect=fake_raw):
            serve._server_content_dispatcher(
                {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                {'id': 'sp1', 'room': 'pressoffice', 'taskType': 'spike',
                 'title': 'Does WASM work headless?', 'instructions': 'spike it',
                 'budgetMs': 30_000}, {})
        self._restore()
        posts = [p for p in self.http_log if p[1] == '/api/library/file' and p[0] == 'POST']
        self.assertEqual(1, len(posts), 'a spike must file a findings artifact')
        self.assertTrue(self.stored['sp1'].get('ok'), 'a successful spike commits a real finding')
        # The full finding lives in the library file (data-minimization, 2026-09-24),
        # not the short note -- the note points at the artifact instead.
        self.assertIsNotNone(self.stored['sp1'].get('libraryPath'))

    def test_spike_model_failure_reports_not_ok(self):
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(), create=False):
                serve._server_content_dispatcher(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'sp2', 'room': 'observatory', 'taskType': 'spike',
                     'title': 'failed spike', 'budgetMs': 30_000}, {})
        finally:
            self._restore()
        self.assertFalse(self.stored['sp2'].get('ok'),
                         'a spike whose model call fails must not claim fabricated findings')


class PressOfficeContentExecutors(unittest.TestCase):
    # Phase 3: the Work Room / Press Office content executors -- _run_workroom_content
    # (the dispatcher), _run_coding_content (probe-before-writing + heredoc
    # continuation + the four mechanical integrity checks), and _run_review_content
    # (unified context + critique + screenshot visual + Jev actionable/clean that
    # queues a follow-up fix via 'queueFix'). Hermetic: patch serve._http_json with
    # a command-routing fake and the Jev decision sync, then assert the store call.
    # No live network, no real Jev/rate-limit/execute.

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='village-pressoffice-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            VILLAGE_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()
        self.stored = {}
        self.http_log = []
        with serve._db() as conn:
            conn.execute("INSERT OR REPLACE INTO model_tiers (band, slug, name, price_per_m, chosen_at) "
                         "VALUES ('mid', 'test-mid-model', 'Test Mid', 0.0, 0)")
            conn.execute("INSERT OR REPLACE INTO model_tiers (band, slug, name, price_per_m, chosen_at) "
                         "VALUES ('coding', 'test-coding-model', 'Test Coding', 0.0, 0)")

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _record(self, tid, r):
        self.stored.update({tid: r})

    def _patch_store(self):
        import sim as _sim
        self._real_store = _sim._store_content_result
        _sim._store_content_result = self._record

    def _restore(self):
        import sim as _sim
        _sim._store_content_result = self._real_store

    def _fake_http(self, *, chat_chain=None, write_exit=0, write_allowed=True,
                   probe=None, screenshot=None, search_matches=None, ls_stdout=None):
        """Command-routing fake for serve._http_json. /api/chat pops the next
        entry from chat_chain (a list of reply strings, or {'error': ...}); any other
        route is routed by the request body's command/path."""
        chat_chain = list(chat_chain) if chat_chain else []

        def fake(method, base, path, body=None, header=None):
            self.http_log.append((method, path))
            # library file reads carry ?path= in the query string.
            if path.startswith('/api/library/file') and '?' in path:
                raw, _, query = path.partition('?')
                if method == 'GET' and body is None:
                    from urllib.parse import parse_qs
                    qs = parse_qs(query)
                    body = {'path': qs.get('path', [''])[0]}
                path = raw
            if path == '/api/chat':
                if not chat_chain:
                    return {'error': 'model failed'}
                nxt = chat_chain.pop(0)
                # A bare string is a reply; a dict is passed through verbatim
                # (so a test can force a failure with {'error': ...}).
                return {'reply': nxt} if isinstance(nxt, str) else nxt
            if path == '/api/library/search':
                return {'matches': search_matches or []}
            if path == '/api/library/file' and method == 'GET':
                return {'error': 'not found'}
            if path == '/api/library/file' and method == 'POST':
                return '_raw = saved'
            if path == '/api/page-probe':
                return probe if probe is not None else {'error': 'probe failed'}
            if path == '/api/screenshot':
                return screenshot if screenshot is not None else {'error': 'no headless browser'}
            if path == '/api/pipeline':
                return {'ok': write_allowed}  # workroom bare path
            if path == '/api/execute':
                body = body or {}
                command = (body.get('command') or '')
                # getSandboxContext budget-cat read.
                if command.startswith('budget=15000'):
                    return {'allowed': True, 'stdout': '--- index.html ---\n<html><body>hi</body></html>\n', 'exitCode': 0}
                # runWorkroomTask's ls -la context listing.
                if command == 'ls -la':
                    return {'allowed': True, 'stdout': ls_stdout or 'index.html\napp.js\n', 'exitCode': 0}
                # unlinked-check grep (all return LINKED so nothing is auto-linked).
                if command.startswith('grep -q'):
                    return {'allowed': True, 'stdout': 'LINKED:app.js', 'exitCode': 0}
                # phantom-ref check loop.
                if 'PHANTOM:' in command and 'EXISTS:' in command:
                    return {'allowed': True, 'stdout': '', 'exitCode': 0}
                # dangling-selector read loop.
                if command.startswith('for f in *.html *.js'):
                    return {'allowed': True, 'stdout': '', 'exitCode': 0}
                # everything else is the actual coding write command.
                return {'allowed': write_allowed, 'exitCode': write_exit, 'timedOut': False,
                        'stdout': '' if write_allowed else 'blocked by classifier', 'reason': None}
            return {'error': f'unexpected {method} {path}'}
        return fake

    # ---- workroom bare pipeline ----

    def test_workroom_bare_runs_shared_tooling(self):
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(), create=False):
                serve._run_workroom_content({}, 'ada',
                                            {'id': 'wr1', 'room': 'pressoffice', 'title': 'ambient check'}, {})
        finally:
            self._restore()
        pipes = [p for p in self.http_log if p[1] == '/api/pipeline']
        self.assertEqual(1, len(pipes), 'bare workroom runs the shared-tooling pipeline')
        self.assertIn('shared tooling in the Work Room', self.stored['wr1']['note'])

    def test_workroom_bare_reports_failed_step(self):
        # The bare shared-tooling pipeline can stop at a failed step; the note
        # must surface which step, not fail the executor.
        def failing_http(method, base, path, body=None, header=None):
            if path == '/api/pipeline':
                return {'ok': False, 'failedStep': 'run it'}
            return self._fake_http()(method, base, path, body, header)
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json', failing_http, create=False):
                serve._run_workroom_content({}, 'ada',
                                            {'id': 'wr1b', 'room': 'pressoffice', 'title': 'ambient check'}, {})
        finally:
            self._restore()
        self.assertIn('hit a failure at "run it"', self.stored['wr1b']['note'])

    # ---- workroom projectLabel -> coding ----

    def test_workroom_project_codes_and_runs(self):
        # A real project task calls the coding pipeline; a write heredoc command is
        # produced and executed, plus integrity checks run, then a success note.
        self._patch_store()
        chat = ['cat > app.js << \'EOF\'\nwindow.answer = 42;\nEOF']
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(chat_chain=chat), create=False):
                serve._run_workroom_content(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'wr2', 'room': 'pressoffice', 'title': 'implement scoring',
                     'instructions': 'add a score field', 'projectLabel': 'Snake'},
                    {})
        finally:
            self._restore()
        result = self.stored['wr2']
        # The coding executor stores its own note on success (env: the shared
        # sandbox write ran and no integrity check surfaced an issue).
        self.assertTrue(result.get('ok'))
        self.assertIn('shared Work Room sandbox', result['note'])
        # The write command must have been executed through /api/execute.
        self.assertIn('cat > app.js', result.get('command', ''))
        execs = [p for p in self.http_log if p[1] == '/api/execute']
        self.assertGreaterEqual(len(execs), 1, 'the coding write must go through /api/execute')

    # ---- coding probe loop + heredoc continuation ----

    def test_coding_probe_then_heredoc_continuation(self):
        # reply 1 = a probe request (real page check), reply 2 = truncated heredoc,
        # reply 3 = the EOF close line. The probe doesn't count against the
        # continuation budget; the final command is balanced and executed.
        probe_json = '{"probeRequest": {"path": "index.html", "actions": [{"type":"click","selector":"text=Go"}], "probes": ["typeof window.answer"]}}'
        truncated = "cat > app.js << 'EOF'\nwindow.answer = 1;"
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(
                                                chat_chain=[probe_json, truncated, '\nEOF'],
                                                probe={'actionLog': ['click Go'], 'console': [],
                                                       'pageErrors': [], 'customGlobals': [],
                                                       'results': {'typeof window.answer': 'undefined'}}),
                                            create=False):
                serve._run_coding_content(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'c1', 'room': 'pressoffice', 'title': 'add score', 'projectLabel': 'Snake'},
                    'Current files:\nindex.html\n')
        finally:
            self._restore()
        result = self.stored['c1']
        self.assertTrue(result.get('ok'), f"coding should succeed, got: {result}")
        # The probe round must not count against continuation attempts -- the
        # final command includes both the heredoc open and the EOF close.
        self.assertIn('EOF', result.get('command', ''))
        probes = [p for p in self.http_log if p[1] == '/api/page-probe']
        self.assertEqual(1, len(probes), 'one real page probe must have been run')

    def test_coding_blocked_by_execute(self):
        # The write command is produced but the /api/execute gate blocks it.
        self._patch_store()
        chat = ["cat > app.js << 'EOF'\nwindow.answer = 42;\nEOF"]
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(chat_chain=chat, write_allowed=False),
                                            create=False):
                serve._run_coding_content(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'c2', 'room': 'pressoffice', 'title': 'add score', 'projectLabel': 'Snake'},
                    {})
        finally:
            self._restore()
        result = self.stored['c2']
        self.assertFalse(result.get('ok'))
        self.assertIn('blocked', result['note'])

    # ---- review actionable / clean / model failure ----

    def _patched_jev(self, choice):
        real_jev = serve._call_openrouter_decision_sync
        serve._call_openrouter_decision_sync = lambda *a, **k: {
            'answers': {'q': {'choice': choice, 'confidence': 0.9}},
            'usage': {'cost': 0.005},
        }
        return real_jev

    def test_review_actionable_queues_fix(self):
        self._patch_store()
        real_jev = self._patched_jev('actionable')
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(
                                                chat_chain=['This genuinely has a real bug: the reset button calls an undefined function.'],
                                                screenshot={'imageBase64': 'c29h', 'width': 10, 'height': 10}),
                                            create=False):
                serve._run_review_content(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'r1', 'room': 'pressoffice', 'title': 'review the game',
                     'instructions': 'check it works', 'projectLabel': 'Snake', 'taskType': 'review'},
                    {})
        finally:
            serve._call_openrouter_decision_sync = real_jev
            self._restore()
        result = self.stored['r1']
        self.assertTrue(result.get('ok'))
        self.assertIn('queued a fix', result['note'])
        self.assertIsNotNone(result.get('queueFix'), 'an actionable review must enqueue a fix task')
        self.assertEqual('pressoffice', result['queueFix']['room'])
        # The review is filed to the Library.
        posts = [p for p in self.http_log if p[1] == '/api/library/file' and p[0] == 'POST']
        self.assertEqual(1, len(posts))

    def test_review_actionable_fix_returns_to_author_not_reviewer(self):
        # A gate review that rejects must hand the fix back to the ORIGINAL
        # author (the worker who built the story), not to the reviewer who ran
        # the review. The reviewer's own id travels in `assignedTo` on the
        # review subtask; the author's id travels in `reviewAuthorId`.
        self._patch_store()
        real_jev = self._patched_jev('actionable')
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(
                                                chat_chain=['Clear regression: the logout flow crashes.'],
                                                screenshot={'imageBase64': 'c29h', 'width': 10, 'height': 10}),
                                            create=False):
                serve._run_review_content(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'r3', 'room': 'pressoffice', 'title': 'review the game',
                     'instructions': 'check it works', 'projectLabel': 'Snake',
                     'taskType': 'review', 'assignedTo': 'ada',   # the reviewer
                     'reviewOf': 'parent-1',                       # a gate review
                     'reviewAuthorId': 'ben'},                    # the author
                    {})
        finally:
            serve._call_openrouter_decision_sync = real_jev
            self._restore()
        result = self.stored['r3']
        self.assertIsNotNone(result.get('queueFix'))
        self.assertEqual(result['queueFix']['assignedTo'], 'ben',  # pinned to author
                         'the fix for a rejected story must go to its author, not the reviewer')
        self.assertEqual(result['queueFix']['reviewOf'], 'parent-1')

    def test_review_clean_no_fix(self):
        self._patch_store()
        real_jev = self._patched_jev('clean')
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(
                                                chat_chain=['This genuinely looks solid -- nothing actionable.'],
                                                screenshot={'error': 'no browser'}),
                                            create=False):
                serve._run_review_content(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'r2', 'room': 'pressoffice', 'title': 'review the game',
                     'projectLabel': 'Snake', 'taskType': 'review'},
                    {})
        finally:
            serve._call_openrouter_decision_sync = real_jev
            self._restore()
        result = self.stored['r2']
        self.assertIn('nothing actionable found', result['note'])
        self.assertNotIn('queueFix', result, 'a clean review must not queue a fix')

    def test_review_model_failure_falls_back_to_note(self):
        # A chat call that returns nothing usable -> a soft "didn't produce
        # anything usable" note, no crash, no queueFix.
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(chat_chain=[{'error': 'model failed'}]),
                                            create=False):
                serve._run_review_content(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'r3', 'room': 'pressoffice', 'title': 'review the game',
                     'projectLabel': 'Snake', 'taskType': 'review'},
                    {})
        finally:
            self._restore()
        result = self.stored['r3']
        self.assertFalse(result.get('ok'))
        self.assertIn('didn\'t produce anything usable', result['note'])

    def test_dispatcher_routes_observatory_review_to_review_executor(self):
        # A peer-gate review/QA subtask inherits its PARENT story's room, so a
        # review of an observatory research deliverable arrives with room
        # 'observatory' + taskType 'review'. The dispatcher must honour taskType
        # BEFORE room: routing to the observatory research executors would
        # produce a "Researched..." note with NO peerVerdict, silently dropping
        # the vote and wedging the story in 'needs_review' forever (the infinite
        # re-review fork -- tasks 7-12 of the same story). The review executor
        # must run instead; only it emits the verdict the gate folds.
        self._patch_store()
        real_jev = self._patched_jev('clean')
        try:
            with unittest.mock.patch.object(
                    serve, '_http_json',
                    self._fake_http(
                        chat_chain=['The research artifact looks solid -- the bulletin is complete and accurate.'],
                        screenshot={'error': 'no headless browser'}),
                    create=False):
                serve._server_content_dispatcher(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'obsrv1', 'room': 'observatory', 'title': 'Draft weather bulletin report',
                     'instructions': 'write the bulletin', 'projectLabel': 'weather',
                     'taskType': 'review', 'reviewOf': 'task-4', 'assignedTo': 'ada',
                     'reviewAuthorId': 'ben'}, {})
        finally:
            serve._call_openrouter_decision_sync = real_jev
            self._restore()
        result = self.stored['obsrv1']
        self.assertEqual('clean', result.get('peerVerdict'),
                         'an observatory-roomed gate review must emit a peerVerdict the parent gate can fold')
        self.assertIn('Filed a review', result['note'],
                      'the review executor, not a research executor, must run for a review task in any room')


class PlayerIntentEndpoints(unittest.TestCase):
    """End-to-end tests of the HTTP routes added in Phase 4/5 -- GET /api/rooms,
    POST /api/intent/assign-big-task, and POST /api/rooms/{room}/purpose --
    driven through TestClient with the DB, model, and identity seams patched so
    nothing touches the real village.db or makes a real model call. TestClient
    is used WITHOUT a context manager so the app's lifespan (which starts the
    real sim/health/mail loops) never runs."""

    def _state(self, room_purpose=None):
        roster = [
            {'id': 'faye', 'isAdmin': True},
            {'id': 'nora', 'isDirector': True},
            {'id': 'zara', 'director': 'nora'},
        ]
        room_defs = {}
        for r, d in serve._DEFAULT_ROOM_DEFINITIONS.items():
            room_defs[r] = {'label': d['label'], 'purpose': d['purpose']}
        if room_purpose:
            room_defs['pressoffice']['purpose'] = room_purpose
        return {
            'agentRoster': roster,
            'agents': {d['id']: {'name': d['id'], 'busy': False, 'offDuty': False, 'role': 'x'} for d in roster},
            'workQueue': [],
            'researchTopics': [],
            'roomDefinitions': room_defs,
        }

    def test_get_rooms_returns_backfilled_definitions(self):
        state = self._state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state):
            c = TestClient(serve.app)
            r = c.get('/api/rooms')
        self.assertEqual(r.status_code, 200)
        rooms = r.json()['rooms']
        # /api/rooms surfaces every DEFINED room (delegable work rooms + the
        # non-delegable hangout), not just the delegable subset.
        self.assertEqual(set(rooms), set(serve._DEFAULT_ROOM_DEFINITIONS))
        self.assertIn('hangout', rooms)
        self.assertIn('pressoffice', rooms)
        self.assertEqual(rooms['pressoffice']['label'], 'Work Room')
        self.assertIn('software development', rooms['pressoffice']['purpose'])
        # No DB write should occur on a plain read.
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = TestClient(serve.app)
            c.get('/api/rooms')
            save.assert_not_called()

    def test_assign_big_task_queues_subtasks_from_model_reply(self):
        # The free admin (Faye) decomposes a goal; the server queues the
        # whitelisted subtasks into state and returns the UI-ready shape.
        fake_reply = {
            'reply': json.dumps({'subtasks': [
                {'title': 'build an index', 'room': 'pressoffice', 'instructions': 'one sentence', 'taskType': 'code'},
                {'title': 'research weather', 'room': 'observatory', 'instructions': 'one sentence',
                 'priority': 'high', 'notBefore': 'not-a-time'},
            ]})
        }
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self._state()), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, '_http_json', return_value=fake_reply), \
             unittest.mock.patch.object(serve, 'get_or_create_agent_key', return_value='key'), \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/intent/assign-big-task', json={'goal': 'make the village better'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['admin'], 'faye')
        self.assertEqual(len(body['subtasks']), 2)
        # Whitelisted rooms survive; the malformed notBefore fails open to None.
        self.assertEqual([s['room'] for s in body['subtasks']], ['pressoffice', 'observatory'])
        self.assertEqual(body['subtasks'][1]['notBefore'], None)
        self.assertEqual(body['subtasks'][1]['priority'], 'high')
        # The subtasks were queued server-side (durable workQueue).
        self.assertEqual(len(saved.get('workQueue', [])), 2)
        log.assert_called_once()

    def test_assign_big_task_rejects_non_whitelisted_rooms(self):
        fake_reply = {'reply': json.dumps({'subtasks': [
            {'title': 'bad', 'room': 'notroom', 'instructions': 'x'},
            {'title': 'good', 'room': 'bank', 'instructions': 'x'},
        ]})}
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self._state()), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, '_http_json', return_value=fake_reply), \
             unittest.mock.patch.object(serve, 'get_or_create_agent_key', return_value='key'), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/intent/assign-big-task', json={'goal': 'task'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([s['room'] for s in r.json()['subtasks']], ['bank'])

    def test_assign_big_task_no_free_authority_errors(self):
        state = self._state()
        # Everyone busy -> no authority -> error, no model call.
        for a in state['agents'].values():
            a['busy'] = True
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, '_http_json') as http, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/intent/assign-big-task', json={'goal': 'task'})
        self.assertIn('free right now', r.json()['error'])
        http.assert_not_called()

    def test_assign_big_task_wakes_resting_admin(self):
        # tasks.js: "a new request from you wakes the admin." With the admin
        # merely RESTING (offDuty, not busy) and no one else free, a delegation
        # request should wake her and proceed -- otherwise an idle village is
        # permanently un-delegable. Contrast the busy case above, which must
        # still error (you can't interrupt real work).
        state = self._state()
        for d in state['agentRoster']:
            state['agents'][d['id']]['offDuty'] = True
        fake_reply = {'reply': json.dumps({'subtasks': [
            {'title': 'build an index', 'room': 'pressoffice', 'instructions': 'one sentence', 'taskType': 'code'},
        ]})}
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, '_http_json', return_value=fake_reply), \
             unittest.mock.patch.object(serve, 'get_or_create_agent_key', return_value='key'), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/intent/assign-big-task', json={'goal': 'make the village better'})
        self.assertEqual(r.status_code, 200, r.text)
        # Faye (the admin) was woken on-duty and carried out the breakdown.
        self.assertEqual(r.json()['admin'], 'faye')
        self.assertEqual(state['agents']['faye']['offDuty'], False)
        self.assertEqual(state['agents']['faye']['visible'], True)
        self.assertEqual(len(saved.get('workQueue', [])), 1)

    def test_room_purpose_update_gated_to_director_and_persisted(self):
        state = self._state()
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='nora'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            c = TestClient(serve.app)
            r = c.post('/api/rooms/pressoffice/purpose', json={'purpose': 'nora rewrote this room'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['label'], 'Work Room', 'label is not editable')
        self.assertEqual(body['purpose'], 'nora rewrote this room')
        # Persisted to state.roomDefinitions through the merge-protected path.
        self.assertEqual(saved['roomDefinitions']['pressoffice']['purpose'], 'nora rewrote this room')
        log.assert_called_once()
        self.assertEqual(log.call_args[0][1], 'room_purpose_update')

    def test_room_purpose_update_blocks_rank_and_file(self):
        state = self._state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='zara'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = TestClient(serve.app)
            r = c.post('/api/rooms/pressoffice/purpose', json={'purpose': 'should fail'})
        self.assertEqual(r.status_code, 403)
        save.assert_not_called()

    def test_room_purpose_update_rejects_unknown_room(self):
        state = self._state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='nora'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/rooms/townhall/purpose', json={'purpose': 'x'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('unknown room', r.json()['error'])


# --- Phase E3.4: player quality-veto --------------------------------

    def _veto_state(self):
        # Two non-admin engineers under one director so _pick_reviewer_ids can
        # return a real pair for author 'ben'. Our test roster admin must be
        # director=None (self-reference leaks into the reviewer pool).
        roster = [
            {'id': 'maya', 'isAdmin': True, 'director': None},
            {'id': 'ben', 'role': 'engineer', 'director': 'robin'},
            {'id': 'ada', 'role': 'engineer', 'director': 'robin'},
            {'id': 'cora', 'role': 'engineer', 'director': 'robin'},
            {'id': 'robin', 'isDirector': True, 'director': None},
        ]
        agents = {
            'maya': {'id': 'maya', 'name': 'Maya', 'busy': True, 'offDuty': False, 'role': 'x',
                     'mailbox': []},
            'ben': {'id': 'ben', 'name': 'Ben', 'busy': False, 'offDuty': False, 'role': 'engineer',
                    'task': None, 'mailbox': []},
            'ada': {'id': 'ada', 'name': 'Ada', 'busy': False, 'offDuty': False, 'role': 'engineer',
                    'task': None, 'mailbox': []},
            'cora': {'id': 'cora', 'name': 'Cora', 'busy': False, 'offDuty': False, 'role': 'engineer',
                     'task': None, 'mailbox': []},
            'robin': {'id': 'robin', 'name': 'Robin', 'busy': True, 'offDuty': False, 'role': 'director',
                      'mailbox': []},
        }
        return {
            'agentRoster': roster,
            'agents': agents,
            'workQueue': [],
            'tasks': {
                'story-1': {'id': 'story-1', 'title': 'Build the checkout flow',
                            'room': 'pressoffice', 'instructions': 'implement it',
                            'projectLabel': 'storefront', 'taskType': 'code',
                            'assignedTo': 'ben', 'status': 'done',
                            'createdAt': 1000, 'goal': 'storefront'},
                'story-live': {'id': 'story-live', 'title': 'In-flight story',
                               'room': 'pressoffice', 'instructions': 'x',
                               'projectLabel': 'storefront', 'taskType': 'code',
                               'assignedTo': 'ada', 'status': 'working',
                               'createdAt': 2000, 'goal': 'storefront'},
            },
            'reports': [],
            'researchTopics': [],
        }

    def test_reject_reopens_done_story_to_needs_review_for_author(self):
        state = self._veto_state()
        # Seed a prior reviewer pair on the done gate -- a player veto should
        # re-verify with the SAME pair (they have the context on this story),
        # not cold strangers.
        state['tasks']['story-1']['_peerGate'] = {
            'reviewerIds': ['ada', 'cora'], 'approvals': 2, 'approvers': ['ada', 'cora'],
            'closed': True, 'enteredMs': 1000,
        }
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action'):
            c = TestClient(serve.app)
            r = c.post('/api/intent/story/story-1/reject', json={'reason': 'wrong currency'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['author'], 'ben')
        self.assertEqual(len(body['reviewers']), 2)
        # A veto re-verifies with the reviewers who already know this story.
        self.assertEqual(sorted(body['reviewers']), ['ada', 'cora'])
        # The story is re-gated (re-enters peer review), author preserved.
        task = saved['tasks']['story-1']
        self.assertEqual(task['status'], 'needs_review')
        self.assertEqual(task['assignedTo'], 'ben')
        gate = task['_peerGate']
        self.assertEqual(gate['approvals'], 0)
        self.assertEqual(sorted(gate['reviewerIds']), ['ada', 'cora'])
        # Two review subtasks were queued, pinned back to the author.
        reviews = [t for t in saved['workQueue'] if t.get('reviewOf') == 'story-1']
        self.assertEqual(len(reviews), 2)
        self.assertTrue(all(rv.get('reviewAuthorId') == 'ben' for rv in reviews))
        # The author was told the PLAYER sent their work back.
        author_mail = saved['agents']['ben']['mailbox']
        self.assertTrue(any(m.get('kind') == 'player_veto' for m in author_mail))

    def test_reject_live_story_is_conflict_not_mutated(self):
        state = self._veto_state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = TestClient(serve.app)
            r = c.post('/api/intent/story/story-live/reject', json={'reason': 'x'})
        self.assertEqual(r.status_code, 409)
        save.assert_not_called()

    def test_reject_unknown_story_is_404(self):
        state = self._veto_state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = TestClient(serve.app)
            r = c.post('/api/intent/story/nope/reject', json={})
        self.assertEqual(r.status_code, 404)
        save.assert_not_called()

    def test_reject_chains_passport_story_vetoed(self):
        state = self._veto_state()
        saved = {}
        logged = []
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action',
                                        side_effect=lambda *a, **k: logged.append((a, k))), \
             unittest.mock.patch.object(serve, '_append_passport_decision') as passport:
            c = TestClient(serve.app)
            r = c.post('/api/intent/story/story-1/reject', json={'reason': 'needs edge case'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(logged[0][0][1], 'story_vetoed')
        self.assertEqual(logged[0][0][2]['author'], 'ben')
        self.assertEqual(passport.call_args[0][0], 'story_vetoed')

# --- Phase E3.5: spike->triage (player promotes findings into real work) -----

    def _spike_state(self):
        # A done spike (findings on the note) + a working spike (not finished) +
        # a non-spike done task (nothing to promote).
        return {
            'agentRoster': [],
            'agents': {},
            'workQueue': [],
            'tasks': {
                'spike-done': {'id': 'spike-done', 'title': 'Can we use WebRTC?',
                               'room': 'pressoffice', 'taskType': 'spike',
                               'status': 'done', 'assignedTo': 'ada',
                               'goal': 'Evaluate WebRTC', 'createdAt': 1000,
                               'note': 'WebRTC works for 1:1 but not mesh. Recommend a relay server.'},
                'spike-live': {'id': 'spike-live', 'title': 'Still investigating auth',
                               'room': 'observatory', 'taskType': 'spike',
                               'status': 'working', 'assignedTo': 'ben', 'createdAt': 2000},
                'story-done': {'id': 'story-done', 'title': 'Build login', 'room': 'pressoffice',
                               'taskType': 'code', 'status': 'done', 'assignedTo': 'ben', 'createdAt': 3000},
            },
            'reports': [],
            'researchTopics': [],
        }

    def test_promote_done_spike_queues_deliverable_embedding_finding(self):
        state = self._spike_state()
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, '_append_passport_decision') as passport:
            c = TestClient(serve.app)
            r = c.post('/api/intent/spike/spike-done/promote', json={'taskType': 'code'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # A real deliverable was queued into pressoffice/code (both explicit
        # now -- taskType is required, not defaulted, see BoundedReviewEscalation's
        # sibling gap: PromoteSpikeTaskType below).
        item = body['queued']
        self.assertEqual(item['room'], 'pressoffice')
        self.assertEqual(item['taskType'], 'code')
        self.assertIn('WebRTC', item['title'])
        # The queued item inherits the spike's goal and embeds its written
        # finding in the instructions (the durable work brief).
        self.assertEqual(item['goal'], 'Evaluate WebRTC')
        self.assertEqual(len(saved.get('workQueue', [])), 1)
        queued = saved['workQueue'][0]
        self.assertEqual(queued['taskType'], 'code')
        self.assertIn('relay server', queued['instructions'])
        # The spike itself stays an immutable done record.
        self.assertEqual(saved['tasks']['spike-done']['status'], 'done')
        self.assertEqual(saved['tasks']['spike-done']['taskType'], 'spike')
        self.assertEqual(log.call_args[0][1], 'spike_promoted')
        self.assertEqual(passport.call_args[0][0], 'spike_promoted')

    def test_promote_working_spike_is_conflict(self):
        state = self._spike_state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = TestClient(serve.app)
            r = c.post('/api/intent/spike/spike-live/promote', json={})
        self.assertEqual(r.status_code, 409)
        save.assert_not_called()

    def test_promote_non_spike_is_conflict(self):
        state = self._spike_state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = TestClient(serve.app)
            r = c.post('/api/intent/spike/story-done/promote', json={})
        self.assertEqual(r.status_code, 409)
        save.assert_not_called()

    def test_promote_unknown_spike_is_404(self):
        state = self._spike_state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = TestClient(serve.app)
            r = c.post('/api/intent/spike/nope/promote', json={})
        self.assertEqual(r.status_code, 404)
        save.assert_not_called()

    def test_promote_with_library_path_embeds_the_real_findings_not_the_short_note(self):
        """Real gap caught live (2026-09-26): task.note is a deliberately
        short pointer ("see the Library entry just filed"); the FULL
        findings (source lists, CSVs, feasibility data) only ever lived in
        the Library file. Promoting a spike must pull the real content
        forward when its exact path was recorded, not just the pointer."""
        tmp = tempfile.mkdtemp(prefix='village-promote-lib-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, 'archive'), exist_ok=True)
        real_findings = ('# Spike: Can we use WebRTC?\n\nBy: ada\n\n'
                         'Source,URL,Basis\nlibwebrtc,https://webrtc.org,verified\n'
                         'Full real findings far more detailed than the short note.')
        with open(os.path.join(tmp, 'archive', '123-spike-spike-done.md'), 'w') as f:
            f.write(real_findings)
        state = self._spike_state()
        state['tasks']['spike-done']['libraryPath'] = 'archive/123-spike-spike-done.md'
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db', side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'LIBRARY_DIR', tmp), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, '_append_passport_decision'):
            c = TestClient(serve.app)
            r = c.post('/api/intent/spike/spike-done/promote', json={'taskType': 'code'})
        self.assertEqual(r.status_code, 200, r.text)
        queued = saved['workQueue'][0]
        self.assertIn('libwebrtc,https://webrtc.org,verified', queued['instructions'])
        self.assertIn('Full real findings far more detailed', queued['instructions'])

    def test_promote_falls_back_to_the_short_note_when_the_library_file_is_missing(self):
        # libraryPath recorded, but the file itself isn't there (deleted,
        # moved, or a stale path) -- must degrade to the note, not error out.
        state = self._spike_state()
        state['tasks']['spike-done']['libraryPath'] = 'archive/does-not-exist.md'
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db', side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, '_append_passport_decision'):
            c = TestClient(serve.app)
            r = c.post('/api/intent/spike/spike-done/promote', json={'taskType': 'code'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('relay server', saved['workQueue'][0]['instructions'])


# --- Publish: player-triggered push of released work ------------------------

    def _publish_state(self):
        return {
            'agentRoster': [],
            'agents': {},
            'workQueue': [],
            'tasks': {},
            'reports': [],
            'researchTopics': [],
        }

    def test_publish_stages_and_chains_passport(self):
        """The endpoint stages the produced surface and pushes to the configured
        private repo, chaining artifact_published into passport + action log.
        git/db/gh are all mocked so nothing leaves the machine under test."""
        state = self._publish_state()
        logged = []
        git_calls = []
        push_calls = []
        staged = ['projects/finger-drums/LOG.md', 'wiki/engineering/trust.md']
        with tempfile.TemporaryDirectory() as tmp, \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db', side_effect=lambda s: s), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'PUBLISH_REPO', 'testowner/test-repo'), \
             unittest.mock.patch.object(serve, 'PUBLISH_STAGING', os.path.join(tmp, 'staging')), \
             unittest.mock.patch.object(serve, '_stage_released_work', return_value=staged) as stage, \
             unittest.mock.patch.object(serve, '_write_publish_readme',
                                        side_effect=lambda s: os.makedirs(os.path.join(tmp, 'staging'), exist_ok=True)), \
             unittest.mock.patch.object(serve, '_run_git_sync', side_effect=lambda *a, **k: git_calls.append(a[1])), \
             unittest.mock.patch.object(serve, 'subprocess') as sub, \
             unittest.mock.patch.object(serve, 'log_action',
                                        side_effect=lambda *a, **k: logged.append((a, k))), \
             unittest.mock.patch.object(serve, '_append_passport_decision') as passport:
            # gh auth token + the push itself both succeed.
            def _run(cmd, **k):
                if cmd[:2] == ['gh', 'auth']:
                    return unittest.mock.Mock(stdout='gho_token')
                push_calls.append(cmd)
                return unittest.mock.Mock(returncode=0, stdout='done', stderr='')
            sub.run.side_effect = _run
            c = TestClient(serve.app)
            r = c.post('/api/intent/publish', json={})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['ok'], True)
        self.assertEqual(body['repo'], 'testowner/test-repo')
        self.assertEqual(body['entries'], 2)
        # The staging dir was git-initialized, committed, remote-added, and pushed.
        self.assertEqual(git_calls[0][0], 'init')
        self.assertIn(['add', '-A'], git_calls)
        self.assertIn(['commit', '-q', '-m', unittest.mock.ANY], git_calls)
        self.assertTrue(any(c[0] == 'remote' for c in git_calls))
        # The push itself goes direct to subprocess (longer timeout), and the
        # origin URL carries the gh token only transiently.
        self.assertTrue(any(c[0] == 'git' and 'push' in c for c in push_calls))
        self.assertTrue(any('HEAD:main' in c for c in push_calls))
        # Passport + action log chained.
        self.assertEqual(passport.call_args[0][0], 'artifact_published')
        self.assertEqual(logged[0][0][1], 'artifact_published')

    def test_publish_conflict_when_nothing_staged(self):
        """With no released projects/wiki, publish fails closed (409) and never
        touches git/gh."""
        state = self._publish_state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'PUBLISH_REPO', 'testowner/test-repo'), \
             unittest.mock.patch.object(serve, '_stage_released_work', return_value=[]), \
             unittest.mock.patch.object(serve, '_run_git_sync') as git, \
             unittest.mock.patch.object(serve, 'subprocess') as sub:
            c = TestClient(serve.app)
            r = c.post('/api/intent/publish', json={})
        self.assertEqual(r.status_code, 409)
        git.assert_not_called()
        sub.run.assert_not_called()

    def test_publish_failed_push_returns_502_not_ok(self):
        """A rejected push (nonzero exit from git) surfaces as a 502 and does NOT
        chain artifact_published -- a failed publish is not a shipped one."""
        state = self._publish_state()
        with tempfile.TemporaryDirectory() as tmp, \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'PUBLISH_REPO', 'testowner/test-repo'), \
             unittest.mock.patch.object(serve, 'PUBLISH_STAGING', os.path.join(tmp, 'staging')), \
             unittest.mock.patch.object(serve, '_stage_released_work',
                                        return_value=['projects/x']), \
             unittest.mock.patch.object(serve, '_write_publish_readme',
                                        side_effect=lambda s: os.makedirs(os.path.join(tmp, 'staging'), exist_ok=True)), \
             unittest.mock.patch.object(serve, '_run_git_sync', side_effect=lambda *a, **k: None), \
             unittest.mock.patch.object(serve, 'subprocess') as sub, \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, '_append_passport_decision') as passport:
            def _run(cmd, **k):
                if cmd[:2] == ['gh', 'auth']:
                    return unittest.mock.Mock(stdout='gho_token')
                return unittest.mock.Mock(returncode=1, stdout='', stderr='rejected: non-fast-forward')
            sub.run.side_effect = _run
            c = TestClient(serve.app)
            r = c.post('/api/intent/publish', json={})
        self.assertEqual(r.status_code, 502, r.text)
        self.assertIn('push rejected', r.json()['error'])
        # A failed publish must not be passport-chained or logged as shipped.
        log.assert_not_called()
        passport.assert_not_called()

    # --- Phase B: passport integrity watchdog ---------------------------------

    def _build_3block_passport(self):
        # A clean 3-block chain: two promoted FILES (real bytes on disk) plus
        # one DECISION. Returns (tmpdir, PASSPORT_PATH). Mirrors how
        # _append_passport / _append_passport_decision link blocks in serve.py.
        tmp = tempfile.mkdtemp(prefix='village-passport-verify-')
        lib = os.path.join(tmp, 'library')
        os.makedirs(lib, exist_ok=True)
        f1 = os.path.join(lib, 'trust.md')
        f2 = os.path.join(lib, 'policy.md')
        with open(f1, 'w') as f:
            f.write('trust is earned\n')
        with open(f2, 'w') as f:
            f.write('policy v2 - updated in place\n')
        p = {'version': 1, 'count': 3, 'head': None, 'blocks': []}
        h1 = serve._sha256_file(f1)
        p['blocks'].append({'index': 1, 'path': 'trust.md', 'sha256': h1,
                            'owner': 'alice', 'promotedBy': 'player', 'prev': None, 'ts': 1})
        p['head'] = h1
        b2 = {'index': 2, 'path': 'policy.md', 'sha256': serve._sha256_file(f2),
              'owner': 'bob', 'promotedBy': 'director', 'prev': p['head'],
              'ts': 2}
        p['blocks'].append(b2)
        h2 = serve._block_link_value(b2)
        p['head'] = h2
        b3 = {'index': 3, 'kind': 'product_released', 'actor': 'player',
              'payload': {'pid': 'p1'}, 'prev': p['head'], 'ts': 3}
        p['blocks'].append(b3)
        p['head'] = serve._block_link_value(b3)
        passport_path = os.path.join(lib, '.passport.json')
        with open(passport_path, 'w') as f:
            json.dump(p, f)
        return tmp, passport_path

    def test_passport_verify_ok_on_intact_chain(self):
        tmp, pp = self._build_3block_passport()
        try:
            # LIBRARY_DIR must be patched too -- _safe_library_path resolves
            # file blocks against serve.LIBRARY_DIR, not PASSPORT_PATH's dir.
            with unittest.mock.patch.object(serve, 'LIBRARY_DIR', os.path.dirname(pp)), \
                 unittest.mock.patch.object(serve, 'PASSPORT_PATH', pp), \
                 unittest.mock.patch.object(serve, 'verify_session', return_value=True):
                c = TestClient(serve.app)
                r = c.get('/api/intent/passport/verify')
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body['ok'], True)
            self.assertEqual(body['count'], 3)
            self.assertEqual(body['badBlocks'], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_passport_verify_catches_mutated_file(self):
        """Editing a promoted file's bytes AFTER it was chained breaks the
        re-hash -- the watchdog must report exactly which block went bad."""
        tmp, pp = self._build_3block_passport()
        try:
            lib = os.path.dirname(pp)
            with open(os.path.join(lib, 'policy.md'), 'w') as f:
                f.write('policy v3 - silently rewritten\n')
            with unittest.mock.patch.object(serve, 'LIBRARY_DIR', lib), \
                 unittest.mock.patch.object(serve, 'PASSPORT_PATH', pp):
                ver = serve.verify_passport()
            self.assertEqual(ver['ok'], False)
            self.assertEqual(ver['badBlocks'], [{'index': 2, 'path': 'policy.md',
                                                 'reason': 'file_mismatch'}])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_passport_verify_catches_broken_link(self):
        """Tampering with an earlier block (here a decision payload) severs the
        link to the next block -- reported as broken_link, not silently ok."""
        tmp, pp = self._build_3block_passport()
        try:
            with open(pp) as f:
                p = json.load(f)
            # Append a 4th (DECISION) block so the chain has a follower, then
            # alter the 3rd DECISION's payload. A clean hash chain cannot detect
            # tampering of the FINAL block via links (that needs an externally-
            # pinned head); adding a follower makes the severed link observable.
            b4 = {'index': 4, 'kind': 'library_write', 'actor': 'player',
                  'payload': {'path': 'village/x.md'}, 'prev': p['head'], 'ts': 4}
            p['blocks'].append(b4)
            p['count'] = 4
            p['head'] = serve._block_link_value(b4)
            # Mutate the 3rd (decision, so no file_mismatch masks the link check).
            p['blocks'][2]['payload']['pid'] = 'p2'
            with open(pp, 'w') as f:
                json.dump(p, f)
            with unittest.mock.patch.object(serve, 'LIBRARY_DIR', os.path.dirname(pp)), \
                 unittest.mock.patch.object(serve, 'PASSPORT_PATH', pp):
                ver = serve.verify_passport()
            self.assertEqual(ver['ok'], False)
            self.assertEqual(ver['badBlocks'][0]['reason'], 'broken_link')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PromoteSpikeTaskType(unittest.TestCase):
    """Real gap caught live (2026-09-26): promote-spike used to silently
    default a missing taskType to 'code' -- a real test promotion of a
    purely informational finding got promoted into taskType='code' anyway
    and burned 45+ review/fix cycles because no reviewer could approve
    'code' that didn't correspond to any real recommendation. taskType is
    now REQUIRED, and 'spike' is a real, valid choice for a non-actionable
    finding (stays out of the peer gate entirely)."""

    def _client(self):
        from fastapi.testclient import TestClient
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        return c

    def _state(self):
        return {
            'agentRoster': [], 'agents': {}, 'workQueue': [],
            'tasks': {'spike-done': {'id': 'spike-done', 'title': 'DreyX research',
                                     'room': 'observatory', 'taskType': 'spike',
                                     'status': 'done', 'assignedTo': 'ada',
                                     'goal': 'Investigate DreyX', 'createdAt': 1000,
                                     'note': 'Purely informational -- no concrete recommendation.'}},
            'reports': [], 'researchTopics': [],
        }

    def test_missing_task_type_is_rejected_not_defaulted(self):
        state = self._state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            r = self._client().post('/api/intent/spike/spike-done/promote', json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn('taskType is required', r.json()['error'])
        save.assert_not_called()

    def test_invalid_task_type_is_rejected(self):
        state = self._state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            r = self._client().post('/api/intent/spike/spike-done/promote', json={'taskType': 'nonsense'})
        self.assertEqual(r.status_code, 400)
        save.assert_not_called()

    def test_spike_task_type_builds_a_real_spike_not_a_deliverable(self):
        state = self._state()
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db', side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, '_append_passport_decision'):
            r = self._client().post('/api/intent/spike/spike-done/promote', json={'taskType': 'spike'})
        self.assertEqual(r.status_code, 200, r.text)
        queued = saved['workQueue'][0]
        self.assertEqual(queued['taskType'], 'spike')
        self.assertIn('budgetMs', queued)
        self.assertIn('Investigate further', queued['instructions'])
        # Never "pursue the recommendation into real work" -- that phrasing
        # presumes actionability a purely informational finding doesn't have.
        self.assertNotIn('Pursue the recommendation', queued['instructions'])

    def test_code_task_type_still_works_explicitly(self):
        state = self._state()
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db', side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, '_append_passport_decision'):
            r = self._client().post('/api/intent/spike/spike-done/promote', json={'taskType': 'code'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(saved['workQueue'][0]['taskType'], 'code')


class ChatEndpointAuth(unittest.TestCase):
    """/api/chat guards its own OpenRouter spend by EITHER a player session OR a
    valid agent key (the server's own assign-big-task planning loopback carries
    only X-Agent-Key). A request with neither must fail closed (401); each valid
    credential alone must pass."""

    def test_chat_requires_session_or_agent_key(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=False), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync') as model:
            c = TestClient(serve.app)
            r = c.post('/api/chat', json={'model': 'm', 'messages': [{'role': 'user', 'content': 'x'}], 'agentId': 'faye'})
        self.assertEqual(r.status_code, 401)
        model.assert_not_called()

    def test_chat_accepts_valid_agent_key_without_session(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        return_value={'choices': [{'message': {'content': 'ok'}}]}), \
             unittest.mock.patch.object(serve, 'log_action'):
            c = TestClient(serve.app)
            r = c.post('/api/chat', json={'model': 'm', 'messages': [{'role': 'user', 'content': 'x'}], 'agentId': 'faye'},
                       headers={'X-Agent-Key': 'real-agent-key'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['reply'], 'ok')

    def test_protected_endpoint_accepts_valid_agent_key_loopback(self):
        # Server content executors loopback to protected endpoints carrying only
        # an agent key (no session cookie). A valid stored key must pass the
        # session middleware so the pipeline/execute/library loopbacks work;
        # with no session AND no key it must still 401.
        c = TestClient(serve.app)
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, '_valid_agent_key_presented', return_value=True):
            r = c.get('/api/sim/status')
        self.assertEqual(r.status_code, 200, 'a valid agent key is a credential for a loopback')
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, '_valid_agent_key_presented', return_value=False):
            r2 = c.get('/api/sim/status')
        self.assertEqual(r2.status_code, 401, 'no session and no key fails closed')

    def test_decisions_and_player_inbox_require_credentials(self):
        # Regression (2026-09-28): /api/decisions (JEV decision tape) and
        # /api/player-inbox (read + the responding WRITE) were absent from
        # AUTH_PROTECTED_PREFIXES, so anonymous callers could read the tape and
        # -- worse -- the respond endpoint could mutate state with no auth at
        # all. The prefix list must now bounce all three without credentials.
        c = TestClient(serve.app)
        for path in ('/api/decisions', '/api/player-inbox', '/api/player-inbox/ask-1/respond'):
            with unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
                 unittest.mock.patch.object(serve, '_valid_agent_key_presented', return_value=False):
                r = c.get(path) if 'respond' not in path else c.post(path, json={'answer': 'x'})
            self.assertEqual(r.status_code, 401, f'{path} must require a credential')

    def test_player_inbox_respond_rejects_valid_agent_key(self):
        # The respond endpoint is PLAYER-ONLY: a valid agent key must NOT be able
        # to answer an awaiting question, because doing so would let an agent
        # unblock its own stalled work around the human-in-the-loop gate.
        c = TestClient(serve.app)
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, '_valid_agent_key_presented', return_value=True):
            r = c.post('/api/player-inbox/ask-1/respond', json={'answer': 'x'})
        self.assertEqual(r.status_code, 401, 'an agent key is not a player credential here')

    def test_high_tier_chat_call_accrues_monthly_budget_exactly_once(self):
        # Regression (2026-09-28): the /api/chat choke point accrued a high-tier
        # call to the monthly budget TWICE via two duplicate blocks, so a $2/mo
        # allowance was exhausted in half the intended calls. A single high-tier
        # call must land exactly once in the kv_spend high-tier monthly series.
        calls = []
        def fake_accrue(cost):
            calls.append(cost)
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, '_high_tier_slug', return_value='expensive-model'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        return_value={'choices': [{'message': {'content': 'ok'}}],
                                                      'usage': {'cost': 0.25}}), \
             unittest.mock.patch.object(serve, '_accrue_spend'), \
             unittest.mock.patch.object(serve, '_accrue_high_tier_spend', side_effect=fake_accrue), \
             unittest.mock.patch.object(serve, 'log_action'):
            c = TestClient(serve.app)
            r = c.post('/api/chat', json={'model': 'expensive-model',
                                          'messages': [{'role': 'user', 'content': 'x'}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(calls), 1, 'a high-tier call accrues to the monthly budget exactly once')
        self.assertEqual(calls, [0.25])


class OperationalGuardrails(unittest.TestCase):
    def test_backup_creates_rotated_snapshots(self):
        # _backup_village_db must snapshot the DB into DB_BACKUP_DIR with a .bak
        # extension and prune to DB_BACKUP_KEEP newest. Point both paths at a
        # throwaway dir so the REAL village.db is never touched.
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, 'the.db')
            bakdir = os.path.join(td, 'backups')
            with sqlite3.connect(src) as c:
                c.execute('CREATE TABLE t (x INTEGER)')
                c.execute('INSERT INTO t VALUES (1)')
            with unittest.mock.patch.object(serve, 'DB_BACKUP_DIR', bakdir), \
                 unittest.mock.patch.object(serve, 'DB_BACKUP_KEEP', 2), \
                 unittest.mock.patch.object(serve, 'DB_PATH', src):
                serve._backup_village_db()
                serve._backup_village_db()
                serve._backup_village_db()
            snaps = sorted(os.listdir(bakdir))
            self.assertEqual(len(snaps), 2, 'pruned to DB_BACKUP_KEEP')
            self.assertTrue(all(f.endswith('.bak') for f in snaps))
            # newest snapshot must contain the same row (a real backup).
            newest = os.path.join(bakdir, snaps[-1])
            with sqlite3.connect(newest) as c:
                self.assertEqual(c.execute('SELECT x FROM t').fetchone()[0], 1)

    def test_idle_shutdown_goes_dormant_when_exceeded(self):
        # Sleep-not-die: once no request has arrived for >= _MAX_IDLE_MINUTES,
        # the watcher must go DORMANT (village paused, process stays bound) --
        # NOT exit, so a remote request can still wake it. Use a tiny poll so
        # the test returns fast; simulate "no request for a while" by backdating
        # _LAST_REQUEST_TIME. Runs to a timeout so the infinite poll loop exits.
        old_last = serve._LAST_REQUEST_TIME
        old_poll = serve._dormant()  # preserve flag across runs
        serve._set_dormant(False)
        went_dormant = False
        try:
            serve._LAST_REQUEST_TIME = time.time() - (5 * 60)
            with unittest.mock.patch.object(serve, '_MAX_IDLE_MINUTES', 1):
                async def run():
                    # Let the loop poll once, then stop it.
                    import asyncio as _a
                    task = _a.create_task(serve._idle_shutdown_loop(poll_s=0.01))
                    try:
                        await _a.wait_for(task, timeout=0.5)
                    except _a.TimeoutError:
                        task.cancel()
                        try:
                            await task
                        except (_a.CancelledError, Exception):
                            pass
                asyncio.run(run())
                went_dormant = serve._dormant()
        finally:
            serve._LAST_REQUEST_TIME = old_last
            serve._set_dormant(old_poll)
        self.assertTrue(went_dormant, 'idle watcher must put the village dormant, not exit')


if __name__ == '__main__':
    unittest.main(verbosity=2)
