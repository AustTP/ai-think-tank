import asyncio
import copy
import json
import threading
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import approval_automation as automation
import blocker_control
import notify
import serve


class MemoryServer:
    def __init__(self):
        self.values = {}
        self.state = {'tasks': {'t': {'id': 't', 'title': 'Build module', 'status': 'blocked',
            'assignedTo': 'ada', 'failNote': 'Generated Python has an incomplete definition.',
            'acceptanceCriteria': 'Module imports and all existing tests pass.'}}, 'workQueue': []}
        self.escalations = {}
        self._STATE_UPDATE_LOCK = threading.RLock()
        self.log_action = Mock()
        self._send_player_email_sync = Mock(return_value=True)
        self._grant_allowlist = Mock(return_value='example.com')
        self._coding_tier_slug = Mock(return_value='coding-model')
        self._mid_tier_slug = Mock(return_value='review-model')

    def _get_setting(self, key, default=None):
        return self.values.get(key, default)

    def _set_setting(self, key, value):
        self.values[key] = value

    def get_state_from_db(self):
        return copy.deepcopy(self.state)

    def save_state_to_db(self, state):
        self.state = copy.deepcopy(state)

    def _load_escalations(self):
        return copy.deepcopy(self.escalations)

    def _save_escalations(self, data):
        self.escalations = copy.deepcopy(data)

    def _require_player_session(self, request):
        return request.cookies.get('player') == 'yes'


def enable(server):
    automation.save_settings(server, {'preset': 'always'})


def plan(action='assign_repair'):
    return {'action': action, 'reason': 'Fix the recorded incomplete Python definition.',
        'evidence': ['Generated Python has an incomplete definition.'],
        'steps': ['Inspect the reported definition and complete its body without changing unrelated files.'],
        'verification': 'Import the module and run the existing tests; record their output.',
        'review': {'approved': True, 'reason': 'Targets the recorded syntax failure and preserves the original criteria.'}}


def test_settings_require_player_and_valid_preset():
    server = MemoryServer()
    app = FastAPI()
    app.include_router(automation.router_for(server))
    with TestClient(app) as client:
        assert client.get('/api/intent/automation').status_code == 401
        assert client.post('/api/intent/automation', json={'preset': 'always'}).status_code == 401
        client.cookies.set('player', 'yes')
        assert client.post('/api/intent/automation', json={'alwaysApproveRequests': 'false'}).status_code == 400
        assert client.post('/api/intent/automation', json={'other': True}).status_code == 400
        assert client.post('/api/intent/automation', json={'preset': 'invalid'}).status_code == 400
        assert client.post('/api/intent/automation', json={'preset': True}).status_code == 400
        response = client.post('/api/intent/automation', json={'preset': 'always'})
        assert response.status_code == 200
        assert client.get('/api/intent/automation').json()['settings'] == {
            'preset': 'always', 'alwaysApproveRequests': True, 'autoResolveBlockers': True, 'reportEvery': 10}
        assert client.post('/api/intent/automation', json={'reportEvery': True}).status_code == 400
        assert client.post('/api/intent/automation', json={'reportEvery': 25}).status_code == 200
        current = client.get('/api/intent/automation').json()
        assert current['reportEvery'] == 25 and current['settings']['preset'] == 'always'
        assert current['reportIntervals'] == [5, 10, 25, 50, 100]


@pytest.mark.parametrize('preset, approves, recovers', [
    ('always', True, True), ('moderation', False, True), ('strict', False, False)])
def test_presets_control_both_requests_and_recovery(preset, approves, recovers):
    server = MemoryServer()
    automation.save_settings(server, {'preset': preset})
    assert automation.settings(server) == {'preset': preset,
        'alwaysApproveRequests': approves, 'autoResolveBlockers': recovers, 'reportEvery': 10}
    assert automation.approve_request(server, 'ada', 'execute', 'command', 'fix') == approves
    if not recovers:
        with patch.object(automation, 'recommend') as model:
            automation.process_blockers(server)
        model.assert_not_called()


@pytest.mark.parametrize('legacy, expected', [
    ({'alwaysApproveRequests': True, 'autoResolveBlockers': True}, 'always'),
    ({'alwaysApproveRequests': False, 'autoResolveBlockers': True}, 'moderation'),
    ({'alwaysApproveRequests': False, 'autoResolveBlockers': False}, 'strict'),
    ({'preset': 'unknown'}, 'strict')])
def test_existing_settings_migrate_without_changing_active_behavior(legacy, expected):
    server = MemoryServer()
    server.values[automation.SETTINGS_KEY] = json.dumps(legacy)
    assert automation.settings(server)['preset'] == expected


def test_strict_never_delegates_pending_requests_to_director():
    server = MemoryServer()
    automation.save_settings(server, {'preset': 'strict'})
    with patch.object(serve, '_get_setting', side_effect=server._get_setting), \
            patch.object(serve, '_load_escalations') as pending, \
            patch.object(serve, '_jev_quorum_choice_sync') as model:
        assert serve._resolve_pending_escalations_sync() == 0
    pending.assert_not_called()
    model.assert_not_called()


def test_moderation_delegates_routine_request_but_leaves_high_risk_pending():
    server = MemoryServer()
    automation.save_settings(server, {'preset': 'moderation'})
    records = {'routine': {'kind': 'routine request', 'status': 'pending'},
        'blocked': {'kind': 'blocked command', 'status': 'pending'}}
    state = {'agentRoster': [{'id': 'director', 'name': 'Director', 'isDirector': True}]}
    with patch.object(serve, '_get_setting', side_effect=server._get_setting), \
            patch.object(serve, '_load_escalations', return_value=records), \
            patch.object(serve, '_save_escalations'), \
            patch.object(serve, 'get_state_from_db', return_value=state), \
            patch.object(serve, '_escalation_judge_drift', return_value=0), \
            patch.object(serve, '_jev_quorum_choice_sync', return_value=('approve', 1.0, 0)), \
            patch.object(serve, '_jev_directory_score', return_value=1.0), \
            patch.object(serve, '_effective_safety_confidence', return_value=0.6), \
            patch.object(serve, '_jev_judge_model', return_value=None), \
            patch.object(serve, 'log_action'), \
            patch.object(automation, 'record_event'):
        assert serve._resolve_pending_escalations_sync() == 1
    assert records['routine']['status'] == 'approved'
    assert records['blocked']['status'] == 'pending'


def test_off_mode_does_not_approve_or_count():
    server = MemoryServer()
    assert not automation.approve_request(server, 'ada', 'execute', 'command', 'fix')
    assert automation.RUNTIME_KEY not in server.values


def test_always_approve_commands_skips_classifier_even_without_api_key():
    server = MemoryServer()
    enable(server)
    approve = automation.approve_request
    with patch.object(serve.approval_automation, 'approve_request', side_effect=lambda *a: approve(server, *a[1:])), \
            patch.object(serve, 'OPENROUTER_API_KEY', ''), \
            patch.object(serve, '_jev_quorum_decision') as classifier:
        allowed, reason = asyncio.run(serve._classify_command('python -m pytest', 'run tests', 'ada'))
    assert allowed and 'always approve' in reason
    classifier.assert_not_called()


def test_pending_requests_approve_but_deliverable_review_stays_a_blocker():
    server = MemoryServer()
    enable(server)
    server.escalations = {'a': {'kind': 'blocked command', 'status': 'pending'},
        'b': {'kind': 'allowlist request', 'status': 'pending', 'note': 'example.com'},
        'c': {'kind': 'stuck review loop', 'status': 'pending'},
        'd': {'kind': 'request', 'status': 'denied'}}
    assert automation.approve_pending(server) == 2
    assert automation.approve_pending(server) == 0
    assert server.escalations['a']['resolvedBy'] == 'player-automation'
    assert server.escalations['c']['status'] == 'pending'
    assert server.escalations['d']['status'] == 'denied'
    server._grant_allowlist.assert_called_once_with('example.com')


def test_report_sends_after_ten_actions_and_is_not_repeated():
    server = MemoryServer()
    for n in range(9):
        automation.record_event(server, 'approval', 'request', 'approved')
    automation.flush_reports(server)
    server._send_player_email_sync.assert_not_called()
    automation.record_event(server, 'blocker_retry', 't', 'repair queued')
    automation.flush_reports(server)
    automation.flush_reports(server)
    server._send_player_email_sync.assert_called_once()
    subject, body = server._send_player_email_sync.call_args.args
    assert '10 automatic' in subject
    assert 'Executive summary' in body and '9 approvals, 1 blocker recovery attempts' in body
    assert sum('. Approved:' in line for line in body.splitlines()) == 9
    assert '10. Recovery attempt: t' in body
    assert json.loads(server.values[automation.RUNTIME_KEY])['reports'][0]['status'] == 'sent'


@pytest.mark.parametrize('interval', [5, 10, 25, 50, 100])
def test_each_interval_batches_every_approved_item_once(interval):
    server = MemoryServer()
    enable(server)
    automation.save_settings(server, {'reportEvery': interval})
    assert automation.settings(server)['preset'] == 'always'
    for n in range(interval - 1):
        automation.record_event(server, 'approval', f'item-{n}', f'Approved request {n}',
                                detail=f'Actual item {n}\nwith a second line')
    automation.flush_reports(server)
    server._send_player_email_sync.assert_not_called()
    automation.record_event(server, 'approval', f'item-{interval - 1}', 'Final approval')
    automation.flush_reports(server)
    automation.flush_reports(server)
    server._send_player_email_sync.assert_called_once()
    subject, body = server._send_player_email_sync.call_args.args
    assert f'{interval} automatic' in subject
    lines = [line for line in body.splitlines() if '. Approved:' in line]
    assert len(lines) == interval
    for n in range(interval):
        assert f'{n + 1}. Approved: item-{n} —' in lines[n]
    runtime = json.loads(server.values[automation.RUNTIME_KEY])
    assert len(runtime['reports'][0]['items']) == interval
    assert runtime['events'] == []
    automation.save_settings(server, {'preset': 'moderation'})
    assert automation.settings(server)['reportEvery'] == interval


@pytest.mark.parametrize('interval', [0, 1, 7, 101, True, '25', 5.0, None])
def test_invalid_email_intervals_are_rejected(interval):
    server = MemoryServer()
    with pytest.raises(ValueError, match='every 5, 10, 25, 50, or 100'):
        automation.save_settings(server, {'reportEvery': interval})
    assert automation.settings(server)['reportEvery'] == 10


def test_lower_interval_batches_waiting_events_without_loss_or_duplicate_ids():
    server = MemoryServer()
    automation.save_settings(server, {'preset': 'moderation', 'reportEvery': 25})
    for n in range(12):
        automation.record_event(server, 'approval', f'item-{n}', 'approved')
    automation.save_settings(server, {'reportEvery': 5})
    runtime = json.loads(server.values[automation.RUNTIME_KEY])
    assert [report['id'] for report in runtime['reports']] == [5, 10]
    assert [item['target'] for report in runtime['reports'] for item in report['items']] == [f'item-{n}' for n in range(10)]
    assert [event['target'] for event in runtime['events']] == ['item-10', 'item-11']


def test_approval_report_includes_actual_request_details():
    server = MemoryServer()
    enable(server)
    automation.approve_request(server, 'ada', 'execute', 'python -m pytest tests/test_app.py', 'Verify app behavior')
    event = json.loads(server.values[automation.RUNTIME_KEY])['events'][0]
    assert event['detail'] == 'python -m pytest tests/test_app.py'
    assert 'Verify app behavior' in event['outcome']


def test_failed_email_is_persisted_and_retried_after_backoff():
    server = MemoryServer()
    for n in range(10):
        automation.record_event(server, 'blocker_retry', 't', 'still blocked')
    server._send_player_email_sync.return_value = False
    automation.flush_reports(server)
    automation.flush_reports(server)
    assert server._send_player_email_sync.call_count == 1
    runtime = json.loads(server.values[automation.RUNTIME_KEY])
    assert runtime['reports'][0]['status'] == 'pending'
    runtime['reports'][0]['lastAttempt'] = 0
    server.values[automation.RUNTIME_KEY] = json.dumps(runtime)
    server._send_player_email_sync.return_value = True
    automation.flush_reports(server)
    assert json.loads(server.values[automation.RUNTIME_KEY])['reports'][0]['status'] == 'sent'


@pytest.mark.parametrize('flag', [{'status': 'blocked'}, {'status': 'failed', 'blocked': True},
    {'status': 'failed', 'blockedReason': 'awaiting_guidance'}, {'status': 'failed', 'manualAttention': True}])
def test_every_explicitly_blocked_task_enters_decision_queue(flag):
    server = MemoryServer()
    server.state['tasks']['t'].update(flag)
    assert [c['taskId'] for c in blocker_control.cards(server.state)] == ['t']
    server.state['tasks']['t']['status'] = 'done'
    assert blocker_control.cards(server.state) == []


@pytest.mark.parametrize('change', [
    {'evidence': ['Invented test failure nowhere in the recorded evidence.']},
    {'steps': []}, {'verification': ''}, {'action': 'cancel'}, {'action': 'retry_review'},
    {'action': 'retry_infrastructure'}, {'action': 'wait_for_inputs', 'missingInputs': ['Unrelated file']}])
def test_unsupported_or_inaccurate_recommendations_are_rejected(change):
    server = MemoryServer()
    proposal = {**plan(), **change}
    with pytest.raises(ValueError):
        automation.validate_recommendation(proposal, blocker_control.cards(server.state)[0])


def test_malformed_steps_get_corrected_before_independent_review():
    server = MemoryServer()
    with patch.object(automation, '_model_json', side_effect=[{**plan(), 'steps': []}, plan(),
            {'approved': True, 'reason': 'The evidence supports this repair.'}]) as model:
        result = automation.recommend(server, blocker_control.cards(server.state)[0], {})
    assert result['review']['approved'] is True
    assert model.call_count == 3
    assert 'failed validation' in model.call_args_list[1].args[2][-1]['content']


def test_reviewer_rejects_plan_before_it_can_drive_a_decision():
    server = MemoryServer()
    with patch.object(automation, '_model_json', side_effect=[plan(),
            {'approved': False, 'reason': 'Steps do not resolve the actual evidence.'}]):
        with pytest.raises(ValueError, match='review rejected'):
            automation.recommend(server, blocker_control.cards(server.state)[0], {})


@pytest.mark.parametrize('content', ['{"action":"investigate"}', '```json\n{"action":"investigate"}\n```'])
def test_recovery_json_uses_remote_reviewer_when_local_is_primary(content):
    import local_work_model
    server = MemoryServer()
    messages = [{'role': 'system', 'content': 'Recommend a recovery.'},
                {'role': 'user', 'content': 'The code model timed out.'}]
    def remote(model, history, tokens, **kwargs):
        assert not local_work_model.primary_enabled({'PRIMARY_MODEL_PROVIDER': 'unsloth'})
        assert history == messages and kwargs['response_format'] == {'type': 'json_object'}
        return {'choices': [{'message': {'content': content}}]}
    server._call_openrouter_sync = remote
    with patch.object(local_work_model, 'complete') as studio:
        assert automation._model_json(server, 'review-model', messages) == {'action': 'investigate'}
    studio.assert_not_called()


def test_old_studio_400_gets_one_immediate_retry_with_correct_transport():
    import time
    server = MemoryServer()
    enable(server)
    card = blocker_control.cards(server.state)[0]
    server.state['tasks']['t']['blockerAutomation'] = {'fingerprint': automation.fingerprint(card),
        'status': 'recommendation_failed', 'attempts': 1, 'lastAttempt': time.time(),
        'error': 'HTTP Error 400: Bad Request'}
    with patch.object(automation, 'recommend', side_effect=ValueError('New response unavailable')) as model:
        automation.process_blockers(server)
        automation.process_blockers(server)
    model.assert_called_once()
    assert server.state['tasks']['t']['blockerAutomation']['attempts'] == 2
    assert server.state['tasks']['t']['blockerAutomation']['recommendationVersion'] == 3


def test_reviewed_repair_is_queued_once_and_preserves_acceptance_criteria():
    server = MemoryServer()
    enable(server)
    original = server.state['tasks']['t']['acceptanceCriteria']
    def queue(state, tid, data):
        state['tasks'][tid].update(status='needs_review', manualAttention=False)
        state['workQueue'].append({'reviewOf': tid, 'instructions': data['guidance']})
    with patch.object(automation, 'recommend', return_value=plan()), \
            patch('sim._peer_coding_class', return_value=False), \
            patch('review_guidance.apply_guidance', side_effect=queue):
        automation.process_blockers(server)
        automation.process_blockers(server)
    task = server.state['tasks']['t']
    assert len(server.state['workQueue']) == 1
    assert task['acceptanceCriteria'] == original
    assert task['decisionHistory'][0]['source'] == 'model-automation'
    assert task['blockerRecommendation']['review']['approved'] is True


def test_missing_inputs_remain_blocked_with_actionable_guidance():
    server = MemoryServer()
    automation.save_settings(server, {'preset': 'moderation'})
    server.state['tasks']['t'].update(failNote='Required historical input file is unavailable.', failureCategory='missing_inputs')
    proposal = {**plan('wait_for_inputs'), 'evidence': ['Required historical input file is unavailable.'],
        'missingInputs': ['The historical input file required by the task contract.']}
    with patch.object(automation, 'recommend', return_value=proposal) as model:
        automation.process_blockers(server)
        automation.process_blockers(server)
    assert model.call_count == 1
    task = server.state['tasks']['t']
    assert task['status'] == 'blocked' and task['manualAttention']
    assert task['blockerAutomation']['status'] == 'waiting_for_inputs'
    assert not server.state['workQueue']


def test_stale_model_plan_cannot_change_newer_blocker():
    server = MemoryServer()
    enable(server)
    def changed(*args):
        server.state['tasks']['t']['failNote'] = 'A new failure superseded the old one.'
        return plan()
    with patch.object(automation, 'recommend', side_effect=changed):
        automation.process_blockers(server)
    assert not server.state['workQueue']
    assert 'decisionHistory' not in server.state['tasks']['t']
    assert 'evidence changed' in server.state['tasks']['t']['blockerAutomation']['error']


def test_ten_failed_recommendations_report_and_continue_scheduled_retries():
    server = MemoryServer()
    enable(server)
    with patch.object(automation, 'recommend', side_effect=ValueError('Invalid model response')) as model:
        for n in range(11):
            server.state['tasks']['t'].get('blockerAutomation', {})['lastAttempt'] = 0
            automation.process_blockers(server)
    assert model.call_count == 11
    runtime = json.loads(server.values[automation.RUNTIME_KEY])
    assert runtime['totalActions'] == 11 and len(runtime['reports']) == 1
    assert 'Invalid model response' in runtime['reports'][0]['body']
    assert not server.state['workQueue']


@pytest.mark.parametrize('preset,expected', [('always', True), ('moderation', False), ('strict', False)])
def test_preset_switch_updates_continuous_recovery_policy(preset, expected):
    server = MemoryServer()
    enable(server)
    automation.save_settings(server, {'preset': preset})
    control = server.state['completionControl']
    assert control['approvalPreset'] == preset
    assert control['budgetOnlyStops'] is expected
    automation.save_settings(server, {'reportEvery': 25})
    assert server.state['completionControl']['budgetOnlyStops'] is expected


def test_moderation_retains_recommendation_attempt_limit():
    server = MemoryServer()
    automation.save_settings(server, {'preset': 'moderation'})
    card = blocker_control.cards(server.state)[0]
    server.state['tasks']['t']['blockerAutomation'] = {
        'fingerprint': automation.fingerprint(card), 'attempts': 10, 'lastAttempt': 0}
    with patch.object(automation, 'recommend') as model:
        automation.process_blockers(server)
    model.assert_not_called()


def test_budget_only_mode_rechecks_missing_inputs_after_cooldown():
    server = MemoryServer()
    enable(server)
    server.state['completionControl'] = {'budgetOnlyStops': True}
    card = blocker_control.cards(server.state)[0]
    server.state['tasks']['t']['blockerAutomation'] = {
        'fingerprint': automation.fingerprint(card), 'status': 'waiting_for_inputs',
        'attempts': 100, 'lastAttempt': 0}
    with patch.object(automation, 'recommend', side_effect=ValueError('Still unavailable')) as model:
        automation.process_blockers(server)
    model.assert_called_once()


def test_exhausted_budget_prevents_recovery_model_spend():
    server = MemoryServer()
    enable(server)
    server.state['completionControl'] = {'budgetOnlyStops': True}
    server.state['tasks']['t']['budgetExhausted'] = True
    with patch.object(automation, 'recommend') as model:
        automation.process_blockers(server)
    model.assert_not_called()


def test_setting_approval_resumes_saved_blocked_command_once():
    state = {'tasks': {'t': {'id': 't', 'assignedTo': 'ada', 'status': 'blocked', 'failureCategory': 'policy'}}}
    records = {'a': {'kind': 'blocked command', 'status': 'approved', 'resolvedBy': 'player-automation',
        'note': '{"taskId":"t","agentId":"ada"}', 'resolvedAt': 10}}
    with patch('review_guidance.apply_guidance') as retry:
        notify.reconcile_command_approvals(state, records)
        notify.reconcile_command_approvals(state, records)
    retry.assert_called_once()
