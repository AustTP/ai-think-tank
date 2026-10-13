import copy
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_work_model as local

CONFIG = {'LOCAL_WORK_MODEL_ENABLED': 'true', 'LOCAL_WORK_MODEL_PATH': 'adapter-path',
          'LOCAL_WORK_MODEL_BASE_URL': 'http://127.0.0.1:8888/v1', 'LOCAL_WORK_MODEL_API_KEY': 'test-key'}
STATE = {'agents': {'worker-a': {'role': 'Researcher'}, 'worker-b': {'role': 'Engineer'}},
         'tasks': {'task-a': {'status': 'working', 'taskType': 'research', 'title': 'Research storage options'}}}


def test_workers_get_stable_distinct_seeds():
    a = local.route(CONFIG, STATE, 'worker-a', 'task-a')
    b = local.route(CONFIG, STATE, 'worker-b', 'task-a')
    assert a['seed'] != b['seed']
    assert a['seed'] == local.route(CONFIG, STATE, 'worker-a', 'task-a')['seed']
    assert len({local.agent_seed(str(i), map(str, range(100))) for i in range(100)}) == 100


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
            local.complete(selection, [], 100)
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
        local.complete(selection, [], 100)
    assert call.call_count == 4
    load = json.loads(call.call_args_list[1].args[0].data)
    assert load['model_path'] == 'adapter-path' and load['is_lora']
    with patch.object(local.urllib.request, 'urlopen', side_effect=[
        response({'active_model': 'ref:adapter'}),
        response({'model': 'adapter-path', 'choices': [{'message': {'content': 'done'}}]})
    ]) as call:
        local.complete(selection, [], 100)
    assert call.call_count == 2  # Canonical Studio alias does not cause a reload.


def test_wrong_model_response_is_rejected():
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        'model': 'base-model', 'choices': [{'message': {'content': 'done'}}]}).encode()
    with patch.object(local.urllib.request, 'urlopen', return_value=response):
        try:
            local.complete(local.route(CONFIG, STATE, 'worker-a', 'task-a'), [], 100)
        except RuntimeError as error:
            assert 'different model' in str(error)
        else:
            raise AssertionError('Must not accept a base-model response')
