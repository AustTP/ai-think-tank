"""The Bank: a spend ledger for the model/API budget, surfaced to directors.

Every model call flows through /api/chat (or /api/decide for Jev, or the
/api/intent/ask tool loop), and OpenRouter reports each call's USD cost in
usage.cost. Those costs accrue to a per-service ledger (used / cap / left / a
trailing-7-day burn forecast) so a director at a bank teller can see the whole
village's spend and coordinate with the other directors not to exceed -- the
teller is deliberately DIRECTORS-ONLY (workers are deflected to their director).

Hermetic: the ledger lives in its own kv_spend row, which these tests replace
with an in-memory dict via patching _spend_ledger_read/_spend_ledger_write, so
no real DB (and no live OpenRouter) is ever touched.
"""

import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402


def _roster():
    # faye = admin, nora = director (two directs), ben/cora = workers.
    return [
        {'id': 'faye', 'name': 'Faye', 'role': 'Control Room', 'isAdmin': True},
        {'id': 'nora', 'name': 'Nora', 'role': 'Personnel', 'isDirector': True},
        {'id': 'eli', 'name': 'Eli', 'role': 'Ops', 'director': 'nora'},
        {'id': 'dev', 'name': 'Dev', 'role': 'Studio', 'director': 'nora'},
        {'id': 'ben', 'name': 'Ben', 'role': 'Banking'},
    ]


def _snapshot(**over):
    roster = _roster()
    snap = {
        'agentRoster': roster,
        'agents': {a['id']: {'id': a['id'], 'name': a['name'], 'role': a['role'],
                             'offDuty': False, 'busy': False, 'task': None,
                             'pairWith': None}
                   for a in roster},
        'products': {},
        'workQueue': [],
        'tasks': {},
    }
    snap.update(over)
    return snap


def _ledger(spec):
    """Build a raw ledger dict from {service: used} plus a deterministic byDay
    series so _forecast has a signal to burn on."""
    return {svc: {'used': used, 'calls': 5, 'lastAt': '2026-09-24T00:00:00+00:00',
                  'byDay': {date: used / 7.0 for date in _last_n_days(7)}}
            for svc, used in spec.items()}


def _last_n_days(n):
    import datetime
    today = datetime.date.today()
    return [(today - datetime.timedelta(days=i)).isoformat() for i in range(n)]


def json_copy(obj):
    import json
    return json.loads(json.dumps(obj))


class Ledger(unittest.TestCase):
    def _leak(self):
        # Replace the kv_spend persistence with a dict. `read` returns a *copy*
        # (like a real DB row would), and `write` replaces the held value -- so
        # the read-modify-write in _accrue_spend goes back through the seam
        # exactly as it would against SQLite, and our held dict stays in sync.
        holder = {}

        def read():
            return json_copy(holder.get('ledger')) if 'ledger' in holder else {}

        def write(ledger):
            holder['ledger'] = json_copy(ledger)

        read_patch = unittest.mock.patch.object(serve, '_spend_ledger_read', side_effect=read)
        write_patch = unittest.mock.patch.object(serve, '_spend_ledger_write', side_effect=write)
        read_patch.start()
        write_patch.start()
        self.addCleanup(read_patch.stop)
        self.addCleanup(write_patch.stop)

    def test_accrue_sums_per_service_and_ignores_non_numeric(self):
        self._leak()
        serve._accrue_spend('alpha', 1.25)
        serve._accrue_spend('alpha', 0.75)
        serve._accrue_spend('beta', 5.0)
        serve._accrue_spend('alpha', None)   # no usage cost -> no-op
        serve._accrue_spend('alpha', 'oops')  # noqa: B156 # defensive no-op
        ledger = serve._spend_ledger_read()
        self.assertAlmostEqual(ledger['alpha']['used'], 2.0)
        self.assertEqual(ledger['alpha']['calls'], 2)
        self.assertAlmostEqual(ledger['beta']['used'], 5.0)

    def test_accrue_noops_on_zero_or_absent(self):
        self._leak()
        serve._accrue_spend('gamma', 0)
        serve._accrue_spend('gamma', 0.0)
        self.assertEqual(serve._spend_ledger_read(), {})

    def test_byday_series_drives_forecast(self):
        self._leak()
        serve._accrue_spend('alpha', 3.5)  # single today call
        ledger = serve._spend_ledger_read()
        self.assertIn('byDay', ledger['alpha'])
        self.assertGreater(sum(ledger['alpha']['byDay'].values()), 0)


class BudgetView(unittest.TestCase):
    def test_view_computes_used_cap_left_and_forecast(self):
        # 7 days at $1/day burn, $14 used against a $50 cap -> $36 left, ~36 days.
        ledger = {'alpha': {'used': 14.0, 'calls': 14, 'lastAt': '2026-09-24T00:00:00+00:00',
                            'byDay': {d: 2.0 for d in _last_n_days(7)}}}
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value=ledger):
            view = serve._bank_budget_view(_snapshot())
        row = view['alpha']
        self.assertAlmostEqual(row['used'], 14.0)
        self.assertAlmostEqual(row['cap'], serve.DEFAULT_BUDGET_CAP_USD)
        self.assertAlmostEqual(row['left'], 36.0)
        self.assertFalse(row['over'])
        self.assertAlmostEqual(row['burnPerDay'], 2.0)
        self.assertIsNotNone(row['daysLeft'])
        self.assertAlmostEqual(row['daysLeft'], 18.0, places=3)  # 36 left / 2 per day

    def test_product_cap_overrides_default(self):
        # products is a DICT keyed by id in real state (see sim.next_product_id);
        # the view must read caps off that shape, not an array.
        snap = _snapshot(products={'p1': {'id': 'p1', 'name': 'Project Echo', 'teamId': 'nora',
                                          'budgetCapUsd': 12.0}})
        ledger = {'p1': {'used': 15.0, 'calls': 3, 'lastAt': '2026-09-24T00:00:00+00:00',
                         'byDay': {d: 1.0 for d in _last_n_days(7)}}}
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value=ledger):
            view = serve._bank_budget_view(snap)
        row = view['p1']
        self.assertAlmostEqual(row['cap'], 12.0)
        self.assertTrue(row['over'])
        self.assertAlmostEqual(row['left'], 0.0)
        self.assertIsNone(row['daysLeft'])  # over cap -> re-budget, not a forecast

    def test_cumulative_across_services(self):
        ledger = {'a': {'used': 10.0, 'calls': 1, 'lastAt': 'x', 'byDay': {d: 1.0 for d in _last_n_days(7)}},
                  'b': {'used': 20.0, 'calls': 1, 'lastAt': 'x', 'byDay': {d: 1.0 for d in _last_n_days(7)}}}
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value=ledger):
            view = serve._bank_budget_view(_snapshot())
        self.assertEqual(set(view), {'a', 'b'})

    def test_no_data_yields_empty_view(self):
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={}):
            self.assertEqual(serve._bank_budget_view(_snapshot()), {})


class BankContent(unittest.TestCase):
    def _store(self):
        # _run_bank_content stores its note via the sim module's
        # _store_content_result (it does `import sim as _sim_module`). Patch
        # that attribute directly on the sim module so the real executor's
        # dispatch actually runs rather than being short-circuited.
        seen = {}
        patcher = unittest.mock.patch('sim._store_content_result',
                                      lambda task_id, result: seen.__setitem__(task_id, result))
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_worker_is_deflected_to_director(self):
        snap = _snapshot()
        seen = self._store()
        task = {'id': 't1', 'room': 'bank', 'title': 'check the budget'}
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={'a': {'used': 1.0, 'calls': 1, 'lastAt': 'x', 'byDay': {d: 1.0 for d in _last_n_days(7)}}}):
            serve._run_bank_content(snap, 'ben', task)
        note = seen['t1']['note']
        self.assertIn('budget authority belongs to the directors', note)
        self.assertNotIn('$1.00', note)  # worker never sees the ledger

    def test_director_gets_cumulative_view(self):
        snap = _snapshot()
        seen = self._store()
        task = {'id': 't2', 'room': 'bank', 'title': 'check the budget'}
        ledger = _ledger({'alpha': 14.0, 'beta': 6.0})
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value=ledger):
            serve._run_bank_content(snap, 'nora', task)
        note = seen['t2']['note']
        self.assertIn('reviewed the bank', note)
        self.assertIn('alpha', note)
        self.assertIn('beta', note)
        self.assertIn('within cap', note)

    def test_admin_is_treated_as_director(self):
        snap = _snapshot()
        seen = self._store()
        task = {'id': 't3', 'room': 'bank'}
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={}):
            serve._run_bank_content(snap, 'faye', task)
        note = seen['t3']['note']
        self.assertIn('nothing has been spent yet', note)
        self.assertIn('director', note)


class Dispatcher(unittest.TestCase):
    def _route(self, room, agent_id, **task_over):
        # Call the ROUTER (_server_content_dispatcher) directly, synchronously,
        # with the sim module's _store_content_result stubbed to capture. This
        # tests the branch I changed -- bank now routes to the bank executor
        # while other placeholder rooms still don't -- without the
        # _dispatch_content_work background thread's nondeterminism.
        import sim as _sim
        seen = {}
        patcher = unittest.mock.patch.object(_sim, '_store_content_result',
                                             lambda tid, res: seen.__setitem__(tid, res))
        patcher.start()
        self.addCleanup(patcher.stop)
        task = {'id': room + agent_id, 'room': room, 'title': 'x', 'taskType': 'code'}
        task.update(task_over)
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={}):
            serve._server_content_dispatcher(_snapshot(), agent_id, task)
        return seen

    def test_bank_room_routes_to_bank_executor(self):
        seen = self._route('bank', 'nora')
        self.assertTrue(seen, 'bank task should have stored a result')
        note = next(iter(seen.values()))['note']
        self.assertIn('nothing has been spent yet', note)

    def test_other_placeholder_room_not_dispatched_to_bank(self):
        seen = self._route('library', 'nora')
        self.assertEqual(seen, {})  # no dispatcher branch for library -> placeholder


if __name__ == '__main__':
    unittest.main(verbosity=2)