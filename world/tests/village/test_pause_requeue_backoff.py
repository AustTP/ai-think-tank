import sim


def test_paused_requeue_backs_off():
    """A parked card must not come due the same pass it parked: the spin was
    126 assign->park cycles per 10 minutes while the free pool was
    quota-saturated (2026-11-05). First park waits 2 minutes."""
    st = {'agents': {'ada': {'id': 'ada'}}, 'workQueue': [], 'tasks': {}}
    task = {'id': 'task-1', 'title': 'Do a thing', 'room': 'pressoffice',
            'assignedTo': 'ada'}
    sim._requeue_paused_work(st, task, now_ms=1_000_000)
    item = st['workQueue'][0]
    assert item['notBefore'] >= 1_000_000 + 120_000 - 1_000, item['notBefore']
    assert item['_pauseParks'] == 1


def test_paused_requeue_backoff_escalates_and_caps():
    st = {'agents': {'ada': {'id': 'ada'}}, 'workQueue': [], 'tasks': {}}
    task = {'id': 'task-1', 'title': 'Do a thing', 'room': 'pressoffice',
            'assignedTo': 'ada', '_pauseParks': 9}
    sim._requeue_paused_work(st, task, now_ms=2_000_000)
    item = st['workQueue'][0]
    # 2 min * 10 parks would be 20 min; the ceiling is 15.
    assert item['notBefore'] == 2_000_000 + 15 * 60_000, item['notBefore']
    assert item['_pauseParks'] == 10


def test_unreachable_requeue_keeps_immediate_due():
    """The unreachable-work reissue is a different path with its own bounds
    (3 walkers then park); it must not inherit the pause backoff."""
    st = {'agents': {'ada': {'id': 'ada'}}, 'workQueue': [], 'tasks': {}}
    task = {'id': 'task-1', 'title': 'Do a thing', 'room': 'pressoffice',
            'assignedTo': 'ada'}
    parked = sim._requeue_unreachable_work(st, task, 'ada', now_ms=5_000_000)
    assert parked is False
    assert st['workQueue'][0]['notBefore'] == 5_000_000
    # the whitelist copies a zeroed counter; the unreachable path must not
    # carry a park count (it parks after 3 walkers, not by backoff)
    assert not st['workQueue'][0].get('_pauseParks')
