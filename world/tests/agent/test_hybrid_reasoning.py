"""The hybrid reasoning protocol (15_body_02_hybrid, from arxiv 2610.18861).

HybridAgents split reasoning between a LATENT mode (private thinking whose
output is distilled into per-task working memory instead of being emitted as
visible text) and TEXT mode (tool use, collaboration). The tank emulates the
paper's behavioral contract at the orchestration layer:

  - _hybrid_task_eligible: the mode policy v0 (research/distill/spike/note
    lanes plan latently; code/bug/meeting/review stay text).
  - _hybrid_memory_append/_hybrid_memory_digest: the working-memory store
    and the digest compiler (the HybridLink analog).
  - _hybrid_prepare (wired into _server_content_dispatcher): runs the
    latent pre-pass and hands the memory to the executor.
  - /api/chat injection: every later call for the task starts from the
    agent's own distilled notes.

Hermetic: no live network; the latent pass's /api/chat call is faked via
serve._http_json; the chat-handler injection is tested through the handler
function directly with TestClient-style callables faked at the boundary.
"""

import importlib
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402  (sys.path insert above is the repo test convention)
import content  # noqa: E402


def _fresh_task(**over):
    task = {'id': 'task-9001', 'title': 'Distill recent think tank knowledge',
            'taskType': 'code', 'deliverable': 'note', 'room': 'library'}
    task.update(over)
    return task


class HybridPolicy(unittest.TestCase):
    def test_research_lane_is_latent(self):
        for task in (_fresh_task(distill=True),
                     _fresh_task(research=True),
                     _fresh_task(taskType='research'),
                     _fresh_task(taskType='spike'),
                     _fresh_task(taskType='report'),
                     _fresh_task(taskType='code', deliverable='note')):
            eligible, _reason = content._hybrid_task_eligible(task)
            self.assertTrue(eligible, task)

    def test_code_bug_meeting_review_stay_text(self):
        for task in (_fresh_task(taskType='code', deliverable='code'),
                     _fresh_task(taskType='bug', deliverable='code'),
                     _fresh_task(taskType='meeting'),
                     _fresh_task(taskType='review'),
                     _fresh_task(taskType='code', pipelineStep='lint')):
            eligible, _reason = content._hybrid_task_eligible(task)
            self.assertFalse(eligible, task)

    def test_overrides(self):
        self.assertTrue(content._hybrid_task_eligible(
            _fresh_task(taskType='code', deliverable='code', hybrid=True))[0])
        self.assertFalse(content._hybrid_task_eligible(
            _fresh_task(distill=True, hybridOff=True))[0])
        self.assertFalse(content._hybrid_task_eligible(None)[0])


class HybridMemory(unittest.TestCase):
    def setUp(self):
        self._old_library = serve.LIBRARY_DIR
        serve.LIBRARY_DIR = tempfile.mkdtemp(prefix='hybrid-test-')

    def tearDown(self):
        import shutil
        shutil.rmtree(serve.LIBRARY_DIR, ignore_errors=True)
        serve.LIBRARY_DIR = self._old_library

    def test_append_then_digest(self):
        task = _fresh_task()
        self.assertTrue(content._hybrid_memory_append('ruth', task,
                                                      'plan: check wiki first'))
        digest = content._hybrid_memory_digest('ruth', task['id'])
        self.assertIn('plan: check wiki first', digest)

    def test_digest_empty_when_absent(self):
        self.assertEqual(content._hybrid_memory_digest('ruth', 'task-none'), '')

    def test_file_is_bounded_and_newest_first(self):
        task = _fresh_task()
        for i in range(40):
            content._hybrid_memory_append('ruth', task, f'note {i} ' + 'x' * 200)
        path = content._hybrid_memory_path('ruth', task['id'])
        body = open(path, encoding='utf-8').read()
        self.assertLessEqual(len(body), content._HYBRID_MEMORY_MAX_CHARS + 200)
        self.assertIn('note 39', body)  # newest survives the trim


class HybridPrepareRunsLatentPass(unittest.TestCase):
    def setUp(self):
        self._old_library = serve.LIBRARY_DIR
        serve.LIBRARY_DIR = tempfile.mkdtemp(prefix='hybrid-test-')

    def tearDown(self):
        import shutil
        shutil.rmtree(serve.LIBRARY_DIR, ignore_errors=True)
        serve.LIBRARY_DIR = self._old_library
        content._HYBRID_LAST_PLAN_AT.clear()

    def test_eligible_task_gets_private_notes_in_base_ctx(self):
        task = _fresh_task(distill=True)
        captured = {}

        def fake_http_json(method, base, path, body=None, header=None,
                           timeout=30, extra_headers=None):
            captured['body'] = body
            return {'reply': 'UNKNOWN: none. PLAN: read wiki, distill. RISK: drift.'}

        with unittest.mock.patch.object(serve, '_http_json', fake_http_json), \
             unittest.mock.patch.dict(os.environ, {'HYBRID_MODE': 'auto'}):
            base_ctx = {}
            content._hybrid_prepare({}, 'ruth', task, base_ctx)
        # the latent pass used the bypass flag so /api/chat will not echo it
        self.assertTrue(captured['body'].get('hybridBypass'))
        self.assertIn('PLAN', base_ctx['hybridMemory'])
        self.assertIn('PLAN', content._hybrid_memory_digest('ruth', task['id']))

    def test_cooldown_prevents_repeat_passes(self):
        task = _fresh_task(distill=True)
        calls = []

        def fake_http_json(*a, **k):
            calls.append(1)
            return {'reply': 'PLAN: x'}

        with unittest.mock.patch.object(serve, '_http_json', fake_http_json), \
             unittest.mock.patch.dict(os.environ, {'HYBRID_MODE': 'auto'}):
            content._hybrid_prepare({}, 'ruth', task, {})
            content._hybrid_prepare({}, 'ruth', task, {})
        self.assertEqual(len(calls), 1)

    def test_off_mode_skips_the_pass(self):
        task = _fresh_task(distill=True)
        calls = []

        def fake_http_json(*a, **k):
            calls.append(1)
            return {'reply': 'PLAN: x'}

        with unittest.mock.patch.object(serve, '_http_json', fake_http_json), \
             unittest.mock.patch.dict(os.environ, {'HYBRID_MODE': 'off'}):
            content._hybrid_prepare({}, 'ruth', task, {})
        self.assertEqual(len(calls), 0)


if __name__ == '__main__':
    unittest.main()


class ChatBridgeInjection(unittest.TestCase):
    """The /api/chat working-memory bridge: the digest is prepended as a
    system message on later calls for the task; bypass and kill-switch skip."""

    def setUp(self):
        self._old_library = serve.LIBRARY_DIR
        serve.LIBRARY_DIR = tempfile.mkdtemp(prefix='hybrid-test-')
        task = _fresh_task()
        content._hybrid_memory_append('ruth', task, 'PLAN: read wiki first')

    def tearDown(self):
        import shutil
        shutil.rmtree(serve.LIBRARY_DIR, ignore_errors=True)
        serve.LIBRARY_DIR = self._old_library

    def _run(self, body, hybrid_mode='auto', openrouter='k'):
        import asyncio
        req = unittest.mock.Mock()
        req.json = unittest.mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'k'}
        env = {'HYBRID_MODE': hybrid_mode, 'OPENROUTER_API_KEY': openrouter}
        captured = {}

        def fake_call_openrouter_sync(model, messages, max_tokens, best_of=1):
            captured['messages'] = messages
            return {'choices': [{'message': {'content': 'ok'}}]}

        with unittest.mock.patch.dict(os.environ, env), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', openrouter, create=True), \
             unittest.mock.patch.object(serve, 'verify_session', lambda c: False), \
             unittest.mock.patch.object(serve, 'verify_agent_key', lambda a, k: True), \
             unittest.mock.patch.object(serve, '_input_guard_violation', lambda m: None), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync', fake_call_openrouter_sync), \
             unittest.mock.patch.object(serve, 'credits_pause_active', lambda: False), \
             unittest.mock.patch.object(serve, '_agent_spend_cap_exceeded', lambda a: False), \
             unittest.mock.patch.object(serve, '_director_budget_gate', lambda *a, **k: None), \
             unittest.mock.patch.object(serve, 'log_action', lambda *a, **k: None):
            resp = asyncio.run(serve.chat(req))
        return resp, captured

    def test_injects_working_memory_as_system_message(self):
        resp, captured = self._run({'model': 'm', 'agentId': 'ruth',
                                    'taskId': 'task-9001', 'messages': [
                                        {'role': 'user', 'content': 'hi'}]})
        self.assertEqual(resp.status_code, 200)
        msgs = captured['messages']
        self.assertTrue(any('Private working memory' in (m.get('content') or '')
                            and 'PLAN: read wiki first' in (m.get('content') or '')
                            for m in msgs if m.get('role') == 'system'))

    def test_bypass_skips_injection(self):
        resp, captured = self._run({'model': 'm', 'agentId': 'ruth',
                                    'taskId': 'task-9001', 'hybridBypass': True,
                                    'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(any('Private working memory' in (m.get('content') or '')
                             for m in captured['messages']))

    def test_kill_switch_skips_injection(self):
        resp, captured = self._run({'model': 'm', 'agentId': 'ruth',
                                    'taskId': 'task-9001',
                                    'messages': [{'role': 'user', 'content': 'hi'}]},
                                   hybrid_mode='off')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(any('Private working memory' in (m.get('content') or '')
                             for m in captured['messages']))
