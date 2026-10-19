import sim
from unittest.mock import patch


def test_failure_coaching_waits_for_its_own_story():
    original = {'id': 'original'}
    other = {'id': 'other'}
    state = {'tasks': {'original': original, 'other': other}}
    sim._write_growth_plan(state, 'ada', 'observatory', 'pipeline_feedback', 1,
                           'Fix the CSV', source_task_id='original')
    assert sim._coaching_note_for(state, 'ada', other) is None
    assert not state['growthPlans']['ada'][0]['applied']
    assert sim._coaching_note_for(state, 'ada', {'reviewOf': 'original'}) == 'Fix the CSV'
    assert sim._coaching_note_for(state, 'ada', {'reviewOf': 'original'}) is None


def test_legacy_failure_notes_do_not_contaminate_new_tasks():
    state = {}
    sim._write_growth_plan(state, 'ada', 'observatory', 'review_denial', 1, 'Old failure')
    sim._write_growth_plan(state, 'ada', 'observatory', 'general', 2, 'Cite your sources')
    assert sim._coaching_note_for(state, 'ada', {'id': 'new'}) == 'Cite your sources'


def test_restart_waits_for_agent_task_restoration():
    task = {'id': 'restoring', 'assignedTo': 'ada', 'status': 'working', '_contentInFlight': True}
    agent = {'busy': False, 'task': None}
    state = {'tasks': {'restoring': task}, 'agents': {'ada': agent}}
    with patch.object(sim, '_restart_recovery_done', False), \
            patch.object(sim, '_content_executor', lambda *a: None), \
            patch.object(sim, '_dispatch_content_work', return_value=True):
        assert sim._recover_content_after_restart(state) == []
        assert not sim._restart_recovery_done
        agent.update(busy=True, task='restoring')
        assert sim._recover_content_after_restart(state) == ['restoring']
        assert sim._recover_content_after_restart(state) == []
