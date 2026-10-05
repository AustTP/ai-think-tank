"""Item 4 -- per-task model-spend ceiling: the bank's per-task ledger buckets,
the /api/chat gate that refuses calls past a task's ceiling, the spike lane's
parallel gate, the fail-closed content-result helper, and the director's
runtime override that grants more budget and re-opens a budget-exhausted card.

Hermetic: the kv_spend ledger is replaced with an in-memory dict (same seam as
test_bank.py) and get_state_from_db is patched per test, so no real DB or live
OpenRouter is ever touched.
"""

import json
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402


def setUpModule():
    serve.COLAB_STANDBY_ENABLED = False


def tearDownModule():
    serve.COLAB_STANDBY_ENABLED = str(
        serve._load_env().get('COLAB_STANDBY_ENABLED', '') or ''
    ).lower() in ('1', 'true', 'yes')


def _leak():
    """Replace kv_spend persistence with an in-memory dict; returns the holder."""
    holder = {}

    def read():
        return json.loads(json.dumps(holder['ledger'])) if 'ledger' in holder else {}

    def write(ledger):
        holder['ledger'] = json.loads(json.dumps(ledger))

    read_patch = unittest.mock.patch.object(serve, '_spend_ledger_read', side_effect=read)
    write_patch = unittest.mock.patch.object(serve, '_spend_ledger_write', side_effect=write)
    read_patch.start()
    write_patch.start()
    return read_patch, write_patch


def _state_with_task(task_id, budget_usd, spent=0.0, status='working', budget_exhausted=False):
    state = {
        'agents': {},
        'workQueue': [],
        'tasks': {
            task_id: {'id': task_id, 'title': 'Build the thing', 'room': 'pressoffice',
                      'status': status, 'taskType': 'code',
                      'assignedTo': 'ben', 'budgetUsd': budget_usd,
                      'budgetExhausted': budget_exhausted},
        },
    }
    if spent:
        state['_task_spend'] = {f'__task__/{task_id}': {'used': spent, 'calls': 3}}
    return state


class BankPerTaskLedger(unittest.TestCase):
    def setUp(self):
        self.read_patch, self.write_patch = _leak()
        self.addCleanup(self.read_patch.stop)
        self.addCleanup(self.write_patch.stop)

    def test_accrue_task_spend_buckets_under_reserved_key(self):
        serve._accrue_task_spend('task-1', 1.25)
        serve._accrue_task_spend('task-1', 0.75)
        ledger = serve._spend_ledger_read()
        self.assertAlmostEqual(ledger['__task__/task-1']['used'], 2.0)
        self.assertEqual(ledger['__task__/task-1']['calls'], 2)
        # A service bucket is never conflated with a task bucket.
        self.assertNotIn('task-1', ledger)

    def test_accrue_task_spend_noops_on_empty_or_non_numeric(self):
        serve._accrue_task_spend('', 5.0)
        serve._accrue_task_spend('task-1', 0)
        serve._accrue_task_spend('task-1', 'oops')  # noqa: B156 # defensive no-op
        self.assertEqual(serve._spend_ledger_read(), {})

    def test_task_budget_spent_reads_own_bucket(self):
        serve._accrue_task_spend('task-1', 1.25)
        serve._accrue_task_spend('task-1', 0.25)
        used, calls = serve._task_budget_spent('task-1')
        self.assertAlmostEqual(used, 1.5)
        self.assertEqual(calls, 2)
        self.assertEqual(serve._task_budget_spent('never-spent'), (0.0, 0))
        self.assertEqual(serve._task_budget_spent(''), (0.0, 0))

    def test_task_budget_exhausted_uses_durable_ceiling(self):
        # No budgetUsd (unknown task / unbudgeted) is NEVER gated.
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=_state_with_task('task-1', None)):
            self.assertFalse(serve._task_budget_exhausted('task-1'))
        # budgetUsd 0 is the explicit unbudgeted opt-out.
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=_state_with_task('task-1', 0)):
            self.assertFalse(serve._task_budget_exhausted('task-1'))
        # No task_id is never gated.
        self.assertFalse(serve._task_budget_exhausted(''))

    def test_task_budget_exhausted_true_at_or_above_ceiling(self):
        serve._accrue_task_spend('task-1', 0.20)
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=_state_with_task('task-1', 0.25)):
            self.assertFalse(serve._task_budget_exhausted('task-1'))  # 0.20 < 0.25
        serve._accrue_task_spend('task-1', 0.05)
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=_state_with_task('task-1', 0.25)):
            self.assertTrue(serve._task_budget_exhausted('task-1'))  # 0.25 >= 0.25

    def test_exhausted_fails_open_on_state_read_failure(self):
        serve._accrue_task_spend('task-1', 99.0)
        with unittest.mock.patch.object(serve, 'get_state_from_db', side_effect=RuntimeError('db down')):
            self.assertFalse(serve._task_budget_exhausted('task-1'))  # never refuse on an accounting hiccup

    def test_per_task_buckets_excluded_from_spend_cap_total(self):
        # A task bucket is an ATTRIBUTION aid, not a service: it must not trip
        # the monthly spend cap on its own.
        serve._accrue_task_spend('task-1', 50.0)  # huge task spend
        serve._accrue_spend('jevalpha', 1.0)      # small real service spend
        with unittest.mock.patch.object(serve, 'SPEND_CAP_USD', 2.0):
            self.assertFalse(serve._think_tank_spend_cap_exceeded())

    def test_per_task_buckets_excluded_from_bank_view(self):
        serve._accrue_task_spend('task-1', 3.0)
        serve._accrue_spend('jevalpha', 2.0)
        snapshot = {'agentRoster': [], 'products': {}, 'tasks': {}}
        services = serve._bank_budget_view(snapshot)  # dict keyed by service
        self.assertIn('jevalpha', services)
        self.assertNotIn('__task__/task-1', services)


class ChatGate(unittest.TestCase):
    def setUp(self):
        self.read_patch, self.write_patch = _leak()
        self.addCleanup(self.read_patch.stop)
        self.addCleanup(self.write_patch.stop)

    def _post(self, state, body):
        from fastapi.testclient import TestClient
        c = TestClient(serve.app)
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'sk-test'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'log_action'):
            return c.post('/api/chat', json=body)

    def test_gate_refuses_exhausted_task_before_any_model_call(self):
        serve._accrue_task_spend('task-1', 0.30)
        state = _state_with_task('task-1', 0.25)
        with unittest.mock.patch.object(serve, '_call_openrouter_sync') as model:
            r = self._post(state, {'model': 'm', 'agentId': 'ben', 'taskId': 'task-1',
                                   'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(r.status_code, 429)
        body = r.json()
        self.assertTrue(body['budgetExhausted'])
        self.assertAlmostEqual(body['taskSpendUsd'], 0.3)
        self.assertEqual(body['taskSpendAttempts'], 1)
        model.assert_not_called()  # no money spent

    def test_gate_passes_unexhausted_task(self):
        serve._accrue_task_spend('task-1', 0.10)
        state = _state_with_task('task-1', 0.25)
        with unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        return_value={'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}],
                                                      'usage': {'cost': 0.05}}):
            r = self._post(state, {'model': 'm', 'agentId': 'ben', 'taskId': 'task-1',
                                   'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(r.status_code, 200, r.text)
        # The call accrued to BOTH the service and the task's own bucket.
        used, calls = serve._task_budget_spent('task-1')
        self.assertAlmostEqual(used, 0.15)
        self.assertEqual(calls, 2)

    def test_gate_never_refuses_a_call_without_task_id(self):
        # The ask lane / player / serve's own loopback carry no taskId: gating
        # them would break every non-task lane.
        state = _state_with_task('task-1', 0.25)
        with unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        return_value={'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}],
                                                      'usage': {'cost': 0.01}}):
            r = self._post(state, {'model': 'm', 'agentId': 'ben',
                                   'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_gate_never_refuses_unknown_task(self):
        with unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        return_value={'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}],
                                                      'usage': {'cost': 0.01}}):
            r = self._post(_state_with_task('task-1', 0.25),
                           {'model': 'm', 'agentId': 'ben', 'taskId': 'ghost-task',
                            'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(r.status_code, 200, r.text)


class AgentToolLoopGate(unittest.TestCase):
    def setUp(self):
        self.read_patch, self.write_patch = _leak()
        self.addCleanup(self.read_patch.stop)
        self.addCleanup(self.write_patch.stop)

    def test_loop_returns_early_when_task_budget_exhausted(self):
        serve._accrue_task_spend('task-1', 0.30)
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=_state_with_task('task-1', 0.25)), \
             unittest.mock.patch.object(serve, '_post_openrouter_raw') as model:
            result = serve._call_agent_tool_loop(
                'm', [{'role': 'user', 'content': 'q'}], [], lambda n, a: 'x',
                max_iterations=3, task_id='task-1')
        self.assertIsNone(result)
        model.assert_not_called()

    def test_loop_accrues_per_task_and_gates_the_next_round(self):
        # Two rounds each cost 0.20 against a 0.25 ceiling: round 1 passes
        # (0.20 < 0.25), round 2 crosses it (0.40), and round 3 is refused at
        # the top-of-loop gate -- no further money spent.
        calls = {'n': 0}

        def fake_post(model, messages, tools=None, max_tokens=None, tool_choice=None):
            calls['n'] += 1
            return {'choices': [{'message': {'role': 'assistant', 'content': None,
                                             'tool_calls': [{'id': 'c1', 'type': 'function',
                                                             'function': {'name': 'browse_page', 'arguments': '{"url":"x"}'}}]}}],
                    'usage': {'cost': 0.20}}

        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=_state_with_task('task-1', 0.25)), \
             unittest.mock.patch.object(serve, '_post_openrouter_raw', side_effect=fake_post):
            result = serve._call_agent_tool_loop(
                'm', [{'role': 'user', 'content': 'q'}], [], lambda n, a: 'page',
                max_iterations=3, task_id='task-1', return_transcript=True)
        # Exactly the two paying rounds ran; the third was refused at the gate
        # and the loop returned the transcript it gathered (no settling text).
        self.assertEqual(calls['n'], 2)
        self.assertIsNone(result[0])
        used, _calls = serve._task_budget_spent('task-1')
        self.assertAlmostEqual(used, 0.40)


class ContentErrorResult(unittest.TestCase):
    def test_budget_exhausted_refusal_folds_into_fail_closed_result(self):
        # A 429 budgetExhausted body surfaced through _http_json is a dict;
        # _chat_error_result turns it into a storable fail-closed result.
        import content
        r = {'budgetExhausted': True, 'taskSpendUsd': 0.3, 'taskSpendAttempts': 2,
             'error': '...'}
        out = content._chat_error_result(r, 'the call was paused on budget')
        self.assertIsNotNone(out)
        self.assertFalse(out['ok'])
        self.assertTrue(out['budgetExhausted'])
        self.assertAlmostEqual(out['taskSpendUsd'], 0.3)
        self.assertEqual(out['taskSpendAttempts'], 2)

    def test_non_budget_failure_is_untouched(self):
        import content
        self.assertIsNone(content._chat_error_result({'error': 'rate limited'}, 'fallback'))
        self.assertIsNone(content._chat_error_result({'reply': 'ok'}, 'fallback'))
        self.assertIsNone(content._chat_error_result(None, 'fallback'))


class DirectorReopen(unittest.TestCase):
    def setUp(self):
        self.read_patch, self.write_patch = _leak()
        self.addCleanup(self.read_patch.stop)
        self.addCleanup(self.write_patch.stop)

    def _grant(self, state, task_id, new_budget, director='nora'):
        import sim
        return sim._director_grant_budget_reopen(state, task_id, new_budget, director_id=director)

    def test_refuses_non_budget_failed_card(self):
        state = _state_with_task('task-1', 0.25, status='working')
        self.assertFalse(self._grant(state, 'task-1', 5.0)['ok'])

    def test_refuses_budget_that_does_not_exceed_spent(self):
        state = _state_with_task('task-1', 0.25, status='failed', budget_exhausted=True)
        serve._accrue_task_spend('task-1', 0.30)
        self.assertFalse(self._grant(state, 'task-1', 0.20)['ok'])
        self.assertFalse(self._grant(state, 'task-1', 0.30)['ok'])  # exactly spent still refuses

    def test_grant_reopens_and_requeues_with_raised_budget(self):
        state = _state_with_task('task-1', 0.25, status='failed', budget_exhausted=True)
        serve._accrue_task_spend('task-1', 0.30)
        result = self._grant(state, 'task-1', 5.0)
        self.assertTrue(result['ok'])
        self.assertAlmostEqual(result['budgetUsd'], 5.0)
        # The failed record keeps the raised ceiling as the audit trail.
        self.assertAlmostEqual(state['tasks']['task-1']['budgetUsd'], 5.0)
        self.assertEqual(state['tasks']['task-1']['budgetGrantedBy'], 'nora')
        # A re-open card is queued carrying the new budget, pinned to the author.
        self.assertEqual(len(state['workQueue']), 1)
        queued = state['workQueue'][0]
        self.assertAlmostEqual(queued['budgetUsd'], 5.0)
        self.assertEqual(queued['assignedTo'], 'ben')


if __name__ == '__main__':
    unittest.main()
