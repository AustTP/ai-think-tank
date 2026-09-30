"""Tests for scheduled Backlog Refinement (Cut 1).

The scrum master -- a standing, director-designated per-team role -- is
responsible for turning agent-filed work into REAL stories. Agents communicate
what needs doing by FILING a work-request (deterministic signal generator:
an agent that just completed a task in a thin room files a follow-up). A weekly
ceremony convenes the scrum master + filing agents at the Command Center; at
resolve the injectable refinement decider grooms each request to a 'queued'
story (accept) or back to the requester (reject). Runs ungated like the Social
but no-ops with no pending requests or no scrum master. Deterministic -- no
Jev, direct _refinement_step calls. NOT actually DB-free: _refinement_step's
carry-away/logging path has inline `from serve import log_action` calls, a
real side effect that writes into whatever real think_tank.db sits at serve.py's
default path unless DB_PATH is redirected below. Found via a live
production think_tank.db that picked up test fixture rows after a routine
`tests/run_all.sh` run.
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
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-refinement-test-')
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


def _stub_decider(choice='accept'):
    """Deterministic stand-in for the Jev-backed _refinement_decider."""
    def decider(instructions, criteria):
        return choice
    return decider


def _step(state, _zero_unused, now, decider=None):
    """Drive one refinement pass with a deterministic decider threaded (a resolve
    would otherwise hit the network default)."""
    return sim._refinement_step(state, 1000.0, now, decider=decider or _stub_decider())


def _seed(**over):
    now_ms = 1_000_000
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': [
            {'id': 'faye', 'name': 'Faye', 'role': 'Admin', 'isAdmin': True},
            {'id': 'dev', 'name': 'Dev', 'role': 'Director', 'isDirector': True},
            {'id': 'ben', 'name': 'Ben', 'role': 'Engineer', 'director': 'dev'},
            {'id': 'ada', 'name': 'Ada', 'role': 'Researcher', 'director': 'dev'},
        ],
        'teams': [
            {'id': 'dev', 'name': "Dev's Crew", 'directorId': 'dev', 'scrumMasterId': 'ada'},
        ],
        'agents': {
            # Scrum master of Dev's crew -- idle on-duty.
            'ada': {'id': 'ada', 'x': 10, 'y': 10, 'dir': 'south', 'visible': True,
                    'busy': False, 'task': None, 'inRoom': None, 'offDuty': False,
                    'name': 'Ada', 'role': 'Researcher', 'weekApprovals': 1},
            # Filer + attendee -- idle on-duty.
            'ben': {'id': 'ben', 'x': 40, 'y': 10, 'dir': 'south', 'visible': True,
                    'busy': False, 'task': None, 'inRoom': None, 'offDuty': False,
                    'name': 'Ben', 'role': 'Engineer', 'weekApprovals': 1},
            # Busy agent -- must NOT be pulled into a ceremony while working.
            'faye': {'id': 'faye', 'x': 70, 'y': 10, 'dir': 'south', 'visible': True,
                     'busy': True, 'task': 'task-1', 'inRoom': 'pressoffice',
                     'offDuty': False, 'name': 'Faye', 'role': 'Admin', 'weekApprovals': 0},
            'dev': {'id': 'dev', 'x': 100, 'y': 10, 'dir': 'south', 'visible': True,
                    'busy': False, 'task': None, 'inRoom': None, 'offDuty': False,
                    'name': 'Dev', 'role': 'Director', 'weekApprovals': 0},
        },
        'tasks': {'task-1': {'id': 'task-1', 'assignedTo': 'faye', 'status': 'working',
                             'room': 'pressoffice', 'title': 'Build ledger tool',
                             'workUntil': 5_000_0}},
        'backlogRequests': [],
        'workQueue': [],
        'lastBacklogRefinementAt': 0,
    }
    state.update(over)
    return state


def _seed_with_request(**over):
    state = _seed()
    req = {
        'id': 'wrq-1', 'filedBy': 'ben', 'title': 'Add a ledger export',
        'room': 'pressoffice',
        'reason': 'Completed build ledger tool in pressoffice and the remaining backlog there has thinned.',
        'filedAt': 1_000_000, 'status': 'pending',
    }
    state['backlogRequests'] = [req]
    state.update(over)
    return state, req


class RefinementEntry(unittest.TestCase):
    def test_no_request_no_ceremony(self):
        state = _seed()
        # No pending requests, even though cadence is due -> no ceremony.
        _step(state, None, sim.REFINEMENT_CADENCE_MS + 1_000_000, decider=_stub_decider())
        self.assertEqual(state.get('pendingRefinements') or {}, {})
        self.assertEqual(state['backlogRequests'], [])
        self.assertEqual(state['workQueue'], [])

    def test_no_scrum_master_noop(self):
        state, _req = _seed_with_request()
        del state['teams']
        _step(state, None, sim.REFINEMENT_CADENCE_MS + 1_000_000, decider=_stub_decider())
        self.assertEqual(state.get('pendingRefinements') or {}, {})
        self.assertEqual(state['workQueue'], [])

    def test_convene_due_at_future(self):
        state, _req = _seed_with_request()
        # Pass 1: cadence due + pending requests -> schedules the record (embarked=False).
        _step(state, None, sim.REFINEMENT_CADENCE_MS + 1_000_000, decider=_stub_decider())
        pending = (state.get('pendingRefinements') or {}).get('dev')
        self.assertIsNotNone(pending)
        self.assertEqual(pending['scrumMasterId'], 'ada')
        self.assertIn('wrq-1', pending['reqIds'])
        # Pass 2: a convened-but-not-embarked record is embarked (attendees busy).
        _step(state, None, sim.REFINEMENT_CADENCE_MS + 1_000_000, decider=_stub_decider())
        pending = state['pendingRefinements']['dev']
        self.assertTrue(pending['embarked'])
        self.assertTrue(state['agents']['ben']['busy'])
        self.assertEqual(state['agents']['ben']['inRoom'], 'commandcenter')
        self.assertTrue(state['agents']['ada']['busy'])
        # Per-team cadence stamp advanced so this team doesn't re-fire immediately.
        self.assertIn('dev', state['teamRefinementAt'])
        self.assertGreaterEqual(state['teamRefinementAt']['dev'], 0)

    def test_busy_filer_defers_convene(self):
        state, req = _seed_with_request()
        req['filedBy'] = 'faye'  # faye is busy -> cannot convene
        _step(state, None, sim.REFINEMENT_CADENCE_MS + 1_000_000, decider=_stub_decider())
        self.assertEqual(state.get('pendingRefinements') or {}, {})
        # Not stamped so a later pass can retry once faye is free.
        self.assertEqual(state['lastBacklogRefinementAt'], 0)


class RefinementResolve(unittest.TestCase):
    def _convened(self):
        state, req = _seed_with_request()
        state['lastBacklogRefinementAt'] = 1_000_000
        state['pendingRefinements'] = {'dev': {
            'at': sim.REFINEMENT_MEET_MS + 1_000_000,
            'embarked': True, 'scrumMasterId': 'ada',
            'reqIds': ['wrq-1'], 'teamId': 'dev',
            'people': {
                'ben': {'offDuty': False, 'visible': True, 'x': 40, 'y': 10,
                        'dir': 'south', 'task': None, 'busy': False,
                        'inRoom': None, 'pairWith': None, 'handoff': None,
                        'workUntil': None},
                'ada': {'offDuty': False, 'visible': True, 'x': 10, 'y': 10,
                        'dir': 'south', 'task': None, 'busy': False,
                        'inRoom': None, 'pairWith': None, 'handoff': None,
                        'workUntil': None},
            },
        }}
        return state, req

    def test_accept_creates_story(self):
        state, req = self._convened()
        _step(state, None, sim.REFINEMENT_MEET_MS + 2_000_000, decider=_stub_decider('accept'))
        self.assertEqual(req['status'], 'accepted')
        # Real story hit the queue.
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(state['workQueue'][0]['title'], 'Add a ledger export')
        self.assertEqual(state['workQueue'][0]['room'], 'pressoffice')
        # Ceremony resolved: attendees restored idle, pending cleared.
        self.assertEqual(state.get('pendingRefinements') or {}, {})
        self.assertFalse(state['agents']['ben']['busy'])
        self.assertEqual(state['agents']['ben']['inRoom'], None)

    def test_reject_grooms_out(self):
        state, req = self._convened()
        _step(state, None, sim.REFINEMENT_MEET_MS + 2_000_000, decider=_stub_decider('reject'))
        self.assertEqual(req['status'], 'rejected')
        self.assertEqual(state['workQueue'], [])

    def test_decider_instructions_carry_medium_difficulty_guidance(self):
        # Absolute Zero: the grooming prompt itself steers toward MEDIUM-
        # difficulty cards and gives the reject criterion the trivial/filler
        # case, so the decider's accept/reject read the guidance, not just the
        # request's own wording.
        state, _req = self._convened()
        seen = {}

        def capturing_decider(instructions, criteria):
            seen['instructions'] = instructions
            seen['criteria'] = criteria
            return 'accept'

        _step(state, None, sim.REFINEMENT_MEET_MS + 2_000_000, decider=capturing_decider)
        self.assertIn('MEDIUM-difficulty', seen['instructions'])
        self.assertIn(sim.ABSOLUTE_ZERO_SCOPING_GUIDANCE, seen['instructions'])
        self.assertTrue(any('trivial/filler' in c['description'] for c in seen['criteria']))

    def test_accept_carryaway_does_not_flag_rejection(self):
        state, _req = self._convened()
        with serve._db() as conn:
            conn.execute("DELETE FROM action_log WHERE action = 'refinement_carryaway'")
        _step(state, None, sim.REFINEMENT_MEET_MS + 2_000_000, decider=_stub_decider('accept'))
        with serve._db() as conn:
            rows = conn.execute(
                "SELECT details FROM action_log WHERE action = 'refinement_carryaway'").fetchall()
        self.assertTrue(rows, 'an accepted carryaway should still be logged')
        self.assertFalse(any('"selfProposedRejected": true' in r[0] for r in rows))

    def test_reject_carryaway_logs_self_proposed_rejected_signal(self):
        # Absolute Zero rejection signal: a groomed-out self-proposal is logged
        # with an explicit marker the health check counts (a rising reject rate
        # = the think tank's own proposals trending trivial).
        state, _req = self._convened()
        with serve._db() as conn:
            conn.execute("DELETE FROM action_log WHERE action = 'refinement_carryaway'")
        _step(state, None, sim.REFINEMENT_MEET_MS + 2_000_000, decider=_stub_decider('reject'))
        with serve._db() as conn:
            rows = conn.execute(
                "SELECT details FROM action_log WHERE action = 'refinement_carryaway'").fetchall()
        self.assertTrue(rows, 'a rejected carryaway should still be logged')
        self.assertTrue(any('"selfProposedRejected": true' in r[0] for r in rows),
                        f'no flagged carryaway in {[r[0] for r in rows]}')

    def test_outage_fallback_accepts_real_gap(self):
        state, req = self._convened()
        _step(state, None, sim.REFINEMENT_MEET_MS + 2_000_000, decider=None)  # default None -> network
        # Outage fallback: req names a delegatable room -> accept deterministically.
        self.assertEqual(req['status'], 'accepted')
        self.assertEqual(len(state['workQueue']), 1)


class RefinementWorkerRestore(unittest.TestCase):
    def test_worker_resumes_task_with_budget(self):
        state, req = _seed_with_request()
        req['filedBy'] = 'ada'  # but last-worker semantics: ada is the scrum master
        # Make ada a mid-task worker when the ceremony resolves.
        del state['teams']  # no scrum master -> can't convene; instead resolve directly
        state['tasks'] = {'task-1': {'id': 'task-1', 'assignedTo': 'ada',
                                     'status': 'working', 'room': 'pressoffice',
                                     'title': 'Build ledger tool', 'workUntil': 5_000_0}}
        # Resolve directly with a snapshot where ada was mid-task.
        state['pendingRefinements'] = {'dev': {
            'at': sim.REFINEMENT_MEET_MS + 1_000_000, 'embarked': True,
            'scrumMasterId': 'ada', 'reqIds': ['wrq-1'], 'teamId': 'dev',
            'people': {
                'ada': {'offDuty': False, 'visible': True, 'x': 10, 'y': 10,
                        'dir': 'south', 'task': 'task-1', 'busy': True,
                        'inRoom': 'pressoffice', 'pairWith': None, 'handoff': None,
                        'workUntil': 5_000_0},
            },
        }}
        _step(state, None, sim.REFINEMENT_MEET_MS + 2_000_000, decider=_stub_decider('accept'))
        a = state['agents']['ada']
        self.assertEqual(a['task'], 'task-1')
        self.assertTrue(a['busy'])
        t = state['tasks']['task-1']
        # Budget extended by the meet duration (seconds), mirroring the Social.
        self.assertEqual(t['workUntil'], 5_000_0 + sim.REFINEMENT_MEET_MS // 1000)

    def test_offduty_filer_returns_offduty(self):
        state, req = _seed_with_request()
        # Simulate an off-duty filer convened then restored.
        state['lastBacklogRefinementAt'] = 1_000_000
        state['pendingRefinements'] = {'dev': {
            'at': sim.REFINEMENT_MEET_MS + 1_000_000, 'embarked': True,
            'scrumMasterId': 'ada', 'reqIds': ['wrq-1'], 'teamId': 'dev',
            'people': {
                'ben': {'offDuty': True, 'visible': False, 'x': 40, 'y': 10,
                        'dir': 'south', 'task': None, 'busy': False,
                        'inRoom': None, 'pairWith': None, 'handoff': None,
                        'workUntil': None},
                'ada': {'offDuty': False, 'visible': True, 'x': 10, 'y': 10,
                        'dir': 'south', 'task': None, 'busy': False,
                        'inRoom': None, 'pairWith': None, 'handoff': None,
                        'workUntil': None},
            },
        }}
        _step(state, None, sim.REFINEMENT_MEET_MS + 2_000_000, decider=_stub_decider('accept'))
        # Ben returns off-duty (vanished), not stranded visible.
        self.assertTrue(state['agents']['ben']['offDuty'])
        self.assertFalse(state['agents']['ben']['visible'])


class RefinementEarlyRelease(unittest.TestCase):
    def test_released_immediately_after_embark_no_fixed_hold(self):
        """A team that finishes refining does not wait out a clock: the ceremony
        resolves on the pass immediately after it embarks, not after a fixed
        window. Prompt release is the point -- nobody is held beyond the groom."""
        state, req = _seed_with_request()
        # Pass 1: cadence due + pending request -> schedule (embarked=False).
        _step(state, None, sim.REFINEMENT_CADENCE_MS + 1_000_000, decider=_stub_decider('accept'))
        # Pass 2: embark. (Everyone is in the Command Center.)
        _step(state, None, sim.REFINEMENT_CADENCE_MS + 1_000_000, decider=_stub_decider('accept'))
        self.assertTrue(state['pendingRefinements']['dev']['embarked'])
        # Pass 3: released the next pass regardless of the `at` clock.
        _step(state, None, sim.REFINEMENT_CADENCE_MS + 1_000_000, decider=_stub_decider('accept'))
        self.assertEqual(state.get('pendingRefinements') or {}, {})
        self.assertFalse(state['agents']['ben']['busy'])
        self.assertFalse(state['agents']['ada']['busy'])
        # And the request was groomed into a story.
        self.assertEqual(req['status'], 'accepted')
        self.assertEqual(len(state['workQueue']), 1)


class RefinementPerTeam(unittest.TestCase):
    def _two_teams(self):
        state = _seed()
        # Retrofit a second team so ben+ada roll under dev and a new pair under maya.
        state['agentRoster'].append({'id': 'zoe', 'name': 'Zoe', 'role': 'Engineer', 'director': 'maya'})
        state['agentRoster'].append({'id': 'kai', 'name': 'Kai', 'role': 'Researcher', 'director': 'maya'})
        state['teams'] = [
            {'id': 'dev', 'name': "Dev's Crew", 'directorId': 'dev', 'scrumMasterId': 'ada'},
            {'id': 'maya', 'name': "Maya's Crew", 'directorId': 'maya', 'scrumMasterId': 'zoe'},
        ]
        state['agents']['zoe'] = {'id': 'zoe', 'name': 'Zoe', 'role': 'Engineer', 'x': 200, 'y': 10,
                                  'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                                  'inRoom': None, 'offDuty': False}
        state['agents']['kai'] = {'id': 'kai', 'name': 'Kai', 'role': 'Researcher', 'x': 230, 'y': 10,
                                  'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                                  'inRoom': None, 'offDuty': False}
        # One request each from two DIFFERENT teams.
        req_dev = {'id': 'wrq-1', 'filedBy': 'ben', 'title': 'Dev ledger export',
                   'room': 'pressoffice', 'reason': 'gap', 'filedAt': 1_000_000, 'status': 'pending'}
        req_maya = {'id': 'wrq-2', 'filedBy': 'kai', 'title': 'Maya observatory sweep',
                    'room': 'observatory', 'reason': 'gap', 'filedAt': 1_000_100, 'status': 'pending'}
        state['backlogRequests'] = [req_dev, req_maya]
        return state

    def test_each_team_grooms_only_its_own_requests(self):
        state = self._two_teams()
        decider = _stub_decider('accept')
        # Pass 1+2+3: BOTH teams' ceremonies run concurrently now (per-team
        # slots) -- dev's scrum master grooms dev's request, maya's grooms
        # maya's. Each team only ever grooms its OWN requests.
        _step(state, None, sim.REFINEMENT_CADENCE_MS + 1_000_000, decider=decider)
        _step(state, None, sim.REFINEMENT_CADENCE_MS + 1_000_000, decider=decider)
        _step(state, None, sim.REFINEMENT_CADENCE_MS + 1_000_000, decider=decider)
        queued_titles = [x['title'] for x in state['workQueue']]
        self.assertIn('Dev ledger export', queued_titles)
        self.assertIn('Maya observatory sweep', queued_titles)
        self.assertEqual(state['backlogRequests'][0]['status'], 'accepted')
        self.assertEqual(state['backlogRequests'][1]['status'], 'accepted')
        # Both ceremonies resolved: no pending records left.
        self.assertEqual(state.get('pendingRefinements') or {}, {})

    def test_second_team_ceremony_runs_on_next_window(self):
        state = self._two_teams()
        st = _stub_decider('accept')
        now = sim.REFINEMENT_CADENCE_MS + 1_000_000
        # Both teams' ceremonies complete concurrently (per-team slots).
        for _ in range(3):
            _step(state, None, now, decider=st)
        # Both groomed their own request in the same window.
        self.assertEqual(state['backlogRequests'][0]['status'], 'accepted')
        self.assertEqual(state['backlogRequests'][1]['status'], 'accepted')
        self.assertIn('Dev ledger export', [x['title'] for x in state['workQueue']])
        self.assertIn('Maya observatory sweep', [x['title'] for x in state['workQueue']])


class RefinementSignalGenerator(unittest.TestCase):
    def test_no_followup_when_room_has_work(self):
        state = _seed()
        # Room pressoffice still has queued/in-flight work -> thin guard blocks.
        state['workQueue'] = [{'title': 'Another story', 'room': 'pressoffice'}]
        task = {'id': 'task-1', 'room': 'pressoffice', 'title': 'Build ledger tool', 'taskType': 'code'}
        sim._maybe_file_followup(state, 'ben', task, 1_000_000)
        self.assertEqual(state['backlogRequests'], [])

    def test_followup_filed_when_room_thin(self):
        state = _seed()
        task = {'id': 'task-1', 'room': 'pressoffice', 'title': 'Build ledger tool', 'taskType': 'code'}
        sim._maybe_file_followup(state, 'ben', task, 1_000_000)
        self.assertEqual(len(state['backlogRequests']), 1)
        self.assertEqual(state['backlogRequests'][0]['room'], 'pressoffice')
        self.assertEqual(state['backlogRequests'][0]['status'], 'pending')

    def test_spike_and_bug_do_not_file(self):
        state = _seed()
        sim._maybe_file_followup(state, 'ben', {'room': 'pressoffice', 'taskType': 'spike'}, 1_000_000)
        sim._maybe_file_followup(state, 'ben', {'room': 'pressoffice', 'taskType': 'bug'}, 1_000_000)
        self.assertEqual(state['backlogRequests'], [])

    def test_duplicate_pending_deduped(self):
        state = _seed()
        task = {'room': 'pressoffice', 'title': 'Build ledger tool', 'taskType': 'code'}
        sim._maybe_file_followup(state, 'ben', task, 1_000_000)
        sim._maybe_file_followup(state, 'ada', task, 1_000_000)
        self.assertEqual(len(state['backlogRequests']), 1)

    def test_invalid_room_rejected(self):
        state = _seed()
        self.assertIsNone(sim.file_work_request(state, 'ben', 'X', 'hangout'))  # not delegatable
        self.assertEqual(state['backlogRequests'], [])

    def test_followup_reason_carries_medium_difficulty_guidance(self):
        # Absolute Zero: the filed follow-up itself steers toward a MEDIUM-
        # difficulty next story (real, well-scoped, materially advancing the
        # room), so the grooming decider reads the scoping nudge with the
        # request, not just the ceremony's generic instructions.
        state = _seed()
        task = {'id': 'task-1', 'room': 'pressoffice', 'title': 'Build ledger tool', 'taskType': 'code'}
        sim._maybe_file_followup(state, 'ben', task, 1_000_000)
        req = state['backlogRequests'][0]
        self.assertIn(sim.ABSOLUTE_ZERO_SCOPING_GUIDANCE, req['reason'])


class RefinementCadence(unittest.TestCase):
    def test_not_due_before_cadence(self):
        state, _req = _seed_with_request()
        state['lastBacklogRefinementAt'] = 1_000_000
        # 1 second before the weekly window elapses -> no ceremony.
        _step(state, None, 1_000_000 + sim.REFINEMENT_CADENCE_MS - 1, decider=_stub_decider())
        self.assertEqual(state.get('pendingRefinements') or {}, {})

    def test_due_after_cadence(self):
        state, _req = _seed_with_request()
        state['lastBacklogRefinementAt'] = 1_000_000
        _step(state, None, 1_000_000 + sim.REFINEMENT_CADENCE_MS + 1, decider=_stub_decider())
        self.assertIn('dev', state.get('pendingRefinements') or {})


if __name__ == '__main__':
    unittest.main()