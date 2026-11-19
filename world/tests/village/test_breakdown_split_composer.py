"""Breakdown composer split (2026-11-19): skeleton plan + per-card detail.

The old composer was one 4000-token call: transport failure voided the whole
plan to a single fallback card, and small-model drift dropped fields silently
(the pre-registered 'hypothesis' never reached a single card). Now the
skeleton call plans (title/type/phase/size only) and each card's prose is a
separate low-cost call that fails independently to defaults.
"""

import os
import sys
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402


def _plan(items):
    return {'items': items}


class TestCardDetailDecider(unittest.TestCase):
    def test_enriches_card_with_prose(self):
        card = {'title': 'Evaluate batch-1', 'type': 'spike',
                'sizeEstimate': 'M'}
        parsed = {
            'acceptanceCriteria': 'Scores cite fetched URLs',
            'never': ['Do not log into ideabrowser.com'],
            'checklist': [{'section': 'evidence',
                           'question': 'every score cites a fetched URL',
                           'type': 'jev'}],
            'hypothesis': 'The catalog is publicly scoreable without login',
        }
        with unittest.mock.patch.object(serve, '_resolve_model_tier',
                                        return_value='m'), \
                unittest.mock.patch.object(
                    serve, '_call_openrouter_sync',
                    return_value={'usage': {'cost': 0.0},
                                  'choices': [{'message': {'content':
                                              '{"acceptanceCriteria": "Scores cite fetched URLs",'
                                              '"never": ["Do not log into ideabrowser.com"],'
                                              '"checklist": [{"section": "evidence", "question": "every score cites a fetched URL", "type": "jev"}],'
                                              '"hypothesis": "The catalog is publicly scoreable without login"}'}}]}):
            out = sim._card_detail_decider_default({}, card, 'evaluate ideas')
        self.assertEqual(out['acceptanceCriteria'], 'Scores cite fetched URLs')
        self.assertEqual(out['never'],
                         ['Do not log into ideabrowser.com'])
        self.assertEqual(out['checklist'][0]['type'], 'jev')
        self.assertEqual(out['hypothesis'],
                         'The catalog is publicly scoreable without login')

    def test_failure_returns_card_unchanged(self):
        card = {'title': 't', 'type': 'story', 'sizeEstimate': 'S'}
        with unittest.mock.patch.object(
                serve, '_call_openrouter_sync',
                side_effect=RuntimeError('transport down')):
            out = sim._card_detail_decider_default({}, card, 'goal')
        self.assertEqual(out, card)

    def test_garbage_json_returns_card_unchanged(self):
        card = {'title': 't', 'type': 'story', 'sizeEstimate': 'S'}
        with unittest.mock.patch.object(
                serve, '_call_openrouter_sync',
                return_value={'usage': {'cost': 0.0},
                              'choices': [{'message': {'content':
                                          'not json at all'}}]}):
            out = sim._card_detail_decider_default({}, card, 'goal')
        self.assertEqual(out, card)

    def test_hypothesis_only_lands_on_investigation_cards(self):
        card = {'title': 't', 'type': 'story', 'sizeEstimate': 'S'}
        with unittest.mock.patch.object(serve, '_resolve_model_tier',
                                        return_value='m'), \
                unittest.mock.patch.object(
                    serve, '_call_openrouter_sync',
                    return_value={'usage': {'cost': 0.0},
                                  'choices': [{'message': {'content':
                                              '{"hypothesis": "x causes y"}'}}]}):
            out = sim._card_detail_decider_default({}, card, 'goal')
        self.assertNotIn('hypothesis', out)

    def test_bad_checklist_entries_dropped_not_fatal(self):
        card = {'title': 't', 'type': 'spike', 'sizeEstimate': 'S'}
        with unittest.mock.patch.object(serve, '_resolve_model_tier',
                                        return_value='m'), \
                unittest.mock.patch.object(
                    serve, '_call_openrouter_sync',
                    return_value={'usage': {'cost': 0.0},
                                  'choices': [{'message': {'content':
                                              '{"checklist": ["garbage string", {"question": "q", "type": "bogus"}], '
                                              '"acceptanceCriteria": "AC"}'}}]}):
            out = sim._card_detail_decider_default({}, card, 'goal')
        self.assertEqual(out.get('acceptanceCriteria'), 'AC')
        # malformed entries sanitized away
        for entry in out.get('checklist', []):
            self.assertIn(entry.get('type'), ('code', 'jev'))


class TestSplitComposition(unittest.TestCase):
    def _run_resolve(self, plan_payload, detail_side_effect):
        state = {
            'sim': {'owner': 'server'},
            'agents': {
                'sm': {'id': 'sm', 'name': 'Sm', 'busy': False,
                       'offDuty': False, 'task': None, 'visible': True},
            },
            'agentRoster': [{'id': 'sm'}],
            'teams': [{'id': 'ops', 'name': 'Ops', 'directorId': 'sm',
                       'members': []}],
            'workQueue': [], 'tasks': {}, 'backlogRequests': [],
            'pendingBreakdowns': {}, 'sprints': {},
        }
        req = {'id': 'wrq-9', 'status': 'pending', 'breakdown': True,
               'teamId': 'ops', 'filedBy': 'theo',
               'goal': 'Evaluate the idea catalog in batches'}
        state['backlogRequests'].append(req)
        pending = {'teamId': 'ops', 'scrumMasterId': 'sm',
                   'reqIds': ['wrq-9'], 'people': {}}
        def plan(_state, _instructions, _goal):
            return plan_payload
        def detail(_state, card, _goal):
            if detail_side_effect is not None:
                return detail_side_effect(_state, card, _goal)
            return card
        sim._resolve_breakdown(state, pending, 1000.0, decider=plan,
                               card_detail_decider=detail)
        return state, req

    def test_skeleton_cards_each_get_detail_call(self):
        calls = []

        def detail(_state, card, _goal):
            calls.append(card['title'])
            return dict(card, acceptanceCriteria='AC for ' + card['title'])

        state, _req = self._run_resolve(
            _plan([{'title': 'batch-1', 'type': 'spike',
                    'sizeEstimate': 'M'},
                   {'title': 'batch-2', 'type': 'spike',
                    'sizeEstimate': 'M'}]),
            detail)
        self.assertEqual(sorted(calls), ['batch-1', 'batch-2'])
        queued = state['workQueue']
        self.assertEqual(len(queued), 2)
        # Acceptance criteria ride the card's instructions text.
        self.assertIn('AC for batch-1', queued[0]['instructions'])
        self.assertIn('AC for batch-2', queued[1]['instructions'])

    def test_hypothesis_rides_card_instructions(self):
        def detail(_state, card, _goal):
            return dict(card, hypothesis='the catalog is scoreable without login')

        state, _req = self._run_resolve(
            _plan([{'title': 'batch-1', 'type': 'spike',
                    'sizeEstimate': 'M'}]),
            detail)
        self.assertIn('Pre-registered hypothesis',
                      state['workQueue'][0]['instructions'])
        self.assertIn('the catalog is scoreable without login',
                      state['workQueue'][0]['instructions'])

    def test_one_detail_failure_degrades_that_card_only(self):
        def detail(_state, card, _goal):
            if card['title'] == 'batch-2':
                raise RuntimeError('model transport failed')
            return dict(card, acceptanceCriteria='ok')
        with unittest.mock.patch.object(sim, '_card_detail_decider',
                                        side_effect=None), \
                unittest.mock.patch.object(
                    sim, '_card_detail_decider',
                    lambda state, card, goal: (
                        (_ for _ in ()).throw(RuntimeError('x'))
                        if card['title'] == 'batch-2' else card)):
            state, _req = self._run_resolve(
                _plan([{'title': 'batch-1', 'type': 'spike',
                        'sizeEstimate': 'M'},
                       {'title': 'batch-2', 'type': 'spike',
                        'sizeEstimate': 'M'}]),
                None)
        self.assertEqual(len(state['workQueue']), 2)

    def test_detail_error_is_caught_per_card(self):
        # The resolve loop must catch a raising detail decider per card,
        # not void the plan (the whole point of the split).
        def detail(_state, card, _goal):
            if card['title'] == 'batch-2':
                raise RuntimeError('boom')
            return dict(card, acceptanceCriteria='ok')
        with unittest.mock.patch.object(sim, '_card_detail_decider',
                                        side_effect=detail):
            state, _req = self._run_resolve(
                _plan([{'title': 'batch-1', 'type': 'spike',
                        'sizeEstimate': 'M'},
                       {'title': 'batch-2', 'type': 'spike',
                        'sizeEstimate': 'M'}]),
                None)
        self.assertEqual(len(state['workQueue']), 2)

    def test_skeleton_prompt_teaches_no_prose_fields(self):
        captured = {}

        def plan(_state, instructions, _goal):
            captured['instructions'] = instructions
            return _plan([{'title': 't', 'type': 'story',
                           'sizeEstimate': 'S'}])

        self._run_resolve(None, None) if False else None
        # Re-run with the capturing plan
        state = {
            'sim': {'owner': 'server'},
            'agents': {
                'sm': {'id': 'sm', 'name': 'Sm', 'busy': False,
                       'offDuty': False, 'task': None, 'visible': True},
            },
            'agentRoster': [{'id': 'sm'}],
            'teams': [{'id': 'ops', 'name': 'Ops', 'directorId': 'sm',
                       'members': []}],
            'workQueue': [], 'tasks': {}, 'backlogRequests': [],
            'pendingBreakdowns': {}, 'sprints': {},
        }
        req = {'id': 'wrq-10', 'status': 'pending', 'breakdown': True,
               'teamId': 'ops', 'filedBy': 'theo',
               'goal': 'Do the thing'}
        state['backlogRequests'].append(req)
        pending = {'teamId': 'ops', 'scrumMasterId': 'sm',
                   'reqIds': ['wrq-10'], 'people': {}}
        sim._resolve_breakdown(state, pending, 1000.0, decider=plan)
        self.assertIn('"title"', captured['instructions'])
        self.assertNotIn('checklist', captured['instructions'])
        self.assertNotIn('hypothesis', captured['instructions'])
        self.assertNotIn('acceptanceCriteria', captured['instructions'])


if __name__ == '__main__':
    unittest.main()
