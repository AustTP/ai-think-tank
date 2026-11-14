"""Outbound model-request secret masking (2026-11-13, operator direction).

Free-tier providers may log or train on request bodies (Numes declares a
per-endpoint data policy; Google's free Gemini tier trains on content).
Every long, secret-named env VALUE must be masked out of chat/decision
payloads before the wire -- one pasted credential in one prompt must never
sit in a provider's logs. The network and the live DB are patched into a
temp dir.

Run:
  cd /Users/poole86/ai-village-template/world
  python3 -m unittest tests.core.test_model_secret_masking -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve  # noqa: E402

_TMP = None
_PATCHER = None

SECRET = 'sk-fake-really-long-credential-value-0123456789abcdef'
OTHER = 'another-fake-long-token-value-abcdefghijklmnopqrstuv'
FAKE_ENV = {
    'MY_API_KEY': SECRET,
    'OTHER_TOKEN': OTHER,
    # Not masked: short, numeric, or not a secret-shaped name.
    'PORT_NUM': '8080',
    'EDITOR': 'vim',
    'SHORT_KEY': 'tiny',
}


def setUpModule():
    global _TMP, _PATCHER
    _TMP = tempfile.mkdtemp(prefix='think-tank-masking-')
    _PATCHER = unittest.mock.patch.object(serve, 'DB_PATH',
                                          os.path.join(_TMP, 'test.db'))
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP, ignore_errors=True)


def _fresh_pairs():
    serve._model_mask_cache.update(at=0.0, pairs=())
    with unittest.mock.patch.object(serve, '_load_env',
                                    lambda key=None: FAKE_ENV):
        return serve._model_secret_pairs()


class SecretPairTests(unittest.TestCase):
    def tearDown(self):
        serve._model_mask_cache.update(at=0.0, pairs=())

    def test_only_long_secret_named_values(self):
        pairs = _fresh_pairs()
        placeholders = {placeholder for _value, placeholder in pairs}
        self.assertEqual(placeholders,
                         {'[REDACTED:MY_API_KEY]', '[REDACTED:OTHER_TOKEN]'})

    def test_longest_first_so_prefixes_cannot_leak(self):
        pairs = _fresh_pairs()
        lengths = [len(value) for value, _placeholder in pairs]
        self.assertEqual(lengths, sorted(lengths, reverse=True))

    def test_cache_survives_within_ttl(self):
        _fresh_pairs()
        with unittest.mock.patch.object(serve, '_load_env',
                                        lambda key=None: {}):
            pairs = serve._model_secret_pairs()
        self.assertNotEqual(pairs, [])


class MaskPayloadTests(unittest.TestCase):
    def tearDown(self):
        serve._model_mask_cache.update(at=0.0, pairs=())

    def test_masks_nested_strings_and_lists(self):
        _fresh_pairs()
        body = {'messages': [{'role': 'user',
                              'content': f'key is {SECRET} ok'}],
                'tools': [{'description': OTHER}]}
        out = serve._mask_model_secrets(body)
        self.assertNotIn(SECRET, json.dumps(out))
        self.assertNotIn(OTHER, json.dumps(out))
        self.assertIn('[REDACTED:MY_API_KEY]', out['messages'][0]['content'])

    def test_leaves_clean_payload_untouched(self):
        _fresh_pairs()
        body = {'messages': [{'role': 'user', 'content': 'hello'}]}
        self.assertEqual(serve._mask_model_secrets(body), body)

    def test_wired_into_chat_transport(self):
        _fresh_pairs()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured['body'] = req.data.decode('utf-8')
            return '{"choices":[{"message":{"content":"ok"}}],"usage":{"cost":0}}'

        spec = {'is_custom': True, 'free': True, 'provider': 'freepool-x',
                'model': 'x/model', 'base_url': 'https://x.example/v1/chat',
                'api_key_env': 'X_KEY', 'service': 'x', 'pool_key': 'x',
                'name': 'x', 'band': 'low', 'send_reasoning': False}
        with unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                        fake_urlopen), \
            unittest.mock.patch.object(serve, '_note_free_pool_call',
                                       lambda provider, result: None), \
            unittest.mock.patch.object(serve, '_record_model_latency',
                                       lambda slug, ms: None):
            serve._openrouter_chat_post(
                {'model': 'x/model',
                 'messages': [{'role': 'user',
                               'content': f'run with {SECRET}'}]},
                provider=spec)
        self.assertNotIn(SECRET, captured['body'])
        self.assertIn('[REDACTED:MY_API_KEY]', captured['body'])


def _chat_post_capturing(spec, body=None):
    """Run one _openrouter_chat_post through a fake wire and return the
    parsed request payload the transport actually serialized."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['payload'] = json.loads(req.data.decode('utf-8'))
        return '{"choices":[{"message":{"content":"ok"}}],"usage":{"cost":0}}'

    with unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                    fake_urlopen), \
        unittest.mock.patch.object(serve, '_note_free_pool_call',
                                   lambda provider, result: None), \
        unittest.mock.patch.object(serve, '_record_model_latency',
                                   lambda slug, ms: None):
        serve._openrouter_chat_post(
            body or {'model': spec['model'],
                     'messages': [{'role': 'user', 'content': 'hello'}]},
            provider=spec)
    return captured['payload']


class DataPolicyPinTests(unittest.TestCase):
    """Paid lanes carry provider.data_collection=deny on the hosts that
    understand OpenRouter's provider preference object (2026-11-13,
    operator direction: many paid picks are Chinese-hosted and their
    training posture is unknown). Merge, never clobber."""

    def test_openrouter_paid_carries_deny(self):
        spec = {'is_custom': False, 'free': False, 'model': 'z-ai/glm-5.3-flash',
                'send_reasoning': False}
        payload = _chat_post_capturing(spec)
        self.assertEqual(payload.get('provider', {}).get('data_collection'),
                         'deny')

    def test_straitly_host_carries_deny(self):
        spec = {'is_custom': True, 'free': False, 'provider': 'straitly',
                'model': 'qwen/qwen3.7-flash',
                'base_url': 'https://api.straitly.ai/v1/chat/completions',
                'api_key_env': 'STRAITLY_API_KEY', 'send_reasoning': False}
        payload = _chat_post_capturing(spec)
        self.assertEqual(payload.get('provider', {}).get('data_collection'),
                         'deny')

    def test_other_custom_hosts_untouched(self):
        spec = {'is_custom': True, 'free': True, 'provider': 'freepool-x',
                'model': 'x/model',
                'base_url': 'https://x.example/v1/chat',
                'api_key_env': 'X_KEY', 'service': 'x', 'pool_key': 'x',
                'name': 'x', 'band': 'low', 'send_reasoning': False}
        payload = _chat_post_capturing(spec)
        self.assertNotIn('provider', payload)

    def test_existing_provider_prefs_merge_not_clobber(self):
        spec = {'is_custom': False, 'free': False, 'model': 'z-ai/glm-5.3-flash',
                'send_reasoning': False}
        payload = _chat_post_capturing(
            spec, body={'model': 'z-ai/glm-5.3-flash',
                        'messages': [{'role': 'user', 'content': 'hi'}],
                        'provider': {'order': ['wafer']}})
        self.assertEqual(payload['provider'].get('order'), ['wafer'])
        self.assertEqual(payload['provider'].get('data_collection'), 'deny')
