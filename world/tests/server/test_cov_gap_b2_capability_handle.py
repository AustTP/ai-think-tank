"""Coverage-gap tests for the capability-handle / captcha / telegram / tier part.

Only new tests. Model calls, Telegram, browser sessions and PIL image handling
are replaced with in-memory fakes; nothing leaves the process.
"""
import asyncio
import base64
import io
import json
import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
from test_serve_helpers import setUpModule, tearDownModule  # noqa: E402,F401


class _Stop(Exception):
    pass


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def _png_b64(width=100, height=100):
    from PIL import Image
    out = io.BytesIO()
    Image.new('RGB', (width, height), 'white').save(out, 'PNG')
    return base64.b64encode(out.getvalue()).decode()


class CreditsWatchdogTests(unittest.TestCase):
    def test_bad_remaining_returns_none(self):
        self.assertIsNone(
            serve._credits_watchdog_step({'remaining': 'nope'}, 0, {}))

    def test_probe_loop_low_and_restored(self):
        async def _run_low():
            with mock.patch.object(serve, '_openrouter_account_credits',
                                   return_value={'remaining': 0}), \
                 mock.patch.object(serve, '_set_credits_pause') as pause, \
                 mock.patch.object(serve.asyncio, 'sleep', side_effect=_Stop):
                try:
                    await serve._openrouter_credits_probe_loop()
                except _Stop:
                    pass
            return pause

        state = serve._CREDITS_WATCHDOG_STATE
        try:
            serve._CREDITS_WATCHDOG_STATE = {'low': False}
            pause = asyncio.run(_run_low())
            self.assertTrue(pause.called)
            self.assertTrue(pause.call_args[0][0])

            serve._CREDITS_WATCHDOG_STATE = {'low': True}

            async def _run_restored():
                with mock.patch.object(serve, '_openrouter_account_credits',
                                       return_value={'remaining': 100}), \
                     mock.patch.object(serve, '_set_credits_pause') as restore, \
                     mock.patch.object(serve.asyncio, 'sleep',
                                       side_effect=_Stop):
                    try:
                        await serve._openrouter_credits_probe_loop()
                    except _Stop:
                        pass
                return restore

            restore = asyncio.run(_run_restored())
            self.assertTrue(restore.called)
            self.assertFalse(restore.call_args[0][0])
        finally:
            serve._CREDITS_WATCHDOG_STATE = state


class TelegramSendTests(unittest.TestCase):
    def test_send_photo_success_and_failure(self):
        with mock.patch.object(serve, 'telegram_push_allowed',
                               return_value=True), \
             mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'tok'), \
             mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', ['1']), \
             mock.patch.object(serve.urllib.request, 'urlopen',
                               return_value=_FakeResp({'ok': True})):
            self.assertEqual(1, serve._telegram_send_photo_sync(b'png', 'cap'))
        with mock.patch.object(serve, 'telegram_push_allowed',
                               return_value=True), \
             mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'tok'), \
             mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', ['1']), \
             mock.patch.object(serve.urllib.request, 'urlopen',
                               side_effect=RuntimeError('down')):
            self.assertEqual(0, serve._telegram_send_photo_sync(b'png', 'cap'))

    def test_send_photo_disabled_and_empty(self):
        with mock.patch.object(serve, 'telegram_push_allowed',
                               return_value=False):
            self.assertEqual(0, serve._telegram_send_photo_sync(b'png', 'cap'))
        with mock.patch.object(serve, 'telegram_push_allowed',
                               return_value=True), \
             mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', ''):
            self.assertEqual(0, serve._telegram_send_photo_sync(b'png', 'cap'))

    def test_send_text_success_conflict_and_disabled(self):
        with mock.patch.object(serve, 'telegram_push_allowed',
                               return_value=True), \
             mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'tok'), \
             mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', ['1', '2']), \
             mock.patch.object(serve, '_telegram_api_sync',
                               return_value={'ok': True}):
            self.assertEqual(2, serve._telegram_send_text_sync('hi'))
        with mock.patch.object(serve, 'telegram_push_allowed',
                               return_value=True), \
             mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'tok'), \
             mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', ['1', '2']), \
             mock.patch.object(serve, '_telegram_api_sync',
                               side_effect=serve._TelegramConflictError('409')):
            self.assertEqual(0, serve._telegram_send_text_sync('hi'))
        with mock.patch.object(serve, 'telegram_push_allowed',
                               return_value=False):
            self.assertEqual(0, serve._telegram_send_text_sync('hi'))
        with mock.patch.object(serve, 'telegram_push_allowed',
                               return_value=True), \
             mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'tok'), \
             mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', []):
            self.assertEqual(0, serve._telegram_send_text_sync('hi'))


class CaptchaVisionCandidateTests(unittest.TestCase):
    def _call(self, content=None, side_effect=None, slug='vision'):
        with mock.patch.object(serve, '_vision_tier_slug',
                               return_value=slug), \
             mock.patch.object(serve, '_call_openrouter_sync',
                               return_value={'choices': [{'message': {
                                   'content': content}}]},
                               side_effect=side_effect):
            return serve._captcha_vision_candidate('aW1n')

    def test_empty_and_missing_slug(self):
        self.assertIsNone(serve._captcha_vision_candidate(''))
        self.assertIsNone(self._call(content='{}', slug=None))

    def test_success(self):
        result = self._call(content='{"text":"AB","confidence":0.8}')
        self.assertEqual({'text': 'AB', 'confidence': 0.8}, result)

    def test_failure_paths(self):
        self.assertIsNone(self._call(side_effect=RuntimeError('down')))
        self.assertIsNone(self._call(content='no json here'))
        self.assertIsNone(self._call(content='{bad json}'))
        self.assertIsNone(self._call(content='{"text":"","confidence":1}'))
        result = self._call(content='{"text":"A","confidence":"bad"}')
        self.assertEqual(0.0, result['confidence'])


class CaptchaCropTests(unittest.TestCase):
    def test_missing_shot_and_bad_rects(self):
        self.assertEqual((None, None), serve._captcha_crop_meta('', []))
        shot = _png_b64()
        bad = [{'rect': {'x': 'x', 'y': 0, 'width': 1, 'height': 1}},
               {'rect': {'x': 0, 'y': 0, 'width': 0, 'height': 1}}]
        self.assertEqual((None, None), serve._captcha_crop_meta(shot, bad))

    def test_success_and_empty_crop(self):
        shot = _png_b64()
        cropped, box = serve._captcha_crop_meta(
            shot, [{'rect': {'x': 10, 'y': 10, 'width': 20, 'height': 20}}])
        self.assertTrue(cropped)
        self.assertEqual((0, 0, 54, 54), box)
        self.assertEqual((None, None), serve._captcha_crop_meta(
            shot, [{'rect': {'x': -100, 'y': -100,
                             'width': 10, 'height': 10}}]))

    def test_invalid_image_is_swallowed(self):
        self.assertEqual((None, None), serve._captcha_crop_meta(
            'not-base64', [{'rect': {'x': 0, 'y': 0,
                                     'width': 10, 'height': 10}}]))


class CaptchaAgenticPlanTests(unittest.TestCase):
    def _call(self, content=None, side_effect=None, slug='vision'):
        with mock.patch.object(serve, '_vision_tier_slug',
                               return_value=slug), \
             mock.patch.object(serve, '_call_openrouter_sync',
                               return_value={'choices': [{'message': {
                                   'content': content}}]},
                               side_effect=side_effect):
            return serve._captcha_agentic_plan('aW1n', 'image')

    def test_empty_and_missing_slug(self):
        self.assertIsNone(serve._captcha_agentic_plan(''))
        self.assertIsNone(self._call(content='{}', slug=None))

    def test_success_skips_bad_pairs(self):
        content = ('{"clicks":[[0.1,0.2],["x",0.2],[0.3],'
                   '["nan",0.5]],"text":"ok","submit":false,'
                   '"confidence":"bad"}')
        result = self._call(content=content)
        self.assertEqual([[0.1, 0.2], [0.0, 0.5]], result['clicks'])
        self.assertEqual('ok', result['text'])
        self.assertFalse(result['submit'])
        self.assertEqual(0.0, result['confidence'])

    def test_failure_paths(self):
        self.assertIsNone(self._call(side_effect=RuntimeError('down')))
        self.assertIsNone(self._call(content='no json'))
        self.assertIsNone(self._call(content='{bad json}'))


class CaptchaAgenticAttemptTests(unittest.TestCase):
    def _run(self, env, session=None, crop=(None, None), plan=None,
             run_result=({'captchaSolver': {'solved': True}}, None),
             captchas=None):
        class _Sess(dict):
            pass

        def _get(_agent):
            if isinstance(session, Exception):
                raise session
            return session

        async def _go():
            with mock.patch.object(serve, '_load_env', return_value=env), \
                 mock.patch.object(serve.browser_session, 'get',
                                   side_effect=_get), \
                 mock.patch.object(serve, '_captcha_crop_meta',
                                   return_value=crop), \
                 mock.patch.object(serve, '_captcha_agentic_plan',
                                   return_value=plan), \
                 mock.patch.object(serve, '_run_browser_act_sync',
                                   return_value=run_result), \
                 mock.patch.object(serve, '_sandbox_dir_for',
                                   return_value='/tmp'):
                return serve._captcha_agentic_attempt(
                    'a', 'sandbox-a', 'https://example.com',
                    captchas or [{'kind': 'image', 'inputSelector': '#in'}],
                    'shot')

        return asyncio.run(_go())

    def test_mode_off_and_session_failures(self):
        self.assertIsNone(self._run({'CAPTCHA_AUTO_SOLVE': 'off'}))
        err = serve.browser_session.BrowserSessionError('boom')
        self.assertFalse(self._run({'CAPTCHA_AUTO_SOLVE': 'agentic'},
                                   session=err)['attempted'])

    def test_parse_fallbacks_and_no_crop(self):
        result = self._run(
            {'CAPTCHA_AUTO_SOLVE': 'agentic',
             'CAPTCHA_AGENTIC_MAX_STEPS': 'bad',
             'CAPTCHA_AGENTIC_CONFIDENCE': 'bad'},
            session={'status': 'running', 'takeover': False})
        self.assertFalse(result['solved'])
        self.assertEqual('no croppable challenge rectangle',
                         result['reason'])

    def test_low_confidence_and_no_action(self):
        result = self._run(
            {'CAPTCHA_AUTO_SOLVE': 'agentic'},
            session={'status': 'running', 'takeover': False},
            crop=('crop', (0, 0, 10, 10)),
            plan={'clicks': [[0.5, 0.5]], 'text': '',
                  'submit': True, 'confidence': 0.1})
        self.assertEqual('vision confidence below floor', result['reason'])
        result = self._run(
            {'CAPTCHA_AUTO_SOLVE': 'agentic'},
            session={'status': 'running', 'takeover': False},
            crop=('crop', (0, 0, 10, 10)),
            plan={'clicks': [], 'text': '',
                  'submit': False, 'confidence': 0.9})
        self.assertEqual('vision produced no actionable step', result['reason'])

    def test_browser_failure_and_step_limit(self):
        result = self._run(
            {'CAPTCHA_AUTO_SOLVE': 'agentic',
             'CAPTCHA_AGENTIC_MAX_STEPS': '1'},
            session={'status': 'running', 'takeover': False},
            crop=('crop', (0, 0, 10, 10)),
            plan={'clicks': [[0.5, 0.5]], 'text': '',
                  'submit': True, 'confidence': 0.9},
            run_result=(None, 'driver down'))
        self.assertEqual('driver down', result['error'])
        result = self._run(
            {'CAPTCHA_AUTO_SOLVE': 'agentic',
             'CAPTCHA_AGENTIC_MAX_STEPS': '1'},
            session={'status': 'running', 'takeover': False},
            crop=('crop', (0, 0, 10, 10)),
            plan={'clicks': [[0.5, 0.5]], 'text': '',
                  'submit': True, 'confidence': 0.9},
            run_result=({'captchas': [{'kind': 'image'}],
                         'challenge': True}, None))
        self.assertEqual('agentic step limit reached', result['reason'])

    def test_solved_path_reports_clicks(self):
        result = self._run(
            {'CAPTCHA_AUTO_SOLVE': 'agentic'},
            session={'status': 'running', 'takeover': False},
            crop=('crop', (0, 0, 10, 10)),
            plan={'clicks': [[0.5, 0.5]], 'text': '',
                  'submit': True, 'confidence': 0.9})
        self.assertTrue(result['solved'])
        self.assertEqual(1, result['clicks'])


class HandleCaptchaDetectedTests(unittest.TestCase):
    def _run(self, captchas, shot='', env=None, candidate=None,
             agentic=None, photo=None, text=None, existing=None,
             needs_human=False):
        captcha_mod = serve.captcha
        request = existing or {'id': 'req-1', 'status': 'open'}
        created = {'id': 'req-1', 'status': 'open'}
        with mock.patch.object(captcha_mod, 'needs_human',
                               return_value=needs_human), \
             mock.patch.object(captcha_mod, 'policy_for_host',
                               return_value='attempt'), \
             mock.patch.object(captcha_mod, 'find_open',
                               return_value=existing), \
             mock.patch.object(captcha_mod, 'create_request',
                               return_value=created), \
             mock.patch.object(captcha_mod, 'set_answer'), \
             mock.patch.object(captcha_mod, 'mark_solved'), \
             mock.patch.object(captcha_mod, 'get', return_value=request), \
             mock.patch.object(serve, '_load_env', return_value=env or {}), \
             mock.patch.object(serve, '_captcha_crop_b64',
                               return_value=shot), \
             mock.patch.object(serve, '_captcha_vision_candidate',
                               return_value=candidate), \
             mock.patch.object(serve, '_captcha_agentic_attempt',
                               return_value=agentic), \
             mock.patch.object(serve, '_telegram_send_photo_sync',
                               side_effect=photo), \
             mock.patch.object(serve, '_telegram_send_text_sync',
                               side_effect=text):
            return serve._handle_captcha_detected(
                'a', 'sandbox-a', 'https://example.com', captchas, 'shot')

    def test_non_list_and_text_relay(self):
        result = self._run('bad', shot='', env={},
                           text=lambda *a: 1)
        self.assertFalse(result['interactive'])
        self.assertTrue(result['noticeSent'])

    def test_auto_candidate(self):
        result = self._run(
            [{'kind': 'image'}], shot='aW1n',
            env={'CAPTCHA_AUTO_ATTEMPT': '1',
                 'CAPTCHA_AUTO_CONFIDENCE': 'bad'},
            candidate={'text': 'AB', 'confidence': 0.99})
        self.assertTrue(result['autoAttempted'])
        self.assertEqual(0.99, result['autoConfidence'])

    def test_agentic_solved(self):
        result = self._run(
            [{'kind': 'checkbox'}], shot='aW1n',
            env={'CAPTCHA_AUTO_SOLVE': 'agentic'},
            agentic={'attempted': True, 'solved': True, 'steps': 1},
            needs_human=True)
        self.assertTrue(result['agenticSolved'])

    def test_photo_relay_failure_is_swallowed(self):
        result = self._run([], shot='aW1n', env={},
                           photo=RuntimeError('telegram down'))
        self.assertFalse(result['imageSent'])

    def test_existing_answered_request_skips_relay(self):
        result = self._run([], shot='', env={},
                           existing={'id': 'req-1', 'status': 'answered'})
        self.assertTrue(result['alreadyAnswered'])


class TelegramProcessUpdateTests(unittest.TestCase):
    def test_unknown_captcha_request_reply(self):
        async def _go():
            with mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', ['1']), \
                 mock.patch.object(serve.captcha, 'set_answer',
                                   return_value=None):
                return await serve._telegram_process_update({
                    'message': {'chat': {'id': 1},
                                'text': 'captcha cap_ab12 answer'}})
        reply = asyncio.run(_go())
        self.assertEqual(('1', 'CAPTCHA request cap_ab12 is unknown, '
                                'expired, or already solved.'), reply)


class TierFallbackTests(unittest.TestCase):
    def test_slugs_empty_and_db_error(self):
        self.assertEqual([], serve._tier_fallback_slugs(''))
        with mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('no db')):
            self.assertEqual([], serve._tier_fallback_slugs('model'))

    def test_try_fallbacks_skips_broken_circuit(self):
        calls = []

        def _call(slug):
            calls.append(slug)
            return 'served'

        with mock.patch.object(serve, '_tier_fallback_slugs',
                               return_value=['a', 'b']), \
             mock.patch.object(serve, 'is_model_circuit_broken',
                               side_effect=[True, False]), \
             mock.patch.object(serve, 'log_action') as log:
            result = serve._try_tier_fallbacks('m', 1, _call)
        self.assertEqual('served', result)
        self.assertEqual(['b'], calls)
        log.assert_called_once()


class _RowConn:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    def execute(self, *args, **kwargs):
        return self

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _RowDB:
    def __init__(self, row=None, rows=None):
        self._conn = _RowConn(row, rows)

    def __enter__(self):
        return self._conn

    def __exit__(self, *args):
        return False


class ResolveCapabilityHandleBranchTests(unittest.TestCase):
    def test_login_secret_payload(self):
        grant = {'credential_name': 'cred', 'purpose': 'test'}
        with mock.patch.object(serve, 'verify_capability_handle_scope',
                               return_value=grant), \
             mock.patch.object(serve, '_open_secret', return_value=json.dumps(
                 {'kind': 'login', 'username': 'u', 'password': 'p'})), \
             mock.patch.object(serve, '_db',
                               return_value=_RowDB(('svc', 'enc'))):
            result = serve.resolve_capability_handle(
                'a', 'h', 'POST', 'https://example.com/login')
        self.assertEqual('u', result['username'])
        self.assertEqual('p', result['password'])
        self.assertIsNone(result['secret'])

    def test_token_secret_payload(self):
        grant = {'credential_name': 'cred', 'purpose': 'test'}
        with mock.patch.object(serve, 'verify_capability_handle_scope',
                               return_value=grant), \
             mock.patch.object(serve, '_open_secret', return_value=json.dumps(
                 {'kind': 'token', 'token': 'tok'})), \
             mock.patch.object(serve, '_db',
                               return_value=_RowDB(('svc', 'enc'))):
            result = serve.resolve_capability_handle(
                'a', 'h', 'GET', 'https://example.com/x')
        self.assertEqual('tok', result['secret'])
        self.assertEqual('token', result['kind'])


class CaptchaCropRemainingTests(unittest.TestCase):
    def test_non_dict_rect_is_skipped_and_wrapper(self):
        shot = _png_b64()
        self.assertEqual(
            (None, None),
            serve._captcha_crop_meta(shot, [{'rect': 'not-a-dict'}]))
        cropped = serve._captcha_crop_b64(
            shot, [{'rect': {'x': 10, 'y': 10,
                              'width': 20, 'height': 20}}])
        self.assertTrue(cropped)


class TelegramProcessUpdateBranchTests(unittest.TestCase):
    def test_missing_chat_or_text_returns_none(self):
        self.assertIsNone(asyncio.run(
            serve._telegram_process_update({'message': {}})))
        self.assertIsNone(asyncio.run(
            serve._telegram_process_update(
                {'message': {'chat': {'id': 1}}})))

    def test_non_allowlisted_chat_is_ignored(self):
        with mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', ['1']):
            reply = asyncio.run(serve._telegram_process_update(
                {'message': {'chat': {'id': 2}, 'text': 'hello'}}))
        self.assertIsNone(reply)

    def test_captcha_answer_saved(self):
        with mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', ['1']), \
             mock.patch.object(serve.captcha, 'set_answer',
                               return_value={'id': 'cap_ab12'}):
            reply = asyncio.run(serve._telegram_process_update(
                {'message': {'chat': {'id': 1},
                             'text': 'captcha cap_ab12 answer'}}))
        self.assertEqual('1', reply[0])
        self.assertIn('CAPTCHA answer saved', reply[1])

    def test_state_unavailable_and_routed_reply(self):
        with mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', ['1']), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value=None):
            reply = asyncio.run(serve._telegram_process_update(
                {'message': {'chat': {'id': 1}, 'text': 'status'}}))
        self.assertEqual(('1', 'The think tank is not up right now.'), reply)

        with mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', ['1']), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={'agents': {}}), \
             mock.patch.object(serve, '_route_player_request',
                               mock.AsyncMock(return_value={
                                   'reply': 'routed'})):
            reply = asyncio.run(serve._telegram_process_update(
                {'message': {'chat': {'id': 1}, 'text': 'status'}}))
        self.assertEqual(('1', 'routed'), reply)

        with mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', ['1']), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={'agents': {}}), \
             mock.patch.object(serve, '_route_player_request',
                               mock.AsyncMock(return_value={})):
            reply = asyncio.run(serve._telegram_process_update(
                {'message': {'chat': {'id': 1}, 'text': 'status'}}))
        self.assertEqual(('1', "Didn't get a usable reply."), reply)


class TierFallbackRemainingTests(unittest.TestCase):
    def test_slug_rows_deduped_and_limited(self):
        rows = [('a',), ('a',), ('b',), ('',), ('c',), ('d',)]
        with mock.patch.object(serve, '_db',
                               return_value=_RowDB(rows=rows)):
            slugs = serve._tier_fallback_slugs('model')
        self.assertEqual(['a', 'b', 'c'], slugs)

    def test_try_fallbacks_skips_exception_then_exhausts(self):
        calls = []

        def _call(slug):
            calls.append(slug)
            if slug == 'a':
                raise RuntimeError('down')
            return 'served'

        with mock.patch.object(serve, '_tier_fallback_slugs',
                               return_value=['a', 'b']), \
             mock.patch.object(serve, 'is_model_circuit_broken',
                               return_value=False), \
             mock.patch.object(serve, 'log_action'):
            result = serve._try_tier_fallbacks('m', 1, _call)
        self.assertEqual('served', result)
        self.assertEqual(['a', 'b'], calls)

        def _always_fail(slug):
            raise RuntimeError('down')

        with mock.patch.object(serve, '_tier_fallback_slugs',
                               return_value=['a', 'b']), \
             mock.patch.object(serve, 'is_model_circuit_broken',
                               return_value=False), \
             mock.patch.object(serve, 'log_action'):
            self.assertIsNone(
                serve._try_tier_fallbacks('m', 1, _always_fail))


if __name__ == '__main__':
    unittest.main()
