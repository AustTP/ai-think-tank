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
                or a.get('pairWith') or a.get('handoff') or a.get('inRoom') or in_room
                or a.get('_suspendedTask'))


# --- Villages -----------------------------------------------------------------
# A VILLAGE is the top-level identity + memory boundary. Each agent belongs to
# exactly one village (default 'main'); wiki pages and design taste docs are
# scoped to the writer/reader's village so one village's knowledge never leaks
# into another. Mobility (workers walking between villages) and opposition come
# later -- THIS layer only proves the boundary: a village's agents only ever
# read their own wiki and their own design taste.

DEFAULT_VILLAGE = 'main'

# Memory clocks (the two-clock rule): SLOW clock (definitions, stable process,
# voice) can be remembered and trusted; FAST clock (status, balance, price,
# assignee, permission, metric) must be re-fetched live, never trusted from a
# stale copy. A wiki page carries a clock; a fast-clock page is still injected
# (so the agent knows it exists) but flagged to re-check the source.
SLOW_CLOCK = 'slow'
FAST_CLOCK = 'fast'
MEMORY_CLOCKS = (SLOW_CLOCK, FAST_CLOCK)

# Default lifetime of a shared-memory write before it must be re-verified.
# reviewAfterMs is the expiry; a page whose reviewAfterMs has passed is no
# longer injected as trusted context (the "review_after" on the memory card).
DEFAULT_REVIEW_DAYS = 30


def ensure_villages(state):
    """State's village registry (list of {id, name}), seeding the default
    'main' village on first access. Every existing agent is backfilled to
    'main' here so a pre-village database keeps working."""
    villages = state.setdefault('villages', [])
    if not any(v.get('id') == DEFAULT_VILLAGE for v in villages):
        villages.insert(0, {'id': DEFAULT_VILLAGE, 'name': 'Main Village'})
    agents = state.setdefault('agents', {})
    for a in agents.values():
        a.setdefault('villageId', DEFAULT_VILLAGE)
    return villages


def next_village_id(state):
    """Server-side monotonic village id (vlg-1, vlg-2, ...), cold-start-at-1 /
    hot-never-collides like next_sprint_id."""
    n = 0
    for v in (state.get('villages') or []):
        vid = v.get('id') or ''
        if isinstance(vid, str) and vid.startswith('vlg-'):
            try:
                n = max(n, int(vid[4:]))
            except ValueError:
                pass
    return f'vlg-{n + 1}'


def create_village(state, name):
    """Pure: add a village to the registry. Returns the record, or None on a
    name clash / blank name / duplicate id. Agents are NOT moved here -- the
    caller reassigns members explicitly."""
    name = (name or '').strip()
    if not name:
        return None
    ensure_villages(state)
    villages = state['villages']
    if any(v.get('name', '').strip().lower() == name.lower() for v in villages):
        return None
    vid = next_village_id(state)
    record = {'id': vid, 'name': name}
    villages.append(record)
    # Operator invariant (2026-11-01): a village is born WITH its chief of
    # staff -- the per-village deputy between the admin and this village's
    # senior directors. Auto-picks the village's most senior existing agent
    # (a teams-row director) when one exists; a brand-new village with no
    # directors yet stays chief-less until an operator appoints one, and the
    # health digest alerts on the gap until then.
    appoint_village_chief(state, vid)
    return record


def appoint_village_chief(state, village_id, agent_id=None):
    """Appoint `agent_id` (or auto-pick) as `village_id`'s chief of staff:
    the roster tier `villageChief: True` + `chiefOfVillage: <vid>` (core
    governance), role 'chief_of_staff', reporting to the admin, with the
    village's other senior directors re-parented under the chief. Exactly
    one chief per village: a re-appointment clears the previous chief's
    flags. Returns the chief id, or None when the village is unknown or no
    eligible candidate exists."""
    villages = state.get('villages') or []
    if not any(v.get('id') == village_id for v in villages):
        return None
    roster = state.setdefault('agentRoster', [])
    agents = state.get('agents') or {}
    entries = {r.get('id'): r for r in roster if isinstance(r, dict) and r.get('id')}
    admin_id = next((r.get('id') for r in roster
                     if isinstance(r, dict) and r.get('isAdmin')), None)

    def _village_of(aid):
        return ((agents.get(aid) or {}).get('villageId')
                or DEFAULT_VILLAGE)

    # One chief per village: strip the flags from whoever holds them now.
    previous = [e for e in entries.values()
                if e.get('villageChief')
                and (e.get('chiefOfVillage') or None) == (village_id or None)]
    for e in previous:
        e.pop('villageChief', None)
        e.pop('chiefOfVillage', None)

    chiefs_elsewhere = {e.get('id') for e in entries.values()
                        if e.get('villageChief') and e.get('chiefOfVillage')}
    candidate = None
    if agent_id:
        entry = entries.get(agent_id)
        if entry is None or agent_id in chiefs_elsewhere \
                or _village_of(agent_id) != village_id:
            return None
        candidate = agent_id
    else:
        # Auto: the village's most senior existing agent -- a teams-row
        # director (the stable senior-tier marker) who belongs to this
        # village, in teams-row order for determinism.
        senior_dirs = [t.get('directorId') for t in (state.get('teams') or [])
                       if isinstance(t, dict) and t.get('directorId')
                       and _village_of(t.get('directorId')) == village_id
                       and t.get('directorId') not in chiefs_elsewhere]
        for aid in senior_dirs:
            if aid in entries:
                candidate = aid
                break
    if not candidate:
        return None

    entry = entries[candidate]
    entry['villageChief'] = True
    entry['chiefOfVillage'] = village_id
    entry['role'] = 'chief_of_staff'
    if admin_id and candidate != admin_id:
        entry['director'] = admin_id
    # Re-parent the village's other senior directors under the chief: the
    # chief sits between the admin and this village's senior layer. Only
    # directors whose reporting pointer is the admin (or unset) move -- a
    # deeper tree keeps its own shape.
    for t in (state.get('teams') or []):
        if not isinstance(t, dict):
            continue
        did = t.get('directorId')
        de = entries.get(did)
        if not de or did == candidate or did in chiefs_elsewhere:
            continue
        if _village_of(did) == village_id \
                and de.get('director') in (None, admin_id):
            de['director'] = candidate
    return candidate


def village_of_agent(state, agent_id):
    """The villageId an agent belongs to, defaulting to 'main' for unknown or
    pre-village agents."""
    return ((state.get('agents') or {}).get(agent_id) or {}).get('villageId') or DEFAULT_VILLAGE


def set_agent_village(state, agent_id, village_id):
    """Pure: move an agent into a village. No-op (False) for unknown agents or
    an unknown village id; True when the assignment happened."""
    agent = (state.get('agents') or {}).get(agent_id)
    if not agent:
        return False
    if not any(v.get('id') == village_id for v in (state.get('villages') or [])):
        return False
    agent['villageId'] = village_id
    return True