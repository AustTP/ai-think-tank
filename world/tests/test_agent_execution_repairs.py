import json
import os
import sys
import tempfile
import unittest
import urllib.request
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import content
import serve


class ExecutionRepairs(unittest.TestCase):
    def test_shell_fence_excludes_prose_and_preserves_embedded_markdown(self):
        reply = "Here is the command:\n```bash\ncat > brief.md << 'EOF'\n```python\nprint(1)\n```\nEOF\n```\nThis writes the brief."
        command = content._extract_shell_command(reply)
        self.assertTrue(command.startswith('cat >'))
        self.assertTrue(command.endswith('EOF'))
        self.assertIn('```python', command)
        self.assertNotIn('Here is', command)

    def test_raw_explanations_and_multiple_shell_blocks_rejected(self):
        for reply in ('It looks like the file is missing.', 'The probe output indicates a problem.',
                      '```sh\necho a\n```\n```sh\necho b\n```'):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                content._extract_shell_command(reply)

    def test_provider_error_recovers_once_on_another_provider(self):
        req = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',
                                     data=json.dumps({'model': 'test', 'max_tokens': 20}).encode())
        failure = {'provider': 'DeepInfra', 'choices': [{'finish_reason': 'error'}]}
        success = {'choices': [{'finish_reason': 'stop', 'message': {'content': 'OK'}}]}
        with patch.object(serve, '_urlopen_with_resilience', side_effect=[json.dumps(failure), json.dumps(success)]) as post, \
                patch.object(serve, '_think_tank_spend_cap_exceeded', return_value=False):
            self.assertEqual(serve._read_openrouter_response(req), success)
            body = json.loads(post.call_args_list[1].args[0].data)
            self.assertEqual(body['provider']['ignore'], ['deepinfra'])
            self.assertEqual(body['model'], 'test')
            self.assertEqual(body['max_tokens'], 20)

    def test_repeated_provider_error_is_not_a_success(self):
        req = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions', data=b'{}')
        failure = json.dumps({'error': {'message': 'Provider failed'}})
        with patch.object(serve, '_urlopen_with_resilience', return_value=failure) as post, \
                patch.object(serve, '_think_tank_spend_cap_exceeded', return_value=False), \
                self.assertRaises(RuntimeError):
            serve._read_openrouter_response(req)
        self.assertEqual(post.call_count, 2)

    def test_prototype_copy_excludes_git_and_replaces_readonly_mirror(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(serve, 'AGENTS_DIR', os.path.join(folder, 'agents')):
            source = os.path.join(folder, 'sandbox')
            os.makedirs(os.path.join(source, '.git'))
            with open(os.path.join(source, 'brief.md'), 'w') as f:
                f.write('Finding')
            serve.sync_prototypes('ada', source)
            dest = os.path.join(serve.AGENTS_DIR, 'ada', 'prototypes')
            self.assertFalse(os.path.exists(os.path.join(dest, '.git')))
            os.chmod(os.path.join(dest, 'brief.md'), 0o444)
            serve.sync_prototypes('ada', source)
            self.assertTrue(os.path.isfile(os.path.join(dest, 'brief.md')))
