"""Residual coverage for world/sim.py residual branches (gap I)."""
import os
import sys
import time
import types


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import sim  # noqa: E402
import serve  # noqa: E402


def test_pathing_skips_agents_without_coords():
    grid = {'cols': 4, 'rows': 4, 'cell': 8,
            'grid': [[0] * 4 for _ in range(4)]}
    sparse = {'a': {'visible': True}}
    sim.find_path(0, 0, 8, 8, 'me', sparse, grid)
    assert sim.agent_blocked_at({'x': 0, 'y': 0, 'w': 5, 'h': 5},
                                sparse) is False


def test_step_movement_skips_agents_without_coords():
    agents = {'a': {'x': 0, 'y': 0, 'visible': True,
                    'path': [{'x': 64, 'y': 0}], 'pathIndex': 0},
              'b': {'visible': True},
              'c': {'visible': True, 'path': [{'x': 1, 'y': 1}]}}
    sim.step_agent_movement(0.1, agents, {'cols': 4, 'rows': 4, 'cell': 8,
                                          'grid': [[0] * 4 for _ in range(4)]})
    assert agents['a']['path']


def test_paused_overlap_and_ages():
    state = {'_simPauseWindows': [['bad'], [10, 20], [15, 30], [50, 60]]}
    assert sim._paused_overlap_ms(state, 0, 40) == 20
    assert sim._task_active_age_ms(state, {'createdAt': 'bad'}, 100) is None
    assert sim._task_active_age_ms(state, {}, 100) is None
    assert sim._effective_work_deadline_ms(
        state, {'workUntil': 'bad'}, 100) == 30


def test_bump_content_heartbeat_trims():
    sim._CONTENT_HEARTBEATS.clear()
    sim._bump_content_heartbeat('')
    old = time.time() - 4000
    for i in range(501):
        sim._CONTENT_HEARTBEATS[f'k{i}'] = old
    sim._bump_content_heartbeat('new')
    assert 'k0' not in sim._CONTENT_HEARTBEATS


def test_completion_evidence_bug_and_missing_cause():
    assert sim._completion_has_evidence({}, None) is True
    assert sim._completion_has_evidence({}, {'taskType': 'bug'}) is False
    assert sim._completion_has_evidence({}, {'taskType': 'bug',
                                             'restoreCheck': True}) is True
    assert sim._completion_has_evidence({}, {'taskType': 'spike'}) is False


def test_fix_titles_and_open_fix():
    assert sim._fix_title_root('Fix: Fix: X (retry 2)') == 'X'
    assert sim._open_fix_exists({}, '') is False
    assert sim._open_fix_exists({'workQueue': [
        {'title': 'Other', 'status': 'queued'}]}, 'Fix') is False
    assert sim._open_fix_exists({'workQueue': [
        {'title': 'Fix', 'status': 'completed'}]}, 'Fix') is False
    assert sim._open_fix_exists({'tasks': {'t': {'title': 'Other'}}},
                                'Fix') is False


def test_queue_peer_repair_guards():
    assert sim._queue_peer_repair({}, {'_peerGate': {'escalated': True}}) is None
    assert sim._queue_peer_repair({}, {'_peerGate': {}, 'assignedTo': 'x'}) is None
    parent = {'id': 'p', 'assignedTo': 'ben', '_peerGate': {}}
    state = {'agents': {'ben': {}}, 'workQueue': [
        {'reviewOf': 'p', 'taskType': 'code'}]}
    assert sim._queue_peer_repair(state, parent) == state['workQueue'][0]
    assert parent['status'] == 'repairing'


def test_peer_gate_required_pool_error_and_lean(monkeypatch):
    monkeypatch.setattr(sim, '_sprint_worker_pool',
                        lambda s, i: (_ for _ in ()).throw(RuntimeError('x')))
    assert sim._peer_gate_required({}, {'sprintId': 's'}) == sim.PEER_APPROVALS_DEFAULT
    monkeypatch.setattr(sim, '_sprint_worker_pool', lambda s, i: ['a', 'b'])
    assert sim._peer_gate_required({}, {'sprintId': 's'}) == sim.PEER_APPROVALS_LEAN_SPRINT


def test_release_agent_after_failure_log_error(monkeypatch):
    monkeypatch.setattr(serve, 'log_action',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    monkeypatch.setattr(sim, '_notify_card_failure', lambda *a, **k: None)
    state = {'agents': {'ben': {'task': 't', 'busy': True}}}
    task = {'id': 't', 'title': 'Card', 'status': 'working'}
    sim._release_agent_after_failure(state, 'ben', task)
    assert task['status'] == 'done'


def test_notify_card_failure_redact_error(monkeypatch):
    monkeypatch.setattr(serve, '_redact_secrets',
                        lambda t: (_ for _ in ()).throw(RuntimeError('x')))
    monkeypatch.setattr(sim, '_notify_player_telegram', lambda *a, **k: True,
                        raising=False)
    state = {}
    sim._notify_card_failure(state, {'id': 't', 'title': 'Card'},
                             'why', now_ms=10 ** 12)
    assert state['_cardFailureNotified']


def test_append_mailbox_archive_trim():
    agent = {'mailbox': [{'i': i} for i in range(sim.MAILBOX_KEEP_COUNT + 1)],
             'mailboxArchive': [{'i': i} for i in range(
                 sim.MAILBOX_ARCHIVE_KEEP_COUNT)]}
    sim._append_mailbox(agent, {'i': 'new'})
    assert len(agent['mailboxArchive']) == sim.MAILBOX_ARCHIVE_KEEP_COUNT
    assert len(agent['mailbox']) == sim.MAILBOX_KEEP_COUNT


def test_mail_card_update_unknown_kind():
    agent = {}
    state = {'agents': {'ben': agent}}
    assert sim._mail_card_update(state, 'ben', 'bogus', 'K', 'T', 'team') is False
    assert sim._mail_card_update(state, 'player', 'issue_done', 'K', 'T', '') is False


def test_ceremony_facilitator_ladders(monkeypatch):
    monkeypatch.setattr(sim, '_refinement_scrum_master_for_team',
                        lambda s, t: 'sm')
    state = {
        'teams': [{'id': 'a', 'directorId': 'did'},
                  {'id': 'b', 'directorId': 'other'}],
        'agents': {'sm': {'busy': False}, 'did': {'busy': True},
                   'other': {'busy': True, 'offDuty': False},
                   'admin': {'busy': False, 'offDuty': True}},
        'agentRoster': [{'id': 'admin', 'isAdmin': True}],
    }
    assert sim._ceremony_facilitator(state, 'a') == 'sm'
    state['agents']['sm']['busy'] = True
    state['agents']['did']['busy'] = False
    assert sim._ceremony_facilitator(state, 'a') == 'did'
    state['agents']['did']['busy'] = True
    state['agents']['other']['busy'] = False
    assert sim._ceremony_facilitator(state, 'a') == 'other'
    state['agents']['other']['busy'] = True
    assert sim._ceremony_facilitator(state, 'a') == 'admin'
    state['agents']['admin']['offDuty'] = False
    state['agents']['sm']['busy'] = True
    state['agents']['did']['busy'] = True
    state['agents']['other']['busy'] = True
    assert sim._ceremony_facilitator(state, 'a') == 'admin'


def test_sprint_failure_ident_and_failures(monkeypatch):
    assert sim._sprint_failure_ident({'title': ' T ', 'room': ' R '}) == ('T', 'R')
    state = {
        'sprints': {'s': {'items': [['Paired', 'room']]}},
        'tasks': {
            'shadow': {'status': 'failed', 'shadow': True},
            'pair': {'id': 'pair', 'title': 'Paired', 'room': 'room',
                     'status': 'failed'},
            'other': {'id': 'other', 'title': 'Nope', 'room': 'x',
                      'status': 'failed'},
        }}
    out = sim._sprint_failures(state, 's')
    assert [f['id'] for f in out] == ['pair']


def test_failure_fix_assignee_ladders(monkeypatch):
    monkeypatch.setattr(sim, 'on_call_agent', lambda *a: 'oc')
    monkeypatch.setattr(sim, '_team_oncall_members', lambda *a: ['backup'])
    agents = {'oc': {'busy': True}, 'backup': {'busy': False}}
    assert sim._failure_fix_assignee({'agents': agents}, 't', 's', 0) == 'backup'
    agents['backup']['busy'] = True
    assert sim._failure_fix_assignee({'agents': agents}, 't', 's', 0) == 'oc'


def test_file_failure_bug_dedup_and_none(monkeypatch):
    failure = {'id': 'f', 'title': 'Card'}
    issue = {'teamId': 't', 'status': 'open', 'title': 'Bug: Card'}
    assert sim._file_failure_bug({'issues': {'i': issue}}, 's', 't', 'd',
                                 failure, {}, 0) is None
    task = {'teamId': 't', 'status': 'working', 'title': 'Bug: Card'}
    assert sim._file_failure_bug({'tasks': {'x': task}}, 's', 't', 'd',
                                 failure, {}, 0) is None
    monkeypatch.setattr(sim, 'file_issue', lambda *a, **k: None)
    assert sim._file_failure_bug({}, 's', 't', 'd', failure, {}, 0) is None


def test_failure_review_decider(monkeypatch):
    monkeypatch.setattr(serve, '_low_tier_slug', lambda: 'm')
    monkeypatch.setattr(serve, '_call_openrouter_sync',
                        lambda *a, **k: {'usage': {'cost': 1},
                                         'choices': [{'message': {
                                             'content': 'x {"findings": []} y'}}]})
    monkeypatch.setattr(serve, '_accrue_spend', lambda *a: None)
    assert sim._failure_review_decider_default({}, 'i', 's', []) == {'findings': []}
    monkeypatch.setattr(serve, '_call_openrouter_sync',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    assert sim._failure_review_decider_default({}, 'i', 's', []) == {}


def test_failure_review_findings_non_list_and_unknown():
    assert sim._failure_review_findings('x', [{'id': 'f'}])[0]['cause'] == 'unknown'
    out = sim._failure_review_findings({'findings': 'x'}, [{'id': 'f'}])
    assert out[0]['cause'] == 'unknown'
    out = sim._failure_review_findings(None, [{'id': 'f', 'infra': True}])
    assert out[0]['cause'] == 'infra'


def test_failure_review_step_skips_future_and_missing(monkeypatch):
    monkeypatch.setattr(sim, '_log_governance', lambda *a, **k: None)
    state = {'pendingFailureReviews': {'future': {'at': 100},
                                       'gone': {'at': 0}}}
    sim._failure_review_step(state, 0, 50, decider=lambda *a: {})
    assert 'future' in state['pendingFailureReviews']
    assert 'gone' not in state['pendingFailureReviews']


def test_supervisor_team_tasks_skips_non_dict():
    state = {'tasks': {'bad': 'x', 't': {'id': 't', 'teamId': 'team',
                                         'updatedAt': 1}},
             'teams': [{'id': 'team', 'members': [], 'directorId': ''}]}
    assert [t['id'] for t in sim._supervisor_team_tasks(state, 'team')] == ['t']


def test_supervisor_brief_branches(monkeypatch, tmp_path):
    assert sim._supervisor_brief({}, 'nope') is None
    monkeypatch.setattr(serve, '_avatar_set_complete',
                        lambda m: (_ for _ in ()).throw(RuntimeError('x')))
    monkeypatch.setattr(sim.os.path, 'isdir', lambda p: True)
    state = {
        'teams': [{'id': 'team', 'name': 'Team', 'members': ['ben'],
                   'directorId': 'dir'}],
        'agents': {'ben': {}, 'dir': {}},
        'tasks': {'t': {'id': 't', 'teamId': 'team', 'status': 'needs_review',
                        'updatedAt': 1, 'title': 'Card'}},
        'sprints': {},
    }
    brief = sim._supervisor_brief(state, 'team', now_ms=10 ** 12)
    assert 'stuck in review' in brief['text']
    assert 'sandboxes' in brief['text']


def test_deliver_supervisor_brief_no_brief(monkeypatch):
    monkeypatch.setattr(sim, '_team_row', lambda s, t: {'directorId': 'd'})
    monkeypatch.setattr(sim, '_supervisor_brief', lambda *a: None)
    state = {'agents': {'d': {'mailbox': []}}}
    assert sim._deliver_supervisor_brief(state, 't') is False


def test_retire_room_and_coach_low_grade(monkeypatch):
    sim.retire_room_grade_signal({}, '')
    state = {'consensusRelay': {'room': 'r',
                                'consensus': 'weak trailing grade'}}
    sim.retire_room_grade_signal(state, 'r')
    assert 'consensusRelay' not in state
    plans = []
    monkeypatch.setattr(sim, 'agent_team_ref', lambda s, a: None)
    monkeypatch.setattr(sim, 'team_policy', lambda *a: 3)
    monkeypatch.setattr(sim, '_write_growth_plan', lambda *a, **k: plans.append(a))
    monkeypatch.setattr(sim, '_escalate_coaching_loop', lambda *a: None)
    state = {'completedDeliverables': [
        {'agentId': 'other', 'grade': 5},
        {'agentId': 'ben', 'grade': 8, 'gradedAt': 10}],
        'growthPlans': {'ben': []}}
    sim._coach_low_grade(state, 'ben', 'room', 2, 0, 't')
    assert plans
    state['growthPlans']['ben'] = [{'kind': 'low_grade', 'ts': 10}] * 3
    sim._coach_low_grade(state, 'ben', 'room', 2, 0, 't')


def test_supervisor_brief_step_branches(monkeypatch):
    monkeypatch.setattr(sim, 'automation_paused', lambda s: False)
    monkeypatch.setattr(sim, '_deliver_supervisor_brief', lambda *a: False)
    monkeypatch.setattr(sim, '_supervisor_attention_reason', lambda *a: '')
    monkeypatch.setattr(sim, '_supervisor_card_open', lambda s, t: t == 'open')
    monkeypatch.setattr(sim, 'queue_work', lambda s, q: None)
    state = {
        'teams': [{'id': ''}, {'id': 'open'}, {'id': 'nodirector'},
                  {'id': 'team'}],
        'sprints': {'s': {'status': 'active', 'teamIds': ['open', 'nodirector', 'team']}},
        'agents': {'admin': {}},
        'agentRoster': [{'id': 'admin', 'isAdmin': True}],
        '_supervisorCardAt': {},
    }
    sim._supervisor_brief_step(state, 0, 10 ** 9)

    state2 = {'teams': [], 'sprints': {}, 'agents': {'admin': {}},
              'agentRoster': [{'id': 'admin', 'isAdmin': True}]}
    sim._supervisor_brief_step(state2, 0, 10 ** 9)
    state3 = {'teams': [{'id': 't'}], 'sprints': {}, 'agents': {'admin': {}},
              'agentRoster': [{'id': 'admin', 'isAdmin': True}],
              '_supervisorCardAt': {'__adminRoutine__': 10 ** 9}}
    sim._supervisor_brief_step(state3, 0, 10 ** 9)


def test_supervisor_card_helpers(monkeypatch):
    assert sim._supervisor_card_open(
        {'workQueue': [{'taskType': 'supervise', 'teamId': 't'}]}, 't') is True
    assert sim._supervisor_card_open(
        {'tasks': {'x': {'taskType': 'supervise', 'teamId': 't',
                         'status': 'working'}}}, 't') is True
    assert sim._supervisor_admin_card_open(
        {'tasks': {'x': {'title': '[SUPERVISE] Cross-team: x',
                         'status': 'working'}}}) is True
    monkeypatch.setattr(sim, '_supervisor_team_tasks', lambda s, t: [
        {'id': 'c', 'status': 'needs_review', 'updatedAt': 1}])
    assert 'stuck in review' in sim._supervisor_attention_reason({}, 't', 10 ** 9)


def test_governance_pass_onboard_queue(monkeypatch):
    monkeypatch.setattr(sim, 'think_tank_has_work', lambda s, n: True)
    monkeypatch.setattr(sim, '_start_auto_hire', lambda *a: None)
    monkeypatch.setattr(sim, '_complete_auto_hire', lambda *a: None)
    monkeypatch.setattr(sim, '_start_onboard_meeting',
                        lambda s, o, n: s.setdefault('_started', []).append(o))
    monkeypatch.setattr(sim, '_resolve_onboard_meeting', lambda *a: None)
    monkeypatch.setattr(sim, '_start_auto_firing_review', lambda *a: None)
    state = {'sim': {'owner': 'server'},
             '_onboardQueue': [{'agentId': 'a'}, {'agentId': 'b'}],
             'agentRoster': [], 'agents': {}, 'tasks': {}, 'workQueue': [],
             'lastHireAt': 10 ** 12, 'lastFiringReviewAt': 10 ** 12}
    sim._governance_pass(state, now_ms=10 ** 12, grid={})
    assert state['_pendingOnboard'] == {'agentId': 'a'}
    assert state['_onboardQueue'] == [{'agentId': 'b'}]
    state['_onboardQueue'] = [{'agentId': 'c'}]
    state['_pendingOnboard'] = None
    sim._governance_pass(state, now_ms=10 ** 12, grid={})
    assert '_onboardQueue' not in state


def test_requeue_paused_work(monkeypatch):
    queued = []
    monkeypatch.setattr(sim, 'queue_work', lambda s, q: queued.extend(q))
    monkeypatch.setattr(sim, '_resolve_assignment_room', lambda t: 'room')
    sim._requeue_paused_work({}, {'title': 'T', 'id': 't'}, 5)
    assert queued[0]['room'] == 'room'


def _task_cycle_patches(monkeypatch):
    for name in ('_check_schedules', '_governance_pass', '_sweep_stuck_gates',
                 '_prune_state_blob', '_reclaim_orphaned_walking_tasks',
                 '_stale_work_step', '_coaching_loop_step', '_refinement_step',
                 '_breakdown_step', '_retro_step', '_failure_review_step',
                 '_supervisor_brief_step', '_first_week_onboard_review_step',
                 '_roadmap_step', '_consensus_relay_step', '_rule_mine_step',
                 '_archive_distill_step', '_success_mine_step',
                 '_escalation_step', '_block_step', '_mail_action_step',
                 '_park_idle_wanderers'):
        monkeypatch.setattr(sim, name, lambda *a, **k: None)
    monkeypatch.setattr(sim, 'think_tank_has_work', lambda s, n: True)
    monkeypatch.setattr(sim, '_any_agent_active', lambda a: True)
    monkeypatch.setattr(sim, 'send_agent_off_duty', lambda *a: None)


def test_task_cycle_credit_pause_and_error(monkeypatch):
    _task_cycle_patches(monkeypatch)
    monkeypatch.setattr(serve, 'credits_pause_active', lambda: True)
    state = {'sim': {'owner': 'server'},
             'agents': {'a': {'task': 't', 'busy': True}},
             'tasks': {'t': {'id': 't', 'status': 'working'}},
             'workQueue': []}
    sim._task_cycle(state, now=1, grid={}, doors={})
    assert state['tasks']['t']['workUntil'] >= 301
    monkeypatch.setattr(serve, 'credits_pause_active',
                        lambda: (_ for _ in ()).throw(RuntimeError('x')))
    sim._task_cycle(state, now=1, grid={}, doors={})


def test_task_cycle_transport_and_credits(monkeypatch):
    _task_cycle_patches(monkeypatch)
    import agents.transport_recovery as transport_recovery
    import agents.story_tracking as story_tracking
    monkeypatch.setattr(serve, 'credits_pause_active', lambda: False)
    monkeypatch.setattr(sim, '_apply_content_result', lambda *a: None)
    monkeypatch.setattr(sim, '_take_content_result',
                        lambda tid: {'note': 'n', 'creditsPaused': True})
    monkeypatch.setattr(transport_recovery, 'queue', lambda *a: 'queued')
    monkeypatch.setattr(story_tracking, 'root',
                        lambda s, t: {'status': 'working'})
    state = {'sim': {'owner': 'server'},
             'agents': {'a': {'task': 't', 'busy': True}},
             'tasks': {'t': {'id': 't', 'status': 'working',
                             'workUntil': 10 ** 12, '_contentInFlight': True}},
             'workQueue': []}
    sim._task_cycle(state, now=1, grid={}, doors={})
    assert state['tasks']['t']['_contentInFlight'] is False

    monkeypatch.setattr(transport_recovery, 'queue',
                        lambda *a: (_ for _ in ()).throw(RuntimeError('x')))
    state = {'sim': {'owner': 'server'},
             'agents': {'a': {'task': 't', 'busy': True}},
             'tasks': {'t': {'id': 't', 'status': 'working',
                             'workUntil': 10 ** 12, '_contentInFlight': True}},
             'workQueue': []}
    sim._task_cycle(state, now=1, grid={}, doors={})


def test_assign_task_pinned_at_door(monkeypatch):
    grid = {'cols': 4, 'rows': 4, 'cell': 8,
            'grid': [[0] * 4 for _ in range(4)]}
    doors = {'room': {'x': 0, 'y': 0, 'w': 8, 'h': 8}}
    monkeypatch.setattr(sim, 'find_path', lambda *a: None)
    state = {'agents': {'a': {'x': 4, 'y': 12, 'busy': False}},
             'tasks': {}}
    state = {'agents': {'a': {'x': 4, 'y': 12, 'busy': False}},
             'tasks': {}, 'sim': {}}
    sim.assign_task(state, 'a', 'T', 'room', 'i', '', {}, grid, doors, 0,
                    [0])


def test_sim_loop_pass_tick_errors(monkeypatch):
    monkeypatch.setattr(serve, '_dormant', lambda: False)
    monkeypatch.setattr(serve, '_state_begin', lambda: {'x': 1})
    monkeypatch.setattr(sim, '_record_tick_gap', lambda *a: None)
    monkeypatch.setattr(sim._engine, 'tick', lambda s: s)
    monkeypatch.setattr(sim.governance, 'supervisor_tick',
                        lambda s: (_ for _ in ()).throw(RuntimeError('sup')))
    monkeypatch.setattr(serve, '_apply_pending_ask_results', lambda s: None)
    monkeypatch.setattr(serve, '_peer_review_tick', lambda s: None)
    monkeypatch.setattr(serve, '_adversarial_village_tick', lambda s: None)
    import agents.story_tracking as story_tracking
    import agents.completion_watchdog as completion_watchdog
    monkeypatch.setattr(story_tracking, 'refresh', lambda *a: None)
    monkeypatch.setattr(completion_watchdog, 'step',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('cw')))
    monkeypatch.setattr(serve, '_spend_ledger_read', lambda: {})
    monkeypatch.setattr(serve, 'save_state_to_db', lambda s: None)
    monkeypatch.setattr(serve, '_state_abort', lambda: None)
    state = sim._sim_loop_pass()
    assert state == {'x': 1, 'completionControl': state['completionControl']}


def test_dispatch_content_credits_paused(monkeypatch):
    import threading

    class _T:
        def __init__(self, target=None, daemon=None):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(threading, 'Thread', _T)
    monkeypatch.setattr(sim, '_build_agent_perception', lambda *a: {})
    stored = []
    monkeypatch.setattr(sim, '_store_content_result',
                        lambda t, r: stored.append(r))
    exc = RuntimeError('no credits')
    exc.credits_paused = True

    def executor(*a, **k):
        raise exc

    task = {'id': 't', 'research': {'seenUrls': ['u']}}
    sim._dispatch_content_work(executor, {}, 'a', task, 0)
    assert stored[0]['creditsPaused'] is True


def test_apply_content_result_parent_contract_sources_and_repeat(monkeypatch):
    import agents.repeat_work as repeat_work
    monkeypatch.setattr(repeat_work, 'record',
                        lambda *a: (_ for _ in ()).throw(RuntimeError('x')))
    parent = {'id': 'p', 'sourceUrls': ['a']}
    state = {'tasks': {'p': parent}}
    task = {'id': 'r', 'reviewOf': 'p'}
    sim._apply_content_result(state, task, {
        'ok': False, 'reviewContract': {'x': 1}, 'sourceUrls': ['b']}, 0)
    assert parent['reviewContract'] == {'x': 1}
    assert parent['sourceUrls'] == ['a', 'b']


def _gate_state():
    return {'tasks': {'p': {'id': 'p', '_peerGate': {
        'enteredMs': 2, 'approvals': 0, 'approvers': []}}}}


def test_apply_content_result_older_review_returns(monkeypatch):
    state = _gate_state()
    task = {'id': 'r', 'reviewOf': 'p', 'taskType': 'review',
            'createdAt': 1, 'assignedTo': 'rev'}
    sim._apply_content_result(state, task, {'peerVerdict': 'clean'}, 10)


def test_apply_content_result_review_format_escalates(monkeypatch):
    state = _gate_state()
    gate = state['tasks']['p']['_peerGate']
    gate['serviceFailures'] = 2
    task = {'id': 'r', 'reviewOf': 'p', 'taskType': 'review',
            'createdAt': 3, 'assignedTo': 'rev'}
    sim._apply_content_result(state, task, {
        'reviewFormatError': True, 'reviewFailureKind': 'service_unavailable',
        'note': 'x'}, 10)
    assert gate['escalated'] is True
    assert state['tasks']['p']['manualAttention'] is True


def test_apply_content_result_clean_errors_escalate(monkeypatch):
    import agents.review_quality as review_quality
    monkeypatch.setattr(review_quality, 'clean_vote_errors',
                        lambda *a, **k: ['bad'])
    monkeypatch.setattr(review_quality, 'known_failure', lambda r: False)
    state = _gate_state()
    gate = state['tasks']['p']['_peerGate']
    gate['formatFailures'] = 2
    task = {'id': 'r', 'reviewOf': 'p', 'taskType': 'review',
            'createdAt': 3, 'assignedTo': 'rev'}
    sim._apply_content_result(state, task, {
        'peerVerdict': 'clean', 'pipelineOk': True}, 10)
    assert gate['escalated'] is True


def test_apply_content_result_clean_red_pipeline_is_actionable(monkeypatch):
    import agents.review_quality as review_quality
    monkeypatch.setattr(review_quality, 'clean_vote_errors',
                        lambda *a, **k: [])
    state = _gate_state()
    task = {'id': 'r', 'reviewOf': 'p', 'taskType': 'review',
            'createdAt': 3, 'assignedTo': 'rev', 'note': 'n'}
    sim._apply_content_result(state, task, {
        'peerVerdict': 'clean', 'pipelineOk': False}, 10)
    assert state['tasks']['p']['_peerGate']['approvals'] == 0


def test_sweep_stuck_gates_second_vote_rescue(monkeypatch):
    state = {'tasks': {'p': {'id': 'p', 'status': 'needs_review',
                             'assignedTo': 'author', 'room': 'r',
                             '_peerGate': {'enteredMs': 1, 'approvals': 1,
                                           'approvers': ['a'],
                                           'requiredApprovals': 2,
                                           'reviewerIds': ['a']}}},
             'workQueue': [], 'agentRoster': [{'id': ''}, {'id': 'new'}, {'id': 'extra'}]}
    monkeypatch.setattr(sim, '_pick_reviewer_ids', lambda *a, **k: [])
    monkeypatch.setattr(sim, '_reenter_gate_review',
                        lambda s, t, ids: True)
    monkeypatch.setattr(sim, '_gate_reviewer_reachable', lambda *a: True)
    monkeypatch.setattr(serve, 'log_action',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    sim._sweep_stuck_gates(state, sim.STUCK_GATE_GRACE_MS * 10)
    assert state['tasks']['p']['_peerGate']['secondVoteRescueTs']


def test_standing_sweep_script_and_check_schedules_disabled(monkeypatch):
    import village.schedule as schedule
    import server.cron_scripts as cron_scripts

    class _Task:
        def __init__(self, **kw):
            self.task_id = kw['task_id']
            self.name = kw['name']
            self.team = kw.get('team')
    monkeypatch.setattr(schedule, 'StandingTask', _Task)
    monkeypatch.setattr(sim._STANDING_SCHEDULER, 'tick_all',
                        lambda tasks, now: {tasks[0].task_id:
                                            types.SimpleNamespace(
                                                action='start',
                                                agent_count=1,
                                                reason='due',
                                                lead_role='lead')})
    ran = []
    monkeypatch.setattr(cron_scripts, 'run_standing',
                        lambda *a: ran.append(a))
    state = {'standingTasks': [{'taskId': 't', 'name': 'N',
                                'script': 's.sh'}], 'agents': {'a': {}}}
    sim._standing_task_sweep(state, time.time(), 0)
    assert ran

    monkeypatch.setattr(sim, '_sync_research_watchlist', lambda s: None)
    monkeypatch.setattr(sim, '_ensure_long_horizon_topic', lambda *a: None)
    state = {'researchTopics': [{'id': 't', 'enabled': False}]}
    sim._check_schedules(state, time.time(), 0)


def test_start_auto_hire_team_cap(monkeypatch):
    monkeypatch.setattr(sim, 'MAX_TOTAL_AGENTS', 10)
    monkeypatch.setattr(sim, 'MAX_ACTIVE_AGENTS', 10)
    monkeypatch.setattr(sim, 'active_agent_count', lambda s: 1)
    monkeypatch.setattr(sim, '_dormant_workers_will_be_woken', lambda *a: False)
    monkeypatch.setattr(sim, '_sim_direct_reports',
                        lambda s, d: ['m'] if d == 'dir' else [])
    monkeypatch.setattr(sim, '_team_under_cap', lambda s, d: False)
    state = {'agentRoster': [{'id': 'dir'}],
             'agents': {'dir': {'busy': False}, 'm': {'busy': False}}}
    assert sim._start_auto_hire(state, 0, {}, None) is False


def test_complete_auto_hire_cap(monkeypatch):
    monkeypatch.setattr(sim, '_hire_name_chooser', lambda *a: 'Newbie')
    monkeypatch.setattr(sim, '_all_used_names', lambda s: set())
    monkeypatch.setattr(sim, '_next_hire_name', lambda s: 'other')
    monkeypatch.setattr(sim, '_remember_name', lambda *a: None)
    monkeypatch.setattr(sim, 'MAX_TOTAL_AGENTS', 1)
    state = {'agents': {'admin': {}, 'help': {'role': 'dev'}},
             'agentRoster': [{'id': 'x'}]}
    out = sim._complete_auto_hire(state, {
        'adminId': 'admin', 'helpForId': 'help', 'helpForName': 'Help',
        'adminName': 'Admin', 'directorId': 'dir'}, {}, 0)
    assert out is None
    assert state['_hireBlockedAtCapNote'] == 1


def test_start_onboard_meeting_fallback_leader(monkeypatch):
    monkeypatch.setattr(sim, '_onboard_coworker_defs', lambda s, o: [])
    monkeypatch.setattr(sim, '_log_governance', lambda *a: None)
    state = {'agents': {'admin': {'busy': True},
                        'dir': {'busy': False, 'name': 'Dir'}},
             'agentRoster': [{'id': 'dir', 'name': 'Dir'}]}
    onboard = {'leaderId': 'admin', 'directorId': 'dir', 'agentId': 'a'}
    assert sim._start_onboard_meeting(state, onboard, 0) is True
    assert onboard['leaderId'] == 'dir'


def test_empower_work_agreement_key_failure(monkeypatch):
    monkeypatch.setattr(sim, '_sim_admin_id', lambda s: 'admin')
    monkeypatch.setattr(sim, '_work_agreement_text', lambda s, a: 'text')
    monkeypatch.setattr(sim, '_log_governance', lambda *a: None)
    monkeypatch.setattr(serve, 'get_or_create_agent_key',
                        lambda a: (_ for _ in ()).throw(RuntimeError('x')))
    state = {'agents': {'new': {}}}
    text, admin = sim._empower_work_agreement(
        state, {'agentId': 'new', 'directorId': 'dir'}, 0)
    assert text == 'text'
    assert state['agents']['new']['profile']['onboarding']['provisioning']['agentKey'] == 'pending'


def test_request_player_input_backlog_cap(monkeypatch):
    monkeypatch.setattr(sim, 'PENDING_PLAYER_ASK_BACKLOG_MAX', 1)
    monkeypatch.setattr(sim, '_issue_director', lambda s, i: 'dir')
    monkeypatch.setattr(serve, 'log_action',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    state = {'_pendingPlayerAsk': {'issueKey': 'open'},
             '_pendingPlayerAskBacklog': [{'issueKey': 'a'},
                                          {'issueKey': 'b'}],
             'issues': {'c': {'needsInput': False}}}
    sim.request_player_input(state, 'c', 'ben', 'q', now_ms=0)
    assert len(state['_pendingPlayerAskBacklog']) == 2
    state['_pendingPlayerAskBacklog'] = [{'issueKey': 'c'}]
    sim.request_player_input(state, 'c', 'ben', 'q', now_ms=0)


def test_peer_coding_class_respects_an_explicit_note_or_report():
    # A note/report card queued into pressoffice is prose, not code. Treating
    # pressoffice as code made the peer review run the code pipeline and demand
    # an executable shell command, which wedged the 2026-10-26 smoke card.
    assert not sim._peer_coding_class({'room': 'pressoffice', 'deliverable': 'note'})
    assert not sim._peer_coding_class({'room': 'pressoffice', 'deliverable': 'report'})
    assert not sim._peer_coding_class({'room': 'pressoffice', 'taskType': 'task'})
    assert not sim._peer_coding_class({'room': 'pressoffice', 'taskType': 'spike'})
    assert sim._peer_coding_class({'room': 'pressoffice', 'deliverable': 'code'})
    assert sim._peer_coding_class({'room': 'pressoffice', 'taskType': 'story'})
    assert sim._peer_coding_class({'room': 'pressoffice', 'taskType': 'bug'})
    assert sim._peer_coding_class({'productId': 'p1'})


def test_notify_card_non_dict_and_mail_done_unknown():
    assert sim._notify_card_failure({}, 'x', 'why') is None
    assert sim._mail_card_update({'agents': {'ben': {'mailbox': []}}}, 'ben',
                                 'bogus', 'K', 'T', '') is False


def test_ceremony_wakeable_ladder(monkeypatch):
    monkeypatch.setattr(sim, '_refinement_scrum_master_for_team',
                        lambda s, t: 'sm')
    state = {'teams': [{'id': 'a', 'directorId': 'did'},
                       {'id': 'b', 'directorId': 'other'}],
             'agents': {'sm': {'busy': False, 'offDuty': True},
                        'did': {'busy': True, 'offDuty': False},
                        'other': {'busy': True, 'offDuty': False},
                        'admin': {'busy': True, 'offDuty': False}},
             'agentRoster': [{'id': 'admin', 'isAdmin': True}]}
    assert sim._ceremony_facilitator(state, 'a') == 'sm'
    state['agents']['sm']['busy'] = True
    state['agents']['other']['busy'] = False
    state['agents']['other']['offDuty'] = True
    assert sim._ceremony_facilitator(state, 'a') == 'other'
    state['agents']['other']['busy'] = True
    state['agents']['admin'] = {'busy': False, 'offDuty': True}
    assert sim._ceremony_facilitator(state, 'a') == 'admin'


def test_failure_decider_import_error(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == 'serve':
            raise ImportError('no serve')
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', fake)
    assert sim._failure_review_decider_default({}, 'i', 's', []) == {}


def test_supervisor_brief_step_card_paths(monkeypatch):
    monkeypatch.setattr(sim, 'automation_paused', lambda s: False)
    monkeypatch.setattr(sim, '_deliver_supervisor_brief', lambda *a: True)
    reasons = {'team': 'attention', 'opencard': 'attention'}
    monkeypatch.setattr(sim, '_supervisor_attention_reason',
                        lambda s, t, n=None: reasons.get(t, ''))
    monkeypatch.setattr(sim, '_supervisor_card_open',
                        lambda s, t: t == 'opencard')
    monkeypatch.setattr(sim, 'queue_work', lambda s, q: None)
    state = {'teams': [{'id': 'opencard'}, {'id': 'team'},
                       {'id': 'nodirector'}],
             'sprints': {'s': {'status': 'active',
                               'teamIds': ['opencard', 'team', 'nodirector']}},
             'agents': {'admin': {}},
             'agentRoster': [{'id': 'admin', 'isAdmin': True}],
             '_supervisorCardAt': {}}
    sim._supervisor_brief_step(state, 0, 10 ** 9)


def test_task_cycle_assign_wait_requeues(monkeypatch):
    _task_cycle_patches(monkeypatch)
    monkeypatch.setattr(serve, 'credits_pause_active', lambda: False)
    item = {'id': 'q', 'title': 'T', 'room': 'r'}
    calls = {'n': 0}

    def pick_next(work, *a, **k):
        calls['n'] += 1
        return 0 if calls['n'] == 1 else -1
    monkeypatch.setattr(sim, 'pick_next_due_index', pick_next)
    monkeypatch.setattr(sim, '_assign_due_item', lambda *a: sim._ASSIGN_WAIT)
    monkeypatch.setattr(sim, '_awake_idle_count', lambda s: 1)
    monkeypatch.setattr(sim, '_any_available_including_off_duty', lambda s: True)
    monkeypatch.setattr(sim, 'can_activate_another', lambda s: True)
    monkeypatch.setattr(sim, 'tier_value_ratios', lambda: None, raising=False)
    monkeypatch.setattr(serve, 'tier_value_ratios', lambda: None)
    state = {'sim': {'owner': 'server'}, 'agents': {'a': {'busy': False}},
             'agentRoster': [{'id': 'a'}], 'tasks': {}, 'workQueue': [item]}
    sim._task_cycle(state, now=1, grid={}, doors={})
    assert state['workQueue'] == [item]


def test_assign_due_item_wait_paths(monkeypatch):
    monkeypatch.setattr(sim, '_resolve_assignment_room', lambda p: 'room')
    monkeypatch.setattr(sim, '_eligible_candidates', lambda *a: [])
    monkeypatch.setattr(sim, '_sprint_worker_pool', lambda *a: None)
    monkeypatch.setattr(sim.governance, 'may_work_code', lambda s, a: False)
    state = {'agents': {}, 'tasks': {}, 'agentRoster': []}
    assert sim._assign_due_item(
        state, {'taskType': 'code', 'assignedTo': 'dir', 'directRoute': True},
        True, {}, {}, 0, ['t']) is None
    assert sim._assign_due_item(
        state, {'taskType': 'task', 'issueKey': 'K'}, True, {}, {}, 0,
        ['t']) is sim._ASSIGN_WAIT
    assert sim._assign_due_item(
        state, {'taskType': 'report', 'directRoute': True}, True, {}, {}, 0,
        ['t']) is sim._ASSIGN_WAIT


def test_peer_repair_carries_original_context():
    parent = {'id': 'p', 'assignedTo': 'ben', 'room': 'pressoffice',
              'taskType': 'spike', 'acceptanceCriteria': 'cite every file',
              'userStory': 'I want the review', 'never': ['do not refetch'],
              'sourcePaths': ['library/x-articles'], 'issueKey': 'BEN-6',
              'requestId': 'iss-1', 'projectLabel': 'X review'}
    state = {'agents': {'ben': {}}, 'workQueue': [], 'tasks': {}}
    sim._queue_peer_repair(state, parent, 'redo it')
    queued = state['workQueue'][0]
    assert queued['reviewOf'] == 'p'
    assert queued['acceptanceCriteria'] == 'cite every file'
    assert queued['sourcePaths'] == ['library/x-articles']
    assert queued['issueKey'] == 'BEN-6'
    assert queued['requestId'] == 'iss-1'
    assert queued['never'] == ['do not refetch']


def test_apply_content_result_dedupes_gate_fix_cards():
    state = {'agents': {}, 'tasks': {'p': {'id': 'p'}}, 'workQueue': []}
    qf = {'title': 'Fix it', 'reviewOf': 'p', 'taskType': 'task',
          'assignedTo': 'author', 'room': 'pressoffice'}
    for rid in ('r1', 'r2'):
        sim._apply_content_result(
            state, {'id': rid, 'assignedTo': 'reviewer', 'taskType': 'review'},
            {'ok': True, 'note': 'rejected', 'queueFix': dict(qf)}, 1000)
    fixes = [q for q in state['workQueue'] if q.get('reviewOf') == 'p']
    assert len(fixes) == 1
