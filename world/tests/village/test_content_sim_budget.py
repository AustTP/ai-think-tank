"""Coverage-gap tests for world/sim.py and world/content.py.

Targets the residual uncovered lines reported by the per-file coverage run.
Every test is hermetic: serve network/DB seams are monkeypatched, no live
model call or fetch happens, and no real state DB is touched.
"""
import asyncio
import importlib
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config  # noqa: E402
import content  # noqa: E402
import serve  # noqa: E402
import sim  # noqa: E402

from _content_sim_helpers import _Base, _completion, _snapshot


BUDGET = {'budgetExhausted': True, 'taskSpendUsd': 1.0,
          'taskSpendAttempts': 2}


def _http_router(responses):
    side = list(responses)

    def _fn(method, base, path, body=None, header=None, timeout=30):
        if side:
            return side.pop(0)
        return {}
    return _fn


class ContentBudgetTest(_Base):
    def test_record_source_evidence_except(self):
        with unittest.mock.patch('serve._http_json',
                                 side_effect=RuntimeError('boom')):
            out = content._record_source_evidence(
                'http://x', 'k', 'cora', 'u', 'ru', 'text', 0, 'tp', 't1')
        self.assertTrue(out.get('recorded'))

    def test_note_research_coverage_failure_swallowed(self):
        with unittest.mock.patch('serve._http_json',
                                 side_effect=RuntimeError('boom')):
            content._note_research_coverage('http://x', 'k', 'cora', 'ident',
                                            'artifact')
            content._note_research_failure('http://x', 'k', 'cora', 'ident',
                                           'why')

    def test_note_research_failure_records_when_server_responds(self):
        with unittest.mock.patch('serve._http_json',
                                 return_value={'ok': True}):
            content._note_research_failure('http://x', 'k', 'cora', 'ident',
                                           'why')

    def test_research_chat_budget(self):
        task = {'id': 't1', 'research': {'topicId': 'rt1', 'since': 0}}
        snap = {'researchTopics': [{'id': 'rt1', 'topic': 'T',
                                    'startUrl': 'https://example.com',
                                    'seenUrls': []}],
                'agents': {}, 'agentRoster': []}
        def _router(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/chat':
                return BUDGET
            if path == '/api/browse':
                return {'allowed': True, 'text': 'keyword text',
                        'links': []}
            if path == '/api/evidence':
                return {'recorded': True}
            if path == '/api/sandbox-save-page':
                return {'allowed': True, 'ok': True}
            return {}
        with unittest.mock.patch('serve._http_json',
                                 side_effect=_router), \
             unittest.mock.patch('serve._resolve_model_tier',
                                 return_value='mid'), \
             unittest.mock.patch('serve.get_or_create_agent_key',
                                 return_value='k'), \
             unittest.mock.patch('sim._store_content_result') as store:
            content._run_research_content(snap, 'cora', task)
        self.assertTrue(store.called)
        self.assertTrue(store.call_args[0][1].get('budgetExhausted'))

    def test_media_chat_budget(self):
        def _router(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/chat':
                return BUDGET
            if path == '/api/browse':
                return {'allowed': True, 'text': 'long text ' * 50}
            return {}
        with unittest.mock.patch('serve._http_json',
                                 side_effect=_router), \
             unittest.mock.patch('content._parse_feed_urls_after',
                                 return_value=['https://feed']), \
             unittest.mock.patch('serve._resolve_model_tier',
                                 return_value='mid'), \
             unittest.mock.patch('serve.get_or_create_agent_key',
                                 return_value='k'), \
             unittest.mock.patch('sim._store_content_result') as store:
            content._run_media_content(_snapshot(), 'cora',
                                       {'id': 't1', 'room': 'media'})
        self.assertTrue(store.call_args[0][1].get('budgetExhausted'))

    def test_distill_chat_budget(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, 'a.md'), 'w') as f:
                f.write('# finding\n')
            with unittest.mock.patch.object(serve, 'LIBRARY_ARCHIVE_DIR', td), \
                 unittest.mock.patch.object(serve, 'LIBRARY_DIR',
                                            os.path.join(td, 'none')), \
                 unittest.mock.patch('serve._http_json',
                                     return_value=BUDGET), \
                 unittest.mock.patch('serve._resolve_model_tier',
                                     return_value='mid'), \
                 unittest.mock.patch('serve.get_or_create_agent_key',
                                     return_value='k'), \
                 unittest.mock.patch('sim._store_content_result') as store:
                content._run_distill_content(
                    _snapshot(), 'cora', {'id': 't1', 'distillSince': 0})
        self.assertTrue(store.call_args[0][1].get('budgetExhausted'))

    def test_dispatcher_training_capture(self):
        cap = unittest.mock.Mock()
        cap.enabled.return_value = True
        cap.metadata.return_value = {}
        cap.CURRENT.set.return_value = 'tok'
        with unittest.mock.patch.object(content, 'training_capture', cap), \
             unittest.mock.patch.object(content, '_run_observatory_content') as run:
            content._server_content_dispatcher(
                _snapshot(), 'cora', {'id': 't1', 'room': 'observatory'})
        self.assertTrue(run.called)
        self.assertTrue(cap.CURRENT.set.called)
        self.assertTrue(cap.CURRENT.reset.called)

    def test_research_project_chat_budget(self):
        with unittest.mock.patch('serve._resolve_model_tier',
                                 return_value='mid'), \
             unittest.mock.patch('serve._http_json', return_value=BUDGET), \
             unittest.mock.patch('serve.get_or_create_agent_key',
                                 return_value='k'), \
             unittest.mock.patch('sim._store_content_result') as store:
            content._run_research_project_content(
                _snapshot(), 'cora',
                {'id': 't1', 'title': 'T', 'projectLabel': 'P'})
        self.assertTrue(store.call_args[0][1].get('budgetExhausted'))


class ContentHelperTest(_Base):
    def test_review_screenshot_budget(self):
        with unittest.mock.patch('serve._http_json',
                                 side_effect=[{'imageBase64': 'x'},
                                              BUDGET]), \
             unittest.mock.patch('serve._vision_tier_slug',
                                 return_value='v'):
            out = content._review_screenshot(
                'b', 'k', 'cora', 'sb', 'index.html', 'q')
        self.assertTrue(out.get('budgetExhausted'))

    def test_quality_snippet_bandit_cap(self):
        stdout = '\n'.join(
            f'>> Issue: [B{i}] thing\n  detail\nLocation: ./f.py:{i}'
            for i in range(7))
        out = content._quality_pipeline_failure_snippet(
            'bandit', stdout, '')
        self.assertIn('Location', out)

    def test_quality_evidence(self):
        out = content._quality_pipeline_evidence({'results': [
            {'name': 'flake8', 'stdout': 'a\nb\nc', 'stderr': 'e'},
            {'name': 'empty', 'stdout': '', 'stderr': ''}]})
        self.assertIn('[flake8]', out)
        self.assertNotIn('[empty]', out)

    def test_list_handles(self):
        handles = [{'credentialName': 'c', 'purpose': 'p',
                    'allowedHosts': ['a.com', 'b.com'],
                    'allowedMethods': ['GET'], 'expiresInSec': 60,
                    'handle': 'h1'}]
        with unittest.mock.patch('serve._http_json',
                                 return_value={'handles': handles}):
            ex = content._make_list_handles_executor('cora', 'k')
            out = ex('list_capability_handles', {})
        self.assertIn('a.com, b.com', out)
        with unittest.mock.patch('serve._http_json',
                                 return_value='not-a-dict'):
            self.assertIn('unexpected', ex('list_capability_handles', {}))
        with unittest.mock.patch('serve._http_json',
                                 return_value={'error': 'boom'}):
            self.assertIn('boom', ex('list_capability_handles', {}))

    def test_browser_act_branches(self):
        ex = content._make_browser_act_executor('cora', 'k', 'sb')
        with unittest.mock.patch('serve._http_json',
                                 return_value='not-a-dict'):
            self.assertIn('unexpected', ex('browser_act', {}))
        with unittest.mock.patch('serve._http_json',
                                 return_value={'allowed': True,
                                               'error': 'boom'}):
            self.assertIn('failed', ex('browser_act', {}))
        with unittest.mock.patch('serve._http_json',
                                 return_value={'allowed': True, 'ok': False}):
            self.assertIn('sandbox', ex('browser_act', {}))
        with unittest.mock.patch('serve._http_json',
                                 return_value={
                                     'allowed': True, 'ok': True, 'url': 'u',
                                     'fields': [{'tag': 'input'}],
                                     'links': [{'text': 't', 'href': 'h'}],
                                     'screenshotBase64': 'x'}), \
             unittest.mock.patch('serve.wrap_external_content',
                                 return_value=('w', 'n', 't', 'instr')):
            out = ex('browser_act', {'op': 'click', 'selector': '#a',
                                     'credentialHandle': 'hh',
                                     'credentialField': 'pw',
                                     'pageUrl': 'u'})
        self.assertIsInstance(out, str)

    def test_library_tools(self):
        ex = content._make_library_tools_executor('cora')
        with unittest.mock.patch('serve._requester_villages',
                                 return_value=None), \
             unittest.mock.patch('serve._library_search_matches',
                                 return_value=[{'path': 'p', 'size': 1,
                                                'snippet': 's'}]), \
             unittest.mock.patch('serve.log_action'):
            out = ex('search_library', {'query': 'q'})
        self.assertIn('p', out)
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve._safe_library_path',
                                 return_value=None):
            out = ex('read_library_file',
                     {'path': 'p', 'offset': 'abc', 'limit': 'xyz'})
        self.assertIn('Not found', out)

    def test_skill_publisher_http_error(self):
        ex = content._make_skill_publisher('cora', 'k')
        args = {'slug': 'my-skill', 'title': 'T', 'sourceTask': 'st',
                'interface': 'i',
                'body': ('## Purpose\np\n## Key facts\nf\n'
                         '## Sources\ns\n## Lessons learned\nl')}
        with unittest.mock.patch('serve._http_json',
                                 side_effect=RuntimeError('boom')), \
             unittest.mock.patch('serve.log_action'):
            out = ex('publish_skill', args)
        self.assertIn('failed', out)

    def test_skill_publisher_unknown_tool_raises(self):
        ex = content._make_skill_publisher('cora', 'k')
        with self.assertRaises(ValueError):
            ex('some_other_tool', {})

    def test_google_tools(self):
        ex = content._make_google_tools_executor()
        with unittest.mock.patch('serve.wrap_external_content',
                                 return_value=('w', 'n', 't', 'instr')):
            with unittest.mock.patch(
                    'serve._google_call',
                    side_effect=[({'messages': [{'id': 'm1'}]}, None),
                                 ({}, 'err')]):
                out = ex('search_gmail_messages', {'query': 'q'})
            self.assertIsInstance(out, str)
            with unittest.mock.patch('serve._google_call',
                                     return_value=({}, 'err')):
                self.assertIn('Could not read the message',
                              ex('read_gmail_message',
                                 {'message_id': 'm1'}))
            with unittest.mock.patch(
                    'serve._google_call',
                    return_value=({'payload': {'parts': [
                        {'mimeType': 'text/plain',
                         'body': {'data': 'aGk='}}]}}, None)):
                self.assertIsInstance(ex('read_gmail_message',
                                         {'message_id': 'm1'}), str)
            with unittest.mock.patch('serve._google_call',
                                     return_value=({}, 'err')):
                self.assertIn('Could not create the draft',
                              ex('create_gmail_draft',
                                 {'to': 'a', 'subject': 's'}))
                self.assertIn('Could not read the doc',
                              ex('read_google_doc',
                                 {'document_id': 'd'}))
                self.assertIn('Could not create the doc',
                              ex('create_google_doc', {'title': 't'}))

    def test_gmail_decode_bad(self):
        self.assertEqual(content._gmail_base64url_decode('abc'), '')

    def test_spike_budget_gate(self):
        with unittest.mock.patch('serve._task_budget_exhausted',
                                 return_value=True), \
             unittest.mock.patch('serve._task_budget_spent',
                                 return_value=(1.0, 2)), \
             unittest.mock.patch('serve.get_or_create_agent_key',
                                 return_value='k'), \
             unittest.mock.patch('sim._store_content_result') as store:
            content._run_spike_content(
                _snapshot(), 'cora', {'id': 't1', 'title': 'T'})
        self.assertTrue(store.call_args[0][1].get('budgetExhausted'))


class CodingAndReviewTest(_Base):
    def _coding(self, task, responses, **extra):
        with unittest.mock.patch('serve._http_json',
                                 side_effect=_http_router(responses)), \
             unittest.mock.patch.object(content, '_review_sandbox_id',
                                        return_value='sb'), \
             unittest.mock.patch('serve._coding_tier_slug',
                                 return_value='code'), \
             unittest.mock.patch('serve.get_or_create_agent_key',
                                 return_value='k'), \
             unittest.mock.patch('sim._store_content_result') as store:
            content._run_coding_content(_snapshot(), 'cora', task)
        return store

    def test_coding_exit_nonzero_detail(self):
        cmd = "cat > index.html << 'EOF'\n<html></html>\nEOF"
        store = self._coding(
            {'id': 't1', 'title': 'T', 'projectLabel': 'P'},
            [{'reply': cmd},
             {'allowed': True, 'exitCode': 1, 'timedOut': False,
              'stderr': 'err line\n', 'stdout': 'out line\n'}])
        result = store.call_args[0][1]
        self.assertFalse(result['ok'])
        self.assertIn('stderr:', result['note'])
        self.assertIn('stdout:', result['note'])

    def test_coding_autolink_budget(self):
        cmd = "cat > app.js << 'EOF'\nconsole.log(1)\nEOF"
        with unittest.mock.patch('serve._http_json',
                                 side_effect=_http_router(
                                     [{'reply': cmd},
                                      {'allowed': True, 'exitCode': 0}])), \
             unittest.mock.patch.object(content, '_review_sandbox_id',
                                        return_value='sb'), \
             unittest.mock.patch('serve._coding_tier_slug',
                                 return_value='code'), \
             unittest.mock.patch('serve.get_or_create_agent_key',
                                 return_value='k'), \
             unittest.mock.patch.object(content, '_extract_written_js_files',
                                        return_value=['app.js']), \
             unittest.mock.patch.object(content,
                                        '_extract_written_python_files',
                                        return_value=[]), \
             unittest.mock.patch.object(content, '_find_unlinked_js_files',
                                        return_value=['app.js']), \
             unittest.mock.patch.object(
                 content, '_auto_link_js_files',
                 return_value={'ok': False, 'budgetExhausted': True,
                               'taskSpendUsd': 1.0,
                               'taskSpendAttempts': 2,
                               'note': 'paused'}), \
             unittest.mock.patch('sim._store_content_result') as store:
            content._run_coding_content(
                _snapshot(), 'cora',
                {'id': 't1', 'title': 'T', 'projectLabel': 'P'})
        self.assertTrue(store.call_args[0][1].get('budgetExhausted'))

    def _review(self, responses, task=None, screenshot=None):
        task = task or {'id': 't1', 'title': 'T'}
        patches = [
            unittest.mock.patch('serve._http_json',
                                side_effect=_http_router(responses)),
            unittest.mock.patch.object(content, '_review_sandbox_id',
                                       return_value='sb'),
            unittest.mock.patch.object(content, '_gather_unified_context',
                                       return_value='ctx'),
            unittest.mock.patch.object(content, '_agent_name',
                                       return_value='Cora'),
            unittest.mock.patch('serve._coding_tier_slug',
                                return_value='tier'),
            unittest.mock.patch('serve._vision_tier_slug',
                                return_value='v'),
            unittest.mock.patch('serve.get_or_create_agent_key',
                                return_value='k'),
            unittest.mock.patch('serve._jev_model', return_value='m'),
            unittest.mock.patch('serve._call_openrouter_decision_sync',
                                return_value={'choice': {'id': 'clean'}}),
            unittest.mock.patch('serve._jev_choice',
                                return_value=('clean', None, None)),
        ]
        if screenshot is not None:
            patches.append(unittest.mock.patch.object(
                content, '_review_screenshot', return_value=screenshot))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        content._run_review_content(_snapshot(), 'cora', task)

    def test_review_structured_sendback(self):
        verdict = ('Bad. {"verdict": "send_back", "summary": "s", '
                   '"checks": ["c"], "risks": ["r"]}')
        store = sim._store_content_result
        with unittest.mock.patch('sim._store_content_result') as store:
            self._review([{'reply': verdict}, {'imageBase64': 'x'},
                          {'reply': 'visual'}, {}])
        result = store.call_args[0][1]
        self.assertEqual(result['peerSummary'], 's')
        self.assertEqual(result['peerChecks'], ['c'])
        self.assertEqual(result['peerRisks'], ['r'])
        self.assertIn('queued a fix', result['note'])

    def test_review_chat_budget_stores_budget_result(self):
        with unittest.mock.patch('sim._store_content_result') as store:
            self._review([{'budgetExhausted': True, 'taskSpendUsd': 1.0,
                           'taskSpendAttempts': 2}])
        self.assertTrue(store.called)
        self.assertTrue(store.call_args[0][1].get('budgetExhausted'))

    def test_review_visual_budget(self):
        with unittest.mock.patch('sim._store_content_result') as store:
            self._review(
                [{'reply': 'Looks ok.'}, {}],
                screenshot={'ok': False, 'budgetExhausted': True,
                            'taskSpendUsd': 1.0, 'taskSpendAttempts': 2,
                            'note': 'b'})
        result = store.call_args[0][1]
        self.assertTrue(result.get('budgetExhausted'))


class SpikeContentTest(_Base):
    def _run_spike(self, task_title='what should we explore next?',
                   extra_patches=None, http=None):
        task = {'id': 't1', 'title': task_title}
        captured = {}

        def fake_loop(model, messages, tools, execute_tool, **kwargs):
            captured['execute_tool'] = execute_tool
            captured['force_first_tool'] = kwargs.get('force_first_tool')
            transcript = list(messages) + [
                {'role': 'assistant',
                 'tool_calls': [{'id': 'c1', 'function': {
                     'name': 'browse_page', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'c1',
                 'content': 'PAGE TEXT'},
            ]
            return ('done', transcript)

        patches = [
            unittest.mock.patch.object(content, '_spike_file_issue_wish',
                                       return_value=None),
            unittest.mock.patch('serve.get_or_create_agent_key',
                                return_value='k'),
            unittest.mock.patch('serve._resolve_model_tier',
                                return_value='mid'),
            unittest.mock.patch('serve._task_budget_exhausted',
                                return_value=False),
            unittest.mock.patch('serve._call_agent_tool_loop',
                                side_effect=fake_loop),
            unittest.mock.patch('serve._http_json',
                                side_effect=http or (lambda *a, **k: {})),
            unittest.mock.patch('serve.wrap_external_content',
                                return_value=('w', 'n', 't', 'instr')),
            unittest.mock.patch('serve.log_action'),
            unittest.mock.patch('serve._local_file_tool',
                                return_value='ok'),
            unittest.mock.patch('serve._read_store_tool', return_value='ok'),
            unittest.mock.patch('serve._app_script_tool', return_value='ok'),
            unittest.mock.patch.object(sim, 'village_of_agent',
                                       return_value='main'),
            unittest.mock.patch('serve._call_openrouter_sync',
                                return_value=_completion('final')),
            unittest.mock.patch('sim._store_content_result'),
        ]
        patches.extend(extra_patches or [])
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        content._run_spike_content(_snapshot(), 'cora', task)
        return captured

    def test_execute_tool_dispatch_arms(self):
        captured = self._run_spike()
        ex = captured['execute_tool']
        ex('browser_act', {'op': 'navigate', 'url': 'u'})
        ex('list_capability_handles', {})
        ex('publish_skill', {})
        ex('write_spike_report', {'summary': 's'})
        ex('local_file', {'path': 'x'})
        ex('read_store', {'path': 'y'})
        ex('app_script', {'code': 'z'})

    def test_first_tool_ais_and_market(self):
        names = ('_spike_wants_internal_review', '_spike_wants_x_trending',
                 '_spike_wants_linkedin_search', '_spike_wants_github',
                 '_spike_wants_ais_feed', '_spike_wants_rss',
                 '_spike_wants_market')
        for target in ('_spike_wants_ais_feed', '_spike_wants_market'):
            patches = [unittest.mock.patch.object(
                content, n, return_value=(n == target)) for n in names]
            self._run_spike(extra_patches=patches)

    def test_epilogue_probe_error(self):
        self._run_spike(extra_patches=[
            unittest.mock.patch('serve._safe_library_path',
                                side_effect=RuntimeError('boom'))])

    def test_workroom_context_paths(self):
        task = {'id': 't1', 'title': 'T', 'projectLabel': 'P',
                'instructions': None}
        with unittest.mock.patch('serve._http_json',
                                 return_value={'allowed': True,
                                               'stdout': 'files'}), \
             unittest.mock.patch('serve.get_or_create_agent_key',
                                 return_value='k'), \
             unittest.mock.patch.object(content, '_review_sandbox_id',
                                        return_value='sb'), \
             unittest.mock.patch.object(content, '_design_context_for_task',
                                        return_value='design'), \
             unittest.mock.patch.object(content, '_run_coding_content') as run:
            content._run_workroom_content(
                _snapshot(), 'cora', task, base_ctx='ctx')
        self.assertTrue(run.called)
        self.assertTrue(run.call_args.kwargs['base_ctx'].startswith('design'))
