"""Cut 4: the agent coding-standards gate.

The think tank's Work Room (pressoffice) agents write and run real Python in a
network-disabled Docker sandbox. This suite locks down that the standard is
(a) instructed in the coding system prompt + room purpose, (b) baked into the
sandbox image as a real toolchain, and (c) HARD-gated: no approval counts unless
the quality pipeline (flake8/mypy/bandit/pytest --cov>=90, measured in TOTAL
across all files) objectively passed.

Three layers are tested, matching where the guarantee actually lives:
  - serve._run_quality_pipeline builds the exact 4 steps and maps a red/green
    /api/pipeline response (objective evidence).
  - serve._run_coding_content fails the AUTHOR'S task (ok=False) when the
    pipeline is red -- nothing reaches review to approve in the first place.
  - sim._apply_content_result (the approval authority) refuses to count a clean
    vote unless pipelineOk is True -- a reviewer's "looks solid" cannot approve
    code that fails the standard, and a red pipeline forces the actionable path.

Hermetic: no live network, no real Jev, no real Docker. /api/pipeline and
/api/execute are faked via serve._http_json; sim helpers are called directly.
"""

import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402  (sys.path insert above is the repo test convention)
import sim  # noqa: E402


# ---------------------------------------------------------------------------
# sim._apply_content_result -- the approval authority
# ---------------------------------------------------------------------------

def _state(**over):
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': [
            {'id': 'maya', 'name': 'Maya', 'role': 'director', 'isAdmin': True},
            {'id': 'ada', 'name': 'Ada', 'role': 'engineer', 'director': 'maya'},
            {'id': 'ben', 'name': 'Ben', 'role': 'engineer', 'director': 'maya'},
            {'id': 'cora', 'name': 'Cora', 'role': 'engineer', 'director': 'maya'},
        ],
        'agents': {
            'maya': {'id': 'maya', 'busy': True},
            'ada': {'id': 'ada', 'busy': False, 'offDuty': True, 'task': None},
            'ben': {'id': 'ben', 'busy': False, 'offDuty': True, 'task': None},
            'cora': {'id': 'cora', 'busy': False, 'offDuty': True, 'task': None},
        },
        'workQueue': [],
        'tasks': {},
        'reports': [],
    }
    state.update(over)
    return state


def _story(**over):
    story = {'id': 'story-1', 'title': 'Build checkout', 'room': 'pressoffice',
             'assignedTo': 'ben', 'projectLabel': 'storefront', 'taskType': 'code'}
    story.update(over)
    return story


class ApprovalGate(unittest.TestCase):
    """The Cut 4 hard gate in sim._apply_content_result: a 'clean' review verdict
    only counts toward approval when the pipeline objectively passed."""

    def _gated(self):
        state = _state()
        story = _story()
        state['tasks'][story['id']] = story
        sim._enter_peer_review(state, story, now_ms=1000)
        return state, story

    def _vote(self, state, reviewer, result):
        parent_id = next(iter(state['tasks']))
        review = {'id': f'rev-{reviewer}', 'reviewOf': parent_id,
                  'assignedTo': reviewer, 'taskType': 'review', 'status': 'working'}
        result = dict(result)
        result.setdefault('note', 'review filed')
        sim._apply_content_result(state, review, result)
        return state['tasks'][parent_id]

    def test_clean_with_green_pipeline_counts(self):
        state, _ = self._gated()
        parent = self._vote(state, 'ada', {'peerVerdict': 'clean', 'pipelineOk': True})
        self.assertEqual(parent['_peerGate']['approvals'], 1)
        self.assertEqual(parent['_peerGate']['approvers'], ['ada'])

    def test_clean_with_red_pipeline_is_not_approval(self):
        # The reviewer said "looks solid" but the pipeline is red -- the verdict
        # must be downgraded so NO approval is counted.
        state, _ = self._gated()
        parent = self._vote(state, 'ada', {'peerVerdict': 'clean', 'pipelineOk': False})
        self.assertEqual(parent['_peerGate']['approvals'], 0)
        self.assertEqual(parent['_peerGate']['approvers'], [], 'a red pipeline must never count an approval')

    def test_clean_with_missing_pipeline_evidence_is_not_approval(self):
        # Defensive: a result that claims clean with NO pipeline evidence at all
        # is treated as unverified, not as approval.
        state, _ = self._gated()
        parent = self._vote(state, 'ada', {'peerVerdict': 'clean'})  # no pipelineOk key
        self.assertEqual(parent['_peerGate']['approvals'], 0)
        self.assertEqual(parent['_peerGate']['approvers'], [])

    def test_red_pipeline_clean_verdict_resets_gate_and_notifies_author(self):
        # Downgraded red -> actionable path: gate reset + author rejection notice.
        state, story = self._gated()
        parent = self._vote(state, 'ada', {'peerVerdict': 'clean', 'pipelineOk': False})
        self.assertEqual(parent['_peerGate']['approvals'], 0)
        kinds = [m.get('kind') for m in state['agents'][story['assignedTo']].get('mailbox', [])]
        self.assertIn('peer_review_rejected', kinds, 'author must be told their red-pipeline work is sent back')

    def test_actionable_red_pipeline_behaves_as_actionable(self):
        # Verdict already actionable stays that way (gate reset, no approval).
        state, story = self._gated()
        parent = self._vote(state, 'cora', {'peerVerdict': 'actionable', 'pipelineOk': False})
        self.assertEqual(parent['_peerGate']['approvals'], 0)
        self.assertEqual(parent['_peerGate']['approvers'], [])


# ---------------------------------------------------------------------------
# serve pipeline helper
# ---------------------------------------------------------------------------

class QualityPipelineSteps(unittest.TestCase):
    def test_builds_four_exact_steps(self):
        steps = serve._quality_pipeline_steps()
        self.assertEqual([s['name'] for s in steps],
                         ['flake8', 'mypy', 'bandit', 'pytest-cov'])
        by_name = {s['name']: s['command'] for s in steps}
        self.assertEqual(by_name['flake8'], 'python -m flake8 .')
        self.assertEqual(by_name['mypy'], 'python -m mypy .')
        self.assertEqual(by_name['bandit'], 'python -m bandit -r . -q')
        # Total coverage across all files (not per-file) is what --cov-fail-under
        # expresses: pytest-cov fails only when the AGGREGATE falls below 90.
        self.assertEqual(by_name['pytest-cov'], 'python -m pytest --cov=. --cov-fail-under=90 -q')


class RunQualityPipeline(unittest.TestCase):
    def _run(self, pipeline_response):
        called = {}

        def fake_http(method, base, path, body=None, header=None):
            if path == '/api/pipeline':
                called['body'] = body
                return pipeline_response
            return {'error': f'unexpected {method} {path}'}

        with unittest.mock.patch.object(serve, '_http_json', fake_http, create=False):
            return serve._run_quality_pipeline('http://base', 'key', 'ada', 'workroom-shared', 'gate it'), called

    def test_green_pipeline(self):
        resp = {'sandboxId': 'workroom-shared', 'failedStep': None,
                'results': [{'name': n, 'exitCode': 0} for n in
                            ('flake8', 'mypy', 'bandit', 'pytest-cov')]}
        result, called = self._run(resp)
        self.assertTrue(result['ok'])
        self.assertIsNone(result['failedStep'])
        self.assertIn('all green', result['note'])
        # The steps are the standard quality pipeline, run via /api/pipeline.
        self.assertEqual([s['name'] for s in called['body']['steps']], _step_names())

    def test_red_pipeline_reports_failing_step_and_snippet(self):
        resp = {'sandboxId': 'workroom-shared', 'failedStep': 'flake8',
                'results': [{'name': 'flake8', 'exitCode': 1,
                             'stdout': '', 'stderr': 'foo.py:3: unused import os\nfoo.py:9: F841'}]}
        result, _ = self._run(resp)
        self.assertFalse(result['ok'])
        self.assertEqual(result['failedStep'], 'flake8')
        self.assertIn('flake8', result['note'])
        self.assertIn('F841', result['note'])

    def test_error_response_is_never_green(self):
        result, _ = self._run({'error': 'boom'})
        self.assertFalse(result['ok'])


# ---------------------------------------------------------------------------
# serve._run_coding_content -- author's task fails on a red pipeline
# ---------------------------------------------------------------------------

class CodingExecutorQualityGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-cut4-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()
        self.stored = {}
        self.http_log = []
        with serve._db() as conn:
            conn.execute("INSERT OR REPLACE INTO model_tiers (band, slug, name, price_per_m, chosen_at) "
                         "VALUES ('coding', 'test-coding-model', 'Test Coding', 0.0, 0)")

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _record(self, tid, r):
        self.stored.update({tid: r})

    def _fake_http(self, *, write_exit=0, pipeline=None):
        """Command-routing fake: a single heredoc write via /api/execute, then a
        /api/pipeline result (green by default, or the supplied red one)."""
        pipeline = pipeline if pipeline is not None else {
            'sandboxId': 'x', 'failedStep': None,
            'results': [{'name': n, 'exitCode': 0} for n in _step_names()]}

        def fake(method, base, path, body=None, header=None):
            self.http_log.append((method, path))
            if path == '/api/chat':
                return {'reply': "cat > app.py << 'EOF'\nx = 1\nEOF"}
            if path == '/api/library/search':
                return {'matches': []}
            if path == '/api/library/file' and method == 'GET':
                return {'error': 'not found'}
            if path == '/api/library/file' and method == 'POST':
                return '_raw = saved'
            if path == '/api/pipeline':
                return pipeline
            if path == '/api/execute':
                body = body or {}
                command = (body.get('command') or '')
                if command.startswith('budget=15000'):
                    return {'allowed': True, 'stdout': '--- index.html ---\n<html></html>\n', 'exitCode': 0}
                if command == 'ls -la':
                    return {'allowed': True, 'stdout': 'index.html\napp.py\n', 'exitCode': 0}
                if command.startswith('grep -q'):
                    return {'allowed': True, 'stdout': 'LINKED:app.js', 'exitCode': 0}
                if 'PHANTOM:' in command and 'EXISTS:' in command:
                    return {'allowed': True, 'stdout': '', 'exitCode': 0}
                if command.startswith('for f in *.html *.js'):
                    return {'allowed': True, 'stdout': '', 'exitCode': 0}
                return {'allowed': True, 'exitCode': write_exit, 'timedOut': False,
                        'stdout': '', 'reason': None}
            return {'error': f'unexpected {method} {path}'}
        return fake

    def test_author_task_succeeds_on_green_pipeline(self):
        import sim as _sim
        real_store = _sim._store_content_result
        _sim._store_content_result = self._record
        try:
            with unittest.mock.patch.object(serve, '_http_json', self._fake_http(), create=False):
                serve._run_coding_content(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'c1', 'room': 'pressoffice', 'title': 'add score', 'projectLabel': 'Snake'},
                    'Current files:\nindex.html\n')
        finally:
            _sim._store_content_result = real_store
        self.assertTrue(self.stored['c1'].get('ok'))
        self.assertIn('quality pipeline all green', self.stored['c1']['note'])
        pipes = [p for p in self.http_log if p[1] == '/api/pipeline']
        self.assertEqual(1, len(pipes), 'the quality pipeline must run once on a coding task')

    def test_author_task_fails_before_review_on_red_pipeline(self):
        # The write succeeds but flake8 reports a real issue: the pipeline is red,
        # so the author's OWN task must fail (ok=False) -- there is no completed
        # deliverable left for a reviewer to approve.
        red = {'sandboxId': 'x', 'failedStep': 'flake8',
               'results': [{'name': 'flake8', 'exitCode': 1, 'stdout': '', 'stderr': 'app.py:1: F401'}]}
        import sim as _sim
        real_store = _sim._store_content_result
        _sim._store_content_result = self._record
        try:
            with unittest.mock.patch.object(serve, '_http_json', self._fake_http(pipeline=red), create=False):
                ok = serve._run_coding_content(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'c2', 'room': 'pressoffice', 'title': 'add score', 'projectLabel': 'Snake'},
                    'Current files:\nindex.html\n')
        finally:
            _sim._store_content_result = real_store
        self.assertFalse(ok, 'a red quality pipeline must fail the author\'s task')
        result = self.stored['c2']
        self.assertFalse(result.get('ok'))
        self.assertIn('flake8', result['note'])


# ---------------------------------------------------------------------------
# Instruction + toolchain wiring
# ---------------------------------------------------------------------------

class Wiring(unittest.TestCase):
    def test_coding_standards_prompt_instructs_standards(self):
        # The real constant appended to every Work Room coding prompt: PEP 8,
        # the toolchain, TOTAL coverage, and the no-approval-on-red rule.
        prompt_text = serve.CODING_STANDARDS_PROMPT
        self.assertIn('PEP 8', prompt_text)
        self.assertIn('flake8 ., python -m mypy ., python -m bandit -r ., and python -m pytest --cov=.', prompt_text)
        self.assertIn('--cov-fail-under=90', prompt_text)
        self.assertIn('TOTAL', prompt_text)
        self.assertIn('not per-file', prompt_text)
        self.assertIn('will NOT be approved until that pipeline is green', prompt_text)

    def test_room_purpose_mentions_quality_bar(self):
        purpose = serve._DEFAULT_ROOM_DEFINITIONS['pressoffice']['purpose']
        self.assertIn('PEP 8', purpose)
        self.assertIn('quality pipeline', purpose)
        self.assertIn('NOT approved unless that pipeline is green', purpose)

    def test_sandbox_image_has_toolchain(self):
        # The SANDBOX_IMAGE must be the baked image (world/sandbox/Dockerfile),
        # not the bare base image -- that is what makes the tools available in the
        # network-disabled sandbox WITHOUT a runtime pip install.
        self.assertEqual(serve.SANDBOX_IMAGE, 'ai-think-tank-work-sandbox')

    def test_dockerfile_installs_toolchain(self):
        dockerfile = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  'sandbox', 'Dockerfile')
        with open(dockerfile) as f:
            text = f.read()
        for tool in ('flake8', 'mypy', 'bandit', 'pytest-cov'):
            self.assertIn(tool, text, f'Dockerfile must pin {tool}')


def _step_names():
    return [s['name'] for s in serve._quality_pipeline_steps()]


if __name__ == '__main__':
    unittest.main()