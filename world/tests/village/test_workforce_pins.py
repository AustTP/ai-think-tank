"""Workforce pins: the player may state how many worker agents an ask needs,
which skills they should have, and how the work divides. Three contracts:
- file_large_request stores workers (int 2-200) and split (str/list) as
  playerConstraints; malformed pins are dropped, never block the filing.
- The breakdown ceremony: a workforce/split pin bypasses the
  single-deliverable shortcut (a workforce pin and a one-card plan
  contradict each other -- the 2026-11-18 ideabrowser live miss, where a
  thousands-of-ideas sweep collapsed to one report card because the goal
  text tripped the heuristic), injects the workforce/split/staffing text
  into the plan prompt, and logs the override.
- The director-split ceremony divides a pinned workforce EVENLY across the
  pieces it mints: each child request carries its share as its own workers
  pin (ceil, so an uneven org never under-delivers the total).
"""
import unittest
import unittest.mock

import sim


def _base_state():
    return {
        'agentRoster': [
            {'id': 'sm', 'name': 'Sam'},
            {'id': 'nora', 'name': 'Nora', 'isDirector': True},
            {'id': 'lead1', 'name': 'One Lead', 'director': 'nora'},
            {'id': 'lead2', 'name': 'Two Lead', 'director': 'nora'},
            {'id': 'w1', 'name': 'W One', 'director': 'lead1'},
            {'id': 'w2', 'name': 'W Two', 'director': 'lead2'},
        ],
        'teams': [
            {'id': 'nora', 'directorId': 'nora', 'scrumMasterId': 'sm'},
        ],
        'agents': {'sm': {'id': 'sm', 'name': 'Sam'},
                   'nora': {'id': 'nora', 'name': 'Nora'},
                   'lead1': {'id': 'lead1', 'name': 'One Lead'},
                   'lead2': {'id': 'lead2', 'name': 'Two Lead'}},
        'backlogRequests': [],
        'tasks': {},
        'workQueue': [],
    }


SINGLE_DELIVERABLE_GOAL = ('Research the idea catalog and write a final '
                           'recommendation report with scores.')


class FilingPins(unittest.TestCase):
    def test_workers_and_split_pins_survive_filing(self):
        state = _base_state()
        req = sim.file_large_request(
            state, 'admin', SINGLE_DELIVERABLE_GOAL, 'nora',
            constraints={'workers': 10, 'split': ['phase1: index all ideas',
                                                  'phase2: batches of 150'],
                         'roles': ['web research', 'analysis']})
        pc = req.get('playerConstraints') or {}
        self.assertEqual(pc.get('workers'), 10)
        self.assertEqual(pc.get('split'),
                         ['phase1: index all ideas', 'phase2: batches of 150'])
        self.assertEqual(pc.get('roles'), ['web research', 'analysis'])

    def test_split_as_single_string_normalizes_to_list(self):
        state = _base_state()
        req = sim.file_large_request(
            state, 'admin', 'Sweep the archive', 'nora',
            constraints={'split': 'batches of 150 ideas, one worker per batch'})
        self.assertEqual(req['playerConstraints']['split'],
                         ['batches of 150 ideas, one worker per batch'])

    def test_malformed_pins_are_dropped_not_fatal(self):
        state = _base_state()
        req = sim.file_large_request(
            state, 'admin', 'Sweep the archive', 'nora',
            constraints={'workers': 1, 'split': 42, 'budgetUsd': -5})
        self.assertIsNone(req.get('playerConstraints'),
                          'nothing survived -> no pin record at all')


class BreakdownWorkforcePin(unittest.TestCase):
    def _pending(self, state, rid, constraints):
        state['backlogRequests'].append({
            'id': rid, 'goal': SINGLE_DELIVERABLE_GOAL,
            'title': SINGLE_DELIVERABLE_GOAL, 'teamId': 'nora',
            'status': 'pending', 'filedBy': 'theo',
            'playerConstraints': constraints})
        return {'scrumMasterId': 'sm', 'teamId': 'nora',
                'reqIds': [rid], 'people': {}}

    def test_pin_bypasses_single_deliverable_and_injects_text(self):
        state = _base_state()
        pending = self._pending(state, 'r1', {'workers': 6,
                                              'split': ['batches of 150'],
                                              'roles': ['web research']})
        captured = {}

        def decider(_state, instructions, _goal):
            captured['instructions'] = instructions
            return {'items': [{'title': f'batch {i}', 'type': 'research'}
                              for i in range(6)]}

        with unittest.mock.patch('serve.log_action') as logged:
            sim._resolve_breakdown(state, pending, 2_000_000, decider=decider)
        self.assertEqual(len(state['workQueue']), 6,
                         'a workforce pin must never collapse to one card')
        self.assertIn('WORKFORCE', captured['instructions'])
        self.assertIn('at least 6', captured['instructions'])
        self.assertIn('batches of 150', captured['instructions'])
        self.assertIn('Staff the cards', captured['instructions'])
        overrides = [c for c in logged.call_args_list
                     if c.args and c.args[1] == 'breakdown_workers_pin_override']
        self.assertEqual(len(overrides), 1,
                         'the heuristic override is visible in governance')

    def test_no_pin_keeps_the_single_card_shortcut(self):
        state = _base_state()
        pending = self._pending(state, 'r2', {})

        def decider(*_a):
            raise AssertionError('no pin: the shortcut must decide alone')

        sim._resolve_breakdown(state, pending, 2_000_000, decider=decider)
        self.assertEqual(len(state['workQueue']), 1)


class SplitEvenShare(unittest.TestCase):
    def _org_state(self):
        state = _base_state()
        state['agentRoster'] += [
            {'id': 'lead1', 'name': 'One Lead', 'director': 'nora',
             'isDirector': True},
            {'id': 'lead2', 'name': 'Two Lead', 'director': 'nora',
             'isDirector': True},
        ]
        state['teams'] += [
            {'id': 'lead1', 'directorId': 'lead1', 'members': ['w1']},
            {'id': 'lead2', 'directorId': 'lead2', 'members': ['w2']},
        ]
        return state

    def _pending(self, state, rid, workers):
        state['backlogRequests'].append({
            'id': rid, 'goal': 'Sweep the idea catalog in batches',
            'title': 'Sweep the idea catalog', 'teamId': 'nora',
            'status': 'pending', 'filedBy': 'theo', 'awaitingSplit': True,
            'playerConstraints': {'workers': workers}})

    def _resolve(self, state, rid, pieces):
        pending = {'directorId': 'nora', 'reqIds': [rid], 'people': {}}
        decider = lambda *a: {'pieces': pieces}  # noqa: E731
        with unittest.mock.patch('serve.log_action'):
            sim._resolve_split(state, pending, 2_000_000, decider=decider)

    def test_ten_workers_over_two_pieces_share_five_each(self):
        state = self._org_state()
        self._pending(state, 'r1', 10)
        self._resolve(state, 'r1', [{'team': 'lead1', 'ask': 'batches 1-N'},
                                    {'team': 'lead2', 'ask': 'batches M-Z'}])
        reqs = {r['id']: r for r in state['backlogRequests']}
        children = [r for r in reqs.values() if r.get('splitFrom') == 'r1']
        self.assertEqual(len(children), 2)
        for child in children:
            self.assertEqual(child['playerConstraints'].get('workers'), 5,
                             'each subteam plans half the pinned workforce')

    def test_uneven_split_never_under_delivers(self):
        state = self._org_state()
        self._pending(state, 'r2', 7)
        self._resolve(state, 'r2', [{'team': 'lead1', 'ask': 'part A'},
                                    {'team': 'lead2', 'ask': 'part B'},
                                    {'team': 'lead2', 'ask': 'part C'}])
        children = [r for r in state['backlogRequests']
                    if r.get('splitFrom') == 'r2']
        shares = sorted(c['playerConstraints']['workers'] for c in children)
        self.assertEqual(shares, [3, 3, 3], 'ceil: 3x3 >= 7, never below')

    def test_no_workers_pin_keeps_parent_constraints_wholesale(self):
        state = self._org_state()
        state['backlogRequests'].append({
            'id': 'r3', 'goal': 'Sweep the idea catalog', 'title': 'sweep',
            'teamId': 'nora', 'status': 'pending', 'filedBy': 'theo',
            'awaitingSplit': True,
            'playerConstraints': {'roles': ['research']}})
        self._resolve(state, 'r3', [{'team': 'lead1', 'ask': 'part A'}])
        child = next(r for r in state['backlogRequests']
                     if r.get('splitFrom') == 'r3')
        self.assertNotIn('workers', child['playerConstraints'])
        self.assertEqual(child['playerConstraints'].get('roles'), ['research'])


if __name__ == '__main__':
    unittest.main()
