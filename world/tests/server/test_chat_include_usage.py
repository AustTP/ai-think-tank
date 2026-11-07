"""The includeUsage option on /api/chat.

Opt-in, already-authenticated callers may read back what the endpoint
already accrued and logged: the call's cost AND the provider-reported
token breakdown. cached_tokens is how provider-side prefix caching is
MEASURED (the provider's own KV-cache accounting), so the benchmark can
show hit rates instead of guessing. Default callers keep the reply-only
contract; nothing here is inferred -- absent fields stay absent.
"""
import os
import sys
import unittest
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


def _openrouter_payload():
    return {
        'choices': [{'message': {'content': 'a reply'}, 'finish_reason': 'stop'}],
        'usage': {
            'cost': 0.0025,
            'prompt_tokens': 1200,
            'completion_tokens': 40,
            'prompt_tokens_details': {'cached_tokens': 900,
                                      'cache_write_tokens': 0},
        },
    }


class IncludeUsage(unittest.TestCase):
    def setUp(self):
        self._patches = mock.patch.multiple(
            serve,
            verify_agent_key=mock.Mock(return_value=True),
            OPENROUTER_API_KEY='sk-test',
            credits_pause_active=mock.Mock(return_value=False),
            _call_openrouter_sync=mock.Mock(return_value=_openrouter_payload()),
            log_action=mock.Mock(),
            check_rate_limit=mock.Mock(return_value=True),
        )
        self._patches.start()
        self.client = TestClient(serve.app)

    def tearDown(self):
        self._patches.stop()

    def _chat(self, body):
        return self.client.post('/api/chat', json=body,
                                headers={'X-Agent-Key': 'k'})

    def test_default_callers_keep_the_reply_only_contract(self):
        resp = self._chat({'model': 'm', 'agentId': 'theo',
                           'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.json().keys()), {'reply'})
        served = serve._call_openrouter_sync
        body = served.call_args[0][1]
        self.assertNotIn('includeUsage', body)  # not forwarded upstream

    def test_include_usage_returns_cost_and_token_breakdown(self):
        resp = self._chat({'model': 'm', 'agentId': 'theo',
                           'includeUsage': True,
                           'messages': [{'role': 'user', 'content': 'hi'}]})
        body = resp.json()
        self.assertEqual(body['reply'], 'a reply')
        usage = body['usage']
        self.assertEqual(usage['cost'], 0.0025)
        self.assertEqual(usage['prompt_tokens'], 1200)
        self.assertEqual(usage['completion_tokens'], 40)
        self.assertEqual(usage['cached_tokens'], 900)
        self.assertEqual(usage['cache_write_tokens'], 0)

    def test_unpriced_call_reports_none_not_zero(self):
        payload = _openrouter_payload()
        del payload['usage']['cost']
        payload['usage']['prompt_tokens_details'] = {}
        serve._call_openrouter_sync = mock.Mock(return_value=payload)
        resp = self._chat({'model': 'm', 'agentId': 'theo',
                           'includeUsage': True,
                           'messages': [{'role': 'user', 'content': 'hi'}]})
        usage = resp.json()['usage']
        self.assertIsNone(usage['cost'])
        self.assertNotIn('cached_tokens', usage)


if __name__ == '__main__':
    unittest.main()
