"""On-demand photo review: path scoping, prompt modes, and fail-soft shape."""

import unittest
from unittest import mock

import serve


class PhotoReviewPathTest(unittest.TestCase):
    def test_default_and_general_share_the_root(self):
        self.assertEqual(serve._photo_review_rel(''), 'photo-reviews')
        self.assertEqual(serve._photo_review_rel('general'), 'photo-reviews')
        self.assertEqual(serve._photo_review_rel(None), 'photo-reviews')

    def test_project_is_sanitized_into_a_subdirectory(self):
        self.assertEqual(serve._photo_review_rel('Store Front'), 'photo-reviews/Store_Front')
        self.assertEqual(serve._photo_review_rel('../etc'), 'photo-reviews/etc')


class PhotoReviewPromptTest(unittest.TestCase):
    def setUp(self):
        self.result = {'choices': [{'message': {'content': 'SUBJECT: a cat\nTEXT: none'}}]}
        self.patches = [
            mock.patch.object(serve, '_vision_tier_slug', return_value='vision/test'),
            mock.patch.object(serve, '_call_openrouter_sync', return_value=self.result),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_general_prompt_is_grounded_and_sectioned(self):
        out = serve._photo_review_from_image('AAAA', 'photo 1', context='whiteboard')
        self.assertTrue(out['ok'])
        messages = serve._call_openrouter_sync.call_args[0][1]
        prompt = messages[0]['content'][0]['text']
        for needle in ('SUBJECT:', 'DETAILS:', 'TEXT:', 'IDEAS:',
                       'untrusted data', 'whiteboard'):
            self.assertIn(needle, prompt)
        self.assertTrue(messages[0]['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))

    def test_design_mode_delegates_to_the_existing_brief(self):
        with mock.patch.object(serve, '_design_brief_from_image',
                               return_value={'ok': True, 'brief': 'PALETTE: #fff'}) as brief:
            out = serve._photo_review_from_image('AAAA', 'photo 1', mode='design')
        brief.assert_called_once()
        self.assertEqual(out['brief'], 'PALETTE: #fff')

    def test_missing_vision_tier_fails_soft(self):
        with mock.patch.object(serve, '_vision_tier_slug', return_value=None):
            out = serve._photo_review_from_image('AAAA', 'photo 1')
        self.assertFalse(out['ok'])
        self.assertIn('vision model tier', out['note'])

    def test_empty_reply_fails_soft(self):
        with mock.patch.object(serve, '_call_openrouter_sync',
                               return_value={'choices': [{'message': {'content': ''}}]}):
            out = serve._photo_review_from_image('AAAA', 'photo 1')
        self.assertFalse(out['ok'])
        self.assertIn('no reply', out['note'])
