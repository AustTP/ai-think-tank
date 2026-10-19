import copy
import json
import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_work_model as local

CONFIG = {'LOCAL_WORK_MODEL_ENABLED': 'true', 'LOCAL_WORK_MODEL_PATH': 'adapter-path',
          'LOCAL_WORK_MODEL_BASE_URL': 'http://127.0.0.1:8888/v1', 'LOCAL_WORK_MODEL_API_KEY': 'test-key'}
STATE = {'agents': {'worker-a': {'role': 'Researcher'}, 'worker-b': {'role': 'Engineer'}},
         'tasks': {'task-a': {'status': 'working', 'taskType': 'research', 'title': 'Research storage options'}}}


@pytest.mark.parametrize('messages', [[], [{'role': 'system', 'content': 'Policy only'}],
    [{'role': 'developer', 'content': 'Policy only'}], [{'role': 'user', 'content': '   '}]])
def test_empty_and_system_only_requests_never_reach_studio(messages):
    with patch.object(local.urllib.request, 'urlopen') as post:
        with pytest.raises(ValueError, match='user task message is required'):
            local.complete(local.route(CONFIG, STATE, 'worker-a', 'task-a'), messages, 100)
    post.assert_not_called()


def test_chat_endpoint_rejects_system_only_before_any_provider_call():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import serve
    app = FastAPI()
    app.post('/chat')(serve._chat_request)
    with patch.object(serve, 'OPENROUTER_API_KEY', 'test'), \
            patch.object(serve, 'verify_session', return_value=True), \
            patch.object(serve, 'verify_agent_key', return_value=True), \
            patch.object(serve, 'get_state_from_db', return_value={}), \
            patch.object(serve, '_call_openrouter_sync') as provider, \
            TestClient(app) as client:
        response = client.post('/chat', json={'model': 'test', 'messages': [
            {'role': 'system', 'content': 'Policy only'}]})
    assert response.status_code == 400
    assert 'user task message is required' in response.json()['error']
    provider.assert_not_called()


@pytest.mark.parametrize('bad_parameters', [
    '<parameter=file_path>source.py</parameter>',
    '<parameter=query>first</parameter><parameter=query>second</parameter>',
])
def test_local_tool_call_validation_requests_correction_before_returning_valid_call(bad_parameters):
    import serve
    tools = [{'type': 'function', 'function': {'name': 'search_library', 'parameters': {
        'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}}}]
    invalid = '<tool_call><function=search_library>' + bad_parameters + '</function></tool_call>'
    valid = '<tool_call><function=search_library><parameter=query>Coinbase source</parameter></function></tool_call>'
    def response(text):
        value = MagicMock()
        value.__enter__.return_value.read.return_value = json.dumps({
            'choices': [{'message': {'content': text}}]}).encode()
        return value
    selection = local.route(CONFIG, STATE, 'worker-a', 'task-a')
    with patch.object(local.urllib.request, 'urlopen', side_effect=[response(invalid), response(valid)]) as post, \
         patch.object(serve, 'log_action'):
        result = local.complete(selection, [{'role': 'user', 'content': 'Inspect Coinbase source'}], 100, tools)
    assert post.call_count == 2
    correction = json.loads(post.call_args.args[0].data)['messages'][-1]['content']
    assert 'Allowed parameters: query' in correction
    assert 'rejected before execution' in correction
    call = result['choices'][0]['message']['tool_calls'][0]
    assert json.loads(call['function']['arguments']) == {'query': 'Coinbase source'}


def test_tool_validation_retries_stop_after_three_invalid_completions():
    import serve
    tools = [{'type': 'function', 'function': {'name': 'search_library', 'parameters': {
        'type': 'object', 'properties': {'query': {'type': 'string'}}}}}]
    invalid = '<tool_call><function=search_library><parameter=password>x</parameter></function></tool_call>'
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        'choices': [{'message': {'content': invalid}}]}).encode()
    with patch.object(local.urllib.request, 'urlopen', return_value=response) as post, \
         patch.object(serve, 'log_action'):
        with pytest.raises(RuntimeError, match='invalid after 3 attempts; no invalid tool was executed'):
            local.complete(local.route(CONFIG, STATE, 'worker-a', 'task-a'),
                           [{'role': 'user', 'content': 'Inspect source'}], 100, tools)
    assert post.call_count == 3


@pytest.mark.parametrize('formatting', [
    lambda value: value,
    lambda value: '```json\n' + value + '\n```',
    lambda value: 'Here is the summary:\n' + value + '\nEnd of summary.',
    lambda value: json.dumps(json.loads(value)['items']),
])
def test_summary_accepts_complete_json_formatting_variations(formatting):
    source = 'source.py returned {"access": "denied"}'
    raw = json.dumps({'items': [{'text': 'source.py access was denied.', 'evidence': source}]})
    assert 'access was denied' in local._grounded_summary(formatting(raw), source)


def test_summary_retry_reformats_invalid_response_and_accrues_each_call():
    import serve
    valid = json.dumps({'items': [{'text': 'Access was denied.', 'evidence': 'access denied'}]})
    responses = [{'choices': [{'message': {'content': raw}}], 'usage': {'cost': 0.01}}
                 for raw in ['The inspection was denied.', valid]]
    with patch.object(serve, '_call_openrouter_sync', side_effect=responses) as call, \
         patch.object(serve, '_task_budget_exhausted', return_value=False), \
         patch.object(serve, '_accrue_spend') as spend, \
         patch.object(serve, '_accrue_task_spend') as task_spend, \
         patch.object(serve, 'log_action'):
        summary = local._summary_with_retries({'task_id': 'task-a'}, 'remote/model',
                                              [{'role': 'user', 'content': 'access denied'}], 'access denied')
    assert 'Access was denied' in summary
    assert call.call_count == spend.call_count == task_spend.call_count == 2
    correction = call.call_args.args[1][-1]['content']
    assert 'Previous invalid response' in correction
    assert 'one complete JSON object' in correction


def test_summary_retries_are_bounded_and_do_not_accept_ungrounded_output():
    import serve
    invalid = {'choices': [{'message': {'content': json.dumps({
        'items': [{'text': 'Live holdings fetched.', 'evidence': 'not in history'}]})}}]}
    with patch.object(serve, '_call_openrouter_sync', return_value=invalid) as call, \
         patch.object(serve, 'log_action'):
        with pytest.raises(RuntimeError, match='after 3 attempts.*oversized history was not sent'):
            local._summary_with_retries({}, 'remote/model', [], 'access denied')
    assert call.call_count == 3


def test_summary_retry_respects_task_budget():
    import serve
    invalid = {'choices': [{'message': {'content': 'not JSON'}}]}
    with patch.object(serve, '_call_openrouter_sync', return_value=invalid) as call, \
         patch.object(serve, '_task_budget_exhausted', side_effect=[False, True]), \
         patch.object(serve, 'log_action'):
        with pytest.raises(RuntimeError, match='task budget is exhausted'):
            local._summary_with_retries({'task_id': 'task-a'}, 'remote/model', [], 'access denied')
    assert call.call_count == 1


def test_success_after_summary_retry_is_cached():
    import serve
    valid = json.dumps({'items': [{'text': 'Access was denied.', 'evidence': 'access denied'}]})
    responses = [{'choices': [{'message': {'content': raw}}]} for raw in ['not JSON', valid]]
    with patch.dict(local._SUMMARY_CACHE, {}, clear=True), \
         patch.object(serve, '_mid_tier_slug', return_value='remote/model'), \
         patch.object(serve, '_call_openrouter_sync', side_effect=responses) as call, \
         patch.object(serve, 'log_action'):
        history = [{'role': 'tool', 'content': 'access denied'}]
        first = local._summarize_history({}, history)
        assert local._summarize_history({}, history) == first
    assert call.call_count == 2


def test_primary_excludes_planning_reviews_but_keeps_workers_repairs_and_chat():
    config = {**CONFIG, 'PRIMARY_MODEL_PROVIDER': 'unsloth'}
    assert local.route(config, {}, None)['model'] == CONFIG['LOCAL_WORK_MODEL_PATH']
    state = copy.deepcopy(STATE)
    state['agents']['worker-a']['role'] = 'Reviewer'
    state['tasks']['task-a'].update(reviewOf='parent', status='repairing', title='Fix integration')
    assert local.route(config, state, 'worker-a', 'task-a', 'correction') is None
    state['agents']['worker-a']['role'] = 'Engineer'
    assert local.route(config, state, 'worker-a', 'task-a', 'correction')
    assert local.route(config, STATE, 'worker-a', 'task-a', 'planning') is None
    for role in ['Scrum Master', 'Dedicated Peer Reviewer', 'Code Director']:
        state['agents']['worker-a']['role'] = role
        assert local.route(config, state, 'worker-a') is None


def test_remote_override_survives_primary_setting_and_restores_after_failure():
    config = {**CONFIG, 'PRIMARY_MODEL_PROVIDER': 'unsloth'}
    assert local.remote_call(local.primary_enabled, config) is False
    try:
        local.remote_call(lambda: (_ for _ in ()).throw(RuntimeError('provider failed')))
    except RuntimeError:
        pass
    assert local.primary_enabled(config)


def test_primary_plain_and_tool_calls_never_post_to_remote_provider():
    import serve
    config = {**CONFIG, 'PRIMARY_MODEL_PROVIDER': 'unsloth'}
    response = {'choices': [{'message': {'content': 'local'}}], 'usage': {'cost': 0}}
    with patch.object(serve, '_load_env', return_value=config), \
         patch.object(local, 'complete', return_value=response) as complete, \
         patch.object(serve, '_read_openrouter_response', side_effect=AssertionError('Remote call')):
        assert serve._call_openrouter_once_sync('remote/model', [], 64) == response
        assert serve._post_openrouter_raw('remote/model', [], tools=[], tool_choice='required') == response
    assert complete.call_count == 2
    assert complete.call_args.kwargs['tool_choice'] == 'required'


def test_review_remote_call_posts_to_configured_provider_with_primary_enabled():
    import serve
    config = {**CONFIG, 'PRIMARY_MODEL_PROVIDER': 'unsloth'}
    response = {'choices': [{'message': {'content': 'review'}}]}
    with patch.object(serve, '_load_env', return_value=config), \
         patch.object(serve, '_think_tank_spend_cap_exceeded', return_value=False), \
         patch.object(serve, 'is_model_circuit_broken', return_value=False), \
         patch.object(serve, 'record_model_result'), \
         patch.object(local, 'complete', side_effect=AssertionError('Local review')), \
         patch.object(serve, '_read_openrouter_response', return_value=response) as post:
        assert local.remote_call(serve._post_openrouter_raw, 'review/model', [], tools=[]) == response
    assert post.call_args.args[0].full_url == 'https://openrouter.ai/api/v1/chat/completions'
    assert json.loads(post.call_args.args[0].data)['model'] == 'review/model'


def test_workers_get_stable_distinct_seeds():
    a = local.route(CONFIG, STATE, 'worker-a', 'task-a')
    b = local.route(CONFIG, STATE, 'worker-b', 'task-a')
    assert a['seed'] != b['seed']
    assert a['seed'] == local.route(CONFIG, STATE, 'worker-a', 'task-a')['seed']
    assert len({local.agent_seed(str(i), map(str, range(100))) for i in range(100)}) == 100


def test_local_completion_sends_summary_instead_of_oversized_history():
    import context_budget
    selection = local.route(CONFIG, STATE, 'worker-a', 'task-a')
    messages = [{'role': 'system', 'content': 'Preserve the task'},
                {'role': 'user', 'content': 'Inspect integration'},
                {'role': 'assistant', 'content': 'Old tool evidence. ' * 8000},
                {'role': 'user', 'content': 'Finish'}]
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        'choices': [{'message': {'content': 'done'}}]}).encode()
    with patch.object(local, '_summarize_history', return_value='Earlier inspection failed; source.py needs access.') as summary, \
         patch.object(local.urllib.request, 'urlopen', return_value=response) as post:
        local.complete(selection, messages, 5000)
    body = json.loads(post.call_args.args[0].data)
    assert summary.call_count == 1
    assert body['max_tokens'] == 1500
    assert 'Earlier inspection failed' in json.dumps(body['messages'])
    assert 'Old tool evidence. ' * 8000 not in json.dumps(body)
    assert context_budget.token_count(body['messages']) <= 8192 - 1500 - 512


def test_summary_failure_prevents_local_inference_request():
    selection = local.route(CONFIG, STATE, 'worker-a', 'task-a')
    messages = [{'role': 'user', 'content': 'Task'},
                {'role': 'assistant', 'content': 'history ' * 10000}]
    with patch.object(local, '_summarize_history', side_effect=RuntimeError('Summary provider offline')), \
         patch.object(local.urllib.request, 'urlopen') as post:
        import pytest
        with pytest.raises(RuntimeError, match='Summary provider offline'):
            local.complete(selection, messages, 900)
    post.assert_not_called()


def test_summary_rejects_invented_evidence_and_unseen_source_paths():
    source = 'Inspected source.py; private account access denied.'
    raw = json.dumps({'items': [
        {'text': 'source.py inspection denied private account access.', 'evidence': 'private account access denied'},
        {'text': 'Inspected invented.py.', 'evidence': 'Inspected source.py'},
        {'text': 'Fetched live holdings.', 'evidence': 'live balance fetched'}]})
    summary = local._grounded_summary(raw, source)
    assert 'private account access' in summary
    assert 'invented.py' not in summary and 'live holdings' not in summary


def test_summary_accepts_quotation_wrappers_and_json_escaped_source():
    source = json.dumps([{'content': 'Access was denied.\nUse "View" permission.'}])
    raw = json.dumps({'items': [
        {'text': 'Access was denied.', 'evidence': '"Access was denied."'},
        {'text': 'View permission is mentioned.', 'evidence': 'Use "View" permission.'},
        {'text': 'Invented approval.', 'evidence': '"Access was approved."'}]})
    summary = local._grounded_summary(raw, source)
    assert 'Access was denied.' in summary
    assert 'View permission is mentioned.' in summary
    assert 'Invented approval.' not in summary


def test_summary_keeps_internal_quotation_marks_significant():
    import pytest
    raw = json.dumps({'items': [{'text': 'Permission', 'evidence': 'Use "View" permission.'}]})
    with pytest.raises(RuntimeError, match='no supported findings'):
        local._grounded_summary(raw, 'Use View permission.')


def test_protected_roles_and_work_stay_remote():
    for role in ['Reviewer', 'QA', 'Code Director', 'Correction Specialist']:
        state = copy.deepcopy(STATE)
        state['agents']['worker-a']['role'] = role
        assert local.route(CONFIG, state, 'worker-a', 'task-a') is None
    for key, value in [('taskType', 'review'), ('title', 'Repair the report'),
                       ('reviewOf', 'parent'), ('recoveryOf', 'parent'), ('investigationOf', 'parent'),
                       ('failNote', 'previous failure'), ('status', 'done')]:
        state = copy.deepcopy(STATE)
        state['tasks']['task-a'][key] = value
        assert local.route(CONFIG, state, 'worker-a', 'task-a') is None
    assert local.route(CONFIG, STATE, 'worker-a', 'task-a', 'correction') is None


def test_inline_corrections_stay_remote():
    assert local.route(CONFIG, STATE, 'worker-a', 'task-a', messages=[{'role': 'user', 'content': 'Fix the syntax error'}]) is None
    assert local.route(CONFIG, STATE, 'worker-a', 'task-a', messages=[{'role': 'tool', 'content': 'Fix the syntax error'}])


def test_no_active_work_or_disabled_config_stays_remote():
    assert local.route(CONFIG, STATE, 'worker-a') is None
    assert local.route({}, STATE, 'worker-a', 'task-a') is None
    assert local.route(CONFIG, STATE, None, 'task-a') is None


def test_local_payload_has_seed_tools_and_zero_cost():
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        'choices': [{'message': {'content': 'done'}}], 'usage': {'cost': 42}}).encode()
    selection = local.route(CONFIG, STATE, 'worker-a', 'task-a')
    with patch.object(local.urllib.request, 'urlopen', return_value=response) as call:
        result = local.complete(selection, [{'role': 'user', 'content': 'Research'}], 100, [], 'required')
    payload = json.loads(call.call_args.args[0].data)
    assert payload['seed'] == selection['seed']
    assert payload['tool_choice'] == 'required'
    assert payload['tools'] == []
    assert result['usage']['cost'] == 0


def test_provider_failure_does_not_fallback_to_paid_model():
    selection = local.route(CONFIG, STATE, 'worker-a', 'task-a')
    with patch.object(local.urllib.request, 'urlopen', side_effect=RuntimeError('offline')):
        try:
            local.complete(selection, [{'role': 'user', 'content': 'Check local model availability.'}], 100)
        except RuntimeError as error:
            assert str(error) == 'offline'
        else:
            raise AssertionError('Failure must surface')


def test_studio_loads_adapter_when_base_is_resident():
    def response(value):
        result = MagicMock()
        result.__enter__.return_value.read.return_value = json.dumps(value).encode()
        return result
    config = {**CONFIG, 'LOCAL_WORK_MODEL_MANAGE_STUDIO': 'true'}
    selection = local.route(config, STATE, 'worker-a', 'task-a')
    with patch.object(local.urllib.request, 'urlopen', side_effect=[
        response({'active_model': 'base-model'}), response({'status': 'loaded'}),
        response({'active_model': 'ref:adapter'}),
        response({'model': 'adapter-path', 'choices': [{'message': {'content': 'done'}}]})
    ]) as call:
        local.complete(selection, [{'role': 'user', 'content': 'Check the loaded adapter.'}], 100)
    assert call.call_count == 4
    load = json.loads(call.call_args_list[1].args[0].data)
    assert load['model_path'] == 'adapter-path' and load['is_lora']
    with patch.object(local.urllib.request, 'urlopen', side_effect=[
        response({'active_model': 'ref:adapter'}),
        response({'model': 'adapter-path', 'choices': [{'message': {'content': 'done'}}]})
    ]) as call:
        local.complete(selection, [{'role': 'user', 'content': 'Check the loaded adapter.'}], 100)
    assert call.call_count == 2  # Canonical Studio alias does not cause a reload.


def test_wrong_model_response_is_rejected():
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        'model': 'base-model', 'choices': [{'message': {'content': 'done'}}]}).encode()
    with patch.object(local.urllib.request, 'urlopen', return_value=response):
        try:
            local.complete(local.route(CONFIG, STATE, 'worker-a', 'task-a'),
                           [{'role': 'user', 'content': 'Check model identity.'}], 100)
        except RuntimeError as error:
            assert 'different model' in str(error)
        else:
            raise AssertionError('Must not accept a base-model response')
