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

    def test_eligible_task_returns_digest_for_the_dispatcher(self):
        task = _fresh_task(distill=True)
        captured = {}

        def fake_http_json(method, base, path, body=None, header=None,
                           timeout=30, extra_headers=None):
            captured['body'] = body
            return {'reply': 'UNKNOWN: none. PLAN: read wiki, distill. RISK: drift.'}

        with unittest.mock.patch.object(serve, '_http_json', fake_http_json), \
             unittest.mock.patch.object(serve, '_load_env',
                                        lambda: {'HYBRID_MODE': 'auto'}):
            # base_ctx is the perception STRING by contract; the digest comes
            # back as the RETURN value for the dispatcher to fold into it.
            digest = content._hybrid_prepare({}, 'ruth', task, 'You are ruth.')
        # the latent pass used the bypass flag so /api/chat will not echo it
        self.assertTrue(captured['body'].get('hybridBypass'))
        self.assertIn('PLAN', digest)
        self.assertIn('PLAN', content._hybrid_memory_digest('ruth', task['id']))

    def test_none_base_ctx_still_decides_and_logs(self):
        # The 2026-11-02 live-fire bug: the dispatch path arrived with
        # base_ctx=None and the isinstance guard silently skipped EVERYTHING
        # -- no eligibility check, no decision row, no memory. A None
        # base_ctx must still produce the mode decision and its log row.
        task = _fresh_task(taskType='review')
        rows = []
        with unittest.mock.patch.object(serve, 'log_action',
                                        lambda *a, **k: rows.append((a, k))), \
             unittest.mock.patch.object(serve, '_load_env',
                                        lambda: {'HYBRID_MODE': 'auto'}):
            content._hybrid_prepare({}, 'ruth', task, None)
        self.assertEqual(len(rows), 1)
        args, _k = rows[0]
        self.assertEqual(args[1], 'hybrid_mode')
        self.assertEqual(args[2].get('decision'), 'text')
        self.assertEqual(args[2].get('reason'), 'type-review')

    def test_failed_latent_pass_is_logged_not_silent(self):
        # A dead chat endpoint (e.g. a broken free pool) must produce a
        # 'latent-failed' hybrid_mode row, otherwise the outage looks
        # exactly like 'the protocol never ran' (2026-11-02 live confusion).
        task = _fresh_task(distill=True)
        rows = []

        def failing_http_json(*a, **k):
            raise RuntimeError('circuit broken')

        with unittest.mock.patch.object(serve, '_http_json', failing_http_json), \
             unittest.mock.patch.object(serve, '_load_env',
                                        lambda: {'HYBRID_MODE': 'auto'}), \
             unittest.mock.patch.object(serve, 'log_action',
                                        lambda *a, **k: rows.append((a, k))):
            content._hybrid_prepare({}, 'ruth', task, {})

        self.assertEqual(len(rows), 1)
        args, kwargs = rows[0]
        # log_action(agent_id, action, details, authorized=...)
        self.assertEqual(args[0], 'ruth')
        self.assertEqual(args[1], 'hybrid_mode')
        row = args[2]
        self.assertEqual(row.get('task'), task['id'])
        # and no memory file was written
        self.assertFalse(content._hybrid_memory_digest('ruth', task['id']))

    def test_no_reply_error_body_is_reported_not_none(self):
        # The chat endpoint can succeed without a reply (it returns an error
        # body from a dead pool). The latent-failed row must say WHY: the
        # response's keys and its error field. Live 2026-11-03 the rows read
        # error 'None' because the success-path `_plan_err = None` reset in
        # the else clause clobbered the capture made inside the try.
        task = _fresh_task(distill=True)
        rows = []

        def error_body_http_json(*a, **k):
            return {'error': 'free pool exhausted'}

        with unittest.mock.patch.object(serve, '_http_json', error_body_http_json), \
             unittest.mock.patch.object(serve, '_load_env',
                                        lambda: {'HYBRID_MODE': 'auto'}), \
             unittest.mock.patch.object(serve, 'log_action',
                                        lambda *a, **k: rows.append((a, k))):
            digest = content._hybrid_prepare({}, 'ruth', task, {})

        self.assertEqual(digest, '')
        self.assertEqual(len(rows), 1)
        row = rows[0][0][2]
        self.assertEqual(row.get('decision'), 'latent-failed')
        err = row.get('error') or ''
        self.assertIn('replyKeys', err)
        self.assertIn('free pool exhausted', err)
        # and no memory file was written
        self.assertFalse(content._hybrid_memory_digest('ruth', task['id']))

    def test_cooldown_prevents_repeat_passes(self):
        task = _fresh_task(distill=True)
        calls = []

        def fake_http_json(*a, **k):
            calls.append(1)
            return {'reply': 'PLAN: x'}

        with unittest.mock.patch.object(serve, '_http_json', fake_http_json), \
             unittest.mock.patch.object(serve, '_load_env',
                                        lambda: {'HYBRID_MODE': 'auto'}):
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
             unittest.mock.patch.object(serve, '_load_env',
                                        lambda: {'HYBRID_MODE': 'off'}):
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
        env = {'HYBRID_MODE': hybrid_mode}
        captured = {}

        def fake_call_openrouter_sync(model, messages, max_tokens, best_of=1):
            captured['messages'] = messages
            return {'choices': [{'message': {'content': 'ok'}}]}

        with unittest.mock.patch.dict(os.environ, {'OPENROUTER_API_KEY': openrouter}), \
             unittest.mock.patch.object(serve, '_load_env', lambda: dict(env)), \
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
