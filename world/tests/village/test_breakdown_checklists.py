"""Breakdown cards carry verifiable review checklists (2026-11-01).

The review-grade calibration loop (review_judge_calibration: the recorder,
the kappa report, the confidence-bar actuator) was fully built but starved:
every archived task carried an EMPTY checklist, so _grade_review_checklist
never ran and no anchored grade was ever recorded. The feed is the breakdown
ceremony: the scrum master now tags every card with a short checklist of
mechanically checkable ('code') and review-verifiable ('jev') requirements,
sanitized into the exact shape the review grader consumes.

Invariants pinned here:
- the breakdown prompt teaches the checklist contract;
- a card's checklist survives the item builder sanitized (capped at 4,
  types coerced to code/jev, lengths bounded, malformed entries dropped);
- a 'report' card loses its 'code' entries (the quality pipeline is skipped
  over prose -- a mechanical gate there would only surface UNSURE noise);
- auto-filed bug cards carry the standard pipeline + regression checklist.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import sim  # noqa: E402


def _state():
    return {
        'sim': {'owner': 'server'},
        'agents': {
            'w1': {'id': 'w1', 'name': 'W One', 'busy': False, 'offDuty': False,
                   'task': None, 'visible': True},
        },
        'agentRoster': [{'id': 'w1'}],
        'teams': [{'id': 'ops', 'name': 'Ops', 'directorId': 'ops',
                   'members': ['w1']}],
        'workQueue': [],
        'tasks': {},
        'backlogRequests': [],
        'pendingBreakdowns': {},
        'sprints': {},
    }


def _request(state, goal='Build the export feature'):
    req = {'id': 'wrq-1', 'status': 'pending', 'breakdown': True,
           'teamId': 'ops', 'filedBy': 'theo', 'goal': goal}
    state['backlogRequests'].append(req)
    return req


def _run_breakdown(state, items, instructions_log=None, now_ms=1000):
    req = _request(state)
    pending = {'teamId': 'ops', 'scrumMasterId': 'w1', 'reqIds': [req['id']],
               'people': {}}

    def decider(_state, instructions, _goal):
        if instructions_log is not None:
            instructions_log.append(instructions)
        return {'items': items}

    sim._resolve_breakdown(state, pending, now_ms, decider=decider)
    return state['workQueue']


class Sanitize(unittest.TestCase):
    def test_caps_at_four_entries(self):
        raw = [{'question': f'req {i}', 'type': 'jev'} for i in range(9)]
        out = sim._sanitize_checklist(raw, 'story')
        self.assertEqual(len(out), sim._CHECKLIST_MAX_ENTRIES)

    def test_unknown_type_coerced_to_jev(self):
        out = sim._sanitize_checklist([{'question': 'q', 'type': 'vibe'}], 'story')
        self.assertEqual(out, [{'section': '', 'question': 'q', 'type': 'jev'}])

    def test_malformed_entries_dropped(self):
        out = sim._sanitize_checklist(
            [None, 'nope', {'section': 'x'}, {'question': '  '}], 'story')
        self.assertEqual(out, [])

    def test_lengths_bounded(self):
        out = sim._sanitize_checklist(
            [{'section': 's' * 99, 'question': 'q' * 999, 'type': 'code'}], 'story')
        self.assertEqual(out[0]['section'], 's' * 40)
        self.assertEqual(out[0]['question'], 'q' * 200)

    def test_report_card_drops_code_entries(self):
        out = sim._sanitize_checklist(
            [{'question': 'pipeline green', 'type': 'code'},
             {'question': 'names the three risks', 'type': 'jev'}], 'report')
        self.assertEqual([e['type'] for e in out], ['jev'])

    def test_non_list_is_empty(self):
        self.assertEqual(sim._sanitize_checklist(None, 'story'), [])
        self.assertEqual(sim._sanitize_checklist('code it', 'story'), [])


class BreakdownEmitsChecklists(unittest.TestCase):
    def test_prompt_teaches_the_checklist_contract(self):
        state = _state()
        log = []
        _run_breakdown(state, [{'title': 'a', 'type': 'story'}],
                       instructions_log=log)
        prompt = log[0]
        self.assertIn('"checklist"', prompt)
        self.assertIn('"code"', prompt)
        self.assertIn('"jev"', prompt)

    def test_card_checklist_survives_sanitized(self):
        state = _state()
        queue = _run_breakdown(state, [{
            'title': 'Ship the export feature', 'type': 'story',
            'checklist': [
                {'section': 'code health', 'question': 'The quality pipeline '
                 'is green after the change.', 'type': 'code'},
                {'question': 'The export honors the date filter.',
                 'type': 'jev'},
                {'question': 42, 'type': 'jev'},
            ]}])
        self.assertEqual(len(queue), 1)
        cl = queue[0]['checklist']
        self.assertEqual(len(cl), 2)
        self.assertEqual(cl[0]['type'], 'code')
        self.assertEqual(cl[1]['question'], 'The export honors the date filter.')

    def test_report_card_code_gate_dropped_in_breakdown(self):
        state = _state()
        queue = _run_breakdown(state, [{
            'title': 'Write the market memo', 'type': 'report',
            'checklist': [{'question': 'pipeline green', 'type': 'code'},
                          {'question': 'names the moat', 'type': 'jev'}]}])
        self.assertEqual([e['type'] for e in queue[0]['checklist']], ['jev'])

    def test_missing_checklist_stays_empty_not_broken(self):
        state = _state()
        queue = _run_breakdown(state, [{'title': 'a', 'type': 'story'}])
        self.assertEqual(queue[0]['checklist'], [])


if __name__ == '__main__':
    unittest.main()
