"""How data is PRESENTED to JEV: the decision-tape kind tag, the
evidence-rich grade prompt, the groom contract, the worker-pick signals,
the escalation rung votes, and the finding-triage inconclusive option.

Background (2026-10-28 tape audit): decisions where the judged artifact was
fully presented (command text, review text) came back confident and
consistent, while decisions whose prompt summarized or withheld the artifact
misbehaved in three distinct ways -- the grader rubber-stamped '0' (it only
ever saw the title), the delegated-approval chain re-derived the same
uncertainty at every rung, and the tape's substring-guessed kind column
mis-bucketed whole decision families so the per-kind success rates lied.
These tests pin the presentation fixes.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402


def _jev_response(choice='allow', confidence=0.9, cost=0.0001):
    return {'answers': {'q': {'choice': choice, 'confidence': confidence}},
            'usage': {'cost': cost}}


class _ServeIsolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='jev-presentation-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
        )
        self._cm.start()
        serve.init_db()
        serve.COLAB_STANDBY_ENABLED = False
        self.addCleanup(self._cm.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class TapeKindTag(_ServeIsolated):
    """`questions['_kind']` reaches the tape as the row's kind and never
    reaches the wire."""

    def test_explicit_kind_is_taped_and_stripped_from_wire(self):
        questions = {'choice': {'type': 'choice',
                                'instructions': 'An in-game agent wants to visit this URL: https://x.test',
                                'criteria': {'allow': 'fine', 'block': 'no'}},
                     '_kind': 'browse'}
        seen = {}

        def fake_urlopen(request, timeout=0):
            seen['payload'] = json.loads(request.data.decode())
            return json.dumps(_jev_response()).encode()

        with unittest.mock.patch('serve._urlopen_with_resilience',
                                 side_effect=fake_urlopen):
            serve._call_openrouter_decision_sync('typesafe/jev-1.13', {}, questions)
        self.assertNotIn('_kind', seen['payload'])
        with serve._db() as conn:
            row = conn.execute(
                'SELECT kind FROM decision_tape ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(row[0], 'browse')

    def test_untagged_calls_fall_back_to_prompt_sniffing(self):
        questions = {'choice': {'type': 'choice',
                                'instructions': 'Hire or fire this worker?',
                                'criteria': {'fire': 'drop them', 'keep': 'retain'}}}
        with unittest.mock.patch('serve._urlopen_with_resilience',
                                 return_value=json.dumps(_jev_response(choice='fire')).encode()):
            serve._call_openrouter_decision_sync('typesafe/jev-1.13', {}, questions)
        with serve._db() as conn:
            row = conn.execute(
                'SELECT kind FROM decision_tape ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(row[0], 'personnel')

    def test_normalize_strips_private_keys_without_choice(self):
        out = serve._normalize_decision_questions({'_kind': 'x', 'other': 'y'})
        self.assertEqual(out, {'other': 'y'})

    def test_normalize_converts_list_criteria_and_strips_kind(self):
        out = serve._normalize_decision_questions({
            '_kind': 'groom',
            'choice': {'type': 'choice', 'instructions': 'i',
                       'criteria': [{'id': 'accept', 'description': 'a'}]}})
        self.assertNotIn('_kind', out)
        self.assertEqual(out['choice']['criteria'], {'accept': 'a'})


class GradeEvidence(unittest.TestCase):
    def test_evidence_lines_cover_the_task_record(self):
        task = {'instructions': 'Write the weather widget report.',
                'deliverableText': 'The widget renders current conditions.',
                'note': 'Shipped, tests pass.', 'writtenPy': ['widget.py'],
                'libraryPath': 'library/widget.md', 'sourceUrls': ['https://a.test/x']}
        lines = '\n\n'.join(sim._deliverable_evidence_lines(task))
        self.assertIn('Work order (what this task asked for):', lines)
        self.assertIn('The widget renders current conditions.', lines)
        self.assertIn('Shipped, tests pass.', lines)
        self.assertIn('widget.py', lines)
        self.assertIn('library/widget.md', lines)
        self.assertIn('https://a.test/x', lines)

    def test_empty_record_yields_no_lines(self):
        self.assertEqual(sim._deliverable_evidence_lines({}), [])

    def test_grade_prompt_carries_the_deliverable(self):
        captured = {}

        def recorder(state, instructions, title, room):
            captured['instructions'] = instructions
            return 6.0

        state = {'agents': {'ada': {'name': 'Ada', 'role': 'Engineer'}},
                 'tasks': {}, 'completedDeliverables': [],
                 'growthPlans': {}}
        task = {'id': 'task-9', 'room': 'observatory', 'title': 'Research X',
                'taskType': 'research', 'assignedTo': 'ada',
                'instructions': 'Investigate the flapping.',
                'deliverableText': 'Root cause: the watchdog timer never reset.'}
        old = sim._grading_decider
        sim._grading_decider = recorder
        try:
            sim._grade_completed_task(state, 'ada', task, 1_000_000)
        finally:
            sim._grading_decider = old
        instructions = captured['instructions']
        self.assertIn('Judge ONLY from the evidence below', instructions)
        self.assertIn('Investigate the flapping.', instructions)
        self.assertIn('Root cause: the watchdog timer never reset.', instructions)

    def test_grade_prompt_names_empty_records(self):
        captured = {}

        def recorder(state, instructions, title, room):
            captured['instructions'] = instructions
            return 2.0

        state = {'agents': {'ada': {'name': 'Ada'}},
                 'tasks': {}, 'completedDeliverables': [], 'growthPlans': {}}
        task = {'id': 'task-9', 'room': 'observatory', 'title': 'Research X',
                'taskType': 'research', 'assignedTo': 'ada'}
        old = sim._grading_decider
        sim._grading_decider = recorder
        try:
            sim._grade_completed_task(state, 'ada', task, 1_000_000)
        finally:
            sim._grading_decider = old
        self.assertIn('no deliverable content beyond the title',
                      captured['instructions'])

    def test_grading_decider_threads_kind(self):
        captured = {}

        def fake_call(model, state, questions):
            captured.update(questions)
            return _jev_response(choice='8')

        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        side_effect=fake_call):
            score = sim._grading_decider_default({}, 'instructions', 'title', 'room')
        self.assertEqual(score, 8.0)
        self.assertEqual(captured.get('_kind'), 'grade')
        self.assertIn('8', captured['choice']['criteria'])


class WorkerPickSignals(unittest.TestCase):
    def test_candidates_carry_grade_and_open_tasks(self):
        state = {
            'agents': {'ada': {'id': 'ada', 'role': 'Engineer', 'approvedCount': 3,
                               'droppedCount': 0}},
            'agentRoster': [{'id': 'ada', 'name': 'Ada', 'director': 'd1'}],
            'tasks': {'task-1': {'id': 'task-1', 'title': 'Fix the loop',
                                 'assignedTo': 'ada', 'status': 'in_progress',
                                 'updatedAt': 5}},
            'completedDeliverables': [
                {'agentId': 'ada', 'grade': 4.0, 'gradeIsReal': True},
                {'agentId': 'ada', 'grade': 6.0, 'gradeIsReal': True},
            ],
        }
        out = sim._team_member_candidates(state, 'd1', ['ada'], 1)
        self.assertEqual(len(out), 1)
        desc = out[0]['description']
        self.assertIn('Trailing grade: 5.0/10', desc)
        self.assertIn('Open tasks right now: 1 (currently on: Fix the loop)', desc)

    def test_candidates_survive_empty_state(self):
        state = {'agents': {'ada': {'id': 'ada'}}, 'agentRoster': []}
        out = sim._team_member_candidates(state, 'd1', ['ada'], 1)
        self.assertEqual(len(out), 1)
        self.assertIn('Trailing grade: none yet/10', out[0]['description'])
        self.assertIn('Open tasks right now: 0', out[0]['description'])


class GroomContract(_ServeIsolated):
    # Hermetic on purpose: this asserts the DECIDER CONTRACT, and the live
    # DB copy (conftest) now legitimately carries an armed groom cascade --
    # the decider would be correctly bypassed by the classifier instead of
    # reached. A tmp DB keeps the contract observable.
    def _state(self, req):
        return {
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana', 'director': None},
                {'id': 'sm1', 'name': 'Sam', 'director': 'd1'},
                {'id': 'm1', 'name': 'Mia', 'director': 'd1'},
            ],
            'agents': {
                'd1': {'id': 'd1', 'busy': False, 'offDuty': False},
                'sm1': {'id': 'sm1', 'name': 'Sam', 'busy': False, 'offDuty': False},
                'm1': {'id': 'm1', 'busy': False, 'offDuty': False},
            },
            'teams': [{'id': 't1', 'directorId': 'd1'}],
            'backlogRequests': [req], 'issues': {}, 'tasks': {}, 'workQueue': [],
        }

    def test_filed_contract_reaches_the_decider(self):
        req = {
            'id': 'r1', 'status': 'pending', 'filedBy': 'm1',
            'title': '[STORY] Add a weather widget', 'room': 'observatory',
            'reason': 'The observatory cannot show the current weather.',
            'issueType': 'story', 'deliverable': 'code',
            'userStory': 'As an operator, I want current weather, so that briefs are accurate.',
            'acceptanceCriteria': 'Given a load, When the page opens, Then the widget shows conditions.',
            'never': 'NEVER: Never call paid APIs',
        }
        captured = {}

        def decider(instructions, criteria, kind=None):
            captured['instructions'] = instructions
            captured['kind'] = kind
            return 'accept'

        pending = {'scrumMasterId': 'sm1', 'teamId': 't1', 'reqIds': ['r1'],
                   'people': {}}
        gov = unittest.mock.patch.object(sim, '_log_governance', lambda *a, **k: None)
        digest = unittest.mock.patch.object(serve, 'log_refinement_digest',
                                            lambda *a, **k: None)
        with gov, digest:
            sim._resolve_refinement(self._state(req), pending, 1_000_000,
                                    decider=decider)
        instructions = captured['instructions']
        self.assertIn('Filed contract:', instructions)
        self.assertIn('As an operator, I want current weather', instructions)
        self.assertIn('Then the widget shows conditions.', instructions)
        self.assertIn('Never call paid APIs', instructions)
        self.assertEqual(captured['kind'], 'groom')

    def test_legacy_two_arg_decider_still_works(self):
        req = {
            'id': 'r1', 'status': 'pending', 'filedBy': 'm1',
            'title': '[STORY] Add a weather widget', 'room': 'observatory',
            'reason': 'reason text',
        }
        calls = []

        def decider(instructions, criteria):
            calls.append(1)
            return 'accept'

        pending = {'scrumMasterId': 'sm1', 'teamId': 't1', 'reqIds': ['r1'],
                   'people': {}}
        gov = unittest.mock.patch.object(sim, '_log_governance', lambda *a, **k: None)
        digest = unittest.mock.patch.object(serve, 'log_refinement_digest',
                                            lambda *a, **k: None)
        with gov, digest:
            sim._resolve_refinement(self._state(req), pending, 1_000_000,
                                    decider=decider)
        self.assertEqual(calls, [1])


class EscalationContext(unittest.TestCase):
    def test_context_lines_render_note_and_votes(self):
        esc = {'note': 'example.test',
               'rungVotes': [
                   {'rung': 'immediate', 'decision': 'approve', 'confidence': 0.42},
                   {'rung': 'senior', 'decision': 'deny', 'confidence': 0.31},
               ]}
        lines = serve._escalation_context_lines(esc)
        joined = ' '.join(lines)
        self.assertIn('Note: example.test', joined)
        self.assertIn('immediate leaned approve (confidence 0.42)', joined)
        self.assertIn('senior leaned deny (confidence 0.31)', joined)
        self.assertIn('decide independently', joined)

    def test_no_context_no_lines(self):
        self.assertEqual(serve._escalation_context_lines({}), [])


class FindingTriageInconclusive(unittest.TestCase):
    def test_inconclusive_files_nothing(self):
        import content
        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        return_value=_jev_response(choice='inconclusive')), \
             unittest.mock.patch.object(serve, '_jev_choice',
                                        return_value=('inconclusive', 0.85, 0.01)):
            wish = content._spike_file_issue_wish(
                'Unverified: the workspace was empty so nothing could be checked.',
                {'teamId': 't1'}, 'Ada', 'backlog here')
        self.assertIsNone(wish)

    def test_criteria_carry_the_inconclusive_option(self):
        import content
        seen = {}

        def fake_call(model, state, questions):
            seen.update(questions)
            return _jev_response(choice='file_bug')

        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        side_effect=fake_call), \
             unittest.mock.patch.object(serve, '_jev_choice',
                                        return_value=('file_bug', 0.9, 0.01)):
            content._spike_file_issue_wish('Bug found: login fails.',
                                           {'teamId': 't1'}, 'Ada', 'b')
        self.assertIn('inconclusive', seen['choice']['criteria'])
        self.assertEqual(seen.get('_kind'), 'finding_triage')


class TierGatePrompt(unittest.TestCase):
    def test_prompt_names_work_type_and_calibration(self):
        captured = {}

        def decider(instructions, criteria, kind=None):
            captured['instructions'] = instructions
            captured['kind'] = kind
            return 'low', 0.9

        old = serve._tier_gate_decider
        serve._tier_gate_decider = decider
        try:
            serve._resolve_model_tier('Answer the player: hi', 'research',
                                      False, best_effort=True)
        finally:
            serve._tier_gate_decider = old
        self.assertIn('Work type: research', captured['instructions'])
        self.assertIn('stays low even when it matters', captured['instructions'])
        self.assertEqual(captured['kind'], 'tier_gate')

    def test_two_arg_stub_still_works(self):
        calls = []

        def stub(purpose, criteria):
            calls.append(1)
            return 'low', 1.0

        old = serve._tier_gate_decider
        serve._tier_gate_decider = stub
        try:
            serve._resolve_model_tier('routine', None, False, best_effort=True)
        finally:
            serve._tier_gate_decider = old
        self.assertEqual(calls, [1])


class RoleFraming(unittest.TestCase):
    """JEV judges a worker's command, a director's request, and the chief of
    staff's grant with rank-specific framing instead of one generic
    "in-game agent" description."""

    ROSTER = [
        {'id': 'theo', 'name': 'Theo', 'role': 'Chief of Staff', 'isAdmin': True,
         'isDirector': True},
        {'id': 'nora', 'name': 'Nora', 'role': 'Supporting Director',
         'isDirector': True},
        {'id': 'ben', 'name': 'Ben', 'role': 'Supervisor', 'isDirector': True,
         'director': 'theo'},
        {'id': 'ruth', 'name': 'Ruth', 'role': 'Assistant to Nora',
         'director': 'nora'},
    ]

    def _state(self):
        return {'agentRoster': self.ROSTER}

    def test_role_classification(self):
        state = self._state()
        self.assertEqual(serve._jev_actor_role(state, 'theo'), 'chief_of_staff')
        self.assertEqual(serve._jev_actor_role(state, 'nora'), 'director')
        self.assertEqual(serve._jev_actor_role(state, 'ben'), 'director')
        self.assertEqual(serve._jev_actor_role(state, 'ruth'), 'worker')
        self.assertEqual(serve._jev_actor_role(state, 'ghost'), 'unknown')
        self.assertEqual(serve._jev_actor_role({}, 'ruth'), 'unknown')

    def test_framing_differs_by_rank(self):
        state = self._state()
        chief = serve._jev_actor_framing(state, 'theo')
        director = serve._jev_actor_framing(state, 'nora')
        worker = serve._jev_actor_framing(state, 'ruth')
        self.assertIn('chief of staff', chief)
        self.assertIn('strictest bar', chief)
        self.assertIn('org-wide authority', chief)
        self.assertIn('director (Supporting Director)', director)
        self.assertIn('team stewardship', director)
        self.assertIn('worker (Assistant to Nora)', worker)
        self.assertIn('task scope', worker)
        self.assertNotEqual(chief, director)
        self.assertNotEqual(director, worker)

    def test_unknown_actor_gets_worker_level_framing(self):
        framing = serve._jev_actor_framing(self._state(), 'ghost')
        self.assertIn('not on the roster', framing)
        self.assertIn('worker-level task scope', framing)

    def test_rung_instructions_differ(self):
        esc = {'kind': 'owner safety decision', 'agentId': 'ruth'}
        q = 'Question text here.'
        imm = serve._escalation_rung_instructions(
            'immediate', 'Nora', q, state=self._state(), esc=esc, esc_kind='x')
        sen = serve._escalation_rung_instructions(
            'senior', 'Nora', q, state=self._state(), esc=esc, esc_kind='x')
        adm = serve._escalation_rung_instructions(
            'admin', 'Theo', q, state=self._state(), esc=esc, esc_kind='x')
        self.assertIn("requester's own director", imm)
        self.assertIn('use that context', imm)
        self.assertIn('second opinion', sen)
        self.assertIn('authority than a director has', sen)
        self.assertIn('chief of staff', adm)
        self.assertIn('strictest bar', adm)
        # The requester's rank framing rides every rung.
        for text in (imm, sen, adm):
            self.assertIn('worker (Assistant to Nora)', text)

    def test_rung_instructions_render_prior_votes(self):
        esc = {'kind': 'owner safety decision', 'agentId': 'ruth',
               'rungVotes': [{'rung': 'immediate', 'decision': 'deny',
                              'confidence': 0.44}]}
        sen = serve._escalation_rung_instructions(
            'senior', 'Nora', 'q', state=self._state(), esc=esc, esc_kind='x')
        self.assertIn('immediate leaned deny (confidence 0.44)', sen)

    def test_classify_command_carries_worker_framing(self):
        captured = {}

        def fake_call(model, state, questions):
            captured.update(questions)
            return _jev_response(choice='allow')

        async def drive():
            return await serve._classify_command('ls /workspace', 'list the task dir', 'ruth')

        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value={'agentRoster': self.ROSTER}), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        side_effect=fake_call), \
             unittest.mock.patch.object(serve, '_jev_choice',
                                        return_value=('allow', 0.95, 0.01)), \
             unittest.mock.patch.object(serve, 'is_model_circuit_broken',
                                        return_value=False), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'test-key'):
            # is_model_circuit_broken is patched because earlier tests in the
            # full suite trip the process-global breaker; in isolation it
            # would be closed either way.
            allowed, reason = asyncio.run(drive())
        self.assertTrue(allowed, f'blocked with reason: {reason}')
        instructions = captured['choice']['instructions']
        self.assertIn('worker (Assistant to Nora)', instructions)
        self.assertIn('task scope', instructions)
        self.assertEqual(captured.get('_kind'), 'cmd_allow')


if __name__ == '__main__':
    unittest.main()
