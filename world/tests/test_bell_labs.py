"""Bell Research Labs-inspired process changes (2026-10-06).

Covers the seven changes made with Bell Labs in mind, hermetically:

1. Spare-time lane: FREE SPIKES an agent earns per week by finishing real
   deliverable work (self-filed, never groomed, moonshot-protected, capped
   per agent + tank-wide).
2. Moonshot flag threading: queue_work -> _assign_due_item -> assign_task.
3. Cross-team peer review: the two reviewers span teams when the tank can
   supply both.
4. Adversarial critic: reviewer[0] of a gate is briefed to find what's wrong,
   reviewer[1] keeps the standard skeptical frame.
5. Rule-mining protection: a moonshot review's failures never enter the ledger.
6. Retention/archive: decision_tape rows are ARCHIVED to decision_archive on
   prune, never deleted, and the archive summarizes + distills to a wiki page.
7. Long-horizon problem: one standing far-future research topic per village.

Hermetic: no live server, no DB writes outside temp dirs, no network.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402
import sim  # noqa: E402
import content  # noqa: E402

_WEEK_MS = 7 * 24 * 3600 * 1000


def _tank_state(**over):
    """A small tank with two teams (dev + ops), each with idle on-duty workers."""
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': [
            {'id': 'theo', 'name': 'Theo', 'role': 'Admin', 'isAdmin': True},
            {'id': 'dev', 'name': 'Dev', 'role': 'Director', 'director': 'theo'},
            {'id': 'ben', 'name': 'Ben', 'role': 'Engineer', 'director': 'dev'},
            {'id': 'ada', 'name': 'Ada', 'role': 'Researcher', 'director': 'dev'},
            {'id': 'ops', 'name': 'Ops', 'role': 'Director', 'director': 'theo'},
            {'id': 'zoe', 'name': 'Zoe', 'role': 'Engineer', 'director': 'ops'},
        ],
        'teams': [
            {'id': 'dev', 'name': "Dev's Crew", 'directorId': 'dev', 'scrumMasterId': 'ada'},
            {'id': 'ops', 'name': "Ops Squad", 'directorId': 'ops', 'scrumMasterId': 'zoe'},
        ],
        'agents': {
            'theo': {'id': 'theo', 'x': 680, 'y': 340, 'busy': False, 'task': None, 'offDuty': False, 'name': 'Theo'},
            'dev': {'id': 'dev', 'x': 600, 'y': 340, 'busy': False, 'task': None, 'offDuty': False, 'name': 'Dev'},
            'ben': {'id': 'ben', 'x': 640, 'y': 340, 'busy': False, 'task': None, 'offDuty': False, 'name': 'Ben'},
            'ada': {'id': 'ada', 'x': 620, 'y': 340, 'busy': False, 'task': None, 'offDuty': False, 'name': 'Ada'},
            'ops': {'id': 'ops', 'x': 560, 'y': 340, 'busy': False, 'task': None, 'offDuty': False, 'name': 'Ops'},
            'zoe': {'id': 'zoe', 'x': 660, 'y': 340, 'busy': False, 'task': None, 'offDuty': False, 'name': 'Zoe'},
        },
        'villages': [{'id': 'main', 'name': 'Main Village'}],
        'workQueue': [],
        'tasks': {},
        'backlogRequests': [],
        'researchTopics': [],
    }
    state.update(over)
    return state


class SpareTimeLane(unittest.TestCase):
    def test_finished_deliverable_earns_one_moonshot_free_spike(self):
        state = _tank_state()
        done = {'id': 'task-1', 'room': 'pressoffice', 'title': 'Build ledger tool', 'taskType': 'code'}
        n = sim._file_free_spike(state, 'ben', done, now_ms=1_000_000)
        self.assertEqual(n, 1)
        item = state['workQueue'][0]
        self.assertEqual(item['taskType'], 'spike')
        self.assertEqual(item['priority'], sim.WORK_PRIORITY['low'])
        self.assertEqual(item['budgetMs'], sim.FREE_SPIKE_BUDGET_MS)
        self.assertTrue(item['moonshot'])
        self.assertEqual(item['room'], 'pressoffice')
        # The findings-only framing: question the premise, no committed deliverable.
        self.assertIn(sim.FREE_SPIKE_PREMISE_GUIDANCE, item['instructions'])
        # Spare time is opportunistic: deferred, not due for assignment yet.
        self.assertEqual(item['notBefore'], 1_000_000 + sim.FREE_SPIKE_DEFER_MS)
        self.assertFalse(sim.is_work_item_due(item, 1_000_000))

    def test_per_agent_allowance_caps_at_one_per_week(self):
        state = _tank_state()
        done = {'id': 'task-1', 'room': 'pressoffice', 'title': 'Build ledger tool', 'taskType': 'code'}
        self.assertEqual(sim._file_free_spike(state, 'ben', done, now_ms=1_000_000), 1)
        # A second completion the SAME week must not earn another.
        self.assertEqual(sim._file_free_spike(state, 'ben', done, now_ms=1_000_001), 0)
        self.assertEqual(len(state['workQueue']), 1)

    def test_allowance_rolls_next_week(self):
        state = _tank_state()
        done = {'id': 'task-1', 'room': 'pressoffice', 'title': 'Build ledger tool', 'taskType': 'code'}
        sim._file_free_spike(state, 'ben', done, now_ms=1_000_000)
        # Same agent, one week later -> a fresh allowance.
        self.assertEqual(sim._file_free_spike(state, 'ben', done, now_ms=1_000_000 + _WEEK_MS), 1)
        self.assertEqual(len(state['workQueue']), 2)

    def test_tank_wide_cap_shared_across_agents(self):
        state = _tank_state()
        # Fill the tank-wide cap across different agents in different rooms
        # (one free exploration per room per week, so distinct rooms).
        for i, (aid, room) in enumerate([('ben', 'pressoffice'), ('ada', 'observatory'),
                                         ('zoe', 'postoffice')]):
            done = {'id': f'task-{i}', 'room': room, 'title': 'Build', 'taskType': 'code'}
            self.assertEqual(sim._file_free_spike(state, aid, done, now_ms=1_000_000), 1)
        # A fourth agent in a fresh room is capped out at the tank level.
        done = {'id': 'task-4', 'room': 'bank', 'title': 'Build', 'taskType': 'code'}
        self.assertEqual(sim._file_free_spike(state, 'dev', done, now_ms=1_000_000), 0)
        self.assertEqual(len(state['workQueue']), sim.FREE_SPIKE_GLOBAL_CAP_PER_WEEK)

    def test_dedup_same_room_pending_free_spike(self):
        state = _tank_state()
        done = {'id': 'task-1', 'room': 'pressoffice', 'title': 'Build ledger tool', 'taskType': 'code'}
        sim._file_free_spike(state, 'ben', done, now_ms=1_000_000)
        # Another agent, same room, same week: identical pending moonshot spike exists.
        self.assertEqual(sim._file_free_spike(state, 'ada', done, now_ms=1_000_001), 0)
        self.assertEqual(len(state['workQueue']), 1)

    def test_no_free_spike_for_busy_off_duty_or_non_valued_rooms(self):
        state = _tank_state()
        state['agents']['ben']['busy'] = True
        self.assertEqual(sim._file_free_spike(state, 'ben',
                                              {'room': 'pressoffice', 'taskType': 'code'}, 1_000_000), 0)
        state['agents']['ben']['busy'] = False
        state['agents']['ben']['offDuty'] = True
        self.assertEqual(sim._file_free_spike(state, 'ben',
                                              {'room': 'pressoffice', 'taskType': 'code'}, 1_000_000), 0)
        state['agents']['ben']['offDuty'] = False
        # 'hangout' is not a delegatable room -> no spare time from it.
        self.assertEqual(sim._file_free_spike(state, 'ben',
                                              {'room': 'hangout', 'taskType': 'code'}, 1_000_000), 0)
        self.assertEqual(state['workQueue'], [])

    def test_maybe_file_followup_wires_the_spare_time_lane(self):
        state = _tank_state()
        # Thin pressoffice backlog -> a follow-up work-request AND a free spike.
        done = {'id': 'task-1', 'room': 'pressoffice', 'title': 'Build ledger tool', 'taskType': 'code'}
        sim._maybe_file_followup(state, 'ben', done, 1_000_000)
        self.assertEqual(len(state['backlogRequests']), 1)
        self.assertEqual(len(state['workQueue']), 1)
        self.assertTrue(state['workQueue'][0]['moonshot'])
        self.assertEqual(state['workQueue'][0]['taskType'], 'spike')

    def test_spike_and_bug_completions_earn_no_spare_time(self):
        state = _tank_state()
        sim._maybe_file_followup(state, 'ben', {'room': 'pressoffice', 'taskType': 'spike'}, 1_000_000)
        sim._maybe_file_followup(state, 'ben', {'room': 'pressoffice', 'taskType': 'bug'}, 1_000_000)
        self.assertEqual(state['workQueue'], [])


class MoonshotThreading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='bell-thread-')
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

    def test_queue_work_whitelists_moonshot(self):
        state = _tank_state()
        sim.queue_work(state, [{'title': 'x', 'room': 'pressoffice', 'moonshot': True}])
        self.assertTrue(state['workQueue'][0]['moonshot'])

    def test_moonshot_survives_assignment_onto_the_task(self):
        state = _tank_state()
        pick = {'title': 'Free exploration: question an assumption about observatory',
                'room': 'observatory', 'taskType': 'spike', 'budgetMs': 10_000,
                'moonshot': True, 'notBefore': None, 'pair': False}
        grid, doors = sim._load_outdoor_geometry()
        task = sim._assign_due_item(state, pick, True, grid, doors, 1_000_000,
                                    task_id_holder=[0])
        self.assertIsNotNone(task)
        self.assertTrue(task['moonshot'])
        self.assertEqual(task['taskType'], 'spike')


class CrossTeamReviewers(unittest.TestCase):
    def _state_two_teams(self):
        return _tank_state()

    def test_pair_spans_teams_when_pool_can_supply_both(self):
        state = self._state_two_teams()
        # Author ben is on Dev's team; Zoe (Ops) is the only genuinely off-team
        # worker. The pair must be one on-team + one off-team.
        reviewers = sim._pick_reviewer_ids(state, 'ben', task_room='pressoffice')
        self.assertEqual(len(reviewers), 2)
        dev_team = set(sim._sim_direct_reports(state, 'dev'))
        ops_team = set(sim._sim_direct_reports(state, 'ops'))
        self.assertIn(reviewers[0], dev_team)
        self.assertIn(reviewers[1], ops_team)

    def test_single_team_pool_falls_back_to_quality_order(self):
        state = self._state_two_teams()
        # Remove the ops team entirely -> no genuinely off-team worker exists,
        # so the quality ordering wins and both picks can come from Dev's crew
        # (the author's own director may stand in as the second reviewer).
        state['agentRoster'] = [d for d in state['agentRoster'] if d.get('id') != 'zoe']
        state['agents'].pop('zoe', None)
        state['teams'] = [t for t in state['teams'] if t.get('id') != 'ops']
        reviewers = sim._pick_reviewer_ids(state, 'ben', task_room='pressoffice')
        self.assertEqual(len(reviewers), 2)
        dev_team = set(sim._sim_direct_reports(state, 'dev'))
        self.assertIn(reviewers[0], dev_team)  # the on-team pick is a real teammate
        self.assertNotIn('ben', reviewers)
        self.assertEqual(len(set(reviewers)), 2)

    def test_preferred_prior_pair_is_preserved(self):
        state = self._state_two_teams()
        # A rejection must be re-verified by the SAME reviewers who flagged it.
        reviewers = sim._pick_reviewer_ids(state, 'ben', task_room='pressoffice',
                                           preferred=['ada', 'zoe'])
        self.assertEqual(reviewers, ['ada', 'zoe'])


class AdversarialCritic(unittest.TestCase):
    def test_first_reviewer_is_the_critic_second_is_standard(self):
        state = _tank_state()
        task = {'id': 'task-1', 'room': 'pressoffice', 'title': 'Ship the report',
                'assignedTo': 'ben', 'projectLabel': 'report'}
        gate = sim._enter_peer_review(state, task, now_ms=1_000_000)
        self.assertIsNotNone(gate)
        items = state['workQueue']
        critic_items = [i for i in items if i.get('assignedTo') == gate['reviewerIds'][0]]
        standard_items = [i for i in items if i.get('assignedTo') == gate['reviewerIds'][1]]
        self.assertTrue(critic_items and standard_items)
        self.assertIn('CRITIC', critic_items[0]['instructions'])
        self.assertNotIn('CRITIC', standard_items[0]['instructions'])
        # The critic is told to assume a hidden flaw until proven otherwise.
        self.assertIn('hidden flaw', critic_items[0]['instructions'])

    def test_critic_mailbox_notice_is_adversarial(self):
        state = _tank_state()
        task = {'id': 'task-1', 'room': 'pressoffice', 'title': 'Ship the report',
                'assignedTo': 'ben', 'projectLabel': 'report'}
        gate = sim._enter_peer_review(state, task, now_ms=1_000_000)
        critic_id = gate['reviewerIds'][0]
        mailbox = (state['agents'].get(critic_id) or {}).get('mailbox') or []
        self.assertTrue(any(m.get('kind') == 'peer_review_request' and 'CRITIC' in m.get('text', '')
                            for m in mailbox))


class MoonshotRuleProtection(unittest.TestCase):
    def _module_tmp(self):
        self._tmp = tempfile.mkdtemp(prefix='bell-moonshot-')

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='bell-moonshot-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            FAILURES_PATH=os.path.join(self._tmp, 'failures.json'),
            RULE_PROPOSALS_PATH=os.path.join(self._tmp, 'rule_proposals.json'),
            THINK_TANK_DIR=self._tmp,
        )
        self._cm.start()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_moonshot_review_failures_never_reach_the_ledger(self):
        grades = [{'id': 'r1', 'verdict': content.GRADE_FAILS, 'question': 'price is wrong',
                   'section': 'numbers'}]
        with unittest.mock.patch.object(serve, '_classify_failure', return_value='factual_error'):
            # A moonshot review records nothing.
            self.assertEqual(content._record_classified_failures(
                'ada', 'review', grades, [], 'text', moonshot=True), 0)
            # A normal review still records.
            self.assertEqual(content._record_classified_failures(
                'ada', 'review', grades, [], 'text'), 1)
        failures = serve._load_failures()
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['type'], 'factual_error')


class DecisionArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='bell-archive-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _insert_old_tape(self, ts):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO decision_tape (ts, kind, model, prompt, criteria, choice, confidence, cost, raw, ok) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (ts, 'browse_allow', 'typesafe/jev-1.13', 'prompt', 'crit', 'block', 0.9, 0.0004, 'raw', 1))
            conn.execute(
                'INSERT INTO decision_tape (ts, kind, model, prompt, criteria, choice, confidence, cost, raw, ok) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (ts, 'browse_allow', 'typesafe/jev-1.13', 'prompt', 'crit', 'allow', 0.8, 0.0002, 'raw', 0))

    def test_prune_archives_decision_tape_instead_of_deleting(self):
        old = time.time() - 30 * 86400  # 30 days old
        self._insert_old_tape(old)
        with unittest.mock.patch.object(serve, '_load_env', return_value={'LOG_RETENTION_DAYS': '7'}):
            serve._prune_logs()
        with serve._db() as conn:
            tape_rows = conn.execute('SELECT COUNT(*) FROM decision_tape').fetchone()[0]
            archive_rows = conn.execute('SELECT COUNT(*) FROM decision_archive').fetchone()[0]
        self.assertEqual(tape_rows, 0)
        self.assertEqual(archive_rows, 2)

    def test_recent_tape_is_not_pruned(self):
        with unittest.mock.patch.object(serve, '_load_env', return_value={'LOG_RETENTION_DAYS': '7'}):
            serve._prune_logs()
        with serve._db() as conn:
            archive_rows = conn.execute('SELECT COUNT(*) FROM decision_archive').fetchone()[0]
        self.assertEqual(archive_rows, 0)

    def test_archive_summary_aggregates(self):
        old = time.time() - 30 * 86400
        self._insert_old_tape(old)
        with unittest.mock.patch.object(serve, '_load_env', return_value={'LOG_RETENTION_DAYS': '7'}):
            serve._prune_logs()
        summary = serve._decision_archive_summary(old - 1)
        self.assertEqual(summary['total'], 2)
        self.assertEqual(summary['failed'], 1)
        self.assertAlmostEqual(summary['cost'], 0.0006, places=6)
        self.assertEqual(len(summary['kinds']), 1)
        self.assertEqual(summary['kinds'][0]['kind'], 'browse_allow')

    def test_archive_summary_fails_closed(self):
        with unittest.mock.patch.object(serve, '_db', side_effect=RuntimeError('db boom')):
            self.assertIsNone(serve._decision_archive_summary(0))


class LongHorizonTopic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='bell-horizon-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_check_schedules_seeds_one_long_horizon_topic_per_village(self):
        state = _tank_state()
        sim._check_schedules(state, 1_000.0, 1_000_000)
        topics = state['researchTopics']
        self.assertEqual(len(topics), 1)
        t = topics[0]
        self.assertTrue(t['longHorizon'])
        self.assertEqual(t['villageId'], 'main')
        # lastRunAt seeded to now so the first crawl is a cadence away, not next pass.
        self.assertEqual(t['lastRunAt'], 1_000_000)
        # A far-future cadence means it is NOT due on this pass.
        self.assertTrue(1_000_000 - t['lastRunAt'] < t['cadenceMs'])
        self.assertEqual(state['workQueue'], [])

    def test_seeding_is_idempotent(self):
        state = _tank_state()
        sim._check_schedules(state, 1_000.0, 1_000_000)
        sim._check_schedules(state, 1_000.0, 1_000_100)
        self.assertEqual(len(state['researchTopics']), 1)

    def test_deleted_topic_is_not_reseeded(self):
        state = _tank_state()
        sim._check_schedules(state, 1_000.0, 1_000_000)
        state['researchTopics'] = []
        sim._check_schedules(state, 1_000.0, 1_000_100)
        self.assertEqual(state['researchTopics'], [])

    def test_env_override_is_honored(self):
        state = _tank_state()
        env = {'LONG_HORIZON_TOPIC': 'Custom horizon', 'LONG_HORIZON_TOPIC_URL': 'https://example.com/x',
               'LONG_HORIZON_CADENCE_DAYS': '7'}
        with unittest.mock.patch.object(serve, '_load_env', return_value=env):
            sim._check_schedules(state, 1_000.0, 1_000_000)
        t = state['researchTopics'][0]
        self.assertEqual(t['topic'], 'Custom horizon')
        self.assertEqual(t['startUrl'], 'https://example.com/x')
        self.assertEqual(t['cadenceMs'], 7 * 24 * 3600 * 1000)

    def test_bad_env_url_falls_back_to_safe_default(self):
        state = _tank_state()
        env = {'LONG_HORIZON_TOPIC_URL': 'not a url', 'LONG_HORIZON_CADENCE_DAYS': '7'}
        with unittest.mock.patch.object(serve, '_load_env', return_value=env):
            sim._check_schedules(state, 1_000.0, 1_000_000)
        self.assertEqual(state['researchTopics'][0]['startUrl'], sim.LONG_HORIZON_TOPIC_URL_DEFAULT)


class ArchiveDistill(unittest.TestCase):
    def test_distills_archive_into_wiki_when_there_is_content(self):
        state = _tank_state()
        summary = {'kinds': [{'kind': 'browse_allow', 'count': 2, 'ok': 1, 'cost': 0.0006}],
                   'total': 2, 'failed': 1, 'cost': 0.0006}
        with unittest.mock.patch.object(serve, '_decision_archive_summary', return_value=summary), \
                unittest.mock.patch.object(serve, '_wiki_page_body', return_value=''), \
                unittest.mock.patch.object(serve, '_write_wiki_server') as w:
            sim._archive_distill_step(state, 1_000_000 + sim.RULE_MINE_CADENCE_MS)
        self.assertTrue(w.called)
        page_id, title, category, body = w.call_args[0]
        self.assertEqual(page_id, 'decision-archive')
        self.assertEqual(category, 'think_tank')
        self.assertIn('browse_allow', body)
        self.assertIn('total: 2', body)

    def test_noop_without_archive_content(self):
        state = _tank_state()
        with unittest.mock.patch.object(serve, '_decision_archive_summary', return_value={'total': 0}), \
                unittest.mock.patch.object(serve, '_write_wiki_server') as w:
            sim._archive_distill_step(state, 1_000_000 + sim.RULE_MINE_CADENCE_MS)
        self.assertFalse(w.called)

    def test_cadence_gated(self):
        state = _tank_state()
        with unittest.mock.patch.object(serve, '_decision_archive_summary',
                                        return_value={'total': 1}) as m, \
                unittest.mock.patch.object(serve, '_write_wiki_server'):
            sim._archive_distill_step(state, 1_000_000)  # too soon
        self.assertFalse(m.called)


class PremiseQuestioning(unittest.TestCase):
    def test_guidance_is_non_empty_and_folded_into_both_executors(self):
        self.assertTrue(content.PREMISE_QUESTIONING_GUIDANCE)
        src = open(content.__file__, encoding='utf-8').read()
        # Both executors must fold the guidance in, or the first-principles
        # pass silently disappears.
        self.assertIn('PREMISE_QUESTIONING_GUIDANCE', src)
        self.assertGreaterEqual(src.count('PREMISE_QUESTIONING_GUIDANCE'), 3)


class FalsificationContract(unittest.TestCase):
    """The desk's falsification filter, applied to the think tank: a research
    question with no named way to be proven wrong is not an answerable claim,
    and a free spike must name the objection before it counts as a finding."""

    def test_research_contract_requires_a_prove_wrong_line(self):
        src = open(content.__file__, encoding='utf-8').read()
        # The contract must ask for the single most likely objection and the
        # concrete observation that would overturn the expected answer.
        self.assertIn('PROVE WRONG', src)
        self.assertIn('would overturn it', src)

    def test_free_spike_guidance_requires_a_named_objection(self):
        guidance = sim.FREE_SPIKE_PREMISE_GUIDANCE
        self.assertIn('prove it wrong', guidance)
        self.assertIn('strongest objection', guidance)


if __name__ == '__main__':
    unittest.main()