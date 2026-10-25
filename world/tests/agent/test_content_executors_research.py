"""Research, media, skill-review, and pipeline-step content executors."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import content  # noqa: E402


def _snapshot(**over):
    base = {
        'agentRoster': [{'id': 'cora', 'name': 'Cora'}, {'id': 'ben', 'name': 'Ben'}],
        'agents': {'cora': {'name': 'Cora'}, 'ben': {'name': 'Ben'}},
        'researchTopics': [],
        'pipelines': [],
        'products': {},
    }
    base.update(over)
    return base


def _store():
    seen = {}
    patcher = mock.patch('sim._store_content_result',
                         lambda task_id, result: seen.__setitem__(task_id, result))
    patcher.start()
    return seen


def _base_mocks():
    patchers = [
        mock.patch.object(serve, 'SELF_BASE_URL', 'http://x'),
        mock.patch.object(serve, 'get_or_create_agent_key', return_value='key-123'),
        mock.patch.object(serve, '_http_json', return_value={'ok': True}),
    ]
    for p in patchers:
        p.start()
    return patchers


class _ExecutorTestCase(unittest.TestCase):
    """The helper methods above start mock patches via .start() that are never
    stopped by the test body (they only need to live for that one test call).
    Without a cleanup, those patches leak across the whole test process and
    contaminate other test modules (a stray _resolve_model_tier patch here
    broke test_spike_content's no-tier short-circuit when run after this
    module). Stopping everything still active at the end of each test keeps
    this module hermetic without touching every call site."""

    def tearDown(self):
        mock.patch.stopall()


class ResearchContent(_ExecutorTestCase):
    def _run(self, task, topic=None, browse=None, resolve=None, chat=None):
        snapshot = _snapshot(researchTopics=[topic] if topic else [])
        seen = _store()
        _base_mocks()
        if browse is not None:
            mock.patch.object(serve, '_http_json', side_effect=browse).start()
        mock.patch.object(serve, '_resolve_model_tier', return_value=resolve).start()
        content._run_research_content(snapshot, 'cora', task)
        return seen[task['id']]

    def test_no_matching_topic_falls_back_to_placeholder(self):
        result = self._run({'id': 'r1', 'research': {'topicId': 'ghost'}})
        self.assertIn('no matching topic', result['note'])

    def test_no_matching_topic_iterates_existing_topics(self):
        # researchTopics non-empty but none matches the scheduled topicId --
        # exercises the loop's non-match arc (the placeholder path with an
        # empty list never runs the loop body at all).
        topic = {'id': 't-other', 'topic': 'Unrelated'}
        result = self._run({'id': 'r9', 'research': {'topicId': 'ghost'}}, topic=topic)
        self.assertIn('no matching topic', result['note'])

    def test_crawl_keeps_changed_and_new_pages_and_synthesizes(self):
        topic = {'id': 't1', 'topic': 'DreyX', 'startUrl': 'https://dreyx.com',
                 'seenUrls': ['https://dreyx.com'], 'pageKeyword': 'DreyX'}
        pages = {
            'https://dreyx.com': {'url': 'https://dreyx.com', 'text': 'DreyX home',
                                  'lastModified': 999, 'allowed': True,
                                  'links': [{'url': 'https://dreyx.com/about', 'text': 'about'}]},
            'https://dreyx.com/about': {'url': 'https://dreyx.com/about', 'text': 'DreyX about page',
                                        'allowed': True, 'lastModified': 999, 'links': []},
        }
        calls = {'browse': 0, 'save': 0, 'chat': 0}
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/browse':
                calls['browse'] += 1
                return pages[body['url']]
            if path == '/api/sandbox-save-page':
                calls['save'] += 1
                return {'allowed': True, 'ok': True}
            if path.startswith('/api/library/file?'):
                return {'content': 'old skill content'}
            if path == '/api/library/file':
                return {'ok': True}
            if path == '/api/chat':
                calls['chat'] += 1
                return {'reply': 'The distilled skill file.'}
            return {'ok': True}
        task = {'id': 'r2', 'research': {'topicId': 't1', 'since': 100}}
        result = self._run(task, topic=topic, browse=side_effect,
                           resolve='mid-tier-slug')
        self.assertIn('collected 2 new page(s)', result['note'])
        self.assertEqual(result['seenUrls'], ['https://dreyx.com', 'https://dreyx.com/about'])
        self.assertEqual((calls['browse'], calls['save'], calls['chat']), (2, 2, 1))

    def test_crawl_skips_errors_denied_and_unchanged_dedup(self):
        topic = {'id': 't2', 'topic': 'X', 'startUrl': 'https://a.com',
                 'seenUrls': ['https://a.com'], 'pageKeyword': ''}
        pages = {
            'https://a.com': {'url': 'https://a.com', 'text': 'A', 'lastModified': 50,
                              'allowed': True, 'links': [{'url': 'https://b.com', 'text': 'b'},
                                                         {'url': 'https://c.com', 'text': 'c'}]},
            'https://b.com': {'url': 'https://b.com', 'error': 'boom', 'links': []},
            'https://c.com': {'url': 'https://c.com', 'text': 'C', 'lastModified': 999,
                              'allowed': False, 'links': []},
        }
        calls = {'save': 0}
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/browse':
                return pages[body['url']]
            if path == '/api/sandbox-save-page':
                calls['save'] += 1
                return {'allowed': True, 'ok': True}
            return {'ok': True}
        task = {'id': 'r3', 'research': {'topicId': 't2', 'since': 100}}
        result = self._run(task, topic=topic, browse=side_effect, resolve='mid-tier-slug')
        self.assertIn('nothing new since', result['note'])
        self.assertEqual(result['seenUrls'], ['https://a.com', 'https://b.com', 'https://c.com'])
        self.assertEqual(calls['save'], 0)

    def test_page_keyword_filters_kept_pages(self):
        topic = {'id': 't3', 'topic': 'Y', 'startUrl': 'https://a.com',
                 'seenUrls': [], 'pageKeyword': 'target'}
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/browse':
                return {'url': body['url'], 'text': 'no keyword here', 'lastModified': 999,
                        'allowed': True, 'links': []}
            if path == '/api/sandbox-save-page':
                return {'allowed': True, 'ok': True}
            return {'ok': True}
        task = {'id': 'r4', 'research': {'topicId': 't3', 'since': 0}}
        result = self._run(task, topic=topic, browse=side_effect, resolve='mid-tier-slug')
        self.assertIn('nothing new since', result['note'])  # page dropped by keyword filter

    def test_link_keyword_prunes_frontier(self):
        topic = {'id': 't4', 'topic': 'Z', 'startUrl': 'https://a.com',
                 'seenUrls': [], 'pageKeyword': '', 'linkKeyword': 'keepme'}
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/browse':
                if body['url'] == 'https://a.com':
                    return {'url': 'https://a.com', 'text': 'A', 'lastModified': 999,
                            'allowed': True,
                            'links': [{'url': 'https://b.com', 'text': 'not matching'},
                                      {'url': 'https://c.com', 'text': 'keepme'},
                                      {'url': 'https://a.com', 'text': 'dup'}]}
                return {'url': body['url'], 'text': 'C', 'lastModified': 999, 'allowed': True,
                        'links': []}
            return {'ok': True}
        task = {'id': 'r5', 'research': {'topicId': 't4', 'since': 0}}
        result = self._run(task, topic=topic, browse=side_effect, resolve='mid-tier-slug')
        # Only https://a.com and https://c.com crawled (the b.com link pruned).
        self.assertEqual(result['seenUrls'], ['https://a.com', 'https://c.com'])

    def test_no_model_tier_reports_synthesis_failure(self):
        topic = {'id': 't5', 'topic': 'W', 'startUrl': 'https://a.com',
                 'seenUrls': [], 'pageKeyword': ''}
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/browse':
                return {'url': body['url'], 'text': 'A', 'lastModified': 999,
                        'allowed': True, 'links': []}
            if path == '/api/sandbox-save-page':
                return {'allowed': True, 'ok': True}
            return {'ok': True}
        task = {'id': 'r6', 'research': {'topicId': 't5', 'since': 0}}
        result = self._run(task, topic=topic, browse=side_effect, resolve=None)
        self.assertIn('synthesis failed (no model tier)', result['note'])

    def test_chat_exception_and_empty_reply_handled(self):
        topic = {'id': 't6', 'topic': 'V', 'startUrl': 'https://a.com',
                 'seenUrls': [], 'pageKeyword': ''}
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/browse':
                return {'url': body['url'], 'text': 'A', 'lastModified': 999,
                        'allowed': True, 'links': []}
            if path == '/api/sandbox-save-page':
                return {'allowed': True, 'ok': True}
            if path == '/api/chat':
                raise RuntimeError('network blip')
            return {'ok': True}
        task = {'id': 'r7', 'research': {'topicId': 't6', 'since': 0}}
        result = self._run(task, topic=topic, browse=side_effect, resolve='mid-tier-slug')
        self.assertIn('didn\'t produce anything usable', result['note'])

    def test_empty_reply_reports_collected_not_usable(self):
        topic = {'id': 't7', 'topic': 'U', 'startUrl': 'https://a.com',
                 'seenUrls': [], 'pageKeyword': ''}
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/browse':
                return {'url': 'https://a.com', 'text': 'A', 'lastModified': 999,
                        'allowed': True, 'links': []}
            if path == '/api/sandbox-save-page':
                return {'allowed': True, 'ok': True}
            if path == '/api/chat':
                return {'reply': ''}
            return {'ok': True}
        task = {'id': 'r8', 'research': {'topicId': 't7', 'since': 0}}
        result = self._run(task, topic=topic, browse=side_effect, resolve='mid-tier-slug')
        self.assertIn('didn\'t produce anything usable', result['note'])

class MediaContent(_ExecutorTestCase):
    def test_media_no_feeds(self):
        seen = _store()
        _base_mocks()
        with mock.patch.object(serve, '_http_json', return_value={'content': None}):
            content._run_media_content(_snapshot(), 'cora', {'id': 'm1'})
        self.assertIn('No feeds configured yet', seen['m1']['note'])
        self.assertIs(seen['m1'].get('noop'), True,
                      'no feeds is a legitimate no-op, not a refused deliverable')

    def test_media_browse_not_approved(self):
        seen = _store()
        _base_mocks()
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path.startswith('/api/library/file?'):
                return {'content': 'https://news.com/a\nhttps://news.com/b'}
            if path == '/api/browse':
                return {'allowed': False, 'reason': 'off-allowlist'}
            return {'ok': True}
        with mock.patch.object(serve, '_http_json', side_effect=side_effect):
            content._run_media_content(_snapshot(), 'cora', {'id': 'm2'})
        self.assertIn('wasn\'t approved', seen['m2']['note'])

    def test_media_render_fallback_upgrades_thin_page(self):
        seen = _store()
        _base_mocks()
        calls = {'n': 0}
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            calls['n'] += 1
            if path.startswith('/api/library/file?'):
                return {'content': 'https://news.com/a'}
            if path == '/api/browse':
                if calls['n'] == 2:  # first browse (n=1 was the feeds GET)
                    return {'allowed': True, 'text': 'short'}
                return {'allowed': True, 'text': 'a much longer page with plenty of real content to read'}
            if path == '/api/chat':
                return {'reply': 'A real summary.'}
            return {'ok': True}
        with mock.patch.object(serve, '_http_json', side_effect=side_effect), \
             mock.patch.object(serve, '_resolve_model_tier', return_value='t'):
            content._run_media_content(_snapshot(), 'cora', {'id': 'm3'})
        self.assertIn('Filed a digest on https://news.com/a', seen['m3']['note'])
        self.assertTrue(str(seen['m3'].get('libraryPath') or '').startswith('media/digests/'),
                        'the digest file is the deliverable -- its path must be recorded')
        self.assertEqual(calls['n'], 5)  # file GET + 2 browse + chat + file POST

    def test_media_long_page_skips_render_retry(self):
        seen = _store()
        _base_mocks()
        calls = {'browse': 0}
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path.startswith('/api/library/file?'):
                return {'content': 'https://news.com/a'}
            if path == '/api/browse':
                calls['browse'] += 1
                return {'allowed': True, 'text': 'x' * 400}
            if path == '/api/chat':
                return {'reply': 'A real summary.'}
            return {'ok': True}
        with mock.patch.object(serve, '_http_json', side_effect=side_effect), \
             mock.patch.object(serve, '_resolve_model_tier', return_value='t'):
            content._run_media_content(_snapshot(), 'cora', {'id': 'm9'})
        self.assertIn('Filed a digest on https://news.com/a', seen['m9']['note'])
        self.assertEqual(calls['browse'], 1)  # already >=300 chars: render retry skipped

    def test_media_empty_page(self):
        seen = _store()
        _base_mocks()
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path.startswith('/api/library/file?'):
                return {'content': 'https://news.com/a'}
            if path == '/api/browse':
                return {'allowed': True, 'text': ''}
            return {'ok': True}
        with mock.patch.object(serve, '_http_json', side_effect=side_effect):
            content._run_media_content(_snapshot(), 'cora', {'id': 'm4'})
        self.assertIn('came back empty', seen['m4']['note'])

    def test_media_no_model_tier(self):
        seen = _store()
        _base_mocks()
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path.startswith('/api/library/file?'):
                return {'content': 'https://news.com/a'}
            if path == '/api/browse':
                return {'allowed': True, 'text': 'some real content here'}
            return {'ok': True}
        with mock.patch.object(serve, '_http_json', side_effect=side_effect), \
             mock.patch.object(serve, '_resolve_model_tier', return_value=None):
            content._run_media_content(_snapshot(), 'cora', {'id': 'm5'})
        self.assertIn('couldn\'t summarize it', seen['m5']['note'])

    def test_media_summary_failure(self):
        seen = _store()
        _base_mocks()
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path.startswith('/api/library/file?'):
                return {'content': 'https://news.com/a'}
            if path == '/api/browse':
                return {'allowed': True, 'text': 'some real content here'}
            if path == '/api/chat':
                return {'reply': ''}
            return {'ok': True}
        with mock.patch.object(serve, '_http_json', side_effect=side_effect), \
             mock.patch.object(serve, '_resolve_model_tier', return_value='t'):
            content._run_media_content(_snapshot(), 'cora', {'id': 'm6'})
        self.assertIn('couldn\'t summarize it', seen['m6']['note'])

    def test_media_skips_comment_lines_in_feeds(self):
        seen = _store()
        _base_mocks()
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path.startswith('/api/library/file?'):
                return {'content': '# comment\n\nhttps://news.com/a'}
            if path == '/api/browse':
                return {'allowed': True, 'text': 'content'}
            if path == '/api/chat':
                return {'reply': 'summary'}
            return {'ok': True}
        with mock.patch.object(serve, '_http_json', side_effect=side_effect), \
             mock.patch.object(serve, '_resolve_model_tier', return_value='t'):
            content._run_media_content(_snapshot(), 'cora', {'id': 'm7'})
        self.assertIn('Filed a digest', seen['m7']['note'])

class SkillReviewContent(_ExecutorTestCase):
    def _run(self, listing_files, file_content=None, decision=('keep', 0.9, 0.01)):
        seen = _store()
        _base_mocks()
        calls = {'reject': 0, 'promote': 0, 'reject_call': 0}
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path.split('?')[0] == '/api/library':
                return {'files': listing_files}
            if path.startswith('/api/library/file?'):
                return file_content
            if path == '/api/library/file':
                return {'ok': True}
            if path == '/api/library/reject':
                calls['reject'] += 1
                return {'ok': True}
            if path == '/api/library/promote':
                calls['promote'] += 1
                return {'ok': True}
            return {'ok': True}
        with mock.patch.object(serve, '_http_json', side_effect=side_effect), \
             mock.patch.object(serve, '_call_openrouter_decision_sync', return_value={'x': 1}), \
             mock.patch.object(serve, '_jev_choice', return_value=decision):
            content._run_skill_review_content(_snapshot(), 'cora', {'id': 's1'})
        return seen['s1'], calls

    def test_no_pending_files(self):
        result, _ = self._run([])
        self.assertIn('nothing waiting', result['note'])

    def test_file_without_content_is_skipped(self):
        result, calls = self._run([{'path': 'pending_review/skills/a.md'}],
                                  file_content={'nope': True})
        self.assertIn('0 promoted, 0 rejected', result['note'])
        self.assertEqual(calls['promote'], 0)

    def test_reject_writes_annotation_and_rejects(self):
        result, calls = self._run([{'path': 'pending_review/skills/a.md'}],
                                  file_content={'content': 'bad skill'},
                                  decision=('reject', 0.9, 0.01))
        self.assertIn('0 promoted, 1 rejected', result['note'])
        self.assertEqual(calls['reject'], 1)

    def test_keep_promotes(self):
        result, calls = self._run([{'path': 'pending_review/skills/a.md'}],
                                  file_content={'content': 'good skill'},
                                  decision=('keep', 0.9, 0.01))
        self.assertIn('1 promoted, 0 rejected', result['note'])
        self.assertEqual(calls['promote'], 1)

    def test_jev_exception_falls_back_to_keep(self):
        # The Jev judge being unreachable must deterministically keep, never
        # crash the sweep. Self-contained (the previous version relied on
        # leaked mocks whose _http_json had already fallen back to {'ok': True},
        # so the pending-file listing was empty and the Jev loop never ran).
        seen = _store()
        _base_mocks()
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path.split('?')[0] == '/api/library':
                return {'files': [{'path': 'pending_review/skills/a.md'}]}
            if path.startswith('/api/library/file?'):
                return {'content': 'skill'}
            return {'ok': True}
        with mock.patch.object(serve, '_http_json', side_effect=side_effect), \
             mock.patch.object(serve, '_call_openrouter_decision_sync',
                               side_effect=RuntimeError('judge down')):
            content._run_skill_review_content(_snapshot(), 'cora', {'id': 's2'})
        self.assertIn('1 promoted, 0 rejected', seen['s2']['note'])

class PipelineStepContent(_ExecutorTestCase):
    def _run(self, task, steps=None, tools_side_effect=None):
        snapshot = _snapshot(pipelines=[{'id': 'pl', 'steps': steps or []}])
        seen = _store()
        _base_mocks()
        if tools_side_effect is not None:
            mock.patch.object(serve, '_treg_call', side_effect=tools_side_effect).start()
            mock.patch.object(serve, '_apify_call', side_effect=tools_side_effect).start()
            mock.patch.object(serve, '_pixellab_call', side_effect=tools_side_effect).start()
        content._run_pipeline_step_content(snapshot, 'cora', task)
        return seen[task['id']]

    def test_no_matching_pipeline(self):
        result = self._run({'id': 'p1', 'pipelineStep': {'pipelineId': 'nope', 'stepIndex': 0}})
        self.assertIn('no matching pipeline record', result['note'])

    def test_step_index_out_of_range(self):
        result = self._run({'id': 'p2', 'pipelineStep': {'pipelineId': 'pl', 'stepIndex': 5}},
                           steps=[{'name': 'one'}])
        self.assertIn('no matching pipeline record', result['note'])

    def test_treg_tool_runs(self):
        result = self._run({'id': 'p3', 'pipelineStep': {'pipelineId': 'pl', 'stepIndex': 0}},
                           steps=[{'tool': 'x_trending_topics', 'args': {'woeid': 1}}],
                           tools_side_effect=lambda *a, **k: ({'trends': []}, None))
        self.assertIn('trends', result['note'])

    def test_apify_tool_runs(self):
        snapshot = _snapshot(pipelines=[{'id': 'pl', 'steps': [
            {'tool': 'apify_get_dataset_items', 'args': {'datasetId': 'd'}}]}])
        seen = _store()
        _base_mocks()
        with mock.patch.object(serve, '_api_execute', return_value={
                'ok': True, 'data': {'items': []}, 'textForModel': '<<<EXTERNAL_DATA', 'modelInstruction': ''}):
            content._run_pipeline_step_content(snapshot, 'cora',
                                               {'id': 'p4', 'pipelineStep': {'pipelineId': 'pl', 'stepIndex': 0}})
        self.assertIn('items', seen['p4']['note'])

    def test_pixellab_tool_runs(self):
        snapshot = _snapshot(pipelines=[{'id': 'pl', 'steps': [
            {'tool': 'generate_pixel_character', 'args': {'description': 'a hero'}}]}])
        seen = _store()
        _base_mocks()
        with mock.patch.object(serve, '_api_execute', return_value={
                'ok': True, 'data': {'rotation_urls': {'up': 'u'}},
                'ids': {'character_id': 'c'}, 'usd': None, 'textForModel': '', 'modelInstruction': ''}):
            content._run_pipeline_step_content(snapshot, 'cora',
                                               {'id': 'p5', 'pipelineStep': {'pipelineId': 'pl', 'stepIndex': 0}})
        self.assertIn('character_id', seen['p5']['note'])

    def test_unknown_tool_reports(self):
        result = self._run({'id': 'p6', 'pipelineStep': {'pipelineId': 'pl', 'stepIndex': 0}},
                           steps=[{'tool': 'not_a_tool', 'args': {}}])
        self.assertIn('unknown tool', result['note'])

    def test_tool_exception_reports(self):
        snapshot = _snapshot(pipelines=[{'id': 'pl', 'steps': [{'tool': 'x_trending_topics', 'args': {}}]}])
        seen = _store()
        _base_mocks()
        with mock.patch.object(serve, '_treg_call', side_effect=RuntimeError('down')):
            content._run_pipeline_step_content(snapshot, 'cora', {'id': 'p7', 'pipelineStep': {'pipelineId': 'pl', 'stepIndex': 0}})
        self.assertIn('Pipeline step tool failed', seen['p7']['note'])

    def test_no_tool_falls_back_to_research_bare(self):
        snapshot = _snapshot(pipelines=[{'id': 'pl', 'steps': [{'name': 'just a step'}]}])
        seen = _store()
        _base_mocks()
        with mock.patch.object(serve, '_http_json', return_value={'failedStep': None, 'ok': True}):
            content._run_pipeline_step_content(snapshot, 'cora',
                                               {'id': 'p8', 'pipelineStep': {'pipelineId': 'pl', 'stepIndex': 0}})
        self.assertIn('Reviewed and logged', seen['p8']['note'])
