"""Durable wake queue: coalescing, claim, recovery, and marker restore."""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import sim  # noqa: E402


def test_enqueue_coalesces_same_target():
    state = {}
    first = sim._wake_enqueue(state, 'ada', 'player_answer',
                              {'issueKey': 'DEV-1'}, 1000)
    second = sim._wake_enqueue(state, 'ada', 'player_answer',
                               {'issueKey': 'DEV-1', 'text': 'new'}, 2000)
    assert first == second
    assert len(sim._wake_queue(state)) == 1
    assert sim._wake_queue(state)[0]['entry']['text'] == 'new'
    other = sim._wake_enqueue(state, 'ada', 'dependency_landed',
                              {'issueKey': 'DEV-1'}, 3000)
    assert other != first
    assert len(sim._wake_queue(state)) == 2


def test_claim_complete_and_unknown_id():
    state = {}
    wake_id = sim._wake_enqueue(state, 'ada', 'player_answer',
                                {'taskId': 'task-1'}, 1000)
    wake = sim._wake_claim(state, 'ada', 2000)
    assert wake['id'] == wake_id
    assert wake['status'] == 'claimed'
    assert wake['attempts'] == 1
    assert sim._wake_claim(state, 'nobody', 2000) is None
    assert sim._wake_complete(state, 'missing', 'done', 3000) is False
    assert sim._wake_complete(state, wake_id, 'routed', 3000) is True
    assert state['wakeQueue'][0]['status'] == 'done'


def test_stale_claim_is_requeued():
    state = {}
    wake_id = sim._wake_enqueue(state, 'ada', 'player_answer',
                                {'taskId': 'task-1'}, 1000)
    sim._wake_claim(state, 'ada', 1000)
    assert sim._wake_recover_stale(state, 1000 + sim.WAKE_CLAIM_TIMEOUT_MS) == 1
    wake = state['wakeQueue'][0]
    assert wake['id'] == wake_id
    assert wake['status'] == 'queued'
    assert wake['claimedAt'] is None


def test_trim_drops_old_done_entries_and_caps_queue():
    state = {'wakeQueue': []}
    for i in range(450):
        state['wakeQueue'].append({'id': f'done-{i}', 'status': 'done'})
    for i in range(55):
        state['wakeQueue'].append({'id': f'live-{i}', 'status': 'queued'})
    sim._wake_trim(state['wakeQueue'])
    assert len(state['wakeQueue']) == sim.WAKE_QUEUE_MAX
    live = [w for w in state['wakeQueue'] if w['status'] == 'queued']
    assert len(live) == 55
    assert len({w['id'] for w in live}) == 55


def test_restore_marker_from_queue():
    state = {'agents': {'ada': {'id': 'ada'}}}
    sim._wake_enqueue(state, 'ada', 'player_answer', {'taskId': 'task-1'}, 1000)
    assert sim._wake_restore_markers(state, 2000) == 1
    assert state['agents']['ada']['_mailAwake']['entry']['taskId'] == 'task-1'
    assert state['wakeQueue'][0]['status'] == 'claimed'
    assert sim._wake_restore_markers(state, 3000) == 0


def test_mail_action_step_completes_routed_and_busy_wakes():
    state = {'agents': {'ada': {'id': 'ada'},
                        'ben': {'id': 'ben', 'busy': True}}}
    sim._wake_enqueue(state, 'ada', 'player_answer', {'taskId': 'task-1'}, 1000)
    sim._wake_enqueue(state, 'ben', 'player_answer', {'taskId': 'task-2'}, 1000)
    state['agents']['ada']['_mailAwake'] = {'kind': 'player_answer',
                                            'entry': {'taskId': 'task-1'},
                                            'at': 1000}
    state['agents']['ben']['_mailAwake'] = {'kind': 'player_answer',
                                            'entry': {'taskId': 'task-2'},
                                            'at': 1000}
    with mock.patch.object(sim, '_queue_mail_work', return_value=True):
        routed = sim._mail_action_step(state, 0, 2000)
    assert routed == 2
    assert {w['outcome'] for w in state['wakeQueue']} == {'routed', 'acting'}
    assert all(w['status'] == 'done' for w in state['wakeQueue'])


def test_unroutable_wake_is_completed():
    state = {'agents': {'ada': {'id': 'ada',
                                '_mailAwake': {'kind': 'notice', 'entry': {},
                                               'at': 1000}}}}
    sim._wake_enqueue(state, 'ada', 'notice', {}, 1000)
    assert sim._mail_action_step(state, 0, 2000) == 0
    assert state['wakeQueue'][0]['outcome'] == 'unroutable'
    assert '_mailAwake' not in state['agents']['ada']


def test_complete_for_agent_without_match_is_false():
    assert sim._wake_complete_for_agent({}, 'ada', 'notice', {}, 'done', 1) is False


def test_restore_skips_completed_and_unknown_agents():
    state = {'wakeQueue': [{'id': 'done-1', 'status': 'done'},
                           {'id': 'wake-1', 'agentId': 'ghost',
                            'kind': 'notice', 'entry': {},
                            'status': 'queued', 'requestedAt': 1}],
             'agents': {}}
    assert sim._wake_restore_markers(state, 2000) == 0
