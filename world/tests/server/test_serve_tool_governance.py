"""Split part of test_serve.py (serve.py behavior tests)."""
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
import sim  # noqa: E402
from test_serve_helpers import setUpModule, tearDownModule  # noqa: E402


class ExternalWriteOnce(unittest.TestCase):
    """The retry ledger must replay a completed write and refuse a concurrent
    duplicate instead of starting a second side effect."""

    def test_completed_write_replays(self):
        calls = []
        key = f'replay-test-{serve.time.time()}'

        def fn():
            calls.append(1)
            return {'runId': 'r1'}, None

        first, err, replayed = serve.external_write_once('test', key, fn)
        self.assertIsNone(err)
        self.assertFalse(replayed)
        second, err, replayed = serve.external_write_once('test', key, fn)
        self.assertIsNone(err)
        self.assertTrue(replayed)
        self.assertEqual(second, first)
        self.assertEqual(len(calls), 1)

    def test_failed_write_releases_the_key(self):
        key = f'fail-test-{serve.time.time()}'
        first, err, _ = serve.external_write_once(
            'test', key, lambda: (None, 'boom'))
        self.assertEqual(err, 'boom')
        second, err, replayed = serve.external_write_once(
            'test', key, lambda: ({'ok': True}, None))
        self.assertIsNone(err)
        self.assertFalse(replayed)
        self.assertEqual(second, {'ok': True})


class ToolConversationPolicy(unittest.TestCase):
    def test_commit_tool_is_refused_without_approval(self):
        execute = unittest.mock.Mock(return_value='EXECUTED')
        responses = [
            {'choices': [{'message': {'tool_calls': [
                {'id': 'c1', 'function': {'name': 'create_calendar_event', 'arguments': '{}'}}]}}]},
            {'choices': [{'message': {'content': 'done'}}]},
        ]
        with unittest.mock.patch.object(serve, '_post_openrouter_raw', side_effect=responses), \
             unittest.mock.patch.object(serve, '_accrue_spend'):
            text, transcript = serve._call_agent_tool_loop(
                'm', [{'role': 'user', 'content': 'go'}], [], execute,
                max_iterations=2, return_transcript=True,
                conversation_policy=lambda name: name != 'create_calendar_event')
        self.assertEqual(text, 'done')
        execute.assert_not_called()
        tool_messages = [m['content'] for m in transcript if m.get('role') == 'tool']
        self.assertTrue(any('needs player approval' in m for m in tool_messages))

    def test_research_tool_is_allowed(self):
        execute = unittest.mock.Mock(return_value='EXECUTED')
        responses = [
            {'choices': [{'message': {'tool_calls': [
                {'id': 'c1', 'function': {'name': 'read_library_file', 'arguments': '{}'}}]}}]},
            {'choices': [{'message': {'content': 'done'}}]},
        ]
        with unittest.mock.patch.object(serve, '_post_openrouter_raw', side_effect=responses), \
             unittest.mock.patch.object(serve, '_accrue_spend'):
            serve._call_agent_tool_loop(
                'm', [{'role': 'user', 'content': 'go'}], [], execute,
                max_iterations=2, conversation_policy=lambda name: True)
        execute.assert_called_once()


class IdempotentToolCall(unittest.TestCase):
    def test_commit_tool_replays_and_readonly_bypasses(self):
        calls = []
        key = f'idem-{serve.time.time()}'

        def raw(name, args):
            calls.append(name)
            return 'ok'

        first = serve.idempotent_tool_call('a', key, raw, 'create_calendar_event', {'x': 1})
        second = serve.idempotent_tool_call('a', key, raw, 'create_calendar_event', {'x': 1})
        self.assertEqual(first, 'ok')
        self.assertIn('replayed from requestId', second)
        self.assertEqual(calls, ['create_calendar_event'])
        serve.idempotent_tool_call('a', key, raw, 'read_library_file', {})
        self.assertEqual(calls, ['create_calendar_event', 'read_library_file'])

    def test_error_output_is_not_cached(self):
        calls = []
        key = f'idem-err-{serve.time.time()}'

        def raw(name, args):
            calls.append(name)
            return 'Could not create the event: boom'

        serve.idempotent_tool_call('a', key, raw, 'create_calendar_event', {'x': 2})
        serve.idempotent_tool_call('a', key, raw, 'create_calendar_event', {'x': 2})
        self.assertEqual(len(calls), 2)


class ToolLoopCheckpoints(unittest.TestCase):
    def test_callbacks_see_pending_then_completed(self):
        seen = {}
        tool_calls = [{'id': 'c1', 'function': {'name': 'read_library_file',
                                                 'arguments': '{}'}}]
        responses = [
            {'choices': [{'message': {'tool_calls': tool_calls}}]},
            {'choices': [{'message': {'content': 'done'}}]},
        ]
        with unittest.mock.patch.object(serve, '_post_openrouter_raw', side_effect=responses), \
             unittest.mock.patch.object(serve, '_accrue_spend'):
            serve._call_agent_tool_loop(
                'm', [{'role': 'user', 'content': 'go'}], [], lambda n, a: 'ok',
                max_iterations=2,
                on_before_tools=lambda calls, msgs: seen.update(before=calls, before_msgs=len(msgs)),
                on_after_tools=lambda msgs: seen.update(after_msgs=len(msgs)))
        self.assertEqual(seen['before'], tool_calls)
        self.assertEqual(seen['before_msgs'], 2)
        self.assertEqual(seen['after_msgs'], 3)


class TaskCheckpoint(unittest.TestCase):
    def test_round_trip_and_clear(self):
        key = f'task-cp-{serve.time.time()}'
        self.assertTrue(serve.save_task_checkpoint(key, 'planned', {'plan': 'P'}))
        cp = serve.load_task_checkpoint(key)
        self.assertEqual(cp.get('phase'), 'planned')
        self.assertEqual(cp.get('plan'), 'P')
        self.assertTrue(serve.clear_task_checkpoint(key))
        self.assertIsNone(serve.load_task_checkpoint(key))


class CollectorCheckpoint(unittest.TestCase):
    def test_checkpoint_round_trip(self):
        key = f'cp-test-{serve.time.time()}'
        self.assertTrue(serve.set_collector_checkpoint(key, {'ts': 123, 'topic': 'x'}))
        cp = serve.get_collector_checkpoint(key)
        self.assertEqual(cp.get('ts'), 123)
        self.assertEqual(cp.get('topic'), 'x')
        self.assertIn('checkpoint_at', cp)

    def test_missing_checkpoint_is_none(self):
        self.assertIsNone(serve.get_collector_checkpoint('no-such-collector-xyz'))


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
    """Stigmergic trail reinforcement/decay, ported from real ant
    pheromone-trail biology: search_library used to rank purely by file
    mtime, so a file read/cited 40 times but written 2 weeks ago always lost
    to one written 2 minutes ago and never read at all. Usage lives in its
    own sidecar file (LIBRARY_USAGE_PATH), fully isolated per test here."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-library-trail-')
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
        # Gap: the top-ranked match isn't
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
            result = asyncio.run(serve.read_library_file('found.md', None))
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
    # date-aware incremental research -- Last-Modified is
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

    def test_complete_block_verifies_at_the_model_chokepoint(self):
        wrapped, _nonce, _tag, instruction = serve.wrap_external_content(
            'hello world', 'a test page')
        self.assertTrue(serve.verify_external_boundaries(wrapped))
        self.assertTrue(serve.verify_external_boundaries(instruction))
        self.assertTrue(serve._messages_pass_boundary_check(
            [{'role': 'user', 'content': instruction + '\n\n' + wrapped}]))

    def test_tampered_complete_block_is_refused(self):
        wrapped, _nonce, _tag, _instruction = serve.wrap_external_content(
            'hello world', 'a test page')
        tampered = wrapped.replace('hello world', 'hello WORLD (tampered)')
        self.assertFalse(serve.verify_external_boundaries(tampered))
        self.assertFalse(serve._messages_pass_boundary_check(
            [{'role': 'user', 'content': tampered}]))


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

    def test_sublow_ceiling_defaults_to_the_players_cap(self):
        # The sublow ceiling IS the band's definition (cheapest coherent
        # model for test scenarios at scale, hard-bounded) -- shipped
        # default is the player's stated $0.09/M cap, not off.
        self.assertEqual(serve.SUBLOW_TIER_MAX_PRICE_USD, 0.09)

    def test_sublow_accessor_never_falls_back_to_low(self):
        # No sublow row in model_tiers (fresh DB, or nothing qualified under
        # the ceiling) must return None, never the low tier: sublow-scale
        # work running at ~5x the ceiling is exactly what the ceiling
        # exists to prevent. Callers refuse/503 on a missing pick.
        def fake_slug(band):
            return {'low': 'low-slug'}.get(band)
        with unittest.mock.patch.object(serve, '_model_tier_slug', side_effect=fake_slug):
            self.assertIsNone(serve._sublow_tier_slug())
        with unittest.mock.patch.object(serve, '_model_tier_slug', return_value='x'):
            self.assertEqual(serve._sublow_tier_slug(), 'x')

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

    def test_includes_models_with_mandatory_reasoning(self):
        # Used to be excluded outright ("can't be turned off"), which hid
        # z-ai/glm-5.3-flash -- best value in its class -- from every pool.
        # A mandatory model rejects reasoning={'enabled': False} with a 400,
        # but answers fine with reasoning CAPPED, which is the recovery
        # _openrouter_chat_post performs; the live verify probe remains the
        # gate that keeps a genuinely unusable mandatory model out.
        buckets = serve._bucket_models_by_price([self._model('vendor/reasoner', 0.00000005, 0.00000005, reasoning={'mandatory': True})])
        self.assertEqual(len(buckets['low']), 1)
        self.assertEqual(buckets['low'][0]['id'], 'vendor/reasoner')

    def test_includes_models_with_reasoning_enabled_by_default(self):
        # default_enabled used to bar a model from the pools: the tight token
        # budget could be swallowed by hidden reasoning (the mid-tier null
        # content incident). The call layer now always sends an explicit
        # `reasoning` override (_call_openrouter_once_sync and
        # _post_openrouter_raw disable it for tiers that don't read reasoning
        # output, cap it for tiers that do), so the override -- not the pool
        # filter -- is the safeguard. Keeping these models hidden was hiding
        # DeepSeek V4 Flash Vision (76.7 MMMU at $0.86/M) from the vision
        # band, so default_enabled models are eligible again.
        buckets = serve._bucket_models_by_price([self._model('vendor/reasoner-default-on', 0.000001, 0.000001, reasoning={'mandatory': False, 'default_enabled': True})])
        self.assertEqual(len(buckets['mid']), 1)
        self.assertEqual(buckets['mid'][0]['id'], 'vendor/reasoner-default-on')

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


class OpenrouterChatPost(unittest.TestCase):
    """The mandatory-reasoning recovery in _openrouter_chat_post: a 400
    "Reasoning is mandatory" must be retried ONCE with reasoning capped to
    half the request budget (the shape that answers fine), and every other
    failure must propagate unchanged."""

    @staticmethod
    def _mandatory_400():
        body = json.dumps({'error': {'message': 'Reasoning is mandatory for this endpoint and cannot be disabled.',
                                     'code': 400}}).encode()
        return urllib.error.HTTPError('https://openrouter.ai/api/v1/chat/completions', 400,
                                      'Bad Request', {}, io.BytesIO(body))

    def _ok_response(self):
        return json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}).encode()

    def test_retries_mandatory_400_with_capped_reasoning(self):
        calls = []

        def fake_urlopen(req, timeout=30):
            calls.append(json.loads(req.data.decode()))
            if len(calls) == 1:
                raise self._mandatory_400()
            return self._ok_response()

        with unittest.mock.patch.object(serve, '_urlopen_with_resilience', fake_urlopen):
            result = serve._openrouter_chat_post(
                {'model': 'z-ai/glm-5.3-flash',
                 'messages': [{'role': 'user', 'content': 'hi'}],
                 'max_tokens': 64, 'reasoning': {'enabled': False}})
        self.assertEqual(result['choices'][0]['message']['content'], 'ok')
        self.assertEqual(len(calls), 2)
        # First attempt kept the caller's disabled override; the retry capped it.
        self.assertEqual(calls[0]['reasoning'], {'enabled': False})
        self.assertEqual(calls[1]['reasoning'], {'max_tokens': 32})

    def test_non_mandatory_400_propagates(self):
        body = json.dumps({'error': {'message': 'invalid model', 'code': 400}}).encode()
        err = urllib.error.HTTPError('https://openrouter.ai/api/v1/chat/completions', 400,
                                     'Bad Request', {}, io.BytesIO(body))
        with unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                        unittest.mock.Mock(side_effect=err)):
            with self.assertRaises(urllib.error.HTTPError):
                serve._openrouter_chat_post(
                    {'model': 'x/y', 'messages': [], 'max_tokens': 64, 'reasoning': {'enabled': False}})


class ModelBenchmarkScores(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        serve.init_db()

    def setUp(self):
        # Real cleanup, not mocked -- these tests write to the real
        # think_tank.db model_benchmark_scores table, same reasoning as the
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
        # model_benchmark_scores rename in init_db() -- a think_tank.db
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
    # JEV-gated tier escalation: everything defaults to LOW; coding
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
        # (CREATE TABLE IF NOT EXISTS), safe against the real think_tank.db.
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
    # own room restriction (extended to cover the
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
    # (real think_tank.db round-trips, real cleanup), not the Jev approval
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
