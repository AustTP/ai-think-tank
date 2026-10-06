"""Closed-position accounting, isolated from the runtime database."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve


class TradingPnl(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        p = patch.object(serve, 'DB_PATH', os.path.join(directory.name, 'bank.db'))
        p.start()
        self.addCleanup(p.stop)
        self.trade = dict(broker='paper', positionId='p1', agentId='ben', symbol='ABC',
                          quoteCurrency='USD', status='closed', side='long', quantity='2',
                          entryPrice='100.10', exitPrice='110.20', fees='0.20',
                          openedAt='2026-10-01T12:00:00Z', closedAt='2026-10-02T12:00:00Z')

    def test_long_short_fees_and_currency_separation(self):
        serve._record_closed_position(self.trade)
        serve._record_closed_position(dict(self.trade, positionId='p2', side='short'))
        serve._record_closed_position(dict(self.trade, positionId='p3', quoteCurrency='EUR'))
        rows = serve._bank_profit_loss_view()['agents']
        usd = next(r for r in rows if r['quoteCurrency'] == 'USD')
        self.assertEqual(usd['netPnl'], '-0.40')
        self.assertEqual((usd['wins'], usd['losses'], usd['closedPositions']), (1, 1, 2))
        self.assertEqual(len(rows), 2)
        self.assertIn('ben (USD)', serve._bank_profit_loss_note())

    def test_retry_and_conflicting_closure(self):
        serve._record_closed_position(self.trade)
        serve._record_closed_position(dict(self.trade, quantity=2))
        with self.assertRaises(ValueError):
            serve._record_closed_position(dict(self.trade, exitPrice=120))
        self.assertEqual(len(serve._bank_profit_loss_view()['positions']), 1)

    def test_invalid_or_unclosed_positions_rejected(self):
        for change in ({'quantity': 0}, {'fees': -1}, {'entryPrice': 'NaN'},
                       {'exitPrice': 'Infinity'}, {'status': 'open'}, {'side': 'buy'},
                       {'closedAt': '2026-09-01T12:00:00Z'}, {'openedAt': '2026-10-01'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                serve._record_closed_position(dict(self.trade, **change))
        self.assertEqual(serve._bank_profit_loss_view()['agents'], [])

    def test_teller_reports_pnl_even_without_spend(self):
        import sim
        serve._record_closed_position(self.trade)
        snapshot = {'agentRoster': [{'id': 'ben', 'name': 'Ben'}], 'agents': {}, 'products': {}}
        with patch.object(serve, '_bank_budget_view', return_value={}), \
                patch.object(sim, '_store_content_result') as store:
            serve._run_bank_content(snapshot, 'ben', {'id': 'review'})
        self.assertIn('net 20.00', store.call_args[0][1]['note'])

    def test_api_authentication_and_agent_ownership(self):
        import asyncio
        from unittest.mock import AsyncMock, Mock
        request = Mock()
        request.json = AsyncMock(return_value=self.trade)
        request.headers = {'X-Agent-Key': 'key'}
        with patch.object(serve, '_require_player_session', return_value=False), \
                patch.object(serve, 'verify_agent_key', return_value=False):
            self.assertEqual(asyncio.run(serve.bank_record_closed_position(request)).status_code, 401)
            self.assertEqual(asyncio.run(serve.bank_profit_loss(request)).status_code, 401)
        with patch.object(serve, '_require_player_session', return_value=False), \
                patch.object(serve, 'verify_agent_key', return_value=True) as verify, \
                patch.object(serve, 'get_state_from_db', return_value={'agentRoster': [{'id': 'ben'}]}):
            result = asyncio.run(serve.bank_record_closed_position(request))
            self.assertEqual(result['netPnl'], '20.00')
            verify.assert_called_once_with('ben', 'key')
