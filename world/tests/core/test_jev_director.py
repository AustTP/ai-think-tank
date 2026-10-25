import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402


def test_low_confidence_allow_goes_to_director():
    state = {'agentRoster': [{'id': 'nora', 'name': 'Nora', 'isDirector': True}]}
    with mock.patch.object(serve, 'get_state_from_db', return_value=state), \
         mock.patch.object(serve, '_senior_most_director_id', return_value='nora'), \
         mock.patch.object(serve, '_effective_safety_confidence', return_value=0.55), \
         mock.patch.object(serve, '_jev_quorum_choice_sync',
                           return_value=('approve', 0.91, 0.0)), \
         mock.patch.object(serve, 'create_escalation') as create, \
         mock.patch.object(serve, 'log_action') as log:
        assert serve._director_second_opinion(
            'clara', 'execute_classify', 'command', 'pytest -q', 'run tests', 0.44)
    create.assert_not_called()
    assert any(c[0][1] == 'jev_chain_approved' for c in log.call_args_list)


def test_director_low_confidence_still_blocks():
    state = {'agentRoster': [{'id': 'nora', 'name': 'Nora', 'isDirector': True}]}
    with mock.patch.object(serve, 'get_state_from_db', return_value=state), \
         mock.patch.object(serve, '_senior_most_director_id', return_value='nora'), \
         mock.patch.object(serve, '_effective_safety_confidence', return_value=0.55), \
         mock.patch.object(serve, '_jev_quorum_choice_sync',
                           return_value=('approve', 0.30, 0.0)), \
         mock.patch.object(serve, 'create_escalation', return_value='esc-1') as create, \
         mock.patch.object(serve, 'log_action') as log:
        assert not serve._director_second_opinion(
            'clara', 'execute_classify', 'command', 'pytest -q', 'run tests', 0.44)
    create.assert_called_once()
    assert any(c[0][1] == 'jev_chain_owner_escalation' for c in log.call_args_list)


def test_low_confidence_nora_deny_moves_to_chief_of_staff():
    state = {'agentRoster': [
        {'id': 'theo', 'name': 'Theo', 'isAdmin': True, 'isDirector': True},
        {'id': 'nora', 'name': 'Nora', 'isDirector': True},
        {'id': 'ruth', 'name': 'Ruth', 'director': 'nora'},
    ]}
    calls = []

    def quorum(instr, criteria):
        calls.append('nora' if ', the senior director' in instr else 'theo')
        return ('deny', 0.30, 0.0) if len(calls) == 1 else ('approve', 0.91, 0.0)

    with mock.patch.object(serve, 'get_state_from_db', return_value=state), \
         mock.patch.object(serve, '_effective_safety_confidence', return_value=0.55), \
         mock.patch.object(serve, '_jev_quorum_choice_sync', side_effect=quorum), \
         mock.patch.object(serve, 'create_escalation') as create, \
         mock.patch.object(serve, 'log_action') as log:
        assert serve._director_second_opinion(
            'ruth', 'execute_classify', 'command', 'pytest -q', 'run tests', 0.44)
    assert calls == ['nora', 'theo']
    create.assert_not_called()
    actions = [c[0][1] for c in log.call_args_list]
    assert actions == ['jev_chain_unsure', 'jev_chain_approved']
    assert log.call_args_list[-1][0][0] == 'theo'


def test_confident_nora_deny_stops_before_chief_of_staff():
    state = {'agentRoster': [
        {'id': 'theo', 'name': 'Theo', 'isAdmin': True, 'isDirector': True},
        {'id': 'nora', 'name': 'Nora', 'isDirector': True},
        {'id': 'ruth', 'name': 'Ruth', 'director': 'nora'},
    ]}
    with mock.patch.object(serve, 'get_state_from_db', return_value=state), \
         mock.patch.object(serve, '_effective_safety_confidence', return_value=0.55), \
         mock.patch.object(serve, '_jev_quorum_choice_sync',
                           return_value=('deny', 0.90, 0.0)) as quorum, \
         mock.patch.object(serve, 'log_action') as log:
        assert not serve._director_second_opinion(
            'ruth', 'execute_classify', 'command', 'rm -rf /', 'cleanup', 0.44)
    assert quorum.call_count == 1
    assert log.call_args[0][1] == 'jev_chain_denied'
