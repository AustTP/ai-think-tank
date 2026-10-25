"""MEETING cards: file -> groom -> queue -> assign -> minutes.

A meeting card is a deliberate conversation: filed like any JIRA issue
(issueType 'meeting' with an attendee spec + facilitator), groomed by
the owning team's scrum master into a taskType 'meeting' queue item
(the facilitator pin wakes her off-duty, same as an incident wakes the
on-call), and executed by ONE non-tool completion whose minutes ARE the
card's note (taskType 'meeting' is a non-gated lane, so no peer gate
opens; the minutes are the completion evidence). Deterministic -- no
Jev: direct _resolve_refinement / _assign_due_item calls with an
injectable decider. NOT actually DB-free: grooming and assignment log
governance via serve.log_action, a real DB write, so serve's paths are
redirected to a tempdir below (same pattern as test_refinement.py).
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402  (import first: serve lazily re-imports content at
import content  # noqa: E402  # its bottom, so content must not be mid-import
import sim  # noqa: E402  # when serve loads)

_MINUTES = ('# Minutes\n\n**Topic**: Sprint retro\n\n'
            '**Attendees**: Ada, Dev, Ben\n\n'
            '**Discussion**: what shipped, what slowed us down.\n\n'
            '**Decisions**: None yet\n\n**Action items**: None')

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-meeting-test-')
    _PATCHER = mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        # Standby off in hermetic modules: single-slug chain keeps the
        # decision breaker unarmed in the shared test process.
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _stub_decider(choice='accept'):
    """Deterministic stand-in for the Jev-backed _refinement_decider."""
    def decider(instructions, criteria):
        return choice
    return decider


def _completion(text):
    return {'choices': [{'message': {'content': text}}],
            'usage': {'cost': 0.0}}


def _seed():
    """faye: Admin (never an assignment candidate); dev: Director;
    ben + ada: dev's direct reports; ada is also team 'dev's scrum
    master, so the team scope resolves to [dev, ada, ben]."""
    return {
        'sim': {'owner': 'server', 'rr': {'task': 0}},
        'agentRoster': [
            {'id': 'faye', 'name': 'Faye', 'role': 'Admin', 'isAdmin': True},
            {'id': 'dev', 'name': 'Dev', 'role': 'Director',
             'isDirector': True},
            {'id': 'ben', 'name': 'Ben', 'role': 'Engineer',
             'director': 'dev'},
            {'id': 'ada', 'name': 'Ada', 'role': 'Researcher',
             'director': 'dev'},
        ],
        'teams': [
            {'id': 'dev', 'name': "Dev's Crew", 'directorId': 'dev',
             'scrumMasterId': 'ada'},
        ],
        'agents': {
            'faye': {'id': 'faye', 'x': 70, 'y': 10, 'dir': 'south',
                     'visible': True, 'busy': False, 'task': None,
                     'inRoom': None, 'offDuty': False, 'name': 'Faye',
                     'role': 'Admin', 'weekApprovals': 1},
            'dev': {'id': 'dev', 'x': 10, 'y': 40, 'dir': 'south',
                    'visible': True, 'busy': False, 'task': None,
                    'inRoom': None, 'offDuty': False, 'name': 'Dev',
                    'role': 'Director', 'weekApprovals': 1},
            'ben': {'id': 'ben', 'x': 40, 'y': 10, 'dir': 'south',
                    'visible': True, 'busy': False, 'task': None,
                    'inRoom': None, 'offDuty': False, 'name': 'Ben',
                    'role': 'Engineer', 'weekApprovals': 1},
            'ada': {'id': 'ada', 'x': 10, 'y': 10, 'dir': 'south',
                    'visible': True, 'busy': False, 'task': None,
                    'inRoom': None, 'offDuty': False, 'name': 'Ada',
                    'role': 'Researcher', 'weekApprovals': 1},
        },
        'workQueue': [],
        'tasks': {},
        'backlogRequests': [],
        'issues': {},
        'issueCounters': {},
    }


class MeetingIssueFiling(unittest.TestCase):
    def test_file_issue_meeting_preserves_attendees_and_facilitator(self):
        state = _seed()
        issue = sim.file_issue(state, 'dev', 'meeting', 'Sprint retro',
                               'DEV', 'ben', attendees=['ben', 'ada'],
                               facilitator_id='ada', now_ms=1000)
        self.assertIsNotNone(issue)
        self.assertEqual(issue['key'], 'DEV-1')
        self.assertEqual(issue['type'], 'meeting')
        self.assertEqual(issue['attendees'], ['ben', 'ada'])
        self.assertEqual(issue['facilitatorId'], 'ada')
        req = state['backlogRequests'][0]
        self.assertEqual(req['id'], 'iss-DEV-1')
        self.assertEqual(req['issueType'], 'meeting')
        self.assertEqual(req['attendees'], ['ben', 'ada'])
        self.assertEqual(req['facilitatorId'], 'ada')

    def test_file_issue_meeting_defaults_to_no_attendees(self):
        state = _seed()
        issue = sim.file_issue(state, 'dev', 'meeting', 'Retro', 'DEV',
                               'ben', now_ms=1000)
        self.assertIsNone(issue['attendees'])
        self.assertIsNone(issue['facilitatorId'])
        req = state['backlogRequests'][0]
        self.assertIsNone(req['attendees'])
        self.assertIsNone(req['facilitatorId'])

    def test_file_issue_rejects_unknown_issue_type(self):
        state = _seed()
        self.assertIsNone(
            sim.file_issue(state, 'dev', 'dance', 'T', 'DEV', 'ben'))

    def test_queue_work_whitelists_attendees_and_facilitator(self):
        state = {'workQueue': []}
        sim.queue_work(state, [{'title': 'Retro', 'room': 'pressoffice',
                                'attendees': ['ben'],
                                'facilitatorId': 'ada'}])
        item = state['workQueue'][0]
        self.assertEqual(item['attendees'], ['ben'])
        self.assertEqual(item['facilitatorId'], 'ada')

    def test_queue_meeting_round_trip(self):
        state = {'workQueue': []}
        sim.queue_meeting(state, 'Sprint retro', 'pressoffice', 'ada',
                          attendees=['dev', 'ben'], now_ms=1000,
                          budget_usd=0.25)
        item = state['workQueue'][0]
        self.assertEqual(item['taskType'], 'meeting')
        self.assertEqual(item['room'], 'pressoffice')
        self.assertEqual(item['facilitatorId'], 'ada')
        self.assertEqual(item['attendees'], ['dev', 'ben'])
        self.assertEqual(item['priority'], sim.WORK_PRIORITY['low'])
        self.assertEqual(item['budgetMs'], 900_000)
        self.assertEqual(item['budgetUsd'], 0.25)
        self.assertIn('minutes', (item['instructions'] or '').lower())

    def test_meeting_budget_tier(self):
        self.assertEqual(sim.budget_usd_for_task('meeting'), 0.10)
        self.assertEqual(
            sim.budget_usd_for_task('meeting', budget_band='generous'), 0.20)
        self.assertEqual(sim.budget_usd_for_task('meeting', budget_usd=0.5),
                         0.5)


class MeetingAttendeeResolution(unittest.TestCase):
    def test_normalize_attendees(self):
        self.assertEqual(
            sim._normalize_attendees(['a', 1, 'b', ' ']), ['a', 'b'])
        self.assertEqual(sim._normalize_attendees('  All '), 'all')
        self.assertIsNone(sim._normalize_attendees(None))
        self.assertIsNone(sim._normalize_attendees(''))

    def test_meeting_team_members(self):
        self.assertEqual(sim._meeting_team_members(_seed(), 'dev'),
                         ['dev', 'ada', 'ben'])
        self.assertEqual(sim._meeting_team_members(_seed(), 'nope'), [])

    def test_resolve_attendees_team_scope(self):
        self.assertEqual(sim._resolve_meeting_attendees(_seed(), None,
                                                        'dev'),
                         ['dev', 'ada', 'ben'])

    def test_resolve_attendees_all_scope(self):
        self.assertEqual(sim._resolve_meeting_attendees(_seed(), 'all',
                                                        'dev'),
                         ['ada', 'ben', 'dev', 'faye'])

    def test_resolve_attendees_pair_scope(self):
        self.assertEqual(
            sim._resolve_meeting_attendees(_seed(), 'pair', 'dev', 'ada'),
            ['ada', 'dev'])

    def test_resolve_attendees_explicit_list_drops_unknown(self):
        self.assertEqual(
            sim._resolve_meeting_attendees(_seed(), ['ben', 'ghost', 'ada'],
                                           'dev'),
            ['ben', 'ada'])

    def test_resolve_attendees_unknown_scope_defaults_to_team(self):
        self.assertEqual(
            sim._resolve_meeting_attendees(_seed(), 'bogus', 'dev'),
            ['dev', 'ada', 'ben'])


class MeetingGrooming(unittest.TestCase):
    def _groom(self, decider='accept', **file_kwargs):
        state = _seed()
        issue = sim.file_issue(state, 'dev', 'meeting', 'Sprint retro',
                               'DEV', 'ben', now_ms=1000, **file_kwargs)
        self.assertIsNotNone(issue)
        pending = {'reqIds': ['iss-DEV-1'], 'scrumMasterId': 'ada',
                   'people': {}}
        sim._resolve_refinement(state, pending, 2000,
                                decider=_stub_decider(decider))
        return state

    def test_accept_grooms_meeting_issue_into_meeting_task(self):
        state = self._groom(attendees='team', facilitator_id='ada')
        self.assertEqual(len(state['workQueue']), 1)
        item = state['workQueue'][0]
        self.assertEqual(item['taskType'], 'meeting')
        self.assertEqual(item['facilitatorId'], 'ada')
        self.assertEqual(item['attendees'], ['dev', 'ada', 'ben'])
        self.assertEqual(item['priority'], sim.WORK_PRIORITY['low'])
        self.assertEqual(item['budgetUsd'], 0.10)
        self.assertEqual(item['budgetBand'], 'standard')
        self.assertEqual(item['issueKey'], 'DEV-1')
        self.assertEqual(item['teamId'], 'dev')
        self.assertTrue((item['instructions'] or '').startswith(
            'Filed by ben during backlog refinement.'))
        self.assertEqual(state['backlogRequests'][0]['status'], 'accepted')

    def test_meeting_agenda_carries_the_story_context(self):
        # The filing's user story rides the meeting AGENDA as discussion
        # context (the minutes stay grounded in it); the acceptance
        # criteria -- a coded deliverable's contract -- do not.
        state = self._groom(description={
            'userStory':
                'As a team, I want to hold a retro, so that we improve.',
            'acceptanceCriteria':
                'Given the retro, When we vote, Then actions exist.'})
        item = state['workQueue'][0]
        self.assertIn('User story', item['instructions'] or '')
        self.assertNotIn('Acceptance criteria', item['instructions'] or '')

    def test_meeting_agenda_carries_never_prohibitions(self):
        state = self._groom(description={'never': ['No spending decisions.']})
        self.assertIn('NEVER', state['workQueue'][0]['instructions'] or '')

    def test_meeting_grooming_prices_the_meeting_tier(self):
        state = self._groom()
        self.assertEqual(state['workQueue'][0]['budgetUsd'], 0.10)

    def test_meeting_grooming_generous_band_doubles_the_ceiling(self):
        state = self._groom(decider='accept_generous')
        self.assertEqual(state['workQueue'][0]['budgetBand'], 'generous')
        self.assertEqual(state['workQueue'][0]['budgetUsd'], 0.20)

    def test_meeting_grooming_prefers_the_named_facilitator(self):
        state = self._groom(facilitator_id='dev')
        self.assertEqual(state['workQueue'][0]['facilitatorId'], 'dev')

    def test_meeting_grooming_falls_back_to_the_filer(self):
        state = self._groom(facilitator_id='ghost')
        self.assertEqual(state['workQueue'][0]['facilitatorId'], 'ben')

    def test_meeting_grooming_falls_back_to_the_scrum_master(self):
        # An unknown facilitator AND an unknown filer land on the team's
        # scrum master -- the one agent whose job is convening.
        state = _seed()
        sim.file_issue(state, 'dev', 'meeting', 'Retro', 'DEV', 'ghost',
                       now_ms=1000)
        pending = {'reqIds': ['iss-DEV-1'], 'scrumMasterId': 'ada',
                   'people': {}}
        sim._resolve_refinement(state, pending, 2000,
                                decider=_stub_decider('accept'))
        self.assertEqual(state['workQueue'][0]['facilitatorId'], 'ada')

    def test_rejected_meeting_issue_queues_nothing(self):
        state = self._groom(decider='reject')
        self.assertEqual(state['workQueue'], [])
        self.assertEqual(state['backlogRequests'][0]['status'], 'rejected')


class MeetingAssignment(unittest.TestCase):
    def test_assign_task_carries_meeting_fields(self):
        state = {'agents': {'ada': {'id': 'ada', 'x': 0, 'y': 0,
                                    'busy': False, 'task': None,
                                    'offDuty': False}},
                 'tasks': {}}
        extra = {'taskType': 'meeting', 'attendees': ['dev', 'ben'],
                 'facilitatorId': 'ada', 'budgetUsd': 0.10,
                 'budgetBand': 'standard'}
        with mock.patch.object(sim, 'find_path',
                               return_value=[{'x': 0, 'y': 0},
                                             {'x': 40, 'y': 40}]):
            task = sim.assign_task(state, 'ada', 'Meeting: retro',
                                   'pressoffice', 'Convene the retro.',
                                   None, extra, {},
                                   {'pressoffice': {'x': 0, 'y': 0,
                                                    'w': 40, 'h': 40}},
                                   now_ms=1000, task_id_holder=[0])
        self.assertIsNotNone(task)
        self.assertEqual(task['taskType'], 'meeting')
        self.assertEqual(task['attendees'], ['dev', 'ben'])
        self.assertEqual(task['facilitatorId'], 'ada')
        self.assertEqual(task['assignedTo'], 'ada')
        self.assertEqual(task['budgetUsd'], 0.10)

    def test_assign_due_item_pins_and_wakes_the_off_duty_facilitator(self):
        state = _seed()
        state['agents']['ada']['offDuty'] = True
        pick = {'title': 'Meeting: Sprint retro', 'room': 'pressoffice',
                'taskType': 'meeting', 'facilitatorId': 'ada',
                'attendees': ['dev', 'ben'],
                'instructions': 'Convene the retro and write the minutes.',
                'teamId': 'dev', 'notBefore': None}
        grid, doors = sim._load_outdoor_geometry()
        task = sim._assign_due_item(state, pick, True, grid, doors, 1000,
                                    task_id_holder=[0])
        self.assertIsNotNone(task)
        self.assertEqual(task['assignedTo'], 'ada')
        self.assertEqual(task['facilitatorId'], 'ada')
        self.assertEqual(task['attendees'], ['dev', 'ben'])
        self.assertFalse(state['agents']['ada']['offDuty'])

    def test_meeting_completion_files_no_followup(self):
        # A meeting's minutes ARE the deliverable: completing one must
        # not propose the next story (the recursion the skip list
        # guards against).
        state = _seed()
        sim._maybe_file_followup(
            state, 'ben', {'room': 'pressoffice', 'taskType': 'meeting',
                           'title': 'Meeting: retro'}, 1000)
        self.assertEqual(state['backlogRequests'], [])
        self.assertEqual(state['workQueue'], [])

    def test_code_completion_in_a_thin_room_still_files(self):
        # Control for the skip above: a REAL deliverable completion in a
        # thin room still earns its free spike and files the follow-up.
        state = _seed()
        sim._maybe_file_followup(
            state, 'ben', {'room': 'pressoffice', 'taskType': 'code',
                           'title': 'Build the dashboard'}, 1000)
        self.assertEqual(len(state['backlogRequests']), 1)
        self.assertTrue(any(it.get('moonshot')
                            for it in state['workQueue']))


class MeetingExecutor(unittest.TestCase):
    def _task(self):
        return {'id': 'task-1', 'title': 'Meeting: Sprint retro',
                'room': 'pressoffice', 'taskType': 'meeting',
                'instructions': 'Agenda: what shipped, what slowed us down.',
                'facilitatorId': 'ada', 'attendees': ['dev', 'ben']}

    def _run(self, agent_id='ada', exhausted=False, tier='test-tier',
             completion=None):
        state = _seed()
        task = self._task()
        stored = []
        with mock.patch.object(content._serve, '_task_budget_exhausted',
                               return_value=exhausted), \
             mock.patch.object(content._serve, '_task_budget_spent',
                               return_value=(0.10, 5)), \
             mock.patch.object(content._serve, '_resolve_model_tier',
                               return_value=tier), \
             mock.patch.object(content._serve, '_call_openrouter_sync',
                               return_value=(completion
                                             if completion is not None
                                             else _completion(_MINUTES))), \
             mock.patch.object(
                 sim, '_store_content_result',
                 side_effect=lambda tid, result: stored.append((tid, result))):
            content._run_meeting_content(state, agent_id, task)
        return stored

    def test_minutes_land_in_the_card_note(self):
        stored = self._run()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0][0], 'task-1')
        self.assertEqual(stored[0][1], {'note': _MINUTES, 'ok': True})

    def test_one_completion_names_the_facilitator_and_attendees(self):
        state = _seed()
        task = self._task()
        calls = []

        def fake_call(model, messages, max_tokens):
            calls.append((model, messages, max_tokens))
            return _completion(_MINUTES)

        with mock.patch.object(content._serve, '_task_budget_exhausted',
                               return_value=False), \
             mock.patch.object(content._serve, '_resolve_model_tier',
                               return_value='test-tier'), \
             mock.patch.object(content._serve, '_call_openrouter_sync',
                               side_effect=fake_call), \
             mock.patch.object(sim, '_store_content_result'):
            content._run_meeting_content(state, 'ada', task)
        self.assertEqual(len(calls), 1)
        model, messages, max_tokens = calls[0]
        self.assertEqual(model, 'test-tier')
        self.assertEqual(max_tokens, 1200)
        self.assertEqual([m['role'] for m in messages], ['system', 'user'])
        system = messages[0]['content']
        self.assertIn('Follow this playbook', system)
        self.assertIn('**Action items**', system)
        user = messages[1]['content']
        for token in ('Meeting: Sprint retro', 'Ada', 'Dev', 'Ben',
                      'what shipped, what slowed us down.'):
            self.assertIn(token, user)

    def test_full_transcript_mode_writes_the_conversation_not_minutes(self):
        state = _seed()
        task = self._task()
        task['instructions'] = ('[FULL TRANSCRIPT] Agenda: what shipped, what slowed us down.')
        calls = []

        def fake_call(model, messages, max_tokens):
            calls.append((model, messages, max_tokens))
            return _completion('Theo: We should ship it.\nNora: Agreed.')

        with mock.patch.object(content._serve, '_task_budget_exhausted',
                               return_value=False), \
             mock.patch.object(content._serve, '_resolve_model_tier',
                               return_value='test-tier'), \
             mock.patch.object(content._serve, '_call_openrouter_sync',
                               side_effect=fake_call), \
             mock.patch.object(sim, '_store_content_result') as stored:
            content._run_meeting_content(state, 'ada', task)
        model, messages, max_tokens = calls[0]
        self.assertEqual(max_tokens, 7000)
        self.assertIn('full transcript', messages[0]['content'])
        self.assertNotIn('Follow this playbook', messages[0]['content'])
        self.assertNotIn('[FULL TRANSCRIPT]', messages[1]['content'])
        self.assertEqual(stored.call_args[0][1]['note'],
                         'Theo: We should ship it.\nNora: Agreed.')

    def test_budget_exhausted_meeting_fails_closed_before_any_call(self):
        state = _seed()
        task = self._task()
        stored = []
        with mock.patch.object(content._serve, '_task_budget_exhausted',
                               return_value=True), \
             mock.patch.object(content._serve, '_task_budget_spent',
                               return_value=(0.10, 5)), \
             mock.patch.object(content._serve, '_call_openrouter_sync') as call, \
             mock.patch.object(
                 sim, '_store_content_result',
                 side_effect=lambda tid, result: stored.append((tid, result))):
            content._run_meeting_content(state, 'ada', task)
        call.assert_not_called()
        self.assertEqual(len(stored), 1)
        self.assertFalse(stored[0][1]['ok'])
        self.assertTrue(stored[0][1]['budgetExhausted'])
        self.assertEqual(stored[0][1]['taskSpendUsd'], 0.10)
        self.assertEqual(stored[0][1]['taskSpendAttempts'], 5)

    def test_missing_model_tier_fails_closed(self):
        stored = self._run(tier='')
        self.assertEqual(len(stored), 1)
        self.assertFalse(stored[0][1]['ok'])
        self.assertIn('no model tier', stored[0][1]['note'])

    def test_empty_completion_fails_closed_with_an_explicit_note(self):
        # Completion evidence is mandatory: a meeting that cannot write
        # minutes still lands an explicit note, never a silent empty card.
        stored = self._run(completion=_completion('   '))
        self.assertEqual(len(stored), 1)
        self.assertFalse(stored[0][1]['ok'])
        self.assertIn('could not be written', stored[0][1]['note'])

    def test_dispatcher_routes_a_meeting_before_its_room(self):
        # taskType 'meeting' is checked before room: a meeting queued
        # into ANY room writes minutes, never the room's craft note.
        state = _seed()
        meeting_run = mock.MagicMock()
        workroom_run = mock.MagicMock()
        with mock.patch.object(content, '_run_meeting_content', meeting_run), \
             mock.patch.object(content, '_run_workroom_content',
                               workroom_run):
            content._server_content_dispatcher(
                state, 'ada', {'taskType': 'meeting', 'room': 'pressoffice'})
        meeting_run.assert_called_once()
        workroom_run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
