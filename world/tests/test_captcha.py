"""CAPTCHA request relay, policy, and broker.

Hermetic: temp DB, fake browser run, no Telegram/network.
"""
import asyncio
import base64
import io
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import captcha  # noqa: E402
import serve  # noqa: E402


class _CaptchaCase(unittest.TestCase):
    def setUp(self):
        serve._rate_limit_calls.clear()
        self.tmp = tempfile.mkdtemp(prefix='think-tank-captcha-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
            _FERNET_EDEK_DIR=os.path.join(self.tmp, '.secret_keys'),
            _FERNET_EDEK_PATH=os.path.join(self.tmp, '.secret_keys', 'edek.key'),
        )
        self._cm.start()
        serve.init_db()
        self.key = serve.get_or_create_agent_key('ben')
        self.headers = {'X-Agent-Key': self.key}
        from starlette.testclient import TestClient
        self.client = TestClient(serve.app)

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _owner(self):
        return unittest.mock.patch.object(serve, '_operator_gate',
                                          return_value=(True, 'player'))

    def _request(self, **overrides):
        body = dict(
            agent_id='ben', sandbox_id='sandbox-test', host='example.com',
            page_url='https://example.com/login', kind='image',
            selector='#cap', input_selector='#cap-input',
            submit_selector='button[type=submit]', purpose='sign in')
        body.update(overrides)
        return captcha.create_request(**body)


class Lifecycle(_CaptchaCase):
    def test_answer_roundtrip_and_single_open(self):
        rec = self._request()
        self.assertEqual(rec['status'], 'pending')
        self.assertEqual(captcha.find_open('ben', 'sandbox-test',
                                           'https://example.com/login', 'image')['id'],
                         rec['id'])
        answered = captcha.set_answer(rec['id'], 'AB12', answered_by='telegram:1')
        self.assertEqual(answered['status'], 'answered')
        self.assertEqual(answered['answer'], 'AB12')
        self.assertEqual(captcha.wait_for_answer(rec['id'], wait_s=1)['status'],
                         'answered')

    def test_expired_answer_rejected(self):
        rec = self._request(ttl_s=60)
        with serve._db() as conn:
            conn.execute('UPDATE captcha_requests SET expires_at=? WHERE id=?',
                         (0, rec['id']))
        self.assertIsNone(captcha.set_answer(rec['id'], 'AB12'))

    def test_solved_request_drops_the_plaintext_answer(self):
        rec = self._request()
        captcha.set_answer(rec['id'], 'ZX9')
        captcha.mark_solved(rec['id'])
        self.assertIsNone(captcha.get(rec['id'])['answer'])

    def test_policy_default_and_set(self):
        self.assertEqual(captcha.policy_for_host('example.com'), 'relay')
        captcha.set_policy('example.com', 'attempt', updated_by='player')
        self.assertEqual(captcha.policy_for_host('example.com'), 'attempt')

    def test_interactive_never_attempts(self):
        self.assertTrue(captcha.needs_human('recaptcha'))
        self.assertTrue(captcha.needs_human('hcaptcha'))
        self.assertTrue(captcha.needs_human('turnstile'))
        self.assertFalse(captcha.needs_human('image'))


class TelegramReply(_CaptchaCase):
    def test_captcha_reply_is_stored_not_routed(self):
        rec = self._request()
        update = {'message': {'chat': {'id': 123}, 'text': f'captcha {rec["id"]} ZX9'}}
        with unittest.mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', {'123'}), \
             unittest.mock.patch.object(serve, '_route_player_request') as route:
            reply = asyncio.run(serve._telegram_process_update(update))
        route.assert_not_called()
        self.assertEqual(reply[0], '123')
        self.assertIn(rec['id'], reply[1])
        self.assertEqual(captcha.get(rec['id'])['answer'], 'ZX9')


class Broker(_CaptchaCase):
    def test_broker_fills_and_marks_solved(self):
        rec = self._request()
        captcha.set_answer(rec['id'], 'ZX9')
        parsed = {'ok': True, 'url': 'https://example.com/home', 'title': 'Home',
                  'text': '', 'fields': [], 'links': [], 'captchas': []}
        with unittest.mock.patch.object(serve, '_run_browser_act_sync',
                                        return_value=(parsed, None)) as run:
            r = self.client.post(f"/api/captcha/{rec['id']}/solve",
                                 json={'waitSec': 1}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['status'], 'solved')
        action = run.call_args.args[1]
        self.assertEqual(action['fields'][0]['value'], 'ZX9')
        self.assertEqual(captcha.get(rec['id'])['status'], 'solved')

    def test_broker_waits_then_times_out(self):
        rec = self._request()
        r = self.client.post(f"/api/captcha/{rec['id']}/solve",
                             json={'waitSec': 1}, headers=self.headers)
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json()['status'], 'waiting')


class Detection(_CaptchaCase):
    def _shot(self, width=320, height=240):
        buffer = io.BytesIO()
        Image.new('RGB', (width, height), 'white').save(buffer, 'PNG')
        return base64.b64encode(buffer.getvalue()).decode()

    def test_detection_creates_request_and_relays_only_the_challenge_crop(self):
        with unittest.mock.patch.object(serve, '_telegram_send_photo_sync',
                                        return_value=1) as send:
            info = serve._handle_captcha_detected(
                'ben', 'sandbox-test', 'https://example.com/login',
                [{'kind': 'image', 'selector': '#cap', 'inputSelector': '#cap-input',
                  'rect': {'x': 10, 'y': 10, 'width': 80, 'height': 30}}],
                self._shot(), purpose='sign in', submit_selector='button[type=submit]')
        self.assertTrue(info['imageSent'])
        self.assertTrue(info['imageCropped'])
        self.assertEqual(info['kind'], 'image')
        sent_png = Image.open(io.BytesIO(send.call_args.args[0]))
        self.assertLess(sent_png.width, 200)
        self.assertLess(sent_png.height, 120)
        self.assertEqual(captcha.get(info['requestId'])['status'], 'pending')

    def test_uncroppable_screenshot_is_withheld_but_the_owner_is_notified(self):
        with unittest.mock.patch.object(serve, '_telegram_send_photo_sync',
                                        return_value=1) as send, \
             unittest.mock.patch.object(serve, '_telegram_send_text_sync',
                                        return_value=1) as text_send:
            info = serve._handle_captcha_detected(
                'ben', 'sandbox-test', 'https://example.com/login',
                [{'kind': 'image', 'selector': '#cap', 'inputSelector': '#cap-input'}],
                self._shot(), purpose='sign in')
        self.assertFalse(info['imageSent'])
        self.assertFalse(info['imageCropped'])
        self.assertTrue(info['noticeSent'])
        send.assert_not_called()
        text_send.assert_called_once()
        self.assertIn('https://example.com/login', text_send.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
