"""Pure, self-contained helpers extracted from sim.py.

These are the tiny utilities sim.py used to define inline -- priority
normalization, work-item due-ness, sprint/product id generation, team row
lookup, and a couple of scalar helpers. Every one is PURE: no sim module
state, no DB, only Python stdlib and their own constants. Extracting them
shrinks the sim.py monolith and makes them independently testable.

Imported back into sim.py as `from sim_helpers import ...`, so no call site
changes were needed.
"""

# Mirrors tasks.js -- copied constants, not drift-prone redefinitions.
WORK_PRIORITY = {'low': 0, 'normal': 1, 'high': 2, 'urgent': 3}
_WORK_PRIORITY_VALUES = frozenset(WORK_PRIORITY.values())


def normalize_priority(p):
    """Port of tasks.js _normalizePriority: a string name or number in WORK_PRIORITY
    -> its int; anything else -> normal."""
    if isinstance(p, (int, float)) and p in _WORK_PRIORITY_VALUES:
        return int(p)
    if isinstance(p, str):
        key = p.lower()
        if key in WORK_PRIORITY:
            return WORK_PRIORITY[key]
    return WORK_PRIORITY['normal']


# A breakdown story's size estimate (S/M/L) is an EFFORT signal, carried onto
# the queued item, the assigned task, and used as a scheduling TIEBREAK at
# equal priority (an L story starts before an S story -- it needs more
# wall-time). It never inflates urgency (an S story filed as urgent still
# wins); it only orders same-priority work.
_SIZE_WEIGHT = {'L': 3, 'M': 2, 'S': 1}


def normalize_size_estimate(size):
    """S/M/L (case-insensitive, whitespace-tolerant) -> the canonical letter;
    anything else -> None (unknown size is not a scheduling signal)."""
    if not size:
        return None
    key = str(size).strip().upper()
    return key if key in _SIZE_WEIGHT else None


def size_estimate_weight(size):
    """Ordering weight for a normalized size estimate: L=3, M=2, S=1, unknown=0."""
    return _SIZE_WEIGHT.get(normalize_size_estimate(size), 0)


def is_work_item_due(item, now_ms):
    """Port of tasks.js _isWorkItemDue."""
    return not item.get('notBefore') or now_ms >= item['notBefore']


def _sprint_item_id(item):
    # Stable-enough identity for progress derivation. Both workQueue items and
    # task dicts are mutable/unhashable, so a bare dict can't key a map; a
    # title+room pair is unambiguous within a freshly-seeded sprint and lets us
    # match across the queue AND the created task (assign_task copies title/room
    # onto the task verbatim).
    return (item.get('title') or '', item.get('room') or '')


def next_sprint_id(state):
    """Server-side monotonic sprint id (spr-1, spr-2, ...) keyed off existing
    records so a cold state starts at 1 and a hot one never collides."""
    n = 0
    for sid in (state.get('sprints') or {}):
        if isinstance(sid, str) and sid.startswith('spr-'):
            try:
                n = max(n, int(sid[4:]))
            except ValueError:
                pass
    return f'spr-{n + 1}'


def next_product_id(state):
    """Server-side monotonic product id (prd-1, prd-2, ...), same cold-start-
    at-1 / hot-never-collides shape as next_sprint_id."""
    n = 0
    for pid in (state.get('products') or {}):
        if isinstance(pid, str) and pid.startswith('prd-'):
            try:
                n = max(n, int(pid[4:]))
            except ValueError:
                pass
    return f'prd-{n + 1}'


def days_since(ts_ms, now_ms):
    """morale.js daysSince (ms -> fractional days)."""
    if not ts_ms:
        return 0
    return max(0.0, (now_ms - ts_ms) / (24 * 3600 * 1000))


def _deliverable_room(room):
    """Rooms whose real content work is gated behind peer approval. Observatory and
    Press Office do genuine research/coding; the others are placeholder chores."""
    return room in ('pressoffice', 'observatory')


def _team_row(state, team_id):
    """The team record for team_id (by `id`), or None."""
    for t in (state.get('teams') or []):
        if t.get('id') == team_id:
            return t
    return None


def ensure_wiki(state):
    return state.setdefault('wiki', {}).setdefault('pages', {})


def _is_fully_idle(a, in_room):
    """True when an agent is doing nothing: no task, movement, pair, handoff,
    or room occupancy. Non-spatial flags only -- the mirror of what the
    assignment/active paths treat as 'working'."""
    return not (a.get('busy') or a.get('task') or a.get('path') or a.get('pathActive')
                or a.get('pairWith') or a.get('handoff') or a.get('inRoom') or in_room)