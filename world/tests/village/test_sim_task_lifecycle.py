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


class SimTaskLifecycle(unittest.TestCase):
    # Phase 3 slice 1: the server re-homes the task lifecycle -- queue
    # consumption, deterministic assignment, arrive/complete/off-duty -- so the
    # think tank works tasks with no browser. Assignment is DETERMINISTIC (round-
    # robin, zero JEV spend). Content execution is short-circuited (workUntil
    # budget); the per-room real executors are the next slice.

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-sim-task-')
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

    def _roster(self):
        return [
            {'id': 'ada', 'name': 'Ada', 'role': 'Research', 'isAdmin': False},
            {'id': 'ben', 'name': 'Ben', 'role': 'Banking', 'isAdmin': False},
            {'id': 'faye', 'name': 'Faye', 'role': 'Admin', 'isAdmin': True},
        ]

    def _agents(self):
        return {
            'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                    'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                    'inRoom': None, 'offDuty': False,
                    'stuckTimer': 0, 'replanCount': 0, 'approvedCount': 0},
            'ben': {'id': 'ben', 'x': sim.SPAWN['x'] + 30, 'y': sim.SPAWN['y'],
                    'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                    'inRoom': None, 'offDuty': False,
                    'stuckTimer': 0, 'replanCount': 0, 'approvedCount': 0},
            'faye': {'id': 'faye', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                     'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                     'inRoom': None, 'offDuty': False,
                     'stuckTimer': 0, 'replanCount': 0, 'approvedCount': 0},
        }

    def _base_state(self, work_queue=None):
        # lastSkillReviewAt set far in the future so the standing skill-review
        # sweep (which fires immediately on a fresh state with lastRunAt=0)
        # doesn't inject an extra queue item and muddy assignment-count tests.
        far_future = int(time.time() * 1000) + 60 * 60 * 24 * 365 * 10
        return {
            'sim': {'owner': 'server'},
            'agentRoster': self._roster(),
            'agents': self._agents(),
            'reports': [],
            'researchTopics': [],
            'lastSkillReviewAt': far_future,
            # Same cadence-stamp pattern for the E3.1 stuck-gate watchdog: it
            # records lastStuckGateSweep on EVERY tick (even a no-op sweep) so
            # it isn't re-examined too often. Pre-seed it like skill review so
            # an idle gate genuinely has nothing to write.
            'lastStuckGateSweep': far_future,
            # Weekly rule mining is a standing sweep too -- pre-seed so it can't
            # fire on a fresh state (and read the real failure ledger) mid-test.
            'lastRuleMineAt': far_future,
            # Weekly Knowledge Social is its own cadence too (stamped in
            # _task_cycle, ungated by the work gate). Pre-seed so a short idle
            # window doesn't spuriously convene the Hangout.
            'lastSocialAt': far_future,
            # The roadmap + consensus relay are standing sweeps too (same
            # cadence-stamp pattern): _roadmap_step creates the roadmap on the
            # first pass, then _consensus_relay_step rewrites
            # consensusRelay.updatedAt = now_ms on EVERY pass. Pre-seeding the
            # roadmap cadence stamp keeps them no-ops, so two back-to-back idle
            # passes are byte-identical even across a wall-clock ms boundary
            # (the idempotency assertion below).
            'lastRoadmapReviewAt': far_future,
            # Hive-mind distillation is a standing sweep too (fires immediately
            # on fresh state with no stamp). Pre-seed so it can't inject an extra
            # roll-up queue item and muddy the assignment-count expectations.
            'lastDistillAt': far_future,
            'workQueue': work_queue if work_queue is not None else [],
        }

    def _item(self, **over):
        item = {'title': 'Maintain tooling', 'room': 'pressoffice',
                'instructions': 'Go.', 'pair': False, 'notBefore': None,
                'priority': sim.WORK_PRIORITY['normal'], 'goal': None,
                'research': None, 'taskType': 'code', 'skillReview': False}
        item.update(over)
        return item

    def test_task_cycle_assigns_due_item_deterministically(self):
        state = self._base_state(work_queue=[self._item()])
        grid, doors = sim._load_outdoor_geometry()
        sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                        task_id_holder=[0])
        agents = state['agents']
        assigned = [aid for aid in agents if agents[aid].get('task')]
        self.assertEqual(len(assigned), 1, 'exactly one worker assigned')
        self.assertNotIn('faye', assigned, 'admin never assigned work')
        tid = agents[assigned[0]]['task']
        task = state['tasks'][tid]
        self.assertEqual(task['room'], 'pressoffice')
        self.assertEqual(task['status'], 'walking')
        self.assertEqual(task['assignedTo'], assigned[0])
        self.assertTrue(agents[assigned[0]].get('path'),
                        'assigned agent must have a real path to the door')
        self.assertEqual(state['workQueue'], [], 'queue drained after assignment')

    def test_task_cycle_idle_gate_spends_nothing(self):
        # An idle think tank parks its idle wanderers off duty (a
        # woken-but-never-assigned agent must vanish, per the player's "no
        # unscheduled/active agent should appear" rule), which IS a state write.
        # What it must NOT do is enqueue work, assign anyone, run governance,
        # or spend Jev. So snapshot, run, and assert the ONLY surface that moved
        # is the idle wanderers going off duty + visible=false.
        state = self._base_state()
        grid, doors = sim._load_outdoor_geometry()
        before_agents = {aid: dict(a) for aid, a in state['agents'].items()}
        sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                        task_id_holder=[0])
        # No work was created, none assigned -- the queue/roster/tasks surfaces
        # are untouched.
        self.assertEqual(state.get('workQueue') or [], [])
        self.assertEqual(state.get('tasks') or {}, {})
        for aid, a in state['agents'].items():
            self.assertIsNone(a.get('task'), f'{aid} must have no task when idle')
        # The only writes are the park: ada/ben (non-admin, idle) off duty +
        # hidden; faye (admin) left alone.
        self.assertTrue(state['agents']['ada']['offDuty'])
        self.assertFalse(state['agents']['ada']['visible'])
        self.assertTrue(state['agents']['ben']['offDuty'])
        self.assertIs(state['agents']['faye']['offDuty'], False)
        # Idempotent: a second pass writes nothing more (parked agents stay).
        settled = json.dumps(state, sort_keys=True)
        sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                        task_id_holder=[0])
        self.assertEqual(json.dumps(state, sort_keys=True), settled,
                         'second idle pass must not keep mutating state')

    def test_task_cycle_repairs_stale_busy_tokens(self):
        # A busy flag is a hold on an IN-FLIGHT ('working') task. A busy agent
        # whose task already ended (done / needs_review) or is missing, or a
        # busy agent with NO task at all, is wedged -- the hold is stale and the
        # agent can never free (server restarts mid-flight persist busy while
        # the task's status moved to done independently). task_cycle must clear
        # these so the roster doesn't bleed idle workers. A genuinely 'working'
        # task is the only legitimate reason to stay busy.
        state = self._base_state()
        tasks = state['tasks'] = {
            # ada: busy holding a DONE task -> stale, must clear.
            'task-1': {'id': 'task-1', 'title': 'done story', 'status': 'done',
                       'room': 'pressoffice', 'taskType': 'code'},
            # ben: busy holding a task in needs_review -> stale, must clear.
            'task-2': {'id': 'task-2', 'title': 'gated story', 'status': 'needs_review',
                       'room': 'observatory', 'taskType': 'code'},
            # cora: busy holding a genuinely WORKING task -> legit, must stay.
            'task-3': {'id': 'task-3', 'title': 'in flight', 'status': 'working',
                       'room': 'pressoffice', 'taskType': 'code'},
        }
        state['agents'].update({
            'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'], 'busy': True,
                    'task': 'task-1', 'inRoom': 'pressoffice', 'visible': True,
                    'offDuty': False},
            'ben': {'id': 'ben', 'x': sim.SPAWN['x'] + 60, 'y': sim.SPAWN['y'], 'busy': True,
                    'task': 'task-2', 'inRoom': 'observatory', 'visible': True,
                    'offDuty': False},
            'faye': {'id': 'faye', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'], 'busy': True,
                     'task': None, 'inRoom': None, 'visible': True, 'offDuty': False},
            'cora': {'id': 'cora', 'x': sim.SPAWN['x'] + 90, 'y': sim.SPAWN['y'], 'busy': True,
                     'task': 'task-3', 'inRoom': 'pressoffice', 'visible': True,
                     'offDuty': False},
        })
        grid, doors = sim._load_outdoor_geometry()
        # 'working' (task-3) isn't due (no workUntil), so it would be skipped by
        # the completion loop anyway -- the reconcile is what we're testing.
        before = {a: dict(state['agents'][a]) for a in ('ada', 'ben', 'faye', 'cora')}
        sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                        task_id_holder=[0])
        for aid in ('ada', 'ben', 'faye'):
            self.assertFalse(state['agents'][aid].get('busy'),
                             f'{aid}: busy holding a non-working task (or none) must be cleared')
            self.assertIsNone(state['agents'][aid].get('task'),
                              f'{aid}: a cleared busy must not keep a dangling task ref')
        self.assertTrue(state['agents']['cora'].get('busy'),
                        'cora: a busy hold on a genuinely WORKING task is legit and must survive')

    def test_task_cycle_respects_notbefore_schedule(self):
        # An item scheduled for later must sit inert (not assigned, not counted).
        # Put it ALONGSIDE an immediately-due item so we prove the future one is
        # skipped while real work still gets done.
        future = int(time.time() * 1000) + 60_000
        state = self._base_state(work_queue=[self._item(notBefore=future),
                                             self._item(title='Now')])
        grid, doors = sim._load_outdoor_geometry()
        sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                        task_id_holder=[0])
        self.assertEqual([i['title'] for i in state['workQueue']], ['Maintain tooling'],
                         'the future item must stay queued untouched')
        # The immediate item got assigned; the future one did not.
        assigned_titles = [t.get('title') for t in (state.get('tasks') or {}).values()]
        self.assertIn('Now', assigned_titles)
        self.assertNotIn('Maintain tooling', assigned_titles)

    def test_work_item_abandoned_after_max_attempts(self):
        # An ordinary queue item that genuinely has no candidate is retried up
        # to WORK_ITEM_MAX_ATTEMPTS, then abandoned -- not retried forever.
        # (A chosen agent who merely cannot walk right now returns ASSIGN_WAIT
        # and is retained with a retry window instead; see the affinity tests.)
        state = self._base_state(work_queue=[self._item(title='Unassignable')])
        grid, doors = sim._load_outdoor_geometry()
        # Force the ordinary "no assignment" result; a chosen-but-unreachable
        # agent returns ASSIGN_WAIT and is retained on purpose.
        with unittest.mock.patch.object(sim, '_assign_due_item', return_value=None):
            for _ in range(20):  # well past the 3-attempt budget
                sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                                task_id_holder=[0])
        self.assertEqual(state['workQueue'], [],
                         'unassignable item must leave the queue (abandoned)')
        self.assertFalse(any(a.get('task') for a in state['agents'].values()))

    def test_item_with_a_room_that_has_no_door_is_rescued_not_abandoned(self):
        # The wedge this guards: a producer hardcoded a room that exists nowhere
        # in door_triggers.json, so assign_task failed at the `not door` guard
        # every cycle -- 21k+ attempts over 38 hours, no agent ever able to take
        # it. Such an item is now routed to a real room and assigned.
        state = self._base_state(work_queue=[
            self._item(title='Act on mail (harold)', room='commandcenter')])
        grid, doors = sim._load_outdoor_geometry()
        for _ in range(5):
            sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                            task_id_holder=[0])
        assigned = [t for t in (state.get('tasks') or {}).values()
                    if t.get('title') == 'Act on mail (harold)']
        self.assertEqual(len(assigned), 1, 'the rescued item must be assigned')
        self.assertIn(assigned[0]['room'], doors)
        self.assertNotEqual(assigned[0]['room'], 'commandcenter')

    def test_full_closed_loop_task_completes_and_agent_goes_off_duty(self):
        # The Phase-3 acceptance seed: an empty think tank with ONE queued item and
        # one on-duty worker. Drive the ENGINE (movement + lifecycle) until the
        # task completes and the worker goes off duty.
        grid, doors = sim._load_outdoor_geometry()
        state = self._base_state(work_queue=[self._item()])
        engine = sim.SimEngine()
        engine._grid, engine._doors = grid, doors
        now = 1000.0
        for _ in range(400):  # well past walk + work + off-duty walk
            now += sim.SIM_TICK_S
            state = engine.tick(state, now=now)
            if all(a.get('offDuty') for a in state['agents'].values()
                   if a.get('id') != 'faye'):
                break
        ada, ben = state['agents']['ada'], state['agents']['ben']
        offduty = [a for a in (ada, ben) if a.get('offDuty')]
        self.assertGreater(len(offduty), 0,
                           'a worker who completed a task must end up off-duty')
        worker = offduty[0]
        self.assertFalse(worker.get('path'), 'off-duty worker should have no live path')
        tasks = state.get('tasks') or {}
        self.assertTrue(any(t.get('status') in ('done', 'needs_review')
                            for t in tasks.values()),
                        'the task must complete or enter peer review')
        self.assertTrue(
            worker.get('approvedCount', 0) > 0
            or any(t.get('status') == 'needs_review' for t in tasks.values()),
            'approvedCount bumps only when the gate closes; a story still in '
            'review must not count as approved')
        # Spare-time lane (2026-10-06): completing a real deliverable earns one
        # deferred free exploration spike, so the queue is never literally
        # empty after work -- but NO committed work may remain, and the spike
        # must be spare time (moonshot, not yet due), not a new obligation.
        q = state.get('workQueue') or []
        committed = [x for x in q if not (x.get('moonshot') and x.get('taskType') == 'spike')]
        self.assertEqual(committed, [], 'no committed work left queued after completion')
        spikes = [x for x in q if x.get('moonshot') and x.get('taskType') == 'spike']
        self.assertEqual(len(spikes), 1, 'the completed deliverable earns one free spike')
        self.assertFalse(sim.is_work_item_due(spikes[0], int(now * 1000)),
                         'a free spike is spare time -- deferred, not immediately due')

    def test_research_arrival_without_executor_uses_workuntil_fallback(self):
        # No content executor registered (the default) -> an arriving research
        # task gets the slice-1 workUntil placeholder, not content dispatch.
        state = self._base_state()
        sim._content_executor = None
        task = {'id': 'task-1', 'room': 'observatory', 'status': 'walking',
                'assignedTo': 'ada',
                'research': {'topicId': 't1', 'since': 0}, 'entryX': 10, 'entryY': 10}
        state['tasks'] = {'task-1': task}
        state['agents']['ada']['task'] = 'task-1'
        state['researchTopics'] = [{'id': 't1', 'topic': 'x', 'seenUrls': []}]
        sim._arrive_at_task(state, 'ada', now=1000.0)
        self.assertEqual(task['workUntil'], 1000.0 + sim.TASK_WORK_DURATION_S,
                         'without an executor, use the short work budget')
        self.assertFalse(task.get('_contentInFlight'),
                         'no content dispatch without an executor')

    def test_content_result_without_note_is_evidence_gap(self):
        # Completion evidence is mandatory: a content result that reports
        # success but carries NO note (no evidence of what was done) must not
        # silently complete -- the card goes back as a gap (failed + fix queued),
        # never a clean 'done' with an empty record.
        sim._content_executor = None
        with sim._content_results_lock:
            sim._content_results.clear()

        # The executor normally runs on a bg thread; to stay deterministic we
        # store the (empty-note, ok) result ourselves the moment dispatch lands.
        def fake_executor(snapshot, agent_id, task, base_ctx):
            pass

        sim._content_executor = fake_executor
        grid, doors = sim._load_outdoor_geometry()
        state = self._base_state(work_queue=[{
            'title': 'Scheduled research: x', 'room': 'observatory',
            'instructions': 'crawl', 'goal': 'x', 'priority': sim.WORK_PRIORITY['normal'],
            'notBefore': 0,
            'research': {'topicId': 't1', 'since': 0}}])
        state['researchTopics'] = [{'id': 't1', 'topic': 'x', 'seenUrls': [],
                                    'cadenceMs': 3600000, 'lastRunAt': 0}]
        engine = sim.SimEngine()
        engine._grid, engine._doors = grid, doors
        now = 1000.0
        done = None
        result_sent = False
        for _ in range(300):
            now += sim.SIM_TICK_S
            state = engine.tick(state, now=now)
            tasks = state.get('tasks') or {}
            task = next((t for t in tasks.values() if t.get('research')), None)
            if task and task.get('_contentInFlight') and not result_sent:
                sim._store_content_result(task['id'], {'ok': True, 'note': ''})
                result_sent = True
            if task and task.get('status') == 'failed':
                done = task
                break
        sim._content_executor = None
        self.assertTrue(result_sent, 'the task must be dispatched for content work')
        self.assertIsNotNone(done, 'empty-note success must be sent back, not done')
        self.assertIn('missing completion evidence', done.get('failNote', ''),
                      'the gap must name the missing evidence')
        self.assertFalse(any(t.get('status') == 'done' for t in (state.get('tasks') or {}).values()),
                         'the empty-evidence task must never land as done')
        reworks = [q for q in (state.get('workQueue') or []) if q.get('title', '').startswith('Fix:')]
        rework_tasks = [t for t in (state.get('tasks') or {}).values()
                        if (t.get('title') or '').startswith('Fix:')]
        self.assertTrue(reworks or rework_tasks,
                        'a rework card is queued for the gap')

    def test_kb_class_survives_queue_round_trip(self):
        # The knowledge-base class flag is part of the queue_work whitelist, so
        # a 'changes how we work' ceremony keeps its KB mandate through the
        # queue (the same contract as distill/skillReview).
        state = self._base_state()
        sim.queue_work(state, [{
            'title': 'Distill recent think tank knowledge', 'room': 'observatory',
            'instructions': 'merge', 'distill': True, 'distillSince': 5,
            'kbClass': 'changes_how_we_work'}])
        self.assertEqual(state['workQueue'][0]['kbClass'], 'changes_how_we_work')
        self.assertEqual(state['workQueue'][0]['distillSince'], 5)

    def test_research_arrival_with_executor_dispatches_and_result_completes(self):
        # With a content executor registered, an arriving task gets the long
        # content timeout + _contentInFlight; a completed result is merged
        # (note + seenUrls) and the task finalizes early.
        calls = {}
        def fake_executor(snapshot, agent_id, task, base_ctx):
            calls['seen'] = (task['id'], task['room'])
            # The executor runs on a bg thread in production; simulate landing
            # a result by calling the same store helper it would.
            sim._store_content_result(task['id'], {
                'note': 'collected 2 pages, wrote updated skill (pending review)',
                'seenUrls': ['https://a.example', 'https://b.example'],
            })
        sim._content_executor = fake_executor
        grid, doors = sim._load_outdoor_geometry()
        # Drive assignment so a worker is actually dispatched to observatory.
        state = self._base_state(work_queue=[{
            'title': 'Scheduled research: x', 'room': 'observatory',
            'instructions': 'crawl', 'goal': 'x', 'priority': sim.WORK_PRIORITY['normal'],
            'notBefore': 0,
            'research': {'topicId': 't1', 'since': 0}}])
        state['researchTopics'] = [{'id': 't1', 'topic': 'x', 'seenUrls': ['https://a.example'], 'cadenceMs': 3600000, 'lastRunAt': 0}]
        engine = sim.SimEngine()
        engine._grid, engine._doors = grid, doors
        now = 1000.0
        # Burn through assignment + walk + arrive (+ the executor result).
        for _ in range(200):
            now += sim.SIM_TICK_S
            state = engine.tick(state, now=now)
            if (state.get('tasks') or {}).get('task-1', {}).get('status') == 'done':
                break
        sim._content_executor = None
        self.assertTrue(calls.get('seen'),
                        'the executor must be dispatched with the arriving task')
        self.assertEqual(calls['seen'][1], 'observatory',
                         'the executor must get the task room')
        topic = state['researchTopics'][0]
        self.assertIn('https://b.example', topic['seenUrls'],
                      'new seenUrl from the content result must be merged back')
        self.assertGreater(state['agents']['ada'].get('approvedCount', 0), 0,
                           'content-completed task still bumps approvedCount')

    def test_content_dispatch_passes_perception_as_base_ctx(self):
        # Item 1: a dispatched content executor gets the agent's situational
        # awareness as base_ctx (who they are, where they are, what they are
        # working on) -- not a bare {} -- so a worker reasons like a teammate,
        # not a stateless API call. The perception is a markdown string, which
        # is exactly the type every real executor's `base_ctx or ''` expects.
        names = {d['id']: d['name'] for d in self._roster()}
        seen = {}

        def fake_executor(snapshot, agent_id, task, base_ctx):
            seen['agent'] = agent_id
            seen['ctx'] = base_ctx
            sim._store_content_result(task['id'], {'ok': True, 'note': 'did the thing'})

        sim._content_executor = fake_executor
        grid, doors = sim._load_outdoor_geometry()
        state = self._base_state(work_queue=[self._item(
            title='Build the login flow',
            acceptanceCriteria='Logging in returns you to the dashboard.')])
        engine = sim.SimEngine()
        engine._grid, engine._doors = grid, doors
        now = 1000.0
        for _ in range(200):
            now += sim.SIM_TICK_S
            state = engine.tick(state, now=now)
            if (state.get('tasks') or {}).get('task-1', {}).get('status') == 'done':
                break
        sim._content_executor = None
        self.assertIn('agent', seen, 'the executor must be dispatched')
        ctx = seen.get('ctx')
        self.assertIsInstance(ctx, str, 'base_ctx must be a markdown string, not {}')
        self.assertIn('You are ' + names[seen['agent']], ctx)
        self.assertIn('pressoffice', ctx)
        self.assertIn('Build the login flow', ctx)


class SimMergeInvariant(unittest.TestCase):
    # The client autosave (agents.js saveState) posts a fixed field list every
    # 5s and does NOT include the server-owned `sim` section. The autosave merge
    # must carry sim forward untouched, or the heartbeat/owner/position snapshot
    # the server maintains would silently vanish on the next autosave -- the
    # exact blob-clobber class that killed server-owned templates earlier.
    def test_client_autosave_does_not_drop_server_owned_sim(self):
        server_state = {
            'agents': {'ada': {'id': 'ada', 'x': 10}},
            'sim': {'tick': 9, 'owner': 'client', 'lastTickEpochS': 99.0, 'agents': {}},
        }
        # Client saveState payload: same top-level keys minus `sim`.
        client_payload = {'agents': {'ada': {'id': 'ada', 'x': 12}}, 'reports': []}
        merged = serve._merge_server_owned(server_state, client_payload)
        self.assertIn('sim', merged, 'autosave must carry the server sim section forward')
        self.assertEqual(merged['sim']['tick'], 9)
        # Non-sim keys are replaced exactly as before.
        self.assertEqual(merged['agents']['ada']['x'], 12)
