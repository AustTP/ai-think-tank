"""Phase E addendum: the two-agent peer-approval gate.

Deliverable-room stories (Work Room / Research Center) no longer complete to
'done' when their primary content work finishes -- they move to 'needs_review'
and wait for two distinct same-team peers to approve. This suite covers the
gate's pure helpers AND the _apply_content_result verdict folding, hermetic
(no network). The "no DB" half of that claim only held for the Integration
class below, which patches DB_PATH itself -- the other three classes call
_apply_content_result/gate-entry paths that have inline `from serve import
log_action` calls, a real side effect that wrote into whatever real
think_tank.db sits at serve.py's default path. Found 2026-09-25 via a live
production think_tank.db that picked up test fixture rows after a routine test
run; module-level isolation below covers the whole file (Integration's own
class-level patch still applies on top of it during its own tests).
"""

import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
import sim

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-peer-approval-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _state(**over):
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': [
            {'id': 'maya', 'name': 'Maya', 'role': 'director', 'isAdmin': True},
            {'id': 'ada', 'name': 'Ada', 'role': 'engineer', 'director': 'maya'},
            {'id': 'ben', 'name': 'Ben', 'role': 'engineer', 'director': 'maya'},
            {'id': 'cora', 'name': 'Cora', 'role': 'engineer', 'director': 'maya'},
            {'id': 'dev', 'name': 'Dev', 'role': 'engineer', 'director': 'zeta'},
        ],
        'agents': {
            'maya': {'id': 'maya', 'x': 0, 'y': 0, 'busy': True},
            'ada': {'id': 'ada', 'x': 10, 'y': 10, 'busy': False, 'offDuty': True, 'task': None},
            'ben': {'id': 'ben', 'x': 20, 'y': 20, 'busy': False, 'offDuty': True, 'task': None},
            'cora': {'id': 'cora', 'x': 30, 'y': 30, 'busy': False, 'offDuty': True, 'task': None},
            'dev': {'id': 'dev', 'x': 40, 'y': 40, 'busy': False, 'offDuty': True, 'task': None},
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


class ReviewerSelection(unittest.TestCase):
    def test_picks_two_distinct_teammates_never_author(self):
        state = _state()
        state['agents']['ada']['busy'] = True   # busy teammate deprioritized
        ids = sim._pick_reviewer_ids(state, 'ben')
        self.assertEqual(len(ids), 2)
        self.assertNotIn('ben', ids)
        self.assertEqual(len(set(ids)), 2)
        # Same reporting team (director pointer on the roster, not the agent).
        roster = {d['id']: d for d in state['agentRoster']}
        for rid in ids:
            self.assertEqual(roster[rid]['director'], 'maya')

    def test_skips_admin_and_isolated_agent(self):
        # 'dev' has a typo'd director (not maya) so is off-team; admins excluded.
        state = _state()
        ids = sim._pick_reviewer_ids(state, 'ben')
        self.assertTrue(all(rid in ('ada', 'cora') for rid in ids))

    def test_fallback_to_any_when_team_is_small(self):
        state = _state()
        # Only ben + ada exist as non-admin; ben is author -> only ada remains.
        state['agentRoster'] = [d for d in state['agentRoster'] if d['id'] in ('maya', 'ben', 'ada')]
        state['agents'] = {k: v for k, v in state['agents'].items()
                           if k in ('maya', 'ben', 'ada')}
        ids = sim._pick_reviewer_ids(state, 'ben')
        self.assertEqual(ids, ['ada'])

    def test_room_contributor_preferred_over_stranger_when_cross_room(self):
        # ben (author) picks up pressoffice work (cross-learned). Among the
        # two remaining same-team peers (ada, cora), the one who has actually
        # completed pressoffice work before ranks first: the known-craft
        # contributor catches context errors a room-stranger would miss.
        state = _state()
        state['agentRoster'] = [d for d in state['agentRoster'] if d['id'] in ('maya', 'ben', 'ada', 'cora')]
        state['agents'] = {k: v for k, v in state['agents'].items()
                           if k in ('maya', 'ben', 'ada', 'cora')}
        state['agents']['cora']['completedRooms'] = ['pressoffice']
        ids = sim._pick_reviewer_ids(state, 'ben', task_room='pressoffice')
        self.assertIn('cora', ids,
                      'a pressoffice-known contributor must be in the review pool')
        self.assertEqual(ids[0], 'cora',
                         'a known-craft room contributor is preferred over a room-stranger teammate')

    def test_completed_rooms_tracked_on_deliverable_finish(self):
        # The per-agent room-contributor signal is bumped by real completion,
        # not just queried -- finish_task and the peer-gate release both record it.
        state = _state()
        task = _task(room='pressoffice', status='working', entryX=None, entryY=None)
        state['agents']['ben']['task'] = task['id']
        state['tasks'][task['id']] = task
        sim.finish_task(state, 'ben', grid=None)
        self.assertIn('pressoffice', state['agents']['ben'].get('completedRooms', []))

        # The peer-gate release path records the room too (gated story).
        state2 = _state()
        t2 = _task(room='observatory', status='working', entryX=None, entryY=None)
        state2['agents']['ben']['task'] = t2['id']
        state2['tasks'][t2['id']] = t2
        sim._release_agent_gated(state2, 'ben', None)
        self.assertIn('observatory', state2['agents']['ben'].get('completedRooms', []))


class GateEntry(unittest.TestCase):
    def test_enter_peer_review_flips_status_and_enqueues_reviews(self):
        state = _state()
        task = _task()
        state['tasks'][task['id']] = task
        gate = sim._enter_peer_review(state, task, now_ms=1000)
        self.assertIsNotNone(gate)
        self.assertEqual(task['status'], 'needs_review')
        self.assertEqual(gate['approvals'], 0)
        # Two review subtasks queued, linked back to the story, pinned to reviewers.
        reviews = state['workQueue']
        self.assertEqual(len(reviews), 2)
        for q in reviews:
            self.assertEqual(q['reviewOf'], task['id'])
            self.assertEqual(q['taskType'], 'review')
            self.assertEqual(q['room'], 'pressoffice')
        self.assertEqual(sorted(q['assignedTo'] for q in reviews), sorted(gate['reviewerIds']))
        # Each review subtask carries the story's ORIGINAL AUTHOR (reviewAuthorId)
        # so an actionable review can pin the fix back to that author, not to the
        # reviewer (Phase E3).
        for q in reviews:
            self.assertEqual(q.get('reviewAuthorId'), task['assignedTo'])
        # Reviewers notified.
        for rid in gate['reviewerIds']:
            kinds = [m.get('kind') for m in state['agents'][rid].setdefault('mailbox', [])]
            self.assertIn('peer_review_request', kinds)

    def test_deliverable_rooms_only(self):
        self.assertTrue(sim._deliverable_room('pressoffice'))
        self.assertTrue(sim._deliverable_room('observatory'))
        for room in ('library', 'bank', 'postoffice'):
            self.assertFalse(sim._deliverable_room(room))


class VerdictFolding(unittest.TestCase):
    AUTH = {'author': 'ben', 'r1': 'ada', 'r2': 'cora'}

    def _gated_state(self):
        state = _state()
        task = _task()  # author ben
        state['tasks'][task['id']] = task
        sim._enter_peer_review(state, task, now_ms=1000)
        return state, task

    def _vote(self, state, reviewer, verdict, now_ms=2000):
        parent_id = next(iter(state['tasks']))
        review_task = {'id': f'rev-{reviewer}', 'reviewOf': parent_id,
                       'assignedTo': reviewer, 'taskType': 'review', 'status': 'working'}
        # Cut 4: a clean vote only counts when the quality pipeline objectively
        # passed -- so a clean verdict here carries pipelineOk=True.
        result = {'note': 'ok', 'peerVerdict': verdict}
        if verdict == 'clean':
            result['pipelineOk'] = True
        sim._apply_content_result(state, review_task, result)
        return state['tasks'][parent_id]

    def test_clean_vote_counts_toward_approvals(self):
        state, task = self._gated_state()
        parent = self._vote(state, 'ada', 'clean')
        self.assertEqual(parent['_peerGate']['approvals'], 1)
        self.assertEqual(parent['_peerGate']['approvers'], ['ada'])

    def test_two_distinct_clean_close(self):
        state, task = self._gated_state()
        sim._apply_content_result(state, {'id': 'r0', 'reviewOf': task['id'],
                                          'assignedTo': 'ada', 'taskType': 'review',
                                          'status': 'working'},
                                  {'note': 'ok', 'peerVerdict': 'clean', 'pipelineOk': True})
        parent = state['tasks'][task['id']]
        self.assertEqual(parent['_peerGate']['approvals'], 1)
        # Parent closes only via _parent_close_from_vote (needs `now`), not the fold.
        closed = sim._parent_close_from_vote(state, parent, now_ms=2000)
        self.assertFalse(closed, '1 clean vote should not close within timeout')
        self.assertEqual(parent['status'], 'needs_review')
        # Second distinct clean vote.
        sim._apply_content_result(state, {'id': 'r1', 'reviewOf': task['id'],
                                          'assignedTo': 'cora', 'taskType': 'review',
                                          'status': 'working'},
                                  {'note': 'ok', 'peerVerdict': 'clean', 'pipelineOk': True})
        parent = state['tasks'][task['id']]
        self.assertEqual(parent['_peerGate']['approvals'], 2)
        self.assertTrue(sim._parent_close_from_vote(state, parent, now_ms=3000),
                        'two distinct clean votes close the story')
        self.assertEqual(parent['status'], 'done')

    def test_same_reviewer_cannot_vote_twice(self):
        state, task = self._gated_state()
        for _ in range(2):
            sim._apply_content_result(state, {'id': 'r0', 'reviewOf': task['id'],
                                              'assignedTo': 'ada', 'taskType': 'review',
                                              'status': 'working'},
                                      {'note': 'ok', 'peerVerdict': 'clean', 'pipelineOk': True})
        parent = state['tasks'][task['id']]
        self.assertEqual(parent['_peerGate']['approvals'], 1,
                         'a second vote from the same reviewer is ignored')
        self.assertEqual(parent['_peerGate']['approvers'], ['ada'])

    def test_actionable_resets_and_notifies_author(self):
        state, task = self._gated_state()
        sim._apply_content_result(state, {'id': 'r0', 'reviewOf': task['id'],
                                          'assignedTo': 'ada', 'taskType': 'review',
                                          'status': 'working'},
                                  {'note': 'bad', 'peerVerdict': 'actionable'})
        parent = state['tasks'][task['id']]
        self.assertEqual(parent['_peerGate']['approvals'], 0)
        self.assertEqual(parent['_peerGate']['approvers'], [])
        # Author was notified of the rejection.
        kinds = [m.get('kind') for m in state['agents']['ben'].setdefault('mailbox', [])]
        self.assertIn('peer_review_rejected', kinds)

    def test_reopen_reprefers_the_prior_reviewer_pair(self):
        # Phase E3.4: a rejection (reviewer verdict OR player veto) is a request
        # to verify the flagged problem was actually fixed -- only the reviewer
        # who rejected it has that context. Re-opening a gated/done task must
        # therefore re-verify with the SAME pair, not a cold stranger.
        state, task = self._gated_state()
        original = list(state['tasks'][task['id']]['_peerGate']['reviewerIds'])
        self.assertEqual(len(original), 2)
        re_gate = sim._enter_peer_review(state, task, now_ms=5000)
        # The prior pair is re-chosen (they were at the head of the candidate
        # order and are still reachable), so the re-review keeps context.
        self.assertEqual(list(re_gate['reviewerIds']), original)

    def test_reopen_falls_back_when_prior_reviewer_unavailable(self):
        # If a prior reviewer is gone/unreachable, the gate falls back to a fresh
        # pick instead of deadlocking -- it must still return a full pair.
        state, task = self._gated_state()
        original = list(state['tasks'][task['id']]['_peerGate']['reviewerIds'])
        # Give the pool more live candidates so a drop still leaves a full pair.
        state['agentRoster'] += [
            {'id': 'eli', 'role': 'engineer', 'director': 'maya'},
            {'id': 'fay', 'role': 'engineer', 'director': 'maya'},
        ]
        state['agents']['eli'] = {'id': 'eli', 'busy': False, 'offDuty': False, 'task': None}
        state['agents']['fay'] = {'id': 'fay', 'busy': False, 'offDuty': False, 'task': None}
        for old in original:
            state['agents'].pop(old, None)  # reviewer no longer exists
        re_gate = sim._enter_peer_review(state, task, now_ms=5000)
        self.assertIsNotNone(re_gate, 'must fall back rather than return None')
        self.assertEqual(len(re_gate['reviewerIds']), 2)
        self.assertTrue(all(rid != old for old in original for rid in re_gate['reviewerIds']),
                        'former reviewers dropped from the pool must not be re-picked')

    def test_timeout_one_clean_vote_closes(self):
        state, task = self._gated_state()
        sim._apply_content_result(state, {'id': 'r0', 'reviewOf': task['id'],
                                          'assignedTo': 'ada', 'taskType': 'review',
                                          'status': 'working'},
                                  {'note': 'ok', 'peerVerdict': 'clean', 'pipelineOk': True})
        parent = state['tasks'][task['id']]
        now = parent['_peerGate']['enteredMs'] + sim.PEER_REVIEW_TIMEOUT_MS + 1
        self.assertTrue(sim._parent_close_from_vote(state, parent, now_ms=now),
                        'one clean vote + elapsed timeout closes')
        self.assertEqual(parent['status'], 'done')


class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp = None

    def test_task_cycle_routes_deliverable_to_gate_and_closes_on_two_reviews(self):
        import tempfile, os, shutil
        import serve
        self.tmp = tempfile.mkdtemp(prefix='think tank-peer-')
        cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp, AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        cm.start()
        self.addCleanup(cm.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        serve.init_db()

        # Deterministic content: primary work stores a note; a review subtask
        # stores a clean verdict.
        real_store = sim._store_content_result
        sim._content_executor = None
        sim._store_content_result = real_store

        grid, doors = sim._load_outdoor_geometry()
        # A single on-duty engineer at the pressoffice door approach gets the
        # primary task deterministically (only ada is on-duty on tick 1, so the
        # round-robin picks her), and the story reliably reaches needs_review
        # without any wake-spot reachability lottery.
        po = (doors or {}).get('pressoffice') or {'x': 1188, 'y': 646, 'w': 52, 'h': 50}
        px, py = po['x'] + po['w'] / 2, po['y'] + po['h'] + 4
        engineers = {
            'ada': {'id': 'ada', 'x': px, 'y': py, 'busy': False, 'task': None,
                    'offDuty': False, 'visible': True},
            # ben and cora are off-duty BUT NOT busy. Off-duty agents are not
            # eligible for the on-duty primary on tick 1 (the wake rule only
            # wakes an off-duty agent for a pinned review), yet they stay
            # assignable to their pinned review subtasks once the gate picks
            # them (the pin's _spawn_at_room_door wakes each reviewer).
            'ben': {'id': 'ben', 'x': 0, 'y': 0, 'busy': False, 'task': None,
                    'offDuty': True, 'visible': False},
            'cora': {'id': 'cora', 'x': 0, 'y': 0, 'busy': False, 'task': None,
                     'offDuty': True, 'visible': False},
        }
        state = {
            'sim': {'owner': 'server'},
            'agentRoster': [
                {'id': 'maya', 'name': 'Maya', 'role': 'director', 'isAdmin': True, 'director': None},
                {'id': 'ada', 'name': 'Ada', 'role': 'engineer', 'director': 'maya'},
                {'id': 'ben', 'name': 'Ben', 'role': 'engineer', 'director': 'maya'},
                {'id': 'cora', 'name': 'Cora', 'role': 'engineer', 'director': 'maya'},
            ],
            'agents': dict({'maya': {'id': 'maya', 'x': 0, 'y': 0, 'busy': True, 'task': None, 'offDuty': True}}, **engineers),
            'workQueue': [
                {'title': 'Build checkout', 'room': 'pressoffice', 'instructions': 'do it',
                 'goal': 'storefront', 'taskType': 'code',
                 'projectLabel': 'storefront', 'notBefore': None, 'priority': sim.WORK_PRIORITY['normal']},
            ],
            'tasks': {},
            'reports': [],
            'researchTopics': [],
            'lastSkillReviewAt': 10 ** 15,
            'sprints': [],
        }
        serve.save_state_to_db(state)
        engine = sim.SimEngine()
        engine._grid, engine._doors = grid, doors
        now = 1000.0

        def fake_executor(snapshot, agent_id, task, base_ctx):
            # Only the primary code task dispatches real work in this harness.
            real_store(task['id'], {'note': 'wrote the code'})

        sim._content_executor = fake_executor

        # Phase 1: tick until the primary story reaches needs_review (ada works
        # it, the author is released, two review subtasks are queued). This is
        # the gate-entry routing -- deterministic because ada is the sole
        # on-duty engineer at the door.
        gated = False
        for _ in range(300):
            now += sim.SIM_TICK_S
            state = serve.get_state_from_db()
            state = engine.tick(state, now=now)
            serve.save_state_to_db(state)
            tasks = state.get('tasks') or {}
            if tasks and any(t.get('status') == 'needs_review' for t in tasks.values()):
                gated = True
                break
        self.assertTrue(gated, 'primary deliverable task never reached needs_review')
        state = serve.get_state_from_db()
        tasks = state.get('tasks') or {}
        story = next(t for t in tasks.values() if t.get('status') == 'needs_review')
        reviewers = story['_peerGate']['reviewerIds']
        self.assertEqual(len(reviewers), 2, 'a gate must pick two reviewers')
        # Both review subtasks exist -- either still queued OR already lifted
        # into live review tasks (the same _task_cycle pass can enqueue AND
        # start assigning them) -- and each is wired back to the story.
        q = state.get('workQueue') or []
        live = [t for t in (state.get('tasks') or {}).values() if t.get('reviewOf') == story['id']]
        # Build the set of distinct review links (queued or already-lifted).
        queued_links = {(x.get('title'), x.get('assignedTo')) for x in q if x.get('reviewOf') == story['id']}
        for t in live:
            queued_links.add(('review-of-' + t.get('title'), t.get('assignedTo')))
        self.assertGreaterEqual(len(queued_links), 2,
                                f'both review subtasks must be traced back to the story '
                                f'(got {len(queued_links)} from queue {q} + live {live})')
        # Whatever form the reviews are in, their reviewers must be exactly the
        # gate's chosen pair (each review is pinned to one distinct reviewer).
        pinned = {t.get('assignedTo') for t in live} | {x.get('assignedTo') for x in q if x.get('reviewOf') == story['id']}
        self.assertEqual(sorted(pinned), sorted(reviewers),
                         'review tasks must be pinned to the gate\'s two reviewers')

        # Phase 2: drive the two reviewers' clean verdicts through the SAME fold
        # path _task_cycle uses (_apply_content_result + _parent_close_from_vote
        # on the review result), independent of movement geometry. The first
        # clean vote counts 1; the second closes the story.
        sim._apply_content_result(state,
                                  {'id': 'rev-1', 'reviewOf': story['id'], 'assignedTo': reviewers[0],
                                   'taskType': 'review', 'status': 'working'},
                                  {'note': 'looks good', 'peerVerdict': 'clean', 'pipelineOk': True})
        self.assertFalse(sim._parent_close_from_vote(state, story, now_ms=int(now * 1000)),
                         'one clean vote must not close within the timeout')
        sim._apply_content_result(state,
                                  {'id': 'rev-2', 'reviewOf': story['id'], 'assignedTo': reviewers[1],
                                   'taskType': 'review', 'status': 'working'},
                                  {'note': 'solid', 'peerVerdict': 'clean', 'pipelineOk': True})
        self.assertTrue(sim._parent_close_from_vote(state, story, now_ms=int(now * 1000)),
                        'two distinct clean votes close the story')
        self.assertEqual(story['status'], 'done', 'story must be done after close')
        serve.save_state_to_db(state)

        # The close persists to the authoritative DB.
        final = serve.get_state_from_db()
        tasks = final.get('tasks') or {}
        closed = [t for t in tasks.values() if t.get('status') == 'done' and t.get('_peerGate', {}).get('closed')]
        self.assertTrue(closed, 'a closed story must persist as done + gate.closed')


class BoundedReviewEscalation(unittest.TestCase):
    """Bounded review-cycle escalation (2026-09-26): real gap caught live --
    a promoted follow-up story cycled through review->fix->review 45+ times
    in under 20 minutes with no bound at all. sim._maybe_escalate_stuck_gate
    is the shared counter checked from both re-entry mechanisms
    (_apply_content_result's 'actionable' fold here, and
    _sweep_stuck_gates's own rescue path -- see test_stuck_gate.py)."""

    def _gated_state(self):
        state = _state()
        task = _task()
        state['tasks'][task['id']] = task
        sim._enter_peer_review(state, task, now_ms=1000)
        return state, task

    def _reject(self, state, parent_id, reviewer='ada'):
        review_task = {'id': f'rev-{reviewer}-{sim.MAX_REVIEW_CYCLES}', 'reviewOf': parent_id,
                       'assignedTo': reviewer, 'taskType': 'review', 'status': 'working'}
        sim._apply_content_result(state, review_task, {'note': 'bad', 'peerVerdict': 'actionable'})
        return state['tasks'][parent_id]

    def test_pure_counter_escalates_exactly_at_the_threshold(self):
        parent = {'id': 't1', 'title': 'A story'}
        gate = {}
        with unittest.mock.patch.object(serve, 'create_escalation') as esc:
            for i in range(sim.MAX_REVIEW_CYCLES - 1):
                frozen = sim._maybe_escalate_stuck_gate(parent, gate, 'test reason')
                self.assertFalse(frozen, f'must not freeze before the threshold (cycle {i+1})')
            esc.assert_not_called()
            frozen = sim._maybe_escalate_stuck_gate(parent, gate, 'test reason')
        self.assertTrue(frozen)
        self.assertTrue(gate['escalated'])
        esc.assert_called_once()
        self.assertIn('A story', esc.call_args.args[1])

    def test_pure_counter_is_idempotent_once_escalated(self):
        parent = {'id': 't1', 'title': 'A story'}
        gate = {'escalated': True, 'cycleCount': 99}
        with unittest.mock.patch.object(serve, 'create_escalation') as esc:
            frozen = sim._maybe_escalate_stuck_gate(parent, gate, 'test reason')
        self.assertTrue(frozen)
        esc.assert_not_called()  # already escalated once -- never re-fires
        self.assertEqual(gate['cycleCount'], 99)  # doesn't keep counting once frozen

    def test_repeated_rejections_freeze_the_gate_instead_of_looping_forever(self):
        state, task = self._gated_state()
        with unittest.mock.patch.object(serve, 'create_escalation') as esc:
            for _ in range(sim.MAX_REVIEW_CYCLES):
                parent = self._reject(state, task['id'])
        self.assertTrue(parent['_peerGate']['escalated'])
        esc.assert_called_once()

    def test_frozen_gate_stops_resetting_approvals_on_further_rejections(self):
        state, task = self._gated_state()
        with unittest.mock.patch.object(serve, 'create_escalation'):
            for _ in range(sim.MAX_REVIEW_CYCLES):
                parent = self._reject(state, task['id'])
        # Manually give it a stray approval, then reject again -- once frozen,
        # a further rejection must NOT reset it back to 0 (that reset/notify
        # path is exactly what's supposed to stop once escalated).
        parent['_peerGate']['approvals'] = 1
        parent['_peerGate']['approvers'] = ['ada']
        with unittest.mock.patch.object(serve, 'create_escalation') as esc:
            self._reject(state, task['id'])
        self.assertEqual(parent['_peerGate']['approvals'], 1)
        esc.assert_not_called()  # no re-escalation once already frozen

    def test_below_threshold_still_resets_and_notifies_normally(self):
        # Regression guard: a normal, small number of real rejections must
        # keep working exactly as before -- the bound only kicks in once
        # genuinely exceeded.
        state, task = self._gated_state()
        with unittest.mock.patch.object(serve, 'create_escalation') as esc:
            parent = self._reject(state, task['id'])
        self.assertFalse(parent['_peerGate'].get('escalated', False))
        self.assertEqual(parent['_peerGate']['approvals'], 0)
        kinds = [m.get('kind') for m in state['agents']['ben'].setdefault('mailbox', [])]
        self.assertIn('peer_review_rejected', kinds)
        esc.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)