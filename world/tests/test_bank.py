"""The Bank: a spend ledger for the model/API budget, surfaced to directors.

Every model call flows through /api/chat (or /api/decide for Jev, or the
/api/intent/ask tool loop), and OpenRouter reports each call's USD cost in
usage.cost. Those costs accrue to a per-service ledger (used / cap / left / a
trailing-7-day burn forecast) so a director at a bank teller can see the whole
think tank's spend and coordinate with the other directors not to exceed -- the
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


def setUpModule():
    # The CLI-run Laya standby appends a provider to every decision chain when
    # enabled (it IS enabled in the project .env, which serve loads at import).
    # Hermetic modules must keep decision chains single-slug by pinning the
    # standby off: with two slugs the multi-slug circuit breaker arms, and its
    # state leaks across modules in the shared test process: an
    # 'all configured decision models are circuit-broken' RuntimeError in this
    # file from breakers opened by earlier modules' collection-injections).
    serve.COLAB_STANDBY_ENABLED = False


def tearDownModule():
    serve.COLAB_STANDBY_ENABLED = str(
        serve._load_env().get('COLAB_STANDBY_ENABLED', '') or ''
    ).lower() in ('1', 'true', 'yes')


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

    def test_village_dimension_rolls_up_per_village(self):
        self._leak()
        serve._accrue_spend('alpha', 1.25, village_id='main')
        serve._accrue_spend('alpha', 0.75, village_id='main')
        serve._accrue_spend('beta', 5.0, village_id='colony-b')
        view = serve._village_spend_view()
        self.assertAlmostEqual(view['main']['used'], 2.0)
        self.assertEqual(view['main']['calls'], 2)
        self.assertAlmostEqual(view['colony-b']['used'], 5.0)

    def test_village_dimension_does_not_double_count_service_used(self):
        self._leak()
        serve._accrue_spend('alpha', 1.0, village_id='main')
        ledger = serve._spend_ledger_read()
        # The service bucket (what the shared cap sums) is untouched by the
        # rollup; the village rollup is a separate, parallel key.
        self.assertAlmostEqual(ledger['alpha']['used'], 1.0)
        self.assertAlmostEqual(ledger['__village__/main']['used'], 1.0)
        self.assertAlmostEqual(serve._village_spend_view()['main']['used'], 1.0)

    def test_village_dimension_excluded_from_shared_cap_total(self):
        self._leak()
        serve._accrue_spend('__village__/main', 999.0)
        with unittest.mock.patch.object(serve, 'SPEND_CAP_USD', 100.0):
            self.assertFalse(serve._think_tank_spend_cap_exceeded())
        self.assertAlmostEqual(serve._village_spend_view()['main']['used'], 999.0)

    def test_byday_series_drives_forecast(self):
        self._leak()
        serve._accrue_spend('alpha', 3.5)  # single today call
        ledger = serve._spend_ledger_read()
        self.assertIn('byDay', ledger['alpha'])
        self.assertGreater(sum(ledger['alpha']['byDay'].values()), 0)

    def test_forecast_divides_by_days_present_not_a_hard_seven(self):
        # A fresh ledger with spend on only 2 of the last 7 days is burning at
        # $5/day, not $10/7 -- dividing by 7 would hide a fast new burn.
        bucket = {'used': 10.0, 'calls': 2,
                  'byDay': {d: 5.0 for d in _last_n_days(2)}}
        burn, days_left = serve._forecast(bucket, cap=40.0)
        self.assertAlmostEqual(burn, 5.0)
        self.assertAlmostEqual(days_left, 6.0)  # 30 left / 5 per day

    def test_forecast_empty_window_yields_no_signal(self):
        # All spend older than 7 days (or an empty series) -> no forecast.
        import datetime
        today = datetime.date.today()
        old = {(today - datetime.timedelta(days=i)).isoformat(): 10.0
               for i in (8, 9, 10)}
        bucket = {'used': 10.0, 'calls': 1, 'byDay': old}
        self.assertEqual(serve._forecast(bucket, cap=40.0), (0.0, None))
        self.assertEqual(serve._forecast({'used': 10.0, 'byDay': {}}, cap=40.0), (0.0, None))


class SpendCap(unittest.TestCase):
    """Hard absolute spend cap: independent, general protection
    against ANY future bug draining the balance -- not a fix for one specific
    bug (that's _peer_gated_lane's research exemption). Checked at the top of
    every real money-spending chokepoint, before any network call."""

    def _leak(self):
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

    def test_disabled_when_cap_is_zero(self):
        self._leak()
        serve._accrue_spend('alpha', 999.0)
        with unittest.mock.patch.object(serve, 'SPEND_CAP_USD', 0.0):
            self.assertFalse(serve._think_tank_spend_cap_exceeded())

    def test_preexisting_historical_spend_never_counts_against_a_new_cap(self):
        # Real requirement: the cap protects the NEW balance going forward --
        # spend from BEFORE the cap was installed (e.g. tonight's incident)
        # must not immediately trip it the moment it's turned on.
        self._leak()
        serve._accrue_spend('AI regulation news', 8.5)  # pre-existing damage
        with unittest.mock.patch.object(serve, 'SPEND_CAP_USD', 5.0):
            self.assertFalse(serve._think_tank_spend_cap_exceeded())  # baseline set, not tripped
            self.assertFalse(serve._think_tank_spend_cap_exceeded())  # still not tripped on re-check

    def test_trips_once_new_spend_since_baseline_reaches_the_cap(self):
        self._leak()
        serve._accrue_spend('alpha', 1.0)
        with unittest.mock.patch.object(serve, 'SPEND_CAP_USD', 5.0):
            self.assertFalse(serve._think_tank_spend_cap_exceeded())  # sets baseline at 1.0
            serve._accrue_spend('alpha', 4.99)
            self.assertFalse(serve._think_tank_spend_cap_exceeded())  # 4.99 new < 5.0
            serve._accrue_spend('alpha', 0.02)
            self.assertTrue(serve._think_tank_spend_cap_exceeded())  # 5.01 new >= 5.0

    def test_cap_resets_at_each_month_boundary(self):
        # The cap is a MONTHLY budget: at the first check of a new UTC month
        # the baseline rolls forward to the current ledger total, so spend
        # from a previous month never counts against the new month's cap.
        self._leak()
        serve._accrue_spend('alpha', 4.0)  # prior-month spend
        with unittest.mock.patch.object(serve, 'SPEND_CAP_USD', 5.0):
            with unittest.mock.patch.object(serve, '_spend_cap_period', return_value='2026-09'):
                self.assertFalse(serve._think_tank_spend_cap_exceeded())  # Sep baseline set at 4.0
                serve._accrue_spend('alpha', 5.0)  # Sep now 9.0 total, 5.0 new
                self.assertTrue(serve._think_tank_spend_cap_exceeded())  # 5.0 new >= 5.0
            with unittest.mock.patch.object(serve, '_spend_cap_period', return_value='2026-10'):
                # Oct: baseline rolls to the current total (9.0); fresh $5 budget.
                self.assertFalse(serve._think_tank_spend_cap_exceeded())
                serve._accrue_spend('alpha', 1.0)
                self.assertFalse(serve._think_tank_spend_cap_exceeded())  # 1.0 new < 5.0
                serve._accrue_spend('alpha', 4.1)
                self.assertTrue(serve._think_tank_spend_cap_exceeded())  # 5.1 new >= 5.0

    def test_legacy_float_baseline_is_adopted_for_current_month(self):
        # Pre-monthly ledgers stored the baseline as a bare float (spend at
        # install time). It must be adopted as the CURRENT month's baseline --
        # not re-rolled to today's total -- so spend accrued since install
        # keeps counting against the cap exactly as it did before the upgrade.
        self._leak()
        serve._accrue_spend('alpha', 7.0)  # 3.0 since the old 4.0 baseline
        holder = {'ledger': serve._spend_ledger_read()}
        holder['ledger'][serve._SPEND_CAP_BASELINE_KEY] = 4.0  # legacy bare-float baseline
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value=holder['ledger']), \
             unittest.mock.patch.object(serve, '_spend_ledger_write',
                                        side_effect=lambda l: holder.__setitem__('ledger', l)):
            with unittest.mock.patch.object(serve, 'SPEND_CAP_USD', 5.0), \
                 unittest.mock.patch.object(serve, '_spend_cap_period', return_value='2026-09'):
                self.assertFalse(serve._think_tank_spend_cap_exceeded())  # adopts 4.0; 3.0 new < 5.0
                serve._accrue_spend('alpha', 2.0)
                self.assertTrue(serve._think_tank_spend_cap_exceeded())  # 5.0 new >= 5.0
            with unittest.mock.patch.object(serve, 'SPEND_CAP_USD', 5.0), \
                 unittest.mock.patch.object(serve, '_spend_cap_period', return_value='2026-10'):
                self.assertFalse(serve._think_tank_spend_cap_exceeded())  # new month: baseline rolls to 9.0

    def test_chat_completion_chokepoint_blocks_before_any_network_call(self):
        self._leak()
        serve._accrue_spend('alpha', 10.0)
        with unittest.mock.patch.object(serve, 'SPEND_CAP_USD', 5.0), \
             unittest.mock.patch.object(serve, '_urlopen_with_resilience') as net:
            serve._think_tank_spend_cap_exceeded()  # sets baseline
            serve._accrue_spend('alpha', 5.0)  # now over cap
            with self.assertRaises(RuntimeError):
                serve._call_openrouter_sync('some-model', [], 100)
        net.assert_not_called()

    def test_tool_loop_chokepoint_blocks_before_any_network_call(self):
        self._leak()
        with unittest.mock.patch.object(serve, 'SPEND_CAP_USD', 5.0), \
             unittest.mock.patch.object(serve, '_urlopen_with_resilience') as net:
            serve._think_tank_spend_cap_exceeded()  # sets baseline at 0
            serve._accrue_spend('alpha', 5.0)
            with self.assertRaises(RuntimeError):
                serve._post_openrouter_raw('some-model', [])
        net.assert_not_called()

    def test_jev_chokepoint_blocks_before_any_network_call(self):
        self._leak()
        with unittest.mock.patch.object(serve, 'SPEND_CAP_USD', 5.0), \
             unittest.mock.patch.object(serve, '_urlopen_with_resilience') as net:
            serve._think_tank_spend_cap_exceeded()  # sets baseline at 0
            serve._accrue_spend('alpha', 5.0)
            with self.assertRaises(RuntimeError):
                serve._call_openrouter_decision_sync('typesafe/jev-1.13', {}, {})
        net.assert_not_called()

    def test_jev_cost_is_now_accrued_into_the_ledger(self):
        # Gap: Jev's cost used to be logged per-call but
        # never summed anywhere, so a cap reading the ledger alone would
        # undercount real spend by every Jev decision ever made.
        self._leak()
        with unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                        return_value='{"answers": {"q1": {"choice": "allow", "confidence": 0.9}}, "usage": {"cost": 0.002}}'), \
             unittest.mock.patch.object(serve, '_append_decision_tape'):
            serve._call_openrouter_decision_sync('typesafe/jev-1.13', {}, {})
        ledger = serve._spend_ledger_read()
        self.assertAlmostEqual(ledger['__jev__']['used'], 0.002)

    def test_bank_view_does_not_crash_with_the_baseline_key_present(self):
        # _bank_budget_view iterates the whole ledger -- the reserved
        # baseline key (a float) must not be treated as a service bucket.
        self._leak()
        with unittest.mock.patch.object(serve, 'SPEND_CAP_USD', 5.0):
            serve._think_tank_spend_cap_exceeded()  # writes the baseline key
        serve._accrue_spend('alpha', 1.0)
        view = serve._bank_budget_view(_snapshot())  # must not raise
        self.assertIn('alpha', view)
        self.assertNotIn(serve._SPEND_CAP_BASELINE_KEY, view)


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
        # Apify budget row disabled so this is purely about the two services.
        with unittest.mock.patch.object(serve, 'APIFY_MONTHLY_BUDGET_USD', 0), \
             unittest.mock.patch.object(serve, '_spend_ledger_read', return_value=ledger):
            view = serve._bank_budget_view(_snapshot())
        self.assertEqual(set(view), {'a', 'b'})

    def test_zero_cap_is_no_cap_never_over(self):
        # __colab_compute__ with COLAB_MONTHLY_UNITS unset (0) has no cap:
        # the row is never 'over' (the health alert's trigger) and shows no
        # left/forecast -- mirroring _colab_budget_exceeded, which fails open.
        ledger = {serve.COLAB_LEDGER_KEY: {'used': 25.0, 'calls': 10, 'lastAt': 'x',
                                           'byDay': {d: 5.0 for d in _last_n_days(7)}}}
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 0.0), \
             unittest.mock.patch.object(serve, 'APIFY_MONTHLY_BUDGET_USD', 0), \
             unittest.mock.patch.object(serve, '_spend_ledger_read', return_value=ledger):
            view = serve._bank_budget_view(_snapshot())
        row = view[serve.COLAB_LEDGER_KEY]
        self.assertFalse(row['over'])
        self.assertIsNone(row['left'])
        self.assertIsNone(row['daysLeft'])
        self.assertAlmostEqual(row['cap'], 0.0)

    def test_no_data_yields_empty_view(self):
        with unittest.mock.patch.object(serve, 'APIFY_MONTHLY_BUDGET_USD', 0), \
             unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={}):
            self.assertEqual(serve._bank_budget_view(_snapshot()), {})


class ApifyBudget(unittest.TestCase):
    """The Apify FREE-plan ~$5/mo budget: a real monthly cap the
    plan enforces, surfaced in the Bank as its own __apify__ row from day one
    (before any accrual) and reconciled live against the real account."""

    def test_bank_view_seeds_apify_row_even_with_zero_spend(self):
        # The cap is real (enforced by the plan), so it belongs in the Bank
        # before the first actor run accrues anything -- same "visible from
        # day one" rule as product caps.
        with unittest.mock.patch.object(serve, 'APIFY_MONTHLY_BUDGET_USD', 5.0), \
             unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={}):
            view = serve._bank_budget_view(_snapshot())
        self.assertIn('__apify__', view)
        row = view['__apify__']
        self.assertAlmostEqual(row['cap'], 5.0)
        self.assertAlmostEqual(row['used'], 0.0)
        self.assertAlmostEqual(row['left'], 5.0)
        self.assertFalse(row['over'])

    def test_bank_view_omits_apify_row_when_budget_disabled(self):
        # 0/unset budget = no row at all.
        with unittest.mock.patch.object(serve, 'APIFY_MONTHLY_BUDGET_USD', 0), \
             unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={}):
            view = serve._bank_budget_view(_snapshot())
        self.assertNotIn('__apify__', view)

    def test_accrue_apify_spend_records_monthly_and_total(self):
        # Accrual lands in the same kv_spend ledger under the reserved bucket,
        # keyed by month so the cap reads cleanly and the Bank sees it.
        store = {}
        def fake_read():
            return dict(store)  # fresh copy each read, like a real DB round-trip
        def fake_write(lg):
            store.clear()
            store.update(lg)
        with unittest.mock.patch.object(serve, '_spend_ledger_read', side_effect=fake_read), \
             unittest.mock.patch.object(serve, '_spend_ledger_write', side_effect=fake_write):
            serve._accrue_apify_spend(1.25)
            serve._accrue_apify_spend(2.75)
            self.assertAlmostEqual(serve._apify_spend_this_month(), 4.0)
        bucket = store['__apify__']
        self.assertAlmostEqual(bucket['used'], 4.0)
        self.assertEqual(bucket['calls'], 2)
        month = serve._apify_budget_month()
        self.assertAlmostEqual(bucket['byMonth'][month], 4.0)

    def test_apify_budget_exceeded_only_when_over_cap(self):
        with unittest.mock.patch.object(serve, 'APIFY_MONTHLY_BUDGET_USD', 5.0), \
             unittest.mock.patch.object(serve, '_apify_spend_this_month', return_value=4.0):
            self.assertFalse(serve._apify_budget_exceeded())
        with unittest.mock.patch.object(serve, 'APIFY_MONTHLY_BUDGET_USD', 5.0), \
             unittest.mock.patch.object(serve, '_apify_spend_this_month', return_value=5.0):
            self.assertTrue(serve._apify_budget_exceeded())
        # Disabled budget is never "exceeded".
        with unittest.mock.patch.object(serve, 'APIFY_MONTHLY_BUDGET_USD', 0), \
             unittest.mock.patch.object(serve, '_apify_spend_this_month', return_value=99.0):
            self.assertFalse(serve._apify_budget_exceeded())

    def test_account_usage_reconciles_live_and_caches(self):
        # GET /users/me (plan cap) then /users/me/usage/monthly (cycle spend).
        me = {'data': {'plan': {'maxMonthlyUsageUsd': 5.0}}}
        usage = {'data': {'usageCycle': {'startAt': '2026-09-01', 'endAt': '2026-10-01'},
                          'monthlyServiceUsage': {'facebook': {'amountAfterVolumeDiscountUsd': 1.25},
                                                  'instagram': {'amountAfterVolumeDiscountUsd': 2.50}}}}
        import json as _json
        responses = iter([me, usage])
        def fake_urlopen(req, timeout=10):
            resp = unittest.mock.MagicMock()
            resp.read.return_value = _json.dumps(next(responses)).encode('utf-8')
            resp.__enter__.return_value = resp  # context manager returns itself
            return resp
        with unittest.mock.patch.object(serve, 'APIFY_API_KEY', 'key'), \
             unittest.mock.patch.object(serve, '_APIFY_USAGE_CACHE', {'at': 0.0, 'data': None}), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=fake_urlopen) as urlopen:
            result = serve._apify_account_usage()
            # Second call within the TTL must NOT hit the network again.
            result2 = serve._apify_account_usage()
        self.assertAlmostEqual(result['capUsd'], 5.0)
        self.assertAlmostEqual(result['usedUsd'], 3.75)
        self.assertAlmostEqual(result['remainingUsd'], 1.25)
        self.assertEqual(result['cycleStart'], '2026-09-01')
        self.assertEqual(result2, result)
        self.assertEqual(urlopen.call_count, 2)  # 2 endpoints, once each

    def test_account_usage_fails_closed_without_key_or_network(self):
        # No key -> None without touching the network.
        with unittest.mock.patch.object(serve, 'APIFY_API_KEY', ''), \
             unittest.mock.patch.object(serve, 'urllib') as fake_urllib:
            self.assertIsNone(serve._apify_account_usage())
        fake_urllib.request.urlopen.assert_not_called()
        # Network failure -> None (the teller omits the line, never fabricates).
        with unittest.mock.patch.object(serve, 'APIFY_API_KEY', 'key'), \
             unittest.mock.patch.object(serve, 'urllib') as fake_urllib:
            fake_urllib.request.urlopen.side_effect = Exception('boom')
            self.assertIsNone(serve._apify_account_usage())

    def test_bank_content_director_appends_apify_reconcile(self):
        # A director's readout includes the live Apify account line (fail-open
        # when the reconcile returns None: the rest of the note still shows).
        snap = _snapshot()
        seen = {}
        patcher = unittest.mock.patch('sim._store_content_result',
                                      lambda task_id, result: seen.__setitem__(task_id, result))
        patcher.start()
        self.addCleanup(patcher.stop)
        task = {'id': 't-apify', 'room': 'bank', 'title': 'check the budget'}
        usage = {'usedUsd': 1.2500, 'capUsd': 5.0, 'remainingUsd': 3.75}
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={}), \
             unittest.mock.patch.object(serve, 'APIFY_MONTHLY_BUDGET_USD', 5.0), \
             unittest.mock.patch.object(serve, '_openrouter_account_credits', return_value=None), \
             unittest.mock.patch.object(serve, '_apify_account_usage', return_value=usage):
            serve._run_bank_content(snap, 'nora', task)
        self.assertIn('Apify account (real): $1.2500 of $5.00 monthly cap', seen['t-apify']['note'])
        self.assertIn('$3.7500 remaining', seen['t-apify']['note'])

    def test_bank_content_omits_apify_reconcile_when_unavailable(self):
        # Reconcile None -> no Apify line, but the rest of the note is intact.
        snap = _snapshot()
        seen = {}
        patcher = unittest.mock.patch('sim._store_content_result',
                                      lambda task_id, result: seen.__setitem__(task_id, result))
        patcher.start()
        self.addCleanup(patcher.stop)
        task = {'id': 't-apify-2', 'room': 'bank', 'title': 'check the budget'}
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={}), \
             unittest.mock.patch.object(serve, 'APIFY_MONTHLY_BUDGET_USD', 5.0), \
             unittest.mock.patch.object(serve, '_openrouter_account_credits', return_value=None), \
             unittest.mock.patch.object(serve, '_apify_account_usage', return_value=None):
            serve._run_bank_content(snap, 'nora', task)
        note = seen['t-apify-2']['note']
        self.assertNotIn('Apify account', note)
        self.assertIn('director) reviewed the bank', note)


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

    def test_worker_gets_readonly_cumulative_view(self):
        # A worker (non-director) gets a READ-ONLY cumulative summary -- hive-mind
        # awareness of think tank spend. Only reallocation AUTHORITY is
        # director-only; visibility into "are we healthy" is not.
        snap = _snapshot()
        seen = self._store()
        task = {'id': 't1', 'room': 'bank', 'title': 'check the budget'}
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={'a': {'used': 1.0, 'calls': 1, 'lastAt': 'x', 'byDay': {d: 1.0 for d in _last_n_days(7)}}}):
            serve._run_bank_content(snap, 'ben', task)
        note = seen['t1']['note']
        # Worker sees the cumulative spend (read-only), not per-service reallocation.
        self.assertIn('$1.00', note)
        self.assertIn('cumulative budget', note)
        self.assertNotIn('reallocate', note)  # no director authority in a worker's view

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
        with unittest.mock.patch.object(serve, 'APIFY_MONTHLY_BUDGET_USD', 0), \
             unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={}):
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
        with unittest.mock.patch.object(serve, 'APIFY_MONTHLY_BUDGET_USD', 0), \
             unittest.mock.patch.object(serve, '_spend_ledger_read', return_value={}):
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