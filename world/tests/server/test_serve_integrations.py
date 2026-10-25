"""Split part of test_serve.py (serve.py behavior tests)."""
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse

import asyncio
import openpyxl

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
import sim  # noqa: E402
from test_serve_helpers import setUpModule, tearDownModule  # noqa: E402


class GoogleOAuth(unittest.TestCase):
    """_google_access_token / _google_call: real OAuth mechanics confirmed
    live against a real refresh token minted through a real
    installed-app consent flow (accounts.google.com -> oauth2.googleapis.com
    token exchange), before anything was built on top of it."""

    def _resp(self, body):
        class R:
            def read(self):
                return body
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
        return R()

    def setUp(self):
        serve._GOOGLE_ACCESS_TOKEN_CACHE['at'] = 0.0
        serve._GOOGLE_ACCESS_TOKEN_CACHE['token'] = None
        self.addCleanup(lambda: serve._GOOGLE_ACCESS_TOKEN_CACHE.update({'at': 0.0, 'token': None}))
        serve._GOOGLE_RATE_WINDOW.clear()
        self.addCleanup(serve._GOOGLE_RATE_WINDOW.clear)
        self._rate_limits_backup = dict(serve._GOOGLE_RATE_LIMITS)
        self.addCleanup(lambda: serve._GOOGLE_RATE_LIMITS.update(self._rate_limits_backup))

    def test_rate_guard_fails_closed_when_per_minute_budget_spent(self):
        serve._GOOGLE_RATE_LIMITS['sheets'] = (3, 0.0)
        now = [1000.0]
        orig = time.monotonic
        time.monotonic = lambda: now[0]
        try:
            self.assertIsNone(serve._google_rate_guard('sheets'))
            self.assertIsNone(serve._google_rate_guard('sheets'))
            self.assertIsNone(serve._google_rate_guard('sheets'))
            # 4th call within the same 60s window is refused.
            err = serve._google_rate_guard('sheets')
            self.assertIsNotNone(err)
            self.assertIn('rate budget spent', err)
            self.assertIn('do not retry', err)
            # A new minute (window slides) frees capacity.
            now[0] += 61.0
            self.assertIsNone(serve._google_rate_guard('sheets'))
        finally:
            time.monotonic = orig

    def test_rate_guard_paces_min_interval(self):
        serve._GOOGLE_RATE_LIMITS['sheets'] = (1000, 0.5)
        t0 = time.monotonic()
        self.assertIsNone(serve._google_rate_guard('sheets'))
        self.assertIsNone(serve._google_rate_guard('sheets'))  # second call paced to >=0.5s
        self.assertGreaterEqual(time.monotonic() - t0, 0.5)

    def test_rate_guard_tracks_apis_separately(self):
        serve._GOOGLE_RATE_LIMITS['calendar'] = (1, 0.0)
        serve._GOOGLE_RATE_LIMITS['sheets'] = (1, 0.0)
        self.assertIsNone(serve._google_rate_guard('calendar'))
        self.assertIsNone(serve._google_rate_guard('sheets'))  # separate bucket
        self.assertIsNotNone(serve._google_rate_guard('calendar'))
        self.assertIsNotNone(serve._google_rate_guard('sheets'))

    def test_google_api_name_maps_urls_to_rate_keys(self):
        self.assertEqual(serve._google_api_name('https://sheets.googleapis.com/v4/spreadsheets/x/values/A1'), 'sheets')
        self.assertEqual(serve._google_api_name('https://gmail.googleapis.com/gmail/v1/users/me/messages'), 'gmail')
        self.assertEqual(serve._google_api_name('https://docs.googleapis.com/v1/documents/d1'), 'docs')
        self.assertEqual(serve._google_api_name('https://www.googleapis.com/calendar/v3/calendars/primary/events'), 'calendar')
        self.assertIsNone(serve._google_api_name('https://oauth2.googleapis.com/token'))

    def test_google_call_refused_before_network_when_rate_budget_spent(self):
        serve._GOOGLE_RATE_LIMITS['gmail'] = (1, 0.0)
        # First call passes the guard; it fails later on the missing credential.
        data, error = serve._google_call('GET', 'https://gmail.googleapis.com/gmail/v1/users/me/messages')
        self.assertIsNone(data)
        self.assertIn('not configured', error)
        # Second call is refused by the rate guard before any token/network work.
        data, error = serve._google_call('GET', 'https://gmail.googleapis.com/gmail/v1/users/me/messages')
        self.assertIsNone(data)
        self.assertIn('rate budget spent', error)

    def test_no_credential_fails_closed(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value=None):
            token, error = serve._google_access_token()
        self.assertIsNone(token)
        self.assertIn('not configured', error)

    def test_missing_refresh_token_fails_closed_with_a_clear_reason(self):
        creds = json.dumps({'client_id': 'cid', 'client_secret': 'sec', 'refresh_token': None})
        with unittest.mock.patch.object(serve, '_open_secret', return_value=creds):
            token, error = serve._google_access_token()
        self.assertIsNone(token)
        self.assertIn('OAuth consent step was never completed', error)

    def test_google_is_configured_checks_vault_without_network(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value=None):
            self.assertFalse(serve._google_is_configured())
        with unittest.mock.patch.object(serve, '_open_secret', return_value='not-json'):
            self.assertFalse(serve._google_is_configured())
        with unittest.mock.patch.object(serve, '_open_secret',
                                       return_value=json.dumps({'client_id': 'c', 'refresh_token': None})):
            self.assertFalse(serve._google_is_configured())
        with unittest.mock.patch.object(serve, '_open_secret',
                                       return_value=json.dumps({'client_id': 'c', 'refresh_token': 'rt'})):
            self.assertTrue(serve._google_is_configured())

    def test_safe_design_reference_name_sanitizes(self):
        self.assertEqual(serve._safe_design_reference_name('landing page v2'), 'landing_page_v2')
        self.assertEqual(serve._safe_design_reference_name('a/b\\c'), 'abc')
        self.assertEqual(serve._safe_design_reference_name('  my.design  '), 'my.design')
        self.assertIsNone(serve._safe_design_reference_name(''))
        self.assertIsNone(serve._safe_design_reference_name('../..'))

    def test_design_brief_from_image_parses_vision_reply(self):
        with unittest.mock.patch.object(serve, '_vision_tier_slug', return_value='vision-x'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        return_value={'choices': [{'message': {'content': 'PALETTE: #fff'}}]}):
            out = serve._design_brief_from_image('QUJD', 'image x')
        self.assertTrue(out['ok'])
        self.assertIn('PALETTE: #fff', out['brief'])

    def test_design_brief_from_image_fails_gracefully_without_vision_tier(self):
        with unittest.mock.patch.object(serve, '_vision_tier_slug', return_value=None):
            out = serve._design_brief_from_image('QUJD', 'image x')
        self.assertFalse(out['ok'])
        self.assertIn('vision model tier not available', out['note'])

    def test_design_brief_from_image_degrades_on_call_failure(self):
        with unittest.mock.patch.object(serve, '_vision_tier_slug', return_value='vision-x'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        side_effect=RuntimeError('boom')):
            out = serve._design_brief_from_image('QUJD', 'image x')
        self.assertFalse(out['ok'])
        self.assertIn('boom', out['note'])

    def test_real_refresh_request_shape(self):
        creds = json.dumps({'client_id': 'cid', 'client_secret': 'sec', 'refresh_token': 'rt-1'})
        captured = {}

        def fake_urlopen(req, timeout=20):
            captured['url'] = req.full_url
            captured['body'] = urllib.parse.parse_qs(req.data.decode())
            return self._resp(b'{"access_token": "at-1", "expires_in": 3599}')

        with unittest.mock.patch.object(serve, '_open_secret', return_value=creds), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=fake_urlopen):
            token, error = serve._google_access_token()
        self.assertIsNone(error)
        self.assertEqual(token, 'at-1')
        self.assertEqual(captured['url'], 'https://oauth2.googleapis.com/token')
        self.assertEqual(captured['body']['refresh_token'], ['rt-1'])
        self.assertEqual(captured['body']['grant_type'], ['refresh_token'])

    def test_cached_token_avoids_a_second_network_call(self):
        creds = json.dumps({'client_id': 'cid', 'client_secret': 'sec', 'refresh_token': 'rt-1'})
        with unittest.mock.patch.object(serve, '_open_secret', return_value=creds), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        return_value=self._resp(b'{"access_token": "at-1"}')) as net:
            serve._google_access_token()
            serve._google_access_token()
        net.assert_called_once()

    def test_google_call_retries_once_on_401_with_a_forced_refresh(self):
        creds = json.dumps({'client_id': 'cid', 'client_secret': 'sec', 'refresh_token': 'rt-1'})
        calls = {'n': 0}

        def fake_urlopen(req, timeout=30):
            if 'oauth2.googleapis.com' in req.full_url:
                return self._resp(b'{"access_token": "at-fresh"}')
            calls['n'] += 1
            if calls['n'] == 1:
                raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'expired'))
            return self._resp(b'{"items": []}')

        with unittest.mock.patch.object(serve, '_open_secret', return_value=creds), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=fake_urlopen):
            data, error = serve._google_call('GET', 'https://www.googleapis.com/calendar/v3/calendars/primary/events')
        self.assertIsNone(error)
        self.assertEqual(data, {'items': []})
        self.assertEqual(calls['n'], 2)  # the real endpoint was hit twice: 401, then success

    def test_google_call_surfaces_a_non_401_error_without_retrying(self):
        creds = json.dumps({'client_id': 'cid', 'client_secret': 'sec', 'refresh_token': 'rt-1'})

        def fake_urlopen(req, timeout=30):
            if 'oauth2.googleapis.com' in req.full_url:
                return self._resp(b'{"access_token": "at-1"}')
            raise urllib.error.HTTPError(req.full_url, 403, 'Forbidden', {}, io.BytesIO(b'quota exceeded'))

        with unittest.mock.patch.object(serve, '_open_secret', return_value=creds), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=fake_urlopen):
            data, error = serve._google_call('GET', 'https://www.googleapis.com/calendar/v3/calendars/primary/events')
        self.assertIsNone(data)
        self.assertIn('403', error)
        self.assertIn('quota exceeded', error)


class PixellabCall(unittest.TestCase):
    """_pixellab_call / _pixellab_poll_job: real API mechanics, matching the
    think tank's own already-tested spike script (scripts/pixellab_spike.py)
    -- not guessed from the public OpenAPI spec, per the same "verify,
    don't assume" discipline as every other real integration."""

    def _resp(self, body):
        class R:
            def read(self):
                return body
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
        return R()

    def test_no_credential_fails_closed_without_a_network_call(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value=None), \
             unittest.mock.patch.object(serve, '_pixellab_token', return_value=None), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen') as net:
            data, error = serve._pixellab_call('POST', '/create-character-with-4-directions', {})
        net.assert_not_called()
        self.assertIsNone(data)
        self.assertIn('not configured', error)

    def test_real_call_shape_matches_the_tested_spike_script(self):
        captured = {}

        def fake_urlopen(req, timeout=30):
            captured['url'] = req.full_url
            captured['method'] = req.get_method()
            captured['headers'] = {k.lower(): v for k, v in req.headers.items()}
            captured['body'] = json.loads(req.data)
            return self._resp(b'{"character_id": "c1", "background_job_id": "j1"}')

        with unittest.mock.patch.object(serve, '_open_secret', return_value='fake-pixellab-key'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=fake_urlopen):
            data, error = serve._pixellab_call('POST', '/create-character-with-4-directions', {
                'description': 'a knight', 'image_size': {'width': 48, 'height': 48},
                'view': 'high top-down', 'template_id': 'mannequin'})
        self.assertIsNone(error)
        self.assertEqual(data, {'character_id': 'c1', 'background_job_id': 'j1'})
        self.assertEqual(captured['url'], 'https://api.pixellab.ai/v2/create-character-with-4-directions')
        self.assertEqual(captured['method'], 'POST')
        self.assertEqual(captured['headers'].get('authorization'), 'Bearer fake-pixellab-key')
        self.assertEqual(captured['body']['description'], 'a knight')

    def test_http_error_is_surfaced(self):
        def raise_http_error(req, timeout=30):
            raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'bad key'))
        with unittest.mock.patch.object(serve, '_open_secret', return_value='fake-key'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=raise_http_error):
            data, error = serve._pixellab_call('GET', '/balance')
        self.assertIsNone(data)
        self.assertIn('401', error)
        self.assertIn('bad key', error)

    def test_poll_job_returns_on_completed(self):
        with unittest.mock.patch.object(serve, '_pixellab_call', return_value=({'status': 'completed'}, None)):
            data, error = serve._pixellab_poll_job('job-1', timeout=10, interval=0)
        self.assertIsNone(error)
        self.assertEqual(data['status'], 'completed')

    def test_poll_job_surfaces_a_failed_status(self):
        with unittest.mock.patch.object(serve, '_pixellab_call', return_value=({'status': 'failed'}, None)):
            data, error = serve._pixellab_poll_job('job-1', timeout=10, interval=0)
        self.assertIsNone(data)
        self.assertIn('job failed', error)

    def test_poll_job_times_out_on_a_never_completing_job(self):
        with unittest.mock.patch.object(serve, '_pixellab_call', return_value=({'status': 'processing'}, None)):
            data, error = serve._pixellab_poll_job('job-1', timeout=0.05, interval=0.01)
        self.assertIsNone(data)
        self.assertIn('timed out', error)

    def test_poll_job_surfaces_a_transient_call_error_immediately(self):
        with unittest.mock.patch.object(serve, '_pixellab_call', return_value=(None, 'PixelLab call failed: network blip')):
            data, error = serve._pixellab_poll_job('job-1', timeout=10, interval=0)
        self.assertIsNone(data)
        self.assertIn('network blip', error)


class TregCall(unittest.TestCase):
    """_treg_call: real API mechanics confirmed against Treg's own docs
    (POST https://treg.to/call/{endpoint_id}, header X-Treg-Token) -- built
    for real X-trending/LinkedIn-search tools.
    """

    def _resp(self, body):
        class R:
            def read(self):
                return body
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
        return R()

    def test_no_credential_fails_closed_without_a_network_call(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value=None), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen') as net:
            data, error = serve._treg_call('x.x.get-trends-by-woeid', {'woeid': 1})
        net.assert_not_called()
        self.assertIsNone(data)
        self.assertIn('not configured', error)

    def test_default_post_call_shape_matches_treg_docs(self):
        captured = {}

        def fake_urlopen(req, timeout=30):
            captured['url'] = req.full_url
            captured['method'] = req.get_method()
            captured['headers'] = {k.lower(): v for k, v in req.headers.items()}
            captured['body'] = req.data
            return self._resp(b'{"posts": []}')

        with unittest.mock.patch.object(serve, '_open_secret', return_value='fake-treg-token'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=fake_urlopen):
            data, error = serve._treg_call('moz.web.url.metrics', {'targets': ['moz.com']})
        self.assertIsNone(error)
        self.assertEqual(data, {'posts': []})
        self.assertEqual(captured['url'], 'https://treg.to/call/moz.web.url.metrics')
        self.assertEqual(captured['method'], 'POST')
        self.assertEqual(captured['headers'].get('x-treg-token'), 'fake-treg-token')
        self.assertEqual(json.loads(captured['body']), {'targets': ['moz.com']})

    def test_get_call_sends_params_as_a_query_string_not_a_body(self):
        # A GET endpoint's params belong in the
        # URL query string, never a request body -- Treg's own real error
        # for the opposite mistake was explicit ("is GET -- add --method
        # GET", then "needs --query woeid=<value>").
        captured = {}

        def fake_urlopen(req, timeout=30):
            captured['url'] = req.full_url
            captured['method'] = req.get_method()
            captured['body'] = req.data
            return self._resp(b'{"data": [{"trend_name": "AI safety"}]}')

        with unittest.mock.patch.object(serve, '_open_secret', return_value='fake-treg-token'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=fake_urlopen):
            data, error = serve._treg_call('x.x.get-trends-by-woeid', {'woeid': 1}, method='GET')
        self.assertIsNone(error)
        self.assertEqual(captured['method'], 'GET')
        self.assertIsNone(captured['body'])
        self.assertEqual(captured['url'], 'https://treg.to/call/x.x.get-trends-by-woeid?woeid=1')

    def test_non_json_response_falls_back_to_raw_text(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value='fake-token'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        return_value=self._resp(b'not json at all')):
            data, error = serve._treg_call('some.endpoint', {})
        self.assertIsNone(error)
        self.assertEqual(data, {'_raw': 'not json at all'})

    def test_http_error_is_surfaced_with_status_and_detail(self):
        def raise_http_error(req, timeout=30):
            raise urllib.error.HTTPError(req.full_url, 402, 'Payment Required',
                                         {}, io.BytesIO(b'insufficient balance'))
        with unittest.mock.patch.object(serve, '_open_secret', return_value='fake-token'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=raise_http_error):
            data, error = serve._treg_call('x.x.get-trends-by-woeid', {'woeid': 1})
        self.assertIsNone(data)
        self.assertIn('402', error)
        self.assertIn('insufficient balance', error)

    def test_network_exception_is_surfaced_not_raised(self):
        with unittest.mock.patch.object(serve, '_open_secret', return_value='fake-token'), \
             unittest.mock.patch.object(serve.urllib.request, 'urlopen', side_effect=OSError('timed out')):
            data, error = serve._treg_call('x.x.get-trends-by-woeid', {'woeid': 1})
        self.assertIsNone(data)
        self.assertIn('timed out', error)

    def test_known_endpoint_prices_match_what_was_verified_live(self):
        # x.x.get-trends-by-woeid: one real live call, billed exactly $0.01.
        # scrapecreators.x.v1-linkedin-search-posts: independently RE-DERIVED
        # from 2 real billed calls ($0.00376 total / 2 = $0.00188), matching
        # the catalog's own stated price exactly -- not just trusted from
        # the catalog page alone (see the Treg skill's own "Lessons learned"
        # about a spike that invented wrong prices once).
        self.assertEqual(serve.TREG_ENDPOINT_COSTS['x.x.get-trends-by-woeid'], 0.01)
        self.assertEqual(serve.TREG_ENDPOINT_COSTS['scrapecreators.x.v1-linkedin-search-posts'], 0.00188)


class JevQuorumDecision(unittest.TestCase):
    """Quorum sensing, ported from real Temnothorax ant nest-site
    selection: pool multiple independent Jev samples specifically to overcome
    errors inherent in any ONE sample -- live-confirmed problem here (the
    same URL got a confident 0.87 allow one run, a low-confidence 0.56
    escalate-and-deny the next). Only re-samples on an ambiguous FIRST call
    (a low-confidence allow); a confident allow or a firm block costs one
    sample, same as before -- real money per extra sample, unlike a real ant."""

    def _mock_sample_sequence(self, choices):
        # choices: list of {'answers': {...}} dicts, one per _call_openrouter_
        # decision_sync invocation, consumed in order.
        return unittest.mock.patch.object(serve, '_call_openrouter_decision_sync', side_effect=choices)

    def _answer(self, decision, confidence, cost=0.01):
        return {'answers': {'choice': {'choice': decision, 'confidence': confidence, 'probabilities': {}}},
                'usage': {'cost': cost}}

    def test_confident_allow_takes_exactly_one_sample(self):
        with self._mock_sample_sequence([self._answer('allow', 0.9)]) as mock_call:
            decision, confidence, cost, _trace = asyncio.run(serve._jev_quorum_decision('i', {}))
        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual((decision, confidence), ('allow', 0.9))

    def test_firm_block_takes_exactly_one_sample_even_at_low_confidence(self):
        # A block is a block -- quorum sensing exists to resolve an unsure
        # ALLOW, not to second-guess a confident classifier that already
        # said no.
        with self._mock_sample_sequence([self._answer('block', 0.3)]) as mock_call:
            decision, confidence, cost, _trace = asyncio.run(serve._jev_quorum_decision('i', {}))
        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual(decision, 'block')

    def test_ambiguous_allow_resamples_up_to_quorum_sample_size(self):
        with self._mock_sample_sequence([self._answer('allow', 0.4)] * serve.QUORUM_SAMPLE_SIZE) as mock_call:
            asyncio.run(serve._jev_quorum_decision('i', {}))
        self.assertEqual(mock_call.call_count, serve.QUORUM_SAMPLE_SIZE)

    def test_two_of_three_agreeing_allow_forms_a_quorum_and_reports_the_strongest_agreeing_confidence(self):
        with self._mock_sample_sequence([
            self._answer('allow', 0.4),   # ambiguous first sample -> triggers resampling
            self._answer('allow', 0.85),  # agrees, confidently
            self._answer('block', 0.9),   # disagrees -- excluded from the agreeing set
        ]):
            decision, confidence, cost, _trace = asyncio.run(serve._jev_quorum_decision('i', {}))
        self.assertEqual(decision, 'allow')
        self.assertAlmostEqual(confidence, 0.85)  # the strongest AGREEING vote, not an average

    def test_no_quorum_reports_the_original_low_confidence_result(self):
        with self._mock_sample_sequence([
            self._answer('allow', 0.4),
            self._answer('block', 0.9),
            self._answer('block', 0.8),
        ]):
            decision, confidence, cost, _trace = asyncio.run(serve._jev_quorum_decision('i', {}))
        # Only 1 agreeing vote (< QUORUM_MIN_AGREEING) -- stays the original
        # unsure result, not silently upgraded or downgraded.
        self.assertEqual((decision, confidence), ('allow', 0.4))

    def test_total_cost_sums_every_sample_actually_taken(self):
        with self._mock_sample_sequence([
            self._answer('allow', 0.4, cost=0.01),
            self._answer('allow', 0.5, cost=0.02),
            self._answer('block', 0.9, cost=0.03),
        ]):
            _decision, _confidence, cost, _trace = asyncio.run(serve._jev_quorum_decision('i', {}))
        self.assertAlmostEqual(cost, 0.06)

    def test_a_failed_resample_is_skipped_not_fatal(self):
        with self._mock_sample_sequence([
            self._answer('allow', 0.4),
            RuntimeError('network blip'),
            self._answer('allow', 0.85),
        ]):
            decision, confidence, cost, _trace = asyncio.run(serve._jev_quorum_decision('i', {}))
        # Still reaches a quorum from the 2 real votes despite the blip.
        self.assertEqual(decision, 'allow')
        self.assertAlmostEqual(confidence, 0.85)

    def test_unreachable_classifier_on_the_first_call_fails_closed(self):
        with self._mock_sample_sequence([RuntimeError('down')]) as mock_call:
            decision, confidence, cost, _trace = asyncio.run(serve._jev_quorum_decision('i', {}))
        self.assertEqual(mock_call.call_count, 1)
        self.assertIsNone(decision)
        # An unreachable classifier is zero trust, not full trust. The old
        # 1.0 value let a transport outage look maximally confident.
        self.assertEqual(confidence, 0.0)

    def test_trace_id_is_propagated_from_the_decision_call(self):
        # The decision_tape row's trace_id must reach the caller so it can be
        # stamped on the resulting action_log rows (the calibration report's
        # exact join key). The real _call_openrouter_decision_sync stamps it
        # into the returned data; here we simulate that by including it.
        with self._mock_sample_sequence([
            {'answers': {'choice': {'choice': 'allow', 'confidence': 0.9, 'probabilities': {}}},
             'usage': {'cost': 0.01}, 'trace_id': 'deadbeef'},
        ]):
            decision, confidence, _cost, trace_id = asyncio.run(serve._jev_quorum_decision('i', {}))
        self.assertEqual((decision, confidence), ('allow', 0.9))
        self.assertEqual(trace_id, 'deadbeef')


class DecisionCalibration(unittest.TestCase):
    """_decision_calibration_report (takeaway #1): bucket Jev's
    stated confidence against the real success rate of the actions it allowed,
    so an overconfident classifier is visible instead of quietly eroding the
    low-confidence-escalation floor. Reads only action_log (gate rows carry
    confidence+decision, outcome rows carry success/failure) -- no model
    calls, no network."""

    def setUp(self):
        # The module DB is shared across methods in this file, and the report
        # reads a 7-day window -- clear the table so each test sees only its
        # own rows.
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')

    def _gate(self, agent, action, decision, confidence, ts, trace_id=None):
        serve.log_action(agent, action, {'target': f'http://x/{ts}', 'purpose': 'p',
                                         'decision': decision, 'confidence': confidence, 'cost': 0.01},
                         authorized=True, trace_id=trace_id)
        with serve._db() as conn:
            conn.execute('UPDATE action_log SET ts = ? WHERE id = (SELECT MAX(id) FROM action_log)', (ts,))

    def _log_outcome(self, agent, action, decision, ts, trace_id=None):
        serve.log_action(agent, action, {'url': f'http://x/{ts}', 'decision': decision}, authorized=True, trace_id=trace_id)
        with serve._db() as conn:
            conn.execute('UPDATE action_log SET ts = ? WHERE id = (SELECT MAX(id) FROM action_log)', (ts,))

    def test_successful_and_failed_allows_are_scored_per_bucket(self):
        base = time.time()
        tid = 0
        for conf, outcome, n in [
            (0.95, 'allowed', 4),   # high-confidence, mostly succeeds
            (0.95, 'allowed_but_fetch_failed', 1),
            (0.55, 'allowed', 1),   # low-confidence, fails -> below the safety floor
            (0.55, 'allowed_but_fetch_failed', 1),
        ]:
            for _ in range(n):
                tid += 1
                self._gate('ada', 'browse', 'allowed', conf, base + 10 * tid, trace_id=f't{tid}')
                self._log_outcome('ada', 'browse', outcome, base + 10 * tid + 1, trace_id=f't{tid}')
        report = serve._decision_calibration_report(window_s=7 * 86400)
        buckets = {b['bin']: b for b in report['buckets']}
        hi = buckets['0.90-1.00']
        self.assertEqual(hi['n_allowed'], 5)
        self.assertEqual(hi['n_outcome'], 5)
        self.assertEqual(hi['n_success'], 4)
        self.assertEqual(hi['n_failure'], 1)
        self.assertEqual(hi['success_rate'], 0.8)
        lo = buckets['0.50-0.60']
        self.assertEqual(lo['n_outcome'], 2)
        self.assertEqual(lo['success_rate'], 0.5)

    def test_blocked_and_escalated_are_counted_but_not_scored(self):
        base = time.time()
        self._gate('ada', 'browse', 'blocked', 0.9, base + 1)
        self._gate('ada', 'download', 'escalated_unsure', 0.4, base + 2)
        self._gate('ada', 'browse', 'allowed', 0.95, base + 3)
        report = serve._decision_calibration_report()
        buckets = {b['bin']: b for b in report['buckets']}
        hi = buckets['0.90-1.00']
        self.assertEqual(hi['n_blocked'], 1)
        self.assertEqual(hi['n_allowed'], 1)
        self.assertEqual(hi['n_outcome'], 0)  # no outcome row -> not scored
        lo = buckets['0.00-0.50']
        self.assertEqual(lo['n_escalated'], 1)
        self.assertEqual(report['total_decisions'], 3)
        self.assertEqual(report['scoreable'], 0)
        self.assertEqual(report['scoreable_fraction'], 0.0)

    def test_trace_id_outcome_match_wins_over_the_heuristic(self):
        base = time.time()
        # A gate with a threaded trace_id whose real outcome row is a failure;
        # an UNRELATED later 'allowed' row (different agent) must NOT be picked.
        self._gate('ada', 'browse', 'allowed', 0.9, base + 1, trace_id='t1')
        self._log_outcome('ada', 'browse', 'allowed_but_fetch_failed', base + 2, trace_id='t1')
        self._log_outcome('bob', 'browse', 'allowed', base + 3)  # unrelated, no trace
        report = serve._decision_calibration_report()
        hi = next(b for b in report['buckets'] if b['bin'] == '0.90-1.00')
        self.assertEqual(hi['n_success'], 0)
        self.assertEqual(hi['n_failure'], 1)
        self.assertEqual(hi['success_rate'], 0.0)

    def test_overconfident_classifier_shows_large_calibration_error(self):
        base = time.time()
        # Confident allows that only succeed half the time = overconfidence.
        for i in range(6):
            self._gate('ada', 'browse', 'allowed', 0.95, base + i, trace_id=f'o{i}')
            self._log_outcome('ada', 'browse', 'allowed' if i % 2 == 0 else 'allowed_but_fetch_failed', base + i + 0.1, trace_id=f'o{i}')
        report = serve._decision_calibration_report()
        self.assertEqual(report['overall_success_rate'], 0.5)
        # Weighted mismatch against the 0.95 center -> clearly > 0.
        self.assertGreater(report['calibration_error'], 0.4)


class CalibrationLoop(unittest.TestCase):
    """_calibration_adjust_pass + _effective_safety_confidence (
    feedback-loop actuator for takeaway #1): the calibration report is the
    sensor -- the escalation threshold was never adjusted by it. This pass
    moves the LIVE bar (settings row `jev_safety_confidence`) so decisions
    auto-approved at ~the current confidence bar succeed at ~the claimed
    reliability: below target -> raise the bar, above -> lower it, and never
    move on noise. Safety-critical kinds ('blocked command' at floor 1.0)
    are unaffected -- only the routine default bar moves."""

    def setUp(self):
        # Shared module DB: each test sees only its own calibration rows and no
        # leftover threshold.
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')
            conn.execute("DELETE FROM settings WHERE key = 'jev_safety_confidence'")

    def _gate(self, confidence, ts, trace_id, ok_outcome=True):
        serve.log_action('ada', 'browse', {'target': f'http://x/{ts}', 'purpose': 'p',
                                           'decision': 'allowed', 'confidence': confidence, 'cost': 0.01},
                         authorized=True, trace_id=trace_id)
        serve.log_action('ada', 'browse', {'url': f'http://x/{ts}',
                                           'decision': 'allowed' if ok_outcome else 'allowed_but_fetch_failed'},
                         authorized=True, trace_id=trace_id)
        with serve._db() as conn:
            conn.execute("UPDATE action_log SET ts = ? WHERE id IN (SELECT id FROM action_log ORDER BY id DESC LIMIT 2)", (ts,))
            # gate row must precede its outcome row
            conn.execute("UPDATE action_log SET ts = ? WHERE id = (SELECT MIN(id) FROM action_log WHERE trace_id = ?)", (ts, trace_id))
            conn.execute("UPDATE action_log SET ts = ? WHERE id = (SELECT MAX(id) FROM action_log WHERE trace_id = ?)", (ts + 0.1, trace_id))

    def _seed_bin(self, confidence, total, failures, base=None):
        base = time.time() - 3600 if base is None else base
        for i in range(total):
            self._gate(confidence, base + i, f'trace{i}', ok_outcome=(i >= failures))

    def test_effective_confidence_defaults_to_the_constant_and_reflects_the_setting(self):
        self.assertEqual(serve._effective_safety_confidence(), serve.JEV_SAFETY_CONFIDENCE)
        serve._set_setting('jev_safety_confidence', '0.73')
        self.assertEqual(serve._effective_safety_confidence(), 0.73)

    def test_effective_confidence_rejects_out_of_range_values(self):
        serve._set_setting('jev_safety_confidence', '0.99')
        self.assertEqual(serve._effective_safety_confidence(), serve.JEV_SAFETY_CONFIDENCE,
                         'above the clamp must fall back to the safe constant')
        serve._set_setting('jev_safety_confidence', 'junk')
        self.assertEqual(serve._effective_safety_confidence(), serve.JEV_SAFETY_CONFIDENCE)

    def test_raises_the_bar_when_actions_at_it_succeed_below_target(self):
        # Threshold baseline 0.6 sits in the 0.60-0.70 bin; half of the 20
        # allowed actions it auto-approved failed -> 50% success at 90% target.
        self._seed_bin(0.65, 20, failures=10)
        new = serve._calibration_adjust_pass()
        self.assertEqual(new, 0.65, 'a reliability shortfall must raise the bar one step')
        self.assertEqual(serve._get_setting('jev_safety_confidence'), '0.65')
        with serve._db() as conn:
            row = conn.execute("SELECT COUNT(*) FROM action_log WHERE action = 'jev_calibration_adjust'").fetchone()[0]
        self.assertEqual(row, 1, 'the adjust must be audit-logged')

    def test_lowers_the_bar_when_actions_at_it_succeed_well_above_target(self):
        self._seed_bin(0.65, 10, failures=0)  # 100% success = over-target
        new = serve._calibration_adjust_pass()
        self.assertEqual(new, 0.55, 'reliability above target lets the bar come down one step')
        self.assertEqual(serve._get_setting('jev_safety_confidence'), '0.55')

    def test_dead_band_does_not_churn_the_bar(self):
        # 13/15 succeed (0.867) -- within the 0.05 dead-band of the 0.9 target.
        self._seed_bin(0.65, 15, failures=2)
        new = serve._calibration_adjust_pass()
        self.assertIsNone(new)
        self.assertIsNone(serve._get_setting('jev_safety_confidence'),
                          'inside the dead-band the bar must not move at all')

    def test_no_move_without_enough_scored_decisions(self):
        self._seed_bin(0.65, 3, failures=3)  # terrible rate, but only 3 samples
        new = serve._calibration_adjust_pass()
        self.assertIsNone(new, 'a handful of decisions must never move the safety bar')
        self.assertIsNone(serve._get_setting('jev_safety_confidence'))

    def test_raises_clamp_at_the_max_threshold(self):
        serve._set_setting('jev_safety_confidence', '0.9')
        self._seed_bin(0.95, 10, failures=10)  # all failed at the top bin
        new = serve._calibration_adjust_pass()
        self.assertEqual(new, 0.95, 'the raised bar must clamp at the max')
        self.assertEqual(serve._get_setting('jev_safety_confidence'), '0.95')
        # Already at max: another pass must not move (and not log a bogus adjust).
        self.assertIsNone(serve._calibration_adjust_pass())

    def test_gate_floors_track_the_live_threshold_but_risk_kinds_stay_fixed(self):
        serve._set_setting('jev_safety_confidence', '0.75')
        self.assertEqual(serve._escalation_floor('unresolved review requirement'), 0.75,
                         'the routine floor must track the live calibration threshold')
        self.assertEqual(serve._escalation_floor('brand new kind'), 0.75)
        self.assertEqual(serve._escalation_floor('blocked command'), 1.0,
                         "a human's own blocked verdict is never auto-approved")
        self.assertEqual(serve._escalation_floor('unsure safety decision'), 0.85,
                         'the risky kind keeps its own raised floor, fixed')


class JevQuorumChoiceSync(unittest.TestCase):
    """_jev_quorum_choice_sync: gap caught -- quorum
    sampling was only ever applied to safety gates, never to this module's
    own routine-but-consequential multi-way routing deciders (request lane,
    team, room, product, the peer-report worker picker, the escalation
    delegated-approval decision). Unlike the safety-specific async version,
    there's no 'a firm block is always trusted' asymmetry -- every option
    is equally worth getting right, so this resamples on ANY low-confidence
    result and resolves by plurality vote, not an allow/block rule."""

    def _mock_sample_sequence(self, choices):
        return unittest.mock.patch.object(serve, '_call_openrouter_decision_sync', side_effect=choices)

    def _answer(self, decision, confidence, cost=0.01):
        return {'answers': {'choice': {'choice': decision, 'confidence': confidence, 'probabilities': {}}},
                'usage': {'cost': cost}}

    def test_confident_choice_takes_exactly_one_sample(self):
        with self._mock_sample_sequence([self._answer('room_a', 0.9)]) as mock_call:
            decision, confidence, cost = serve._jev_quorum_choice_sync('i', {})
        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual((decision, confidence), ('room_a', 0.9))

    def test_low_confidence_resamples_up_to_quorum_sample_size(self):
        with self._mock_sample_sequence([self._answer('room_a', 0.4)] * serve.QUORUM_SAMPLE_SIZE) as mock_call:
            serve._jev_quorum_choice_sync('i', {})
        self.assertEqual(mock_call.call_count, serve.QUORUM_SAMPLE_SIZE)

    def test_plurality_vote_picks_the_option_most_samples_agreed_on(self):
        with self._mock_sample_sequence([
            self._answer('room_a', 0.4),  # ambiguous first sample -> triggers resampling
            self._answer('room_b', 0.85),
            self._answer('room_b', 0.5),
        ]):
            decision, confidence, cost = serve._jev_quorum_choice_sync('i', {})
        self.assertEqual(decision, 'room_b')  # 2 votes beats 1
        self.assertAlmostEqual(confidence, 0.85)  # the winning option's OWN strongest vote

    def test_three_way_tie_breaks_on_highest_confidence(self):
        with self._mock_sample_sequence([
            self._answer('room_a', 0.4),
            self._answer('room_b', 0.55),
            self._answer('room_c', 0.3),
        ]):
            decision, confidence, cost = serve._jev_quorum_choice_sync('i', {})
        self.assertEqual(decision, 'room_b')  # all 1 vote each -- highest confidence wins
        self.assertAlmostEqual(confidence, 0.55)

    def test_total_cost_sums_every_sample_actually_taken(self):
        with self._mock_sample_sequence([
            self._answer('room_a', 0.4, cost=0.01),
            self._answer('room_b', 0.5, cost=0.02),
            self._answer('room_a', 0.45, cost=0.03),
        ]):
            _decision, _confidence, cost = serve._jev_quorum_choice_sync('i', {})
        self.assertAlmostEqual(cost, 0.06)

    def test_a_failed_resample_is_skipped_not_fatal(self):
        with self._mock_sample_sequence([
            self._answer('room_a', 0.4),
            RuntimeError('network blip'),
            self._answer('room_a', 0.7),
        ]):
            decision, confidence, cost = serve._jev_quorum_choice_sync('i', {})
        self.assertEqual(decision, 'room_a')
        self.assertAlmostEqual(confidence, 0.7)

    def test_unreachable_classifier_on_the_first_call_fails_closed(self):
        with self._mock_sample_sequence([RuntimeError('down')]) as mock_call:
            decision, confidence, cost = serve._jev_quorum_choice_sync('i', {})
        self.assertEqual(mock_call.call_count, 1)
        self.assertIsNone(decision)
        # An unreachable classifier is zero trust, not full trust. The old
        # 1.0 value let a transport outage look maximally confident.
        self.assertEqual(confidence, 0.0)


class _ClassifyCommandJev(unittest.TestCase):
    # The shared command gate behind /api/execute and /api/pipeline. Same
    # confidence contract as the URL gates: low-confidence allow escalates.
    @classmethod
    def setUpClass(cls):
        serve.init_db()

    def _run(self, decision, confidence):
        async def go():
            return await serve._classify_command('echo hi', 'runs a test')
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'test-key'), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        return_value={'answers': {'choice': {'choice': decision, 'confidence': confidence, 'probabilities': {}}}}), \
             unittest.mock.patch.object(serve, '_send_escalation_email_sync') as mock_email, \
             unittest.mock.patch.object(serve, '_load_escalations', return_value={}), \
             unittest.mock.patch.object(serve, '_save_escalations') as mock_save:
            allowed, reason = asyncio.run(go())
        return allowed, reason, mock_save

    def test_confident_allow_runs(self):
        allowed, reason, _ = self._run('allow', 0.95)
        self.assertTrue(allowed)
        self.assertEqual(reason, 'allow')

    def test_low_confidence_allow_blocks_without_escalation(self):
        allowed, reason, save_called = self._run('allow', 0.4)
        self.assertFalse(allowed, 'low-confidence allow must not run the command')
        # The block reason is now actionable (names the confidence, the
        # threshold, the block categories, and what to do differently) --
        # the old bare "unsure (Jev confidence 0.xx), escalated" left agents
        # with a score and no fix path.
        self.assertIn('not confident enough', reason)
        self.assertIn('0.40', reason)
        save_called.assert_not_called()

    def test_block_blocks_without_escalation(self):
        allowed, reason, save_called = self._run('block', 0.99)
        self.assertFalse(allowed)
        save_called.assert_not_called()


class ResearchContentExecutor(unittest.TestCase):
    # Phase 3 slice 2: the research content executor runs the real crawl > save
    # > chat > Library-write pipeline on a background thread, then stashes its
    # result ({note, seenUrls}) for the next sim task_cycle pass. Hermetic test:
    # patch the loopback helper to return canned endpoint responses and assert
    # the executor's store call -- no live network, no real Jev/rate-limit.

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-research-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()
        self.stored = {}
        self.http_log = []

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_http(self, browse_pages, synth_reply=None, existing_skill=None):
        # Returns a mapped fake for serve._http_json.
        pages = list(browse_pages)  # [{url, text, links:[...], lastModified}]

        def fake(method, base, path, body=None, header=None, timeout=None):
            self.http_log.append((method, path))
            if path == '/api/browse':
                p = pages.pop(0) if pages else None
                if p is None:
                    return {'error': 'exhausted'}
                resp = {'allowed': True, 'url': p['url'], 'text': p.get('text', ''),
                        'links': p.get('links', []),
                        'lastModified': p.get('lastModified')}
                return resp
            if path == '/api/sandbox-save-page':
                return {'allowed': True, 'ok': True}
            if path == '/api/library/file' and method == 'GET':
                if existing_skill is None:
                    return {'error': 'not found'}
                return {'path': 'skills/x.md', 'content': existing_skill}
            if path == '/api/chat':
                if synth_reply is None:
                    return {'error': 'model failed'}
                return {'reply': synth_reply}
            if path == '/api/library/file' and method == 'POST':
                return '_raw = saved'
            return {'error': f'unexpected {method} {path}'}

        return fake

    def test_executor_crawl_save_chat_store(self):
        pages = [
            {'url': 'https://a.example', 'text': 'alpha unique content', 'links': [], 'lastModified': 0},
        ]
        # Seed a mid-tier so synthesis proceeds (the test DB's model_tiers is
        # empty by default; _mid_tier_slug correctly returns None otherwise).
        with serve._db() as conn:
            conn.execute("INSERT OR REPLACE INTO model_tiers (band, slug, name, price_per_m, chosen_at) "
                         "VALUES ('mid', 'test-mid-model', 'Test Mid', 0.0, 0)")
        import sim as _sim
        real_store = _sim._store_content_result
        _sim._store_content_result = lambda tid, r: self.stored.update({tid: r})
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(pages, synth_reply='# Weather\n\nFresh synthesis.'), create=False):
                topic = {'id': 't1', 'topic': 'weather data', 'startUrl': 'https://a.example',
                         'seenUrls': [], 'since': 0}
                task = {'id': 'task-1', 'room': 'observatory',
                        'research': {'topicId': 't1', 'since': 0}}
                serve._run_research_content({'researchTopics': [topic]}, 'ada', task, {})
        finally:
            _sim._store_content_result = real_store
        self.assertIn('task-1', self.stored, 'executor must store a completed result')
        result = self.stored['task-1']
        self.assertIn('collected 1 new page', result['note'])
        self.assertIn('https://a.example', result['seenUrls'])
        # The skill file write must have been attempted (library POST).
        posts = [p for (m, p) in self.http_log if m == 'POST' and p == '/api/library/file']
        self.assertEqual(len(posts), 1, 'a fresh skill synthesis must write via /api/library/file')

    def test_executor_dedups_already_collected_unchanged(self):
        # A page already in seenUrls and unchanged since `since` is NOT saved
        # again (the crawl's dedup), and with nothing new kept there is no
        # synthesis/library-write -- the note reflects "nothing new".
        pages = [
            {'url': 'https://b.example', 'text': 'beta content', 'links': [],
             'lastModified': 1000000},
        ]
        topic = {'id': 't1', 'topic': 'weather data',
                 'startUrl': 'https://b.example', 'seenUrls': ['https://b.example'],
                 'since': 2000000}  # page lastModified < since -> unchanged
        task = {'id': 'task-2', 'room': 'observatory',
                'research': {'topicId': 't1', 'since': 2000000}}
        import sim as _sim
        real_store = _sim._store_content_result
        _sim._store_content_result = lambda tid, r: self.stored.update({tid: r})
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(pages, synth_reply=None), create=False):
                serve._run_research_content({'researchTopics': [topic]}, 'ada', task, {})
        finally:
            _sim._store_content_result = real_store
        result = self.stored['task-2']
        self.assertIn('nothing new', result['note'])
        # No save of the unchanged page, no library write (synthesis skipped).
        saves = [p for (m, p) in self.http_log if p == '/api/sandbox-save-page']
        posts = [p for (m, p) in self.http_log if m == 'POST' and p == '/api/library/file']
        self.assertEqual(saves, [], 'an unchanged already-collected page must not be re-saved')
        self.assertEqual(posts, [], 'nothing kept -> no synthesis/library write')
