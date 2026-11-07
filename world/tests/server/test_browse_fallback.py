"""The read-only API-to-browser fallback on /api/browse.

Operator pitch #6, smallest slice: when the plain fetch of an
ALREADY-APPROVED url dies with 404/410, the same url is retried once
through the headless browser the render path already uses. These tests
pin the contract: only the same url, only on 404/410, only when
enabled, never on the VPN branches, and the response says when the
fallback fired. No real browser, no real network.
"""
import os
import sys
import unittest
import urllib.error
from unittest import mock

from fastapi.testclient import TestClient

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
import test_serve_helpers as _helpers  # noqa: E402


def setUpModule():
    _helpers.setUpModule()
    _helpers._AUTH_PATCHER = mock.patch.object(
        serve, '_valid_agent_key_presented', return_value=True)
    _helpers._AUTH_PATCHER.start()


def tearDownModule():
    _helpers._AUTH_PATCHER.stop()
    _helpers.tearDownModule()


URL = 'https://example.com/api/widgets'


def _browse_patches(fetch_side_effect, rendered=None, **overrides):
    """Build the patch set for a /api/browse call. Returns (patches, mocks):
    `mock.patch.multiple(serve, **patches)` inside the test, `mocks` for
    assertions. The rendered fetch is ALWAYS mocked so no test ever
    reaches the real internet."""
    rendered_mock = (mock.Mock(return_value=rendered) if rendered is not None
                     else mock.Mock(
                         side_effect=AssertionError('real render reached')))
    patches = {
        'check_rate_limit': mock.Mock(return_value=True),
        'OPENROUTER_API_KEY': 'sk-test',
        '_page_budget_exhausted': mock.Mock(return_value=False),
        '_is_safe_public_host': mock.Mock(return_value=True),
        '_is_allowlisted_host': mock.Mock(return_value=True),
        '_is_standing_safe_host': mock.Mock(return_value=False),
        'verify_agent_key': mock.Mock(return_value=True),
        '_fetch_page_sync': fetch_side_effect,
        '_fetch_rendered_page_sync': rendered_mock,
        '_jev_re_gate_redirect_final':
            mock.AsyncMock(return_value=(True, 'ok')),
        '_accrue_page_request': mock.Mock(),
        'wrap_external_content':
            mock.Mock(return_value=('w', 'n', 't', 'm')),
    }
    patches.update(overrides)
    return patches, {'rendered': rendered_mock}


def _post(body, patches):
    with mock.patch.multiple(serve, **patches):
        client = TestClient(serve.app)
        return client.post('/api/browse', json=body,
                           headers={'X-Agent-Key': 'k'})


class BrowserFallback(unittest.TestCase):
    def test_404_retries_same_url_through_the_browser(self):
        seen = {}

        def fake_fetch(url, proxy=None):
            seen['url'] = url
            raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)

        patches, mocks = _browse_patches(
            fake_fetch,
            ('https://example.com/widgets', 'rendered body text',
             ['https://example.com/widgets/list']))
        resp = _post({'url': URL, 'purpose': 't'}, patches)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body.get('allowed'), body)
        self.assertTrue(body.get('viaBrowserFallback'))
        self.assertEqual(body.get('text'), 'rendered body text')
        self.assertEqual(seen['url'], URL)  # the SAME approved url, once
        mocks['rendered'].assert_called_once_with(URL)

    def test_410_also_falls_back_but_500_does_not(self):
        def gone(url, proxy=None):
            raise urllib.error.HTTPError(url, 410, 'Gone', {}, None)

        patches, _ = _browse_patches(gone, ('https://example.com/', 'g', []))
        resp = _post({'url': URL}, patches)
        self.assertTrue(resp.json().get('viaBrowserFallback'))

        def server_error(url, proxy=None):
            raise urllib.error.HTTPError(url, 500, 'boom', {}, None)

        patches, _ = _browse_patches(server_error,
                                     ('https://example.com/', 'x', []))
        resp = _post({'url': URL}, patches)
        body = resp.json()
        self.assertFalse(body.get('viaBrowserFallback'))
        self.assertIn('could not be loaded', body.get('error', ''))

    def test_explicit_render_request_never_double_falls_back(self):
        # The caller already asked for the browser; a 404 there is an
        # honest answer, not a signal to retry the same thing again.
        def not_found(url, proxy=None):
            raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)

        patches, mocks = _browse_patches(not_found, None)
        resp = _post({'url': URL, 'render': True}, patches)
        self.assertFalse(resp.json().get('viaBrowserFallback'))
        # The render branch's own call is the ONE render; the fallback
        # wrapper must not stack a second one on top of it.
        self.assertEqual(mocks['rendered'].call_count, 1)

    def test_kill_switch_stops_the_fallback(self):
        def not_found(url, proxy=None):
            raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)

        patches, _ = _browse_patches(not_found, None,
                                     BROWSER_FALLBACK_ENABLED=False)
        resp = _post({'url': URL}, patches)
        body = resp.json()
        self.assertFalse(body.get('viaBrowserFallback'))
        self.assertIn('could not be loaded', body.get('error', ''))

    def test_fallback_failure_reports_the_original_problem(self):
        def not_found(url, proxy=None):
            raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)

        patches, _ = _browse_patches(
            not_found, None,
            _fetch_rendered_page_sync=mock.Mock(
                side_effect=RuntimeError('browser died too')))
        resp = _post({'url': URL}, patches)
        body = resp.json()
        self.assertTrue(body.get('allowed'))
        self.assertIn('could not be loaded', body.get('error', ''))
        self.assertFalse(body.get('viaBrowserFallback'))


if __name__ == '__main__':
    unittest.main()
