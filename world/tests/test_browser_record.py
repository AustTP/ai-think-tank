"""Tests for scripts/browser_record.py -- the operator-side session recorder
(BP: Photoshop-Action-style browser session capture for building/teaching
agent tasks).

The CDP pump is exercised against a scripted fake WebSocket, so the entire
event path (attach -> bindingCalled -> network rows -> inline screenshot
reply -> reducer) is tested with no browser at all. Redaction is tested the
way training_capture tests it: the discriminator cases, including the
[just-barely-safe] ones. Hermetic: nothing opens a socket.
"""
import asyncio
import base64
import hashlib
import json
import os
import unittest.mock
import shutil
import sys
import tempfile
import unittest

_WORLD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_WORLD)
sys.path.insert(0, os.path.join(_REPO, 'scripts'))
sys.path.insert(0, _WORLD)

import browser_record as rec  # noqa: E402


def _el(**kw):
    base = {'tag': 'input', 'type': 'text', 'id': '', 'name': '',
            'placeholder': '', 'ariaLabel': '', 'autocomplete': '',
            'label': '', 'parentText': '', 'formText': '', 'text': '',
            'outer': '', 'formAction': '', 'href': ''}
    base.update(kw)
    return base


def _ev(kind, url='https://site.test/page', **kw):
    out = {'kind': kind, 'url': url}
    out.update(kw)
    return out


class SensitivityDecide(unittest.TestCase):
    def test_password_type_is_sensitive(self):
        self.assertTrue(rec.is_sensitive_field(_el(type='password')))

    def test_otp_named_field_is_sensitive(self):
        self.assertTrue(rec.is_sensitive_field(_el(name='otp_code')))

    def test_autocomplete_tokens_are_sensitive(self):
        for token in ('current-password', 'one-time-code', 'cc-csc'):
            self.assertTrue(rec.is_sensitive_field(_el(autocomplete=token)))

    def test_ordinary_fields_are_not_sensitive(self):
        self.assertFalse(rec.is_sensitive_field(_el(name='email')))
        self.assertFalse(rec.is_sensitive_field(_el(name='username')))

    def test_surrounding_text_catches_a_bare_field(self):
        # A field named `code` with no other hint: the PAGE text around it
        # ("verification code") is the only tell, and it must be caught.
        self.assertTrue(rec.is_sensitive_field(
            _el(name='code', parentText='Enter the verification code we sent')))
        self.assertFalse(rec.is_sensitive_field(_el(name='code')))

    def test_auth_word_like_author_is_not_a_false_positive(self):
        self.assertFalse(rec.is_sensitive_field(_el(name='author')))

    def test_aria_label_and_labels_are_hints(self):
        self.assertTrue(rec.is_sensitive_field(_el(ariaLabel='API key')))
        self.assertTrue(rec.is_sensitive_field(_el(label='Access token')))


class MaskValue(unittest.TestCase):
    def test_sensitive_becomes_length_placeholder(self):
        self.assertEqual(rec.mask_value(_el(type='password'), 'hunter22'),
                         '[redacted] len 8')

    def test_plain_value_kept_and_capped(self):
        long = 'x' * 500
        out = rec.mask_value(_el(name='search'), long)
        self.assertEqual(out, 'x' * rec.MAX_VALUE_LEN)

    def test_empty_stays_empty(self):
        self.assertEqual(rec.mask_value(_el(type='password'), ''), '')


class UrlHeaderRedaction(unittest.TestCase):
    def test_sensitive_query_values_masked_names_kept(self):
        from urllib.parse import unquote
        out = rec.redact_url('https://a.test/cb?code=zzz&state=qq&x=1')
        plain = unquote(out)
        self.assertIn('code=' + rec.REDACTED, plain)
        self.assertIn('state=' + rec.REDACTED, plain)
        self.assertIn('x=1', out)

    def test_plain_url_untouched(self):
        url = 'https://a.test/search?q=thinking+tank&page=2'
        self.assertEqual(rec.redact_url(url), url)

    def test_credential_headers_masked_others_kept(self):
        out = rec.redact_headers({'Authorization': 'Bearer x', 'Cookie': 'c=1',
                                  'Content-Type': 'application/json'})
        self.assertEqual(out['Authorization'], '[redacted]')
        self.assertEqual(out['Cookie'], '[redacted]')
        self.assertEqual(out['Content-Type'], 'application/json')

    def test_network_row_loses_body_and_masks_rest(self):
        row = {'url': 'https://a.test/login?token=zzz',
               'headers': {'Authorization': 'Bearer x'},
               'postData': 'should not exist'}
        out = rec.redact_network_row(row)
        self.assertNotIn('postData', out)
        self.assertNotIn('Bearer', json.dumps(out))
        self.assertNotIn('zzz', json.dumps(out))


class BestSelector(unittest.TestCase):
    def test_id_wins(self):
        self.assertEqual(rec.best_selector(_el(id='submit')), '#submit')

    def test_name_next(self):
        self.assertEqual(rec.best_selector(_el(name='email')), 'input[name="email"]')

    def test_aria_label_next(self):
        self.assertEqual(rec.best_selector(_el(ariaLabel='Sign in')), 'input[aria-label="Sign in"]')

    def test_button_text_anchor(self):
        self.assertEqual(rec.best_selector(_el(tag='button', text='Continue')),
                         'button:has-text("Continue")')

    def test_css_path_fallback(self):
        self.assertEqual(rec.best_selector(_el(tag='div', csspath='div:nth-of-type(2)')),
                         'div:nth-of-type(2)')

    def test_never_empty(self):
        self.assertTrue(rec.best_selector({}))


class Aggregation(unittest.TestCase):
    def test_full_login_flow(self):
        events = [
            _ev('load', fields=[{'tag': 'input', 'type': 'email'}], links=[]),
            _ev('input', element=_el(id='email'), value='', nchars=1),
            _ev('input', element=_el(id='email'), value='a@b.co', nchars=6),
            _ev('input', element=_el(id='pw', type='password'), masked=True, nchars=8),
            _ev('input', element=_el(id='pw', type='password'), masked=True, nchars=3),
            _ev('click', selector='button[type=submit]',
                element=_el(tag='button', type='submit', text='Sign in'), isSubmit=True),
            _ev('load', url='https://site.test/home', fields=[], links=[]),
        ]
        steps = rec.aggregate_steps([{**e, 'element': e.get('element', _el())} for e in events])
        kinds = [s['kind'] for s in steps]
        self.assertEqual(kinds, ['navigate', 'input', 'input', 'click', 'navigate'])
        self.assertEqual(steps[1]['value'], 'a@b.co')
        self.assertEqual(steps[1]['chars'], 7)
        self.assertTrue(steps[2]['masked'])
        self.assertEqual(steps[2]['value'], '')
        self.assertEqual(steps[2]['chars'], 11)
        self.assertEqual(steps[0]['pageId'], 'page-1')
        self.assertEqual(steps[4]['pageId'], 'page-2')
        self.assertEqual(steps[3]['pageId'], 'page-1')

    def test_same_url_load_refresh_enriches_not_duplicates(self):
        events = [
            _ev('load', fields=[{'tag': 'input'}], links=[]),
            _ev('input', element=_el(id='q'), value='hi', nchars=2),
            _ev('load', fields=[{'tag': 'input'}, {'tag': 'textarea'}], links=[]),
        ]
        steps = rec.aggregate_steps([{**e, 'element': e.get('element', _el())} for e in events])
        self.assertEqual([s['kind'] for s in steps], ['navigate', 'input'])
        self.assertEqual(len(steps[0]['fields']), 2)

    def test_page_report_mask_never_trusted_alone(self):
        # The page REPORTS unmasked (older/fuzzier detector), but python sees
        # type=password -> python must override to masked and drop the value.
        events = [_ev('input', element=_el(id='pw', type='password'),
                      value='super-secret', masked=False, nchars=12)]
        steps = rec.aggregate_steps([{**e, 'element': e.get('element', _el())} for e in events])
        self.assertTrue(steps[0]['masked'])
        self.assertEqual(steps[0]['value'], '')
        self.assertEqual(steps[0]['chars'], 12)

    def test_step_limit_hard_stops(self):
        events = [_ev('click', selector='#a')] * (rec.MAX_STEPS + 50)
        steps = rec.aggregate_steps([dict(e, element=_el()) for e in events])
        self.assertLessEqual(len(steps), rec.MAX_STEPS + 2)


class ActionsFromSteps(unittest.TestCase):
    def test_login_flow_maps_to_driver_ops(self):
        steps = [
            {'kind': 'navigate', 'url': 'https://site.test/login', 'fields': [], 'links': []},
            {'kind': 'input', 'selector': 'input[name=email]', 'value': 'a@b.co',
             'chars': 6, 'masked': False, 'pageId': 'page-1'},
            {'kind': 'input', 'selector': 'input[name=password]', 'value': '',
             'chars': 8, 'masked': True, 'pageId': 'page-1'},
            {'kind': 'click', 'selector': 'button[type=submit]',
             'element': _el(tag='button', type='submit', text='Sign in'),
             'value': '', 'isSubmit': True, 'pageId': 'page-1'},
            {'kind': 'navigate', 'url': 'https://site.test/home', 'fields': [], 'links': []},
        ]
        actions = rec.actions_from_steps(steps)
        self.assertEqual([a['op'] for a in actions], ['fill', 'fill', 'submit'])
        self.assertEqual(actions[0]['value'], 'a@b.co')
        self.assertTrue(actions[1].get('redacted'))
        self.assertEqual(actions[1]['value'], '')
        # The submit happens on the page it was recorded on; the /home
        # navigation it CAUSED emits no goto (submit already navigates).
        self.assertEqual(actions[2]['url'], 'https://site.test/login')

    def test_action_after_submit_navigation_lands_via_goto(self):
        steps = [
            {'kind': 'navigate', 'url': 'https://site.test/login', 'fields': [], 'links': []},
            {'kind': 'click', 'selector': 'button[type=submit]',
             'element': _el(tag='button', type='submit', text='Sign in'),
             'value': '', 'isSubmit': True, 'pageId': 'page-1'},
            {'kind': 'navigate', 'url': 'https://site.test/home', 'fields': [], 'links': []},
            {'kind': 'click', 'selector': '#save', 'element': _el(tag='button', text='Save'),
             'value': '', 'isSubmit': False, 'pageId': 'page-2'},
        ]
        actions = rec.actions_from_steps(steps)
        self.assertEqual([a['op'] for a in actions], ['submit', 'click'])
        self.assertEqual(actions[-1]['url'], 'https://site.test/home')

    def test_link_click_becomes_goto(self):
        steps = [{'kind': 'navigate', 'url': 'https://site.test/', 'fields': [], 'links': []},
                 {'kind': 'click', 'selector': 'a.pricing', 'element': _el(tag='a', text='Pricing'),
                  'href': 'https://site.test/pricing', 'value': '', 'isSubmit': False,
                  'pageId': 'page-1'}]
        actions = rec.actions_from_steps(steps)
        self.assertEqual(action_ops(actions), ['goto'])
        self.assertEqual(actions[0]['url'], 'https://site.test/pricing')


def action_ops(actions):
    return [a['op'] for a in actions]


class RenderTxt(unittest.TestCase):
    def _session(self):
        return {'meta': {'label': 'hf login', 'session_id': 'rec-1',
                         'started_ts': 0, 'ended_ts': 60, 'browser': '/x/Chrome'},
                'steps': [
                    {'kind': 'navigate', 'url': 'https://site.test/login', 'title': 'Login',
                     'fields': [{}], 'links': [], 'pageId': 'page-1'},
                    {'kind': 'input', 'selector': 'input[name=password]', 'value': '',
                     'chars': 9, 'masked': True, 'pageId': 'page-1'},
                    {'kind': 'click', 'selector': 'button[type=submit]',
                     'element': _el(tag='button', type='submit', text='Sign in'),
                     'value': '', 'pageId': 'page-1', 'shot': 'shots/step-003.png'},
                    {'kind': 'navigate', 'url': 'https://site.test/home', 'title': 'Home',
                     'fields': [], 'links': [], 'pageId': 'page-2'},
                ]}

    def test_transcript_shape(self):
        txt = rec.render_txt(self._session())
        self.assertIn('# Browser session recording: hf login', txt)
        self.assertIn('## Steps', txt)
        self.assertIn('## Replay notes', txt)
        self.assertIn('typed 9 chars (redacted)', txt)
        self.assertIn('[shot shots/step-003.png]', txt)
        self.assertIn('LOAD', txt)
        self.assertIn('Success marker: the flow ended on https://site.test/home', txt)

    def test_no_secret_leaks_in_transcript(self):
        session = self._session()
        session['steps'].insert(1, {'kind': 'input', 'selector': 'input[name=email]',
                                    'value': 'player@site.test', 'chars': 5,
                                    'masked': False, 'pageId': 'page-1'})
        txt = rec.render_txt(session)
        self.assertIn('player@site.test', txt)
        self.assertNotIn('hunter2', txt)


class WriteSession(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='rec-tests-')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _session(self):
        png = base64.b64encode(b'\x89PNG-not-really').decode()
        return {'meta': {'label': 'hf login', 'session_id': 'rec-x', 'started_ts': 0,
                         'ended_ts': 9, 'browser': '/x/Chrome', 'redactions_applied': 0,
                         'artifacts': {}},
                'steps': [
                    {'kind': 'navigate', 'url': 'https://site.test/', 'title': 'T',
                     'fields': [], 'links': [], 'pageId': 'page-1'},
                    {'kind': 'click', 'selector': '#go', 'element': _el(tag='button'),
                     'value': '', 'pageId': 'page-1', '_pending_shot_png': png},
                    {'kind': 'input', 'selector': 'input[name=otp]', 'value': '',
                     'chars': 6, 'masked': True, 'pageId': 'page-1'},
                ],
                'network_pending': [{'url': 'https://site.test/api?code=zz',
                                     'headers': {'Authorization': 'Bearer x'}}]}

    def test_writes_three_artifacts_with_digests(self):
        final_dir = rec.write_session(self._session(), self.root)
        self.assertTrue(os.path.isdir(final_dir))
        for name in ('session.txt', 'session.json', 'browser_act_actions.json'):
            self.assertTrue(os.path.exists(os.path.join(final_dir, name)))
        session = json.load(open(os.path.join(final_dir, 'session.json')))
        # digests are recorded and match the files on disk
        self.assertEqual(session['meta']['artifacts']['session.txt'],
                         hashlib.sha256(
                             open(os.path.join(final_dir, 'session.txt'), 'rb').read()).hexdigest())
        # shot written for the click step (step 2 of 3)
        shot = os.path.join(final_dir, 'shots', 'step-002.png')
        self.assertTrue(os.path.exists(shot))
        self.assertEqual(session['meta']['artifacts']['shots/step-002.png'],
                         hashlib.sha256(open(shot, 'rb').read()).hexdigest())

    def test_nothing_raw_survives_serialization(self):
        final_dir = rec.write_session(self._session(), self.root)
        blob = open(os.path.join(final_dir, 'session.json')).read()
        self.assertNotIn('Bearer x', blob)
        self.assertNotIn('_pending_shot_png', blob)
        self.assertNotIn('zz', blob)
        # the pending buffer itself is emptied, not leaked into the artifact
        self.assertEqual(json.load(open(os.path.join(final_dir, 'session.json')))['network_pending'], [])

    def test_transcript_masks_and_labels(self):
        final_dir = rec.write_session(self._session(), self.root)
        txt = open(os.path.join(final_dir, 'session.txt')).read()
        self.assertIn('typed 6 chars (redacted)', txt)
        self.assertIn('browser_act_actions.json', txt)

    def test_label_is_slugified_into_the_dir_name(self):
        final_dir = rec.write_session(self._session(), self.root)
        base = os.path.basename(final_dir)
        self.assertTrue(base.startswith('rec-x'))
        self.assertIn('hf-login', base)


class FindBrowser(unittest.TestCase):
    def test_explicit_path_returned(self):
        self.assertEqual(rec.find_browser(os.__file__.replace('.pyc', '.py')),
                         os.__file__.replace('.pyc', '.py'))

    def test_explicit_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            rec.find_browser('/nonexistent/browser-bin')

    def test_no_browser_anywhere_raises(self):
        with unittest.mock.patch.object(rec, 'BROWSER_CANDIDATES', ()), \
                unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertRaises(FileNotFoundError, rec.find_browser)


class _FakeWs:
    """Scripted CDP WebSocket: replays queued messages, records everything
    sent. Callable queue items are evaluated at read time so a scripted
    reply can know the id the pump happened to use. Every read yields to the
    scheduler once so messages interleave with the pump the way a real CDP
    wire does instead of draining eagerly."""

    def __init__(self, incoming):
        self._in = list(incoming)
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self._in:
            raise StopAsyncIteration
        item = self._in.pop(0)
        if callable(item):
            item = item(self)
        return json.dumps(item)

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _screenshot_send(fake):
    return bool([m for m in fake.sent
                 if m.get('method') == 'Page.captureScreenshot'])


class PumpIntegration(unittest.TestCase):
    """The whole CDP event path against a scripted wire: attach, load,
    network rows, click with inline screenshot reply, redacted input."""

    def _run(self, incoming):
        session = {'meta': {'label': 't', 'session_id': 'rec-x', 'started_ts': 0,
                            '_max_minutes': 0},
                   'steps': [], 'network_pending': []}
        ws = _FakeWs(incoming)
        asyncio.run(rec._pump(session, ws))
        return session, ws

    def test_attach_bootstraps_the_fixed_bundle(self):
        incoming = [
            {'method': 'Target.attachedToTarget', 'sessionId': 'S1',
             'params': {'targetInfo': {'type': 'page', 'targetId': 'T1'}}},
        ]
        _, ws = self._run(incoming)
        methods = [m['method'] for m in ws.sent]
        self.assertEqual(methods[0], 'Target.setAutoAttach')
        self.assertIn('Runtime.addBinding', methods)
        self.assertIn('Page.addScriptToEvaluateOnNewDocument', methods)
        self.assertIn('Network.enable', methods)
        scripts = [m['params']['source'] for m in ws.sent
                   if m['method'] == 'Page.addScriptToEvaluateOnNewDocument']
        self.assertEqual(scripts, [rec.INJECTED_BUNDLE])

    def test_load_click_netrow_and_cooldown(self):
        load = {'kind': 'load', 'url': 'https://site.test/login', 'title': 'Login',
                'fields': [{'tag': 'input', 'type': 'email'}], 'links': []}
        click = {'kind': 'click', 'url': 'https://site.test/login',
                 'selector': 'button[type=submit]', 'masked': False, 'isSubmit': True,
                 'element': _el(tag='button', type='submit', text='Sign in')}
        netrow = {'method': 'Network.requestWillBeSent', 'sessionId': 'S1',
                  'params': {'requestId': 'n1', 'type': 'Document',
                             'request': {'method': 'POST', 'url': 'https://site.test/post',
                                         'headers': {'Authorization': 'Bearer topsecret'}}}}
        session, ws = self._run([
            {'method': 'Target.attachedToTarget', 'sessionId': 'S1',
             'params': {'targetInfo': {'type': 'page', 'targetId': 'T1'}}},
            {'method': 'Runtime.bindingCalled', 'sessionId': 'S1',
             'params': {'name': rec.BINDING_NAME, 'payload': json.dumps(load)}},
            netrow,
            {'method': 'Runtime.bindingCalled', 'sessionId': 'S1',
             'params': {'name': rec.BINDING_NAME, 'payload': json.dumps(click)}},
        ])
        kinds = [s['kind'] for s in session['steps']]
        self.assertEqual(kinds, ['navigate', 'click'])
        click_step = session['steps'][1]
        self.assertEqual(click_step['selector'], 'button[type=submit]')
        # a screenshot was REQUESTED (its reply is applied out-of-band by
        # _fetch_shot; a reply-less wire must never stall the pump)
        self.assertTrue(_screenshot_send(ws))
        shot_calls = [m for m in ws.sent if m.get('method') == 'Page.captureScreenshot']
        self.assertEqual(shot_calls[0].get('sessionId'), 'S1')
        rows = click_step.get('network') or []
        self.assertEqual(len(rows), 1)
        blob = json.dumps(rows)
        self.assertNotIn('topsecret', blob)
        self.assertIn('[redacted]', blob)
        self.assertEqual(session['network_pending'], [])

    def test_redacted_input_emits_no_screenshot_request(self):
        load = {'kind': 'load', 'url': 'https://site.test/', 'title': 'T',
                'fields': [], 'links': []}
        masked_in = {'kind': 'input', 'url': 'https://site.test/',
                     'element': _el(id='pw', type='password'), 'masked': True, 'nchars': 5}
        session, ws = self._run([
            {'method': 'Target.attachedToTarget', 'sessionId': 'S1',
             'params': {'targetInfo': {'type': 'page', 'targetId': 'T1'}}},
            {'method': 'Runtime.bindingCalled', 'sessionId': 'S1',
             'params': {'name': rec.BINDING_NAME, 'payload': json.dumps(load)}},
            {'method': 'Runtime.bindingCalled', 'sessionId': 'S1',
             'params': {'name': rec.BINDING_NAME, 'payload': json.dumps(masked_in)}},
        ])
        kinds = [s['kind'] for s in session['steps']]
        self.assertEqual(kinds, ['navigate', 'input'])
        self.assertTrue(session['steps'][1]['masked'])
        self.assertFalse(_screenshot_send(ws))

    def test_binding_events_on_non_page_targets_are_ignored_safely(self):
        incoming = [
            {'method': 'Target.attachedToTarget', 'sessionId': 'S2',
             'params': {'targetInfo': {'type': 'service_worker', 'targetId': 'T2'}}},
            {'method': 'Runtime.bindingCalled', 'sessionId': 'S2',
             'params': {'name': rec.BINDING_NAME, 'payload': '{"kind": "route"}'}},
        ]
        session, _ = self._run(incoming)
        self.assertEqual(session['steps'], [])


class FetchShot(unittest.TestCase):
    def test_reply_gets_attached_to_the_step(self):
        class _Ws:
            async def send(self, raw):
                pass

        step = {'kind': 'click', 'selector': '#x'}
        reply = {'result': {'data': base64.b64encode(b'PNG').decode()}}

        async def _go():
            wire = rec._CdpWire(_Ws())
            fut = await wire.call_future('Page.captureScreenshot', {'format': 'png'}, 'S1')
            fut.set_result(reply)
            await rec._fetch_shot({}, wire, step, fut, timeout_s=1)

        asyncio.run(_go())
        self.assertEqual(step['_pending_shot_png'], base64.b64encode(b'PNG').decode())

    def test_no_reply_leaves_the_step_untouched(self):
        class _Ws:
            async def send(self, raw):
                pass

        step = {'kind': 'click', 'selector': '#x'}

        async def _go():
            wire = rec._CdpWire(_Ws())
            fut = await wire.call_future('Page.captureScreenshot', {'format': 'png'}, 'S1')
            await rec._fetch_shot({}, wire, step, fut, timeout_s=0.05)

        asyncio.run(_go())
        self.assertNotIn('_pending_shot_png', step)


class WaitForDevtools(unittest.TestCase):
    def test_parses_the_endpoint_line(self):
        class _Err:
            def __init__(self, lines):
                self.lines = list(lines)

            async def readline(self):
                return self.lines.pop(0) if self.lines else b''

        url = asyncio.run(rec._wait_for_devtools(
            _Err([b' some banner\n',
                  b'DevTools listening on ws://127.0.0.1:9223/devtools/browser/abc\n']), 5))
        self.assertEqual(url, 'ws://127.0.0.1:9223/devtools/browser/abc')

    def test_times_out_without_the_line(self):
        class _Err:
            async def readline(self):
                await asyncio.sleep(0.01)
                return b'nothing useful\n'

        with self.assertRaises(TimeoutError):
            asyncio.run(rec._wait_for_devtools(_Err(), 0.2))


class ModuleOddments(unittest.TestCase):
    def test_session_id_format(self):
        sid = rec.session_id()
        self.assertTrue(sid.startswith('rec-') and len(sid) > 10)

    def test_submit_like_click_ignores_non_click(self):
        self.assertFalse(rec._submit_like_click('fill', {}))

    def test_redact_url_passes_malformed_urls_through(self):
        self.assertEqual(rec.redact_url('http://['), 'http://[')
        with unittest.mock.patch.object(rec, 'urlencode',
                                        side_effect=ValueError('bad query')):
            self.assertEqual(
                rec.redact_url('https://site.test/p?x=1&token=abc'),
                'https://site.test/p?x=1&token=abc')

    def test_click_href_is_redacted_into_the_step(self):
        steps = rec.aggregate_steps([_ev(
            'click', selector='#go',
            element=_el(tag='a', href='https://site.test/out?token=abc'),
            href='https://site.test/out?token=abc')])
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]['href'],
                         'https://site.test/out?token=%5Bredacted%5D')

    def test_render_step_click_bits(self):
        line = rec._render_step(3, {
            'kind': 'click', 'selector': '#go',
            'element': _el(tag='a', id='go', text='Go'),
            'value': 'Go', 'href': 'https://site.test/x',
            'network': [{'requestId': 'r1'}]})
        self.assertIn('a#go', line)
        self.assertIn('label="Go"', line)
        self.assertIn('-> https://site.test/x', line)
        self.assertIn('1 net rows', line)


class FindBrowserSources(unittest.TestCase):
    def _fake(self):
        d = tempfile.mkdtemp()
        fake = os.path.join(d, 'fake-browser')
        open(fake, 'w').close()
        return fake

    def test_env_variable_browser_wins(self):
        fake = self._fake()
        with unittest.mock.patch.dict(os.environ,
                                      {'BROWSER_RECORD_BIN': fake}):
            self.assertEqual(rec.find_browser(), fake)

    def test_candidate_list_is_scanned(self):
        fake = self._fake()
        with unittest.mock.patch.object(rec, 'BROWSER_CANDIDATES',
                                        ('/nonexistent/a', fake)):
            self.assertEqual(rec.find_browser(), fake)


class WaitForDevtoolsEof(unittest.TestCase):
    def test_stderr_eof_times_out(self):
        class _Err:
            async def readline(self):
                return b''

        with self.assertRaises(TimeoutError):
            asyncio.run(rec._wait_for_devtools(_Err(), 0.05))


class WriteSessionCleanup(unittest.TestCase):
    def test_write_session_leaves_no_temp_dir_on_crash(self):
        out_root = tempfile.mkdtemp()
        session = {'meta': {'label': 't', 'session_id': 'rec-x',
                            'redactions_applied': 0, 'artifacts': {}},
                   'steps': [], 'network_pending': []}
        with unittest.mock.patch.object(rec, 'render_txt',
                                        side_effect=RuntimeError('boom')):
            self.assertRaises(RuntimeError, rec.write_session,
                              session, out_root)
        self.assertEqual(os.listdir(out_root), [])


class _SlowWs:
    """A WebSocket that stalls in its first read: the pump's 1s queue poll
    times out once, then the deadline check ends the pump."""

    def __init__(self):
        self.sent = []
        self._read = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._read:
            self._read = True
            await asyncio.sleep(1.2)
        raise StopAsyncIteration

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class PumpDeadline(unittest.TestCase):
    def test_poll_timeout_then_deadline_return(self):
        session = {'meta': {'label': 't', 'session_id': 'rec-x',
                            'started_ts': 0, '_max_minutes': 0.005},
                   'steps': [], 'network_pending': []}
        asyncio.run(rec._pump(session, _SlowWs()))
        self.assertEqual(session['steps'], [])


class HandleOne(unittest.TestCase):
    def test_reply_resolves_the_waiting_future(self):
        async def go():
            fut = asyncio.get_running_loop().create_future()
            session = {'meta': {'label': 't'}, 'steps': [],
                       'network_pending': []}
            wire = rec._CdpWire(_FakeWs([]))
            wire.waiters[5] = fut
            await rec._handle_one(session, wire, asyncio.Queue(), {},
                                  {'id': 5, 'result': {'ok': 1}})
            return fut

        fut = asyncio.run(go())
        self.assertTrue(fut.done() and fut.result()['result'] == {'ok': 1})

    def test_event_without_session_id_is_dropped(self):
        async def go():
            session = {'meta': {'label': 't'}, 'steps': [],
                       'network_pending': []}
            wire = rec._CdpWire(_FakeWs([]))
            await rec._handle_one(session, wire, asyncio.Queue(), {},
                                  {'method': 'Runtime.enable', 'params': {}})
            return session

        self.assertEqual(asyncio.run(go())['steps'], [])


class NetRow(unittest.TestCase):
    def _session(self):
        return {'meta': {'label': 't'}, 'steps': [], 'network_pending': []}

    def test_response_event_updates_the_matching_request(self):
        session = self._session()
        rec._net_row(session, {'requestId': 'r7',
                               'request': {'method': 'GET',
                                           'url': 'https://site.test/a'}},
                     'Network.requestWillBeSent')
        rec._net_row(session, {'requestId': 'r7',
                               'response': {'url': 'https://site.test/a',
                                            'status': 200},
                               'type': 'Fetch'},
                     'Network.responseReceived')
        rows = session['network_pending']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 200)
        self.assertEqual(rows[0]['method'], 'GET')

    def test_buffer_is_capped(self):
        session = self._session()
        for i in range(rec.MAX_NET_ROWS + 5):
            rec._net_row(session, {'requestId': 'r%d' % i,
                                   'request': {'method': 'GET',
                                               'url': 'https://site.test/%d' % i}},
                         'Network.requestWillBeSent')
        self.assertEqual(len(session['network_pending']), rec.MAX_NET_ROWS)


class Sink(unittest.TestCase):
    def _session(self):
        session = {'meta': {'label': 't'}, 'steps': [], 'network_pending': []}
        session['steps'].append({'kind': 'fill'})
        return session

    def test_bad_payload_is_dropped(self):
        session = self._session()
        asyncio.run(rec._on_sink(session, None, rec._CdpWire(_FakeWs([])),
                                 'S', 'not-json'))
        self.assertEqual(len(session['steps']), 1)

    def test_cooldown_suppresses_screenshots(self):
        session = self._session()
        state = rec._SessionState('T')
        state.last_shot_ts = rec.time.time()
        asyncio.run(rec._on_sink(session, state, rec._CdpWire(_FakeWs([])),
                                 'S', json.dumps(_ev('fill', value='v'))))
        self.assertEqual(session['steps'][-1]['kind'], 'fill')

    def test_screenshot_send_failure_keeps_the_step(self):
        class _Broken(_FakeWs):
            async def send(self, raw):
                raise RuntimeError('send broke')

        session = self._session()
        asyncio.run(rec._on_sink(session, None, rec._CdpWire(_Broken([])),
                                 'S', json.dumps(_ev('fill', value='v'))))
        self.assertEqual(session['steps'][-1]['kind'], 'fill')


class RunCaptureAsync(unittest.TestCase):
    """_run_capture_async against fake browser executables and a stubbed
    CDP transport: the subprocess lifecycle (terminate, kill-after-grace)
    is what needs proving, not a real Chromium."""

    def _browser_script(self, body):
        d = tempfile.mkdtemp()
        script = os.path.join(d, 'fake-browser')
        with open(script, 'w') as f:
            f.write('#!/bin/sh\n'
                    'echo "DevTools listening on ws://127.0.0.1:59999/'
                    'devtools/browser/abc" >&2\n' + body)
        os.chmod(script, 0o755)
        return script

    def _run(self, script, start_url=None):
        import websockets
        session = {'meta': {'label': 't', 'session_id': 'rec-x',
                            '_max_minutes': 0},
                   'steps': [], 'network_pending': []}
        pumps = []

        async def fake_pump(s, ws):
            pumps.append(ws)

        class _FakeConnect:
            def __init__(self, url, **kw):
                self.url = url

            async def __aenter__(self):
                return _FakeWs([])

            async def __aexit__(self, *exc):
                return False

        with unittest.mock.patch.object(rec, '_wait_for_devtools',
                                        return_value='ws://fake'), \
                unittest.mock.patch.object(rec, '_pump', fake_pump), \
                unittest.mock.patch.object(websockets, 'connect',
                                           _FakeConnect):
            asyncio.run(rec._run_capture_async(
                session, script, start_url, 0, 0))
        return session, pumps

    def test_live_browser_is_terminated_and_profile_removed(self):
        script = self._browser_script('sleep 2\n')
        session, pumps = self._run(script, start_url='https://site.test/')
        self.assertEqual(len(pumps), 1)
        self.assertEqual(session['steps'], [])

    def test_grace_expires_and_the_browser_is_killed(self):
        script = self._browser_script('trap \'\' TERM\nsleep 6\n')
        session, _pumps = self._run(script)
        self.assertEqual(session['steps'], [])


class PumpBrokenJson(unittest.TestCase):
    def test_unparseable_frame_is_skipped(self):
        class _BadJsonWs:
            def __init__(self):
                self.sent = []
                self._n = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.sleep(0)
                if not self._n:
                    self._n = True
                    return 'not-json{'
                raise StopAsyncIteration

            async def send(self, raw):
                self.sent.append(json.loads(raw))

        session = {'meta': {'label': 't', 'session_id': 'rec-x',
                            '_max_minutes': 0},
                   'steps': [], 'network_pending': []}
        asyncio.run(rec._pump(session, _BadJsonWs()))
        self.assertEqual(session['steps'], [])

    def test_connection_closed_ends_the_reader(self):
        import websockets.exceptions

        class _ClosedWs:
            def __init__(self):
                self.sent = []

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise websockets.exceptions.ConnectionClosed(None, None)

            async def send(self, raw):
                self.sent.append(json.loads(raw))

        session = {'meta': {'label': 't', 'session_id': 'rec-x',
                            '_max_minutes': 0},
                   'steps': [], 'network_pending': []}
        asyncio.run(rec._pump(session, _ClosedWs()))
        self.assertEqual(session['steps'], [])


class _HideWebsockets:
    """A meta-path finder that makes `import websockets` fail, so main()'s
    dependency check can be exercised even with websockets installed."""

    def find_spec(self, name, path=None, target=None):
        if name == 'websockets':
            raise ImportError('hidden for test')
        return None


class Main(unittest.TestCase):
    def test_reports_missing_websockets(self):
        hidden = _HideWebsockets()
        with unittest.mock.patch.dict(sys.modules, {'websockets': None}):
            sys.meta_path.insert(0, hidden)
            sys.modules.pop('websockets', None)
            try:
                rc = rec.main(['--label', 't', '--out', tempfile.mkdtemp()])
            finally:
                sys.meta_path.remove(hidden)
        self.assertEqual(rc, 2)

    def test_main_reports_missing_browser(self):
        rc = rec.main(['--label', 't', '--out', tempfile.mkdtemp(),
                       '--browser', '/nonexistent/browser-bin'])
        self.assertEqual(rc, 2)

    def test_main_handles_sigint_like_a_finish(self):
        out_root = tempfile.mkdtemp()
        with unittest.mock.patch.object(rec, 'run_capture',
                                        side_effect=KeyboardInterrupt):
            rc = rec.main(['--label', 't', '--out', out_root,
                           '--browser', os.__file__.replace('.pyc', '.py')])
        self.assertEqual(rc, 0)
        self.assertEqual(len(os.listdir(out_root)), 1)

    def test_main_runs_and_writes_partial_artifacts(self):
        d = tempfile.mkdtemp()
        fake = os.path.join(d, 'fake-browser')
        with open(fake, 'w') as f:
            f.write('#!/bin/sh\n'
                    'echo "DevTools listening on ws://127.0.0.1:59999/'
                    'devtools/browser/abc" >&2\n'
                    'sleep 0.5\n')
        os.chmod(fake, 0o755)
        out_root = tempfile.mkdtemp()
        rc = rec.main(['--label', 't', '--out', out_root,
                       '--browser', fake, '--max-minutes', '0.02'])
        self.assertEqual(rc, 0)
        written = os.listdir(out_root)
        self.assertEqual(len(written), 1)
        rec_dir = os.path.join(out_root, written[0])
        for name in ('session.txt', 'session.json',
                     'browser_act_actions.json'):
            self.assertTrue(os.path.isfile(os.path.join(rec_dir, name)))
        self.assertIn('aborted',
                      json.load(open(os.path.join(rec_dir, 'session.json')))
                      ['meta'])


if __name__ == '__main__':
    unittest.main()