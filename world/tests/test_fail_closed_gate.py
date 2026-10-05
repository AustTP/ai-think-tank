"""Fail-closed quality gate + the player-filed card contract.

Two regressions, one file:

FIX 1 -- a player-filed JIRA card (file_issue -> backlogRequests -> refinement)
was handed to the coding worker as only a one-line summary: the normalized
userStory + acceptanceCriteria were never embedded into the worker's task
instructions, and they were silently dropped by queue_work's field whitelist /
_assign_due_item's extra dict / assign_task's task record. The coding executor
(the thing that actually RUNS the pipeline) never saw the spec, so its
flake8/mypy/bandit/pytest-cov line was built from a bare title.

FIX 2 -- a red-pipeline content result (the coding executor stores ok=False on a
failed quality run) could still advance a deliverable to peer review or re-arm
an existing gate. _task_cycle's content-result branch now checks
`result.get('ok')`: a gated-lane primary deliverable whose pipeline FAILED goes
to 'failed' (never 'needs_review'), the author is notified, the agent is
released WITHOUT an approval/grade bump, and a fix is queued back to the author
-- bounded by the shared MAX_REVIEW_CYCLES cap so a permanently-red story
escalates instead of looping.

DB-redirection is required: _task_cycle's pre-passes and _resolve_refinement's
carry-away both have inline `from serve import log_action` calls that would
otherwise write into whatever real think_tank.db sits at serve.py's default
path (same hazard test_refinement.py / test_peer_approval.py already document).
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
import sim

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-fail-closed-gate-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        # The standing distillation sweep content-gates on real archive mtimes;
        # point it at an empty temp dir so a machine with real archive files
        # can never inject a distill task into a test's assignment counts.
        LIBRARY_ARCHIVE_DIR=os.path.join(_TMP_DIR, 'library', 'archive'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


STORY = ('As a shopper, I want a one-click checkout so that I can buy '
         'without filling a form.')
CRITERIA = ('Given items in the cart, when I click checkout, then it '
            'completes in one step.')


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
            'maya': {'id': 'maya', 'x': 0, 'y': 0, 'busy': True, 'offDuty': True,
                     'task': None},
            'ada': {'id': 'ada', 'x': 10, 'y': 10, 'busy': False, 'offDuty': True,
                    'task': None},
            'ben': {'id': 'ben', 'x': 20, 'y': 20, 'busy': False, 'offDuty': True,
                    'task': None},
            'cora': {'id': 'cora', 'x': 30, 'y': 30, 'busy': False, 'offDuty': True,
                     'task': None},
        },
        'workQueue': [],
        'tasks': {},
        'reports': [],
    }
    state.update(over)
    return state


def _task(**over):
    task = {
        'id': 'task-1',
        'title': 'Build the checkout flow',
        'room': 'pressoffice',
        'instructions': 'implement it',
        'projectLabel': 'storefront',
        'taskType': 'code',
        'assignedTo': 'ben',
        'status': 'working',
        'createdAt': 0,
    }
    task.update(over)
    return task


def _find_fix(state, parent_id):
    """Find the fix queued (or already lifted into a walking task) for a failed
    story. The assignment loop may re-pin it to the author in the SAME pass, so
    it can live in either surface."""
    for t in (state.get('tasks') or {}).values():
        if t.get('reviewOf') == parent_id:
            return t
    for q in (state.get('workQueue') or []):
        if q.get('reviewOf') == parent_id:
            return q
    return None


class RefinementForwardsContract(unittest.TestCase):
    """FIX 1: the player's card (user story + acceptance criteria) travels from
    the refinement ceremony's resolve onto the queued story AND, via the queue
    round trip, onto the real assigned task -- the coding executor builds its
    pipeline's backlog line from task.instructions, which must carry the full
    spec, not a one-line summary."""

    def _convened_with_card(self):
        now_ms = 1_000_000
        state = {
            'sim': {'owner': 'server'},
            'agentRoster': [
                {'id': 'faye', 'name': 'Faye', 'role': 'Admin', 'isAdmin': True},
                {'id': 'dev', 'name': 'Dev', 'role': 'Director', 'isDirector': True},
                {'id': 'ben', 'name': 'Ben', 'role': 'Engineer', 'director': 'dev'},
                {'id': 'ada', 'name': 'Ada', 'role': 'Researcher', 'director': 'dev'},
            ],
            'teams': [
                {'id': 'dev', 'name': "Dev's Crew", 'directorId': 'dev',
                 'scrumMasterId': 'ada'},
            ],
            'agents': {
                'faye': {'id': 'faye', 'x': 0, 'y': 0, 'busy': True, 'task': 'task-1',
                         'inRoom': 'pressoffice', 'offDuty': False},
                'dev': {'id': 'dev', 'x': 0, 'y': 0, 'busy': False, 'task': None,
                        'offDuty': False},
                'ada': {'id': 'ada', 'x': 0, 'y': 0, 'busy': False, 'task': None,
                        'offDuty': False},
                'ben': {'id': 'ben', 'x': 0, 'y': 0, 'busy': False, 'task': None,
                        'offDuty': False},
            },
            'tasks': {'task-1': {'id': 'task-1', 'assignedTo': 'faye',
                                 'status': 'working', 'room': 'pressoffice',
                                 'title': 'Build ledger tool', 'workUntil': 5_0000}},
            'backlogRequests': [{
                'id': 'iss-JIR-1', 'filedBy': 'ben', 'title': '[BUG] One-click checkout',
                'room': 'pressoffice', 'reason': 'JIRA issue JIR-1',
                'filedAt': now_ms, 'status': 'pending',
                'origin': 'jira_issue', 'issueKey': 'JIR-1', 'teamId': 'dev',
                'issueType': 'bug', 'userStory': STORY,
                'acceptanceCriteria': CRITERIA,
            }],
            'workQueue': [],
            'lastBacklogRefinementAt': now_ms,
            'pendingRefinements': {'dev': {
                'at': sim.REFINEMENT_MEET_MS + now_ms, 'embarked': True,
                'scrumMasterId': 'ada', 'reqIds': ['iss-JIR-1'], 'teamId': 'dev',
                'people': {
                    'ben': {'offDuty': False, 'visible': True, 'x': 40, 'y': 10,
                            'dir': 'south', 'task': None, 'busy': False,
                            'inRoom': None, 'pairWith': None, 'handoff': None,
                            'workUntil': None},
                    'ada': {'offDuty': False, 'visible': True, 'x': 10, 'y': 10,
                            'dir': 'south', 'task': None, 'busy': False,
                            'inRoom': None, 'pairWith': None, 'handoff': None,
                            'workUntil': None},
                },
            }},
        }
        return state

    def _resolve(self, state, now_ms=sim.REFINEMENT_MEET_MS + 2_000_000):
        def accept(instructions, criteria):
            return 'accept'
        return sim._refinement_step(state, 1000.0, now_ms, decider=accept)

    def test_resolve_embeds_story_and_criteria_into_queued_instructions(self):
        state = self._convened_with_card()
        self._resolve(state)
        self.assertEqual(len(state['workQueue']), 1, 'the card must become a story')
        item = state['workQueue'][0]
        # The full contract is IN the worker's instructions (the coding executor
        # builds its backlog line from task.instructions), AND survives as
        # structured fields through the queue whitelist.
        self.assertIn(STORY, item['instructions'])
        self.assertIn(CRITERIA, item['instructions'])
        self.assertEqual(item['userStory'], STORY)
        self.assertEqual(item['acceptanceCriteria'], CRITERIA)
        self.assertEqual(item['teamId'], 'dev')

    def test_queue_work_whitelists_contract_fields(self):
        state = _state()
        sim.queue_work(state, [{'title': 'One-click checkout', 'room': 'pressoffice',
                                'instructions': 'go', 'userStory': STORY,
                                'acceptanceCriteria': CRITERIA}])
        item = state['workQueue'][0]
        self.assertEqual(item['userStory'], STORY)
        self.assertEqual(item['acceptanceCriteria'], CRITERIA)
        # Absent fields stay None, not KeyError / stray defaults.
        sim.queue_work(state, [{'title': 'Plain task', 'room': 'pressoffice'}])
        self.assertIsNone(state['workQueue'][1]['userStory'])
        self.assertIsNone(state['workQueue'][1]['acceptanceCriteria'])

    def test_file_issue_accepts_list_shaped_acceptance_criteria(self):
        # Regression: the JSON client sends acceptanceCriteria as a LIST (the
        # endpoint docstring's natural shape). _normalize_description used to
        # pass it straight into normalize_gwt_block, which calls .strip() on it
        # -> AttributeError 500 on POST /api/intent/issues. The list must be
        # joined into one GWT block and normalized, not crash.
        state = _state(teams=[{'id': 'dev', 'name': "Dev's Crew", 'directorId': 'dev',
                               'scrumMasterId': 'ada'}])
        issue = sim.file_issue(
            state, 'dev', 'story', 'One-click checkout', 'storefront', 'player',
            description={'userStory': 'As a shopper, I want to check out in one '
                                      'click so that I can buy faster.',
                         'acceptanceCriteria': [CRITERIA, 'Given a cart, when I pay, then it receipts.']})
        self.assertIsNotNone(issue)
        self.assertIn('I want to check out in one click', issue['userStory'])
        self.assertIn('Given items in the cart', issue['acceptanceCriteria'])
        self.assertIn('Given a cart', issue['acceptanceCriteria'])

    def test_contract_survives_to_assigned_task(self):
        # End-to-end through the real assignment path (deterministic round-robin
        # via _assign_due_item -> assign_task): the structured fields ride the
        # queue item's whitelist -> the extra dict -> the task record.
        far_future = int(time.time() * 1000) + 60 * 60 * 24 * 365 * 10
        state = {
            'sim': {'owner': 'server'},
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada', 'role': 'Research'},
                {'id': 'ben', 'name': 'Ben', 'role': 'Banking'},
                {'id': 'faye', 'name': 'Faye', 'role': 'Admin', 'isAdmin': True},
            ],
            'agents': {
                'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                        'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                        'inRoom': None, 'offDuty': False, 'stuckTimer': 0,
                        'replanCount': 0, 'approvedCount': 0},
                'ben': {'id': 'ben', 'x': sim.SPAWN['x'] + 30, 'y': sim.SPAWN['y'],
                        'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                        'inRoom': None, 'offDuty': False, 'stuckTimer': 0,
                        'replanCount': 0, 'approvedCount': 0},
                'faye': {'id': 'faye', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                         'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                         'inRoom': None, 'offDuty': False, 'stuckTimer': 0,
                         'replanCount': 0, 'approvedCount': 0},
            },
            'reports': [],
            'researchTopics': [],
            'lastSkillReviewAt': far_future,
            'lastStuckGateSweep': far_future,
            'lastSocialAt': far_future,
            'lastDistillAt': far_future,
            'lastRuleMineAt': far_future,
            'workQueue': [{'title': 'One-click checkout', 'room': 'pressoffice',
                           'instructions': 'go', 'goal': 'storefront',
                           'projectLabel': 'storefront', 'taskType': 'code',
                           'priority': sim.WORK_PRIORITY['normal'],
                           'userStory': STORY, 'acceptanceCriteria': CRITERIA}],
        }
        grid, doors = sim._load_outdoor_geometry()
        sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                        task_id_holder=[0])
        assigned = [aid for aid in state['agents'] if state['agents'][aid].get('task')]
        self.assertEqual(len(assigned), 1)
        task = state['tasks'][state['agents'][assigned[0]]['task']]
        self.assertEqual(task['userStory'], STORY)
        self.assertEqual(task['acceptanceCriteria'], CRITERIA)


class SendBackAfterFailure(unittest.TestCase):
    """FIX 2 helpers: a red-pipeline result send the story back to 'failed' and
    queue a fix back to the ORIGINAL author -- bounded by the shared cycle cap,
    so a permanently-red story escalates instead of looping."""

    def test_send_back_marks_failed_and_pins_fix_to_author(self):
        state = _state()
        task = _task(assignedTo='ben', userStory=STORY, acceptanceCriteria=CRITERIA)
        state['tasks'][task['id']] = task
        sim._send_back_after_failure(state, task, fail_note='flake8 failed')
        # Never advances to the peer gate; it is failed, not needs_review.
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['failNote'], 'flake8 failed')
        self.assertIsNotNone(task['_peerGate'], 'a real gate is seeded for the cap')
        # A fix is queued back to the original author, pinned, high priority,
        # carrying the full contract for the coding executor.
        self.assertEqual(len(state['workQueue']), 1)
        fix = state['workQueue'][0]
        self.assertEqual(fix['reviewOf'], task['id'])
        self.assertEqual(fix['assignedTo'], 'ben')
        self.assertEqual(fix['taskType'], 'code')
        self.assertEqual(fix['priority'], sim.WORK_PRIORITY['high'])
        self.assertEqual(fix['userStory'], STORY)
        self.assertEqual(fix['acceptanceCriteria'], CRITERIA)
        # The author was notified the pipeline failed (not a reviewer verdict).
        kinds = [m.get('kind') for m in state['agents']['ben'].setdefault('mailbox', [])]
        self.assertIn('peer_review_rejected', kinds)

    def test_send_back_escalates_at_cap_and_stops_queuing_fixes(self):
        state = _state()
        task = _task(assignedTo='ben')
        state['tasks'][task['id']] = task
        with unittest.mock.patch.object(serve, 'create_escalation') as esc:
            for _ in range(sim.MAX_REVIEW_CYCLES - 1):
                sim._send_back_after_failure(state, task)
            self.assertFalse(task['_peerGate'].get('escalated'))
            sim._send_back_after_failure(state, task)  # the cap-crossing call
        self.assertTrue(task['_peerGate']['escalated'], 'cap crossed -> frozen')
        esc.assert_called_once()
        # Exactly cap-1 fixes were ever queued; the cap-crossing call queues none.
        self.assertEqual(len(state['workQueue']), sim.MAX_REVIEW_CYCLES - 1)

    def test_enter_peer_review_carries_cycle_count_and_escalation(self):
        # A re-armed gate (via _send_back's fix completing, or a reviewer
        # rejection) must carry the prior gate's cycleCount + escalated flag --
        # otherwise a re-entry RESETS the shared cap and the loop starts over.
        state = _state()
        task = _task()
        state['tasks'][task['id']] = task
        task['_peerGate'] = {'approvals': 0, 'approvers': [],
                             'reviewerIds': ['ada', 'cora'], 'enteredMs': 1000,
                             'cycleCount': 4, 'escalated': False}
        gate = sim._enter_peer_review(state, task, now_ms=5000)
        self.assertEqual(gate['cycleCount'], 4, 'cycle count must survive re-arm')
        self.assertFalse(gate['escalated'])
        # An already-escalated gate re-arms only as a display entry -- it stays
        # frozen; callers guard on `escalated`, never re-start the loop.
        task['_peerGate']['escalated'] = True
        gate2 = sim._enter_peer_review(state, task, now_ms=6000)
        self.assertTrue(gate2['escalated'])

    def test_send_back_writes_pipeline_feedback_growth_plan(self):
        state = _state()
        task = _task(assignedTo="ben", userStory=STORY, acceptanceCriteria=CRITERIA)
        state["tasks"][task["id"]] = task
        sim._send_back_after_failure(state, task, fail_note='flake8 failed')
        plans = state['growthPlans']['ben']
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]['kind'], 'pipeline_feedback')
        self.assertIn('flake8 failed', plans[0]['note'])
        self.assertIn('Your deliverable', plans[0]['note'])
        aug = sim._augment_task_instructions(state,'ben',None,'Next deliverable')
        self.assertIn('flake8 failed', aug)
        self.assertNotIn('flake8 failed', sim._augment_task_instructions(state,'ben',None,'Another task'))
        self.assertTrue(state['growthPlans']['ben'][0]['applied'])

    def test_send_back_without_author_skips_feedback_plan(self):
        state = _state()
        task = _task(assignedTo=None)
        state["tasks"][task["id"]] = task
        with unittest.mock.patch.object(sim, 'queue_work'), \
             unittest.mock.patch.object(sim, '_write_growth_plan') as wgp:
            sim._send_back_after_failure(state, task, fail_note='flake8 failed')
        wgp.assert_not_called()
        self.assertEqual(task['status'], 'failed')

    def test_release_after_failure_clears_agent_without_bumps(self):
        # A red deliverable is NOT shipped work: releasing its author must not
        # bump approvedCount, record a completed room, grade, or file a
        # follow-up -- and the task itself is marked 'done' so the orphan-reclaim
        # never re-issues it (it was already sent back via _send_back).
        state = _state()
        state['agents']['ben']['task'] = 'task-1'
        state['agents']['ben']['busy'] = True
        state['agents']['ben']['inRoom'] = 'pressoffice'
        state['agents']['ben']['approvedCount'] = 3
        state['agents']['ben']['weekApprovals'] = 2
        state['agents']['ben']['completedRooms'] = ['pressoffice']
        task = _task(assignedTo='ben')
        state['tasks'][task['id']] = task
        sim._release_agent_after_failure(state, 'ben', task)
        a = state['agents']['ben']
        self.assertIsNone(a['task'])
        self.assertFalse(a['busy'])
        self.assertIsNone(a['inRoom'])
        self.assertEqual(a.get('approvedCount'), 3, 'no approval bump on a failure')
        self.assertEqual(a.get('weekApprovals'), 2)
        self.assertEqual(a.get('completedRooms'), ['pressoffice'],
                         'no completed-room recorded for a failed deliverable')
        self.assertEqual(task['status'], 'done')


class FailClosedTaskCycle(unittest.TestCase):
    """FIX 2 integration through the real _task_cycle completion loop: a content
    result with ok=False on a gated-lane deliverable must NEVER enter the peer
    gate, and a failed fix must never re-arm it."""

    def _quiet_base(self, task, agent_id='ada'):
        """A server-owned think tank with ONE working, content-in-flight task and
        every standing-sweep cadence stamped at 'now' (test_idle_park's pattern:
        a stamp equal to now_ms keeps every cadence quiet, unlike a far-future
        stamp, which the legacy-clamp treats as "never ran"). Returns the state
        plus the now_ms to drive _task_cycle with. The completion loop is then
        the only thing that moves."""
        now_ms = int(time.time() * 1000)
        state = {
            'sim': {'owner': 'server'},
            'agentRoster': [
                {'id': 'faye', 'name': 'Faye', 'role': 'Admin', 'isAdmin': True},
                {'id': 'ada', 'name': 'Ada', 'role': 'Research'},
                {'id': 'ben', 'name': 'Ben', 'role': 'Engineering'},
                {'id': 'cora', 'name': 'Cora', 'role': 'Engineering'},
            ],
            'agents': {
                'faye': {'id': 'faye', 'x': 0, 'y': 0, 'busy': True, 'task': None,
                         'offDuty': True, 'visible': False},
                'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                        'busy': False, 'task': None, 'offDuty': False, 'visible': True},
                'ben': {'id': 'ben', 'x': sim.SPAWN['x'] + 30, 'y': sim.SPAWN['y'],
                        'busy': False, 'task': None, 'offDuty': True, 'visible': False},
                'cora': {'id': 'cora', 'x': sim.SPAWN['x'] + 60, 'y': sim.SPAWN['y'],
                         'busy': False, 'task': None, 'offDuty': True, 'visible': False},
            },
            'reports': [],
            'researchTopics': [],
            'lastSkillReviewAt': now_ms,
            'lastStuckGateSweep': now_ms,
            'lastSocialAt': now_ms,
            'lastDistillAt': now_ms,
            'lastRuleMineAt': now_ms,
            'workQueue': [],
            'tasks': {task['id']: task},
        }
        state['agents'][agent_id]['task'] = task['id']
        state['agents'][agent_id]['busy'] = True
        state['agents'][agent_id]['inRoom'] = task.get('room')
        return state, now_ms

    def test_red_primary_result_never_enters_the_peer_gate(self):
        task = _task(assignedTo='ada', status='working', _contentInFlight=True,
                     userStory=STORY, acceptanceCriteria=CRITERIA)
        state, now_ms = self._quiet_base(task)
        task['workUntil'] = now_ms / 1000.0 + 10
        sim._store_content_result(task['id'], {'note': 'flake8 failed', 'ok': False})
        grid, doors = sim._load_outdoor_geometry()
        # Start the id counter high so the queued fix can never collide with the
        # fixture task-1 (in production the server counter is monotonic).
        sim._task_cycle(state, now=now_ms / 1000.0, grid=grid, doors=doors,
                        task_id_holder=[100])
        # Fail-closed: the deliverable is FAILED, never needs_review, and no
        # reviewer pair was ever picked (the gate was never entered).
        self.assertEqual(task['status'], 'failed')
        self.assertNotEqual(task['status'], 'needs_review')
        self.assertFalse((task.get('_peerGate') or {}).get('reviewerIds'),
                         'a red deliverable must not get reviewers pinned')
        # Author notified of the pipeline failure.
        kinds = [m.get('kind') for m in state['agents']['ada'].setdefault('mailbox', [])]
        self.assertIn('peer_review_rejected', kinds)
        # Author released from the failed story WITHOUT an approval/grade bump (a
        # red result is not shipped work).
        a = state['agents']['ada']
        self.assertNotEqual(a.get('task'), 'task-1',
                            'author must be released from the failed story')
        self.assertEqual(a.get('approvedCount', 0), 0,
                         'a failed deliverable must not count as an approval')
        self.assertFalse(a.get('completedRooms'),
                         'a failed deliverable must not record a completed room')
        # The fix is queued (and the same pass re-pins it straight back to the
        # author) carrying the full contract for the coding executor.
        fix = _find_fix(state, task['id'])
        self.assertIsNotNone(fix, 'a fix must be queued or re-pinned for the failed story')
        self.assertEqual(fix['assignedTo'], 'ada')
        self.assertEqual(fix['userStory'], STORY)
        self.assertEqual(fix['acceptanceCriteria'], CRITERIA)
        # Not escalated on the first cycle.
        self.assertFalse(task['_peerGate'].get('escalated'))

    def test_ok_result_still_enters_the_peer_gate(self):
        # Control: the same path with ok=True (the normal case) still advances to
        # needs_review -- the new check must not break the happy path.
        task = _task(assignedTo='ada', status='working', _contentInFlight=True)
        state, now_ms = self._quiet_base(task)
        task['workUntil'] = now_ms / 1000.0 + 10
        sim._store_content_result(task['id'], {'note': 'wrote the code', 'ok': True})
        grid, doors = sim._load_outdoor_geometry()
        sim._task_cycle(state, now=now_ms / 1000.0, grid=grid, doors=doors,
                        task_id_holder=[100])
        self.assertEqual(task['status'], 'needs_review')
        self.assertEqual(len(task['_peerGate']['reviewerIds']), 2)

    def test_failed_fix_does_not_rearm_the_gate(self):
        # A FIX subtask whose pipeline failed must NOT re-enter the parent gate:
        # the parent stays 'failed' and another fix is queued (cycle-capped), so
        # a permanently-red story cannot loop review->fix->review forever.
        fix = _task(id='task-2', title='Fix: Build the checkout flow',
                    assignedTo='ben', status='working', _contentInFlight=True,
                    reviewOf='task-1', userStory=STORY, acceptanceCriteria=CRITERIA)
        state, now_ms = self._quiet_base(fix, agent_id='ben')
        fix['workUntil'] = now_ms / 1000.0 + 10
        parent = _task(assignedTo='ben', status='needs_review',
                       userStory=STORY, acceptanceCriteria=CRITERIA,
                       _peerGate={'approvals': 0, 'approvers': [],
                                  'reviewerIds': ['ada', 'cora'], 'enteredMs': 0,
                                  'cycleCount': 1, 'escalated': False})
        state['tasks'] = {'task-1': parent, 'task-2': fix}
        fix = state['tasks']['task-2']
        state['agents']['ben']['task'] = 'task-2'
        state['agents']['ben']['busy'] = True
        state['agents']['ben']['inRoom'] = 'pressoffice'
        sim._store_content_result('task-2', {'note': 'still red', 'ok': False})
        grid, doors = sim._load_outdoor_geometry()
        sim._task_cycle(state, now=now_ms / 1000.0, grid=grid, doors=doors,
                        task_id_holder=[100])
        # Parent stays failed -- the gate was NOT re-armed (no fresh reviewers,
        # status never bounced back to needs_review).
        self.assertEqual(parent['status'], 'failed')
        self.assertFalse(parent.get('_peerGate', {}).get('escalated'))
        self.assertEqual(parent['_peerGate']['cycleCount'], 2,
                         'the shared cap keeps counting across mechanisms')
        # The fix itself is terminal (released as done) and the author is freed
        # from it, with no approval/grade bump.
        self.assertEqual(fix['status'], 'done')
        self.assertNotEqual(state['agents']['ben'].get('task'), 'task-2')
        self.assertEqual(state['agents']['ben'].get('approvedCount', 0), 0)
        # Another fix is queued (or already re-pinned to the author).
        next_fix = _find_fix(state, parent['id'])
        self.assertIsNotNone(next_fix, 'a fresh fix must follow the failed one')
        self.assertEqual(next_fix['assignedTo'], 'ben')

    def test_quality_gate_reject_writes_red_pipeline_telemetry(self):
        # The fail-closed send-back must leave a durable, queryable marker so the
        # health check can count red-pipeline rejects vs. escapes (the
        # selfProposedRejected pattern). A red primary deliverable logs a
        # quality_gate_reject row with redPipelineEscaped=False; an escalated
        # send-back logs redPipelineEscalated=True on the same marker.
        task = _task(assignedTo='ada', status='working', _contentInFlight=True)
        state, now_ms = self._quiet_base(task)
        task['workUntil'] = now_ms / 1000.0 + 10
        sim._store_content_result(task['id'], {'note': 'flake8 failed', 'ok': False})
        grid, doors = sim._load_outdoor_geometry()
        sim._task_cycle(state, now=now_ms / 1000.0, grid=grid, doors=doors,
                        task_id_holder=[100])
        self.assertEqual(task['status'], 'failed')
        rows = []
        with serve._db() as conn:
            rows = conn.execute(
                "SELECT details FROM action_log WHERE action = 'quality_gate_reject' "
                "ORDER BY id").fetchall()
        self.assertTrue(rows, 'a failed deliverable must log a quality_gate_reject row')
        import json as _json
        latest = _json.loads(rows[-1][0])
        self.assertEqual(latest.get('redPipelineEscaped'), False,
                         'a caught red result must never be flagged escaped')
        self.assertEqual(latest.get('taskId'), task['id'])
        self.assertFalse(latest.get('redPipelineEscalated'),
                         'first cycle is not the escalation')

    def test_escalated_send_back_marks_red_pipeline_escalated(self):
        # Once the shared cycle cap is hit, the send-back escalates and the same
        # marker records it -- so the health signal can separate routine rejects
        # from cap-escalations.
        parent = _task(assignedTo='ben', status='needs_review',
                       _peerGate={'approvals': 0, 'approvers': [],
                                  'reviewerIds': ['ada', 'cora'], 'enteredMs': 0,
                                  'cycleCount': sim.MAX_REVIEW_CYCLES,
                                  'escalated': False})
        state, now_ms = self._quiet_base(parent, agent_id='ben')
        sim._send_back_after_failure(state, parent,
                                     fail_note='red flake8, cycle cap crossed')
        self.assertTrue(parent['_peerGate']['escalated'])
        rows = []
        with serve._db() as conn:
            rows = conn.execute(
                "SELECT details FROM action_log WHERE action = 'quality_gate_reject' "
                "ORDER BY id").fetchall()
        self.assertTrue(rows)
        import json as _json
        latest = _json.loads(rows[-1][0])
        self.assertEqual(latest.get('redPipelineEscalated'), True)
        self.assertEqual(latest.get('redPipelineEscaped'), False)


if __name__ == '__main__':
    unittest.main(verbosity=2)