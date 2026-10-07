"""Tests for the Higgsfield AI image/video generation wiring (2026-10-06).

Agents get two real, metered tools (generate_image / generate_video) that
run through one authenticated, asynchronous request lifecycle: estimate the
cost up-front via the real /estimate endpoint (a verified number, never a
guess), submit, poll to a terminal state, accrue the estimated cost on
SUCCESS only (failed/nsfw/canceled requests are not charged by Higgsfield),
and write a durable manifest into the Library. Hermetic: every test patches
the network call (_higgsfield_call) and the ledger, so nothing touches the
real API or the spend cap.
"""
import os
import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve

# Always-on regardless of what the operator's .env has: the helper and tool
# code paths are tested with both halves patched in, never dependent on the
# real config.
CONFIG = {
    'HIGGSFIELD_API_KEY_ID': 'test-id',
    'HIGGSFIELD_API_KEY_SECRET': 'test-secret',
}


class HiggsfieldCall(unittest.TestCase):
    def test_call_requires_both_key_halves(self):
        with mock.patch.object(serve, 'HIGGSFIELD_API_KEY_ID', ''), \
             mock.patch.object(serve, 'HIGGSFIELD_API_KEY_SECRET', 'sec'):
            data, error = serve._higgsfield_call('GET', '/requests/x/status')
        self.assertIsNone(data)
        self.assertIn('not configured', error)

    def test_call_sends_the_two_part_key_auth_header(self):
        captured = {}

        class FakeResp:
            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0):
            captured['header'] = req.get_header('Authorization')
            captured['url'] = req.full_url
            captured['method'] = req.get_method()
            return FakeResp()

        with mock.patch('serve.urllib.request.urlopen', side_effect=fake_urlopen), \
             mock.patch.object(serve, 'HIGGSFIELD_API_KEY_ID', 'id1'), \
             mock.patch.object(serve, 'HIGGSFIELD_API_KEY_SECRET', 'sec1'):
            data, error = serve._higgsfield_call('GET', '/requests/x/status')
        self.assertIsNone(error)
        self.assertEqual(data, {'ok': True})
        self.assertEqual(captured['header'], 'Key id1:sec1')
        self.assertEqual(captured['url'], 'https://api.higgsfield.ai/requests/x/status')

    def test_call_surfaces_http_errors(self):
        with mock.patch('serve.urllib.request.urlopen',
                        side_effect=_http_error(401, '{"error":"bad creds"}')), \
             mock.patch.object(serve, 'HIGGSFIELD_API_KEY_ID', 'id'), \
             mock.patch.object(serve, 'HIGGSFIELD_API_KEY_SECRET', 'sec'):
            data, error = serve._higgsfield_call('POST', '/estimate/x', {'prompt': 'p'})
        self.assertIsNone(data)
        self.assertIn('401', error)
        self.assertIn('bad creds', error)


class HiggsfieldEstimate(unittest.TestCase):
    def test_parses_real_usd(self):
        with mock.patch.object(serve, '_higgsfield_call',
                               return_value=({'credits': '1.500', 'usd': '0.094'}, None)):
            self.assertEqual(serve._higgsfield_estimate_usd('x', {'prompt': 'p'}), 0.094)

    def test_returns_none_on_error(self):
        with mock.patch.object(serve, '_higgsfield_call', return_value=(None, 'boom')):
            self.assertIsNone(serve._higgsfield_estimate_usd('x', {'prompt': 'p'}))

    def test_returns_none_on_garbage(self):
        with mock.patch.object(serve, '_higgsfield_call', return_value=({'usd': 'nope'}, None)):
            self.assertIsNone(serve._higgsfield_estimate_usd('x', {'prompt': 'p'}))


class HiggsfieldPoll(unittest.TestCase):
    def test_polls_until_terminal(self):
        with mock.patch.object(serve, '_higgsfield_call',
                               side_effect=[({'status': 'queued'}, None),
                                            ({'status': 'queued'}, None),
                                            ({'status': 'completed', 'request_id': 'r',
                                              'images': [{'url': 'u'}]}, None)]):
            data, error = serve._higgsfield_poll('r', timeout=60, interval=0)
        self.assertIsNone(error)
        self.assertEqual(data['status'], 'completed')

    def test_returns_error_when_terminal_is_failed(self):
        with mock.patch.object(serve, '_higgsfield_call',
                               return_value=({'status': 'failed'}, None)):
            data, error = serve._higgsfield_poll('r', timeout=60, interval=0)
        self.assertIsNone(error)
        self.assertEqual(data['status'], 'failed')

    def test_times_out_without_a_terminal_state(self):
        with mock.patch.object(serve, '_higgsfield_call',
                               return_value=({'status': 'queued'}, None)), \
             mock.patch.object(serve.time, 'sleep'):
            _data, error = serve._higgsfield_poll('r', timeout=0.05, interval=0)
        self.assertIn('timed out', error)


class HiggsfieldGenerate(unittest.TestCase):
    def _submit(self, **over):
        body = {'status': 'queued', 'request_id': 'req-1',
                'status_url': 'https://api.higgsfield.ai/requests/req-1/status',
                'cancel_url': 'https://api.higgsfield.ai/requests/req-1/cancel'}
        body.update(over)
        return (body, None)

    def test_prompt_is_required(self):
        self.assertEqual(serve._higgsfield_generate('image', {}), 'prompt is required')
        self.assertEqual(serve._higgsfield_generate('video', {'prompt': '  '}), 'prompt is required')

    def _wf(self, data, usd=0.094):
        return {'ok': True, 'data': data, 'usd': usd, 'ids': {},
                'textForModel': '<<<EXTERNAL_DATA', 'modelInstruction': ''}

    def test_image_success_uses_the_workflow_and_manifests(self):
        completed = {'status': 'completed', 'request_id': 'req-1',
                     'images': [{'url': 'https://cdn.example.com/i.jpg'}]}
        with mock.patch.object(serve, '_api_execute', return_value=self._wf(completed, 0.094)), \
             mock.patch.object(serve, '_higgsfield_log_manifest') as mani:
            out = serve._higgsfield_generate('image', {'prompt': 'an alpine lake'}, 'ada', 'key')
        self.assertIn('https://cdn.example.com/i.jpg', out)
        self.assertIn('$0.094', out)
        mani.assert_called_once()

    def test_video_success_surfaces_the_url(self):
        done = {'status': 'completed', 'request_id': 'req-2',
                'video': {'url': 'https://cdn.example.com/v.mp4'}}
        with mock.patch.object(serve, '_api_execute', return_value=self._wf(done, 0.300)), \
             mock.patch.object(serve, '_higgsfield_log_manifest'):
            out = serve._higgsfield_generate('video', {'prompt': 'sunset drive'}, 'ada', 'key')
        self.assertIn('https://cdn.example.com/v.mp4', out)

    def test_failed_request_is_never_charged(self):
        failed = {'status': 'failed', 'request_id': 'req-1'}
        with mock.patch.object(serve, '_api_execute', return_value=self._wf(failed, 0.094)), \
             mock.patch.object(serve, '_higgsfield_log_manifest') as mani:
            out = serve._higgsfield_generate('image', {'prompt': 'x'}, 'ada', 'key')
        self.assertIn('did not complete', out)
        self.assertIn('not charged', out)
        mani.assert_not_called()

    def test_nsfw_request_is_never_charged(self):
        nsfw = {'status': 'nsfw', 'request_id': 'req-1'}
        with mock.patch.object(serve, '_api_execute', return_value=self._wf(nsfw, 0.094)), \
             mock.patch.object(serve, '_higgsfield_log_manifest') as mani:
            out = serve._higgsfield_generate('video', {'prompt': 'x'}, 'ada', 'key')
        self.assertIn('nsfw', out)
        mani.assert_not_called()

    def test_missing_estimate_still_generates_but_charges_nothing(self):
        completed = {'status': 'completed', 'request_id': 'req-1',
                     'images': [{'url': 'https://cdn.example.com/i.jpg'}]}
        with mock.patch.object(serve, '_api_execute', return_value=self._wf(completed, None)), \
             mock.patch.object(serve, '_higgsfield_log_manifest'):
            out = serve._higgsfield_generate('image', {'prompt': 'x'}, 'ada', 'key')
        self.assertIn('https://cdn.example.com/i.jpg', out)
        self.assertNotIn('billed to the think tank', out)

    def test_video_body_clamps_and_validates_params(self):
        captured = {}
        with mock.patch.object(serve, '_api_execute', return_value=self._wf(
                {'status': 'completed', 'request_id': 'r',
                 'video': {'url': 'https://cdn.example.com/v.mp4'}}, 0.2)) as ae, \
             mock.patch.object(serve, '_higgsfield_log_manifest'):
            serve._higgsfield_generate('video',
                                       {'prompt': 'sunset', 'duration': 20,
                                        'aspect_ratio': '1:1', 'sound': 'off'}, 'ada', 'key')
        captured['path'] = ae.call_args.kwargs['workflow_params']['endpoint']
        captured['body'] = ae.call_args.args[4]
        self.assertEqual(captured['path'], serve._HIGGSFIELD_VIDEO_ENDPOINT)
        self.assertEqual(captured['body']['prompt'], 'sunset')
        self.assertEqual(captured['body']['duration'], 15, 'duration clamps to the documented max')
        self.assertEqual(captured['body']['aspect_ratio'], '1:1')
        self.assertEqual(captured['body']['sound'], 'off')

    def test_image_body_is_just_the_prompt(self):
        captured = {}
        with mock.patch.object(serve, '_api_execute', return_value=self._wf(
                {'status': 'completed', 'request_id': 'r',
                 'images': [{'url': 'https://cdn.example.com/i.jpg'}]}, 0.1)) as ae, \
             mock.patch.object(serve, '_higgsfield_log_manifest'):
            serve._higgsfield_generate('image', {'prompt': 'lake'}, 'ada', 'key')
        captured['path'] = ae.call_args.kwargs['workflow_params']['endpoint']
        captured['body'] = ae.call_args.args[4]
        self.assertEqual(captured['path'], serve._HIGGSFIELD_IMAGE_ENDPOINT)
        self.assertEqual(captured['body'], {'prompt': 'lake'})


class HiggsfieldTools(unittest.TestCase):
    def test_tool_definitions_are_well_formed(self):
        for tool in (serve._HIGGSFIELD_IMAGE_TOOL, serve._HIGGSFIELD_VIDEO_TOOL):
            fn = tool['function']
            self.assertIn(fn['name'], ('generate_image', 'generate_video'))
            self.assertTrue(fn['description'])
            required = fn['parameters']['required']
            self.assertIn('prompt', required)

    def test_tools_present_in_ask_list_exactly_when_configured(self):
        names = {t['function']['name'] for t in serve.AGENT_ASK_TOOLS}
        configured = bool(serve.HIGGSFIELD_API_KEY_ID and serve.HIGGSFIELD_API_KEY_SECRET)
        self.assertEqual(('generate_image' in names) and ('generate_video' in names), configured,
                         'the tools must appear iff the two-part key is configured')

    def test_web_tools_executor_dispatches_generate_tools(self):
        ex = serve._make_web_tools_executor('ben', 'key-123')
        with mock.patch.object(serve, '_higgsfield_generate', return_value='ok') as gen:
            out = ex('generate_image', {'prompt': 'x'})
        self.assertEqual(out, 'ok')
        gen.assert_called_once_with('image', {'prompt': 'x'}, 'ben', 'key-123')
        with mock.patch.object(serve, '_higgsfield_generate', return_value='ok') as gen:
            out = ex('generate_video', {'prompt': 'y'})
        self.assertEqual(out, 'ok')
        gen.assert_called_once_with('video', {'prompt': 'y'}, 'ben', 'key-123')


def _http_error(code, body):
    import urllib.error
    class FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            self.code = code
            self._body = body.encode('utf-8')

        def read(self):
            return self._body

    def raiser(*a, **k):
        raise FakeHTTPError()
    return raiser


if __name__ == '__main__':
    unittest.main()