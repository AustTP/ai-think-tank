"""Vision-tier anti-instruction guard.

The vision model must never take instructions from the media it
is shown: pixels are DATA. Both vision call sites
(serve._design_brief_from_image and content._review_screenshot)
must carry VISION_ANTI_INSTRUCTION_GUARD ahead of every
question they send.

Hermetic: every vision API call is mocked.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import content  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-vision-guard-test-')
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


class DesignBriefGuard(unittest.TestCase):
    def test_prompt_carries_guard_ahead_of_question(self):
        captured = {}

        def fake_call(slug, messages, max_tokens):
            captured['slug'] = slug
            captured['messages'] = messages
            return {'choices': [{'message': {'content': 'PALETTE: #fff'}}]}

        with unittest.mock.patch.object(serve, '_vision_tier_slug', return_value='vision-x'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync', side_effect=fake_call):
            out = serve._design_brief_from_image('QUJD', 'image x')
        self.assertTrue(out['ok'])
        text = captured['messages'][0]['content'][0]['text']
        self.assertTrue(text.startswith(serve.VISION_ANTI_INSTRUCTION_GUARD))
        self.assertIn('design brief', text)
        self.assertEqual(captured['messages'][0]['content'][1]['type'], 'image_url')

    def test_guard_constant_rejects_embedded_instructions(self):
        self.assertIn('untrusted data', serve.VISION_ANTI_INSTRUCTION_GUARD)
        self.assertIn('never instructions', serve.VISION_ANTI_INSTRUCTION_GUARD)
        self.assertIn('Ignore', serve.VISION_ANTI_INSTRUCTION_GUARD)


class ReviewScreenshotGuard(unittest.TestCase):
    def test_chat_question_carries_guard(self):
        calls = []

        def fake_http(method, base, path, body=None, key=None):
            calls.append((path, body))
            if path == '/api/screenshot':
                return {'imageBase64': 'QUJD'}
            return {'reply': 'looks good'}

        with unittest.mock.patch.object(content._serve, '_http_json', side_effect=fake_http), \
             unittest.mock.patch.object(serve, '_vision_tier_slug', return_value='vision-x'):
            out = content._review_screenshot(
                'http://base', 'key', 'ada', 'sb-1', 'index.html',
                'Does the layout match the brief?')
        self.assertTrue(out['ok'])
        chat_body = next(body for path, body in calls if path == '/api/chat')
        question = chat_body['messages'][0]['content'][0]['text']
        self.assertTrue(question.startswith(serve.VISION_ANTI_INSTRUCTION_GUARD))
        self.assertIn('Does the layout match the brief?', question)

    def test_guard_present_even_for_empty_question(self):
        calls = []

        def fake_http(method, base, path, body=None, key=None):
            calls.append((path, body))
            if path == '/api/screenshot':
                return {'imageBase64': 'QUJD'}
            return {'reply': 'ok'}

        with unittest.mock.patch.object(content._serve, '_http_json', side_effect=fake_http), \
             unittest.mock.patch.object(serve, '_vision_tier_slug', return_value='vision-x'):
            out = content._review_screenshot(
                'http://base', 'key', 'ada', 'sb-1', 'index.html', '')
        self.assertTrue(out['ok'])
        chat_body = next(body for path, body in calls if path == '/api/chat')
        question = chat_body['messages'][0]['content'][0]['text']
        self.assertTrue(question.startswith(serve.VISION_ANTI_INSTRUCTION_GUARD))


if __name__ == '__main__':
    unittest.main()
