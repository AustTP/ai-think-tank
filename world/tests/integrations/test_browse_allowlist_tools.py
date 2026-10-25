"""Player-vetted browse allowlist: a small set of domains that
skip Jev's classify+escalate round trip for /api/browse entirely, because the
PLAYER already vetted them -- everything else still goes through full Jev
policy-as-code, unchanged. Real request after a DreyX.com investigation got
blocked mid-run: Jev's classification confidence for the SAME url swung
across runs (one run: clean allow; the next: 'escalated_unsure' at 0.56
confidence, denied by the director escalation), so a site the player had
already decided was fine could still get randomly blocked.

DB-isolated (setUpModule below) since /api/browse's every path calls
log_action, a real DB write.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-browse-allowlist-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        # BROWSE_TRAIL_PATH is a module-level constant computed once from the
        # REAL THINK_TANK_DIR at import time -- patching THINK_TANK_DIR alone does
        # NOT recompute it, so any test exercising a confident Jev allow
        # without explicitly mocking record_browse_success would otherwise
        # write a real browse_trail.json into the actual project directory
        # (running this exact suite).
        BROWSE_TRAIL_PATH=os.path.join(_TMP_DIR, 'browse_trail.json'),
        # The CLI-run Laya standby appends a provider to every decision chain
        # when enabled (it IS enabled in the project .env, which serves loads).
        # Hermetic modules must pin it off so decision chains stay single-slug
        # and the multi-slug circuit breaker never arms in the test process
        # (arm + leak would poison later modules with open breakers).
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)
def _fake_db_ctx(rows):
    """A stand-in for `with serve._db() as conn:` whose execute() returns
    `rows` from fetchall() -- used by the team_status note query tests."""
    conn = unittest.mock.MagicMock()
    conn.execute.return_value.fetchall.return_value = list(rows)
    ctx = unittest.mock.MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    return ctx


class AllowlistToolExecutor(unittest.TestCase):
    """The grant_allowlist/revoke_allowlist AGENT TOOLS in the shared web-tools
    executor (_make_web_tools_executor). The executor only FORWARDS with the
    caller's own key; the endpoint's _operator_gate decides authority. These
    tests pin the forward shape (path/body/key), the success message, and that
    a server refusal is surfaced to the model verbatim -- never swallowed."""

    def _drive(self, tool, args, http_result):
        seen = {}

        def fake_http(method, base, path, body=None, header=None, timeout=30):
            seen.update(method=method, path=path, body=body, header=header)
            return http_result

        with unittest.mock.patch.object(serve, '_http_json', side_effect=fake_http):
            execute_tool = serve._make_web_tools_executor('theo', 'theo-key-1')
            out = execute_tool(tool, args)
        return out, seen

    def test_grant_forwards_to_the_grant_endpoint_with_the_callers_key(self):
        out, seen = self._drive('grant_allowlist',
                                {'host': 'X.Example', 'purpose': 'quotes'},
                                {'ok': True, 'host': 'x.example', 'granted': True,
                                 'message': 'x.example is now on the allowlist.'})
        self.assertEqual(seen['method'], 'POST')
        self.assertEqual(seen['path'], '/api/allowlist/grant')
        self.assertEqual(seen['body'], {'agentId': 'theo', 'host': 'X.Example',
                                        'purpose': 'quotes'})
        # The caller's OWN key rides the forward -- the endpoint authenticates
        # the real agent, never a bare server-side loopback.
        self.assertEqual(seen['header'], 'theo-key-1')
        self.assertIn('x.example is now on the allowlist.', out)
        self.assertIn('granted', out)

    def test_revoke_forwards_to_the_revoke_endpoint(self):
        out, seen = self._drive('revoke_allowlist',
                                {'host': 'x.example', 'purpose': 'untrusted now'},
                                {'ok': True, 'host': 'x.example', 'revoked': True,
                                 'message': 'x.example removed from the allowlist.'})
        self.assertEqual(seen['path'], '/api/allowlist/revoke')
        self.assertEqual(seen['body']['host'], 'x.example')
        self.assertIn('x.example removed from the allowlist.', out)
        self.assertIn('revoked', out)

    def test_server_refusal_is_surfaced_to_the_model(self):
        # An ordinary agent gets 401 from the endpoint -- that refusal must
        # REACH the model (the tool description promises the server refuses),
        # not vanish into a cheerful success string.
        out, _seen = self._drive('grant_allowlist', {'host': 'x.example', 'purpose': 'p'},
                                 {'error': 'Unauthorized'})
        self.assertIn('Could not grant the allowlist host: Unauthorized', out)

    def test_non_dict_response_is_reported_not_crashed(self):
        out, _seen = self._drive('revoke_allowlist', {'host': 'x.example', 'purpose': 'p'},
                                 None)
        self.assertIn('unexpected response', out)


