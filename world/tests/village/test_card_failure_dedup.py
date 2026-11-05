"""The card-failure Telegram dedup must survive the re-card machine's
renames. Repair chains retitle the same failing card ('Fix: [SPIKE] X' after
'[SPIKE] X' after 'X'), and each rename used to mint a fresh dedup key --
the player got a failure notice per rename within the same 30-minute window
(task-12576 weatherstation chain, 2026-11-05)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import sim  # noqa: E402  (sim_parts are exec'd into one namespace)

_card_failure_dedup_title = sim._card_failure_dedup_title


class CardFailureDedupTitle(unittest.TestCase):
    def test_prefix_chains_collapse_to_one_identity(self):
        base = _card_failure_dedup_title(
            'Strip leaked weatherstation text from task-6320')
        variants = [
            '[SPIKE] Strip leaked weatherstation text from task-6320',
            'Fix: [SPIKE] Strip leaked weatherstation text from task-6320',
            'Fix: Fix: [BUG] Strip leaked weatherstation text from task-6320',
            'Re: Fix: strip leaked weatherstation text from task-6320',
            'Reopened: [REPORT] strip  leaked weatherstation text from task-6320',
        ]
        for v in variants:
            self.assertEqual(base, _card_failure_dedup_title(v), v)

    def test_distinct_cards_stay_distinct(self):
        a = _card_failure_dedup_title('Fix the login redirect')
        b = _card_failure_dedup_title('Fix the signup redirect')
        self.assertNotEqual(a, b)

    def test_display_title_is_not_mutated(self):
        # The function is key-only: callers keep showing the full title.
        raw = 'Fix: [SPIKE] Strip leaked weatherstation text'
        self.assertEqual(_card_failure_dedup_title(raw),
                         'strip leaked weatherstation text')


if __name__ == '__main__':
    unittest.main()
