"""Tests for Cut 3 -- on-call escalation.

When the on-call agent CANNOT restore a broken product's work -- an incident is
repeatedly unassignable (assignment abandonment) OR a picked-up bug stays open
past the restore window (unrestored timeout) -- the escalation works WITH the
owning team's scrum master to file a backlog STORY (root cause scoped) or a
SPIKE (root cause unknown) back into the pipeline.

Deterministic -- no Jev, direct function calls with injected stubs, the same
pattern as test_refinement.py / test_cut2_processes.py. NOT actually DB-free
despite the state() helper's plain dicts: sim.py's escalation/refinement path
has inline `from serve import log_action` calls (a real side effect, not a
stub), which -- unless DB_PATH is redirected below -- write straight into
whatever real think_tank.db sits at serve.py's default path. Found:
a real "Auth broken" task and an "esc-test" escalation from THIS file's own
fixtures turned up in a live production think_tank.db after a routine test run,
because this file never isolated it.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
import sim

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-oncall-escalation-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _stub_escalate(choice='story'):
    def decider(state, instructions, product_id, title):
        return choice
    return decider


def _state():
    """A server-owned think tank: maya (admin director) -> ben (SM) with cora/zia
    as reports. One product owned by maya's team, one assigned bug task."""
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': [
            {'id': 'maya', 'name': 'Maya', 'role': 'Director', 'isDirector': True, 'isAdmin': True},
            {'id': 'ben', 'name': 'Ben', 'role': 'Engineer', 'director': 'maya'},
            {'id': 'cora', 'name': 'Cora', 'role': 'Engineer', 'director': 'maya'},
            {'id': 'zia', 'name': 'Zia', 'role': 'Engineer', 'director': 'maya'},
        ],
        'agents': {
            'ben': {'id': 'ben', 'name': 'Ben', 'role': 'Engineer', 'director': 'maya',
                    'x': 10, 'y': 10, 'busy': False, 'task': None, 'offDuty': False, 'visible': True},
            'cora': {'id': 'cora', 'name': 'Cora', 'role': 'Engineer', 'director': 'maya',
                     'x': 40, 'y': 10, 'busy': False, 'task': None, 'offDuty': False, 'visible': True},
            'zia': {'id': 'zia', 'name': 'Zia', 'role': 'Engineer', 'director': 'maya',
                    'x': 70, 'y': 10, 'busy': False, 'task': None, 'offDuty': False, 'visible': True},
        },
        'workQueue': [],
        'tasks': {},
        'products': {},
        'backlogRequests': [],
        'teams': [{'id': 'maya', 'name': "Maya's Crew", 'directorId': 'maya', 'scrumMasterId': 'ben'}],
        'roadmap': {},
        'runbooks': {},
        'growthPlans': {},
        'completedDeliverables': [],
        '_escalatedProducts': {},
        '_pendingEscalation': None,
    }
    sim.create_product(state, 'prod-1', 'Widget API', 's', 'spec', 'cora', 'sandbox-1',
                       team_id='maya')
    return state


def _open_bug(state, title='Auth broken', opened=100_000, task_id='task-1'):
    """Create an open, working incident task owned by prod-1."""
    state['tasks'][task_id] = {
        'id': task_id, 'title': title, 'room': 'pressoffice',
        'taskType': 'bug', 'productId': 'prod-1', 'assignedTo': 'cora',
        'status': 'working', 'openedAt': opened, 'workUntil': (opened // 1000) + 3600,
    }
    return state['tasks'][task_id]


class EscalationTrigger(unittest.TestCase):
    def test_abandoned_bug_escalates_to_scrum_master(self):
        """Trigger 1: an unassignable bug that hits the attempt cap files a
        pending escalation bound to the product team's scrum master."""
        state = _state()
        # Simulate the abandon path calling the hook with a bug work item.
        pick = {'title': 'Auth broken', 'room': 'pressoffice', 'taskType': 'bug',
                'productId': 'prod-1', 'incident': True}
        sim._on_work_item_abandoned(state, pick, 1000)
        pend = state['_pendingEscalation']
        self.assertIsNotNone(pend)
        self.assertEqual(pend['scrumMasterId'], 'ben')
        self.assertEqual(pend['directorId'], 'maya')
        self.assertEqual(pend['productId'], 'prod-1')
        self.assertEqual(pend['source'], 'assignment_abandoned')

    def test_unrestored_bug_times_out_escalates(self):
        """Trigger 2: a bug opened past RESTORE_TIMEOUT_MS and still working
        escalates through the sweep."""
        state = _state()
        _open_bug(state, opened=100_000)
        now_ms = 100_000 + sim.RESTORE_TIMEOUT_MS + 1
        sim._escalation_step(state, now_ms / 1000.0, now_ms)
        pend = state['_pendingEscalation']
        self.assertIsNotNone(pend)
        self.assertEqual(pend['source'], 'unrestored')
        self.assertEqual(pend['productId'], 'prod-1')

    def test_unrestored_bug_within_window_no_escalation(self):
        """A bug freshly opened does NOT escalate until the window elapses."""
        state = _state()
        _open_bug(state, opened=100_000)
        now_ms = 100_000 + sim.RESTORE_TIMEOUT_MS - 100
        sim._escalation_step(state, now_ms / 1000.0, now_ms)
        self.assertIsNone(state.get('_pendingEscalation'))

    def test_no_scrum_master_no_escalation(self):
        """A team with no designated scrum master has nobody to groom to -- no
        escalation is filed."""
        state = _state()
        state['teams'][0]['scrumMasterId'] = None
        pick = {'taskType': 'bug', 'productId': 'prod-1', 'incident': True,
                'title': 'broken'}
        sim._on_work_item_abandoned(state, pick, 1000)
        self.assertIsNone(state.get('_pendingEscalation'))

    def test_non_incident_abandon_not_escalated(self):
        """Only incidents/bugs escalate; a mundane abandoned card does not."""
        state = _state()
        pick = {'title': 'Write docs', 'room': 'observatory', 'taskType': 'code'}
        sim._on_work_item_abandoned(state, pick, 1000)
        self.assertIsNone(state.get('_pendingEscalation'))


class EscalationResolve(unittest.TestCase):
    def test_story_files_backlog_request_via_sm(self):
        """A 'story' groom files a backlogRequests record tagged
        origin:'oncall_escalation', filed BY the scrum master, so the Cut-1
        refinement ceremony (SM-owned story creation) cards it."""
        state = _state()
        sim._start_escalation(state, 'prod-1', 'maya', 'Auth broken', 'unrestored', 1000)
        pend = state['_pendingEscalation']
        old = sim._escalation_decider
        sim._escalation_decider = _stub_escalate('story')
        try:
            sim._resolve_escalation(state, pend, 2000)
        finally:
            sim._escalation_decider = old
        reqs = [r for r in state['backlogRequests'] if r.get('origin') == 'oncall_escalation']
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]['filedBy'], 'ben')  # the scrum master files it
        self.assertEqual(reqs[0]['productId'], 'prod-1')
        self.assertIn('Auth broken', reqs[0]['title'])
        self.assertEqual(reqs[0]['status'], 'pending')
        self.assertIsNone(state.get('_pendingEscalation'))
        self.assertTrue(state['_escalatedProducts']['prod-1']['resolved'])

    def test_spike_queues_spike_card(self):
        """A 'spike' groom queues a taskType:'spike' card with the unknown root
        cause in the title (non-gated lane)."""
        state = _state()
        sim._start_escalation(state, 'prod-1', 'maya', 'Auth broken', 'unrestored', 1000)
        pend = state['_pendingEscalation']
        old = sim._escalation_decider
        sim._escalation_decider = _stub_escalate('spike')
        try:
            sim._resolve_escalation(state, pend, 2000)
        finally:
            sim._escalation_decider = old
        self.assertEqual(state['workQueue'][0]['taskType'], 'spike')
        self.assertIn('root cause', state['workQueue'][0]['title'])
        self.assertIsNone(state.get('_pendingEscalation'))

    def test_outage_fallback_files_spike(self):
        """A None/unknown decider (Jev outage) defaults to a SPIKE -- an
        investigation card is strictly safer than guessing a story."""
        state = _state()
        sim._start_escalation(state, 'prod-1', 'maya', 'Auth broken', 'unrestored', 1000)
        pend = state['_pendingEscalation']
        old = sim._escalation_decider
        sim._escalation_decider = _stub_escalate(None)
        try:
            sim._resolve_escalation(state, pend, 2000)
        finally:
            sim._escalation_decider = old
        self.assertEqual(state['workQueue'][0]['taskType'], 'spike')
        self.assertEqual(state['_escalatedProducts']['prod-1']['outcome'], 'spike')

    def test_capped_concurrent_escalations_per_team(self):
        """ESCALATION_MAX_OPEN concurrent unresolved escalations per owning team
        are respected; further failures defer to later."""
        state = _state()
        for i in range(sim.ESCALATION_MAX_OPEN):
            sim._start_escalation(state, f'prod-{i}', 'maya', f'Incident {i}', 'unrestored', 1000 + i)
        # All capped slots are now open (unresolved). A next one is refused --
        # it must NOT open a new pending record or register a new escalated product.
        sim._start_escalation(state, 'prod-99', 'maya', 'Another', 'unrestored', 5000)
        self.assertNotIn('prod-99', state.get('_escalatedProducts', {}))
        self.assertEqual(len(state['_escalatedProducts']), sim.ESCALATION_MAX_OPEN)


class EscalationLifecycle(unittest.TestCase):
    def test_dedup_same_product_while_open(self):
        """While an escalation for a product is unresolved, a second failure on
        the same product does not open a duplicate."""
        state = _state()
        sim._start_escalation(state, 'prod-1', 'maya', 'Auth broken', 'unrestored', 1000)
        self.assertIsNotNone(state.get('_pendingEscalation'))
        sim._start_escalation(state, 'prod-1', 'maya', 'Auth broken', 'assignment_abandoned', 2000)
        self.assertIs(state.get('_pendingEscalation'), state.get('_pendingEscalation'))
        # Still only one pending record (the original, untouched) and one product key.
        self.assertEqual(len([x for x in state['_escalatedProducts'] if x == 'prod-1']), 1)

    def test_full_ceremony_driven_end_to_end(self):
        """Driving _escalation_step twice after a filed failure embargoes then
        resolves, restoring the scrum master to a clean prior state and landing
        the outcome."""
        state = _state()
        # Seed a pending escalation directly (as a trigger would).
        sim._start_escalation(state, 'prod-1', 'maya', 'Auth broken', 'unrestored', 1000)
        pend = state['_pendingEscalation']
        sim._escalation_step(state, 5.0, 5000)  # pass 1: embark (SM busy)
        self.assertTrue(state['_pendingEscalation']['embarked'])
        # SM was marked busy at the command center.
        self.assertTrue(state['agents']['ben']['busy'])
        old = sim._escalation_decider
        sim._escalation_decider = _stub_escalate('story')
        try:
            sim._escalation_step(state, 7.0, 7000)  # pass 2: resolve + restore
        finally:
            sim._escalation_decider = old
        # SM restored (not busy, no inRoom).
        self.assertFalse(state['agents']['ben']['busy'])
        self.assertIsNone(state['agents']['ben']['inRoom'])
        # A backlog request was filed.
        self.assertEqual(len(state['backlogRequests']), 1)
        self.assertIsNone(state.get('_pendingEscalation'))


class EscalationIntoRefinement(unittest.TestCase):
    def test_story_surfaces_in_next_refinement_groom(self):
        """Composition proof: the story the escalation files (origin:
        oncall_escalation, filed by the SM) is picked up by the SAME scrum
        master's refinement ceremony and groomed into a real card."""
        state = _state()
        sim._start_escalation(state, 'prod-1', 'maya', 'Auth broken', 'unrestored', 1000)
        sim._resolve_escalation(state, state['_pendingEscalation'], 2000,
                                decider=_stub_escalate('story'))
        req = state['backlogRequests'][0]
        self.assertEqual(req['status'], 'pending')
        # Now run the refinement ceremony: schedule -> embark -> resolve.
        def accept(instructions, criteria):
            return 'accept'
        state['teamRefinementAt'] = {'maya': 0}
        for _ in range(3):
            sim._refinement_step(state, 10.0, sim.REFINEMENT_CADENCE_MS + 50_000,
                                 decider=accept)
        # The escalation story was groomed into a real queued card.
        self.assertEqual(req['status'], 'accepted')
        self.assertTrue(any(i.get('title') == 'Auth broken' and i.get('room') == 'pressoffice'
                            for i in state['workQueue']))


if __name__ == '__main__':
    unittest.main()