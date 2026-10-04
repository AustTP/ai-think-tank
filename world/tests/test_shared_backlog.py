"""Tests for WS-14: shared backlog + features + sprint retrospectives.

When every team is already committed to an active sprint, extra large asks are
broken down into stories/spikes filed in a SHARED unassigned backlog under a
FEATURE (created when none exists). Any team is eligible to pull at sprint close
(first-come): the pull claims the item with a team-scoped story key and queues
it as real work tagged with its feature + backlog item. A closed sprint also
runs a START / STOP / CONTINUE retrospective (scrum master + team, DIRECTOR
excluded) and kicks backlog refinement -- which a team in an active sprint
never convenes mid-sprint.

Hermetic: temp DB (the ceremony logging path writes through serve.log_action),
no network (retro/refinement deciders are injected stubs).
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
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-shared-backlog-test-')
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


def _seed(**over):
    """A minimal roster + teams map: faye (admin), dev (director), ben + ada
    (dev's reports, ada the designated scrum master)."""
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': [
            {'id': 'faye', 'name': 'Faye', 'role': 'Admin', 'isAdmin': True},
            {'id': 'dev', 'name': 'Dev', 'role': 'Director', 'isDirector': True},
            {'id': 'ben', 'name': 'Ben', 'role': 'Engineer', 'director': 'dev'},
            {'id': 'ada', 'name': 'Ada', 'role': 'Researcher', 'director': 'dev'},
        ],
        'teams': [
            {'id': 'faye', 'name': "Faye's Crew", 'directorId': 'faye'},
            {'id': 'dev', 'name': "Dev's Crew", 'directorId': 'dev', 'scrumMasterId': 'ada'},
        ],
        'agents': {
            'faye': {'id': 'faye', 'name': 'Faye', 'x': 1, 'y': 1, 'dir': 'south',
                     'visible': True, 'busy': False, 'task': None, 'inRoom': None,
                     'offDuty': False, 'pairWith': None, 'handoff': None, 'workUntil': None},
            'dev': {'id': 'dev', 'name': 'Dev', 'x': 1, 'y': 1, 'dir': 'south',
                    'visible': True, 'busy': False, 'task': None, 'inRoom': None,
                    'offDuty': False, 'pairWith': None, 'handoff': None, 'workUntil': None},
            'ben': {'id': 'ben', 'name': 'Ben', 'x': 1, 'y': 1, 'dir': 'south',
                    'visible': True, 'busy': False, 'task': None, 'inRoom': None,
                    'offDuty': False, 'pairWith': None, 'handoff': None, 'workUntil': None},
            'ada': {'id': 'ada', 'name': 'Ada', 'x': 1, 'y': 1, 'dir': 'south',
                    'visible': True, 'busy': False, 'task': None, 'inRoom': None,
                    'offDuty': False, 'pairWith': None, 'handoff': None, 'workUntil': None},
        },
        'workQueue': [],
        'tasks': {},
        'sprints': {},
        'backlogRequests': [],
        'issues': {},
        'issueCounters': {},
    }
    state.update(over)
    return state


def _stub_retro():
    """Deterministic stand-in for the Jev-backed _retro_decider."""
    def decider(state, instructions, sprint_id, landed):
        return {'start': ['Ship smaller slices'], 'stop': ['Review late in the cycle'],
                'continue': ['Peer approvals']}
    return decider


class FeaturesAndBacklog(unittest.TestCase):
    def test_create_or_reuse_feature(self):
        state = _seed()
        f1 = sim.create_or_reuse_feature(state, 'Summer Summit', 1_000_000,
                                         quarter='2026-Q4', created_by='faye')
        self.assertEqual(f1['id'], 'feat-1')
        self.assertEqual(f1['name'], 'Summer Summit')
        self.assertEqual(f1['quarter'], '2026-Q4')
        self.assertEqual(state['featureCounter'], 1)
        # Same name (case-insensitive) reuses, never duplicates.
        f2 = sim.create_or_reuse_feature(state, 'summer summit', 1_000_001, created_by='dev')
        self.assertIs(f2, f1)
        self.assertEqual(state['featureCounter'], 1)
        # A different name creates a new feature.
        f3 = sim.create_or_reuse_feature(state, 'Winter Sale', 1_000_002)
        self.assertEqual(f3['id'], 'feat-2')
        self.assertEqual(state['featureCounter'], 2)

    def test_add_backlog_item_defaults_and_cap(self):
        state = _seed()
        feature = sim.create_or_reuse_feature(state, 'Summer Summit', 1_000_000)
        item = sim.add_backlog_item(state, 'Land a launch page', feature['id'],
                                    'faye', 1_000_000,
                                    acceptance_criteria='Page loads without errors',
                                    size_estimate='M')
        self.assertEqual(item['id'], 'bl-1')
        self.assertEqual(item['status'], 'ready')
        self.assertEqual(item['type'], 'story')
        self.assertIsNone(item['teamId'])
        self.assertIsNone(item['storyKey'])
        self.assertIn('bl-1', feature['storyIds'])
        # A card carries NO room: the buildings are shared, so the room is an
        # execution detail the agent who picks the card up decides.
        self.assertNotIn('room', item)
        self.assertNotIn('rooms', item)
        # Malformed (no title) is rejected.
        self.assertIsNone(sim.add_backlog_item(state, '', feature['id'], 'faye', 1_000_000))
        # Cap: once at MAX_BACKLOG_ITEMS, further files are rejected.
        for _ in range(sim.MAX_BACKLOG_ITEMS - 1):
            sim.add_backlog_item(state, 'More work', None, 'faye', 1_000_000)
        self.assertIsNone(sim.add_backlog_item(state, 'Over the cap', None, 'faye', 1_000_000))
        self.assertEqual(len(state['backlog']), sim.MAX_BACKLOG_ITEMS)

    def test_blocked_item_is_not_eligible(self):
        state = _seed()
        sim.add_backlog_item(state, 'A ready story', None, 'faye', 1_000_000)
        sim.add_backlog_item(state, 'B blocked story', None, 'faye', 1_000_000,
                             blocked_by='Dependency not landed')
        ready = sim.backlog_ready_items(state)
        self.assertEqual([r['title'] for r in ready], ['A ready story'])

    def test_pull_backlog_item_fifo_and_assignment(self):
        state = _seed()
        sim.add_backlog_item(state, 'Oldest', None, 'faye', 1_000_000)
        sim.add_backlog_item(state, 'Newest', None, 'faye', 1_000_001)
        first = sim.pull_backlog_item(state, 'dev', 1_000_002)
        self.assertEqual(first['title'], 'Oldest')  # FIFO
        self.assertEqual(first['teamId'], 'dev')
        self.assertEqual(first['storyKey'], 'DEV-1')  # team-scoped issue counter
        self.assertEqual(first['status'], 'picked')
        self.assertEqual(state['issueCounters'], {'DEV': 1})
        second = sim.pull_backlog_item(state, 'dev', 1_000_003)
        self.assertEqual(second['title'], 'Newest')
        self.assertEqual(second['storyKey'], 'DEV-2')
        self.assertIsNone(sim.pull_backlog_item(state, 'dev', 1_000_004))  # empty

    def test_pull_backlog_for_team_queues_tagged_work(self):
        state = _seed()
        feature = sim.create_or_reuse_feature(state, 'Summer Summit', 1_000_000)
        sim.add_backlog_item(state, 'Land a launch page', feature['id'],
                             'faye', 1_000_000, acceptance_criteria='Page loads clean')
        sim.add_backlog_item(state, 'Explore the approach', feature['id'],
                             'faye', 1_000_001, item_type='spike', size_estimate='M')
        # The card is queued ROOM-LESS: the agent who picks it up figures out
        # where the work needs to happen (_assign_due_item resolves the room at
        # assignment). Exactly ONE work item is queued per card -- no fan-out.
        sim._pull_backlog_for_team(state, 'dev', 1_000_002)
        story = state['workQueue'][0]
        self.assertEqual(story['title'], 'Land a launch page')
        self.assertEqual(story['featureId'], 'feat-1')
        self.assertEqual(story['backlogItemId'], 'bl-1')
        self.assertEqual(story['teamId'], 'dev')
        self.assertEqual(story['taskType'], 'code')
        self.assertIsNone(story['room'])  # room-free until assignment
        self.assertNotIn('room', state['backlog'][0])  # the card itself too
        # Acceptance criteria survive onto the queued story's instructions.
        self.assertIn('Page loads clean', story['instructions'])
        # The spike is pulled next, as a time-boxed investigation.
        sim._pull_backlog_for_team(state, 'dev', 1_000_003)
        spike = state['workQueue'][1]
        self.assertEqual(spike['taskType'], 'spike')
        self.assertEqual(spike['budgetMs'], 60_000)
        self.assertIsNone(spike['room'])
        self.assertEqual(spike['featureId'], 'feat-1')
        self.assertEqual(len(state['workQueue']), 2)  # one item per card

    def test_assign_resolves_room_for_room_less_card(self):
        # A room-free card gets its room only at ASSIGNMENT time, chosen by the
        # nature of the work: an investigation spike -> observatory (research),
        # a deliverable story -> pressoffice (real files get written).
        self.assertEqual(sim._resolve_assignment_room({'taskType': 'spike'}), 'observatory')
        self.assertEqual(sim._resolve_assignment_room({'taskType': 'code'}), 'pressoffice')
        # _assign_due_item writes the resolved room onto the pick so the
        # walk/gate/grade lifecycle (which routes by task['room']) sees it.
        state = _seed()
        pick = {'title': 'Land a launch page', 'taskType': 'code',
                'backlogItemId': 'bl-1', 'featureId': 'feat-1'}
        self.assertNotIn('room', pick)
        self.assertIsNone(sim._pull_backlog_for_team(state, 'dev', 1_000_000))
        # Room resolution is deterministic via the helper the assignment path uses.
        self.assertEqual(sim._resolve_assignment_room(pick), 'pressoffice')


class SprintClose(unittest.TestCase):
    def _closed_ready(self):
        state = _seed()
        state['sprints']['spr-1'] = {
            'id': 'spr-1', 'name': 'Launch', 'goal': 'Ship April', 'ownerId': 'faye',
            'createdAt': 900_000, 'targetDate': None, 'teamIds': ['dev'],
            'status': 'active', 'items': [('Land a launch page', 'pressoffice')],
        }
        state['tasks']['t1'] = {'title': 'Land a launch page', 'room': 'pressoffice',
                                'status': 'done'}
        return state

    def test_auto_close_queues_retro_pull_and_refinement(self):
        state = self._closed_ready()
        feature = sim.create_or_reuse_feature(state, 'Summer Summit', 800_000)
        sim.add_backlog_item(state, 'Land a launch page', feature['id'],
                             'faye', 800_001, size_estimate='S')
        sim.add_backlog_item(state, 'A second item', feature['id'],
                             'faye', 800_002, size_estimate='S')
        closed = sim._auto_close_completed_sprints(state, 1_000_000)
        self.assertEqual(closed, ['spr-1'])
        self.assertEqual(state['sprints']['spr-1']['status'], 'closed')
        # Retro queued for the closed sprint.
        self.assertIn('spr-1', state['pendingSprintRetros'])
        # First eligible item pulled for the sprint's team (first-come).
        pulled = state['backlog'][0]
        self.assertEqual(pulled['status'], 'picked')
        self.assertEqual(pulled['teamId'], 'dev')
        self.assertEqual(pulled['storyKey'], 'DEV-1')
        self.assertNotIn('spr-1', state['workQueue'][0].get('sprintId') or [])
        self.assertEqual(state['workQueue'][0]['backlogItemId'], 'bl-1')
        self.assertEqual(state['workQueue'][0]['featureId'], 'feat-1')
        # The other item stays unassigned for the next free team.
        self.assertEqual(state['backlog'][1]['teamId'], None)
        # Refinement kicked to be due immediately (stamp zeroed), not weekly.
        self.assertEqual(state['teamRefinementAt']['dev'], 0)
        # A second close does not re-queue the retro.
        sim._auto_close_completed_sprints(state, 1_000_001)
        self.assertEqual(state['pendingSprintRetros'].count('spr-1'), 1)

    def test_no_retro_for_incomplete_sprint(self):
        state = self._closed_ready()
        state['sprints']['spr-1']['items'] = [('Land a launch page', 'pressoffice'),
                                              ('Still open', 'observatory')]
        closed = sim._auto_close_completed_sprints(state, 1_000_000)
        self.assertEqual(closed, [])
        self.assertEqual(state['sprints']['spr-1']['status'], 'active')
        self.assertNotIn('spr-1', state.get('pendingSprintRetros', []))


class Retrospective(unittest.TestCase):
    def _closed_state(self):
        state = _seed()
        state['sprints']['spr-1'] = {
            'id': 'spr-1', 'name': 'Launch', 'goal': 'Ship April', 'ownerId': 'faye',
            'createdAt': 900_000, 'targetDate': None, 'teamIds': ['dev'],
            'status': 'closed', 'closedAt': 1_000_000, 'autoClosed': True,
            'items': [('Land a launch page', 'pressoffice')],
        }
        state['tasks']['t1'] = {'title': 'Land a launch page', 'room': 'pressoffice',
                                'status': 'done'}
        sim._on_sprint_closed(state, state['sprints']['spr-1'], 1_000_000)
        return state

    def test_retro_convenes_and_resolves_excluding_director(self):
        state = self._closed_state()
        # Pass 1: convene the ceremony at the Command Center.
        sim._retro_step(state, 1000.0, 1_000_001, decider=_stub_retro())
        self.assertIn('spr-1', state['pendingRetrospectives'])
        self.assertNotIn('spr-1', state['pendingSprintRetros'])
        pend = state['pendingRetrospectives']['spr-1']
        # Attendees = scrum master + team members; the DIRECTOR is excluded.
        self.assertIn('ada', pend['people'])
        self.assertIn('ben', pend['people'])
        self.assertNotIn('dev', pend['people'])
        for aid in pend['people']:
            self.assertTrue(state['agents'][aid]['busy'])
            self.assertEqual(state['agents'][aid]['inRoom'], 'commandcenter')
        # Pass 2: resolve -- the record lands and attendees are restored.
        sim._retro_step(state, 1000.0, 1_000_001 + sim.RETRO_MEET_MS + 1, decider=_stub_retro())
        self.assertNotIn('spr-1', state['pendingRetrospectives'])
        retro = state['retrospectives']['spr-1']
        self.assertEqual(retro['start'], ['Ship smaller slices'])
        self.assertEqual(retro['stop'], ['Review late in the cycle'])
        self.assertEqual(retro['continue'], ['Peer approvals'])
        self.assertEqual(retro['landed'], ['Land a launch page'])
        self.assertNotIn('dev', retro['attendees'])
        for aid in ('ada', 'ben'):
            self.assertFalse(state['agents'][aid]['busy'])
        # A second resolve of the same sprint is a no-op.
        sim._retro_step(state, 1000.0, 2_000_000, decider=_stub_retro())
        self.assertEqual(state['retrospectives']['spr-1']['start'], ['Ship smaller slices'])

    def test_retro_uses_own_director_then_borrows_when_no_sm(self):
        state = _seed()
        del state['teams'][1]['scrumMasterId']  # dev's crew has no real scrum master
        state['sprints']['spr-1'] = {
            'id': 'spr-1', 'name': 'Launch', 'goal': 'g', 'ownerId': 'faye',
            'createdAt': 900_000, 'targetDate': None, 'teamIds': ['dev'],
            'status': 'closed', 'closedAt': 1_000_000, 'items': [],
        }
        sim._on_sprint_closed(state, state['sprints']['spr-1'], 1_000_000)
        # Small team with a director-only SM: the team's OWN director (dev)
        # stands in as facilitator -- the retro is the team's own reflection,
        # run by someone the team works with, never an outsider by default.
        self.assertEqual(sim._retro_scrum_master(state, ['dev']), 'dev')
        sim._retro_step(state, 1000.0, 1_000_001, decider=_stub_retro())
        self.assertIn('spr-1', state['pendingRetrospectives'])
        pend = state['pendingRetrospectives']['spr-1']
        self.assertIn('dev', pend['people'])  # own director facilitates
        self.assertIn('ada', pend['people'])
        # When the OWN director is busy, a NON-BUSY director from ANOTHER team
        # (faye) is borrowed in, so the team still gets its retro.
        state1 = _seed()
        del state1['teams'][1]['scrumMasterId']
        state1['agents']['dev']['busy'] = True  # own director busy
        state1['sprints']['spr-1'] = {
            'id': 'spr-1', 'name': 'Launch', 'goal': 'g', 'ownerId': 'faye',
            'createdAt': 900_000, 'targetDate': None, 'teamIds': ['dev'],
            'status': 'closed', 'closedAt': 1_000_000, 'items': [],
        }
        sim._on_sprint_closed(state1, state1['sprints']['spr-1'], 1_000_000)
        self.assertEqual(sim._retro_scrum_master(state1, ['dev']), 'faye')
        # When BOTH the own director and every other free director are busy, the
        # retro waits rather than running without a facilitator.
        state2 = _seed()
        del state2['teams'][1]['scrumMasterId']
        state2['agents']['dev']['busy'] = True   # own director busy
        state2['agents']['faye']['busy'] = True  # the only other director busy
        state2['sprints']['spr-1'] = {
            'id': 'spr-1', 'name': 'Launch', 'goal': 'g', 'ownerId': 'faye',
            'createdAt': 900_000, 'targetDate': None, 'teamIds': ['dev'],
            'status': 'closed', 'closedAt': 1_000_000, 'items': [],
        }
        sim._on_sprint_closed(state2, state2['sprints']['spr-1'], 1_000_000)
        self.assertIsNone(sim._retro_scrum_master(state2, ['dev']))
        sim._retro_step(state2, 1000.0, 1_000_001, decider=_stub_retro())
        self.assertNotIn('spr-1', state2.get('pendingRetrospectives', {}))
        self.assertIn('spr-1', state2['pendingSprintRetros'])  # still queued

    def test_retro_resolve_carries_velocity_points_line(self):
        # A sprint closed WITH size estimates records velocity (pointsTotal), and
        # the resolved retrospective keeps it -- the retro's velocity line reads
        # "X of Y points shipped" instead of the plain item counts.
        state = _seed()
        state['sprints']['spr-1'] = {
            'id': 'spr-1', 'name': 'Launch', 'goal': 'Ship April', 'ownerId': 'faye',
            'createdAt': 900_000, 'targetDate': None, 'teamIds': ['dev'],
            'status': 'active', 'items': [('Land a launch page', 'pressoffice'),
                                          ('Still open', 'observatory')],
        }
        state['workQueue'] = [{'title': 'Still open', 'room': 'observatory',
                               'sprintId': 'spr-1'}]
        state['tasks']['t1'] = {'title': 'Land a launch page', 'room': 'pressoffice',
                                'status': 'done', 'sizeEstimate': 'L'}
        rec = sim.close_sprint(state, 'spr-1', now_ms=1_000_000)
        self.assertEqual(rec['velocity']['pointsTotal'], 3)  # L lands
        sim._on_sprint_closed(state, rec, 1_000_000)
        sim._retro_step(state, 1000.0, 1_000_001, decider=_stub_retro())
        sim._retro_step(state, 1000.0, 1_000_001 + sim.RETRO_MEET_MS + 1,
                        decider=_stub_retro())
        retro = state['retrospectives']['spr-1']
        self.assertEqual(retro['velocity']['landed'], 1)
        self.assertEqual(retro['velocity']['pointsLanded'], 3)
        self.assertEqual(retro['velocity']['pointsTotal'], 3)


class RefinementGate(unittest.TestCase):
    def test_team_in_active_sprint_skips_refinement_then_refines_at_close(self):
        state = _seed()
        state['sprints']['spr-1'] = {
            'id': 'spr-1', 'name': 'Active', 'goal': 'g', 'ownerId': 'faye',
            'createdAt': 800_000, 'targetDate': None, 'teamIds': ['dev'],
            'status': 'active', 'items': [('In flight', 'pressoffice')],
        }
        state['backlogRequests'] = [{
            'id': 'wrq-1', 'filedBy': 'ben', 'title': 'Groom me', 'room': 'pressoffice',
            'reason': 'A gap', 'filedAt': 1_000_000, 'status': 'pending', 'teamId': 'dev',
        }]
        # Cadence is due but the team is in an active sprint -> NO ceremony.
        sim._refinement_step(state, 1000.0, sim.REFINEMENT_CADENCE_MS + 1_000_000,
                             decider=lambda ins, crit: 'accept')
        self.assertNotIn('dev', state.get('pendingRefinements', {}))
        # The stamp is NOT advanced, so once the sprint closes the team is due
        # and the very next pass convenes the ceremony (refine at close).
        self.assertNotIn('dev', state.get('teamRefinementAt', {}))
        del state['sprints']['spr-1']
        sim._refinement_step(state, 1000.0, sim.REFINEMENT_CADENCE_MS + 1_000_001,
                             decider=lambda ins, crit: 'accept')
        self.assertIn('dev', state['pendingRefinements'])


class BreakdownCeremony(unittest.TestCase):
    """Sprint-staffing breakdown ceremony: a staffable large ask is filed as a
    pending breakdown request for a team, whose scrum master + workers card it
    into stories/spikes at the Command Center. Never predicts subtasks up front;
    on a model outage a single card titled the goal is queued so the ask is
    never dropped. The breakdown ceremony's requests are never groomed by the
    refinement ceremony (two ceremonies, two kinds)."""

    def _stub_breakdown(self):
        def decider(state, instructions, goal):
            return {'items': [
                {'title': 'Lay the foundation', 'type': 'story',
                 'acceptanceCriteria': 'Foundation is level'},
                {'title': 'Study the soil', 'type': 'spike'},
            ]}
        return decider

    def test_breakdown_convenes_and_queues_stories_from_decider(self):
        state = _seed()
        sim.file_large_request(state, 'faye', 'Build a new wing', 'dev', 1_000_000)
        # Pass 1: convene at the Command Center -- the scrum master (ada) +
        # the team's workers (ben); the director (dev) is excluded unless she
        # IS the effective scrum master.
        sim._breakdown_step(state, 1000.0, 1_000_001, decider=self._stub_breakdown())
        self.assertIn('dev', state['pendingBreakdowns'])
        pend = state['pendingBreakdowns']['dev']
        self.assertIn('ada', pend['people'])
        self.assertIn('ben', pend['people'])
        self.assertNotIn('dev', pend['people'])
        for aid in pend['people']:
            self.assertTrue(state['agents'][aid]['busy'])
            self.assertEqual(state['agents'][aid]['inRoom'], 'commandcenter')
        self.assertEqual(state['backlogRequests'][0]['status'], 'pending')
        # Pass 2: resolve -- the pieces are queued as real work for the team.
        sim._breakdown_step(state, 1000.0, 1_000_001 + sim.BREAKDOWN_MEET_MS + 1,
                            decider=self._stub_breakdown())
        self.assertNotIn('dev', state.get('pendingBreakdowns', {}))
        req = state['backlogRequests'][0]
        self.assertEqual(req['status'], 'accepted')
        self.assertEqual(req['brokenDown'], True)
        self.assertEqual(len(state['workQueue']), 2)
        story, spike = state['workQueue']
        self.assertEqual(story['title'], 'Lay the foundation')
        self.assertEqual(story['teamId'], 'dev')
        self.assertEqual(story['taskType'], 'code')
        self.assertIsNone(story['room'])  # room resolved at assignment
        self.assertIn('Acceptance criteria:\nFoundation is level', story['instructions'])
        self.assertEqual(spike['title'], 'Study the soil')
        self.assertEqual(spike['taskType'], 'spike')
        self.assertEqual(spike['teamId'], 'dev')
        # Attendees were restored to their prior (idle) state.
        for aid in ('ada', 'ben'):
            self.assertFalse(state['agents'][aid]['busy'])

    def test_breakdown_outage_queues_single_card_never_drops_ask(self):
        state = _seed()
        sim.file_large_request(state, 'faye', 'Build a new wing', 'dev', 1_000_000)

        def outage(state, instructions, goal):
            return None

        sim._breakdown_step(state, 1000.0, 1_000_001, decider=outage)
        sim._breakdown_step(state, 1000.0, 1_000_001 + sim.BREAKDOWN_MEET_MS + 1,
                            decider=outage)
        self.assertEqual(len(state['workQueue']), 1)
        card = state['workQueue'][0]
        self.assertEqual(card['title'], 'Build a new wing')
        self.assertEqual(card['taskType'], 'code')
        self.assertEqual(card['teamId'], 'dev')
        self.assertEqual(state['backlogRequests'][0]['status'], 'accepted')

    def test_refinement_skips_breakdown_requests(self):
        state = _seed()
        # A normal agent-filed request AND a large-request breakdown for dev.
        sim.file_work_request(state, 'ben', 'Groom me', 'pressoffice', reason='A gap')
        sim.file_large_request(state, 'faye', 'Build a new wing', 'dev', 1_000_000)
        # Refinement is due (cadence elapsed) and the team is not in a sprint.
        sim._refinement_step(state, 1000.0, sim.REFINEMENT_CADENCE_MS + 1_000_000,
                             decider=lambda ins, crit: 'accept')
        self.assertIn('dev', state.get('pendingRefinements', {}))
        req_ids = state['pendingRefinements']['dev']['reqIds']
        self.assertEqual(req_ids, ['wrq-1'])  # ONLY the normal request is groomed
        self.assertNotIn('wrq-2', req_ids)  # the breakdown waits for its own ceremony

    def test_breakdown_defers_team_in_active_sprint(self):
        state = _seed()
        state['sprints']['spr-1'] = {
            'id': 'spr-1', 'name': 'Active', 'goal': 'g', 'ownerId': 'faye',
            'createdAt': 800_000, 'targetDate': None, 'teamIds': ['dev'],
            'status': 'active', 'items': [('In flight', 'pressoffice')],
        }
        sim.file_large_request(state, 'faye', 'Build a new wing', 'dev', 1_000_000)
        sim._breakdown_step(state, 1000.0, 1_000_001,
                            decider=lambda s, i, g: {'items': []})
        # The team is committed to a sprint -> no ceremony; the ask stays queued.
        self.assertNotIn('dev', state.get('pendingBreakdowns', {}))
        self.assertEqual(state['backlogRequests'][0]['status'], 'pending')


class MergeAndWhitelist(unittest.TestCase):
    def test_merge_server_owned_carries_ws14_keys(self):
        existing = {
            'sim': {'owner': 'server'},
            'features': {'feat-1': {'id': 'feat-1', 'name': 'Summer Summit'}},
            'backlog': [],
            'retrospectives': {},
            'pendingRetrospectives': {},
            'pendingSprintRetros': ['spr-9'],
            'featureCounter': 2,
            'backlogCounter': 5,
        }
        incoming = {'sim': {'owner': 'server'}, 'agents': {}, 'workQueue': [], 'tasks': {}}
        merged = serve._merge_server_owned(existing, incoming)
        for k in ('features', 'backlog', 'retrospectives', 'pendingRetrospectives',
                  'pendingSprintRetros', 'featureCounter', 'backlogCounter'):
            self.assertIn(k, merged)
        self.assertEqual(merged['featureCounter'], 2)
        self.assertEqual(merged['backlogCounter'], 5)
        self.assertEqual(merged['pendingSprintRetros'], ['spr-9'])

    def test_queue_work_carries_feature_tags(self):
        state = {'workQueue': []}
        sim.queue_work(state, [{
            'title': 'Land a page', 'room': 'pressoffice',
            'featureId': 'feat-1', 'backlogItemId': 'bl-2', 'teamId': 'dev',
        }])
        item = state['workQueue'][0]
        self.assertEqual(item['featureId'], 'feat-1')
        self.assertEqual(item['backlogItemId'], 'bl-2')
        self.assertEqual(item['teamId'], 'dev')


class SizeEstimateCarry(unittest.TestCase):
    """The breakdown story's S/M/L size estimate survives every queue round
    trip onto the real work card and orders same-priority scheduling (an L
    story starts before an S story -- it needs more wall-time)."""

    def test_queue_work_normalizes_size_estimate(self):
        state = {'workQueue': []}
        sim.queue_work(state, [
            {'title': 'Big', 'sizeEstimate': 'L'},
            {'title': 'med', 'sizeEstimate': 'm'},
            {'title': 'tiny', 'sizeEstimate': '  s  '},
            {'title': 'junk', 'sizeEstimate': 'XL'},
            {'title': 'none', 'sizeEstimate': None},
        ])
        sizes = {i['title']: i.get('sizeEstimate') for i in state['workQueue']}
        self.assertEqual(sizes['Big'], 'L')
        self.assertEqual(sizes['med'], 'M')
        self.assertEqual(sizes['tiny'], 'S')
        self.assertIsNone(sizes['junk'], 'unknown size is not a scheduling signal')
        self.assertIsNone(sizes['none'])

    def test_breakdown_queues_stories_with_their_size(self):
        state = _seed()
        sim.file_large_request(state, 'faye', 'Build a new wing', 'dev', 1_000_000)

        def decider(state, instructions, goal):
            return {'items': [
                {'title': 'Lay the foundation', 'type': 'story',
                 'acceptanceCriteria': 'Foundation is level', 'sizeEstimate': 'L'},
                {'title': 'Study the soil', 'type': 'spike', 'sizeEstimate': 'S'},
            ]}

        sim._breakdown_step(state, 1000.0, 1_000_001, decider=decider)
        sim._breakdown_step(state, 1000.0, 1_000_001 + sim.BREAKDOWN_MEET_MS + 1,
                            decider=decider)
        story, spike = state['workQueue']
        self.assertEqual(story['sizeEstimate'], 'L')
        self.assertEqual(spike['sizeEstimate'], 'S')

    def test_shared_backlog_pull_keeps_size(self):
        state = _seed()
        sim.create_or_reuse_feature(state, 'Summer Summit', 1_000_000)
        sim.add_backlog_item(state, 'Land a launch page', 'feat-1',
                             'faye', 1_000_000, size_estimate='L')
        sim._pull_backlog_for_team(state, 'dev', 1_000_001)
        card = state['workQueue'][-1]
        self.assertEqual(card['sizeEstimate'], 'L',
                         'the shared-backlog pull keeps the estimate on the card')

    def test_pick_next_due_index_uses_size_as_equal_priority_tiebreak(self):
        now = 10 ** 12
        q = [
            {'title': 'small story', 'priority': sim.WORK_PRIORITY['normal'],
             'sizeEstimate': 'S'},
            {'title': 'big story', 'priority': sim.WORK_PRIORITY['normal'],
             'sizeEstimate': 'L'},
        ]
        idx = sim.pick_next_due_index(q, now, set())
        self.assertEqual(q[idx]['title'], 'big story',
                         'at equal priority the larger story starts first')
        # Priority always outranks size: a small URGENT story beats a big normal one.
        q2 = [
            {'title': 'big normal', 'priority': sim.WORK_PRIORITY['normal'],
             'sizeEstimate': 'L'},
            {'title': 'small urgent', 'priority': sim.WORK_PRIORITY['urgent'],
             'sizeEstimate': 'S'},
        ]
        idx2 = sim.pick_next_due_index(q2, now, set())
        self.assertEqual(q2[idx2]['title'], 'small urgent',
                         'urgency outranks size -- size is never an urgency override')
        # Unknown size sorts last among equal-priority work.
        q3 = [
            {'title': 'no size', 'priority': sim.WORK_PRIORITY['normal']},
            {'title': 'sized', 'priority': sim.WORK_PRIORITY['normal'],
             'sizeEstimate': 'M'},
        ]
        idx3 = sim.pick_next_due_index(q3, now, set())
        self.assertEqual(q3[idx3]['title'], 'sized')


if __name__ == '__main__':
    unittest.main()