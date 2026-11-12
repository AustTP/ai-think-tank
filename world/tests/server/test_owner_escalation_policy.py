"""Owner direction 2026-11-12: the operator does not make the tank's safety
calls. Three changes keep the player out of the decision loop:

1. Human-only escalations (floor 1.0 -- blocked command/pipeline/browser)
   resolve TANK-SIDE at creation: the block stands, no player page.
2. A delegable escalation whose full director chain declines or cannot
   decide FAILS CLOSED tank-side (deny + digest note), no player page.
3. Change requests gain a Telegram veto path: the created message carries a
   short veto code, /cr lists active windows, /veto <code> vetoes.

Plus: the Colab/Laya standby route is only advertised while its ssh forward
is alive, so a torn-down standby stops tripping spurious circuit breakers.
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402


class BlockedKindDeniesAtCreation(unittest.TestCase):
    def test_floor_1_kind_denies_tank_side_without_a_page(self):
        with unittest.mock.patch.object(serve, '_load_escalations', return_value={}), \
             unittest.mock.patch.object(serve, '_save_escalations') as save, \
             unittest.mock.patch.object(serve, '_escalation_floor', return_value=1.0), \
             unittest.mock.patch.object(serve, 'notify_human_of_escalation') as notify, \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, '_push_new_health_alerts') as push:
            esc_id = serve.create_escalation('blocked command', 'run rm -rf /',
                                             agent_id='ada')
        esc = save.call_args[0][0][esc_id]
        self.assertEqual(esc['status'], 'denied',
                         'a hard-blocked kind resolves at creation, not pending')
        self.assertIn('fails closed', esc['resolvedBy'])
        notify.assert_not_called()
        push.assert_called_once()

    def test_delegable_kind_stays_pending_and_silent(self):
        with unittest.mock.patch.object(serve, '_load_escalations', return_value={}), \
             unittest.mock.patch.object(serve, '_save_escalations') as save, \
             unittest.mock.patch.object(serve, '_escalation_floor', return_value=0.85), \
             unittest.mock.patch.object(serve, 'notify_human_of_escalation') as notify, \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, '_push_new_health_alerts') as push:
            esc_id = serve.create_escalation('owner safety decision', 'q?',
                                             agent_id='ada')
        esc = save.call_args[0][0][esc_id]
        self.assertEqual(esc['status'], 'pending')
        notify.assert_not_called()
        push.assert_not_called()


class ChainExhaustedFailsClosed(unittest.TestCase):
    def test_exhaustion_denies_without_notifying_the_player(self):
        esc = {'id': 'e1', 'status': 'pending', 'kind': 'owner safety decision',
               'question': 'q?', 'agentId': 'ada', 'ts': 0}
        with unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, '_push_new_health_alerts') as push, \
             unittest.mock.patch.object(serve, 'notify_human_of_escalation') as notify:
            ok = serve._fail_closed_chain_exhausted('e1', esc)
        self.assertTrue(ok)
        self.assertEqual(esc['status'], 'denied')
        self.assertIn('fails closed', esc['resolvedBy'])
        self.assertTrue(esc['humanNotified'])
        notify.assert_not_called()
        log.assert_called_once()
        push.assert_called_once()


class VetoByCode(unittest.TestCase):
    def setUp(self):
        # One registered window with a known token. The REAL schema comes
        # from selfedit's own _ensure_windows_table (veto_window writes
        # note/vetoed_by/vetoed_at/veto_reason -- a hand-rolled table broke
        # this on the fresh CI database).
        with serve._db() as conn:
            serve._selfedit._ensure_windows_table(conn)
            conn.execute(
                'INSERT INTO dormancy_windows (id, created, agent, date, start, '
                'start_ts, end_ts, duration_min, reason, status, veto_token) VALUES '
                "('win-t1', '1', 'leo', '2026-11-12', '11:00', 1.0, 2.0, 15, 'test', "
                "'approved', 'abcdefghij-SECRET-REST')")

    def tearDown(self):
        with serve._db() as conn:
            conn.execute("DELETE FROM dormancy_windows WHERE id = 'win-t1'")

    def test_veto_by_prefix_code_matches(self):
        with unittest.mock.patch.object(serve, 'log_action'):
            ok, payload = serve._selfedit.veto_window_by_code(
                'abcdefghij', 'not this one')
        self.assertTrue(ok, payload)
        with serve._db() as conn:
            row = conn.execute(
                "SELECT status FROM dormancy_windows WHERE id = 'win-t1'").fetchone()
        self.assertIn(row[0], ('cancelled', 'vetoed'))

    def test_veto_by_unknown_code_fails_closed(self):
        ok, payload = serve._selfedit.veto_window_by_code('zzzzzzzzzz')
        self.assertFalse(ok)
        self.assertIn('no pending', payload['error'])

    def test_veto_by_full_token_also_matches(self):
        with unittest.mock.patch.object(serve, 'log_action'):
            ok, payload = serve._selfedit.veto_window_by_code(
                'abcdefghij-SECRET-REST', 'full token')
        self.assertTrue(ok, payload)

    def test_active_window_rows_lists_only_live_windows(self):
        rows = serve._selfedit.active_window_rows(10)
        ids = [r['id'] for r in rows]
        self.assertIn('win-t1', ids)

    def test_veto_code_is_a_prefix_of_the_token(self):
        code = serve._selfedit._veto_code('win-t1')
        self.assertEqual(code, 'abcdefghij')


class StandbyRouteLiveness(unittest.TestCase):
    def test_dead_forward_keeps_the_route_off_the_chain(self):
        dead = {'pid': None, 'proc': None, 'session': None}
        with unittest.mock.patch.object(serve, '_colab_standby_enabled',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_COLAB_STANDBY_FORWARD', dead), \
             unittest.mock.patch.object(serve, '_get_setting', return_value='m1'), \
             unittest.mock.patch.object(serve, '_load_env', return_value={}), \
             unittest.mock.patch.object(serve, '_credits_remaining_cached',
                                        return_value=None), \
             unittest.mock.patch.object(serve, 'credits_pause_active',
                                        return_value=False), \
             unittest.mock.patch.object(serve, '_freepool_route_for_band',
                                        return_value=None):
            chain = serve._decision_model_chain()
        self.assertFalse(any('://' in c for c in chain),
                         'a torn-down standby must not be advertised')

    def test_live_forward_advertises_the_route(self):
        live = {'pid': 1, 'proc': object(), 'session': 'think-tank-standby'}
        with unittest.mock.patch.object(serve, '_colab_standby_enabled',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_COLAB_STANDBY_FORWARD', live), \
             unittest.mock.patch.object(serve.os, 'kill',
                                        side_effect=lambda pid, sig: None), \
             unittest.mock.patch.object(serve, '_get_setting', return_value='m1'), \
             unittest.mock.patch.object(serve, '_load_env', return_value={}), \
             unittest.mock.patch.object(serve, '_credits_remaining_cached',
                                        return_value=None), \
             unittest.mock.patch.object(serve, 'credits_pause_active',
                                        return_value=False), \
             unittest.mock.patch.object(serve, '_freepool_route_for_band',
                                        return_value=None):
            chain = serve._decision_model_chain()
        self.assertTrue(any('systemone' in c for c in chain),
                        'a live standby stays on the chain')


class TelegramVetoCommands(unittest.IsolatedAsyncioTestCase):
    async def test_veto_command_matches_by_code(self):
        with unittest.mock.patch.object(serve, '_selfedit') as se, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            se.veto_window_by_code.return_value = (True, {'id': 'win-t1'})
            chat_id, reply = await serve._telegram_operator_command(
                8906196966, '/veto abcdefghij bad idea')
        self.assertIn('Vetoed win-t1', reply)
        se.veto_window_by_code.assert_called_once()
        log.assert_called_once()

    async def test_veto_command_reports_failure(self):
        with unittest.mock.patch.object(serve, '_selfedit') as se, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            se.veto_window_by_code.return_value = (False,
                                                   {'error': 'no pending change request matches that code'})
            chat_id, reply = await serve._telegram_operator_command(
                8906196966, '/veto zzzzzzzzzz')
        self.assertIn('no pending', reply)
        log.assert_not_called()

    async def test_cr_command_lists_windows(self):
        with unittest.mock.patch.object(serve, '_selfedit') as se:
            se.active_window_rows.return_value = [{
                'id': 'win-t1', 'status': 'approved', 'date': '2026-11-12',
                'start': '11:00', 'duration_min': 15, 'title': 'Traceback capture',
                'reason': 'capture', 'veto_token': 'abcdefghij-REST'}]
            se._veto_code.return_value = 'abcdefghij'
            chat_id, reply = await serve._telegram_operator_command(8906196966, '/cr')
        self.assertIn('win-t1', reply)
        self.assertIn('code=abcdefghij', reply)
        self.assertIn('/veto', reply)

    async def test_non_command_falls_through(self):
        self.assertIsNone(await serve._telegram_operator_command(1, 'hello admin'))


if __name__ == '__main__':
    unittest.main()
