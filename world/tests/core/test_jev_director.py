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
