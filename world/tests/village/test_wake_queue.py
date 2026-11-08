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


def test_agent_mail_handoff_queues_pinned_research_card():
    state = {'workQueue': []}
    # API-path mail carries a sender prefix on the SAME line; the marker
    # must still be recognized after it.
    assert sim._queue_mail_work(
        state, 'ruth', 'agent_mail',
        {'text': 'Theo: HANDOFF: review @dankornas tweets and report.'},
        5000) is True
    assert len(state['workQueue']) == 1
    card = state['workQueue'][0]
    assert card['assignedTo'] == 'ruth'
    assert card['taskType'] == 'research'
    assert 'dankornas' in card['instructions']
    assert card['title'].startswith('HANDOFF: ')


def test_agent_mail_conversation_queues_no_card():
    state = {'workQueue': []}
    assert sim._queue_mail_work(
        state, 'ruth', 'agent_mail', {'text': 'Thanks for the review!'},
        5000) is True
    assert state['workQueue'] == []


def test_agent_mail_wake_always_files_triage_card():
    state = {'workQueue': []}
    assert sim._queue_mail_work(
        state, 'ruth', 'agent_mail',
        {'text': 'Theo: please check the gate logs tonight.',
         'wake': 'always'}, 5000) is True
    assert len(state['workQueue']) == 1
    card = state['workQueue'][0]
    assert card['assignedTo'] == 'ruth'
    assert card['taskType'] == 'research'
    assert card['title'] == 'Act on mail from Theo'
    assert 'gate logs' in card['instructions']
    assert card['_mailResume'] is True


def test_agent_mail_wake_always_dedupes_open_triage_card():
    state = {'workQueue': [
        {'title': 'Act on mail from Theo', 'assignedTo': 'ruth',
         'taskType': 'research', 'status': 'queued'}]}
    assert sim._queue_mail_work(
        state, 'ruth', 'agent_mail',
        {'text': 'Theo: second mail, same recipient.', 'wake': 'always'},
        6000) is True
    assert len(state['workQueue']) == 1


def test_agent_mail_wake_always_handoff_still_wins():
    # The HANDOFF: prefix keeps its own card shape; wake must not double-mint.
    state = {'workQueue': []}
    assert sim._queue_mail_work(
        state, 'ruth', 'agent_mail',
        {'text': 'Theo: HANDOFF: review the shard rows.',
         'wake': 'always'}, 5000) is True
    assert len(state['workQueue']) == 1
    assert state['workQueue'][0]['title'].startswith('HANDOFF: ')


def test_deliver_mail_carries_wake_into_entry():
    state = {'agents': {'ruth': {'mailbox': []}}, 'wakeQueue': []}
    assert sim._deliver_mail(
        state, 'ruth', 'agent_mail',
        {'text': 'Theo: check it.', 'wake': 'always'}) is True
    entry = state['agents']['ruth']['mailbox'][-1]
    assert entry['wake'] == 'always'


def test_complete_for_agent_without_match_is_false():
    assert sim._wake_complete_for_agent({}, 'ada', 'notice', {}, 'done', 1) is False


def test_restore_skips_completed_and_unknown_agents():
    state = {'wakeQueue': [{'id': 'done-1', 'status': 'done'},
                           {'id': 'wake-1', 'agentId': 'ghost',
                            'kind': 'notice', 'entry': {},
                            'status': 'queued', 'requestedAt': 1}],
             'agents': {}}
    assert sim._wake_restore_markers(state, 2000) == 0


def test_deliver_mail_does_not_wake_admin_for_unroutable_mail():
    # The admin copy of an escalation has no routable target. Waking the
    # admin is pointless (isAdmin is excluded from assignment) and harmful:
    # the idle-park pass skips admins, so the wake would leave an externally
    # operated chief of staff awake and wandering forever.
    roster = [{'id': 'theo', 'isAdmin': True}]
    state = {'agentRoster': roster,
             'agents': {'theo': {'mailbox': [], 'offDuty': True}},
             'wakeQueue': []}
    called = []
    orig = sim.appear_from_outskirts
    sim.appear_from_outskirts = lambda *a, **k: called.append(a)
    try:
        assert sim._deliver_mail(
            state, 'theo', 'sim_watchdog_escalated',
            {'text': 'Watchdog escalation: something is wrong.'}) is True
    finally:
        sim.appear_from_outskirts = orig
    a = state['agents']['theo']
    assert a['offDuty'] is True
    assert not a.get('_mailAwake')
    assert called == []
    assert a['mailbox'][-1]['text'].startswith('Watchdog escalation')


def test_deliver_mail_still_wakes_admin_for_routable_mail():
    roster = [{'id': 'theo', 'isAdmin': True}]
    state = {'agentRoster': roster,
             'agents': {'theo': {'mailbox': [], 'offDuty': True}},
             'wakeQueue': []}
    called = []
    orig = sim.appear_from_outskirts
    sim.appear_from_outskirts = lambda *a, **k: called.append(a)
    try:
        assert sim._deliver_mail(
            state, 'theo', 'system_notice',
            {'text': 'Card DEV-1 needs your call.', 'issueKey': 'DEV-1'}
        ) is True
    finally:
        sim.appear_from_outskirts = orig
    assert called, 'routable admin mail still wakes the admin'
    assert state['agents']['theo'].get('_mailAwake'), 'marker set'
