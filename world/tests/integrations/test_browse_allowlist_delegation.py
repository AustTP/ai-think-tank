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


class CandidateDirectorDelegation(unittest.TestCase):
    """'allowlist candidate' is DELEGABLE (not floor 1.0): the director loop
    auto-approving it must APPLY the runtime grant (the 2026-10 fix), with the
    director named in the action log -- not leave a silent no-op approval the
    player never hears about."""

    def test_director_auto_approve_applies_grant_with_attribution(self):
        esc = {'status': 'pending', 'kind': 'allowlist candidate',
               'question': 'CandHost.example has been confidently approved by Jev 5 times',
               'note': 'candhost.example', 'askCount': 0, 'lastAskedAt': 0}
        pending_id = 'esc-cand'
        for name, mock_value in (
            ('_load_escalations', unittest.mock.Mock(return_value={pending_id: esc})),
            ('_save_escalations', unittest.mock.Mock()),
            ('get_state_from_db', unittest.mock.Mock(return_value={
                'agentRoster': [{'id': 'nora', 'name': 'Nora', 'isDirector': True,
                                 'isAdmin': False}]})),
            ('_jev_quorum_choice_sync', unittest.mock.Mock(return_value=('approve', 0.95, 0.0))),
            ('ensure_sandbox_networking', unittest.mock.Mock()),
            ('log_action', unittest.mock.Mock()),
        ):
            patcher = unittest.mock.patch.object(serve, name, mock_value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', set()):
            serve._resolve_pending_escalations_sync()
            self.assertEqual(esc['status'], 'approved')
            self.assertIn('nora', esc.get('resolvedBy', ''))
            self.assertTrue(serve._is_allowlisted_host('candhost.example'))
        # Attribution: the GRANT itself names the director and the delegation
        # path, so the audit trail distinguishes it from the human's link.
        grant_calls = [c for c in serve.log_action.call_args_list
                       if c.args[:2] == ('nora', 'allowlist_grant')]
        self.assertTrue(grant_calls, 'director-attributed grant must be logged')
        self.assertEqual(grant_calls[0].args[2]['via'], 'director delegation')


class StandingTierAndDelegation(unittest.TestCase):
    """2026-10-13 agent-feedback changes: STANDING_SAFE_DOMAINS (read-only
    tier that skips Jev but is NOT a full allowlist grant), allowlist requests
    escalated to DIRECTORS (floor dropped to delegable, grant applies on
    director approve), the wait estimate surfaced to agents, and the two
    read-only status tools."""

    def test_standing_host_is_not_in_the_full_allowlist(self):
        # The read-only promise: a standing host skips Jev on browse/GET but
        # must NOT appear in _effective_allowlist_domains -- the sandbox
        # egress proxy and Colab gate read that set and would otherwise gain
        # standing WRITE reachability from a reference-reading tier.
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', set()), \
             unittest.mock.patch.object(serve, '_runtime_allowlist_grants', return_value=set()):
            self.assertTrue(serve._is_standing_safe_host('en.wikipedia.org'))
            self.assertFalse(serve._is_allowlisted_host('en.wikipedia.org'))
            self.assertNotIn('en.wikipedia.org', serve._effective_allowlist_domains())

    def test_standing_suffix_matching_is_subdomain_only(self):
        # Bare 'gov'/'edu' entries must match real institutional hosts, not
        # lookalike strings: 'evil-gov' or 'x.edu.attacker.com' don't match.
        self.assertTrue(serve._is_standing_safe_host('www.nih.gov'))
        self.assertTrue(serve._is_standing_safe_host('berkeley.edu'))
        self.assertFalse(serve._is_standing_safe_host('evil-gov.example.com'))
        self.assertFalse(serve._is_standing_safe_host('x.edu.attacker.example'))

    def test_director_auto_approve_applies_grant_for_allowlist_request(self):
        # Operator decision 2026-10-13: an agent-filed 'allowlist request' is
        # DELEGABLE now -- the director loop approving it must apply the
        # runtime grant with director attribution, same as 'allowlist
        # candidate', instead of the old floor-1.0 human-only path.
        esc = {'status': 'pending', 'kind': 'allowlist request',
               'question': 'Agent june requests delegationhost.example',
               'note': 'delegationhost.example', 'askCount': 0, 'lastAskedAt': 0}
        pending_id = 'esc-alw'
        for name, mock_value in (
            ('_load_escalations', unittest.mock.Mock(return_value={pending_id: esc})),
            ('_save_escalations', unittest.mock.Mock()),
            ('get_state_from_db', unittest.mock.Mock(return_value={
                'agentRoster': [{'id': 'nora', 'name': 'Nora', 'isDirector': True,
                                 'isAdmin': False}]})),
            ('_jev_quorum_choice_sync', unittest.mock.Mock(return_value=('approve', 0.95, 0.0))),
            ('ensure_sandbox_networking', unittest.mock.Mock()),
            ('log_action', unittest.mock.Mock()),
        ):
            patcher = unittest.mock.patch.object(serve, name, mock_value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', set()):
            serve._resolve_pending_escalations_sync()
            self.assertEqual(esc['status'], 'approved')
            self.assertIn('nora', esc.get('resolvedBy', ''))
            self.assertTrue(serve._is_allowlisted_host('delegationhost.example'))
        grant_calls = [c for c in serve.log_action.call_args_list
                       if c.args[:2] == ('nora', 'allowlist_grant')]
        self.assertTrue(grant_calls, 'director-attributed grant must be logged')
        self.assertEqual(grant_calls[0].args[2]['via'], 'director delegation')

    def test_wait_estimate_reports_median_from_real_pairs(self):
        with unittest.mock.patch.object(serve, '_allowlist_wait_samples',
                                        return_value=[120.0, 300.0, 600.0]):
            self.assertIn('about 5 minutes', serve._allowlist_wait_estimate())
        with unittest.mock.patch.object(serve, '_allowlist_wait_samples',
                                        return_value=[40.0]):
            self.assertIn('under a minute', serve._allowlist_wait_estimate())

    def test_wait_estimate_degrades_to_honest_no_history(self):
        with unittest.mock.patch.object(serve, '_allowlist_wait_samples', return_value=[]):
            self.assertIn('no approval history', serve._allowlist_wait_estimate())

    def test_wait_samples_pair_requests_to_grants(self):
        # Two requests, one resolved grant: exactly one sample, sized
        # request->grant. Unpaired rows (grant without request) are ignored.
        now = time.time()
        rows = [
            ('allowlist_request', now - 1000, json.dumps({'host': 'a.example'})),
            ('allowlist_grant', now - 400, json.dumps({'host': 'a.example', 'via': 'x'})),
            ('allowlist_request', now - 900, json.dumps({'host': 'b.example'})),
            ('allowlist_grant', now - 800, json.dumps({'host': 'other.example'})),
        ]

        class _Conn:
            def execute(self, _sql, _args):
                class _R:
                    def fetchall(self_inner):
                        return rows
                return _R()

        class _Ctx:
            def __enter__(self):
                return _Conn()

            def __exit__(self, *a):
                return False

        with unittest.mock.patch.object(serve, '_db', lambda: _Ctx()):
            samples = serve._allowlist_wait_samples()
        self.assertEqual(samples, [600.0])

    def test_allowlist_status_text_reports_tiers_pending_and_estimate(self):
        esc = {'status': 'pending', 'kind': 'allowlist request',
               'note': 'pendinghost.example', 'ts': time.time() - 600,
               'askCount': 2}
        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', set()), \
             unittest.mock.patch.object(serve, '_runtime_allowlist_grants',
                                        return_value={'granted.example'}), \
             unittest.mock.patch.object(serve, '_load_escalations',
                                        return_value={'esc-1': esc}), \
             unittest.mock.patch.object(serve, '_db', side_effect=RuntimeError('no db')), \
             unittest.mock.patch.object(serve, '_allowlist_wait_samples', return_value=[]):
            out = serve._allowlist_status_text('en.wikipedia.org')
            out2 = serve._allowlist_status_text('granted.example')
        self.assertIn('standing safe domain', out)
        self.assertIn('pendinghost.example', out)
        self.assertIn('director pass 2', out)
        self.assertIn('no approval history', out)
        self.assertIn('ALLOWLISTED (runtime grant)', out2)

    def test_team_status_text_shows_queue_and_working(self):
        fake_state = {
            'agents': {'nora': {'task': 'task-1'}, 'ben': {}},
            'tasks': {'task-1': {'title': 'Write the verdict section',
                                 'status': 'working', 'workUntil': time.time() + 300},
                      'task-2': {'title': 'A failed card', 'status': 'failed'}},
            'workQueue': [{'taskType': 'spike', 'title': 'Survey the fleet'}],
        }
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=fake_state), \
             unittest.mock.patch.object(serve, '_load_escalations', return_value={}):
            out = serve._team_status_text()
        self.assertIn('nora: Write the verdict section [working]', out)
        self.assertIn('[spike] Survey the fleet', out)
        self.assertIn('failed=1', out)
        self.assertIn('NEEDS ATTENTION', out)

    def test_team_status_text_shows_owner_room_attempt_count_and_why(self):
        # 2026-10-13 review: agents could not see WHO owns a queued item (a
        # 21k-attempt wedge hid behind that), WHERE work is happening, or WHY a
        # task is stuck -- only the status word.
        fake_state = {
            'agents': {'nora': {'task': 'task-9'}},
            'tasks': {'task-9': {'title': 'Draft a section', 'status': 'blocked',
                                 'room': 'pressoffice'}},
            'workQueue': [{'taskType': 'work', 'title': 'Act on mail (harold)',
                           'assignedTo': 'nora', 'room': 'pressoffice', 'attempts': 21213}],
        }
        esc = {'e7': {'status': 'pending', 'kind': 'stuck review loop',
                      'question': '"Document public API" has gone through 8 review cycles.',
                      'ts': time.time() - 600, 'askCount': 2, 'humanNotified': False}}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=fake_state), \
             unittest.mock.patch.object(serve, '_load_escalations', return_value=esc), \
             unittest.mock.patch.object(serve, '_db', return_value=_fake_db_ctx([
                 ('quality_gate_reject',
                  '{"taskId": "task-9", "note": "quality pipeline FAILED at node-tests"}')])):
            out = serve._team_status_text()
        self.assertIn('pinned to nora', out)
        self.assertIn('room pressoffice', out)
        self.assertIn('21213 attempts', out)
        self.assertIn('why: quality pipeline FAILED at node-tests', out)
        self.assertIn('Document public API', out)
        self.assertIn('stuck review loop', out)
        self.assertIn('waiting 10m, director pass 2', out)
        self.assertIn('nora: Draft a section [blocked, pressoffice]', out)

    def test_team_status_escalation_detail_falls_back_to_no_detail(self):
        # A record with no question/note must still say so rather than render
        # as a bare "routine x1" the reader cannot act on.
        esc = {'e1': {'status': 'pending', 'kind': 'routine', 'question': 'q?',
                      'ts': time.time(), 'askCount': 6}}
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value={'agents': {}, 'tasks': {}, 'workQueue': []}), \
             unittest.mock.patch.object(serve, '_load_escalations', return_value=esc), \
             unittest.mock.patch.object(serve, '_db', return_value=_fake_db_ctx([])):
            out = serve._team_status_text()
        self.assertIn('routine x1', out)
        self.assertIn('e1 [routine] q?', out)
        self.assertIn('director pass 6', out)


