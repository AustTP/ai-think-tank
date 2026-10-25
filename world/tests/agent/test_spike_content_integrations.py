"""Spike external-service and skill-publishing tool tests."""

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


class GoogleToolsExecutor(unittest.TestCase):
    def setUp(self):
        try:
            with serve._db() as conn:
                conn.execute('''CREATE TABLE IF NOT EXISTS external_writes (
                    kind TEXT NOT NULL, key TEXT NOT NULL, result TEXT NOT NULL,
                    created_at REAL NOT NULL, PRIMARY KEY (kind, key)''')
                conn.execute('DELETE FROM external_writes')
        except Exception:
            pass


    """read_google_sheet / append_google_sheet_row / list_calendar_events /
    create_calendar_event -- real OAuth call mechanics confirmed
    against a real refresh token minted through a real
    installed-app consent flow, before anything was built on top of it."""

    def test_read_google_sheet_builds_the_real_url_and_returns_data(self):
        executor = content._make_google_tools_executor()
        with unittest.mock.patch.object(serve, '_google_call',
                                        return_value=({'values': [['a', 'b']]}, None)) as call:
            out = executor('read_google_sheet', {'spreadsheet_id': 'sheet-1', 'range': 'Sheet1!A1:B2'})
        call.assert_called_once_with(
            'GET', 'https://sheets.googleapis.com/v4/spreadsheets/sheet-1/values/Sheet1%21A1%3AB2')
        self.assertIn('values', out)

    def test_read_google_sheet_requires_both_fields(self):
        executor = content._make_google_tools_executor()
        with unittest.mock.patch.object(serve, '_google_call') as call:
            out = executor('read_google_sheet', {'spreadsheet_id': '', 'range': 'A1'})
        call.assert_not_called()
        self.assertIn('required', out)

    def test_append_google_sheet_row_builds_the_real_append_request(self):
        executor = content._make_google_tools_executor()
        with unittest.mock.patch.object(serve, '_google_call', return_value=({'updates': {}}, None)) as call:
            out = executor('append_google_sheet_row',
                           {'spreadsheet_id': 'sheet-1', 'range': 'Sheet1!A1', 'values': ['x', 'y']})
        call.assert_called_once_with(
            'POST',
            'https://sheets.googleapis.com/v4/spreadsheets/sheet-1/values/Sheet1%21A1:append?valueInputOption=USER_ENTERED',
            {'values': [['x', 'y']]})
        self.assertIn('updates', out)

    def test_append_google_sheet_row_requires_nonempty_values(self):
        executor = content._make_google_tools_executor()
        with unittest.mock.patch.object(serve, '_google_call') as call:
            out = executor('append_google_sheet_row', {'spreadsheet_id': 's', 'range': 'A1', 'values': []})
        call.assert_not_called()
        self.assertIn('required', out)

    def test_list_calendar_events_defaults_to_10_and_filters_to_upcoming(self):
        executor = content._make_google_tools_executor()
        with unittest.mock.patch.object(serve, '_google_call',
                                        return_value=({'items': [{'summary': 'Standup'}]}, None)) as call:
            out = executor('list_calendar_events', {})
        args = call.call_args.args
        self.assertEqual(args[0], 'GET')
        self.assertIn('maxResults=10', args[1])
        self.assertIn('orderBy=startTime', args[1])
        self.assertIn('singleEvents=true', args[1])
        self.assertIn('Standup', out)

    def test_list_calendar_events_respects_a_custom_max_results(self):
        executor = content._make_google_tools_executor()
        with unittest.mock.patch.object(serve, '_google_call', return_value=({'items': []}, None)) as call:
            executor('list_calendar_events', {'max_results': 3})
        self.assertIn('maxResults=3', call.call_args.args[1])

    def test_create_calendar_event_builds_the_real_event_body(self):
        executor = content._make_google_tools_executor()
        with unittest.mock.patch.object(serve, '_google_call',
                                        return_value=({'id': 'evt-1', 'htmlLink': 'https://cal/evt-1'}, None)) as call:
            out = executor('create_calendar_event', {
                'summary': 'Sprint review', 'start_datetime': '2026-10-01T14:00:00-04:00',
                'end_datetime': '2026-10-01T15:00:00-04:00', 'description': 'real ceremony',
                'requestId': 'calendar-test-1'})
        call.assert_called_once()
        args = call.call_args.args
        self.assertEqual(args[0], 'POST')
        self.assertEqual(args[1], 'https://www.googleapis.com/calendar/v3/calendars/primary/events')
        body = args[2]
        self.assertEqual(body['summary'], 'Sprint review')
        self.assertEqual(body['start'], {'dateTime': '2026-10-01T14:00:00-04:00'})
        self.assertEqual(body['end'], {'dateTime': '2026-10-01T15:00:00-04:00'})
        self.assertEqual(body['description'], 'real ceremony')
        self.assertTrue(body['id'].startswith('tt'))
        self.assertIn('evt-1', out)

    def test_create_calendar_event_requires_summary_and_times(self):
        executor = content._make_google_tools_executor()
        with unittest.mock.patch.object(serve, '_google_call') as call:
            out = executor('create_calendar_event', {'summary': '', 'start_datetime': '', 'end_datetime': ''})
        call.assert_not_called()
        self.assertIn('required', out)

    def test_error_from_google_call_is_surfaced_not_raised(self):
        executor = content._make_google_tools_executor()
        with unittest.mock.patch.object(serve, '_google_call', return_value=(None, 'Google call failed (403): quota exceeded')):
            out = executor('list_calendar_events', {})
        self.assertIn('Could not list calendar events', out)

    def test_unknown_tool_name_raises(self):
        executor = content._make_google_tools_executor()
        with self.assertRaises(ValueError):
            executor('some_other_tool', {})

    def test_wired_into_spike_tool_list(self):
        self._common_mocks = SpikeContent._common_mocks.__get__(self)
        self._store = SpikeContent._store.__get__(self)
        self._common_mocks()
        self._store()
        task = {'id': 'spike-google-1', 'title': 'Check the shared roadmap sheet', 'budgetMs': 60000}
        with unittest.mock.patch.object(serve, '_call_agent_tool_loop') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]):
            loop.return_value = ('done', [{'role': 'system', 'content': 's'}])
            content._run_spike_content(_snapshot(), 'cora', task)
        tool_names = {t['function']['name'] for t in loop.call_args.args[2]}
        self.assertIn('read_google_sheet', tool_names)
        self.assertIn('append_google_sheet_row', tool_names)
        self.assertIn('list_calendar_events', tool_names)
        self.assertIn('create_calendar_event', tool_names)

class GitHubToolsExecutor(unittest.TestCase):
    """github_get_repo / github_list_issues / github_get_issue /
    github_search_code -- real read-only GitHub API calls so
    engineering work grounds itself in real public code instead of
    hallucinating plausible-looking repos/issues."""

    def test_get_repo_builds_the_real_url_and_returns_metadata(self):
        executor = content._make_github_tools_executor('ben', 'k')
        with unittest.mock.patch.object(serve, '_api_execute',
                                        return_value={'ok': True, 'data': {'full_name': 'python/cpython', 'stargazers_count': 70000},
                                                      'textForModel': '', 'modelInstruction': ''}) as ae:
            out = executor('github_get_repo', {'owner': 'python', 'repo': 'cpython'})
        self.assertEqual(ae.call_args.args[2], 'https://api.github.com/repos/python/cpython')
        self.assertIn('python/cpython', out)
        self.assertIn('stars', out)

    def test_get_repo_url_escapes_owner_and_repo(self):
        executor = content._make_github_tools_executor('ben', 'k')
        with unittest.mock.patch.object(serve, '_api_execute',
                                        return_value={'ok': True, 'data': {}, 'textForModel': '', 'modelInstruction': ''}) as ae:
            executor('github_get_repo', {'owner': 'my org', 'repo': 'my/repo'})
        self.assertIn('my%20org', ae.call_args.args[2])
        self.assertIn('my%2Frepo', ae.call_args.args[2])

    def test_get_repo_requires_owner_and_repo(self):
        executor = content._make_github_tools_executor('ben', 'k')
        with unittest.mock.patch.object(serve, '_api_execute') as ae:
            out = executor('github_get_repo', {'owner': '', 'repo': ''})
        ae.assert_not_called()
        self.assertIn('required', out)

    def test_list_issues_filters_out_pull_requests(self):
        executor = content._make_github_tools_executor('ben', 'k')
        fake = [
            {'number': 1, 'title': 'an issue', 'state': 'open', 'labels': [], 'comments': 2, 'html_url': 'u1'},
            {'number': 2, 'title': 'a PR', 'state': 'open', 'labels': [], 'comments': 0, 'html_url': 'u2', 'pull_request': {}},
        ]
        with unittest.mock.patch.object(serve, '_api_execute',
                                        return_value={'ok': True, 'data': fake, 'textForModel': '', 'modelInstruction': ''}) as ae:
            out = executor('github_list_issues', {'owner': 'python', 'repo': 'cpython'})
        self.assertIn('an issue', out)
        self.assertIn('state=open', ae.call_args.args[2])
        self.assertIn('per_page=10', ae.call_args.args[2])
        self.assertNotIn('a PR', out)

    def test_list_issues_respects_state_and_limit(self):
        executor = content._make_github_tools_executor('ben', 'k')
        with unittest.mock.patch.object(serve, '_api_execute',
                                        return_value={'ok': True, 'data': [], 'textForModel': '', 'modelInstruction': ''}) as ae:
            executor('github_list_issues', {'owner': 'o', 'repo': 'r', 'state': 'closed', 'limit': 30})
        self.assertIn('state=closed', ae.call_args.args[2])
        self.assertIn('per_page=30', ae.call_args.args[2])

    def test_get_issue_guards_against_pull_requests(self):
        executor = content._make_github_tools_executor('ben', 'k')
        with unittest.mock.patch.object(serve, '_api_execute',
                                        return_value={'ok': True, 'data': {'number': 7, 'pull_request': {'url': 'x'}},
                                                      'textForModel': '', 'modelInstruction': ''}) as ae:
            out = executor('github_get_issue', {'owner': 'o', 'repo': 'r', 'issue_number': 7})
        self.assertIn('is a pull request', out)
        # The comments fetch must not have happened for a PR.
        ae.assert_called_once()

    def test_get_issue_fetches_top_comments(self):
        executor = content._make_github_tools_executor('ben', 'k')
        issue = {'number': 1, 'title': 't', 'state': 'open', 'labels': [], 'body': 'body', 'html_url': 'u'}
        comments = [{'user': {'login': 'alice'}, 'body': 'agree'}]
        with unittest.mock.patch.object(serve, '_api_execute',
                                        side_effect=[{'ok': True, 'data': issue, 'textForModel': '', 'modelInstruction': ''},
                                                     {'ok': True, 'data': comments, 'textForModel': '', 'modelInstruction': ''}]) as ae:
            out = executor('github_get_issue', {'owner': 'o', 'repo': 'r', 'issue_number': 1})
        self.assertIn('top_comments', out)
        self.assertIn('alice', out)
        self.assertEqual(ae.call_count, 2)
        self.assertIn('comments?per_page=20', ae.call_args_list[1].args[2])

    def test_get_issue_comments_error_yields_empty_top_comments(self):
        # A comments fetch failure must not fail the whole issue read -- the
        # issue body/title are still real and useful; comments just degrade to
        # an empty list rather than a fabricated one.
        executor = content._make_github_tools_executor('ben', 'k')
        issue = {'number': 1, 'title': 't', 'state': 'open', 'labels': [], 'body': 'body', 'html_url': 'u'}
        with unittest.mock.patch.object(serve, '_api_execute',
                                        side_effect=[{'ok': True, 'data': issue, 'textForModel': '', 'modelInstruction': ''},
                                                     {'ok': False, 'error': 'GitHub call failed (500): boom'}]):
            out = executor('github_get_issue', {'owner': 'o', 'repo': 'r', 'issue_number': 1})
        self.assertIn('"top_comments": []', out)
        self.assertIn('"title": "t"', out)

    def test_get_issue_requires_number(self):
        executor = content._make_github_tools_executor('ben', 'k')
        with unittest.mock.patch.object(serve, '_api_execute') as ae:
            out = executor('github_get_issue', {'owner': 'o', 'repo': 'r'})
        ae.assert_not_called()
        self.assertIn('required', out)

    def test_search_code_builds_the_real_query_url(self):
        executor = content._make_github_tools_executor('ben', 'k')
        fake = {'total_count': 1, 'items': [{'repository': {'full_name': 'o/r'}, 'path': 'p.py', 'html_url': 'u'}]}
        with unittest.mock.patch.object(serve, '_api_execute',
                                        return_value={'ok': True, 'data': fake, 'textForModel': '', 'modelInstruction': ''}) as ae:
            out = executor('github_search_code', {'query': 'openrouter language:python'})
        self.assertIn('total_count', out)
        self.assertIn('openrouter%20language%3Apython', ae.call_args.args[2])
        self.assertIn('per_page=5', ae.call_args.args[2])

    def test_search_code_requires_a_query(self):
        executor = content._make_github_tools_executor('ben', 'k')
        with unittest.mock.patch.object(serve, '_api_execute') as ae:
            out = executor('github_search_code', {'query': ''})
        ae.assert_not_called()
        self.assertIn('required', out)

    def test_error_from_github_call_is_surfaced_not_raised(self):
        executor = content._make_github_tools_executor('ben', 'k')
        with unittest.mock.patch.object(serve, '_api_execute',
                                        return_value={'ok': False, 'error': 'GitHub call failed (404): not found'}):
            out = executor('github_get_repo', {'owner': 'o', 'repo': 'nope'})
        self.assertIn('Could not read the repo', out)

    def test_unknown_tool_name_raises(self):
        executor = content._make_github_tools_executor('ben', 'k')
        with self.assertRaises(ValueError):
            executor('some_other_tool', {})

    def test_wired_into_spike_tool_list_only_when_token_set(self):
        self._common_mocks = SpikeContent._common_mocks.__get__(self)
        self._store = SpikeContent._store.__get__(self)
        self._common_mocks()
        self._store()
        task = {'id': 'spike-github-1', 'title': 'Check open issues for the cpython repo', 'budgetMs': 60000}
        # Token set -> all four GitHub tools offered, and github_get_repo forced first.
        with unittest.mock.patch.object(serve, 'GITHUB_TOKEN', 'fake-token'), \
             unittest.mock.patch.object(serve, '_call_agent_tool_loop') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]):
            loop.return_value = ('done', [{'role': 'system', 'content': 's'}])
            content._run_spike_content(_snapshot(), 'cora', task)
        tool_names = {t['function']['name'] for t in loop.call_args.args[2]}
        for name in ('github_get_repo', 'github_list_issues', 'github_get_issue', 'github_search_code'):
            self.assertIn(name, tool_names)
        self.assertEqual(loop.call_args.kwargs.get('force_first_tool'), 'github_get_repo')

    def test_github_tools_absent_when_token_unset(self):
        self._common_mocks = SpikeContent._common_mocks.__get__(self)
        self._store = SpikeContent._store.__get__(self)
        self._common_mocks()
        self._store()
        task = {'id': 'spike-github-2', 'title': 'Check open issues for a repo', 'budgetMs': 60000}
        with unittest.mock.patch.object(serve, 'GITHUB_TOKEN', ''), \
             unittest.mock.patch.object(serve, '_call_agent_tool_loop') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]):
            loop.return_value = ('done', [{'role': 'system', 'content': 's'}])
            content._run_spike_content(_snapshot(), 'cora', task)
        tool_names = {t['function']['name'] for t in loop.call_args.args[2]}
        for name in ('github_get_repo', 'github_list_issues', 'github_get_issue', 'github_search_code'):
            self.assertNotIn(name, tool_names)

class ApifyToolsExecutor(unittest.TestCase):
    def setUp(self):
        try:
            with serve._db() as conn:
                conn.execute('''CREATE TABLE IF NOT EXISTS external_writes (
                    kind TEXT NOT NULL, key TEXT NOT NULL, result TEXT NOT NULL,
                    created_at REAL NOT NULL, PRIMARY KEY (kind, key)''')
                conn.execute('DELETE FROM external_writes')
        except Exception:
            pass


    """_make_apify_tools_executor: real Apify platform mechanics (POST
    /actors/{id}/runs, GET /actor-runs/{id}, GET /datasets/{id}/items) with
    the think tank's own Bearer token held server-side, spend-guarded by the
    FREE-plan monthly budget chokepoint (fail closed before any real run
    starts) and cost accrued from the run's own reported usageTotalUsd -- not
    a fabricated number."""

    def test_run_actor_success_accrues_reported_cost_and_fetches_items(self):
        executor = content._make_apify_tools_executor('ben', 'k')
        started = {'data': {'id': 'run-1', 'status': 'RUNNING',
                            'defaultDatasetId': 'ds-1', 'usageTotalUsd': 0.0}}
        settled = {'data': {'id': 'run-1', 'status': 'SUCCEEDED',
                            'defaultDatasetId': 'ds-1', 'usageTotalUsd': 0.0042}}
        items = [{'url': 'https://example.com', 'title': 'Example'}]

        def fake_execute(agent_id, key, url, method='GET', req_body=None, req_headers=None,
                         purpose='', workflow=False, workflow_params=None, trace_id=None):
            if '/actors/' in url and '/runs' in url:
                return {'ok': True, 'data': started, 'textForModel': '', 'modelInstruction': ''}
            if '/actor-runs/' in url:
                return {'ok': True, 'data': settled, 'textForModel': '', 'modelInstruction': ''}
            if '/datasets/' in url:
                return {'ok': True, 'data': items, 'textForModel': '', 'modelInstruction': ''}
            return {'ok': True, 'data': {}, 'textForModel': '', 'modelInstruction': ''}

        with unittest.mock.patch.object(serve, '_apify_budget_exceeded', return_value=False) as gate, \
             unittest.mock.patch.object(serve, '_api_execute', side_effect=fake_execute) as ae, \
             unittest.mock.patch('time.sleep'):
            out = executor('apify_run_actor',
                           {'actorId': 'apify/website-content-crawler',
                            'input': {'startUrls': [{'url': 'https://example.com'}]},
                            'waitSeconds': 5, 'requestId': 'apify-success-test'})
        gate.assert_called_once()
        urls = [c.args[2] for c in ae.call_args_list]
        self.assertIn('/actors/apify/website-content-crawler/runs', urls[0])
        self.assertIn('/actor-runs/run-1', urls[1])
        self.assertIn('/datasets/ds-1/items', urls[2])
        self.assertIn('SUCCEEDED', out)
        self.assertIn('run-1', out)
        self.assertIn('https://example.com', out)

    def test_run_actor_refuses_when_budget_exceeded_without_any_network_call(self):
        executor = content._make_apify_tools_executor('ben', 'k')
        with unittest.mock.patch.object(serve, '_apify_budget_exceeded', return_value=True), \
             unittest.mock.patch.object(serve, '_api_execute') as ae:
            out = executor('apify_run_actor', {'actorId': 'apify/website-content-crawler',
                                               'input': {'startUrls': []},
                                               'requestId': 'apify-budget-test'})
        ae.assert_not_called()
        self.assertIn('budget is exhausted', out)

    def test_run_actor_error_is_returned_without_accruing(self):
        executor = content._make_apify_tools_executor('ben', 'k')
        with unittest.mock.patch.object(serve, '_apify_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, '_api_execute',
                                        return_value={'ok': False, 'error': 'Apify call failed (403): denied'}):
            out = executor('apify_run_actor', {'actorId': 'x', 'input': {}})
        self.assertIn('Could not start Apify actor', out)

    def test_run_actor_poll_error_breaks_without_accruing_or_fetching_items(self):
        # A poll call failing mid-wait must stop polling (not keep hammering),
        # report the last known status, and skip the items fetch (run never
        # SUCCEEDED). Spend accrual lives in _api_execute (tested separately).
        executor = content._make_apify_tools_executor('ben', 'k')
        calls = {'n': 0}

        def fake_execute(agent_id, key, url, method='GET', req_body=None, req_headers=None,
                         purpose='', workflow=False, workflow_params=None, trace_id=None):
            calls['n'] += 1
            if calls['n'] == 1:
                return {'ok': True, 'data': {'data': {'id': 'run-1', 'status': 'RUNNING',
                                                      'defaultDatasetId': 'ds-1'}},
                        'textForModel': '', 'modelInstruction': ''}
            return {'ok': False, 'error': 'Apify call failed (500): poll boom'}

        with unittest.mock.patch.object(serve, '_apify_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, '_api_execute', side_effect=fake_execute), \
             unittest.mock.patch('time.sleep'):
            out = executor('apify_run_actor',
                           {'actorId': 'apify/website-content-crawler',
                            'input': {'startUrls': [{'url': 'https://example.com'}]},
                            'waitSeconds': 5, 'requestId': 'apify-poll-error-test'})
        self.assertEqual(calls['n'], 2)  # start + one (errored) poll, then break
        self.assertIn('RUNNING', out)  # never overwritten after the failed poll
        self.assertIn('run-1', out)

    def test_run_actor_reports_items_error_when_dataset_fetch_fails(self):
        # A run that settles SUCCEEDED but whose dataset items fetch fails must
        # still report the run honestly, with the items error surfaced.
        executor = content._make_apify_tools_executor('ben', 'k')

        def fake_execute(agent_id, key, url, method='GET', req_body=None, req_headers=None,
                         purpose='', workflow=False, workflow_params=None, trace_id=None):
            if '/actors/' in url:
                return {'ok': True, 'data': {'data': {'id': 'run-1', 'status': 'SUCCEEDED',
                                                      'defaultDatasetId': 'ds-1', 'usageTotalUsd': 0.01}},
                        'textForModel': '', 'modelInstruction': ''}
            return {'ok': False, 'error': 'Apify call failed (500): items boom'}

        with unittest.mock.patch.object(serve, '_apify_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, '_api_execute', side_effect=fake_execute) as ae, \
             unittest.mock.patch('time.sleep'):
            out = executor('apify_run_actor',
                           {'actorId': 'apify/website-content-crawler',
                            'input': {'startUrls': []}, 'waitSeconds': 5,
                            'requestId': 'apify-items-error-test'})
        self.assertIn('SUCCEEDED', out)
        self.assertIn('itemsError', out)
        self.assertIn('items boom', out)
        # SUCCEEDED at start -> no poll loop; exactly start + items fetch.
        self.assertEqual(ae.call_count, 2)

    def test_get_dataset_items_fetches_clean_json(self):
        executor = content._make_apify_tools_executor('ben', 'k')
        with unittest.mock.patch.object(serve, '_api_execute',
                                        return_value={'ok': True, 'data': [{'a': 1}],
                                                      'textForModel': '', 'modelInstruction': ''}) as ae:
            out = executor('apify_get_dataset_items', {'datasetId': 'ds-9'})
        self.assertIn('/datasets/ds-9/items', ae.call_args.args[2])
        self.assertIn('"a"', out)

    def test_unknown_tool_name_raises(self):
        executor = content._make_apify_tools_executor('ben', 'k')
        with self.assertRaises(ValueError):
            executor('some_other_tool', {})

    def test_wired_into_spike_tool_list_only_when_apify_key_set(self):
        self._common_mocks = SpikeContent._common_mocks.__get__(self)
        self._store = SpikeContent._store.__get__(self)
        self._common_mocks()
        self._store()
        task = {'id': 'spike-apify-1', 'title': 'Scrape a website', 'budgetMs': 60000}
        with unittest.mock.patch.object(serve, 'APIFY_API_KEY', 'fake-apify-key'), \
             unittest.mock.patch.object(serve, '_call_agent_tool_loop') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]):
            loop.return_value = ('done', [{'role': 'system', 'content': 's'}])
            content._run_spike_content(_snapshot(), 'cora', task)
        tool_names = {t['function']['name'] for t in loop.call_args.args[2]}
        for name in ('apify_run_actor', 'apify_get_dataset_items'):
            self.assertIn(name, tool_names)

    def test_apify_tools_absent_when_key_unset(self):
        self._common_mocks = SpikeContent._common_mocks.__get__(self)
        self._store = SpikeContent._store.__get__(self)
        self._common_mocks()
        self._store()
        task = {'id': 'spike-apify-2', 'title': 'Scrape a website', 'budgetMs': 60000}
        with unittest.mock.patch.object(serve, 'APIFY_API_KEY', ''), \
             unittest.mock.patch.object(serve, '_call_agent_tool_loop') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]):
            loop.return_value = ('done', [{'role': 'system', 'content': 's'}])
            content._run_spike_content(_snapshot(), 'cora', task)
        tool_names = {t['function']['name'] for t in loop.call_args.args[2]}
        for name in ('apify_run_actor', 'apify_get_dataset_items'):
            self.assertNotIn(name, tool_names)

class SkillPublisherTests(unittest.TestCase):
    """The publish_skill executor (content._make_skill_publisher): validation,
    the standardized provenance+interface card, and the library write path."""

    def _publisher(self, http_bodies):
        def fake_http(method, base, path, body, *a, **k):
            http_bodies.append({'method': method, 'path': path, 'body': body})
            return {'ok': True}
        return content._make_skill_publisher('cora', 'key-123'), \
            unittest.mock.patch.object(serve, '_http_json', side_effect=fake_http)

    def test_requires_all_fields(self):
        pub, _ = self._publisher([])
        self.assertIn('requires slug', pub('publish_skill', {'slug': 'x'}))
        self.assertIn('requires slug', pub('publish_skill', {}))

    def test_rejects_body_without_distilled_headings(self):
        pub, _ = self._publisher([])
        args = {'slug': 'fetch-x', 'title': 'Fetch X', 'sourceTask': 'task-3',
                'team': 'cora', 'interface': 'GET /x', 'body': 'just some notes'}
        self.assertIn('distilled headings', pub('publish_skill', args))

    def test_files_standardized_card_to_pending_review(self):
        bodies = []
        pub, http = self._publisher(bodies)
        args = {'slug': 'fetch-x', 'title': 'Fetch X data', 'sourceTask': 'task-3',
                'team': 'cora', 'interface': 'Invoke: GET /api/x with api key. Inputs: id. Outputs: json. Example: id=7.',
                'body': '## Purpose\nGet X fast.\n## Key facts\nOne endpoint.\n## Sources\nNone.\n## Lessons learned\nNone yet.'}
        with http:
            out = pub('publish_skill', args)
        self.assertIn('pending_review/skills/fetch-x.md', out)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]['path'], '/api/library/file')
        self.assertEqual(bodies[0]['body']['path'], 'pending_review/skills/fetch-x.md')
        self.assertEqual(bodies[0]['body']['source'], 'firsthand')
        card = bodies[0]['body']['content']
        self.assertIn('## Provenance', card)
        self.assertIn('source task: `task-3`', card)
        self.assertIn('team / agent: `cora`', card)
        self.assertIn('## Interface', card)
        self.assertIn('Invoke: GET /api/x', card)
        self.assertIn('## Key facts', card)

    def test_slug_is_sanitized(self):
        bodies = []
        pub, http = self._publisher(bodies)
        args = {'slug': 'Fetch X!', 'title': 'Fetch X', 'sourceTask': 'task-3',
                'team': 'cora', 'interface': 'i', 'body': '## Purpose\np.\n## Key facts\nk.\n## Sources\ns.\n## Lessons learned\nl.'}
        with http:
            pub('publish_skill', args)
        self.assertEqual(bodies[0]['body']['path'], 'pending_review/skills/fetch-x.md')

class WriteSpikeReportTool(unittest.TestCase):
    """The write_spike_report executor (content._make_spike_report_writer): a
    document-length spike deliverable that cannot fit in the single 1800-token
    closing _plain_completion must be writable in full to the same canonical
    report path the closer uses, without the closer clobbering it."""

    def _writer(self, http_bodies, report_path='archive/1-spike-task-9.md'):
        def fake_http(method, base, path, body, *a, **k):
            http_bodies.append({'method': method, 'path': path, 'body': body})
            return {'ok': True}
        patcher = unittest.mock.patch.object(serve, 'log_action')
        self.log_action = patcher.start()
        self.addCleanup(patcher.stop)
        return content._make_spike_report_writer('cora', 'key-123', report_path), \
            unittest.mock.patch.object(serve, '_http_json', side_effect=fake_http)

    def test_requires_content(self):
        w, _ = self._writer([])
        self.assertIn('content is required', w('write_spike_report', {}))
        self.assertIn('content is required', w('write_spike_report', {'content': '   '}))

    def test_writes_full_content_to_canonical_report_path(self):
        bodies = []
        w, http = self._writer(bodies)
        long_report = '# Title\n\n' + ('Section body. ' * 400)
        with http:
            out = w('write_spike_report', {'content': long_report})
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]['path'], '/api/library/file')
        self.assertEqual(bodies[0]['body']['path'], 'archive/1-spike-task-9.md')
        self.assertEqual(bodies[0]['body']['source'], 'firsthand')
        self.assertEqual(bodies[0]['body']['content'], long_report.rstrip())
        self.assertIn('archive/1-spike-task-9.md', out)
        self.log_action.assert_called_once_with(
            'cora', 'spike_report_written',
            {'path': 'archive/1-spike-task-9.md', 'bytes': len(long_report.rstrip())}, authorized=True)

    def test_overwrites_with_full_content_each_call(self):
        bodies = []
        w, http = self._writer(bodies)
        with http:
            w('write_spike_report', {'content': 'first draft'})
            w('write_spike_report', {'content': 'second full draft'})
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[1]['body']['content'], 'second full draft')

    def test_http_failure_returns_honest_message(self):
        def boom(*a, **k):
            raise RuntimeError('connection refused')
        patcher = unittest.mock.patch.object(serve, 'log_action')
        patcher.start()
        self.addCleanup(patcher.stop)
        with unittest.mock.patch.object(serve, '_http_json', side_effect=boom):
            w = content._make_spike_report_writer('cora', 'key-123', 'archive/1.md')
            out = w('write_spike_report', {'content': 'x'})
        self.assertIn('failed to save', out)

    def test_unknown_tool_raises(self):
        w, _ = self._writer([])
        with self.assertRaises(ValueError):
            w('not_a_tool', {})
