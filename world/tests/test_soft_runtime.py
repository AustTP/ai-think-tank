import os
import sys
import unittest
import asyncio
import json
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import serve
import content
import sim
import runtime_limits


class SoftRuntime(unittest.TestCase):
    def test_background_job_completes_and_checks_owner(self):
        async def exercise():
            request = Mock()
            request.json = unittest.mock.AsyncMock(return_value={
                'background': True, 'agentId': 'agent', 'steps': []})
            request.headers = {'X-Agent-Key': 'key'}
            async def execute(req):
                await asyncio.sleep(0)
                return serve.JSONResponse({'results': [{'name': 'check', 'exitCode': 0}]})
            with patch.object(serve, '_require_player_session', return_value=False), \
                 patch.object(serve, 'verify_agent_key', return_value=True), \
                 patch.object(serve, '_pipeline_execute', side_effect=execute), \
                 patch.dict(serve._pipeline_jobs, {}, clear=True):
                response = await serve.pipeline(request)
                self.assertEqual(response.status_code, 202)
                job_id = json.loads(response.body)['jobId']
                await serve._pipeline_jobs[job_id]['worker']
                result = await serve.pipeline_job(job_id, request)
                self.assertEqual(result['status'], 'done')
                self.assertEqual(result['result']['results'][0]['exitCode'], 0)
                self.assertNotIn('worker', result)
                with patch.object(serve, 'verify_agent_key', return_value=False):
                    denied = await serve.pipeline_job(job_id, request)
                    self.assertEqual(denied.status_code, 401)
        asyncio.run(exercise())

    def test_live_worker_can_pass_soft_deadline_repeatedly(self):
        task = {'id': 'soft-test', '_contentInFlight': True, 'status': 'working', 'workUntil': 1}
        worker = Mock();worker.is_alive.return_value = True
        with patch.dict(sim._content_workers, {'soft-test': worker}):
            for now in (100, 10000, 100000):
                self.assertTrue(sim._extend_running_work(task, now, now * 1000))
                self.assertGreater(task['workUntil'], now)
                self.assertEqual(task['status'], 'working')

    def test_dead_worker_does_not_get_unlimited_extensions(self):
        task = {'id': 'dead-test', '_contentInFlight': True}
        self.assertFalse(sim._extend_running_work(task, 10, 10000))

    def test_transport_scales_with_tokens_and_history(self):
        with patch.dict(runtime_limits._OBSERVED, {}, clear=True):
            self.assertGreater(runtime_limits.transport('/api/chat', {'max_tokens': 4000}),
                               runtime_limits.transport('/api/chat', {'max_tokens': 100}))
            runtime_limits.observe('/api/chat', 600)
            self.assertGreater(runtime_limits.transport('/api/chat'), 600)

    def test_pipeline_waits_for_job_without_restarting_it(self):
        steps = content._quality_pipeline_steps()
        replies = [{'jobId': 'job'}, {'status': 'running', 'currentStep': 'pytest-cov'},
                   {'status': 'done', 'result': {'results': [
                       {'name': s['name'], 'exitCode': 0} for s in steps]}}]
        with patch.object(serve, 'SANDBOXES_DIR', '/nonexistent-test-root'), \
             patch.object(serve, '_http_json', side_effect=replies) as http, \
             patch.object(content.time, 'sleep'):
            result = content._run_quality_pipeline_locked('http://local', 'key', 'agent', 'test', 'validation')
        self.assertTrue(result['ok'])
        self.assertEqual(sum(c.args[0] == 'POST' for c in http.call_args_list), 1)
