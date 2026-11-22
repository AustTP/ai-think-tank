# Tests for the change lane (operator, 2026-11-22): card completions with
# validated code deliverables become tank/<taskId> branches, PRs, CI, and a
# risk-tiered merge decision (docs/tests auto-merge on green CI; code =
# Jev + director review; unsure -> escalation). All git/GitHub contact is
# mocked; no network, no credentials.
import os
import sys
import time
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402

_J = {'taskId': 'task-12345', 'title': 'Fix a thing', 'agent': 'ruth',
      'queuedAt': 0.0}

_GIT_GREEN_DOCS = [
    (0, '0'),          # fetch storage
    (0, '1'),          # rev-list: 1 commit ahead
    (0, 'abc123'),    # rev-parse HEAD
    (0, ''),           # push
    (0, 'README.md\nworld/tests/test_x.py'),
    (0, 'diff text'),
]


def _git(code=False):
    names = 'README.md\nworld/tests/test_x.py'
    if code:
        names = 'world/server/serve.py'
    return [
        (0, '0'), (0, '1'), (0, 'abc123'), (0, ''),
        (0, names), (0, 'diff text'),
    ]


def _api_caller(recorder):
    """Builds an _change_lane_api side_effect that records (method, path,
    body) calls immediately before returning sequenced canned responses."""
    def side(method, path, body=None):
        recorder.append((method, path, body))
        if method == 'POST' and path == '/pulls':
            return (201, {'number': 7})
        if method == 'GET' and 'actions/runs' in path:
            return (200, {'workflow_runs': [
                {'status': 'completed', 'conclusion': 'success'}]})
        if method == 'PUT' and '/merge' in path:
            return (200, {})
        if method == 'PATCH' and path == '/pulls/7':
            return (200, {})
        return (201, {})
    return side

def _fake_task(task_id='task-12345', deliverable='code', code_published=True,
               title='Fix a thing'):
    return {'id': task_id, 'deliverable': deliverable,
            'codePublished': code_published, 'title': title}


def _fake_task(task_id='task-12345', deliverable='code', code_published=True,
               title='Fix a thing'):
    return {'id': task_id, 'deliverable': deliverable,
            'codePublished': code_published, 'title': title}


class ChangeLaneClassification(unittest.TestCase):
    def test_all_docs_paths_is_docs_tier(self):
        self.assertEqual(serve.change_lane_classify(
            ['README.md', 'world/tests/test_x.py']), 'docs')

    def test_any_code_path_is_code_tier(self):
        self.assertEqual(serve.change_lane_classify(
            ['README.md', 'world/server/serve_parts/serve.py']), 'code')

    def test_empty_paths_is_code_tier_fail_closed(self):
        self.assertEqual(serve.change_lane_classify([]), 'code')

    def test_nested_docs_dir_is_docs(self):
        self.assertEqual(serve.change_lane_classify(['docs/guide.md']), 'docs')

    def test_root_source_is_code(self):
        self.assertEqual(serve.change_lane_classify(['serve.py']), 'code')


class ChangeLaneEnqueue(unittest.TestCase):
    def setUp(self):
        self.saved = (None, serve._CHANGE_LANE_ENABLED)
        serve._CHANGE_LANE_QUEUE.clear()
        # CI has no .env (GITHUB_WATCH_REPO/GITHUB_TOKEN come from the
        # repo's secret store, not the environment): without stubs the
        # creds check would correctly refuse to enqueue and this class
        # would test the refusal rather than the queue.
        self._cred_patches = [
            mock.patch.object(serve, '_CHANGE_LANE_REPO', 'AustTP/ai-think-tank'),
            mock.patch.object(serve, '_CHANGE_LANE_TOKEN', 'token-for-tests'),
        ]
        for p in self._cred_patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: (
            serve._CHANGE_LANE_QUEUE.clear(),
            setattr(serve, '_CHANGE_LANE_ENABLED', self.saved[1])))

    def test_code_published_enqueues_and_record(self):
        serve._CHANGE_LANE_ENABLED = True
        job = serve._change_lane_on_task_completed(_fake_task(), 'ruth')
        self.assertIsNotNone(job)
        self.assertEqual(serve._CHANGE_LANE_QUEUE[-1]['taskId'], 'task-12345')
        self.assertEqual(serve._CHANGE_LANE_QUEUE[-1]['agent'], 'ruth')

    def test_non_code_or_unpublished_is_ignored(self):
        serve._CHANGE_LANE_ENABLED = True
        self.assertIsNone(serve._change_lane_on_task_completed(
            _fake_task(deliverable='report'), 'ruth'))
        self.assertIsNone(serve._change_lane_on_task_completed(
            _fake_task(code_published=False), 'ruth'))
        self.assertEqual(list(serve._CHANGE_LANE_QUEUE), [])

    def test_disabled_lane_ignores(self):
        serve._CHANGE_LANE_ENABLED = False
        self.assertIsNone(serve._change_lane_on_task_completed(
            _fake_task(), 'ruth'))
        self.assertEqual(list(serve._CHANGE_LANE_QUEUE), [])

    def test_missing_creds_skips_with_log(self):
        serve._CHANGE_LANE_ENABLED = True
        with mock.patch.object(serve, '_CHANGE_LANE_REPO', ''), \
                mock.patch.object(serve, '_CHANGE_LANE_TOKEN', ''):
            result = serve._change_lane_on_task_completed(_fake_task(), 'ruth')
        self.assertIsNone(result)
        self.assertEqual(list(serve._CHANGE_LANE_QUEUE), [])

class ChangeLaneProcess(unittest.TestCase):
    def setUp(self):
        self.saved = (serve._CHANGE_LANE_DAY, serve._CHANGE_LANE_COUNT)
        self.addCleanup(lambda: (
            setattr(serve, '_CHANGE_LANE_DAY', self.saved[0]),
            setattr(serve, '_CHANGE_LANE_COUNT', self.saved[1])))
        # _change_lane_wait_ci sleeps between polls -- kill real sleeps.
        sleep_patcher = mock.patch.object(serve.time, 'sleep', lambda s: None)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def _run(self, git=None, api_record=None, jev=None, director=None,
             sandbox='sandboxes/story-task-12345'):
        """Runs _change_lane_process fully mocked. Returns (api_calls)."""
        recorded = []
        patches = [
            mock.patch.object(serve, '_change_lane_sandbox_for',
                              return_value=sandbox),
            mock.patch.object(serve, '_change_lane_run', side_effect=git),
            mock.patch.object(serve, '_change_lane_api',
                              side_effect=_api_caller(recorded)),
            mock.patch.object(serve, 'log_action'),
        ]
        if jev is not None:
            patches.append(mock.patch.object(
                serve, '_jev_quorum_choice_sync', return_value=jev))
        if director is not None:
            patches.append(mock.patch.object(
                serve, '_director_second_opinion',
                return_value=director))
        for p in patches:
            p.start()
        try:
            serve._change_lane_process(dict(_J))
        finally:
            for p in patches:
                p.stop()
        return recorded

    def _merges(self, recorded):
        return [c for c in recorded if c[0] == 'PUT' and '/merge' in c[1]]

    def _closes(self, recorded):
        return [c for c in recorded if c[0] == 'PATCH'
                and c[1] == '/pulls/7']

    def test_docs_tier_auto_merges_on_green(self):
        rec = self._run(git=_git())
        self.assertEqual(len(self._merges(rec)), 1)
        self.assertFalse(self._closes(rec))

    def test_code_tier_jev_approve_merges(self):
        rec = self._run(git=_git(code=True), jev=('approve', 0.95, 0.01))
        self.assertEqual(len(self._merges(rec)), 1)

    def test_code_tier_jev_deny_closes_pr(self):
        rec = self._run(git=_git(code=True), jev=('deny', 0.95, 0.01))
        self.assertEqual(len(self._merges(rec)), 0)
        self.assertEqual(len(self._closes(rec)), 1)

    def test_unsure_director_approve_merges(self):
        rec = self._run(git=_git(code=True), jev=('unsure', 0.3, 0.01),
                        director=True)
        self.assertEqual(len(self._merges(rec)), 1)

    def test_unsure_director_deny_leaves_pr_open(self):
        rec = self._run(git=_git(code=True), jev=('unsure', 0.3, 0.01),
                        director=False)
        self.assertEqual(len(self._merges(rec)), 0)
        self.assertEqual(len(self._closes(rec)), 0)

    def test_ci_red_withholds_merge(self):
        def api(method, path, body=None):
            rec.append((method, path, body))
            if method == 'POST' and path == '/pulls':
                return (201, {'number': 7})
            if method == 'GET' and 'actions/runs' in path:
                return (200, {'workflow_runs': [
                    {'status': 'completed', 'conclusion': 'failure'}]})
            return (201, {})
        rec = []
        with mock.patch.object(serve, '_change_lane_sandbox_for',
                               return_value='sb'), \
                mock.patch.object(serve, '_change_lane_run',
                                  side_effect=_git()), \
                mock.patch.object(serve, '_change_lane_api', side_effect=api), \
                mock.patch.object(serve, '_jev_quorum_choice_sync') as jev, \
                mock.patch.object(serve, '_director_second_opinion') as director, \
                mock.patch.object(serve, 'log_action'):
            serve._change_lane_process(dict(_J))
        self.assertEqual(self._merges(rec), [])
        jev.assert_not_called()
        director.assert_not_called()

    def test_daily_cap_blocks_contact(self):
        serve._CHANGE_LANE_COUNT = 99
        serve._CHANGE_LANE_DAY = time.strftime('%Y-%m-%d')
        with mock.patch.object(serve, '_change_lane_sandbox_for',
                               return_value='x') as sandbox, \
                mock.patch.object(serve, 'log_action'):
            serve._change_lane_process(dict(_J))
        sandbox.assert_not_called()
        self.assertEqual(serve._CHANGE_LANE_COUNT, 99)

    def test_no_commits_ahead_skips(self):
        calls = []

        def git(*a, **k):
            calls.append(list(a))
            if '--count' in a:
                return (0, '0')
            return (0, '0')

        rec = []
        with mock.patch.object(serve, '_change_lane_sandbox_for',
                               return_value='sb'), \
                mock.patch.object(serve, '_change_lane_run', side_effect=git), \
                mock.patch.object(serve, '_change_lane_api',
                                  side_effect=_api_caller(rec)), \
                mock.patch.object(serve, 'log_action'):
            serve._change_lane_process(dict(_J))
        self.assertFalse([c for c in rec if c[0] == 'POST'])
        self.assertTrue(calls)

    def test_no_sandbox_skips(self):
        with mock.patch.object(serve, '_change_lane_sandbox_for',
                               return_value=None), \
                mock.patch.object(serve, 'log_action') as log:
            serve._change_lane_process(dict(_J))
        self.assertTrue(any(
            'no preserved sandbox' in str(c)
            for c in log.call_args_list))

    def test_pr_create_failure_is_logged(self):
        def api(method, path, body=None):
            rec.append((method, path, body))
            return (500, {'message': 'boom'})

        rec = []
        with mock.patch.object(serve, '_change_lane_sandbox_for',
                               return_value='sb'), \
                mock.patch.object(serve, '_change_lane_run',
                                  side_effect=_git()), \
                mock.patch.object(serve, '_change_lane_api', side_effect=api), \
                mock.patch.object(serve, 'log_action'):
            serve._change_lane_process(dict(_J))
        self.assertFalse(self._merges(rec))
        self.assertEqual(rec[0][0], 'POST')

    def test_failed_stage_push_is_logged(self):
        with mock.patch.object(serve, '_change_lane_sandbox_for',
                               return_value='sb'), \
                mock.patch.object(serve, '_change_lane_run',
                                  side_effect=[(0, '0'), (0, '1'), (0, 'abc'),
                                               (1, 'rejected')]), \
                mock.patch.object(serve, 'log_action'):
            serve._change_lane_process(dict(_J))

    def test_worker_exception_is_swallowed_and_logged(self):
        with mock.patch.object(serve, '_change_lane_sandbox_for',
                               side_effect=RuntimeError('boom')), \
                mock.patch.object(serve, 'log_action'):
            serve._change_lane_process(dict(_J))


class ChangeLaneReviewResilience(unittest.TestCase):
    """Retry covers hard failures only; unsure routes unretried; an
    un-convenable review leaves the PR open and escalates to the owner."""

    def setUp(self):
        self.saved = (serve._CHANGE_LANE_DAY, serve._CHANGE_LANE_COUNT)
        self.addCleanup(lambda: (
            setattr(serve, '_CHANGE_LANE_DAY', self.saved[0]),
            setattr(serve, '_CHANGE_LANE_COUNT', self.saved[1])))
        sleep_patcher = mock.patch.object(serve.time, 'sleep', lambda s: None)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def _merges(self, rec):
        return [c for c in rec if c[0] == 'PUT' and '/merge' in c[1]]

    def _closes(self, rec):
        return [c for c in rec if c[0] == 'PATCH' and c[1] == '/pulls/7']

    def test_hard_failure_then_success_merges(self):
        rec = []
        calls = []
        with mock.patch.object(serve, '_change_lane_sandbox_for',
                               return_value='sb'), \
                mock.patch.object(serve, '_change_lane_run', side_effect=_git(True)), \
                mock.patch.object(serve, '_change_lane_api',
                                  side_effect=_api_caller(rec)), \
                mock.patch.object(serve, '_jev_quorum_choice_sync',
                                  side_effect=[RuntimeError('pool down'),
                                               ('approve', 0.95, 0.01)]), \
                mock.patch.object(serve, 'log_action'):
            serve._change_lane_process(dict(_J))
        self.assertEqual(len(self._merges(rec)), 1)
        self.assertEqual(len(calls), 0)

    def test_three_hard_failures_escalate_and_keep_pr_open(self):
        rec = []
        with mock.patch.object(serve, '_change_lane_sandbox_for',
                               return_value='sb'), \
                mock.patch.object(serve, '_change_lane_run', side_effect=_git(True)), \
                mock.patch.object(serve, '_change_lane_api',
                                  side_effect=_api_caller(rec)), \
                mock.patch.object(serve, '_jev_quorum_choice_sync',
                                  side_effect=RuntimeError('pool down')), \
                mock.patch.object(serve, '_director_second_opinion') as director, \
                mock.patch.object(serve, 'create_escalation') as esc, \
                mock.patch.object(serve, 'log_action'):
            serve._change_lane_process(dict(_J))
        self.assertEqual(self._merges(rec), [])
        self.assertEqual(self._closes(rec), [])
        director.assert_not_called()
        esc.assert_called_once()

    def test_unsure_is_not_retried(self):
        rec = []
        with mock.patch.object(serve, '_change_lane_sandbox_for',
                               return_value='sb'), \
                mock.patch.object(serve, '_change_lane_run', side_effect=_git(True)), \
                mock.patch.object(serve, '_change_lane_api',
                                  side_effect=_api_caller(rec)), \
                mock.patch.object(serve, '_jev_quorum_choice_sync',
                                  return_value=('unsure', 0.2, 0.01)) as jev, \
                mock.patch.object(serve, '_director_second_opinion',
                                  return_value=False), \
                mock.patch.object(serve, 'create_escalation') as esc, \
                mock.patch.object(serve, 'log_action'):
            serve._change_lane_process(dict(_J))
        self.assertEqual(jev.call_count, 1)
        esc.assert_not_called()
        self.assertEqual(self._merges(rec), [])
