"""Spike pipeline, sandbox, review-detection, and report-close tests."""

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

class SpikeContent(unittest.TestCase):
    def setUp(self):
        # Plan checkpoints are intentionally kept after a failed spike, so a
        # shared test DB would otherwise let one test's plan leak into the
        # next test that reuses the same task id.
        try:
            with serve._db() as conn:
                conn.execute('''CREATE TABLE IF NOT EXISTS task_checkpoints (
                    task_id TEXT PRIMARY KEY, phase TEXT NOT NULL,
                    payload TEXT NOT NULL, updated_at REAL NOT NULL)''')
                conn.execute('DELETE FROM task_checkpoints')
        except Exception:
            pass

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

    def _mock_loop(self, execute_text, transcript=None):
        # _call_agent_tool_loop is called with return_transcript=True, so it
        # must return a (text, transcript) tuple, not a bare string.
        transcript = transcript if transcript is not None else [
            {'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'Go ahead.'},
            {'role': 'assistant', 'tool_calls': [{'id': 'c1', 'function': {'name': 'browse_page', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'PAGE TEXT'},
        ]
        return unittest.mock.patch.object(
            serve, '_call_agent_tool_loop', return_value=(execute_text, transcript))

    def test_spike_reuses_a_saved_plan_checkpoint(self):
        self._common_mocks()
        seen = self._store()
        task = {'id': 'spike-resume-plan', 'title': 'Resume me', 'budgetMs': 60000}
        serve.save_task_checkpoint('spike-resume-plan', 'planned',
                                   {'title': 'Resume me', 'plan': 'SAVED PLAN TEXT'})
        self.addCleanup(serve.clear_task_checkpoint, 'spike-resume-plan')
        with unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        side_effect=[_completion('report')]) as model_call, \
             self._mock_loop('summary') as loop:
            content._run_spike_content(_snapshot(), 'cora', task)
        self.assertEqual(model_call.call_count, 1, 'the plan call must be skipped')
        messages = loop.call_args.args[1]
        system = next(m['content'] for m in messages if m.get('role') == 'system')
        self.assertIn('SAVED PLAN TEXT', system)
        self.assertIn(task['id'], seen)
        self.assertIsNone(serve.load_task_checkpoint(task['id']))

    def test_spike_resumes_an_execute_checkpoint_with_interrupted_tools(self):
        self._common_mocks()
        seen = self._store()
        task = {'id': 'spike-resume-exec', 'title': 'Resume exec', 'budgetMs': 60000}
        transcript = [
            {'role': 'system', 'content': 'ORIGINAL SYSTEM'},
            {'role': 'user', 'content': 'Go ahead.'},
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'c1', 'function': {'name': 'browse_page', 'arguments': '{}'}},
                {'id': 'c2', 'function': {'name': 'send_mail', 'arguments': '{}'}}]},
        ]
        serve.save_task_checkpoint('spike-resume-exec', 'execute', {
            'title': 'Resume exec', 'plan': 'SAVED PLAN',
            'transcript': transcript, 'pending': transcript[-1]['tool_calls']})
        self.addCleanup(serve.clear_task_checkpoint, 'spike-resume-exec')
        with unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        side_effect=[_completion('report')]) as model_call, \
             self._mock_loop('summary') as loop:
            content._run_spike_content(_snapshot(), 'cora', task)
        self.assertEqual(model_call.call_count, 1, 'plan and execute must be skipped')
        messages = loop.call_args.args[1]
        self.assertEqual(messages[0]['content'], 'ORIGINAL SYSTEM')
        notes = [m['content'] for m in messages if m.get('role') == 'tool']
        self.assertTrue(any('safe to retry' in n for n in notes))
        self.assertTrue(any('NOT replayed' in n for n in notes))
        self.assertIn(task['id'], seen)
        self.assertIsNone(serve.load_task_checkpoint(task['id']))

    def test_google_quota_awareness_block_folded_when_google_configured(self):
        """The quota wiring: when a Google credential is in the vault, the
        spike system prompt carries the Google-API quota-awareness block so the
        agent paces its Sheets/Calendar/Gmail/Docs calls and knows Gmail is
        read+draft-only. Absent when no Google credential exists (no tokens
        wasted advertising a tool that can't work)."""
        self._common_mocks(tavily=True)
        self._store()
        task = {'id': 'spike-quota', 'title': 'Check a shared sheet', 'budgetMs': 60000}
        with unittest.mock.patch.object(serve, '_google_is_configured', return_value=True), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        side_effect=[_completion('plan'), _completion('report')]), \
             self._mock_loop('summary') as loop:
            content._run_spike_content(_snapshot(), 'cora', task)
        args, kwargs = loop.call_args
        model, messages, tools, execute_tool = args[:4]
        system = next(m['content'] for m in messages if m.get('role') == 'system')
        self.assertIn('READ + DRAFT ONLY', system)
        self.assertIn('~300/min/project', system)
        self.assertIn('a draft 10', system)
        tool_names = {t['function']['name'] for t in tools}
        for name in ('search_gmail_messages', 'read_gmail_message', 'create_gmail_draft',
                     'read_google_doc', 'create_google_doc'):
            self.assertIn(name, tool_names)
        # No send tool is ever offered.
        self.assertNotIn('send_gmail_message', tool_names)

    def test_google_quota_awareness_block_absent_when_not_configured(self):
        self._common_mocks(tavily=True)
        self._store()
        task = {'id': 'spike-quota-2', 'title': 'Check a shared sheet', 'budgetMs': 60000}
        with unittest.mock.patch.object(serve, '_google_is_configured', return_value=False), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        side_effect=[_completion('plan'), _completion('report')]), \
             self._mock_loop('summary') as loop:
            content._run_spike_content(_snapshot(), 'cora', task)
        args, kwargs = loop.call_args
        model, messages, tools, execute_tool = args[:4]
        system = next(m['content'] for m in messages if m.get('role') == 'system')
        self.assertNotIn('Google APIs', system)

    def test_uses_real_tool_loop_with_transcript_and_forces_search_web_first(self):
        """The tool-access fix: the executor must call _call_agent_tool_loop
        with AGENT_ASK_TOOLS (browse_page/search_web), return_transcript=True
        (needed for synthesis), and force search_web SPECIFICALLY as the
        first tool (not just force_first_tool=True's "any tool") -- real gap:
        any tool alone always reached for browse_page and
        never called search_web, missing facts that only exist in OTHER
        sites' coverage of the target."""
        self._common_mocks(tavily=True)
        seen = self._store()
        task = {'id': 'spike-1', 'title': 'Review DreyX.com sources', 'budgetMs': 60000}
        with self._mock_loop('execute-step summary') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('1. Visit the site\n2. Follow links'),
                                                      _completion('Final synthesized report.')]):
            content._run_spike_content(_snapshot(), 'cora', task)
        self.assertTrue(loop.called)
        args, kwargs = loop.call_args
        model, messages, tools, execute_tool = args[:4]
        self.assertEqual(model, 'mid-tier-slug')
        tool_names = {t['function']['name'] for t in tools}
        self.assertIn('browse_page', tool_names)
        # Spikes can produce a real deliverable now, not just prose --
        # "There might be a deliverable although
        # it is a spike" (a CSV of sources, a processed dataset, etc.).
        self.assertIn('execute_script', tool_names)
        # Chunked into rounds now (reflection/replan) -- 18 total, spent
        # _REFLECTION_CHUNK_SIZE at a time, not one flat 18-iteration call.
        self.assertEqual(kwargs.get('max_iterations'), content._REFLECTION_CHUNK_SIZE)
        self.assertEqual(kwargs.get('force_first_tool'), 'search_web')
        self.assertTrue(kwargs.get('return_transcript'))
        self.assertTrue(seen['spike-1']['ok'])
        self.assertIn('DreyX.com sources', seen['spike-1']['note'])
        # The FINAL report comes from synthesis, not the execute step's text.
        self.assertIn('Final synthesized report.', seen['spike-1']['notifyPlayer']['body'])

    def test_falls_back_to_generic_force_first_tool_without_tavily(self):
        self._common_mocks(tavily=False)
        self._store()
        task = {'id': 'spike-1b', 'title': 'Review DreyX.com sources', 'budgetMs': 60000}
        with self._mock_loop('summary') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]):
            content._run_spike_content(_snapshot(), 'cora', task)
        self.assertIs(loop.call_args.kwargs.get('force_first_tool'), True)

    def test_internal_review_question_forces_search_library_first(self):
        """Gap: a spike asked to "review the
        think tank's own prior research on DreyX.com" went straight to
        browse_page and reported "no existing records" despite 7+ real
        matching Library entries -- the PLAN prompt's own "search_library
        first" instruction was not reliably followed. Same fix as the
        earlier search_web gap: force the specific tool, don't just ask."""
        self._common_mocks(tavily=True)
        self._store()
        task = {'id': 'spike-3', 'title': "Review the think tank's own prior research on X",
               'budgetMs': 60000}
        with self._mock_loop('summary') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]):
            content._run_spike_content(_snapshot(), 'cora', task)
        self.assertEqual(loop.call_args.kwargs.get('force_first_tool'), 'search_library')

    def test_ordinary_external_question_still_forces_search_web(self):
        # Regression guard: an ordinary external-research spike (no internal-
        # review marker) must keep forcing search_web, not search_library.
        self._common_mocks(tavily=True)
        self._store()
        task = {'id': 'spike-4', 'title': 'Review DreyX.com sources', 'budgetMs': 60000}
        with self._mock_loop('summary') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]):
            content._run_spike_content(_snapshot(), 'cora', task)
        self.assertEqual(loop.call_args.kwargs.get('force_first_tool'), 'search_web')


    def test_plan_feeds_into_the_execute_system_prompt(self):
        """The PLAN step's checklist must actually reach the execute step's
        system prompt -- a plan nobody reads is just wasted spend."""
        self._common_mocks()
        self._store()
        task = {'id': 'spike-2', 'title': 'Review DreyX.com sources', 'budgetMs': 60000}
        with self._mock_loop('summary') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('1. Check /tools\n2. Check /about'),
                                                      _completion('report')]):
            content._run_spike_content(_snapshot(), 'cora', task)
        system_text = loop.call_args[0][1][0]['content']
        self.assertIn('1. Check /tools', system_text)
        self.assertIn('render=true', system_text)
        self.assertIn('search_web', system_text)
        self.assertIn('never invent', system_text.lower())

    def _capture_web_tool(self, task):
        """Run a spike with a single browse_page tool-call in the transcript and
        return the captured _http_json call bodies. Drives the REAL execute_tool
        built by _run_spike_content so we can assert on what it actually sends
        to the /api/browse gate."""
        bodies = []

        def fake_http(method, base, path, body, *a, **k):
            bodies.append({'method': method, 'path': path, 'body': body})
            if path == '/api/browse':
                return {'allowed': True, 'textForModel': 'page text', 'links': [], 'modelInstruction': ''}
            return {'ok': True}

        self._common_mocks(tavily=True)
        self._store()
        transcript = [
            {'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'Go ahead.'},
            {'role': 'assistant', 'tool_calls': [{'id': 'c1', 'function': {'name': 'browse_page',
                                                                           'arguments': '{"url":"https://docs.python.org/3/library/urllib.html"}'}}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'PAGE TEXT'},
        ]
        with unittest.mock.patch.object(serve, '_http_json', side_effect=fake_http), \
             unittest.mock.patch.object(serve, '_call_agent_tool_loop',
                                        return_value=('summary', transcript)) as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        side_effect=[_completion('plan'), _completion('report')]):
            content._run_spike_content(_snapshot(), 'cora', task)
            _, _, _, execute_tool = loop.call_args.args[:4]
            execute_tool('browse_page', {'url': 'https://docs.python.org/3/library/urllib.html',
                                         'purpose': 'research'})
        browse = next(b for b in bodies if b['path'] == '/api/browse')
        return browse['body']

    def test_agent_authored_spike_threads_untrusted_work_context(self):
        # The adversarial case end-to-end: an agent-filed spike (no
        # playerAuthored) must send NO work-trust nonce into the /api/browse
        # gate (Fix B), so a URL the agent named in its OWN story text goes
        # through the story-aware Jev prompt instead of bypassing it. An empty
        # nonce is the "untrusted" signal now; the legacy boolean is gone.
        task = {'id': 'spike-adv', 'title': 'Research urllib internals',
                'instructions': 'Investigate https://docs.python.org/3/library/urllib.html',
                'budgetMs': 60000}
        body = self._capture_web_tool(task)
        self.assertIn('docs.python.org', body['workContext'])
        self.assertNotIn('workContextTrusted', body)
        self.assertFalse(body['workTrustNonce'])

    def test_player_authored_spike_threads_trusted_work_context(self):
        # A player-authored spike (playerAuthored=True) keeps the trusted
        # bypass: the player vetted the host by writing it into the task, and
        # the executor mints a server-side nonce proving that provenance.
        task = {'id': 'spike-ply', 'title': 'Research urllib internals',
                'instructions': 'Investigate https://docs.python.org/3/library/urllib.html',
                'budgetMs': 60000, 'playerAuthored': True}
        body = self._capture_web_tool(task)
        self.assertIn('docs.python.org', body['workContext'])
        self.assertNotIn('workContextTrusted', body)
        nonce = body['workTrustNonce']
        self.assertTrue(nonce)
        # It is a real, redeemable, single-use credential -- not a decorative
        # string the endpoint would ignore.
        self.assertTrue(serve._redeem_work_trust_nonce(nonce))
        self.assertFalse(serve._redeem_work_trust_nonce(nonce))

    def test_plan_prompt_requires_a_mandatory_csv_step_for_enumerable_questions(self):
        """Real refinement: a plan that only SUGGESTS a CSV lets
        the model describe one in prose instead of building it. The PLAN
        step's own instructions must make that step explicit, mandatory, and
        concrete (real filename/columns), never just an optional nicety."""
        self._common_mocks()
        self._store()
        task = {'id': 'spike-11', 'title': 'List every source on a site', 'budgetMs': 60000}
        with self._mock_loop('summary'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]) as oc:
            content._run_spike_content(_snapshot(), 'cora', task)
        plan_system_text = oc.call_args_list[0].args[1][0]['content']
        self.assertIn('MANDATORY', plan_system_text)
        self.assertIn('execute_script to write', plan_system_text)
        self.assertIn('does NOT satisfy', plan_system_text)

    def test_plan_prompt_requires_verification_honesty_for_judgment_columns(self):
        """Real refinement, caught reviewing an actual spike's
        output: a feasibility CSV looked equally authoritative whether each
        row was really checked or just guessed from training knowledge --
        no way to tell which from the report alone. The PLAN must require an
        explicit basis (verified/estimated) column plus a real spot-check
        sample, not let every row read as equally confident."""
        self._common_mocks()
        self._store()
        task = {'id': 'spike-14', 'title': 'Assess feasibility of many sources', 'budgetMs': 60000}
        with self._mock_loop('summary'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]) as oc:
            content._run_spike_content(_snapshot(), 'cora', task)
        plan_system_text = oc.call_args_list[0].args[1][0]['content']
        self.assertIn('VERIFICATION HONESTY', plan_system_text)
        self.assertIn('"verified"', plan_system_text)
        self.assertIn('"estimated"', plan_system_text)
        self.assertIn('3-5 representative', plan_system_text)

    def test_execute_prompt_forbids_marking_estimates_as_verified(self):
        self._common_mocks()
        self._store()
        task = {'id': 'spike-15', 'title': 'Assess feasibility of many sources', 'budgetMs': 60000}
        with self._mock_loop('summary') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]):
            content._run_spike_content(_snapshot(), 'cora', task)
        execute_system_text = loop.call_args[0][1][0]['content']
        self.assertIn('do not mark a row "verified" just because', execute_system_text)

    def test_synthesize_prompt_preserves_the_basis_column_and_summarizes_it(self):
        self._common_mocks()
        self._store()
        task = {'id': 'spike-16', 'title': 'Assess feasibility of many sources', 'budgetMs': 60000}
        with self._mock_loop('summary'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]) as oc:
            content._run_spike_content(_snapshot(), 'cora', task)
        synth_user_text = oc.call_args_list[1].args[1][-1]['content']
        self.assertIn('basis', synth_user_text.lower())
        self.assertIn('verified vs estimated', synth_user_text)

    def test_missing_basis_column_gets_a_disclaimer_prepended(self):
        """Deterministic safety net: confirmed that the
        model doesn't reliably follow through on the basis-column plan
        requirement even when the plan itself calls for it. Rather than
        trying to block a non-compliant CSV mid-flight (fragile), catch it
        AFTER the fact and disclose it so the player is never silently left
        without knowing."""
        self._common_mocks()
        seen = self._store()
        task = {'id': 'spike-17', 'title': 'Assess feasibility of many sources', 'budgetMs': 60000}
        plan_with_basis_requirement = 'Plan: ... require a basis column, verified vs estimated ...'
        report_missing_basis_column = 'Source,Feasibility\nOpenAI,High\nAnthropic,High'
        with self._mock_loop('summary'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion(plan_with_basis_requirement),
                                                      _completion(report_missing_basis_column)]):
            content._run_spike_content(_snapshot(), 'cora', task)
        body = seen['spike-17']['notifyPlayer']['body']
        self.assertIn('NOTE (added automatically)', body)
        self.assertIn('UNVERIFIED', body)
        self.assertIn('OpenAI,High', body)  # the real report content is still there, not replaced

    def test_present_basis_column_gets_no_disclaimer(self):
        self._common_mocks()
        seen = self._store()
        task = {'id': 'spike-18', 'title': 'Assess feasibility of many sources', 'budgetMs': 60000}
        plan_with_basis_requirement = 'Plan: ... require a basis column, verified vs estimated ...'
        report_with_basis_column = 'Source,Feasibility,Basis\nOpenAI,High,verified\nAnthropic,High,estimated'
        with self._mock_loop('summary'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion(plan_with_basis_requirement),
                                                      _completion(report_with_basis_column)]):
            content._run_spike_content(_snapshot(), 'cora', task)
        body = seen['spike-18']['notifyPlayer']['body']
        self.assertNotIn('NOTE (added automatically)', body)

    def test_no_disclaimer_when_the_plan_never_required_a_basis_column(self):
        # Most spikes have no judgment column at all -- the disclaimer must
        # only fire when the plan itself decided one was needed.
        self._common_mocks()
        seen = self._store()
        task = {'id': 'spike-19', 'title': 'Simple fact lookup', 'budgetMs': 60000}
        with self._mock_loop('summary'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan without any judgment column'),
                                                      _completion('report with no csv at all')]):
            content._run_spike_content(_snapshot(), 'cora', task)
        body = seen['spike-19']['notifyPlayer']['body']
        self.assertNotIn('NOTE (added automatically)', body)

    def test_plan_requires_verification_basis_detection(self):
        self.assertTrue(content._plan_requires_verification_basis('needs a basis column: verified or estimated'))
        self.assertFalse(content._plan_requires_verification_basis('just browse the site and summarize'))
        self.assertFalse(content._plan_requires_verification_basis(''))
        self.assertFalse(content._plan_requires_verification_basis(None))

    def test_finding_shows_basis_column_detection(self):
        self.assertTrue(content._finding_shows_basis_column('Source,Basis\nX,verified'))
        self.assertTrue(content._finding_shows_basis_column('some rows verified, others estimated'))
        self.assertFalse(content._finding_shows_basis_column('Source,Feasibility\nX,High'))
        self.assertFalse(content._finding_shows_basis_column(''))

    def test_extract_execute_script_outputs_pulls_stdout_only_from_execute_script_calls(self):
        transcript = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'assistant', 'tool_calls': [{'id': 'c1', 'function': {'name': 'browse_page', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'some page text, not a script result'},
            {'role': 'assistant', 'tool_calls': [{'id': 'c2', 'function': {'name': 'execute_script', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'c2', 'content': 'exit code: 0\n\nstdout:\nSource,Basis\nX,verified\n\nstderr:\n'},
        ]
        outputs = content._extract_execute_script_outputs(transcript)
        self.assertEqual(outputs, ['Source,Basis\nX,verified'])

    def test_extract_execute_script_outputs_handles_no_stderr_section(self):
        transcript = [
            {'role': 'assistant', 'tool_calls': [{'id': 'c1', 'function': {'name': 'execute_script', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'exit code: 0\n\nstdout:\nfinal line, no stderr block'},
        ]
        self.assertEqual(content._extract_execute_script_outputs(transcript), ['final line, no stderr block'])

    def test_extract_execute_script_outputs_ignores_tool_results_without_stdout_marker(self):
        transcript = [
            {'role': 'assistant', 'tool_calls': [{'id': 'c1', 'function': {'name': 'execute_script', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'exit code: 0'},
        ]
        self.assertEqual(content._extract_execute_script_outputs(transcript), [])

    def test_extract_execute_script_outputs_ignores_whitespace_only_stdout(self):
        transcript = [
            {'role': 'assistant', 'tool_calls': [{'id': 'c1', 'function': {'name': 'execute_script', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'exit code: 0\n\nstdout:\n   \n\nstderr:\n'},
        ]
        self.assertEqual(content._extract_execute_script_outputs(transcript), [])

    def test_missing_raw_output_gets_appended_automatically(self):
        """Second deterministic safety net, caught in the SAME
        real investigation as the basis-column one: a real file got cat'd
        into the transcript, but synthesis summarized it in prose instead
        of including it verbatim -- the real data never reached the report."""
        self._common_mocks()
        seen = self._store()
        task = {'id': 'spike-20', 'title': 'Build a sources CSV', 'budgetMs': 60000}
        transcript = [
            {'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'Go ahead.'},
            {'role': 'assistant', 'tool_calls': [{'id': 'c1', 'function': {'name': 'execute_script', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'c1',
             'content': ('exit code: 0\n\nstdout:\nSource,URL\nOpenAI,https://openai.com\n'
                        'Anthropic,https://anthropic.com\nHuggingFace,https://huggingface.co\n\nstderr:\n')},
        ]
        with self._mock_loop('summary', transcript=transcript), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'),
                                                      _completion('I created a CSV of sources found.')]):
            content._run_spike_content(_snapshot(), 'cora', task)
        body = seen['spike-20']['notifyPlayer']['body']
        self.assertIn('Raw output from execute_script', body)
        self.assertIn('Source,URL', body)
        self.assertIn('OpenAI,https://openai.com', body)

    def test_present_raw_output_is_not_duplicated(self):
        self._common_mocks()
        seen = self._store()
        task = {'id': 'spike-21', 'title': 'Build a sources CSV', 'budgetMs': 60000}
        transcript = [
            {'role': 'assistant', 'tool_calls': [{'id': 'c1', 'function': {'name': 'execute_script', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'c1',
             'content': ('exit code: 0\n\nstdout:\nSource,URL\nOpenAI,https://openai.com\n'
                        'Anthropic,https://anthropic.com\nHuggingFace,https://huggingface.co\n\nstderr:\n')},
        ]
        report_already_includes_it = ('Here is the CSV:\n\nSource,URL\nOpenAI,https://openai.com\n'
                                      'Anthropic,https://anthropic.com\nHuggingFace,https://huggingface.co')
        with self._mock_loop('summary', transcript=transcript), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion(report_already_includes_it)]):
            content._run_spike_content(_snapshot(), 'cora', task)
        body = seen['spike-21']['notifyPlayer']['body']
        self.assertNotIn('Raw output from execute_script', body)
        self.assertEqual(body.count('OpenAI,https://openai.com'), 1)

    def test_execute_prompt_rejects_describing_the_csv_instead_of_building_it(self):
        self._common_mocks()
        self._store()
        task = {'id': 'spike-12', 'title': 'List every source on a site', 'budgetMs': 60000}
        with self._mock_loop('summary') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]):
            content._run_spike_content(_snapshot(), 'cora', task)
        execute_system_text = loop.call_args[0][1][0]['content']
        self.assertIn('MANDATORY', execute_system_text)
        self.assertIn('is NOT the same as building it', execute_system_text)

    def test_synthesize_uses_the_full_transcript(self):
        """SYNTHESIZE must be handed the execute step's actual transcript
        (every real tool result), not just its short closing summary."""
        self._common_mocks()
        self._store()
        task = {'id': 'spike-3', 'title': 'Review DreyX.com sources', 'budgetMs': 60000}
        transcript = [
            {'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'Go ahead.'},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'REAL PAGE CONTENT ABOUT DREYX TOOLS'},
        ]
        with self._mock_loop('summary', transcript=transcript), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('final report')]) as oc:
            content._run_spike_content(_snapshot(), 'cora', task)
        synth_call_messages = oc.call_args_list[1].args[1]
        joined = ' '.join(m.get('content', '') for m in synth_call_messages)
        self.assertIn('REAL PAGE CONTENT ABOUT DREYX TOOLS', joined)
        # The spike's plan/synthesize go through the JEV tier gate now (no
        # separate reasoning band); the test's decider stub returns mid, so
        # both bookends use the mid tier model.
        self.assertEqual(oc.call_args_list[1].args[0], 'mid-tier-slug')

    def test_synthesis_failure_falls_back_to_execute_text(self):
        self._common_mocks()
        seen = self._store()
        task = {'id': 'spike-4', 'title': 'Review DreyX.com sources', 'budgetMs': 60000}
        with self._mock_loop('execute step fallback text'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('')]):
            content._run_spike_content(_snapshot(), 'cora', task)
        self.assertTrue(seen['spike-4']['ok'])
        self.assertIn('execute step fallback text', seen['spike-4']['notifyPlayer']['body'])

    def test_both_synthesis_and_execute_empty_stores_nothing_usable(self):
        # Even with a real investigation (transcript has a tool result), when
        # BOTH the synthesis call AND the execute step's own closing text come
        # back empty, the spike must fail honestly instead of filing an empty
        # finding -- the exact same nothing-usable path as a non-investigation.
        self._common_mocks()
        seen = self._store()
        task = {'id': 'spike-30', 'title': 'Review DreyX.com sources', 'budgetMs': 60000}
        with self._mock_loop(''), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('')]):
            content._run_spike_content(_snapshot(), 'cora', task)
        self.assertFalse(seen['spike-30']['ok'])
        self.assertIn('came up empty', seen['spike-30']['notifyPlayer']['subject'])
        self.assertIn('nothing usable', seen['spike-30']['notifyPlayer']['body'])

    def test_run_on_colab_tool_absent_when_colab_cli_unavailable(self):
        # Same conditional-availability rule as search_web/GitHub/Apify: no
        # colab CLI on this machine -> run_on_colab is simply not offered, so
        # the surface never advertises a tool that would fail.
        self._common_mocks()
        self._store()
        task = {'id': 'spike-colab-1', 'title': 'Run a GPU job', 'budgetMs': 60000}
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', False), \
             unittest.mock.patch.object(serve, '_call_agent_tool_loop') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        side_effect=[_completion('plan'), _completion('report')]):
            loop.return_value = ('done', [{'role': 'system', 'content': 's'}])
            content._run_spike_content(_snapshot(), 'cora', task)
        tool_names = {t['function']['name'] for t in loop.call_args.args[2]}
        self.assertNotIn('run_on_colab', tool_names)

    def test_no_tools_used_notifies_player_and_skips_synthesis(self):
        # force_first_tool=True should make this unreachable in practice, but
        # the code must still fail honestly rather than synthesize a report
        # from a transcript that never investigated anything.
        self._common_mocks()
        seen = self._store()
        task = {'id': 'spike-5', 'title': 'A hard question', 'budgetMs': 60000}
        empty_transcript = [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'Go ahead.'}]
        with self._mock_loop(None, transcript=empty_transcript), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         return_value=_completion('plan')) as oc, \
             unittest.mock.patch.object(serve, '_http_json') as http_mock:
            content._run_spike_content(_snapshot(), 'cora', task)
        self.assertFalse(seen['spike-5']['ok'])
        self.assertEqual(seen['spike-5']['notifyPlayer']['kind'], 'spike_done')
        self.assertIn('came up empty', seen['spike-5']['notifyPlayer']['subject'])
        # PLAN always runs before EXECUTE; only SYNTHESIZE (a second call) is
        # skipped when nothing was ever investigated.
        self.assertEqual(oc.call_count, 1)
        http_mock.assert_not_called()  # no library file written either

    def test_no_model_tier_short_circuits_before_any_planning(self):
        seen = self._store()
        task = {'id': 'spike-6', 'title': 'Anything', 'budgetMs': 60000}
        with unittest.mock.patch.object(serve, '_coding_tier_slug', return_value=None), \
             unittest.mock.patch.object(serve, '_low_tier_slug', return_value=None), \
             unittest.mock.patch.object(serve, '_mid_tier_slug', return_value=None), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync') as oc, \
             unittest.mock.patch.object(serve, '_call_agent_tool_loop') as loop:
            content._run_spike_content(_snapshot(), 'cora', task)
        loop.assert_not_called()
        oc.assert_not_called()
        self.assertFalse(seen['spike-6']['ok'])
        self.assertIn('no model tier is configured', seen['spike-6']['notifyPlayer']['body'])

    def test_widens_workUntil_for_the_slower_multi_tool_investigation(self):
        self._common_mocks()
        self._store()
        task = {'id': 'spike-7', 'title': 'Review DreyX.com sources', 'budgetMs': 60000,
                'workUntil': 1.0}
        with self._mock_loop('findings'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]):
            content._run_spike_content(_snapshot(), 'cora', task)
        self.assertGreater(task['workUntil'], 1.0)

    def test_successful_finding_still_files_a_library_entry(self):
        self._common_mocks()
        seen = self._store()
        task = {'id': 'spike-8', 'title': 'Review DreyX.com sources', 'budgetMs': 60000}
        with self._mock_loop('execute summary'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'),
                                                      _completion('Real findings from actually browsing.')]), \
             unittest.mock.patch.object(serve, '_http_json', return_value={'ok': True}) as http_mock:
            content._run_spike_content(_snapshot(), 'cora', task)
        library_calls = [c for c in http_mock.call_args_list if c.args[2] == '/api/library/file']
        self.assertEqual(len(library_calls), 1)
        self.assertTrue(library_calls[0].args[3]['path'].startswith('archive/'))
        self.assertIn('Real findings from actually browsing.', seen['spike-8']['notifyPlayer']['body'])

    def test_empty_plan_still_lets_execute_and_synthesize_proceed(self):
        # PLAN is best-effort -- an empty/failed plan call must not block the
        # rest of the pipeline.
        self._common_mocks()
        seen = self._store()
        task = {'id': 'spike-9', 'title': 'Review DreyX.com sources', 'budgetMs': 60000}
        with self._mock_loop('summary'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion(''), _completion('report anyway')]):
            content._run_spike_content(_snapshot(), 'cora', task)
        self.assertTrue(seen['spike-9']['ok'])
        self.assertIn('report anyway', seen['spike-9']['notifyPlayer']['body'])

    def test_sandbox_scoped_to_this_spike_not_the_shared_workroom(self):
        # Real security-audit finding, same evening: the shared
        # workroom-shared/research-shared sandboxes persist forever and are
        # readable by any later, unrelated task. A spike's sandbox must be
        # scoped to just this task, never one of the shared ones.
        self._common_mocks()
        self._store()
        task = {'id': 'spike-10', 'title': 'Review DreyX.com sources', 'budgetMs': 60000}
        with self._mock_loop('summary') as loop, \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                         side_effect=[_completion('plan'), _completion('report')]):
            content._run_spike_content(_snapshot(), 'cora', task)
        execute_tool = loop.call_args[0][3]
        with unittest.mock.patch.object(serve, '_http_json', return_value={'allowed': True, 'stdout': 'ok', 'exitCode': 0}) as http_mock:
            execute_tool('execute_script', {'command': 'echo ok', 'purpose': 'test'})
        call = http_mock.call_args
        self.assertEqual(call.args[2], '/api/execute')
        self.assertEqual(call.args[3]['sandboxId'], 'spike-spike-10')
        self.assertNotIn(call.args[3]['sandboxId'], ('workroom-shared', 'research-shared'))

class SpikeFileIssueWish(unittest.TestCase):
    """content._spike_file_issue_wish (W3): the model-driven probe that turns
    a spike finding into a fileIssue WISH. Hermetic -- Jev's decision call is
    mocked. The function must NEVER file anything itself (it returns a wish
    dict the sim side consumes inside the tick's read-modify-write), and it
    must spend no Jev at all when the deterministic problem-signal pre-filter
    finds nothing -- most spikes are informational."""

    _TASK = {'id': 'spike-wish-1', 'title': 'Check the auth flow',
             'projectLabel': 'Library Tools', 'teamId': 'dev'}

    def _decision(self, choice, confidence=0.9):
        return {'answers': {'q1': {'choice': choice, 'confidence': confidence}},
                'usage': {'cost': 0.0}}

    def _call_wish(self, finding, task=None, name='Cora', backlog='Check the auth flow'):
        return content._spike_file_issue_wish(finding, task or self._TASK, name, backlog)

    def test_no_problem_signal_returns_none_without_spending_jev(self):
        # An informational finding ("everything is fine") must never call Jev.
        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync') as decider, \
             unittest.mock.patch.object(serve, '_jev_choice') as choice:
            wish = self._call_wish('The auth flow works correctly in all tested browsers.')
        self.assertIsNone(wish)
        decider.assert_not_called()
        choice.assert_not_called()

    def test_empty_finding_returns_none(self):
        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync') as decider:
            self.assertIsNone(self._call_wish('   '))
            self.assertIsNone(self._call_wish(''))
        decider.assert_not_called()

    def test_problem_signal_with_file_bug_verdict_returns_a_bug_wish(self):
        finding = ('Gap found: login failures are silently dropped. The error is '
                   'swallowed before it reaches the UI.')
        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        return_value=self._decision('file_bug')) as decider:
            wish = self._call_wish(finding)
        self.assertIsNotNone(wish)
        self.assertEqual(wish['issueType'], 'bug')
        self.assertIn('login failures are silently dropped', wish['summary'])
        self.assertEqual(wish['feature'], 'Library Tools')
        self.assertEqual(wish['teamId'], 'dev')
        self.assertIn('swallowed', wish['description'])
        decider.assert_called_once()

    def test_problem_signal_with_file_story_verdict_returns_a_story_wish(self):
        finding = ('The dashboard is missing a way to export the data; it needs a '
                   'CSV download button.')
        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        return_value=self._decision('file_story')):
            wish = self._call_wish(finding)
        self.assertIsNotNone(wish)
        self.assertEqual(wish['issueType'], 'story')

    def test_none_verdict_returns_no_wish(self):
        finding = 'Noted a limitation, but it is expected behavior -- nothing to fix.'
        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        return_value=self._decision('none')):
            wish = self._call_wish(finding)
        self.assertIsNone(wish)

    def test_feature_falls_back_across_task_fields(self):
        # feature is task.projectLabel -> productId -> room -> pressoffice.
        for task, expected in (({'id': 't', 'projectLabel': 'P', 'teamId': 'dev'}, 'P'),
                               ({'id': 't', 'productId': 'prod-1', 'teamId': 'dev'}, 'prod-1'),
                               ({'id': 't', 'room': 'observatory', 'teamId': 'dev'}, 'observatory'),
                               ({'id': 't', 'teamId': 'dev'}, 'pressoffice')):
            with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                            return_value=self._decision('file_bug')):
                wish = self._call_wish('Found a broken integration.', task=task)
            self.assertEqual(wish['feature'], expected)

    def test_wish_never_mutates_shared_state(self):
        # The executor thread runs against a snapshot -- the wish is a pure
        # return value, never a file_issue call on the live state.
        import sim as _sim
        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        return_value=self._decision('file_bug')), \
             unittest.mock.patch.object(_sim, 'file_issue') as file_issue:
            wish = self._call_wish('Gap: the export endpoint fails on empty input.')
        self.assertIsNotNone(wish)
        file_issue.assert_not_called()

class InternalReviewDetection(unittest.TestCase):
    """content._spike_wants_internal_review: the pure keyword heuristic
    behind the search_library-forcing fix above."""

    def test_detects_common_internal_review_phrasings(self):
        cases = [
            ("Review our own prior research on X", None),
            ('What did another team build for auth?', None),
            ('Before investigating anything new, check our existing findings', None),
            (None, 'This depends on what a different team already investigated'),
        ]
        for backlog, instructions in cases:
            self.assertTrue(content._spike_wants_internal_review(backlog, instructions),
                            f'expected True for backlog={backlog!r} instructions={instructions!r}')

    def test_ordinary_external_questions_are_not_flagged(self):
        self.assertFalse(content._spike_wants_internal_review('Review DreyX.com sources', None))
        self.assertFalse(content._spike_wants_internal_review('What is the weather in Tokyo?', None))

    def test_both_none_is_false(self):
        self.assertFalse(content._spike_wants_internal_review(None, None))

class SpikeSandboxExecutor(unittest.TestCase):
    """_make_spike_sandbox_executor: the real request behind execute_script --
    "there might be a deliverable although it is a spike"."""

    def test_successful_command_reports_exit_code_and_stdout(self):
        executor = content._make_spike_sandbox_executor('cora', 'key-123', 'spike-1')
        with unittest.mock.patch.object(serve, '_http_json',
                                        return_value={'allowed': True, 'exitCode': 0,
                                                     'stdout': 'a,b\n1,2\n', 'stderr': '', 'timedOut': False}):
            out = executor('execute_script', {'command': 'cat sources.csv', 'purpose': 'read the csv'})
        self.assertIn('exit code: 0', out)
        self.assertIn('a,b\n1,2', out)

    def test_blocked_command_is_surfaced_clearly(self):
        executor = content._make_spike_sandbox_executor('cora', 'key-123', 'spike-1')
        with unittest.mock.patch.object(serve, '_http_json',
                                        return_value={'allowed': False, 'reason': 'looked risky'}):
            out = executor('execute_script', {'command': 'rm -rf /', 'purpose': 'test'})
        self.assertIn('blocked', out.lower())
        self.assertIn('looked risky', out)

    def test_unknown_tool_name_raises(self):
        executor = content._make_spike_sandbox_executor('cora', 'key-123', 'spike-1')
        with self.assertRaises(ValueError):
            executor('some_other_tool', {})

    def test_uses_the_given_sandbox_id_and_agent_key(self):
        executor = content._make_spike_sandbox_executor('cora', 'key-abc', 'spike-42')
        with unittest.mock.patch.object(serve, '_http_json',
                                        return_value={'allowed': True, 'exitCode': 0, 'stdout': '', 'stderr': ''}) as http_mock:
            executor('execute_script', {'command': 'ls', 'purpose': 'test'})
        call = http_mock.call_args
        self.assertEqual(call.args[3]['sandboxId'], 'spike-42')
        self.assertEqual(call.args[3]['agentId'], 'cora')
        self.assertEqual(call.args[4], 'key-abc')

class SpikeReportCloseDoesNotClobber(SpikeContent):
    """The close path (content._run_spike_content) must NOT overwrite a report
    that write_spike_report already saved to the canonical path. The guard reads
    via the in-process library_tool, which checks _safe_library_path + os.path.isfile
    on DISK (not HTTP) -- so pointing _safe_library_path at a real temp file
    exercises the REAL existence check. This is the regression that let the
    1800-token closing synthesis clobber a full document-length report."""

    def _drive_close(self, report_file_exists):
        """Drive _run_spike_content to its close. If report_file_exists, patch
        _safe_library_path to a real temp file (guard sees a report); else patch
        it to a non-existent path (guard sees none). Returns (posts, seen,
        report_path)."""
        tmp = tempfile.mkdtemp(prefix='spike-close-guard-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        real_report = os.path.join(tmp, 'report.md')
        if report_file_exists:
            with open(real_report, 'w') as f:
                f.write('# Full long report\n\n' + ('body ' * 200))
        posts = []

        def fake_http(method, base, path, body, *a, **k):
            if path == '/api/library/file' and method == 'POST':
                posts.append(body)
                return 'saved'
            return {'ok': True}

        def fake_loop(model, messages, tools, execute_tool, *a, **k):
            return ('closing summary', [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'Go.'},
                {'role': 'assistant', 'tool_calls': [{'id': 'c1', 'function': {'name': 'browse_page', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'c1', 'content': 'PAGE'},
            ])

        captured = {}
        orig_writer = content._make_spike_report_writer
        def capture(agent_id, key, report_path):
            captured['path'] = report_path
            return orig_writer(agent_id, key, report_path)

        self._common_mocks(tavily=True)
        seen = self._store()
        task = {'id': 'close-guard', 'title': 'Write the long report', 'budgetMs': 60000}
        safe_target = real_report if report_file_exists else os.path.join(tmp, 'missing.md')
        with unittest.mock.patch.object(content, '_make_spike_report_writer', side_effect=capture), \
             unittest.mock.patch.object(serve, '_safe_library_path', return_value=safe_target), \
             unittest.mock.patch.object(serve, 'record_library_read'), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, '_google_is_configured', return_value=False), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        side_effect=[_completion('plan'), _completion('full report')]), \
             unittest.mock.patch.object(serve, '_call_agent_tool_loop', side_effect=fake_loop), \
             unittest.mock.patch.object(serve, '_http_json', side_effect=fake_http), \
             unittest.mock.patch.object(content, '_spike_file_issue_wish', return_value=None):
            content._run_spike_content(_snapshot(), 'cora', task)
        return posts, seen, captured.get('path'), task

    def test_closer_skips_write_when_report_already_exists(self):
        posts, _seen, report_path, _task = self._drive_close(report_file_exists=True)
        self.assertTrue(report_path and report_path.startswith('archive/') and report_path.endswith('.md'))
        self.assertEqual(posts, [], 'closer must not clobber an already-written report')

    def test_closer_writes_when_no_report_exists(self):
        posts, seen, report_path, _task = self._drive_close(report_file_exists=False)
        self.assertEqual(len(posts), 1, 'closer must write when the tool never saved a report')
        self.assertEqual(posts[0]['path'], report_path)
        self.assertEqual(posts[0]['source'], 'firsthand')
        self.assertIn('close-guard', seen['close-guard']['libraryPath'])

    def test_heartbeat_bumped_throughout_run(self):
        # The grace mechanism (sim.TASK_CONTENT_GRACE_*) reads this field off
        # the LIVE task dict to decide whether an expired workUntil earns
        # another pass. The executor must bump it during the run -- a run that
        # never heartbeats would be abandoned at the ceiling even while alive.
        import time as _time
        t0 = _time.time()
        _posts, _seen, _path, task = self._drive_close(report_file_exists=False)
        hb = task.get('_contentHeartbeat')
        self.assertIsInstance(hb, float,
                              '_run_spike_content must bump _contentHeartbeat on the live task')
        self.assertGreaterEqual(hb, t0 - 0.5,
                                'heartbeat must be stamped during this run, not left over')
