"""Tests pinning the "every model call accrues its cost" guarantee.

The think tank's spend accounting is a hard invariant: any real OpenRouter call
that does NOT reach _accrue_spend silently undercounts the monthly cap
(SPEND_CAP_USD) and the Bank. This suite locks the paths that used to leak:

  - the daily model-tier verify probes (_verify_model_works_sync /
    _verify_decision_model_works_sync) -- the "just existing" cost;
  - the /api/intent/clarify lane (one or two mid-tier chat calls per question).

Every test patches the network boundary and captures _accrue_spend, so no real
money is spent and no real state is touched.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve


class ModelVerifyProbeAccrual(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-spend-accrual-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_chat_verify_probe_accrues_to_model_verify_bucket(self):
        # The daily tier-refresh probe is a REAL chat-completions call -- its
        # cost must land in the ledger (__model_verify__), or the cap/Bank
        # would undercount the "just existing" housekeeping spend.
        fake = {'choices': [{'message': {'content': 'hi'}}],
                'usage': {'cost': 0.0123}}
        calls = []
        with unittest.mock.patch.object(serve, '_call_openrouter_sync', return_value=fake), \
             unittest.mock.patch.object(serve, '_accrue_spend',
                                        side_effect=lambda s, c: calls.append((s, c))):
            ok = serve._verify_model_works_sync('some/model')
        self.assertTrue(ok)
        self.assertEqual(calls, [('__model_verify__', 0.0123)])

    def test_chat_verify_probe_no_cost_still_accrues_zero_cleanly(self):
        # No usage.cost in the response -> _accrue_spend is called with 0.0,
        # which it drops (a falsy cost records nothing) -- the probe must not
        # raise on that path.
        fake = {'choices': [{'message': {'content': 'hi'}}]}
        with unittest.mock.patch.object(serve, '_call_openrouter_sync', return_value=fake), \
             unittest.mock.patch.object(serve, '_accrue_spend') as accrue:
            self.assertTrue(serve._verify_model_works_sync('some/model'))
        accrue.assert_called_once()
        self.assertEqual(accrue.call_args[0][0], '__model_verify__')

    def test_decision_verify_probe_accrues_to_model_verify_bucket(self):
        # Same guarantee for the decisions-endpoint probe: a real API call on
        # the alpha/decisions wire format must accrue, never silently free.
        fake = json.dumps({'answers': {'choice': {'choice': 'a', 'confidence': 0.9}},
                           'usage': {'cost': 0.0045}})
        calls = []
        with unittest.mock.patch.object(serve, '_decision_request',
                                        return_value=object()), \
             unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                        return_value=fake), \
             unittest.mock.patch.object(serve, '_accrue_spend',
                                        side_effect=lambda s, c: calls.append((s, c))):
            ok = serve._verify_decision_model_works_sync('some/decision-model')
        self.assertTrue(ok)
        self.assertEqual(calls, [('__model_verify__', 0.0045)])


def _director(rid, is_admin=False, name=None, director='maya', **over):
    d = {'id': rid, 'name': name or rid.title(), 'role': 'admin' if is_admin else 'engineer',
         'isAdmin': is_admin, 'director': director}
    d.update(over)
    return d


def _state():
    roster = [
        _director('maya', is_admin=True, name='Maya', director='maya'),
        _director('ben', name='Ben'),
        _director('cora', name='Cora'),
        _director('dax', name='Dax'),
        _director('zia', name='Zia'),
    ]
    return {
        'agentRoster': roster,
        'agents': {a['id']: {'id': a['id'], 'name': a['name'], 'role': a['role'],
                             'offDuty': False, 'profile': {'mission': 'help the think tank'}}
                   for a in roster},
        'teams': [{'id': 'mayateam', 'directorId': 'maya', 'scrumMasterId': 'cora'}],
        'products': {'p1': {'id': 'p1', 'name': 'Parser', 'teamId': 'maya',
                            'nameKeeps': ['parser']}},
        'completedDeliverables': [
            {'id': 't1', 'title': 'Refactor the Parser', 'room': 'pressoffice',
             'agentId': 'dax', 'grade': 8.0, 'gradedAt': 1000, 'lastReviewed': '2026-09-01T00:00:00Z'}],
        'sprints': {},
        'workQueue': [],
        'tasks': {},
    }


class ClarifyLaneAccrual(unittest.TestCase):
    """The clarify lane makes one or two real mid-tier chat calls per question.
    Each must accrue to the __clarify__ bucket."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-clarify-accrual-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()
        # Clarify shares the rate-limited ask lane -- clear it per test.
        serve._rate_limit_calls.pop(serve.ASK_LANE_RATE_LIMIT_KEY, None)

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _client(self, state):
        from fastapi.testclient import TestClient
        c = TestClient(serve.app)
        sid = serve.create_session()
        c.cookies.set(serve.SESSION_COOKIE_NAME, sid)
        return c, unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state)

    def test_clarify_answer_accrues_to_clarify_bucket(self):
        s = _state()
        c, state_patch = self._client(s)
        fake = {'choices': [{'message': {'content': 'We streamed the input.'}}],
                'usage': {'cost': 0.0234}}
        calls = []
        # The clarify handler resolves its model TIER via a live Jev decision --
        # patch the decider so no real decisions call leaves the test.
        with state_patch, \
             unittest.mock.patch.object(serve, '_tier_gate_decider',
                                        lambda purpose, criteria: ('low', 1.0)), \
             unittest.mock.patch.object(serve, '_library_search_matches',
                                        return_value=[{'path': 'projects/p1/README.md', 'snippet': 'input'}]), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync', return_value=fake), \
             unittest.mock.patch.object(serve, '_accrue_spend',
                                        side_effect=lambda s2, c2: calls.append((s2, c2))):
            resp = c.post('/api/intent/clarify',
                          json={'productId': 'p1', 'question': 'how was it parsed?'})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn('streamed', resp.json()['reply'])
        self.assertIn(('__clarify__', 0.0234), calls,
                      'the clarify answer must accrue its real usage cost')

    def test_clarify_escalation_accrues_both_calls(self):
        s = _state()
        # Completing agent is the admin so escalation is deterministic (admins
        # are never on-call, so completing != on-call).
        s['completedDeliverables'] = [
            {'id': 't1', 'title': 'Refactor the Parser', 'room': 'pressoffice',
             'agentId': 'maya', 'grade': 8.0, 'gradedAt': 1000, 'lastReviewed': '2026-09-01T00:00:00Z'}]
        c, state_patch = self._client(s)
        first = {'choices': [{'message': {'content': serve._CLARIFY_ESCALATE_TOKEN}}],
                 'usage': {'cost': 0.0111}}
        second = {'choices': [{'message': {'content': 'I landed that.'}}],
                  'usage': {'cost': 0.0222}}
        calls = []
        # Non-empty KB so the on-call's grounded read runs first and the
        # explicit-refusal token path fires (an empty KB would skip the on-call
        # entirely -- see test_empty_kb_direct_escalation_accrues_single_call).
        with state_patch, \
             unittest.mock.patch.object(serve, '_tier_gate_decider',
                                        lambda purpose, criteria: ('low', 1.0)), \
             unittest.mock.patch.object(serve, '_library_search_matches',
                                        return_value=[{'path': 'projects/p1/README.md', 'snippet': 'x'}]), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        side_effect=[first, second]), \
             unittest.mock.patch.object(serve, '_accrue_spend',
                                        side_effect=lambda s2, c2: calls.append((s2, c2))):
            resp = c.post('/api/intent/clarify',
                          json={'productId': 'p1', 'question': 'q'})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['escalatedTo'], 'maya')
        self.assertEqual(calls, [('__clarify__', 0.0111), ('__clarify__', 0.0222)],
                         'BOTH clarify calls (on-call answer + escalation) must accrue')

    def test_empty_kb_direct_escalation_accrues_single_call(self):
        # Zero KB matches skips the on-call router entirely (grounding: nothing
        # to answer from) and asks the completing agent directly -- exactly ONE
        # mid-tier call, and that single call must still accrue.
        s = _state()
        s['completedDeliverables'] = [
            {'id': 't1', 'title': 'Refactor the Parser', 'room': 'pressoffice',
             'agentId': 'maya', 'grade': 8.0, 'gradedAt': 1000, 'lastReviewed': '2026-09-01T00:00:00Z'}]
        c, state_patch = self._client(s)
        fake = {'choices': [{'message': {'content': 'I landed that.'}}],
                'usage': {'cost': 0.01}}
        calls = []
        with state_patch, \
             unittest.mock.patch.object(serve, '_tier_gate_decider',
                                        lambda purpose, criteria: ('low', 1.0)), \
             unittest.mock.patch.object(serve, '_library_search_matches', return_value=[]), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync', return_value=fake) as mc, \
             unittest.mock.patch.object(serve, '_accrue_spend',
                                        side_effect=lambda s2, c2: calls.append((s2, c2))):
            resp = c.post('/api/intent/clarify',
                          json={'productId': 'p1', 'question': 'q'})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(mc.call_count, 1)
        self.assertEqual(resp.json()['escalatedTo'], 'maya')
        self.assertEqual(calls, [('__clarify__', 0.01)],
                         'the completing agent\'s single call must still accrue')


if __name__ == '__main__':
    unittest.main(verbosity=2)