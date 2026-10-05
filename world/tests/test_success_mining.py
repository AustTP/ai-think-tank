"""Coverage tests for the positive half of the rule-mining loop: serve.py's
success ledger and success-lesson proposals, the player-only surface, and
sim.py's weekly success-mine + consensus-relay steps, plus the two feedback
injection seams (_grade_completed_task's success branch and _augment_task_
instructions's relay prepend).

ChatDev ECL comparison: "what made a good deliverable good" should propagate
into future work the same way "what went wrong" does. Mirrors
test_failure_ledger.py's isolation contract: every real file path is redirected
into a temp dir, including the derived SUCCESSES_PATH / SUCCESS_PROPOSALS_PATH
computed at import time from the real THINK_TANK_DIR. Jev seams are patched per test.



Run (isolated coverage file):
  cd /Users/poole86/ai-village-template/world
  COVERAGE_FILE=/tmp/cov_success_mining.coverage python3 -m coverage run --source=serve,sim tests/test_success_mining.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
import sim

_TMP_DIR = None
_MODULE_PATCHER = None
_EXTRA_PATCHER = None


def setUpModule():
    global _TMP_DIR, _MODULE_PATCHER, _EXTRA_PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-success-mining-')
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        THINK_TANK_DIR=_TMP_DIR,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
    )
    _MODULE_PATCHER.start()
    _EXTRA_PATCHER = unittest.mock.patch.multiple(
        serve,
        SUCCESSES_PATH=os.path.join(_TMP_DIR, 'successes.json'),
        SUCCESS_PROPOSALS_PATH=os.path.join(_TMP_DIR, 'success_proposals.json'),
    )
    _EXTRA_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _EXTRA_PATCHER.stop()
    _MODULE_PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class _LedgerTestCase(unittest.TestCase):
    def setUp(self):
        for p in (serve.SUCCESSES_PATH, serve.SUCCESS_PROPOSALS_PATH):
            if os.path.exists(p):
                os.remove(p)


class RecordSuccess(_LedgerTestCase):
    def test_appends_record_with_truncation_and_id(self):
        room_str = 'r' * 200
        title_str = 't' * 300
        section_str = 's' * 150
        grade_val = 8.5
        success_id = serve._record_success(room_str, title_str, grade_val,
                                          agent_id='ada', section=section_str)
        grade2 = 9.0
        serve._record_success(None, None, grade2)
        rows = serve._load_successes()
        assert len(rows) == 2
        first = rows[0]
        assert first.get('id') == success_id
        assert len(first.get('room')) == 80
        assert len(first.get('title')) == 200
        assert len(first.get('section')) == 120
        assert first.get('agentId') == 'ada'
        assert first.get('grade') == 8.5
        assert rows[1].get('room') == ''
        assert rows[1].get('title') == ''

    def test_ledger_bounded_to_tail(self):
        rec_grade = 8.5
        for i in range(serve.SUCCESS_MAX_RECORDS + 10):
            serve._record_success('observatory', f'title {i}', rec_grade)
        rows = serve._load_successes()
        assert len(rows) == serve.SUCCESS_MAX_RECORDS
        expected_last = 'title {0}'.format(serve.SUCCESS_MAX_RECORDS + 9)
        assert rows[-1].get('title') == expected_last
        assert rows[0].get('title') == 'title 10'

    def test_load_successes_non_list_returns_empty(self):
        with open(serve.SUCCESSES_PATH, 'w') as f:
            f.write('{}')
        assert serve._load_successes() == []

    def test_load_success_proposals_non_list_returns_empty(self):
        with open(serve.SUCCESS_PROPOSALS_PATH, 'w') as f:
            f.write('{"nope": 1}')
        assert serve._load_success_proposals() == []


class SuccessTextFor(unittest.TestCase):
    def test_builds_lesson_from_room_count_and_avg(self):
        room_name = 'observatory'
        count_val = 2
        avg_val = 8.75
        text = serve._success_text_for(room_name, count_val, avg_val)
        assert 'observatory' in text
        assert '2 recent deliverables' in text
        assert '8.8/10' in text
        assert 'Apply that standard' in text


class MineSuccessProposals(_LedgerTestCase):
    def _seed_room(self, room_name, n=2):
        base_grade = 8.5
        step_grade = 0.5
        for i in range(n):
            title_text = 'Work {0} {1}'.format(room_name, i)
            serve._record_success(room_name, title_text, base_grade + i * step_grade,
                                 section='sec', agent_id='ada')

    def test_recurring_room_becomes_proposal_with_lesson_and_fixture(self):
        self._seed_room('observatory')
        created = serve._mine_success_proposals(now_ts=1000.0)
        assert len(created) == 1
        proposal = created[0]
        assert proposal.get('room') == 'observatory'
        assert proposal.get('count') == 2
        assert proposal.get('avgGrade') == 8.8
        assert 'observatory' in proposal.get('lesson')
        assert '8.8/10' in proposal.get('lesson')
        assert proposal.get('status') == 'pending'
        assert proposal.get('dedupeKey') == 'success:observatory'
        assert 'succ-rule-' in proposal.get('id')
        assert proposal.get('ts') == 1000.0
        examples_list = proposal.get('examples')
        assert len(examples_list) == 2
        assert examples_list[0].get('title') == 'Work observatory 0'
        fixture_map = proposal.get('fixture')
        assert fixture_map.get('title') == 'Work observatory 0'
        assert fixture_map.get('room') == 'observatory'
        assert len(serve._load_success_proposals()) == 1

    def test_below_recurrence_no_proposal_and_no_save(self):
        self._seed_room('observatory', n=1)
        assert serve._mine_success_proposals() == []
        assert os.path.exists(serve.SUCCESS_PROPOSALS_PATH) is False

    def test_already_proposed_room_deduped(self):
        self._seed_room('observatory')
        serve._mine_success_proposals()
        self._seed_room('observatory')
        assert serve._mine_success_proposals() == []

    def test_empty_room_records_ignored(self):
        grade_val = float('8.5')
        serve._record_success('', 'no room', grade_val)
        self._seed_room('pressoffice')
        created = serve._mine_success_proposals(now_ts=2000.0)
        assert len(created) == 1
        assert created[0].get('room') == 'pressoffice'

    def test_custom_recurrence_threshold(self):
        self._seed_room('observatory')
        assert serve._mine_success_proposals(recurrence=3) == []

    def test_proposals_bounded(self):
        for i in range(serve.SUCCESS_PROPOSALS_MAX + 5):
            room_name = 'room-{0}'.format(i)
            self._seed_room(room_name)
        created = serve._mine_success_proposals(now_ts=3000.0)
        assert len(created) == serve.SUCCESS_PROPOSALS_MAX + 5
        assert len(serve._load_success_proposals()) == serve.SUCCESS_PROPOSALS_MAX


class SuccessProposalsEndpoint(_LedgerTestCase):
    def _request(self):
        return unittest.mock.MagicMock(cookies={})

    def test_player_reads_proposals(self):
        grade_a = float('8.5')
        grade_b = float('9.0')
        serve._record_success('observatory', 'A', grade_a)
        serve._record_success('observatory', 'B', grade_b)
        serve._mine_success_proposals()
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            resp = asyncio.run(serve.success_proposals_get(self._request()))
        import json as _json
        data = _json.loads(resp.body)
        assert len(data.get('proposals')) == 1
        assert data.get('proposals')[0].get('room') == 'observatory'

    def test_unauthenticated_denied(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False):
            resp = asyncio.run(serve.success_proposals_get(self._request()))
        assert resp.status_code == 403


class SuccessMineStep(unittest.TestCase):
    def test_mines_on_cadence_and_logs_governance(self):
        state = {}
        proposals_list = [
            {'id': 'succ-rule-1', 'room': 'observatory',
             'lesson': 'Keep doing what makes the observatory room ship well.',
             'count': 2, 'dedupeKey': 'success:observatory'},
        ]
        with unittest.mock.patch.object(serve, '_mine_success_proposals', return_value=proposals_list) as mine:

            sim._success_mine_step(state, sim.RULE_MINE_CADENCE_MS + 1)
        mine.assert_called_once()
        assert state.get('lastSuccessMineAt') == sim.RULE_MINE_CADENCE_MS + 1
        with serve._db() as conn:
            rows = conn.execute("SELECT details FROM action_log WHERE action = 'success_proposal'").fetchall()
        assert rows
        assert '"proposalId": "succ-rule-1"' in rows[-1][0]
        assert '"dedupeKey": "success:observatory"' in rows[-1][0]

    def test_cadence_gated(self):
        state = {}
        with unittest.mock.patch.object(serve, '_mine_success_proposals',
                                        side_effect=AssertionError('must not mine early')):
            sim._success_mine_step(state, 1)
        assert 'lastSuccessMineAt' not in state

    def test_ledger_failure_swallowed(self):
        state = {}
        with unittest.mock.patch.object(serve, '_mine_success_proposals',
                                        side_effect=RuntimeError('file lock')):
            sim._success_mine_step(state, sim.RULE_MINE_CADENCE_MS + 1)
        assert state.get('lastSuccessMineAt') == sim.RULE_MINE_CADENCE_MS + 1


class ConsensusRelay(unittest.TestCase):
    def _state(self, roadmap_map, deliverables_list=None):
        return {'roadmap': roadmap_map, 'completedDeliverables': deliverables_list or []}

    def test_no_roadmap_is_a_noop(self):
        state = self._state({})
        sim._consensus_relay_step(state, 1000)
        assert 'consensusRelay' not in state

    def test_weak_room_reason(self):
        roadmap_map = {'observatory': {'priority': 2}, 'pressoffice': {'priority': 1}}
        deliverables_list = [{'room': 'observatory', 'grade': float('4.0'), 'gradeIsReal': True}]
        state = self._state(roadmap_map, deliverables_list)
        sim._consensus_relay_step(state, 1000)
        relay_map = state.get('consensusRelay')
        assert relay_map.get('room') == 'observatory'
        assert 'weak trailing grade (4.0/10)' in relay_map.get('consensus')
        assert relay_map.get('updatedAt') == 1000

    def test_starved_room_reason(self):
        state = self._state({'observatory': {'priority': 2}})
        sim._consensus_relay_step(state, 1000)
        relay_map = state.get('consensusRelay')
        assert relay_map.get('room') == 'observatory'
        assert 'no recent delivery' in relay_map.get('consensus')

    def test_no_reason_defaults_to_director_priority(self):
        roadmap_map = {'pressoffice': {'priority': 2}, 'observatory': {'priority': 1}}
        deliverables_list = [{'room': 'pressoffice', 'grade': float('9.0'), 'gradeIsReal': True}]
        state = self._state(roadmap_map, deliverables_list)
        sim._consensus_relay_step(state,  1000)
        relay_map = state.get('consensusRelay')
        assert relay_map.get('room') == 'pressoffice'
        assert 'prioritized by the director' in relay_map.get('consensus')

    def test_relay_note_reads_none_or_consensus(self):
        assert sim._consensus_relay_note({}) is None
        state = {'consensusRelay': {'consensus': 'Current direction: focus next work on pressoffice.'}}
        assert sim._consensus_relay_note(state) == 'Current direction: focus next work on pressoffice.'


class AugmentTaskInstructionsRelay(unittest.TestCase):
    def test_relay_is_read_first(self):
        state = {'consensusRelay': {'consensus': 'Current direction: focus next work on observatory.'},
                 'growthPlans': {'ada': [{'kind': 'low_grade', 'applied': False,
                                           'note': 'Coaching: meet the spec.'}]}}
        aug = sim._augment_task_instructions(state, 'ada', None, 'Original instructions')
        lines_list = aug.split('\n')
        assert lines_list[0] == 'Current direction: focus next work on observatory.'
        assert lines_list[1] == 'Original instructions'
        assert 'Coaching: meet the spec.' in lines_list[2]
        assert state.get('growthPlans').get('ada')[0].get('applied') is True

    def test_no_relay_passthrough(self):
        state = {'growthPlans': {}}
        assert sim._augment_task_instructions(state, 'ada', None, 'Plain task') == 'Plain task'


class GradeSuccessBranch(unittest.TestCase):
    def _task(self):
        return {'id': 't1', 'room': 'pressoffice', 'title': 'Great parser',
                'taskType': 'feature', 'assignedTo': 'ada'}

    def test_high_grade_records_success(self):
        state = {'agents': {}}
        old_decider = sim._grading_decider
        sim._grading_decider = lambda *a, **k: float('8.5')
        with unittest.mock.patch.object(serve, '_record_success') as recorder:

            sim._grade_completed_task(state, 'ada', self._task(), 1000)
        sim._grading_decider = old_decider
        assert recorder.call_count == 1
        call_args = recorder.call_args
        assert call_args[0][:3] == ('pressoffice', 'Great parser', float('8.5'))
        assert call_args[1].get('agent_id') == 'ada'
        assert len(state.get('completedDeliverables')) == 1

    def test_ledger_failure_swallowed(self):
        state = {'agents': {}}
        old_decider = sim._grading_decider
        sim._grading_decider = lambda *a, **k: float('9.0')
        with unittest.mock.patch.object(serve, '_record_success',
                                        side_effect=RuntimeError('file lock')):
            sim._grade_completed_task(state, 'ada', self._task(), 2000)
        sim._grading_decider = old_decider
        assert len(state.get('completedDeliverables')) == 1


if __name__ == '__main__':
    unittest.main()
