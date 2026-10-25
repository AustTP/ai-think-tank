"""Pre-refinement comprehension wiring (ask 2): the scrum master's
read-back of every filed work request before the decider judges it.

comprehension.py is pure and already tested on its own. These tests
cover the WIRING in sim._resolve_refinement: the comprehension is
computed before the judgment, stored on the pendingRefinements
record, logged as governance, threaded into the decider's
instructions as a read-back, and carried into the refinement digest
-- while a needs-clarification verdict stays CONTEXT (the injectable
decider still makes the accept/reject call, never the reader).

DB-isolated per test (SimIsolation below) since the refinement
ceremony touches serve paths; _log_governance and the digest writer
are patched with recorders so no DB or library writes happen.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import comprehension  # noqa: E402
import serve  # noqa: E402
import sim  # noqa: E402


class SimIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think-tank-comprehension-')
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
        self.governance = []
        self.digests = []
        self._gov = unittest.mock.patch.object(
            sim, '_log_governance',
            side_effect=lambda state, agent_id, action, details:
                self.governance.append((agent_id, action, details)))
        self._gov.start()
        self._digest = unittest.mock.patch.object(
            serve, 'log_refinement_digest',
            side_effect=lambda state, agents, groom:
                self.digests.append(groom))
        self._digest.start()
        self.addCleanup(self._gov.stop)
        self.addCleanup(self._digest.stop)

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self, requests):
        return {
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana', 'director': None},
                {'id': 'sm1', 'name': 'Sam', 'director': 'd1'},
                {'id': 'm1', 'name': 'Mia', 'director': 'd1'},
            ],
            'agents': {
                'd1': {'id': 'd1', 'busy': False, 'offDuty': False},
                'sm1': {'id': 'sm1', 'name': 'Sam', 'busy': False,
                        'offDuty': False},
                'm1': {'id': 'm1', 'busy': False, 'offDuty': False},
            },
            'teams': [{'id': 't1', 'directorId': 'd1'}],
            'backlogRequests': requests,
            'issues': {},
            'tasks': {},
            'workQueue': [],
        }

    def _pending(self, req_ids):
        return {'scrumMasterId': 'sm1', 'teamId': 't1',
                'reqIds': req_ids, 'people': {}}

    def _request(self, request_id, reason=None, title='Add a weather widget'):
        return {
            'id': request_id, 'status': 'pending', 'filedBy': 'm1',
            'title': title, 'room': 'observatory',
            'reason': reason or ('The observatory cannot show the current '
                                 'weather, and every desk check starts with '
                                 'the sky.'),
        }

    def _decider(self, choice='accept', captured=None):
        def decider(instructions, criteria):
            if captured is not None:
                captured['instructions'] = instructions
                captured['criteria'] = criteria
            return choice
        return decider


class ComprehensionWiring(SimIsolation):
    def test_comprehension_is_stored_logged_and_digested(self):
        state = self._state([self._request('r1')])
        pending = self._pending(['r1'])
        sim._resolve_refinement(state, pending, 1_000_000,
                                decider=self._decider())
        comp = pending['comprehensions']['r1']
        self.assertEqual(comp['requestId'], 'r1')
        self.assertEqual(comp['title'], 'Add a weather widget')
        self.assertEqual(comp['requester'], 'm1')
        self.assertEqual(comp['room'], 'observatory')
        self.assertEqual(comp['readiness'], comprehension.READY)
        self.assertIn('weather', comp['summary'])
        self.assertEqual(state['backlogRequests'][0]['status'], 'accepted')
        # Logged as governance by the scrum master, with the whole map.
        self.assertEqual(self.governance[0], (
            'sm1', 'refinement_comprehension',
            {'team': 't1', 'comprehensions': pending['comprehensions']}))
        # And carried into the refinement digest's groom record.
        self.assertEqual(self.digests[0]['scrumMasterId'], 'sm1')
        self.assertEqual(self.digests[0]['comprehensions'],
                         pending['comprehensions'])

    def test_the_read_back_is_woven_into_the_decider_instructions(self):
        state = self._state([self._request('r1')])
        pending = self._pending(['r1'])
        captured = {}
        sim._resolve_refinement(state, pending, 1_000_000,
                                decider=self._decider(captured=captured))
        instructions = captured['instructions']
        self.assertIn('Read-back:', instructions)
        self.assertIn('readiness: ready', instructions)
        self.assertIn(pending['comprehensions']['r1']['summary'],
                      instructions)

    def test_the_reader_override_replaces_summary_and_readiness(self):
        state = self._state([self._request('r1')])
        pending = self._pending(['r1'])

        def reader(req):
            return {'summary': 'override summary',
                    'readiness': comprehension.NEEDS_CLARIFICATION}

        sim._resolve_refinement(state, pending, 1_000_000,
                                decider=self._decider(), reader=reader)
        comp = pending['comprehensions']['r1']
        self.assertEqual(comp['summary'], 'override summary')
        self.assertEqual(comp['readiness'],
                         comprehension.NEEDS_CLARIFICATION)

    def test_needs_clarification_never_mechanically_decides(self):
        # A request too thin to comprehend is still the DECIDER's call:
        # the same thin request is accepted by one decider and rejected
        # by another, proving the reader verdict is context, not a vote.
        thin = self._request('r1', reason='too thin')
        accepted = self._state([dict(thin)])
        sim._resolve_refinement(accepted, self._pending(['r1']), 1_000_000,
                                decider=self._decider('accept'))
        self.assertEqual(accepted['backlogRequests'][0]['status'],
                         'accepted')
        rejected = self._state([dict(thin)])
        sim._resolve_refinement(rejected, self._pending(['r1']), 1_000_000,
                                decider=self._decider('reject'))
        self.assertEqual(rejected['backlogRequests'][0]['status'],
                         'rejected')
        # The ceremony record is popped once resolved.
        self.assertEqual(accepted['pendingRefinements'], {})
        self.assertEqual(rejected['pendingRefinements'], {})
        # The ceremony stored the thin read-back before popping it.
        self.assertEqual(self.governance[0][1], 'refinement_comprehension')
        stored = self.governance[0][2]['comprehensions']['r1']
        self.assertEqual(stored['readiness'],
                         comprehension.NEEDS_CLARIFICATION)

    def test_requests_outside_the_ceremony_are_not_comprehended(self):
        state = self._state([self._request('r1'),
                             self._request('r2')])
        pending = self._pending(['r1'])
        sim._resolve_refinement(state, pending, 1_000_000,
                                decider=self._decider())
        self.assertEqual(list(pending['comprehensions']), ['r1'])


if __name__ == '__main__':
    unittest.main()
