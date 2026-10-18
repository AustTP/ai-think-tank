"""Supervisor-meeting and budget-review endpoints (asks 8 and 9).

planner.py and story_budget.py are pure and already tested on
their own. These tests cover the WIRING in serve: the
operator-gated POST /api/supervisor/meeting endpoint that runs
a SupervisorMeeting over the roster view of the sim state and
persists the budget lines it opens plus a meeting audit record,
the GET /api/budget/story overview, and the operator-gated
POST /api/budget/review endpoint that records a director's
grant-or-stop decision on an over-budget story.

DB-isolated (setUpModule below, the test_vpn_exit_endpoints
pattern).
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-meeting-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        BROWSE_TRAIL_PATH=os.path.join(_TMP_DIR, 'browse_trail.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _client():
    from starlette.testclient import TestClient
    c = TestClient(serve.app)
    c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
    return c


def _seed(state):
    serve.save_state_to_db(state)
    return serve.get_state_from_db()


def _roster_state(off_duty=False):
    return {
        'agents': {
            'ada': {'id': 'ada', 'offDuty': off_duty, 'busy': False,
                    'task': None, 'x': 0, 'y': 0},
        },
        'agentRoster': [{'id': 'ada', 'director': 'dev'}],
        'teams': [{'id': 'dev'}],
    }


def _submission():
    return {
        'title': 'Next batch',
        'goal': 'Ship the observatory work',
        'items': ['wire the ticker', 'write the runbook'],
        'specialties': {'dev': ['ticker', 'runbook']},
        'durations': {'wire the ticker': 30.0,
                      'write the runbook': 45.0},
    }


class SupervisorMeetingEndpoint(unittest.TestCase):
    def _meeting(self, body, operator=(True, 'player')):
        with unittest.mock.patch.object(serve, '_operator_gate',
                                        return_value=operator):
            return _client().post('/api/supervisor/meeting', json=body)

    def test_meeting_requires_the_operator_gate(self):
        _seed(_roster_state())
        response = self._meeting(_submission(), operator=(False, None))
        self.assertEqual(response.status_code, 401)

    def test_meeting_requires_title_goal_items_and_specialties(self):
        _seed(_roster_state())
        for body in (
            {},
            {'goal': 'g', 'items': ['i'], 'specialties': {'dev': ['i']}},
            {'title': 't', 'items': ['i'], 'specialties': {'dev': ['i']}},
            {'title': 't', 'goal': 'g', 'specialties': {'dev': ['i']}},
            {'title': 't', 'goal': 'g', 'items': ['i']},
            {'title': 't', 'goal': 'g', 'items': []},
            {'title': 't', 'goal': 'g', 'items': ['i'], 'specialties': {}},
        ):
            response = self._meeting(body)
            self.assertEqual(response.status_code, 400)

    def test_meeting_requires_a_roster(self):
        _seed({})
        response = self._meeting(_submission())
        self.assertEqual(response.status_code, 409)
        self.assertIn('roster', response.json()['error'])

    def test_meeting_breaks_down_and_assigns_stories(self):
        _seed(_roster_state())
        response = self._meeting(_submission())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        brief = body['brief']
        self.assertEqual(brief['totalItems'], 2)
        self.assertEqual(brief['totalMinutes'], 75.0)
        stories = body['stories']
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]['itemIds'], ['w-0001', 'w-0002'])
        self.assertEqual(stories[0]['estimateMinutes'], 75.0)
        assignments = body['assignments']
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0]['team'], 'dev')
        self.assertEqual(assignments[0]['storyIds'], [stories[0]['storyId']])

    def test_meeting_persists_the_ledger_and_an_audit_record(self):
        _seed(_roster_state())
        response = self._meeting(_submission())
        self.assertEqual(response.status_code, 200)
        story_id = response.json()['stories'][0]['storyId']
        state = serve.get_state_from_db()
        ledger = state['budgetLedger']
        self.assertEqual(
            [line['story_id'] for line in ledger['lines']], [story_id])
        # The line's budget is the story's estimate scaled by
        # the (empty) overrun history, so scale 1.0.
        self.assertEqual(ledger['lines'][0]['budget_minutes'], 75.0)
        meetings = state['meetings']
        self.assertEqual(len(meetings), 1)
        self.assertEqual(meetings[0]['title'], 'Next batch')
        self.assertEqual(meetings[0]['decidedBy'], 'player')
        self.assertEqual(len(meetings[0]['stories']), 1)

    def test_meeting_scales_budgets_by_overrun_history(self):
        snapshot = {
            'lines': [],
            'alerts': [],
            'reviews': [{
                'story_id': 1, 'reason': 'r', 'decision': 'grant',
                'completed_items': 1, 'total_items': 1,
                'spent_minutes': 90.0, 'budget_minutes': 30.0,
                'additional_minutes': 30.0,
            }],
        }
        state = _roster_state()
        state['budgetLedger'] = snapshot
        _seed(state)
        response = self._meeting(_submission())
        self.assertEqual(response.status_code, 200)
        # overrun factor 3.0: the new line's budget is the
        # estimate times 4.
        ledger = serve.get_state_from_db()['budgetLedger']
        self.assertEqual(ledger['lines'][0]['budget_minutes'], 300.0)

    def test_work_no_team_can_take_is_dropped(self):
        state = _roster_state()
        # A team with no agents has no capacity: the
        # specialty match never claims, and the capacity
        # fallback has nobody left.
        state['teams'] = [{'id': 'dev'}]
        state['agentRoster'] = [{'id': 'bee', 'director': 'other'}]
        state['agents'] = {'bee': {'id': 'bee', 'offDuty': False,
                                   'busy': False, 'task': None,
                                   'x': 0, 'y': 0}}
        state['agentRoster'] = [{'id': 'bee', 'director': 'other'}]
        _seed(state)
        response = self._meeting(_submission())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['assignments'], [])


class BudgetStoryOverviewEndpoint(unittest.TestCase):
    def test_overview_is_empty_without_a_ledger(self):
        _seed({})
        response = _client().get('/api/budget/story')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(),
                         {'alerts': [], 'reviews': [],
                          'overrunFactor': 0.0, 'lines': []})

    def test_overview_reads_the_persisted_ledger(self):
        _seed(_roster_state())
        self._meeting_seed()
        response = _client().get('/api/budget/story')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body['lines']), 1)
        self.assertEqual(body['overrunFactor'], 0.0)

    def _meeting_seed(self):
        with unittest.mock.patch.object(serve, '_operator_gate',
                                        return_value=(True, 'player')):
            return _client().post(
                '/api/supervisor/meeting', json=_submission())


class BudgetReviewEndpoint(unittest.TestCase):
    def _review(self, body, operator=(True, 'player')):
        with unittest.mock.patch.object(serve, '_operator_gate',
                                        return_value=operator):
            return _client().post('/api/budget/review', json=body)

    def _ledger_state(self):
        state = _roster_state()
        _seed(state)
        with unittest.mock.patch.object(serve, '_operator_gate',
                                        return_value=(True, 'player')):
            _client().post('/api/supervisor/meeting',
                           json=_submission())
        state = serve.get_state_from_db()
        story_id = state['budgetLedger']['lines'][0]['story_id']
        return state, story_id

    def test_review_requires_the_operator_gate(self):
        state, story_id = self._ledger_state()
        _seed(state)
        response = self._review({'storyId': story_id, 'reason': 'r',
                                 'decision': 'stop'},
                                operator=(False, None))
        self.assertEqual(response.status_code, 401)

    def test_review_requires_a_story_and_a_decision(self):
        state, story_id = self._ledger_state()
        _seed(state)
        for body in (
            {},
            {'storyId': story_id, 'reason': 'r'},
            {'reason': 'r', 'decision': 'stop'},
            {'storyId': story_id, 'reason': 'r', 'decision': 'maybe'},
        ):
            response = self._review(body)
            self.assertEqual(response.status_code, 400)

    def test_a_grant_requires_positive_additional_minutes(self):
        state, story_id = self._ledger_state()
        _seed(state)
        for additional in (None, 0, -5, 'soon'):
            response = self._review({
                'storyId': story_id, 'reason': 'r',
                'decision': 'grant', 'additionalMinutes': additional,
            })
            self.assertEqual(response.status_code, 400)

    def test_review_without_a_ledger_is_a_409(self):
        _seed({})
        response = self._review({'storyId': 1, 'reason': 'r',
                                 'decision': 'stop'})
        self.assertEqual(response.status_code, 409)

    def test_review_of_an_unknown_story_is_a_404(self):
        state, _ = self._ledger_state()
        _seed(state)
        response = self._review({'storyId': 999, 'reason': 'r',
                                 'decision': 'stop'})
        self.assertEqual(response.status_code, 404)

    def test_a_stop_decision_closes_the_line(self):
        state, story_id = self._ledger_state()
        _seed(state)
        response = self._review({'storyId': story_id,
                                 'reason': 'scope was wrong',
                                 'decision': 'stop'})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['review']['decision'], 'stop')
        self.assertTrue(body['line']['closed'])
        state = serve.get_state_from_db()
        line = state['budgetLedger']['lines'][0]
        self.assertTrue(line['closed'])
        self.assertEqual(len(state['budgetLedger']['reviews']), 1)

    def test_a_grant_decision_adds_minutes(self):
        state, story_id = self._ledger_state()
        _seed(state)
        response = self._review({'storyId': story_id,
                                 'reason': 'transcript was dense',
                                 'decision': 'grant',
                                 'additionalMinutes': 20.0})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['review']['additionalMinutes'], 20.0)
        self.assertEqual(body['line']['budgetMinutes'], 95.0)
        state = serve.get_state_from_db()
        self.assertEqual(
            state['budgetLedger']['lines'][0]['budget_minutes'], 95.0)


if __name__ == '__main__':
    unittest.main()
