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

import core.config as config
import serve
import sim


class WorkerSpikeIssueFiling(unittest.TestCase):
    """W3: autonomous issue filing. A spike worker's executor reports a `fileIssue`
    WISH (it runs off-thread against a snapshot); the tick consumes it inside
    _apply_content_result -> _file_spike_issue, which resolves the owning team
    (wish teamId -> task teamId -> product director -> roster director, each
    matched by id OR directorId), dedups against an already-open issue by the
    same reporter + summary, files via the real file_issue, and logs governance.
    Fail safe: no filing on unknown team / malformed wish / missing reporter.
    Purely state-based -- no Jev, no network."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-sim-file-issue-')
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

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self, teams=None, with_director=True):
        roster = [{'id': 'ada', 'name': 'Ada', 'role': 'Research', 'isAdmin': False}]
        if with_director:
            roster[0]['director'] = 'zoe'
        return {
            'sim': {'owner': 'server'},
            'agentRoster': roster,
            'agents': {
                'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                        'dir': 'south', 'visible': True, 'busy': True,
                        'task': 'task-1', 'inRoom': 'pressoffice', 'offDuty': False},
            },
            'teams': teams if teams is not None else [
                {'id': 'dev', 'directorId': 'zoe', 'scrumMasterId': 'ben',
                 'room': 'pressoffice'},
            ],
            'products': {},
            'tasks': {'task-1': {'id': 'task-1', 'title': 'Spike: investigate auth',
                                 'room': 'pressoffice', 'status': 'working',
                                 'assignedTo': 'ada'}},
            'workQueue': [],
        }

    def _wish(self, **over):
        wish = {
            'issueType': 'story',
            'summary': 'Auth flow silently drops logins',
            'title': 'Auth: surface login failures',
            'feature': 'Library Tools',
            'description': {'userStory': 'As a user, I want to know why login '
                                         'failed, so that I can fix it.'},
        }
        wish.update(over)
        return wish

    def _task(self, **over):
        task = {'id': 'task-1', 'title': 'Spike: investigate auth',
                'room': 'pressoffice', 'status': 'working', 'assignedTo': 'ada'}
        task.update(over)
        return task

    def test_verdict_wish_files_issue_and_kicks_refinement(self):
        # A well-formed fileIssue wish files a real issue: the backlogRequests
        # pipe record is appended AND the team's refinement is kicked to due on
        # the next pass -- the worker's finding becomes real queue-able work.
        state = self._state()
        self.assertTrue(sim._file_spike_issue(state, self._wish(), self._task(),
                                              now_ms=1000))
        issues = list((state.get('issues') or {}).values())
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue['key'], 'DEV-1')
        self.assertEqual(issue['teamId'], 'dev')
        self.assertEqual(issue['type'], 'story')
        self.assertEqual(issue['reporterId'], 'ada')
        self.assertEqual(issue['feature'], 'Library Tools')
        self.assertEqual(issue['title'], 'Auth: surface login failures')
        self.assertTrue(state.get('backlogRequests'),
                        'filing feeds the backlog pipe')
        self.assertEqual((state.get('teamRefinementAt') or {}).get('dev'), 0,
                         'filing kicks the owning team\'s refinement to due')

    def test_team_resolved_by_director_id_when_wish_carries_director(self):
        # The wish may name the team's DIRECTOR id (the other key teams records
        # use) rather than the team record's `id` -- resolve to the real team.
        state = self._state()
        wish = self._wish(teamId='zoe')
        self.assertTrue(sim._file_spike_issue(state, wish, self._task(),
                                              now_ms=1000))
        issues = list((state.get('issues') or {}).values())
        self.assertEqual(issues[0]['teamId'], 'dev',
                         'a director-id wish must file under the real team id')

    def test_team_resolved_from_task_team_id(self):
        # No teamId on the wish itself: fall back to the task's teamId.
        state = self._state()
        wish = self._wish(teamId=None)
        self.assertTrue(sim._file_spike_issue(state, wish,
                                              self._task(teamId='dev'),
                                              now_ms=1000))
        issues = list((state.get('issues') or {}).values())
        self.assertEqual(issues[0]['teamId'], 'dev')

    def test_product_director_resolution_when_no_team_id(self):
        # No teamId on wish OR task: the task's productId resolves its owning
        # team's director, which matches the team record by directorId.
        state = self._state()
        state['products'] = {'prod-1': {'id': 'prod-1', 'name': 'Library Tools',
                                        'teamId': 'zoe'}}
        wish = self._wish(teamId=None)
        self.assertTrue(sim._file_spike_issue(state, wish,
                                              self._task(teamId=None,
                                                         productId='prod-1'),
                                              now_ms=1000))
        issues = list((state.get('issues') or {}).values())
        self.assertEqual(issues[0]['teamId'], 'dev')

    def test_roster_director_last_resort_team_source(self):
        # Nothing on the wish/task: the worker's roster `director` pointer is the
        # last-resort team source -- the finding still lands on the right team.
        state = self._state()
        wish = self._wish(teamId=None)
        self.assertTrue(sim._file_spike_issue(state, wish,
                                              self._task(teamId=None,
                                                         productId=None),
                                              now_ms=1000))
        issues = list((state.get('issues') or {}).values())
        self.assertEqual(issues[0]['teamId'], 'dev')

    def test_no_resolvable_team_files_nothing(self):
        # A worker with no team anywhere in the chain must never file -- a stray
        # idea can't wedge the backlog under a phantom owner.
        state = self._state(teams=[], with_director=False)
        self.assertFalse(sim._file_spike_issue(state, self._wish(teamId=None),
                                               self._task(teamId=None,
                                                          productId=None),
                                               now_ms=1000))
        self.assertFalse(state.get('issues'), 'no team -> no filing')

    def test_open_duplicate_same_reporter_summary_skipped(self):
        # The same gap reported by the same worker (same summary) that has NOT
        # reached a terminal state must not be re-filed every spike re-run.
        state = self._state()
        state['issues'] = {
            'DEV-1': {'key': 'DEV-1', 'teamId': 'dev', 'reporterId': 'ada',
                      'status': 'open', 'summary': 'Auth flow silently drops logins'},
        }
        self.assertFalse(sim._file_spike_issue(state, self._wish(), self._task(),
                                               now_ms=1000))
        self.assertEqual(len(state.get('issues')), 1,
                         'an open duplicate is never re-filed')

    def test_refile_allowed_after_terminal_status(self):
        # A terminal ('done'/'closed') issue is a resolved gap -- the spike may
        # legitimately file a fresh one for the same summary.
        state = self._state()
        state['issues'] = {
            'DEV-1': {'key': 'DEV-1', 'teamId': 'dev', 'reporterId': 'ada',
                      'status': 'done', 'summary': 'Auth flow silently drops logins'},
        }
        state['issueCounters'] = {'DEV': 1}  # next key is DEV-2, not a re-file
        self.assertTrue(sim._file_spike_issue(state, self._wish(), self._task(),
                                              now_ms=1000))
        self.assertEqual(len(state.get('issues')), 2,
                         'a terminal prior issue does not block re-filing')
        self.assertIn('DEV-2', state.get('issues'), 'the fresh filing gets its own key')

    def test_malformed_wish_fails_safe_no_filing(self):
        # Empty summary / empty feature / unknown issue type must all fail safe
        # to no filing (never a backlog wedge from a half-shaped wish).
        state = self._state()
        for bad in (self._wish(summary=''),
                    self._wish(feature=''),
                    self._wish(issueType='epic')):
            sim._file_spike_issue(state, bad, self._task(), now_ms=1000)
        self.assertFalse(state.get('issues'),
                         'malformed wishes must never file anything')

    def test_missing_reporter_fails_safe(self):
        # No reporter (task.assignedTo) -> nothing to attribute the card to.
        state = self._state()
        self.assertFalse(sim._file_spike_issue(state, self._wish(),
                                               self._task(assignedTo=None),
                                               now_ms=1000))
        self.assertFalse(state.get('issues'))

    def test_apply_content_result_consumes_fileissue_wish(self):
        # The tick integration: a content result carrying a fileIssue wish is
        # filed against the live state inside _apply_content_result.
        state = self._state()
        result = {'note': 'investigated; login failures are silently dropped',
                  'fileIssue': self._wish()}
        sim._apply_content_result(state, self._task(), result, now_ms=1000)
        issues = list((state.get('issues') or {}).values())
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['summary'], 'Auth flow silently drops logins')
        self.assertEqual(issues[0]['reporterId'], 'ada')

    def test_apply_content_result_without_wish_files_nothing(self):
        # A content result that found nothing actionable carries no fileIssue
        # wish -- the note lands, no issue is filed.
        state = self._state()
        sim._apply_content_result(state, self._task(),
                                  {'note': 'no actionable gap found'},
                                  now_ms=1000)
        self.assertFalse(state.get('issues'), 'no wish -> no filing')
