"""Tests for the server-side simulation engine (world/sim.py).

Mirrors the ServerOwnedSeed isolation pattern from test_serve.py: every test
redirects state/disk side effects into a throwaway temp directory so the real
think_tank.db is never touched.
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

import config
import serve
import sim


class IncidentRestoreProbe(unittest.TestCase):
    """BP-31 close probe: a bug task closing on a bare 'fixed it' (ok result,
    zero evidence) is loud, not silent -- restoreVerified False on the task,
    a feedback note to the on-call, and an escalation to the owning team's
    scrum master. Real fix evidence (writtenPy, an executor restore check, a
    Library artifact) closes verified; stories and failed results never probe."""

    def _state(self):
        return {
            'agents': {'rep': {'id': 'rep', 'inRoom': 'pressoffice', 'offDuty': False}},
            'agentRoster': [{'id': 'rep', 'name': 'Repa', 'role': 'Research'}],
            'teams': [{'id': 't1', 'name': 'Fixers', 'directorId': 'faye',
                       'scrumMasterId': 'ada'}],
            'products': {'p1': {'id': 'p1', 'teamId': 'faye'}},
            'tasks': {},
        }

    def _bug(self, task_type='bug', product='p1'):
        return {'id': 'k1', 'taskType': task_type, 'title': 'Login loop',
                'assignedTo': 'rep', 'productId': product, 'room': 'pressoffice'}

    def test_landed_files_close_verified(self):
        state = self._state()
        task = self._bug()
        result = {'ok': True, 'writtenPy': ['app.py', 'screens.py']}
        sim._apply_content_result(state, task, result, 1000)
        self.assertTrue(task['restoreVerified'])
        self.assertEqual(task['restoreCheck']['evidence'], 'app.py, screens.py')
        self.assertFalse(state['agents']['rep'].get('feedback'))

    def test_bare_fixed_it_closes_loud(self):
        state = self._state()
        task = self._bug()
        sim._apply_content_result(state, task, {'ok': True, 'note': 'tweaked it'}, 1000)
        self.assertFalse(task['restoreVerified'])
        self.assertEqual(task['restoreCheck']['evidence'], 'none')
        self.assertEqual(state['_pendingEscalation']['source'], 'restore_unverified')
        feedback = ' '.join(f.get('text', '')
                            for f in (state.get('_feedback') or {}).get('rep', []))
        self.assertIn('restore evidence', feedback)

    def test_executor_check_and_library_count_as_evidence(self):
        state = self._state()
        task = self._bug()
        sim._apply_content_result(state, task,
                                  {'ok': True, 'restoreCheck': 'reloaded the page, error gone',
                                   'command': 'curl -fsS http://localhost/health'}, 1000)
        self.assertTrue(task['restoreVerified'])
        self.assertIn('error gone', task['restoreCheck']['evidence'])
        task2 = self._bug()
        sim._apply_content_result(state, task2,
                                  {'ok': True, 'libraryPath': 'library/notes/fix.md'}, 1000)
        self.assertTrue(task2['restoreVerified'])

    def test_stories_failed_results_and_no_product_do_not_probe(self):
        state = self._state()
        story = self._bug(task_type='story')
        sim._apply_content_result(state, story, {'ok': True, 'note': 'shipped'}, 1000)
        self.assertNotIn('restoreVerified', story)
        bug = self._bug()
        sim._apply_content_result(state, bug, {'ok': False, 'note': 'no model tier'}, 1000)
        self.assertNotIn('restoreVerified', bug)
        self.assertIsNone(state.get('_pendingEscalation'))
        orphan = self._bug(product=None)
        orphan['productId'] = None
        sim._apply_content_result(state, orphan, {'ok': True}, 1000)
        # Evidence check still runs (recorded), but there is no owning team to
        # escalate to -- and no escalation must be filed for a bare claim.
        self.assertFalse(orphan['restoreVerified'])
        self.assertIsNone(state.get('_pendingEscalation'))

    def test_already_probed_task_never_reprobes(self):
        state = self._state()
        task = self._bug()
        task['restoreCheck'] = {'ok': True, 'ts': 1, 'evidence': 'app.py'}
        task['restoreVerified'] = True
        sim._apply_content_result(state, task, {'ok': True, 'note': 'again'}, 1000)
        self.assertEqual(task['restoreCheck'], {'ok': True, 'ts': 1, 'evidence': 'app.py'})
        self.assertTrue(task['restoreVerified'])
        self.assertIsNone(state.get('_pendingEscalation'))

    def test_unknown_product_skips_escalation_quietly(self):
        state = self._state()
        task = self._bug(product='p-ghost')
        sim._apply_content_result(state, task, {'ok': True, 'note': 'tweaked it'}, 1000)
        self.assertFalse(task['restoreVerified'])
        feedback = ' '.join(f.get('text', '')
                            for f in (state.get('_feedback') or {}).get('rep', []))
        self.assertIn('restore evidence', feedback)
        self.assertIsNone(state.get('_pendingEscalation'))

    def test_runbook_records_the_probe_verdict(self):
        state = self._state()
        task = self._bug()
        task['restoreVerified'] = False
        task['restoreCheck'] = {'ok': False, 'evidence': 'none', 'ts': 1000}
        sim._runbook_task(state, task, 1000)
        entry = state['runbooks']['p1'][-1]
        self.assertFalse(entry['restoreVerified'])
        self.assertEqual(entry['evidence'], 'none')
        fresh = self._bug()
        sim._runbook_task(state, fresh, 1000)
        self.assertIsNone(state['runbooks']['p1'][-1]['restoreVerified'])
