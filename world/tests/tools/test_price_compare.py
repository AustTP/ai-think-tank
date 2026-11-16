"""Vendor-id aliasing for price comparison: match by identity key, never
merge on ambiguity. The catalog never renames an id -- model_id is what
the provider's API expects -- so aliasing lives only in the comparison
layer, and only when it is UNIQUE in both directions."""
import importlib.util
import json
import os
import sys
import unittest

_TOOLS = os.path.join(os.path.dirname(__file__), '..', '..', 'tools')
_spec = importlib.util.spec_from_file_location(
    'price_compare', os.path.join(_TOOLS, 'price_compare.py'))
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)


class AliasKeys(unittest.TestCase):
    def test_vendor_prefix_and_variant_are_aliases(self):
        self.assertEqual(pc.alias_keys('openai/gpt-5.6-sol:beta'),
                         ['gpt-5.6-sol:beta', 'gpt-5.6-sol',
                          'openai/gpt-5.6-sol:beta'])
        self.assertEqual(pc.alias_keys('gpt-5.6-sol'), ['gpt-5.6-sol'])

    def test_unique_match_aliases_across_providers(self):
        right = {'openai/gpt-5.6-sol': {'blend': 10.0}}
        self.assertEqual(pc.alias_match('gpt-5.6-sol', right),
                         'openai/gpt-5.6-sol')

    def test_ambiguity_never_merges(self):
        right = {'openai/gpt-5.6-sol': {'blend': 10.0},
                 'straitly/gpt-5.6-sol': {'blend': 9.0}}
        self.assertIsNone(pc.alias_match('gpt-5.6-sol', right))

    def test_batch_tier_never_absorbs_the_standard_id(self):
        """'name:batch' is a different pricing tier: a no-variant CI id
        must not merge into it, and a variant id must not merge into the
        standard row."""
        right = {'anthropic/claude-opus-5': {'blend': 10.0},
                 'anthropic/claude-opus-5:batch': {'blend': 5.0}}
        self.assertEqual(pc.alias_match('claude-opus-5', right),
                         'anthropic/claude-opus-5')
        self.assertEqual(pc.alias_match('claude-opus-5:batch', right),
                         'anthropic/claude-opus-5:batch')

    def test_cross_compare_reports_and_skips_ambiguity(self):
        left = {'gpt-5.6-sol': {'in': 1.0, 'out': 2.0, 'blend': 1.5}}
        right = {'openai/gpt-5.6-sol': {'in': 4.0, 'out': 8.0, 'blend': 6.0},
                 'other/gpt-5.6-sol': {'in': 4.0, 'out': 8.0, 'blend': 6.0}}
        sig = pc.cross_compare(left, right, 'L', 'R')
        self.assertEqual(sig, [])

    def test_cross_compare_scores_a_unique_pair(self):
        left = {'gpt-5.6-sol': {'in': 1.0, 'out': 2.0, 'blend': 1.5}}
        right = {'openai/gpt-5.6-sol': {'in': 4.0, 'out': 8.0, 'blend': 6.0}}
        sig = pc.cross_compare(left, right, 'L', 'R')
        self.assertEqual(len(sig), 1)
        self.assertAlmostEqual(sig[0]['save_pct'], 0.75)


if __name__ == '__main__':
    unittest.main()
