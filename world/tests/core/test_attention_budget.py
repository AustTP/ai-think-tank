"""Hermetic tests for the human-attention budget (core/budget.py attention
section) and its consumers.

The pricing property under test: answering from existing evidence is always
cheaper than spending a colleague's time; a repeat of an already-asked
question costs more; an executive interruption is the most expensive ask;
and the cheapest_ask() selector keeps the planner honest about alternatives.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.budget as budget  # noqa: E402


class AttentionQuote(unittest.TestCase):
    def test_email_is_the_baseline_cost(self):
        quote = budget.attention_quote('Brian Barrett')
        self.assertEqual(quote['cost'], budget.ATTENTION_COSTS['email'])
        self.assertEqual(quote['reasons'], [])

    def test_meeting_channels_price_time(self):
        for channel, expected in (('15_minute_meeting', 3.0),
                                  ('30_minute_meeting', 5.0),
                                  ('executive_interruption', 10.0)):
            quote = budget.attention_quote('Elias', channel=channel)
            self.assertEqual(quote['cost'], expected)

    def test_repeat_question_penalized(self):
        quote = budget.attention_quote('Brian Barrett', asked_before=True)
        self.assertEqual(quote['cost'],
                         budget.ATTENTION_COSTS['email'] + budget.ATTENTION_REPEAT_PENALTY)
        self.assertIn('repeat question', quote['reasons'])

    def test_answerable_from_evidence_is_the_worst_ask(self):
        quote = budget.attention_quote('Caleb', answerable_from_evidence=True)
        self.assertEqual(quote['cost'],
                         budget.ATTENTION_COSTS['email'] + budget.ATTENTION_ANSWERED_PENALTY)
        self.assertIn('answerable from existing evidence', quote['reasons'])

    def test_unknown_channel_rejected(self):
        with self.assertRaises(ValueError):
            budget.attention_quote('Brian', channel='smoke_signal')

    def test_quote_carries_context(self):
        quote = budget.attention_quote('Brian', question_text='Who owns detections?')
        self.assertEqual(quote['question_text'], 'Who owns detections?')
        self.assertEqual(quote['person'], 'Brian')


class CheapestAsk(unittest.TestCase):
    def test_prefers_the_lowest_price(self):
        quotes = [
            budget.attention_quote('exec', channel='executive_interruption'),
            budget.attention_quote('brian', channel='email'),
            budget.attention_quote('caleb', channel='30_minute_meeting'),
        ]
        self.assertEqual(budget.cheapest_ask(quotes)['person'], 'brian')

    def test_repeat_question_loses_to_fresh_ask(self):
        quotes = [
            budget.attention_quote('brian', asked_before=True),
            budget.attention_quote('caleb', channel='email'),
        ]
        self.assertEqual(budget.cheapest_ask(quotes)['person'], 'caleb')

    def test_empty_list_is_none(self):
        self.assertIsNone(budget.cheapest_ask([]))
        self.assertIsNone(budget.cheapest_ask(None))

    def test_malformed_quotes_ignored(self):
        quotes = ['not a quote', {'cost': 5.0, 'person': 'ok'}]
        self.assertEqual(budget.cheapest_ask(quotes)['person'], 'ok')


if __name__ == '__main__':
    unittest.main()
