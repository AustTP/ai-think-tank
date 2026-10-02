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

    def test_retro_defers_for_small_team_with_director_only_sm(self):
        state = _seed()
        del state['teams'][1]['scrumMasterId']  # dev's crew has no real scrum master
        state['sprints']['spr-1'] = {
            'id': 'spr-1', 'name': 'Launch', 'goal': 'g', 'ownerId': 'faye',
            'createdAt': 900_000, 'targetDate': None, 'teamIds': ['dev'],
            'status': 'closed', 'closedAt': 1_000_000, 'items': [],
        }
        sim._on_sprint_closed(state, state['sprints']['spr-1'], 1_000_000)
        # Small team: the only effective SM is the director (dev) -- the retro
        # must exclude the director, so it waits rather than running without one.
        self.assertIsNone(sim._retro_scrum_master(state, ['dev']))
        sim._retro_step(state, 1000.0, 1_000_001, decider=_stub_retro())
        self.assertNotIn('spr-1', state.get('pendingRetrospectives', {}))
        self.assertIn('spr-1', state['pendingSprintRetros'])  # still queued


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


if __name__ == '__main__':
    unittest.main()