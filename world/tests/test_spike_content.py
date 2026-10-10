"""The spike content executor's plan-execute-synthesize pipeline.

History: this started as a single free-text /api/chat completion with NO tool
access at all -- the model just guessed from training knowledge and called it
"findings" (a real, confirmed problem: a DreyX.com spike produced confident-
sounding but almost certainly ungrounded claims). Got real search_web/
browse_page tool access next, via the shared serve._make_web_tools_executor --
fixed the fabrication, but a harder follow-up request ("list every source
ever used on DreyX, assess replicating each daily") showed the next gap: one
flat tool loop on a non-reasoning model settles too early on genuinely open-
ended work. Real fix: PLAN (reasoning tier, a checklist) -> EXECUTE (mid
tier, the existing many-iteration tool loop, following that checklist) ->
SYNTHESIZE (reasoning tier, given the full transcript, writes the real
report).

Hermetic: serve._call_agent_tool_loop (the EXECUTE step) and
serve._call_openrouter_sync (the PLAN/SYNTHESIZE steps -- the only things
that would touch the real network/OpenRouter) are mocked in every test here,
so no live model call or fetch ever happens.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402
import content  # noqa: E402


def _snapshot():
    return {
        'agentRoster': [{'id': 'cora', 'name': 'Cora'}],
        'agents': {'cora': {'name': 'Cora'}},
    }


def _completion(text):
    return {'choices': [{'message': {'content': text}}], 'usage': {'cost': 0.0}}


class SpikeContent(unittest.TestCase):
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
        # Chunked into rounds now (reflection/replan, ported from the user's
        # own framework) -- 18 total, spent _REFLECTION_CHUNK_SIZE at a
        # time, not one flat 18-iteration call.
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
        # playerAuthored) must carry workContextTrusted=False into the /api/
        # browse gate, so a URL the agent named in its OWN story text goes
        # through the story-aware Jev prompt instead of bypassing it.
        task = {'id': 'spike-adv', 'title': 'Research urllib internals',
                'instructions': 'Investigate https://docs.python.org/3/library/urllib.html',
                'budgetMs': 60000}
        body = self._capture_web_tool(task)
        self.assertIn('docs.python.org', body['workContext'])
        self.assertIs(body['workContextTrusted'], False)

    def test_player_authored_spike_threads_trusted_work_context(self):
        # A player-authored spike (playerAuthored=True) keeps the trusted
        # bypass: the player vetted the host by writing it into the task.
        task = {'id': 'spike-ply', 'title': 'Research urllib internals',
                'instructions': 'Investigate https://docs.python.org/3/library/urllib.html',
                'budgetMs': 60000, 'playerAuthored': True}
        body = self._capture_web_tool(task)
        self.assertIn('docs.python.org', body['workContext'])
        self.assertIs(body['workContextTrusted'], True)

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


class OneStrikePerTool(unittest.TestCase):
    """Ported (design) from the user's own framework's ReflectionEngine:
    a real policy denial (Jev said no) must never be retried -- the SAME
    tool, called again, is refused locally with no second network call."""

    def test_struck_browse_page_is_refused_without_a_second_network_call(self):
        struck = set()
        executor = serve._make_web_tools_executor('cora', 'key-123', struck_tools=struck)
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
    """Ported (design) from the user's own framework's ReflectionEngine:
    the EXECUTE budget is chunked into rounds with a cheap confidence check
    between them, never exceeding the original total iteration ceiling."""

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
        self.log_action.assert_called_once_with('cora', 'read_library_file', {'path': 'finding.md', 'found': True}, authorized=True)

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


class GoogleToolsExecutor(unittest.TestCase):
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
                'end_datetime': '2026-10-01T15:00:00-04:00', 'description': 'real ceremony'})
        call.assert_called_once_with(
            'POST', 'https://www.googleapis.com/calendar/v3/calendars/primary/events',
            {'summary': 'Sprint review',
             'start': {'dateTime': '2026-10-01T14:00:00-04:00'},
             'end': {'dateTime': '2026-10-01T15:00:00-04:00'},
             'description': 'real ceremony'})
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
                            'waitSeconds': 5})
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
                                               'input': {'startUrls': []}})
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
                            'waitSeconds': 5})
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
                            'input': {'startUrls': []}, 'waitSeconds': 5})
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
                'team': 'cora', 'interface': 'i', 'body': '## Purpose\np\n## Key facts\nk\n## Sources\ns\n## Lessons learned\nl'}
        with http:
            pub('publish_skill', args)
        self.assertEqual(bodies[0]['body']['path'], 'pending_review/skills/fetch-x.md')


if __name__ == '__main__':
    unittest.main()
