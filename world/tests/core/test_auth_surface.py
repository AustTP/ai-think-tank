"""Deny-by-default auth surface tests.

serve.py: every route requires a session or agent key UNLESS it appears in
AUTH_PUBLIC_PATHS (or is a static asset with a public extension). These tests
pin the classification so a route can never be accidentally public:

  1. the public list is an exact, curated set -- adding to it without
     updating EXPECTED_PUBLIC here fails CI;
  2. every registered route is classified: it is either on the public list
     or it 401s with no credentials (the "unclassified route" check);
  3. source/config files under world/ are no longer served to anonymous GETs.

Run:
  cd /Users/poole86/ai-village-template/world
  python3 -m pytest tests/test_auth_surface.py -q
"""
import os
import sys
import unittest

from fastapi.routing import APIRoute

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve

# The exact public surface. Every entry must be intentionally public: either
# it enforces its own credential in-handler (session, operator gate, agent
# key, or a token header), or it is part of the login surface / open UI reads.
EXPECTED_PUBLIC = (
    '/login', '/logout',
    '/api/rooms', '/api/teams', '/api/backlog',
    '/api/templates', '/api/avatar',
    '/api/escalation/resolve',
    '/api/chat',
    '/api/mail', '/api/reports', '/api/download',
    '/api/file-grants', '/api/app-grants',
    '/api/device/checkin',
    '/api/task-budget', '/api/budget', '/api/env/reload',
    '/api/ais', '/api/market', '/api/rss', '/api/ws',
    '/api/data-sources', '/api/credentials',
    '/api/sms',
    '/api/experiments',
    '/api/rule-proposals', '/api/success-proposals',
    # A2A Agent Card: public metadata only (name, skills, auth scheme). The
    # /a2a endpoint itself is NOT public; it requires a session or a
    # director/admin bearer key.
    '/.well-known/agent-card.json', '/.well-known/agent.json',
    # Inbound webhook intake. Not a village route: the handler requires the
    # X-Webhook-Key header (403 without it) and only appends to the JSONL
    # queue; a bad or missing key stores nothing.
    '/api/webhook/',
)

# Routes that must stay reachable without a session (login page + assets).
ALWAYS_PUBLIC_PATHS = ('/', '/index.html')

# Static extensions allowed without a session (the login shell's own assets).
PUBLIC_STATIC_EXTS = ('.js', '.css', '.html', '.png', '.jpg', '.jpeg',
                      '.gif', '.svg', '.ico', '.webp', '.woff', '.woff2', '.map')


def _all_route_paths():
    paths = set()
    for route in serve.app.routes:
        if isinstance(route, APIRoute):
            paths.add(route.path)
        else:  # Mount and everything else
            paths.add(getattr(route, 'path', '') or '/')
    return paths


class PublicListPinned(unittest.TestCase):
    def test_public_list_is_exactly_the_curated_set(self):
        # A new public route must be added HERE too, on purpose. If this
        # fails because you deliberately opened a route: add it to
        # EXPECTED_PUBLIC with a comment saying why it is safe.
        self.assertEqual(set(serve.AUTH_PUBLIC_PATHS), set(EXPECTED_PUBLIC))

    def test_static_extension_allowlist_is_pinned(self):
        self.assertEqual(set(serve.AUTH_PUBLIC_STATIC_EXTENSIONS), set(PUBLIC_STATIC_EXTS))


class EveryRouteClassified(unittest.TestCase):
    """The unclassified-route check: every route is either on the public list
    or it 401s with no credentials."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.client = TestClient(serve.app)

    def test_every_route_is_public_or_401s(self):
        unclassified = []
        for path in sorted(_all_route_paths()):
            if path in ALWAYS_PUBLIC_PATHS or serve._is_public_path(path):
                continue
            # Middleware runs before body parsing, so a dummy body is fine.
            r = self.client.post(path, json={})
            if r.status_code != 401:
                unclassified.append((path, r.status_code))
        self.assertEqual(
            unclassified, [],
            'these routes are neither public nor protected (expected 401) '
            'and must be classified: add them to AUTH_PUBLIC_PATHS or fix '
            'the middleware')


class NewlyProtectedRoutes(unittest.TestCase):
    """The six routes that the old protected-prefix allowlist left open."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.client = TestClient(serve.app)

    def _assert_401(self, method, path):
        r = self.client.request(method, path, json={})
        self.assertEqual(r.status_code, 401, f'{method} {path} must require auth')

    def test_review_escalate_is_protected(self):
        self._assert_401('POST', '/api/review/escalate')

    def test_escalation_status_is_protected(self):
        self._assert_401('GET', '/api/escalation/5')

    def test_tier_value_windows_is_protected(self):
        self._assert_401('GET', '/api/tier-value-windows')

    def test_tier_daily_log_is_protected(self):
        self._assert_401('GET', '/api/tier-daily-log')

    def test_catalog_price_history_is_protected(self):
        self._assert_401('GET', '/api/catalog-price-history')

    def test_adversarial_village_is_protected(self):
        self._assert_401('GET', '/api/adversarial-village')

    def test_player_email_credential_is_protected(self):
        # The historical gap: its own handler only rejects a caller who
        # SELF-identifies as an agent, so a credential-less request used to
        # reach it and overwrite the player's Gmail app-password.
        self._assert_401('POST', '/api/player-email/credential')


class PublicRoutesReachable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.client = TestClient(serve.app)

    def test_open_ui_reads_serve(self):
        self.assertEqual(self.client.get('/api/rooms').status_code, 200)
        self.assertEqual(self.client.get('/api/teams').status_code, 200)
        self.assertEqual(self.client.get('/api/backlog').status_code, 200)

    def test_login_surface_is_reachable(self):
        # Reaches the handler (not 401); the handler itself decides what to
        # render for an anonymous caller.
        for path in ('/', '/index.html'):
            self.assertNotEqual(self.client.get(path).status_code, 401, path)

    def test_escalation_resolve_reaches_handler(self):
        # Public by design (tappable from an email); no session -> handler
        # validates the token itself (422 on missing params, not 401).
        r = self.client.get('/api/escalation/resolve')
        self.assertNotEqual(r.status_code, 401)


class StaticMountLockedDown(unittest.TestCase):
    """world/ source and config are no longer served to anonymous GETs."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.client = TestClient(serve.app)

    def test_asset_extension_is_public(self):
        r = self.client.get('/js/world.js')
        # Served by the static mount (200) or not found (404) if the file
        # moved -- but never 401: assets must load before any session exists.
        self.assertIn(r.status_code, (200, 404))

    def test_source_and_config_are_401(self):
        for path in ('/grants.json', '/serve.py', '/tests/test_serve.py',
                     '/sandbox_egress_grants.json', '/api_services.json',
                     '/research_desk.json', '/watchlist.json'):
            self.assertEqual(self.client.get(path).status_code, 401, path)


if __name__ == '__main__':
    unittest.main()
