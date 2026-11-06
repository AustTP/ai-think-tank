import sim


def test_stale_task_ref_cleared_when_row_archived():
    """Bulk sweeps archive cards while the agent keeps task=<gone id>:
    busy=False means the old repair skipped her, so she was never fully idle
    (no dispersal, no parking) and jammed the room mouth forever (2026-11-06
    observatory clot: 19 stacked loiterers, 95 dead walks per 10 minutes)."""
    st = {'agents': {'dev': {'id': 'dev', 'busy': False,
                             'task': 'task-16469', 'inRoom': 'observatory',
                             'visible': True}},
          'tasks': {}, 'workQueue': []}
    sim._repair_stale_holds(st)
    a = st['agents']['dev']
    assert a.get('task') is None, a
    assert a.get('inRoom') is None


def test_live_walk_and_working_tasks_untouched():
    st = {'agents': {
        'walker': {'id': 'walker', 'busy': False, 'task': 'task-1',
                   'inRoom': None, 'visible': True},
        'worker': {'id': 'worker', 'busy': True, 'task': 'task-2',
                   'inRoom': 'observatory', 'visible': True}},
        'tasks': {'task-1': {'status': 'walking'},
                  'task-2': {'status': 'working'}},
        'workQueue': []}
    sim._repair_stale_holds(st)
    assert st['agents']['walker']['task'] == 'task-1'
    assert st['agents']['worker']['task'] == 'task-2'
    assert st['agents']['worker']['busy'] is True
