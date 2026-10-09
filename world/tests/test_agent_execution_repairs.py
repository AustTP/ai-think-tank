import json
import os
import sys
import tempfile
import unittest
import urllib.request
import urllib.error
import io
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import content
import serve


class ExecutionRepairs(unittest.TestCase):
    def test_provider_workspace_budget_403_is_explained_without_retry(self):
        req=urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',data=b'{}')
        error=urllib.error.HTTPError(req.full_url,403,'Forbidden',{},io.BytesIO(b'{"error":{"message":"Workspace monthly budget of $10.00 exceeded. Contact your org admin."}}'))
        with patch.object(serve,'_urlopen_with_resilience',side_effect=error) as post:
            with self.assertRaisesRegex(RuntimeError,'Workspace monthly budget.*fallbacks cannot resolve'):
                serve._read_openrouter_response(req)
            self.assertEqual(post.call_count,1)

    def test_both_research_models_fail_then_healthy_model_is_used(self):
        req=urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',data=json.dumps({'model':'meta-llama/llama-3.1-70b-instruct','max_tokens':50}).encode())
        failure=json.dumps({'error':{'message':'Unavailable'}})
        success=json.dumps({'choices':[{'message':{'content':'Actual findings'},'finish_reason':'stop'}]})
        with patch.object(serve,'_urlopen_with_resilience',side_effect=[failure,failure,success]) as post, \
             patch.object(serve,'_research_model_fallback',side_effect=['mistralai/mistral-nemo','healthy-model']), \
             patch.object(serve,'_think_tank_spend_cap_exceeded',return_value=False), \
             patch.object(serve,'record_model_result'):
            result=serve._read_openrouter_response(req)
        self.assertEqual(post.call_count,3)
        self.assertEqual(result['fallbackModel'],'healthy-model')

    def test_unavailable_research_fallback_uses_healthy_coding_model(self):
        with patch.object(serve,'_load_env',return_value={}), \
             patch.object(serve,'_coding_tier_slug',return_value='healthy-model'), \
             patch.object(serve,'_high_tier_slug',return_value=None), \
             patch.object(serve,'is_model_circuit_broken',side_effect=lambda model:model=='mistralai/mistral-nemo'):
            self.assertEqual(serve._research_model_fallback('mistralai/mistral-nemo'),'healthy-model')
            self.assertEqual(serve._research_model_fallback('meta-llama/llama-3.1-70b-instruct'),'healthy-model')

    def test_report_exists_and_is_nonempty_before_success(self):
        with tempfile.TemporaryDirectory() as root:
            target=os.path.join(root,'report.md')
            with patch.object(serve,'_safe_library_path',return_value=target):
                self.assertFalse(content._library_report_saved('report.md'))
                with open(target,'w') as f: f.write('')
                self.assertFalse(content._library_report_saved('report.md'))
                with open(target,'w') as f: f.write('Actual findings')
                self.assertTrue(content._library_report_saved('report.md'))

    def test_llama_failure_uses_fallback_and_preserves_tools_and_budget(self):
        payload={'model':'meta-llama/llama-3.1-70b-instruct','max_tokens':500,
                 'messages':[{'role':'user','content':'test'}],'tools':[{'type':'function'}]}
        req=urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',data=json.dumps(payload).encode())
        failure={'error':{'message':'Unavailable'},'usage':{'cost':0.01}}
        success={'choices':[{'finish_reason':'stop','message':{'content':'OK'}}],'usage':{'cost':0.02}}
        with patch.object(serve,'_urlopen_with_resilience',side_effect=[json.dumps(failure),json.dumps(success)]) as post, \
             patch.object(serve,'_think_tank_spend_cap_exceeded',return_value=False), \
             patch.object(serve,'is_model_circuit_broken',return_value=False), \
             patch.object(serve,'_load_env',return_value={}):
            result=serve._read_openrouter_response(req)
            retry=json.loads(post.call_args_list[1].args[0].data)
        self.assertEqual(retry['model'],'mistralai/mistral-nemo')
        self.assertEqual(retry['tools'],payload['tools'])
        self.assertEqual(retry['messages'],payload['messages'])
        self.assertEqual(retry['max_tokens'],500)
        self.assertAlmostEqual(result['usage']['cost'],0.03)
        self.assertEqual(result['fallbackFrom'],payload['model'])

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
