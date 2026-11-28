"""Supervisor budget autopilot (2026-11-28, operator directive): a card that
exhausted its budget gets a server-side supervisor decision (grant via the
enforced desk tool, or close) without waiting for the player, and the original
author is told the ceiling was raised so they can resume when free."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import sim  # noqa: E402
import serve  # noqa: E402

NOW = 1_770_000_000_000


def _exhausted_task(**over):
    task = {'id': 't1', 'title': 'Draft the pilot', 'room': 'observatory',
            'taskType': 'research', 'status': 'failed', 'budgetExhausted': True,
            '_budgetExhaustedNotified': True, 'budgetUsd': 0.15,
            'budgetExhaustedAtMs': NOW - 60_000, 'assignedTo': 'ada',
            'teamId': 'team-1', 'createdAt': NOW - 120_000}
    task.update(over)
    return task


def _state(task):
    return {'agents': {'ada': {'id': 'ada', 'mailbox': []}},
            'tasks': {'t1': task},
            'teams': [{'id': 'team-1', 'directorId': 'ruth'}],
            'agentRoster': []}


class SupervisorBudgetStep(unittest.TestCase):
    def _decide(self, state, task):
        self.decided.append(task['id'])
        return self.decision

    def _consult(self, state, task, spent, attempts, question=None):
        self.consulted.append(question)
        report = dict(self.worker_report)
        if question:
            report['reason'] = 'reply to the follow-up'
        return report

    def setUp(self):
        self.decided = []
        self.consulted = []
        self.decision = None
        self.worker_report = {'completed': 'outline and two scenes',
                              'whyOver': 'the transcript ran longer than planned',
                              'moreBudgetUsd': 0.30,
                              'reason': 'three scenes remain'}

    def _step(self, state, **kw):
        params = {'decide': self._decide, 'consult': self._consult}
        params.update(kw)
        with mock.patch.object(serve, 'log_action'):
            return serve._supervisor_budget_step(state, **params)

    def _apply(self, state, **kw):
        with mock.patch.object(serve, 'log_action'):
            return serve._supervisor_budget_apply(state, **kw)

    def test_grant_is_clamped_to_the_supervisor_cap(self):
        task = _exhausted_task()
        state = _state(task)
        self.decision = {'decision': 'grant', 'budgetUsd': 50,
                         'reason': 'the spend bought real progress on the draft'}
        grants = []

        def grant(agent_id, task_id, amount, reason):
            grants.append((agent_id, task_id, amount, reason))
            return True, {'ok': True}

        made = self._step(state)
        applied = self._apply(state, grant=grant)
        self.assertEqual((made, applied), (1, 1))
        self.assertEqual(grants[0][0], 'ruth')
        self.assertEqual(grants[0][2], 5.00)  # the $50 ask clamps to the cap
        decision = task['_supervisorBudgetDecision']
        self.assertEqual(decision['decision'], 'grant')
        self.assertTrue(decision['applied'])
        self.assertTrue(decision['grantOk'])

    def test_grant_amount_respects_the_spent_floor(self):
        task = _exhausted_task()
        state = _state(task)
        self.decision = {'decision': 'grant', 'budgetUsd': 0.05,
                         'reason': 'the spend bought real progress on the draft'}
        grants = []

        def grant(agent_id, task_id, amount, reason):
            grants.append((agent_id, task_id, amount, reason))
            return True, {'ok': True}

        with mock.patch.object(serve, '_task_budget_spent',
                               return_value=(0.14, 3)):
            self._step(state)
            self._apply(state, grant=grant)
        self.assertEqual(grants[0][2], 0.19)  # spent 0.14 + the 0.05 floor

    def test_close_decision_records_the_reason(self):
        task = _exhausted_task()
        state = _state(task)
        self.decision = {'decision': 'close', 'budgetUsd': None,
                         'reason': 'the work duplicated an existing deliverable'}
        grants = []

        def grant(agent_id, task_id, amount, reason):
            grants.append(1)
            return False, {'error': 'should not be called'}

        made = self._step(state)
        applied = self._apply(state, grant=grant)
        self.assertEqual((made, applied), (1, 1))
        self.assertEqual(grants, [])
        self.assertEqual(task['_supervisorBudgetDecision']['decision'], 'close')
        self.assertIn('Supervisor closed it', task['failNote'])

    def test_a_decided_card_is_not_decided_twice(self):
        task = _exhausted_task(
            _supervisorBudgetDecision={'decision': 'close', 'applied': True})
        state = _state(task)
        made = self._step(state)
        applied = self._apply(state, grant=lambda *a: (True, {}))
        self.assertEqual((made, applied, self.decided), (0, 0, []))

    def test_a_human_grant_racing_in_is_observed_not_clobbered(self):
        task = _exhausted_task(
            budgetGrants=[{'by': 'ruth', 'to': 0.5}],
            _supervisorBudgetDecision={'decision': 'grant', 'budgetUsd': 0.3,
                                       'applied': False})
        state = _state(task)
        applied = self._apply(state, grant=lambda *a: (True, {}))
        self.assertEqual(applied, 1)
        self.assertTrue(task['_supervisorBudgetDecision']['humanGrantObserved'])
        self.assertEqual(task['budgetGrants'][0]['by'], 'ruth')

    def test_model_outage_defers_to_the_human_after_the_retry_budget(self):
        task = _exhausted_task()
        state = _state(task)
        calls = []

        def decide(state, t):
            calls.append(1)
            raise RuntimeError('model down')

        with mock.patch.object(serve, 'log_action'), \
                mock.patch.object(serve, 'SUPERVISOR_BUDGET_RETRY_COOLDOWN_MS', 0):
            for _ in range(serve.SUPERVISOR_BUDGET_MAX_LLM_ATTEMPTS):
                serve._supervisor_budget_step(state, decide=decide,
                                              consult=self._consult)
            serve._supervisor_budget_step(state, decide=decide,
                                          consult=self._consult)
        self.assertEqual(len(calls), serve.SUPERVISOR_BUDGET_MAX_LLM_ATTEMPTS)
        self.assertEqual(task['_supervisorBudgetDecision']['decision'], 'deferred')


    def test_worker_report_reaches_the_director_before_judgment(self):
        task = _exhausted_task()
        state = _state(task)
        seen = []

        def decide(state, t):
            seen.append((t.get('_supervisorBudgetNegotiationDraft') or {}).get('worker'))
            return self.decision

        self.decision = {'decision': 'close', 'budgetUsd': None,
                         'reason': 'duplicated work'}
        self._step(state, decide=decide)
        self.assertEqual(seen[0]['completed'], 'outline and two scenes')
        self.assertEqual(seen[0]['moreBudgetUsd'], 0.30)
        self.assertTrue(self.consulted[0] is None)

    def test_director_may_ask_one_follow_up_before_deciding(self):
        task = _exhausted_task()
        state = _state(task)
        answers = [{'decision': 'grant', 'budgetUsd': 0.4,
                    'reason': 'progress is real'},
                   {'decision': 'grant', 'budgetUsd': 0.4,
                    'reason': 'concluded after the reply'}]
        questions = []

        def decide(state, t):
            draft = t.get('_supervisorBudgetNegotiationDraft') or {}
            if draft.get('round_no') == 1 and len(questions) == 0:
                questions.append(1)
                return {'action': 'ask', 'question': 'what did the spend buy?'}
            return answers[1]

        consults = []

        def consult(state, t, spent, attempts, question=None):
            consults.append(question)
            if question:
                return {'completed': 'the scenes', 'whyOver': 'length',
                        'moreBudgetUsd': 0.3, 'reason': 'reply to follow-up'}
            return dict(self.worker_report)

        with mock.patch.object(serve, 'log_action'):
            made = serve._supervisor_budget_step(state, decide=decide,
                                                 consult=consult)
        self.assertEqual(made, 1)
        self.assertEqual(consults, [None, 'what did the spend buy?'])
        sides = [r['side'] for r in task['_supervisorBudgetDecision']['negotiation']]
        self.assertEqual(sides, ['worker', 'director', 'worker'])
        decision = task['_supervisorBudgetDecision']
        self.assertEqual(decision['workerEstimateUsd'], 0.30)
        self.assertTrue(decision['applied'] is False)

    def test_worker_consult_failure_does_not_block_the_decision(self):
        task = _exhausted_task()
        state = _state(task)
        self.decision = {'decision': 'close', 'budgetUsd': None,
                         'reason': 'decide from the record'}

        def consult(state, t, spent, attempts, question=None):
            raise RuntimeError('model down')

        with mock.patch.object(serve, 'log_action'):
            made = serve._supervisor_budget_step(state, decide=self._decide,
                                                 consult=consult)
        self.assertEqual(made, 1)
        self.assertEqual(task['_supervisorBudgetDecision']['decision'], 'close')
        self.assertIsNone(task['_supervisorBudgetDecision']['workerEstimateUsd'])

    def test_worker_ask_feeds_the_default_grant_amount(self):
        task = _exhausted_task()
        state = _state(task)
        self.decision = {'decision': 'grant', 'budgetUsd': None,
                         'reason': 'fund the remainder the author sized'}
        grants = []

        def grant(agent_id, task_id, amount, reason):
            grants.append((agent_id, task_id, amount, reason))
            return True, {'ok': True}

        with mock.patch.object(serve, '_task_budget_spent',
                               return_value=(0.14, 3)):
            self._step(state)
            self._apply(state, grant=grant)
        # the author asked for 0.30 more on top of 0.14 spent: 0.44 beats the
        # old default (max(2x spent, 1.5x ceiling) = 0.28)
        self.assertEqual(grants[0][2], 0.44)
        self.assertIn('estimated $0.30 more', grants[0][3])

    def test_decided_card_stamps_the_negotiation(self):
        task = _exhausted_task()
        state = _state(task)
        self.decision = {'decision': 'close', 'budgetUsd': None,
                         'reason': 'not worth continuing'}
        self._step(state)
        decision = task['_supervisorBudgetDecision']
        self.assertEqual(decision['negotiation'][0]['side'], 'worker')
        self.assertIn('three scenes remain', decision['negotiation'][0]['text'])
        self.assertNotIn('_supervisorBudgetNegotiationDraft', task)


class WorkerNotification(unittest.TestCase):
    def test_grant_notifies_the_original_author(self):
        state = _state(_exhausted_task())
        task = state['tasks']['t1']
        sim._notify_budget_granted(state, task, 0.50, 'ruth',
                                   'the spend bought progress; funding the rest')
        note = state['agents']['ada']['mailbox'][0]
        self.assertEqual(note['kind'], 'budget_granted')
        self.assertEqual(note['budgetUsd'], 0.50)
        self.assertIn('$0.50', note['text'])
        self.assertIn('normal queue', note['text'])

    def test_author_is_not_notified_about_their_own_grant(self):
        state = _state(_exhausted_task())
        task = state['tasks']['t1']
        sim._notify_budget_granted(state, task, 0.50, 'ada', 'reason')
        self.assertEqual(state['agents']['ada']['mailbox'], [])

    def test_exhaustion_stamps_the_review_timestamps(self):
        task = _exhausted_task(_budgetReviewQueued=False,
                               _budgetExhaustedNotified=False)
        state = _state(task)
        state['agents']['ruth'] = {'id': 'ruth', 'mailbox': []}
        with mock.patch.object(serve, 'log_action'):
            sim._notify_task_budget_exhausted(state, task, spent=0.15,
                                              attempts=3, analysis=None,
                                              completed=None)
        self.assertTrue(task['_budgetReviewQueued'])
        self.assertIsInstance(task['budgetExhaustedAtMs'], int)
        self.assertIsInstance(task['supervisorBudgetReviewQueuedAtMs'], int)
        kinds = [n['kind'] for n in state['agents']['ruth']['mailbox']]
        self.assertIn('task_budget_exhausted', kinds)


if __name__ == '__main__':
    unittest.main()
