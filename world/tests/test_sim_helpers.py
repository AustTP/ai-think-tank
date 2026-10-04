"""Pure helpers from sim_helpers.py -- priority normalization, sprint/product
id derivation, size estimates, due-ness, and the small lookup/scalar helpers.
The happy paths are exercised through sim.py's own integration tests; these
tests pin the edge cases (unknown priorities, malformed id keys, ValueErrors)
directly.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim_helpers  # noqa: E402


class NormalizePriority(unittest.TestCase):
    def test_known_strings_and_numbers_map_to_ints(self):
        self.assertEqual(sim_helpers.normalize_priority('urgent'), 3)
        self.assertEqual(sim_helpers.normalize_priority('High'), 2)
        self.assertEqual(sim_helpers.normalize_priority(1), 1)
        self.assertEqual(sim_helpers.normalize_priority(0.0), 0)

    def test_unknown_string_falls_back_to_normal(self):
        self.assertEqual(sim_helpers.normalize_priority('ASAP!!!'), 1)
        self.assertEqual(sim_helpers.normalize_priority('urgent '), 1)

    def test_unrecognized_numeric_values_fall_back_to_normal(self):
        self.assertEqual(sim_helpers.normalize_priority(99), 1)
        self.assertEqual(sim_helpers.normalize_priority(-1), 1)

    def test_none_and_other_types_fall_back_to_normal(self):
        self.assertEqual(sim_helpers.normalize_priority(None), 1)
        self.assertEqual(sim_helpers.normalize_priority(3.7), 1)


class NormalizeSizeEstimate(unittest.TestCase):
    def test_recognized_sizes_are_canonicalized(self):
        self.assertEqual(sim_helpers.normalize_size_estimate('l'), 'L')
        self.assertEqual(sim_helpers.normalize_size_estimate(' M '), 'M')
        self.assertEqual(sim_helpers.normalize_size_estimate('s'), 'S')

    def test_unknown_sizes_are_none(self):
        self.assertIsNone(sim_helpers.normalize_size_estimate('XL'))
        self.assertIsNone(sim_helpers.normalize_size_estimate(''))
        self.assertIsNone(sim_helpers.normalize_size_estimate(None))

    def test_size_estimate_weight(self):
        self.assertEqual(sim_helpers.size_estimate_weight('L'), 3)
        self.assertEqual(sim_helpers.size_estimate_weight('m'), 2)
        self.assertEqual(sim_helpers.size_estimate_weight('S'), 1)
        self.assertEqual(sim_helpers.size_estimate_weight('???'), 0)


class WorkItemDue(unittest.TestCase):
    def test_no_not_before_is_always_due(self):
        self.assertTrue(sim_helpers.is_work_item_due({}, 0))
        self.assertTrue(sim_helpers.is_work_item_due({'notBefore': 5}, 6))
        self.assertFalse(sim_helpers.is_work_item_due({'notBefore': 5}, 4))


class SprintProductIds(unittest.TestCase):
    def test_cold_state_starts_at_one(self):
        self.assertEqual(sim_helpers.next_sprint_id({}), 'spr-1')
        self.assertEqual(sim_helpers.next_product_id({}), 'prd-1')

    def test_hot_state_derives_next_above_existing(self):
        self.assertEqual(sim_helpers.next_sprint_id({'sprints': {'spr-1': {}, 'spr-5': {}}}), 'spr-6')
        self.assertEqual(sim_helpers.next_product_id({'products': {'prd-3': {}}}), 'prd-4')

    def test_ignores_non_matching_and_non_str_keys(self):
        state = {'sprints': {'spr-x': {}, 'spr-2': {}, 7: {}, 'not-a-sprint': {}}}
        self.assertEqual(sim_helpers.next_sprint_id(state), 'spr-3')
        pstate = {'products': {'prd-y': {}, 'prd-1': {}, None: {}}}
        self.assertEqual(sim_helpers.next_product_id(pstate), 'prd-2')

    def test_malformed_sprint_id_suffix_is_skipped(self):
        # 'spr-abc' passes the str + prefix check but blows up on int(); that
        # ValueError is swallowed and the id is just not counted.
        self.assertEqual(sim_helpers.next_sprint_id({'sprints': {'spr-abc': {}, 'spr-9': {}}}), 'spr-10')

    def test_malformed_product_id_suffix_is_skipped(self):
        self.assertEqual(sim_helpers.next_product_id({'products': {'prd-abc': {}, 'prd-9': {}}}), 'prd-10')


class ScalarHelpers(unittest.TestCase):
    def test_days_since(self):
        self.assertEqual(sim_helpers.days_since(None, 1000), 0)
        self.assertEqual(sim_helpers.days_since(0, 86400000), 0)  # 0 ts is "never"
        self.assertEqual(sim_helpers.days_since(1, 86400001), 1.0)
        self.assertEqual(sim_helpers.days_since(86400000, 0), 0.0)  # clamps to 0

    def test_deliverable_room(self):
        self.assertTrue(sim_helpers._deliverable_room('pressoffice'))
        self.assertTrue(sim_helpers._deliverable_room('observatory'))
        self.assertFalse(sim_helpers._deliverable_room('mailroom'))
        self.assertFalse(sim_helpers._deliverable_room(None))

    def test_team_row(self):
        state = {'teams': [{'id': 't1', 'name': 'One'}, {'id': 't2'}]}
        self.assertEqual(sim_helpers._team_row(state, 't1')['name'], 'One')
        self.assertIsNone(sim_helpers._team_row(state, 'nope'))
        self.assertIsNone(sim_helpers._team_row({}, 't1'))

    def test_ensure_wiki_creates_nested_pages(self):
        state = {}
        self.assertEqual(sim_helpers.ensure_wiki(state), {})
        sim_helpers.ensure_wiki(state)['Home'] = {'content': 'hi'}
        self.assertEqual(state['wiki']['pages']['Home']['content'], 'hi')

    def test_is_fully_idle(self):
        self.assertTrue(sim_helpers._is_fully_idle({}, False))
        for flag in ('busy', 'task', 'path', 'pathActive', 'pairWith', 'handoff', 'inRoom'):
            self.assertFalse(sim_helpers._is_fully_idle({flag: True}, False), flag)
        self.assertFalse(sim_helpers._is_fully_idle({}, True))


class SprintItemId(unittest.TestCase):
    def test_uses_title_and_room(self):
        self.assertEqual(sim_helpers._sprint_item_id({'title': 'x', 'room': 'r'}), ('x', 'r'))
        self.assertEqual(sim_helpers._sprint_item_id({'title': None, 'room': None}), ('', ''))
        self.assertEqual(sim_helpers._sprint_item_id({}), ('', ''))


if __name__ == '__main__':
    unittest.main()
