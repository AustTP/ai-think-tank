"""Spike tool-loop, Treg, library-review, and character-tool tests."""

import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import content  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    # Hermetic DB. These tests use external_write_once with FIXED request ids
    # (calendar-test-1, apify-success-test, ...), so against a shared live DB
    # the second run REPLAYS the first run's cached result: _google_call and
    # _apify_budget_exceeded are never invoked and the assertions fail. A
    # throwaway DB also stops the suite writing replay rows into production.
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-spike-content-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _snapshot():
    return {
        'agentRoster': [{'id': 'cora', 'name': 'Cora'}],
        'agents': {'cora': {'name': 'Cora'}},
    }


def _completion(text):
    return {'choices': [{'message': {'content': text}}], 'usage': {'cost': 0.0}}

class SpikeContent:
    """Mock helpers shared with the pipeline test class (not collected)."""
    __test__ = False

    def _store(self):
        seen = {}
        patcher = unittest.mock.patch('sim._store_content_result',
                                      lambda task_id, result: seen.__setitem__(task_id, result))
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def _common_mocks(self, tavily=True):
        # Real network boundaries every test must stub: agent-key lookup, the
        # model tiers, and the library-file write (a real _http_json call).
        # The JEV tier gate is stubbed deterministically to 'mid'
        # so a spike (an investigation) exercises the mid path -- and the
        # underlying tier slugs are pinned so no real model resolution happens.
        patchers = [
            unittest.mock.patch.object(serve, 'get_or_create_agent_key', return_value='key-123'),
            unittest.mock.patch.object(serve, '_coding_tier_slug', return_value=None),
            unittest.mock.patch.object(serve, '_low_tier_slug', return_value='low-tier-slug'),
            unittest.mock.patch.object(serve, '_mid_tier_slug', return_value='mid-tier-slug'),
            unittest.mock.patch.object(serve, '_reasoning_tier_slug', return_value='reasoning-tier-slug'),
            unittest.mock.patch.object(serve, '_tier_gate_decider',
                                       lambda instructions, criteria: ('mid', 0.9)),
            unittest.mock.patch.object(serve, 'TAVILY_API_KEY', 'fake-tavily-key' if tavily else ''),
            unittest.mock.patch.object(serve, '_http_json', return_value={'ok': True}),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)


class OneStrikePerTool(unittest.TestCase):
    """A real policy denial (Jev said no) must never be retried -- the SAME
    tool, called again, is refused locally with no second network call."""

    def test_struck_browse_page_is_refused_without_a_second_network_call(self):
        struck = set()
        executor = serve._make_web_tools_executor('cora', 'key-123', struck_tools=struck)
        executor.observe_urls('https://x.com')
        with unittest.mock.patch.object(serve, '_http_json',
                                        return_value={'allowed': False, 'reason': 'not approved'}) as http_mock:
            first = executor('browse_page', {'url': 'https://x.com', 'purpose': 'p'})
        self.assertIn('not approved', first)
        self.assertIn('browse_page', struck)
        second = executor('browse_page', {'url': 'https://x.com/other', 'purpose': 'p'})
        self.assertIn('already blocked', second.lower())
        http_mock.assert_called_once()  # the second call never touched the network at all

    def test_transient_fetch_error_does_not_strike(self):
        struck = set()
        executor = serve._make_web_tools_executor('cora', 'key-123', struck_tools=struck)
        executor.observe_urls('https://x.com')
        with unittest.mock.patch.object(serve, '_http_json',
                                        return_value={'allowed': True, 'error': 'timeout'}):
            executor('browse_page', {'url': 'https://x.com', 'purpose': 'p'})
        self.assertNotIn('browse_page', struck)

    def test_struck_execute_script_is_refused_without_a_second_network_call(self):
        struck = set()
        executor = content._make_spike_sandbox_executor('cora', 'key-123', 'spike-1', struck_tools=struck)
        with unittest.mock.patch.object(serve, '_http_json',
                                        return_value={'allowed': False, 'reason': 'looked risky'}) as http_mock:
            executor('execute_script', {'command': 'rm -rf /', 'purpose': 'p'})
        self.assertIn('execute_script', struck)
        second = executor('execute_script', {'command': 'rm -rf /tmp', 'purpose': 'p'})
        self.assertIn('already blocked', second.lower())
        http_mock.assert_called_once()

    def test_strikes_are_per_tool_not_global(self):
        # A browse_page strike must NOT block execute_script -- one-strike
        # is per SERVICE/tool (matching the framework's own naming), not a blanket
        # "something failed, stop everything." They share one struck set
        # (spent from the same iteration ceiling) but track independently.
        self._common_mocks = SpikeContent._common_mocks.__get__(self)
        self._store = SpikeContent._store.__get__(self)
        self._common_mocks()
        self._store()
        task = {'id': 'spike-13', 'title': 'Test', 'budgetMs': 60000}
        captured = {}

        def fake_loop(model, messages, tools, execute_tool, **kwargs):
            captured['execute_tool'] = execute_tool
            return ('done', messages)

        with unittest.mock.patch.object(serve, '_call_agent_tool_loop', side_effect=fake_loop), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]), \
             unittest.mock.patch.object(serve, '_http_json',
                                         return_value={'allowed': False, 'reason': 'denied'}):
            content._run_spike_content(_snapshot(), 'cora', task)
            execute_tool = captured['execute_tool']
            execute_tool('browse_page', {'url': 'https://x.com', 'purpose': 'p'})
            # First execute_script attempt -- not yet struck, must actually
            # try (and this mocked run gets blocked too, striking IT now).
            first = execute_tool('execute_script', {'command': 'ls', 'purpose': 'p'})
            self.assertIn('command blocked', first.lower())
            # Second execute_script attempt -- NOW struck, refused locally.
            second = execute_tool('execute_script', {'command': 'ls', 'purpose': 'p'})
        self.assertIn('already blocked', second.lower())

class ReflectionAndReplan(unittest.TestCase):
    """The EXECUTE budget is chunked into rounds with a cheap confidence
    check between them, never exceeding the original total iteration
    ceiling."""

    def test_settling_in_the_first_round_never_triggers_reflection(self):
        with unittest.mock.patch.object(serve, '_call_agent_tool_loop',
                                        return_value=('done', [{'role': 'system', 'content': 's'}])) as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync') as oc:
            text, transcript = content._run_spike_tool_loop_with_reflection(
                'mid', 'reasoning', [{'role': 'system', 'content': 's'}], [], lambda n, a: 'ok',
                total_iterations=18, max_tokens=900, force_first_tool='search_web')
        self.assertEqual(text, 'done')
        self.assertEqual(loop.call_count, 1)
        oc.assert_not_called()  # no reflection call when it settled immediately

    def test_total_iterations_across_all_rounds_never_exceeds_the_ceiling(self):
        seen_max_iterations = []

        def fake_loop(model, messages, tools, execute_tool, **kwargs):
            seen_max_iterations.append(kwargs['max_iterations'])
            messages = messages + [{'role': 'assistant', 'tool_calls': [{'id': 'c', 'function': {'name': 'x', 'arguments': '{}'}}]},
                                    {'role': 'tool', 'tool_call_id': 'c', 'content': 'progress'}]
            return (None, messages)  # never settles -> keep going until budget exhausted

        with unittest.mock.patch.object(serve, '_call_agent_tool_loop', side_effect=fake_loop), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync', return_value=_completion('{"confidence": 0.9, "note": "on track"}')):
            content._run_spike_tool_loop_with_reflection(
                'mid', 'reasoning', [{'role': 'system', 'content': 's'}], [], lambda n, a: 'ok',
                total_iterations=18, max_tokens=900, force_first_tool='search_web')
        self.assertEqual(sum(seen_max_iterations), 18)
        self.assertEqual(seen_max_iterations, [4, 4, 4, 4, 2])

    def test_low_confidence_injects_a_nudge_before_the_next_round(self):
        rounds = {'n': 0}

        def fake_loop(model, messages, tools, execute_tool, **kwargs):
            rounds['n'] += 1
            if rounds['n'] == 1:
                grown = messages + [{'role': 'assistant', 'content': 'progress note'}]
                return (None, grown)
            return ('final answer', messages)

        with unittest.mock.patch.object(serve, '_call_agent_tool_loop', side_effect=fake_loop), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        return_value=_completion('{"confidence": 0.2, "note": "try a different site"}')):
            text, transcript = content._run_spike_tool_loop_with_reflection(
                'mid', 'reasoning', [{'role': 'system', 'content': 's'}], [], lambda n, a: 'ok',
                total_iterations=18, max_tokens=900, force_first_tool='search_web')
        self.assertEqual(text, 'final answer')
        nudge = next((m for m in transcript if 'try a different site' in m.get('content', '')), None)
        self.assertIsNotNone(nudge)

    def test_high_confidence_skips_the_nudge(self):
        rounds = {'n': 0}

        def fake_loop(model, messages, tools, execute_tool, **kwargs):
            rounds['n'] += 1
            if rounds['n'] == 1:
                grown = messages + [{'role': 'assistant', 'content': 'progress note'}]
                return (None, grown)
            return ('final answer', messages)

        with unittest.mock.patch.object(serve, '_call_agent_tool_loop', side_effect=fake_loop), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        return_value=_completion('{"confidence": 0.9, "note": "on track"}')):
            text, transcript = content._run_spike_tool_loop_with_reflection(
                'mid', 'reasoning', [{'role': 'system', 'content': 's'}], [], lambda n, a: 'ok',
                total_iterations=18, max_tokens=900, force_first_tool='search_web')
        # round 1 grew messages to length 2; round 2's fake passes that
        # through unchanged -- the point is no nudge got appended on top.
        self.assertEqual(len(transcript), 2)
        self.assertFalse(any('Self-check' in m.get('content', '') for m in transcript))

    def test_stalled_round_with_zero_progress_bails_without_reflecting(self):
        with unittest.mock.patch.object(serve, '_call_agent_tool_loop',
                                        return_value=(None, [{'role': 'system', 'content': 's'}])) as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync') as oc:
            content._run_spike_tool_loop_with_reflection(
                'mid', 'reasoning', [{'role': 'system', 'content': 's'}], [], lambda n, a: 'ok',
                total_iterations=18, max_tokens=900, force_first_tool='search_web')
        self.assertEqual(loop.call_count, 1)  # zero progress -> stop, don't waste a reflection call
        oc.assert_not_called()

    def test_parse_reflection_handles_prose_wrapped_json_and_malformed_text(self):
        self.assertEqual(content._parse_reflection('{"confidence": 0.3, "note": "x"}'), (0.3, 'x'))
        self.assertEqual(content._parse_reflection('Sure, here it is: {"confidence": 0.7, "note": "y"} thanks'), (0.7, 'y'))
        self.assertEqual(content._parse_reflection('not json at all'), (1.0, ''))
        self.assertEqual(content._parse_reflection(''), (1.0, ''))
        self.assertEqual(content._parse_reflection('{"confidence": 5.0, "note": "x"}'), (1.0, 'x'))  # out of range -> default
        # A JSON object that fails mid-parse (unclosed brace) hits the
        # except branch and falls back to the safe default.
        self.assertEqual(content._parse_reflection('{oops'), (1.0, ''))

class LibraryReviewTools(unittest.TestCase):
    """search_library / read_library_file: the inverse of
    promote-spike's fix -- a spike reviewing another team's real completed
    work, instead of guessing at it, needs a way to actually search and
    read the Library. In-process, mirrors _library_search_matches /
    _safe_library_path exactly (the same functions the real
    /api/library/search and /api/library/file endpoints use)."""

    def setUp(self):
        # log_action is a real DB write (added after a live burn-in, see
        # _make_library_tools_executor's own docstring) -- mock it here so
        # these direct-executor tests never touch a real DB.
        patcher = unittest.mock.patch.object(serve, 'log_action')
        self.log_action = patcher.start()
        self.addCleanup(patcher.stop)

    def test_search_library_formats_real_matches(self):
        matches = [{'path': 'archive/1-spike-x.md', 'snippet': 'WebRTC works for 1:1...',
                   'modified': 1.0, 'size': 240}]
        executor = content._make_library_tools_executor('cora')
        with unittest.mock.patch.object(serve, '_library_search_matches', return_value=matches) as search, \
             unittest.mock.patch.object(serve, '_requester_villages', return_value={'main'}):
            out = executor('search_library', {'query': 'webrtc'})
        search.assert_called_once_with('webrtc', allowed={'main'})
        self.assertIn('archive/1-spike-x.md', out)
        self.assertIn('240 bytes', out)
        self.assertIn('WebRTC works for 1:1', out)
        self.log_action.assert_called_once_with('cora', 'search_library', {'query': 'webrtc', 'matches': 1}, authorized=True)

    def test_search_library_empty_query_is_rejected_without_a_call(self):
        executor = content._make_library_tools_executor('cora')
        with unittest.mock.patch.object(serve, '_library_search_matches') as search:
            out = executor('search_library', {'query': '  '})
        search.assert_not_called()
        self.assertIn('required', out)
        self.log_action.assert_not_called()

    def test_search_library_no_matches_says_so_plainly(self):
        executor = content._make_library_tools_executor('cora')
        with unittest.mock.patch.object(serve, '_library_search_matches', return_value=[]):
            out = executor('search_library', {'query': 'nonexistent thing'})
        self.assertIn('No Library matches', out)
        self.assertIn('no internal record', out.lower())
        self.log_action.assert_called_once_with('cora', 'search_library', {'query': 'nonexistent thing', 'matches': 0}, authorized=True)

    def test_read_library_file_returns_real_content_and_records_a_trail_read(self):
        tmp = tempfile.mkdtemp(prefix='think tank-lib-tool-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with open(os.path.join(tmp, 'finding.md'), 'w') as f:
            f.write('Real findings from Team A.')
        executor = content._make_library_tools_executor('cora')
        with unittest.mock.patch.object(serve, '_safe_library_path',
                                        return_value=os.path.join(tmp, 'finding.md')), \
             unittest.mock.patch.object(serve, 'record_library_read') as record:
            out = executor('read_library_file', {'path': 'finding.md'})
        self.assertEqual(out, 'Real findings from Team A.')
        record.assert_called_once_with('finding.md')
        self.log_action.assert_called_once_with('cora', 'read_library_file', {'path': 'finding.md', 'found': True, 'offset': 0, 'bytes': 26}, authorized=True)

    def test_read_library_file_offset_continues_past_the_20k_cap(self):
        # Confirmed gap (2026-10-12): the old hard 20k read had no offset, so a
        # spike could never see past byte 20k of a large file -- deep source
        # reads kept coming back truncated with whole topics missing. Offset
        # must resume exactly where the previous window ended.
        tmp = tempfile.mkdtemp(prefix='think-tank-lib-tool-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, 'big.md')
        payload = 'A' * 25_000
        with open(path, 'w') as f:
            f.write(payload)
        executor = content._make_library_tools_executor('cora')
        with unittest.mock.patch.object(serve, '_safe_library_path', return_value=path), \
             unittest.mock.patch.object(serve, 'record_library_read'):
            first = executor('read_library_file', {'path': 'big.md'})
            second = executor('read_library_file', {'path': 'big.md', 'offset': 20_000})
        self.assertEqual(len(first), 20_000)
        self.assertEqual(second, 'A' * 5_000, 'offset must read the tail past the first window')

    def test_read_library_file_offset_at_eof_says_so(self):
        tmp = tempfile.mkdtemp(prefix='think-tank-lib-tool-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, 'short.md')
        with open(path, 'w') as f:
            f.write('short')
        executor = content._make_library_tools_executor('cora')
        with unittest.mock.patch.object(serve, '_safe_library_path', return_value=path), \
             unittest.mock.patch.object(serve, 'record_library_read'):
            out = executor('read_library_file', {'path': 'short.md', 'offset': 999})
        self.assertIn('EOF', out, 'an offset past the end must say so, not return empty')

    def test_read_library_file_missing_path_reports_not_found(self):
        executor = content._make_library_tools_executor('cora')
        with unittest.mock.patch.object(serve, '_safe_library_path', return_value=None):
            out = executor('read_library_file', {'path': '../escape'})
        self.assertIn('Not found', out)
        self.log_action.assert_called_once_with('cora', 'read_library_file', {'path': '../escape', 'found': False}, authorized=True)

    def test_read_library_file_read_oserror_is_surfaced_not_raised(self):
        # A real file that exists but cannot actually be read (I/O error) must
        # be surfaced as a message, never crash the investigation.
        tmp = tempfile.mkdtemp(prefix='think tank-lib-tool-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, 'finding.md')
        with open(path, 'w') as f:
            f.write('Real findings.')
        executor = content._make_library_tools_executor('cora')
        with unittest.mock.patch.object(serve, '_safe_library_path', return_value=path), \
             unittest.mock.patch('builtins.open', side_effect=OSError('disk error')):
            out = executor('read_library_file', {'path': 'finding.md'})
        self.assertEqual(out, 'Could not read finding.md')
        self.log_action.assert_not_called()  # the OSError branch returns before the found=True log

    def test_unknown_tool_name_raises(self):
        executor = content._make_library_tools_executor('cora')
        with self.assertRaises(ValueError):
            executor('some_other_tool', {})

    def test_struck_tool_is_refused_without_a_second_call(self):
        struck = {'search_library'}
        executor = content._make_library_tools_executor('cora', struck_tools=struck)
        with unittest.mock.patch.object(serve, '_library_search_matches') as search:
            out = executor('search_library', {'query': 'x'})
        search.assert_not_called()
        self.assertIn('already blocked', out.lower())
        self.log_action.assert_not_called()

    def test_spike_tool_list_and_dispatch_include_library_review_tools(self):
        """Wiring: _run_spike_content's tool list and execute_tool dispatcher
        must actually reach these two, not just define them unused."""
        self._common_mocks = SpikeContent._common_mocks.__get__(self)
        self._store = SpikeContent._store.__get__(self)
        self._common_mocks()
        self._store()
        task = {'id': 'spike-review-1', 'title': "Review Team A's login work", 'budgetMs': 60000}
        captured = {}

        def fake_loop(model, messages, tools, execute_tool, **kwargs):
            captured['tools'] = tools
            captured['execute_tool'] = execute_tool
            return ('done', messages)

        with unittest.mock.patch.object(serve, '_call_agent_tool_loop', side_effect=fake_loop), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]), \
             unittest.mock.patch.object(serve, '_library_search_matches',
                                         return_value=[{'path': 'archive/x.md', 'snippet': 'hit', 'modified': 1.0}]):
            content._run_spike_content(_snapshot(), 'cora', task)
            tool_names = {t['function']['name'] for t in captured['tools']}
            self.assertIn('search_library', tool_names)
            self.assertIn('read_library_file', tool_names)
            out = captured['execute_tool']('search_library', {'query': 'login'})
        self.assertIn('archive/x.md', out)

class TrendForcedToolDetection(unittest.TestCase):
    """content._spike_wants_x_trending / _spike_wants_linkedin_search: the
    pure keyword heuristics behind forcing the real Treg tools first,
    same proven pattern as _spike_wants_internal_review, built preemptively
    this time rather than after a live miss."""

    def test_x_trending_requires_both_trending_and_a_real_x_twitter_mention(self):
        self.assertTrue(content._spike_wants_x_trending("What's trending on X right now?", None))
        self.assertTrue(content._spike_wants_x_trending('Trending topics on Twitter', None))
        self.assertTrue(content._spike_wants_x_trending(None, 'check trending on x.com'))
        # 'trending' alone (no X/Twitter mention) must not match.
        self.assertFalse(content._spike_wants_x_trending('What is trending in AI research?', None))
        # A bare 'x' inside an ordinary word must not false-positive.
        self.assertFalse(content._spike_wants_x_trending('Trending topics for flexible teams', None))

    def test_linkedin_search_detects_the_platform_name(self):
        self.assertTrue(content._spike_wants_linkedin_search('Review LinkedIn posts about AI security', None))
        self.assertFalse(content._spike_wants_linkedin_search('Review posts about AI security', None))

class TregToolsExecutor(unittest.TestCase):
    """_make_treg_tools_executor: real Treg API mechanics (POST /call/{id},
    X-Treg-Token), confirmed against Treg's own docs and the actual
    upstream provider -- not guessed (see the Treg skill's own "Lessons
    learned" about an earlier agent inventing wrong prices)."""

    def test_x_trending_topics_success_accrues_the_known_price(self):
        executor = content._make_treg_tools_executor()
        with unittest.mock.patch.object(serve, '_treg_call', return_value=({'trends': ['ai']}, None)) as call, \
             unittest.mock.patch.object(serve, '_accrue_spend') as accrue:
            out = executor('x_trending_topics', {'woeid': 1})
        # method='GET' confirmed: Treg's own real error for
        # the first (POST) attempt was explicit -- "x.x.get-trends-by-woeid
        # is GET -- add --method GET", then "needs --query woeid=<value>".
        call.assert_called_once_with('x.x.get-trends-by-woeid', {'woeid': 1}, method='GET')
        accrue.assert_called_once_with('treg', serve.TREG_ENDPOINT_COSTS['x.x.get-trends-by-woeid'])
        self.assertIn('trends', out)

    def test_x_trending_topics_defaults_to_worldwide_woeid(self):
        executor = content._make_treg_tools_executor()
        with unittest.mock.patch.object(serve, '_treg_call', return_value=({}, None)) as call, \
             unittest.mock.patch.object(serve, '_accrue_spend'):
            executor('x_trending_topics', {})
        call.assert_called_once_with('x.x.get-trends-by-woeid', {'woeid': 1}, method='GET')

    def test_x_trending_topics_error_does_not_accrue_spend(self):
        executor = content._make_treg_tools_executor()
        with unittest.mock.patch.object(serve, '_treg_call', return_value=(None, 'Treg call failed (500): boom')), \
             unittest.mock.patch.object(serve, '_accrue_spend') as accrue:
            out = executor('x_trending_topics', {})
        accrue.assert_not_called()
        self.assertIn('Could not get trending topics', out)

    def test_search_linkedin_posts_builds_the_real_live_verified_request_shape(self):
        # Gap: the originally planned endpoint
        # (harvestapi.linkedin.post.search) failed closed on the first real
        # call -- its declared query params were never published anywhere
        # findable, and Treg's own error gave no field names. Switched to
        # scrapecreators.x.v1-linkedin-search-posts, whose real upstream API
        # IS publicly documented and was confirmed working on the first
        # correctly-shaped real call.
        executor = content._make_treg_tools_executor()
        with unittest.mock.patch.object(serve, '_treg_call', return_value=({'posts': []}, None)) as call, \
             unittest.mock.patch.object(serve, '_accrue_spend') as accrue:
            out = executor('search_linkedin_posts', {'query': 'AI security'})
        call.assert_called_once_with('scrapecreators.x.v1-linkedin-search-posts',
                                     {'query': 'AI security', 'date_posted': 'last-week'}, method='GET')
        accrue.assert_called_once_with('treg', serve.TREG_ENDPOINT_COSTS['scrapecreators.x.v1-linkedin-search-posts'])
        self.assertIn('posts', out)

    def test_search_linkedin_posts_respects_a_custom_date_posted(self):
        executor = content._make_treg_tools_executor()
        with unittest.mock.patch.object(serve, '_treg_call', return_value=({}, None)) as call, \
             unittest.mock.patch.object(serve, '_accrue_spend'):
            executor('search_linkedin_posts', {'query': 'AI security', 'date_posted': 'last-hour'})
        call.assert_called_once_with('scrapecreators.x.v1-linkedin-search-posts',
                                     {'query': 'AI security', 'date_posted': 'last-hour'}, method='GET')

    def test_search_linkedin_posts_empty_query_is_rejected_without_a_call(self):
        executor = content._make_treg_tools_executor()
        with unittest.mock.patch.object(serve, '_treg_call') as call:
            out = executor('search_linkedin_posts', {'query': '  '})
        call.assert_not_called()
        self.assertIn('required', out)

    def test_search_linkedin_posts_error_does_not_accrue_spend(self):
        executor = content._make_treg_tools_executor()
        with unittest.mock.patch.object(serve, '_treg_call', return_value=(None, 'not configured')), \
             unittest.mock.patch.object(serve, '_accrue_spend') as accrue:
            out = executor('search_linkedin_posts', {'query': 'AI security'})
        accrue.assert_not_called()
        self.assertIn('Could not search LinkedIn posts', out)

    def test_unknown_tool_name_raises(self):
        executor = content._make_treg_tools_executor()
        with self.assertRaises(ValueError):
            executor('some_other_tool', {})

class SpikeTregWiring(unittest.TestCase):
    """Wiring: _run_spike_content's tool list, dispatcher, and forced-first-
    tool selection must actually reach the two Treg tools."""

    def _fake_loop_setup(self):
        self._common_mocks = SpikeContent._common_mocks.__get__(self)
        self._store = SpikeContent._store.__get__(self)
        self._common_mocks()
        self._store()

    def test_x_trending_question_forces_the_x_trending_tool_and_is_in_the_tool_list(self):
        self._fake_loop_setup()
        task = {'id': 'spike-treg-1', 'title': "What's trending on X right now?", 'budgetMs': 60000}
        with unittest.mock.patch.object(serve, '_call_agent_tool_loop') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]):
            loop.return_value = ('done', [{'role': 'system', 'content': 's'}])
            content._run_spike_content(_snapshot(), 'cora', task)
        tool_names = {t['function']['name'] for t in loop.call_args.args[2]}
        self.assertIn('x_trending_topics', tool_names)
        self.assertIn('search_linkedin_posts', tool_names)
        self.assertEqual(loop.call_args.kwargs.get('force_first_tool'), 'x_trending_topics')

    def test_linkedin_question_forces_the_linkedin_search_tool(self):
        self._fake_loop_setup()
        task = {'id': 'spike-treg-2', 'title': 'Review LinkedIn posts about AI security', 'budgetMs': 60000}
        with unittest.mock.patch.object(serve, '_call_agent_tool_loop') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]):
            loop.return_value = ('done', [{'role': 'system', 'content': 's'}])
            content._run_spike_content(_snapshot(), 'cora', task)
        self.assertEqual(loop.call_args.kwargs.get('force_first_tool'), 'search_linkedin_posts')

    def test_execute_tool_dispatch_reaches_the_treg_executor(self):
        self._fake_loop_setup()
        task = {'id': 'spike-treg-3', 'title': "What's trending on X right now?", 'budgetMs': 60000}
        captured = {}

        def fake_loop(model, messages, tools, execute_tool, **kwargs):
            captured['execute_tool'] = execute_tool
            return ('done', messages)

        with unittest.mock.patch.object(serve, '_call_agent_tool_loop', side_effect=fake_loop), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]), \
             unittest.mock.patch.object(serve, '_treg_call', return_value=({'trends': ['ai']}, None)), \
             unittest.mock.patch.object(serve, '_accrue_spend'):
            content._run_spike_content(_snapshot(), 'cora', task)
            out = captured['execute_tool']('x_trending_topics', {'woeid': 1})
        self.assertIn('trends', out)

class PixellabCharacterTool(unittest.TestCase):
    """generate_pixel_character now runs through the generic workflow
    chokepoint (_api_execute with the registry's pixellab workflow spec):
    submit /create-character-with-4-directions, poll the background job, fetch
    the final character, and accrue the real before/after balance delta as
    cost (PixelLab has no per-call price list). The balance-delta accrual
    rules (no accrual on zero/negative delta, skip on unreadable balance) are
    tested against _api_execute_workflow in test_api_call.py; here we cover
    the executor's thin contract."""

    def _wf(self, data=None, ids=None):
        return {'ok': True, 'data': data or {'rotation_urls': {'north': 'https://x/n.png'}},
                'ids': ids or {'character_id': 'char-1'}, 'usd': None,
                'textForModel': '', 'modelInstruction': ''}

    def test_full_flow_returns_character_id_and_rotation_urls(self):
        executor = content._make_pixellab_tools_executor('ben', 'k')
        with unittest.mock.patch.object(serve, '_api_execute', return_value=self._wf()):
            out = executor('generate_pixel_character', {'description': 'a knight'})
        result = json.loads(out)
        self.assertEqual(result['character_id'], 'char-1')
        self.assertEqual(result['rotation_urls'], {'north': 'https://x/n.png'})

    def test_empty_description_is_rejected_without_a_call(self):
        executor = content._make_pixellab_tools_executor('ben', 'k')
        with unittest.mock.patch.object(serve, '_api_execute') as ae:
            out = executor('generate_pixel_character', {'description': '  '})
        ae.assert_not_called()
        self.assertIn('required', out)

    def test_create_error_is_surfaced_not_raised(self):
        executor = content._make_pixellab_tools_executor('ben', 'k')
        with unittest.mock.patch.object(serve, '_api_execute',
                                        return_value={'ok': False, 'error': 'PixelLab call failed (500): boom'}):
            out = executor('generate_pixel_character', {'description': 'a knight'})
        self.assertIn('Could not generate character', out)

    def test_unknown_tool_name_raises(self):
        executor = content._make_pixellab_tools_executor('ben', 'k')
        with self.assertRaises(ValueError):
            executor('some_other_tool', {})

    def test_wired_into_spike_tool_list(self):
        self._common_mocks = SpikeContent._common_mocks.__get__(self)
        self._store = SpikeContent._store.__get__(self)
        self._common_mocks()
        self._store()
        task = {'id': 'spike-pixellab-1', 'title': 'Generate a pixel art sprite for a knight', 'budgetMs': 60000}
        with unittest.mock.patch.object(serve, '_call_agent_tool_loop') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]):
            loop.return_value = ('done', [{'role': 'system', 'content': 's'}])
            content._run_spike_content(_snapshot(), 'cora', task)
        tool_names = {t['function']['name'] for t in loop.call_args.args[2]}
        self.assertIn('generate_pixel_character', tool_names)
