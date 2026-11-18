"""Briefing endpoint tests (coherence Priority 3): one endpoint joining
beliefs (ledger), open cases (investigations), decisions-for-player
(escalations), material activity (action_log), and spend posture. The
contract: every section degrades independently -- one wedged store must
never blank the whole brief -- and the surface stays operator-only."""

import json
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import serve  # noqa: E402


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _sql, _params):
        return self

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_sources(escalations=None, activity_rows=None, busted=None):
    """Patch every briefing input. `busted` names a section whose source
    raises, to prove independent degradation."""
    escalations = escalations or {}
    activity_rows = activity_rows or []
    patchers = [
        unittest.mock.patch(
            'serve._brief_beliefs',
            return_value=({'establishedCount': 2, 'established': [],
                           'disputedCount': 1, 'disputed': [],
                           'openCount': 3, 'open': [], 'staleCount': 0,
                           'stale': []}, None)),
        unittest.mock.patch(
            'serve._brief_investigations',
            return_value=({'openCount': 1, 'overdue': ['inv-x'],
                           'reassessmentDue': [],
                           'contradictedCases': [], 'open': []}, None)),
        unittest.mock.patch(
            'serve._brief_decisions_for_player',
            return_value=({'pendingCount': len(escalations),
                           'pending': list(escalations)}, None)),
        unittest.mock.patch(
            'serve._brief_activity',
            return_value=({'sinceTs': 0, 'actions': activity_rows}, None)),
        unittest.mock.patch(
            'serve._brief_spend',
            return_value=({'overrunFactor': 1.2, 'alerts': 1}, None)),
    ]
    if busted == 'investigations':
        patchers[1] = unittest.mock.patch(
            'serve._brief_investigations',
            return_value=(None, 'store wedged'))
    if busted == 'beliefs':
        patchers[0] = unittest.mock.patch(
            'serve._brief_beliefs', return_value=(None, 'ledger wedged'))
    for p in patchers:
        p.start()
    return patchers


class BriefShape(unittest.TestCase):
    def setUp(self):
        self.patchers = _patch_sources(
            escalations=[{'id': 'esc-1', 'kind': 'owner safety decision'}],
            activity_rows=[{'action': 'route_classified', 'count': 40},
                           {'action': 'investigation_concluded', 'count': 1}])

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    def test_all_four_questions_present(self):
        out = serve._build_briefing(1)
        sections = out['sections']
        self.assertEqual(out['sectionErrors'], {})
        self.assertEqual(sections['beliefs']['establishedCount'], 2)
        self.assertEqual(sections['investigations']['overdue'], ['inv-x'])
        self.assertEqual(sections['decisionsForPlayer']['pendingCount'], 1)
        self.assertEqual(sections['activity']['actions'][0]['action'],
                         'route_classified')
        self.assertEqual(sections['spend']['overrunFactor'], 1.2)

    def test_activity_window_is_bounded(self):
        serve._build_briefing(30)  # over the cap
        serve._build_briefing(0)   # under the floor
        # _brief_activity clamps internally; both calls must not raise.


class Degradation(unittest.TestCase):
    def test_one_wedged_store_does_not_blank_the_brief(self):
        patchers = _patch_sources(busted='investigations')
        try:
            out = serve._build_briefing(1)
            self.assertEqual(out['sectionErrors'],
                             {'investigations': 'store wedged'})
            self.assertIsNone(out['sections']['investigations'])
            self.assertEqual(out['sections']['beliefs']['openCount'], 3)
            self.assertEqual(out['sections']['decisionsForPlayer']
                             ['pendingCount'], 0)
        finally:
            for p in patchers:
                p.stop()

    def test_real_escalation_store_missing_is_empty_not_error(self):
        with unittest.mock.patch.object(serve, 'ESCALATIONS_PATH',
                                        '/nonexistent/escalations.json'):
            out, err = serve._brief_decisions_for_player()
            self.assertIsNone(err)
            self.assertEqual(out, {'pendingCount': 0, 'pending': []})

    def test_escalation_tokens_never_appear_in_the_brief(self):
        # The list-only contract: a pending escalation's approve/deny token
        # is the credential for its emailed link; the briefing must surface
        # the ID and kind, never the token.
        import tempfile
        store = {'esc-9': {'status': 'pending',
                           'kind': 'allowlist candidate',
                           'token': 'secret-token-value',
                           'openedAt': 1795020000.0}}
        with tempfile.NamedTemporaryFile('w', suffix='.json',
                                         delete=False) as fh:
            json.dump(store, fh)
            path = fh.name
        try:
            with unittest.mock.patch.object(serve, 'ESCALATIONS_PATH', path):
                out, err = serve._brief_decisions_for_player()
                self.assertIsNone(err)
                self.assertEqual(out['pendingCount'], 1)
                blob = json.dumps(out)
                self.assertNotIn('secret-token-value', blob)
                self.assertNotIn('token', blob)
        finally:
            os.unlink(path)


class EndpointGate(unittest.TestCase):
    def test_briefing_requires_operator(self):
        from starlette.testclient import TestClient
        response = TestClient(serve.app).get('/api/briefing')
        self.assertEqual(response.status_code, 401)
