# CI storm gates on the github watch (BEN-212 autopsy, 2026-11-23): a newer
# green run on the same branch voids older red runs, and one failure ask per
# branch|workflow family per window. Hermetic: no GitHub API, no webhook
# file writes (gate function is pure + in-memory state).
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import serve


def _run(rid, branch='main', wf='ci', conclusion='failure',
         status='completed', started='2026-11-23T21:00:00Z'):
    return {'id': rid, 'status': status, 'conclusion': conclusion,
            'head_branch': branch, 'name': wf, 'run_started_at': started,
            'head_sha': 'abc1234', 'actor': {'login': 'a'}, 'html_url': 'u'}


class CiStormGate(unittest.TestCase):
    def setUp(self):
        self.snap = dict(serve._CI_STORM_STATE)
        serve._CI_STORM_STATE.clear()
        self.addCleanup(serve._CI_STORM_STATE.update, self.snap)

    def test_newer_green_voids_older_red(self):
        runs = [_run(1, conclusion='failure', started='2026-11-23T20:00:00Z'),
                _run(2, conclusion='success', started='2026-11-23T21:00:00Z')]
        keep, fam = serve._ci_storm_gate(runs, [runs[0]])
        self.assertEqual(keep, [], 'stale red run must be suppressed')
        self.assertEqual(fam, {})

    def test_red_without_newer_green_survives(self):
        runs = [_run(1, conclusion='failure', started='2026-11-23T21:00:00Z'),
                _run(2, conclusion='success', started='2026-11-23T20:00:00Z')]
        keep, fam = serve._ci_storm_gate(runs, [runs[0]])
        self.assertEqual([r['id'] for r in keep], [1])
        self.assertEqual(fam[1], 'main|ci')

    def test_pump_cap_one_ask_per_family_per_window(self):
        r1 = _run(1)
        keep1, _ = serve._ci_storm_gate([r1], [r1])
        self.assertEqual(len(keep1), 1)
        r2 = _run(2, started='2026-11-23T21:05:00Z')  # same family, later
        keep2, _ = serve._ci_storm_gate([r2], [r2])
        self.assertEqual(keep2, [], 'second same-family ask inside the '
                         'window must be capped')

    def test_different_branches_are_different_families(self):
        r1 = _run(1, branch='main')
        _keep, _ = serve._ci_storm_gate([r1], [r1])
        r2 = _run(2, branch='feature')
        keep2, fam2 = serve._ci_storm_gate([r2], [r2])
        self.assertEqual([r['id'] for r in keep2], [2])
        self.assertEqual(fam2[2], 'feature|ci')

    def test_unparseable_time_fails_open(self):
        r1 = _run(1, started='not-a-date')
        keep, fam = serve._ci_storm_gate([r1], [r1])
        self.assertEqual([r['id'] for r in keep], [1])
        self.assertEqual(fam[1], 'main|ci')

    def test_watch_once_uses_the_gate(self):
        # wiring check: a red run + a newer green run appends nothing.
        seen = []
        with mock.patch.object(serve, '_load_watch_file',
                               return_value={'runs': [], 'issues': []}), \
             mock.patch.object(serve, '_save_watch_file'), \
             mock.patch.object(serve, '_github_call',
                               side_effect=[({'workflow_runs': [
                                   _run(9, conclusion='failure',
                                        started='2026-11-23T20:00:00Z'),
                                   _run(10, conclusion='success',
                                        started='2026-11-23T21:00:00Z')]},
                                  None),
                                 ([], None)]), \
             mock.patch.object(serve, '_github_watch_append',
                               side_effect=lambda e: seen.append(e)), \
             mock.patch.object(serve, 'log_action'):
            serve._github_watch_once('x/y')
        self.assertEqual(seen, [])


if __name__ == '__main__':
    unittest.main()
