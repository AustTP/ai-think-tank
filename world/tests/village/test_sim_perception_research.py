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


class SimAgentPerception(unittest.TestCase):
    """Item 1: situational awareness for content executors. _build_agent_
    perception is a pure read of a frozen snapshot -- who am I, where am I,
    who's around (acquaintance-aware), what am I working on, what feedback came
    back at me -- and the acquaintance helpers that gate how a stranger is
    named."""

    def _state(self):
        return {
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada', 'role': 'Research', 'isAdmin': False},
                {'id': 'ben', 'name': 'Ben', 'role': 'Banking', 'isAdmin': False},
                {'id': 'faye', 'name': 'Faye', 'role': 'Admin', 'isAdmin': True},
            ],
            'agents': {
                'ada': {'id': 'ada', 'inRoom': 'pressoffice', 'offDuty': False},
                'ben': {'id': 'ben', 'inRoom': 'pressoffice', 'offDuty': False},
                'faye': {'id': 'faye', 'inRoom': 'observatory', 'offDuty': False},
            },
        }

    def test_perception_carries_identity_room_present_task(self):
        state = self._state()
        task = {'title': 'Build the login flow',
                'instructions': 'wire the form',
                'acceptanceCriteria': 'Logging in returns you to the dashboard.',
                'room': 'pressoffice'}
        out = sim._build_agent_perception(state, 'ada', task)
        self.assertIn('You are Ada.', out)
        self.assertIn('Your role on the team: Research.', out)
        self.assertIn('You are currently in the pressoffice.', out)
        # Ben shares the room but ada has not met him -> role only, never a name.
        self.assertIn('the Banking', out)
        self.assertIn('Build the login flow -- wire the form.', out)
        self.assertIn('Logging in returns you to the dashboard.', out)
        self.assertNotIn('Ben', out,
                         "a stranger's name is not a name the viewer can use")

    def test_acquaintance_gates_naming(self):
        state = self._state()
        self.assertFalse(sim._are_acquainted(state, 'ada', 'ben'))
        self.assertEqual(sim._describe_agent(state, 'ben', 'ada'), 'the Banking')
        sim._mark_acquaintance(state, 'ada', 'ben')
        self.assertTrue(sim._are_acquainted(state, 'ada', 'ben'))
        self.assertTrue(sim._are_acquainted(state, 'ben', 'ada'))
        self.assertEqual(sim._describe_agent(state, 'ben', 'ada'),
                         'Ben (the Banking)')
        self.assertIn('Ben (the Banking)',
                      sim._build_agent_perception(state, 'ada'))

    def test_acquaintance_mark_is_json_safe_and_idempotent(self):
        state = self._state()
        sim._mark_acquaintance(state, 'ada', 'ben')
        sim._mark_acquaintance(state, 'ada', 'ben')  # idempotent
        sim._mark_acquaintance(state, 'ada', 'ada')  # self-intro is a no-op
        round_tripped = json.loads(json.dumps(state))
        self.assertEqual(round_tripped['_acquaintances']['ada'], ['ben'])
        self.assertEqual(round_tripped['_acquaintances']['ben'], ['ada'])

    def test_perception_includes_pending_feedback(self):
        state = self._state()
        state['_feedback'] = {'ada': [
            {'text': 'Your last fix shipped but the modal is still 2px off.'},
            'Keep the heredoc under 200 lines.',
        ]}
        out = sim._build_agent_perception(state, 'ada', {'title': 'Fix the modal'})
        self.assertIn('Feedback on your recent work:', out)
        self.assertIn('modal is still 2px off', out)
        self.assertIn('Keep the heredoc under 200 lines.', out)

    def test_perception_unknown_agent_falls_back_gracefully(self):
        out = sim._build_agent_perception(self._state(), 'ghost',
                                          {'title': 'T'})
        self.assertIn('You are ghost.', out)

    def test_append_feedback_writes_and_caps(self):
        # Item 2: _append_feedback is a JSON-safe buffer per agent, capped to a
        # recent-memory window (not a ledger). Empty text / unknown agent no-op.
        state = self._state()
        sim._append_feedback(state, 'ada', 'fix the modal offset')
        sim._append_feedback(state, 'ada', 'keep heredocs under 200 lines',
                             source='peer_review')
        sim._append_feedback(state, 'ada', '   ')  # blank -> no-op
        sim._append_feedback(state, None, 'orphan')  # unknown -> no-op
        bucket = state['_feedback']['ada']
        self.assertEqual(len(bucket), 2)
        self.assertEqual(bucket[0], {'text': 'fix the modal offset'})
        self.assertEqual(bucket[1]['text'], 'keep heredocs under 200 lines')
        self.assertEqual(bucket[1]['source'], 'peer_review')
        self.assertNotIn('ghost', state['_feedback'])
        round_tripped = json.loads(json.dumps(state))
        self.assertEqual(round_tripped['_feedback']['ada'], bucket,
                         'feedback must survive the kv_state round trip')
        for i in range(sim._FEEDBACK_MAX + 5):
            sim._append_feedback(state, 'ada', f'note {i}')
        self.assertLessEqual(len(state['_feedback']['ada']), sim._FEEDBACK_MAX,
                             'buffer is capped, not a ledger')
        self.assertIn('note {}'.format(sim._FEEDBACK_MAX + 4),
                      state['_feedback']['ada'][-1]['text'],
                      'cap trims the OLDEST entries, newest survives')

    def test_append_feedback_dedups_same_source_same_task(self):
        # Agents reported the same rejection flooding their feedback buffer
        # 3-5 times with no way to tell which copy was current. When the
        # newest entry has the same source and the same first-40-char prefix
        # (the task-title prefix both pipeline and peer-review notices carry),
        # the old entry is REPLACED, not stacked.
        state = self._state()
        sim._append_feedback(state, 'ada',
                             "Your deliverable 'Build the login flow' failed the quality pipeline and was sent back. It will be reviewed again after you fix it.",
                             source='pipeline')
        sim._append_feedback(state, 'ada',
                             "Your deliverable 'Build the login flow' failed the quality pipeline and was sent back. It will be reviewed again after you fix it.",
                             source='pipeline')
        bucket = state['_feedback']['ada']
        self.assertEqual(len(bucket), 1, 'same source + same task prefix must replace, not stack')
        # A DIFFERENT task still stacks (new prefix).
        sim._append_feedback(state, 'ada',
                             "Your deliverable 'Fix the header layout' failed the quality pipeline and was sent back.",
                             source='pipeline')
        self.assertEqual(len(state['_feedback']['ada']), 2)
        # A DIFFERENT source still stacks even for the same task.
        sim._append_feedback(state, 'ada',
                             "Your deliverable 'Build the login flow' failed the quality pipeline and was sent back.",
                             source='peer_review')
        self.assertEqual(len(state['_feedback']['ada']), 3)

    def test_upsert_rejection_mailbox_dedups_unread_same_task(self):
        # Agents reported the same rejection appearing 3-5 times in their
        # mailbox. _upsert_rejection_mailbox updates an UNREAD entry with the
        # same kind + about (task id) in place instead of appending.
        state = self._state()
        agent = state['agents']['ada']
        sim._upsert_rejection_mailbox(agent, {
            'kind': 'peer_review_rejected', 'about': 'task-1',
            'title': 'Build the login flow',
            'text': 'Your work was sent back for a fix.', 'ts': 1000})
        sim._upsert_rejection_mailbox(agent, {
            'kind': 'peer_review_rejected', 'about': 'task-1',
            'title': 'Build the login flow',
            'text': 'Still needs a fix (second cycle).', 'ts': 2000})
        mailbox = agent['mailbox']
        self.assertEqual(len(mailbox), 1, 'unread same-task rejection must be updated, not stacked')
        self.assertIn('second cycle', mailbox[0]['text'])
        # A different task appends a new entry.
        sim._upsert_rejection_mailbox(agent, {
            'kind': 'peer_review_rejected', 'about': 'task-2',
            'title': 'Other task', 'text': 'Sent back.', 'ts': 3000})
        self.assertEqual(len(agent['mailbox']), 2)
        # A READ entry is never updated -- a new rejection is new information.
        agent['mailbox'][0]['read'] = True
        sim._upsert_rejection_mailbox(agent, {
            'kind': 'peer_review_rejected', 'about': 'task-1',
            'title': 'Build the login flow',
            'text': 'Third cycle.', 'ts': 4000})
        self.assertEqual(len(agent['mailbox']), 3)

    def test_coach_low_grade_includes_acceptance_criteria(self):
        # Agents reported 0.0-grade coaching that said "meet the spec" but
        # never stated what the spec was. When the task carries acceptance
        # criteria or a user story, the coaching note quotes them.
        state = self._state()
        task = {'id': 'task-1', 'title': 'Distill knowledge',
                'room': 'pressoffice',
                'acceptanceCriteria': 'Cover all three knowledge sources. Cite each source inline.',
                'userStory': 'As a researcher I want distilled findings so I can reuse them.'}
        sim._coach_low_grade(state, 'ada', 'pressoffice', 2.0, 1000,
                             'Distill knowledge', task=task)
        plans = state['growthPlans']['ada']
        low = [p for p in plans if p.get('kind') == 'low_grade']
        self.assertEqual(len(low), 1)
        self.assertIn('Cover all three knowledge sources', low[0]['note'])
        self.assertIn('As a researcher', low[0]['note'])

    def test_coach_low_grade_without_task_has_no_spec_block(self):
        state = self._state()
        sim._coach_low_grade(state, 'ada', 'pressoffice', 2.0, 1000, 'Vague task')
        plans = state['growthPlans']['ada']
        low = [p for p in plans if p.get('kind') == 'low_grade']
        self.assertEqual(len(low), 1)
        self.assertNotIn('Acceptance criteria', low[0]['note'])

    def test_send_back_after_failure_stamps_cycle_count(self):
        # Agents reported "sent back" reading the same on cycle 1 and cycle 5.
        # The mailbox notice now carries "rejection N of MAX" so the agent can
        # tell them apart without counting copies.
        state = self._state()
        task = {'id': 'task-1', 'title': 'Build thing', 'assignedTo': 'ada',
                'room': 'pressoffice', 'status': 'working'}
        sim._send_back_after_failure(state, task, fail_note='flake8 failed')
        mailbox = state['agents']['ada']['mailbox']
        self.assertEqual(len(mailbox), 1)
        self.assertIn('rejection 1 of', mailbox[0]['text'])
        # Second failure bumps the gate cycle counter.
        task2 = {'id': 'task-1', 'title': 'Build thing', 'assignedTo': 'ada',
                 'room': 'pressoffice', 'status': 'working',
                 '_peerGate': task['_peerGate']}
        sim._send_back_after_failure(state, task2, fail_note='flake8 failed again')
        # The upsert updates the unread entry in place (same kind + about).
        mailbox = state['agents']['ada']['mailbox']
        self.assertEqual(len(mailbox), 1)
        self.assertIn('rejection 2 of', mailbox[0]['text'])

    def test_send_back_infra_failure_skips_coaching(self):
        # An infrastructure failure (model-call fault) is not the agent's
        # quality -- no coaching note is written, and the mailbox wording
        # says infrastructure, not quality pipeline.
        state = self._state()
        task = {'id': 'task-1', 'title': 'Build thing', 'assignedTo': 'ada',
                'room': 'pressoffice', 'status': 'working',
                '_infraFailure': True,
                'note': 'the model call failed or returned nothing (infrastructure error, retried once)'}
        sim._send_back_after_failure(state, task)
        plans = state.get('growthPlans', {}).get('ada') or []
        pipeline_plans = [p for p in plans if p.get('kind') == 'pipeline_feedback']
        self.assertEqual(len(pipeline_plans), 0,
                         'infra failures must not write a pipeline coaching note')
        mailbox = state['agents']['ada']['mailbox']
        self.assertEqual(len(mailbox), 1)
        self.assertIn('infrastructure', mailbox[0]['text'].lower())
        self.assertNotIn('quality pipeline', mailbox[0]['text'])
        # No pipeline feedback entry in the feedback buffer either.
        fb = state.get('_feedback', {}).get('ada') or []
        pipeline_fb = [f for f in fb if f.get('source') == 'pipeline']
        self.assertEqual(len(pipeline_fb), 0)

    def test_send_back_infra_failure_retries_same_work_no_cycle_bump(self):
        # An infra-tagged failure says nothing about the code: no 'Fix:' card,
        # no review-cycle burn -- the SAME work is re-queued unchanged and the
        # retries count separately on the gate (an outage must never auto-close
        # a healthy story at the cycle cap).
        state = self._state()
        task = {'id': 'task-1', 'title': 'Build thing', 'assignedTo': 'ada',
                'room': 'pressoffice', 'status': 'working',
                '_infraFailure': True,
                'note': 'the model call failed or returned nothing (infrastructure error)'}
        sim._send_back_after_failure(state, task)
        queue = state['workQueue']
        self.assertEqual(len(queue), 1)
        item = queue[0]
        self.assertEqual(item['title'], 'Build thing')  # no 'Fix: ' prefix
        self.assertEqual(item['reviewOf'], 'task-1')    # pin + green re-arm path
        self.assertEqual(item['assignedTo'], 'ada')
        self.assertIn('infrastructure', item['instructions'])
        gate = task['_peerGate']
        self.assertEqual(gate.get('cycleCount', 0), 0)  # no quality cycle burned
        self.assertEqual(gate.get('infraRetries'), 1)
        self.assertEqual(task['status'], 'failed')
        # Infra retries are not quality rejections: no "rejection N of MAX" stamp.
        self.assertNotIn('rejection', state['agents']['ada']['mailbox'][0]['text'])

    def test_send_back_infra_failure_caps_then_freezes_gate(self):
        # After INFRA_SEND_BACKS same-work retries the story freezes for manual
        # attention instead of looping forever -- and never auto-closes, since
        # the pipeline never passed clean ("ship what exists" would be a
        # red-pipeline escape).
        state = self._state()
        task = {'id': 'task-1', 'title': 'Build thing', 'assignedTo': 'ada',
                'room': 'pressoffice', 'status': 'working',
                '_infraFailure': True,
                'note': 'the sandbox pipeline timed out (infrastructure)'}
        for _ in range(sim.INFRA_SEND_BACKS + 1):
            sim._send_back_after_failure(state, task)
        self.assertEqual(len(state['workQueue']), sim.INFRA_SEND_BACKS)
        gate = task['_peerGate']
        self.assertEqual(gate.get('infraRetries'), sim.INFRA_SEND_BACKS)
        self.assertTrue(gate.get('escalated'))
        self.assertEqual(gate.get('cycleCount', 0), 0)
        self.assertNotIn('closed', gate)  # not _close_gated_story'd
        self.assertEqual(task['status'], 'failed')
        self.assertIn('frozen', task['failNote'])
        kinds = [m.get('kind') for m in state['agents']['ada']['mailbox']]
        self.assertIn('infrastructure_failure_frozen', kinds)

    def test_send_back_explicit_is_infra_beats_missing_task_tag(self):
        # A FIX subtask that fails on infra carries the tag, but the PARENT is
        # the card being resent -- the parent's (absent) tag alone must not
        # downgrade the resend to quality wording.
        state = self._state()
        parent = {'id': 'task-1', 'title': 'Build thing', 'assignedTo': 'ada',
                  'room': 'pressoffice', 'status': 'failed'}
        sim._send_back_after_failure(state, parent,
                                     fail_note='the sandbox pipeline timed out',
                                     is_infra=True)
        mailbox = state['agents']['ada']['mailbox']
        self.assertIn('infrastructure', mailbox[0]['text'].lower())
        self.assertNotIn('quality pipeline', mailbox[0]['text'])
        self.assertEqual(parent['_peerGate']['infraRetries'], 1)
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(state['workQueue'][0]['title'], 'Build thing')

    def test_team_overview_tool_is_offered(self):
        # Theo's round-3 request: a combined status+digest+allowlist view.
        tool_names = {t['function']['name'] for t in serve.AGENT_ASK_TOOLS}
        self.assertIn('team_overview', tool_names)

    def test_treasury_status_tool_is_offered(self):
        # Ben's round-4 request: a read-only budget/ledger view.
        tool_names = {t['function']['name'] for t in serve.AGENT_ASK_TOOLS}
        self.assertIn('treasury_status', tool_names)

    def test_team_digest_falls_back_to_live_summary_when_no_weekly_review(self):
        # The weekly loop sleeps 7 days before its first run, so the table is
        # empty on a fresh deploy. The digest must return real action-log data,
        # not "none yet".
        text = serve._team_digest_text()
        self.assertNotIn('No weekly review has been generated yet', text)
        self.assertIn('Live summary from the action log', text)
        self.assertIn('Jev decisions logged', text)


    def test_apply_content_result_consumes_executor_feedback(self):
        # Item 2: an executor-reported block/failure rides the content result
        # channel into the tick's single read-modify-write, where it lands in the
        # author's feedback buffer (rendered on the next dispatch's perception).
        state = self._state()
        task = {'id': 'task-1', 'title': 'Build the login flow',
                'assignedTo': 'ada', 'room': 'pressoffice'}
        sim._apply_content_result(state, task, {
            'note': 'blocked: security gate',
            'ok': False,
            'feedback': 'Your command was blocked before running: security gate.',
        }, now_ms=1000)
        bucket = state['_feedback']['ada']
        self.assertEqual(len(bucket), 1)
        self.assertIn('security gate', bucket[0]['text'])
        self.assertEqual(bucket[0]['source'], 'executor')

    def test_apply_content_result_consumes_feedback_dict_source(self):
        # Item 2: the feedback entry may be a dict carrying its own source.
        state = self._state()
        task = {'id': 'task-1', 'title': 'T', 'assignedTo': 'ben', 'room': 'media'}
        sim._apply_content_result(state, task, {
            'ok': False,
            'feedback': {'text': 'sandbox missing', 'source': 'executor'},
        }, now_ms=1000)
        self.assertEqual(state['_feedback']['ben'][0]['source'], 'executor')

    def test_sim_notify_author_records_feedback_for_author(self):
        # Item 2: a peer rejection writes the author's feedback buffer, so the
        # next dispatch's perception block shows what came back at them -- the
        # W6 coaching intent, visible in situational awareness.
        state = self._state()
        state['agents']['ada']['profile'] = {'notes': []}
        parent = {'id': 'task-1', 'title': 'Build the login flow',
                  'assignedTo': 'ada', 'room': 'pressoffice', '_peerGate': {}}
        sim._sim_notify_author(state, parent, 'ben',
                               rationale='the modal is 2px off')
        bucket = state['_feedback']['ada']
        self.assertEqual(len(bucket), 1)
        self.assertIn('Build the login flow', bucket[0]['text'])
        self.assertIn('2px off', bucket[0]['text'])
        self.assertEqual(bucket[0]['source'], 'peer_review')
        self.assertIn('peer_review_rejected',
                      [m.get('kind') for m in state['agents']['ada']['mailbox']],
                      'the mailbox note is still filed alongside the buffer')

    def test_sim_notify_author_failed_records_feedback(self):
        # Item 2: a red-pipeline send-back lands in the author's feedback buffer.
        state = self._state()
        state['agents']['ada']['profile'] = {'notes': []}
        task = {'id': 'task-1', 'title': 'Build the login flow',
                'assignedTo': 'ada', 'room': 'pressoffice'}
        sim._sim_notify_author_failed(state, task)
        bucket = state['_feedback']['ada']
        self.assertEqual(len(bucket), 1)
        self.assertIn('Build the login flow', bucket[0]['text'])
        self.assertEqual(bucket[0]['source'], 'pipeline')


class SyncResearchWatchlist(unittest.TestCase):
    """The watchlist is a live mirror: an operator edit to world/watchlist.json
    must reach state['researchTopics'] without a restart (the boot seed alone
    could not), and a topic removed from the file must stop firing. Mirrors
    the _check_schedules gate: only the real server boot enables
    ALLOW_WATCHLIST_WRITE, so tests that enable it exercise the sync on a
    controlled fake watchlist."""

    def _call(self, state, topics, ok=True):
        with unittest.mock.patch.object(config, 'ALLOW_WATCHLIST_WRITE', True), \
             unittest.mock.patch.object(config, 'get_research_watchlist',
                                        return_value=(topics, ok)):
            return sim._sync_research_watchlist(state)

    def test_adds_new_watchlist_topic_live(self):
        state = {'researchTopics': []}
        added, removed = self._call(state, [
            {'topic': 'z', 'startUrl': 'https://z.example', 'cadenceMs': 3600000}])
        self.assertEqual((added, removed), (1, 0))
        self.assertEqual(len(state['researchTopics']), 1)
        t = state['researchTopics'][0]
        self.assertEqual(t['topic'], 'z')
        self.assertGreaterEqual(t['cadenceMs'], sim.MIN_RESEARCH_CADENCE_MS)

    def test_does_not_duplicate_existing_topic(self):
        state = {'researchTopics': [
            {'id': 't1', 'topic': 'z', 'startUrl': 'https://z.example',
             'cadenceMs': 3600000, 'lastRunAt': 0, 'seenUrls': []}]}
        added, removed = self._call(state, [
            {'topic': 'z', 'startUrl': 'https://z.example', 'cadenceMs': 3600000}])
        self.assertEqual((added, removed), (0, 0))
        self.assertEqual(len(state['researchTopics']), 1)

    def test_removes_deleted_topic_but_keeps_long_horizon(self):
        state = {'researchTopics': [
            {'id': 't1', 'topic': 'x', 'startUrl': 'https://x.example',
             'cadenceMs': 3600000, 'lastRunAt': 0, 'seenUrls': []},
            {'id': 't2', 'topic': 'horizon', 'startUrl': 'https://h.example',
             'cadenceMs': 3600000, 'lastRunAt': 0, 'seenUrls': [], 'longHorizon': True}]}
        added, removed = self._call(state, [])
        self.assertEqual((added, removed), (0, 1))
        self.assertEqual([t['id'] for t in state['researchTopics']], ['t2'])

    def test_removal_skipped_when_file_unreadable(self):
        state = {'researchTopics': [
            {'id': 't1', 'topic': 'x', 'startUrl': 'https://x.example',
             'cadenceMs': 3600000, 'lastRunAt': 0, 'seenUrls': []}]}
        added, removed = self._call(state, [], ok=False)
        self.assertEqual((added, removed), (0, 0))
        self.assertEqual(len(state['researchTopics']), 1)

    def test_inert_when_write_gate_disabled(self):
        state = {'researchTopics': [
            {'id': 't1', 'topic': 'x', 'startUrl': 'https://x.example',
             'cadenceMs': 3600000, 'lastRunAt': 0, 'seenUrls': []}]}
        with unittest.mock.patch.object(config, 'ALLOW_WATCHLIST_WRITE', False), \
             unittest.mock.patch.object(config, 'get_research_watchlist',
                                        return_value=([], True)):
            added, removed = sim._sync_research_watchlist(state)
        self.assertEqual((added, removed), (0, 0))
        self.assertEqual(len(state['researchTopics']), 1)
