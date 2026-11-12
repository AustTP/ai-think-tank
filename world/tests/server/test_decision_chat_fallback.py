"""The decisions endpoint is experimental and has broken twice under the
tank: first the criteria shape, then (2026-11-12) it rejected EVERY model id
while the same slugs kept answering on /v1/chat/completions. These tests pin
the two fixes: criteria normalization keeps every call site's option list,
and the chat-completions fallback parses a reply or yields NO choice (fail
closed, never a fabricated option)."""
import json
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402


class NormalizeCriteria(unittest.TestCase):
    def test_both_item_shapes_and_bare_strings(self):
        q = serve._normalize_decision_questions(
            {'choice': {'type': 'choice', 'instructions': 'i',
                        'criteria': [{'name': 'approve', 'criteria': 'yes'},
                                     {'id': 'deny', 'description': 'no'},
                                     'plain']}})
        self.assertEqual(q['choice']['criteria'],
                         {'approve': 'yes', 'deny': 'no', 'plain': 'plain'})

    def test_record_passes_through(self):
        q = serve._normalize_decision_questions(
            {'choice': {'type': 'choice', 'instructions': 'i',
                        'criteria': {'a': 'b'}}})
        self.assertEqual(q['choice']['criteria'], {'a': 'b'})

    def test_private_keys_stripped(self):
        q = serve._normalize_decision_questions(
            {'choice': {'type': 'choice', 'instructions': 'i', 'criteria': {}},
             '_kind': 'cmd_allow'})
        self.assertNotIn('_kind', q)

    def test_empty_record_never_hides_real_options(self):
        # The 2026-11-12 root cause: {name, criteria} items produced an EMPTY
        # record, so every decision went out offering zero options.
        q = serve._normalize_decision_questions(
            {'choice': {'type': 'choice', 'instructions': 'i',
                        'criteria': [{'name': 'approve', 'criteria': 'y'}]}})
        self.assertEqual(q['choice']['criteria'], {'approve': 'y'})


class ChatFallbackParse(unittest.TestCase):
    QUESTIONS = {'choice': {'type': 'choice', 'instructions': 'i',
                            'criteria': [{'name': 'approve', 'criteria': 'y'},
                                         {'name': 'reject', 'criteria': 'n'}]}}

    def _fallback(self, text):
        data = {'choices': [{'message': {'content': text}}], 'usage': {'cost': 0.001}}
        with unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                        return_value=json.dumps(data)):
            return serve._decision_via_chat_completions(
                'm', {'messages': [], 'signals': {}}, self.QUESTIONS)

    def test_exact_name_parsed_with_confidence(self):
        out = self._fallback('{"choice": "approve", "confidence": 0.9}')
        self.assertEqual(out['answers']['choice']['choice'], 'approve')
        self.assertEqual(out['answers']['choice']['confidence'], 0.9)
        self.assertEqual(out['usage']['cost'], 0.001)

    def test_paraphrased_choice_is_dropped_not_fabricated(self):
        out = self._fallback('{"choice": "Yes", "confidence": 1.0}')
        self.assertEqual(out['answers']['choice'], {})

    def test_bare_option_name_reply_matches(self):
        out = self._fallback('approve')
        self.assertEqual(out['answers']['choice']['choice'], 'approve')

    def test_garbage_reply_is_empty(self):
        out = self._fallback('I cannot decide')
        self.assertEqual(out['answers']['choice'], {})

    def test_json_embedded_in_prose_is_found(self):
        out = self._fallback('My pick: {"choice": "reject", "confidence": 0.6} thanks')
        self.assertEqual(out['answers']['choice']['choice'], 'reject')


if __name__ == '__main__':
    unittest.main()
