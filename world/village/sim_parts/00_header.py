# Server-side simulation engine. The server owns the heartbeat, spatial
# snapshot, and task cycle; browsers are read-only views of that state.
import asyncio
import datetime
import json
import math
import os
import random
import re
import threading
import time
import urllib.parse
from collections import deque

# Operator-policy binding helpers (world/operator_policy.json): shared with
# serve/content/sublow -- one declarative mechanism instead of per-module
# rebind functions.
from core.config import apply_policy_bindings, bind_policy
import core.governance as governance
import core.policies as policies

# Pure, self-contained helpers extracted to their own module to
# shrink the sim.py monolith. See sim_helpers.py.
from village.sim_helpers import (  # noqa: E402, F401
    WORK_PRIORITY,
    _deliverable_room,
    _is_fully_idle,
    _sprint_item_id,
    _team_row,
    days_since,
    ensure_wiki,
    is_work_item_due,
    # Re-exported for serve.py / tests (e.g. `from sim import next_sprint_id`).
    next_product_id,
    next_sprint_id,
    normalize_priority,
    normalize_size_estimate,
    size_estimate_weight,
    # Villages: the top-level identity + memory boundary.
    DEFAULT_VILLAGE,
    create_village,
    ensure_villages,
    next_village_id,
    set_agent_village,
    village_of_agent,
    # Memory engineering: two-clock rule + expiry.
    DEFAULT_REVIEW_DAYS,
    FAST_CLOCK,
    MEMORY_CLOCKS,
    SLOW_CLOCK,
)

# Standing-task scheduling, event-driven signal wake-ups, and
# pre-refinement comprehension -- pure modules that import
# nothing from the sim, so the top-level import cannot create
# a cycle (serve is the one module imported inline below).
import agents.comprehension as comprehension  # noqa: E402
import server.cron_scripts as cron_scripts  # noqa: E402
import village.schedule as schedule  # noqa: E402
import core.signals as signals  # noqa: E402
import village.roster as roster  # noqa: E402

# The standing-task scheduler (schedule.py) keeps per-task
# runtime state in-process: the sim owns the clock and supplies
# `now` on every _check_schedules pass. Runtime state is
# deliberately NOT persisted -- a restart re-anchors hours-mode
# tasks and forgets occurrence counts. The durable declarations
# are the state['standingTasks'] records themselves; the
# scheduler's state is only "what was running at the last
# tick". _standing_task_sweep is the wiring point.
_STANDING_SCHEDULER = schedule.StandingScheduler()

# 1.0s (was 2.0s). The server only PUBLISHES new positions ~once
# per tick, and the client renders whatever the server last published -- so the
# visible map animation is gated by SIM_TICK_S regardless of the client's 60fps
# frame loop. Halving it makes agent movement + task/status changes display
# ~2x faster on the map (the "increase display speed" ask) at the cost of 2x
# state-write + governance-loop frequency. Governance cadences (hire/fire
# cooldowns, stuck-gate + skill-review sweeps) all key off wall-time deltas
# (now_ms - lastX >= interval), so they still fire at the same REAL rhythm --
# they're just checked on a finer heartbeat.
# 2026-10-29: briefly 1.0 -> 5.0 as a stopgap while the outage was live.
# The real culprit was the RSS poller re-reading the whole state per
# headline on the event loop (fixed in d36ea86); the tick itself measured
# ~0.65s and the save ~1.5s after dropping the wasted whole-state deepcopy
# (Pass 1 no longer deep-copies; it reads only recovery fields). 1.0
# restored: same cadence the tank ran at for months, with a ~2.5s real
# cycle. The read-only archive alias merge (get_state_from_db
# archive_merge='alias') keeps per-request readers off the whole-archive
# deepcopy. Tests pin the cadence (world/tests/conftest.py).
SIM_TICK_S = 1.0
# to advance a 60px/s walker in one step (60px >> the 16px grid cell), which
# overshoots waypoints and oscillates. The browser moves per-frame (tiny dt);
# the server chunks each tick into small fixed steps so arrival/sliding resolve
# like per-frame movement. 0.1s -> 6px/step, well under TASK_ARRIVE_DIST.
SIM_SUBSTEP_S = 0.1

# Bind the real CancelledError at import time. Tests patch `sim.asyncio`
# wholesale with a Mock, so writing `except asyncio.CancelledError` inside
# _sim_loop would resolve to a Mock attribute (not an exception class) and
# raise TypeError during exception matching. This name is unaffected by that
# patch; _sim_loop must re-raise it so lifespan shutdown still works.
_SimCancelledError = asyncio.CancelledError

# Wall-clock of the last SUCCESSFUL _sim_loop_pass (set after save_state_to_db).
# The serve.py sim watchdog reads this cheap module global to detect a frozen
# tick without touching the DB. 0.0 = no tick has completed yet this process.
_LAST_TICK_WALL_EPOCH = 0.0

# Sim-loop failure event (R6 escalation ladder), stamped by serve's watchdog
# thread and consumed ONCE by the next non-dormant _sim_loop_pass (clear-on-read,
# inside the single read-modify-write -- the watchdog thread must never touch
# the DB). Shape: {'kind': 'dead'|'stale'|'dead_restart_failed'|...,
# 'detail': str, 'restarts': int, 'at': float, 'player_alert': bool}.
# None = nothing pending; a stamp that lands while DORMANT waits for the wake.
_SIM_WATCHDOG_EVENT = None

# Locate the world files (collision_grid.json, door_triggers.json) relative to
# this module -- serve.py runs from world/ so a plain relative path works, but
# tests import sim from tests/ too, so anchor on this file's directory.
_WORLD_DIR = os.path.dirname(os.path.abspath(__file__))


def _village_geometry_paths(village='main'):
    """Per-village data file names. The main village keeps the original
    collision_grid.json / door_triggers.json names; every other village uses a
    `_<village>` suffix (collision_grid_south.json, ...). Pure data selection:
    movement physics and pathfinding are untouched."""
    name = ''.join(c for c in str(village or 'main') if c.isalnum() or c in '_-') or 'main'
    suffix = '' if name == 'main' else '_' + name
    return (os.path.join(_WORLD_DIR, 'collision_grid%s.json' % suffix),
            os.path.join(_WORLD_DIR, 'door_triggers%s.json' % suffix), name)


def _load_outdoor_geometry(village='main'):
    """Load + scale one village's outdoor collision grid and door triggers
    exactly as the client does. Returns (grid, doors): grid is the raw
    collision_grid*.json {cols,rows,cell,grid}; doors is world-space
    {building:{x,y,w,h}} formed by scaling door_triggers*.json rects by SCALE
    (mirrors rooms.js). Unknown villages fall back to the main files so a stale
    village id can never crash the sim. Called once and cached -- these are
    static assets, not re-read every tick."""
    grid_path, doors_path, _name = _village_geometry_paths(village)
    if not os.path.exists(grid_path):
        grid_path, doors_path, _name = _village_geometry_paths('main')
    with open(grid_path) as f:
        grid = json.load(f)
    doors = {}
    try:
        with open(doors_path) as f:
            raw_doors = json.load(f)
        for building, r in raw_doors.items():
            doors[building] = {
                'x': r['x'] * SCALE, 'y': r['y'] * SCALE,
                'w': r['w'] * SCALE, 'h': r['h'] * SCALE,
            }
    except FileNotFoundError:
        doors = None
    return grid, doors

# Movement geometry constants -- mirrored from the client (agents.js
# AGENT_W/AGENT_H, world.js SCALE). find_path() below is a byte-for-byte
# port of tasks.js findPath(); keeping these as module attributes lets the
# pure function be fed a different geometry for testing without threading
# them through every call.
AGENT_W = 20
AGENT_H = 16
SCALE = 2
TARGET_RELAX_RADIUS = 3

# WORLD dims + spawn, mirrored from world.js (NATIVE_W/H = 688/384).
GROUND_W = 688 * SCALE
GROUND_H = 384 * SCALE
SPAWN = {'x': 300 * SCALE, 'y': 170 * SCALE}
PLACEMENT_MIN_DIST = 60

# Movement constants, mirrored from tasks.js.
TASK_WALK_SPEED = 60.0
TASK_ARRIVE_DIST = 12.0
TASK_STUCK_TIMEOUT = 1.2
# A task-walk that can't make progress is released once replanCount blows past
# this sane budget. Healthy movement resets replanCount to 0 on every step and
# emits a cancel (which clears it) within a few replans; a count above this
# means the target is unwalkable and the cancel isn't reaching a dispatcher --
# a stuck-loop (seen live: vela, replanCount 55,350, cancelling forever).
TASK_CANCEL_REPLAN_CEILING = 15


def _overlaps(a, b):
    return a['x'] < b['x'] + b['w'] and a['x'] + a['w'] > b['x'] and a['y'] < b['y'] + b['h'] and a['y'] + a['h'] > b['y']


def _cell_world_pos(gx, gy, cell):
    return {
        'x': (gx * cell + cell / 2) * SCALE - AGENT_W / 2,
        'y': (gy * cell + cell / 2) * SCALE - AGENT_H / 2,
    }


def _blocked_at(p, grid):
    """Mirror of world.js blockedAt(p): sample every grid cell the box
    overlaps, in native (= world/SCALE) coordinates. Returns True if any is
    nonzero."""
    cols, rows, cell = grid['cols'], grid['rows'], grid['cell']
    cells = grid['grid']
    nx0, ny0 = p['x'] / SCALE, p['y'] / SCALE
    nx1, ny1 = (p['x'] + p['w']) / SCALE, (p['y'] + p['h']) / SCALE
    gx0 = max(0, math.floor(nx0 / cell))
    gy0 = max(0, math.floor(ny0 / cell))
    gx1 = min(cols - 1, math.floor((nx1 - 0.001) / cell))
    gy1 = min(rows - 1, math.floor((ny1 - 0.001) / cell))
    for gy in range(gy0, gy1 + 1):
        row = cells[gy]
        for gx in range(gx0, gx1 + 1):
            if row[gx]:
                return True
    return False


def _cell_fits_agent(gx, gy, cell, grid):
    p = _cell_world_pos(gx, gy, cell)
    return not _blocked_at({'x': p['x'], 'y': p['y'], 'w': AGENT_W, 'h': AGENT_H}, grid)


# BEGIN MOVEMENT PHYSICS -- owner-protected, see selfedit._physics_refusal
def find_path(start_x, start_y, target_x, target_y, exclude_agent_id, agents, grid):
    """Python port of tasks.js findPath -- a BFS over the collision grid that
    returns a list of top-left-corner waypoints, or None when no route exists.

    `agents` mirrors the client's AGENTS dict: { id: { x, y, visible,
    pathTarget? } }. `grid` is the collision grid (from collision_grid.json):
    { cols, rows, cell, grid: [[0|1, ...], ...] }.

    This must remain byte-identical to the JS findPath it was ported from --
    every branch below maps to a documented bug in tasks.js (the
    co-located-overlap exemption, the centered-start/raw-target cell
    conversion, target ring relaxation, pathTarget claiming, 4-point
    transition sampling, the single-waypoint start==target return, and the
    out-of-bounds start/target -> None guards). Do not "improve" any of it.
    """
    cols, rows, cell = grid['cols'], grid['rows'], grid['cell']

    # "Co-located" = already overlapping the mover's own start box, not
    # bit-identical coordinates -- a live deadlock is caused by two agents a
    # fraction of a pixel apart whose 20px boxes still fully overlap.
    co_located = set()
    start_box = {'x': start_x, 'y': start_y, 'w': AGENT_W, 'h': AGENT_H}
    for aid, other in agents.items():
        if aid == exclude_agent_id:
            continue
        if other is None or not other.get('visible'):
            continue
        if other.get('x') is None or other.get('y') is None:
            continue
        if _overlaps(start_box, {'x': other['x'], 'y': other['y'], 'w': AGENT_W, 'h': AGENT_H}):
            co_located.add(aid)

    def agent_blocked_ignoring_co_located(box):
        for aid, other in agents.items():
            if aid == exclude_agent_id or aid in co_located:
                continue
            if not other or not other.get('visible'):
                continue
            if other.get('x') is None or other.get('y') is None:
                continue
            if _overlaps(box, {'x': other['x'], 'y': other['y'], 'w': AGENT_W, 'h': AGENT_H}):
                return True
        return False

    def box_free(x, y):
        return not _blocked_at({'x': x, 'y': y, 'w': AGENT_W, 'h': AGENT_H}, grid) \
            and not agent_blocked_ignoring_co_located({'x': x, 'y': y, 'w': AGENT_W, 'h': AGENT_H})

    def cell_is_free(gx, gy):
        p = _cell_world_pos(gx, gy, cell)
        return box_free(p['x'], p['y'])

    def cell_claimed_by_anothers_target(gx, gy):
        # Another agent's ALREADY-CHOSEN pathTarget claims this cell (not just
        # its current physical position) -- this is what lets two agents
        # planned in the SAME synchronous batch pick different relaxed cells
        # instead of gridlocking on the identical one. callers set
        # pathTarget AFTER find_path returns, so this only ever sees a real one.
        for oid, other in agents.items():
            if oid == exclude_agent_id:
                continue
            if not other or not other.get('visible') or not other.get('pathTarget'):
                continue
            other_target = other['pathTarget']
            ogx = math.floor((other_target['x'] / SCALE) / cell)
            ogy = math.floor((other_target['y'] / SCALE) / cell)
            if ogx == gx and ogy == gy:
                return True
        return False

    def cell_is_free_for_destination(gx, gy):
        return cell_is_free(gx, gy) and not cell_claimed_by_anothers_target(gx, gy)

    def nearest_free_cell(gx, gy, max_radius):
        if cell_is_free_for_destination(gx, gy):  # pragma: no cover -- caller only invokes when contested
            return (gx, gy)
        for r in range(1, max_radius + 1):
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:  # ring only, closest radius first
                        continue
                    ngx, ngy = gx + dx, gy + dy
                    if ngx < 0 or ngy < 0 or ngx >= cols or ngy >= rows:
                        continue
                    if cell_is_free_for_destination(ngx, ngy):
                        return (ngx, ngy)
        return None

    def transition_is_free(gx1, gy1, gx2, gy2):
        # The agent box (AGENT_W=20) is wider than one cell (16px world), so
        # validating only cell centers isn't enough -- the line BETWEEN two
        # individually-fitting centers can still clip an obstacle. Sample 4
        # points along the segment (not just the midpoint) to close that gap.
        p1 = _cell_world_pos(gx1, gy1, cell)
        p2 = _cell_world_pos(gx2, gy2, cell)
        steps = 4
        for i in range(1, steps):
            t = i / steps
            if not box_free(p1['x'] + (p2['x'] - p1['x']) * t, p1['y'] + (p2['y'] - p1['y']) * t):
                return False
        return True

    # START cell is read as the box CENTER (matching cellFitsAgent's own
    # convention) -- AGENT_W (20) doesn't evenly divide cell*SCALE (16), so
    # the naive reverse conversion is off by one cell in X for any position
    # that came from a real waypoint. TARGETS stay on the raw conversion (a
    # door-front/task coordinate is a literal point, not a re-examined box).
    start_gx = math.floor(((start_x + AGENT_W / 2) / SCALE) / cell)
    start_gy = math.floor(((start_y + AGENT_H / 2) / SCALE) / cell)
    target_gx = math.floor((target_x / SCALE) / cell)
    target_gy = math.floor((target_y / SCALE) / cell)

    if target_gx < 0 or target_gy < 0 or target_gx >= cols or target_gy >= rows:
        return None
    if start_gx < 0 or start_gy < 0 or start_gx >= cols or start_gy >= rows:
        return None

    # START cell needs only the STATIC check -- the mover is already validly
    # standing here regardless of who else is nearby; only the route AHEAD of
    # her needs to account for other agents.
    def start_cell_is_free(gx, gy):
        if not _cell_fits_agent(gx, gy, cell, grid):
            return False
        p = _cell_world_pos(gx, gy, cell)
        return not _blocked_at({'x': p['x'], 'y': p['y'], 'w': AGENT_W, 'h': AGENT_H}, grid)

    if not start_cell_is_free(start_gx, start_gy):
        return None

    # A fixed approach point in front of a door/desk is exactly one cell, so
    # the instant anyone stands on or near it the cell is contested -- relax
    # to the nearest actually-free cell within TARGET_RELAX_RADIUS instead of
    # rejecting the whole request (that rejection is what made a parked agent
    # "block the door" for everyone else, forever, including the replan retry).
    if not cell_is_free_for_destination(target_gx, target_gy):
        relaxed = nearest_free_cell(target_gx, target_gy, TARGET_RELAX_RADIUS)
        if relaxed is None:
            return None  # genuinely blocked, not just contested
        target_gx, target_gy = relaxed

    # start === target: return a single real waypoint, never a truthy-but-empty
    # array (the old `[]` crashed tickAgentMovement reading path[0].x).
    if start_gx == target_gx and start_gy == target_gy:
        return [_cell_world_pos(target_gx, target_gy, cell)]

    visited = [[False] * cols for _ in range(rows)]
    prev = [[None] * cols for _ in range(rows)]
    queue = deque([(start_gx, start_gy)])
    visited[start_gy][start_gx] = True
    found = False

    while queue and not found:
        gx, gy = queue.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = gx + dx, gy + dy
            if nx < 0 or ny < 0 or nx >= cols or ny >= rows:
                continue
            if visited[ny][nx] or not cell_is_free(nx, ny) or not transition_is_free(gx, gy, nx, ny):
                continue
            visited[ny][nx] = True
            prev[ny][nx] = (gx, gy)
            if nx == target_gx and ny == target_gy:
                found = True
                break
            queue.append((nx, ny))

    if not visited[target_gy][target_gx]:
        return None  # genuinely unreachable from here

    cells = []
    cx, cy = target_gx, target_gy
    while cx != start_gx or cy != start_gy:
        cells.append((cx, cy))
        cx, cy = prev[cy][cx]
    cells.reverse()

    # Top-left-corner convention, matching a.x/a.y and blockedAt() -- NOT the
    # cell center (returning raw centers was a real, reproducible freeze at a
    # bridge: a systematic half-agent-box offset from where positions live).
    return [_cell_world_pos(gx, gy, cell) for (gx, gy) in cells]


# ---------------------------------------------------------------------------
# Phase 2: server-owned movement -- pure-function ports of the client's
# geometry + step/slide loop. These mirror the JS ONE-FOR-ONE so the parity
# test (tests/test_movement_parity.mjs) can feed identical state to both
# engines and diff the results. Everything here is PURE: no globals, no DB,
# no serve import -- state is passed in and returned out, so the same
# functions will drive the server engine once `sim.owner` flips to 'server'.
# ---------------------------------------------------------------------------

def _is_on_door_tile(x, y, doors):
    """Mirror of agents.js isOnADoorTile: True when the 1x1 point box at
    (x,y) overlaps any ROOM_DOOR_TRIGGERS rect. `doors` is the world-space
    (already *SCALE'd) { building: {x,y,w,h} } dict -- callers load and
    scale door_triggers.json exactly as rooms.js does."""
    if not doors:
        return False
    point = {'x': x, 'y': y, 'w': 1, 'h': 1}
    for _rect in doors.values():
        if _overlaps(point, _rect):
            return True
    return False


def agent_blocked_at(box, agents, exclude_id=None, ignore_ids=None, doors=None):
    """Mirror of agents.js agentBlockedAt: boxes (_box) solid agents collide
    with, EXCEPT the mover itself, `ignore_ids` members, non-visible agents,
    and any agent standing on a door tile. `ignore_ids` is a set/iterable.
    `agents` mirrors AGENTS: { id: { x, y, visible } }."""
    ignore = set(ignore_ids) if ignore_ids else set()
    for aid, a in agents.items():
        if aid == exclude_id or aid in ignore:
            continue
        if not a or not a.get('visible'):
            continue
        if a.get('x') is None or a.get('y') is None:
            continue
        if _is_on_door_tile(a.get('x', 0), a.get('y', 0), doors):
            continue
        if _overlaps(box, {'x': a['x'], 'y': a['y'], 'w': AGENT_W, 'h': AGENT_H}):
            return True
    return False


def cell_world_pos(gx, gy, cell):
    return {
        'x': (gx * cell + cell / 2) * SCALE - AGENT_W / 2,
        'y': (gy * cell + cell / 2) * SCALE - AGENT_H / 2,
    }


def cell_fits_agent(gx, gy, cell, grid):
    """Mirror of agents.js cellFitsAgent (top-level helper; the private
    _cell_fits_agent/`_cell_world_pos` are used by find_path's BFS)."""
    p = cell_world_pos(gx, gy, cell)
    return not _blocked_at({'x': p['x'], 'y': p['y'], 'w': AGENT_W, 'h': AGENT_H}, grid)


def compute_reachable_mask(grid, spawn=None):
    """Mirror of agents.js computeReachableMask: flood-fill from the spawn
    cell over cellFitsAgent cells (full-box placement, the live-fixed rule).
    Returns a rows x cols list-of-lists of bool. A spawn cell that doesn't
    fit the agent box yields an all-false mask (JS bails the same way)."""
    spawn = spawn or SPAWN
    cols, rows, cell = grid['cols'], grid['rows'], grid['cell']
    mask = [[False] * cols for _ in range(rows)]
    start_gx = math.floor((spawn['x'] / SCALE) / cell)
    start_gy = math.floor((spawn['y'] / SCALE) / cell)
    if not cell_fits_agent(start_gx, start_gy, cell, grid):
        return mask
    stack = [(start_gx, start_gy)]
    mask[start_gy][start_gx] = True
    while stack:
        gx, gy = stack.pop()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = gx + dx, gy + dy
            if nx < 0 or ny < 0 or nx >= cols or ny >= rows:
                continue
            if mask[ny][nx] or not cell_fits_agent(nx, ny, cell, grid):
                continue
            mask[ny][nx] = True
            stack.append((nx, ny))
    return mask


def is_reachable(x, y, grid, mask):
    """Mirror of agents.js isReachable against a precomputed mask."""
    cell = grid['cell']
    gx = math.floor((x / SCALE) / cell)
    gy = math.floor((y / SCALE) / cell)
    if gy < 0 or gy >= len(mask):
        return False
    row = mask[gy]
    if gx < 0 or gx >= len(row):
        return False
    return bool(row[gx])


def pick_free_spot(grid, avoid_points=None, ground_w=GROUND_W, ground_h=GROUND_H,
                   spawn=None, placement_min_dist=PLACEMENT_MIN_DIST, rnd=random.random):
    """Mirror of agents.js pickFreeSpot: up to 300 tries at a random box not
    on static collision, reachable at its CENTER (the reverted-to convention
    -- see the JS comment), and not within PLACEMENT_MIN_DIST of an avoid
    point; else falls back to SPAWN. `rnd` is injectable so tests (and the
    parity harness) can drive a deterministic stream."""
    rnd = rnd or random.random
    avoid_points = avoid_points or []
    spawn = spawn or SPAWN
    for _ in range(300):
        x = rnd() * (ground_w - AGENT_W)
        y = rnd() * (ground_h - AGENT_H)
        if _blocked_at({'x': x, 'y': y, 'w': AGENT_W, 'h': AGENT_H}, grid):
            continue
        if not is_reachable(x + AGENT_W / 2, y + AGENT_H / 2, grid, compute_reachable_mask(grid, spawn)):
            continue
        too_close = False
        for p in avoid_points:
            if math.hypot(p['x'] - x, p['y'] - y) < placement_min_dist:
                too_close = True
                break
        if too_close:
            continue
        return {'x': x, 'y': y}
    return {'x': spawn['x'], 'y': spawn['y']}


def step_agent_movement(dt, agents, grid, doors=None, walk_speed=TASK_WALK_SPEED,
                        arrive_dist=TASK_ARRIVE_DIST, stuck_timeout=TASK_STUCK_TIMEOUT,
                        pick_free=None):
    """Pure-function port of tasks.js tickAgentMovement(dt) -- the real
    per-frame step/slide/stuck/replan/respawn loop, minus the task system.

    `agents`: { id: { x, y, visible, path, pathIndex, pathTarget,
    stuckTimer?, replanCount?, respawnedForTask?, handoff?, pairWith? } } -- MUTATED
    in place, exactly like the JS AGENTS is, so one agent's move within a tick is
    visible to the next (ordering parity). Replans via find_path; on respawn
    calls `pick_free`(avoid_points, agents,
    grid) -> fixed point, defaulting to pick_free_spot bound to this grid.

    Phase-2 boundary: ARRIVAL and CANCEL handlers are NOT this function's
    job. When an agent finishes its path (`pathIndex >= len(path)`) or gives
    up, this returns an event tuple -- it does NOT call the task system.
    Arrival events: ('arrive', kind, id) where kind is 'handoff' | 'pair' |
    'offduty' | 'task' (mirroring the JS branch). Cancel events: ('cancel',
    kind, id) with kind 'handoff' | 'offduty' | 'task'.

    Returns a list of events (possibly empty) for the caller to dispatch."""
    events = []
    if pick_free is None:
        pick_free = lambda avoid, agents_map: pick_free_spot(
            grid, avoid, spawn=SPAWN, ground_w=GROUND_W, ground_h=GROUND_H)

    for aid, a in agents.items():
        if not a.get('path') or not a.get('visible'):
            continue
        if a.get('x') is None or a.get('y') is None:
            continue
        path = a['path']
        path_index = a.get('pathIndex', 0)

        if path_index >= len(path):
            wp = None
        else:
            wp = path[path_index]
        if not wp:
            a['path'] = None
            continue

        dx = wp['x'] - a['x']
        dy = wp['y'] - a['y']
        dist = math.hypot(dx, dy)
        if dist < arrive_dist:
            # Snap exactly onto the waypoint's clean coord (the
            # float-drift-across-a-cell-boundary bug).
            a['x'] = wp['x']
            a['y'] = wp['y']
            a['pathIndex'] = path_index + 1
            a['stuckTimer'] = 0
            a['replanCount'] = 0
            if a['pathIndex'] >= len(path):
                if a.get('handoff'):
                    events.append(('arrive', 'handoff', aid))
                elif a.get('pairWith'):
                    events.append(('arrive', 'pair', aid))
                else:
                    events.append(('arrive', 'task', aid))
            continue

        step_dx = (dx / dist) * walk_speed * dt
        step_dy = (dy / dist) * walk_speed * dt
        if abs(dx) > abs(dy):
            a['dir'] = 'east' if dx > 0 else 'west'
        else:
            a['dir'] = 'south' if dy > 0 else 'north'

        try_both = {'x': a['x'] + step_dx, 'y': a['y'] + step_dy, 'w': AGENT_W, 'h': AGENT_H}
        try_x = {'x': a['x'] + step_dx, 'y': a['y'], 'w': AGENT_W, 'h': AGENT_H}
        try_y = {'x': a['x'], 'y': a['y'] + step_dy, 'w': AGENT_W, 'h': AGENT_H}
        before_x, before_y = a['x'], a['y']

        # Co-located overlap exemption: any OTHER visible agent whose box
        # overlaps the mover's own box is freed from agent-vs-agent blocking
        # -- the deadlock where two agents legitimately share a
        # door-front spot. Matched by overlap, not bit-equality.
        co_located = set()
        a_box = {'x': a['x'], 'y': a['y'], 'w': AGENT_W, 'h': AGENT_H}
        for oid, other in agents.items():
            if oid == aid:
                continue
            if not other or not other.get('visible'):
                continue
            if other.get('x') is None or other.get('y') is None:
                continue
            if _overlaps(a_box, {'x': other['x'], 'y': other['y'], 'w': AGENT_W, 'h': AGENT_H}):
                co_located.add(oid)

        if not _blocked_at(try_both, grid) and not agent_blocked_at(try_both, agents, aid, co_located, doors):
            a['x'] = try_both['x']
            a['y'] = try_both['y']
        elif not _blocked_at(try_x, grid) and not agent_blocked_at(try_x, agents, aid, co_located, doors):
            a['x'] = try_x['x']
        elif not _blocked_at(try_y, grid) and not agent_blocked_at(try_y, agents, aid, co_located, doors):
            a['y'] = try_y['y']

        moved = math.hypot(a['x'] - before_x, a['y'] - before_y) > 0.01
        if moved:
            a['stuckTimer'] = 0
            a['replanCount'] = 0
            continue

        a['stuckTimer'] = (a.get('stuckTimer') or 0) + dt
        if a['stuckTimer'] > stuck_timeout:
            a['stuckTimer'] = 0
            target = a.get('pathTarget')
            new_path = None
            if target:
                new_path = find_path(a['x'], a['y'], target['x'], target['y'], aid, agents, grid)
            a['replanCount'] = (a.get('replanCount') or 0) + 1
            if new_path and a['replanCount'] < 3:
                a['path'] = new_path
                a['pathIndex'] = 0
                continue

            if target and not a.get('respawnedForTask'):
                a['respawnedForTask'] = True
                occupied = [{'x': v['x'], 'y': v['y']} for v in agents.values() if v.get('visible') and v.get('id') != aid]
                spot = pick_free(occupied, agents)
                retry_path = find_path(spot['x'], spot['y'], target['x'], target['y'], aid, agents, grid)
                if retry_path:
                    a['x'] = spot['x']
                    a['y'] = spot['y']
                    a['path'] = retry_path
                    a['pathIndex'] = 0
                    a['replanCount'] = 0
                    continue

            if a.get('handoff'):
                events.append(('cancel', 'handoff', aid))
            else:
                events.append(('cancel', 'task', aid))

    return events


def _reconcile_stranded_agents(state, grid):
    """Heal agents stranded in a reachability gap (the map has
    occlusions that leave some outdoor cells unreachable from SPAWN, which used
    to strand agents who couldn't path to a trailhead door -- ben at the map
    edge, theo at the origin corner). A VISIBLE agent with no task,
    path, pair, or handoff that sits on a collision cell or in an unreachable
    region is re-picked to a free reachable spot (pick_free_spot's reachability
    guarantee). Bound: >40px/year sliding -- if a visible, idle agent is stuck
    on an unreachable cell this pass, it moves it ONCE. Idempotent and cheap --
    no-op when every visible idle agent is already reachable."""
    agents = state.get('agents')
    if not isinstance(agents, dict) or not agents:
        return 0
    mask = compute_reachable_mask(grid, SPAWN)
    moved = 0
    occupied = [{'x': v['x'], 'y': v['y']} for v in agents.values()
                if isinstance(v, dict) and v.get('visible')
                and v.get('x') is not None and v.get('y') is not None]
    for aid, a in agents.items():
        if not isinstance(a, dict) or not a.get('visible'):
            continue
        # Only idle wanderers -- don't touch anyone mid-task/walk/pair/handoff.
        if a.get('task') or a.get('path') or a.get('pairWith') or a.get('handoff'):
            continue
        x, y = a.get('x'), a.get('y')
        if x is None or y is None:
            stranded = True
        elif _blocked_at({'x': x, 'y': y, 'w': AGENT_W, 'h': AGENT_H}, grid):
            stranded = True
        elif not is_reachable(x + AGENT_W / 2, y + AGENT_H / 2, grid, mask):
            stranded = True
        else:
            stranded = False
        if not stranded:
            continue
        avoid = [p for p in occupied if p['x'] != x or p['y'] != y]
        spot = pick_free_spot(grid, avoid_points=avoid)
        a['x'], a['y'] = spot['x'], spot['y']
        a['stuckTimer'] = 0
        a['replanCount'] = 0
        moved += 1
    return moved


def _repair_stalled_walkers(state, grid, doors, now_ms=None):
    """An agent can be left HOLDING a 'walking' task with an EMPTY
    path (busy cleared but task kept -- e.g. a save landed between path-clear and
    arrival, or a pinned spawn raced its assign). step_agent_movement skips any
    agent with no path, so once the path is gone nothing ever re-paths her, and
    the task sits 'walking' forever while she freezes on the map -- invisible to
    _reconcile_stranded_agents (which skips task-holders). Repair: re-issue a
    fresh path to the task's room door exactly as assign_task does. Painless,
    idempotent -- no-op unless a task-holder actually lacks a path."""
    agents = state.get('agents') or {}
    tasks = state.get('tasks') or {}
    for aid, a in list(agents.items()):
        if not isinstance(a, dict):
            continue
        tid = a.get('task')
        if not tid or a.get('handoff') or a.get('pairWith'):
            continue  # no task or mid-a different interaction (pair/handoff)
        task = tasks.get(tid)
        if not task or task.get('status') != 'walking':
            continue  # not a walking task (working/done handled elsewhere)
        # A 'walking' agent hasn't arrived (she'd be busy+inRoom+status 'working'
        # if she had), so she must be ON the map to walk -- i.e. visible. A
        # stalled walker can be left coroutine-permanently 'visible=False' with
        # inRoom None (arrival marked her in-room invisible, then a reissue
        # reverted task status to 'walking'), which makes step_agent_movement
        # skip her forever. Re-affirm visibility every pass, whether or not a
        # fresh path is needed -- otherwise a walker that got a path one tick but
        # remained invisible stays frozen (movement skips pathless-only AND
        # invisible-only).
        a['visible'] = True
        # Pathological stuck: a walking-task holder whose replan counter has
        # blown past the sane budget has been re-pathing toward an unwalkable
        # target for a long time (healthy movement resets replanCount on every
        # step and a give-up cancel clears it within a few replans -- a huge
        # count means cancels are being dropped or the target can't be reached
        # at all). Release her rather than let the loop run forever; this is
        # the same give-up cancelTask() performs, called directly so a leaked
        # cancel can never wedge an agent indefinitely.
        if (a.get('replanCount') or 0) >= TASK_CANCEL_REPLAN_CEILING:
            _cancel_at_task(state, aid)
            continue
        if a.get('path'):
            continue  # already has a walk to the door -- just needed to be seen
        room = resolve_room_with_overflow(state, task.get('room'))
        door = (doors or {}).get(room)
        if not door:
            continue
        tx = door['x'] + door['w'] / 2
        ty = door['y'] + door['h'] + 4
        path = find_path(a['x'], a['y'], tx, ty, aid, agents, grid)
        if not path:
            # A fresh BFS from her spot finds no route at all -- JS's exact
            # "give up" condition ("Even a fresh spot can't reach it", reached
            # only AFTER a respawn). If she's already used her one relocation
            # and STILL can't route, release now instead of waiting for the slow
            # cancel path (stuck-timeout + 3 replans + respawn) to emit one --
            # with the dispatch wired, that path converges here anyway, just
            # slower. Without the respawn guard this would drop a task during a
            # transient pile-up (an agent momentarily boxed by neighbours), so
            # the grace is deliberate.
            if a.get('respawnedForTask'):
                _cancel_at_task(state, aid)
            continue
        a['path'] = path
        a['pathIndex'] = 0
        a['pathTarget'] = {'x': tx, 'y': ty}
        a['stuckTimer'] = 0
        a['replanCount'] = 0
    return state


# handoffs.js (attemptHandoff/arriveAtHandoff/cancelHandoff) and tasks.js
# (assignPairTask/arriveAtPair) are entirely CLIENT-owned -- no server-side
# code ever sets or resolves `.handoff`/`.pairWith`. Both mark the walking
# agent, set a path toward the other party (a door-front offset or another
# agent's position, computed client-side), and clear the field again once
# `tickAgentMovement`'s own arrival/cancel branch fires (also client-only).
# _repair_stalled_walkers explicitly skips anyone holding handoff/pairWith
# (its re-path logic only knows how to recompute a TASK's door target, not
# "walk over to agent X" or "stand next to the driver"), so a session whose
# path is lost or never set before the browser that started it closes/stalls
# has no recovery path at all -- she freezes wherever she stopped, forever,
# invisible to _reconcile_stranded_agents and _park_idle_wanderers too (both
# also skip anyone holding pairWith/handoff): two
# named agents frozen together, visible, in a doorway.
def _release_stalled_interaction(state, aid):
    """Release a VISIBLE agent holding `handoff`/`pairWith`: park her off duty
    (invisible, in place) rather than resume a conversation whose other half
    and content live only in a client that may not be open any more. Shared by
    _repair_stalled_interactions (the no-path stall sweep) and the movement
    cancel dispatch (a handoff/pair session that got stuck and cancelled).
    Mirrors cancelHandoff/arriveAtPair's release shape."""
    agents = state.get('agents') or {}
    a = agents.get(aid)
    if not isinstance(a, dict):
        return False
    a['handoff'] = None
    a['pairWith'] = None
    a['pairTaskId'] = None
    a['pathTarget'] = None
    a['stuckTimer'] = 0
    a['replanCount'] = 0
    a['respawnedForTask'] = False
    a['busy'] = False
    a['offDuty'] = True
    a['visible'] = False
    return True


def _release_pair_navigator(state, navigator_id, task_id):
    """W1: release a PAIR task's navigator once the driver's task ships. The
    navigator rides along on the driver's task (busy/inRoom at the shared desk,
    no task of her own); when the driver finishes, she must be released back to
    the pool -- pairWith/pairTaskId/busy/inRoom cleared -- or she'd sit busy at
    the desk forever. Mirrors the client's runPairProgrammingSession teardown.
    Idempotent, pure on `state`. Returns True when a navigator was released."""
    agents = state.get('agents') or {}
    n = agents.get(navigator_id)
    if not isinstance(n, dict):
        return False
    if n.get('pairTaskId') != task_id:
        return False
    n['pairWith'] = None
    n['pairTaskId'] = None
    n['busy'] = False
    n['inRoom'] = None
    n['visible'] = True
    n['x'] = (n.get('x') or 0)
    n['y'] = (n.get('y') or 0) + 20  # walks out beside the driver, not on top
    return True


def _repair_stalled_interactions(state):
    """Releases any VISIBLE agent holding `handoff`/`pairWith` with no active
    path -- she can only be in that combination if the session stalled before
    ever arriving (both fields are always set together WITH a path in the
    same synchronous block client-side, and cleared together on arrival), so
    this is never a normal transient window, only a genuinely stuck one.
    Mirrors cancelHandoff/arriveAtPair's own release shape: park her off duty
    rather than try to resume a conversation whose other half and content
    live only in a client that may not be open any more. Cheap, idempotent --
    no-op unless someone is actually stalled. Returns count released."""
    agents = state.get('agents') or {}
    released = 0
    for aid, a in agents.items():
        if not isinstance(a, dict) or not a.get('visible') or a.get('path'):
            continue
        if not a.get('handoff') and not a.get('pairWith'):
            continue
        if _release_stalled_interaction(state, aid):
            released += 1
    return released
# END MOVEMENT PHYSICS


# Fault-aware routing memory, ported from a real 2026 paper
# (StigmergyRouter, UC Berkeley/ACM CAIS): a lightweight pheromone-memory
# layer that steers multi-agent routing away from an agent whose work just
# failed, using only cheap local counters -- no re-classification, no LLM
# call, same "trust a real signal over another model call" spirit as Cut
# 4's automated pipeline gate. Deliberately SHORT-lived (a cooldown, not a
# lasting judgment -- that's the grading system's job, see
# _grade_completed_task/_room_trailing_grade): an agent whose task was
# reclaimed 5 minutes ago is worth routing around for a bit; one whose task
# was reclaimed last week is not still being penalized for it.
FAILURE_COOLDOWN_HALF_LIFE_S = 1800  # 30 minutes
# 0.5, not 1.0: a single fresh failure scores ~1.0 but starts decaying
# immediately, so a 1.0 threshold would stop treating it as "hot" within
# milliseconds (a float-epsilon false negative). 0.5 gives a fresh single
# failure a real ~one-half-life (30 min) cooldown window, matching the
# constant's own name.
FAILURE_COOLDOWN_THRESHOLD = 0.5


def record_agent_failure(state, agent_id, now_ms=None):
    """Bump agent_id's fault-memory the moment its work attempt genuinely
    failed to complete (currently: its task was orphaned/reclaimed -- see
    _reclaim_orphaned_walking_tasks). Pure on state; safe to call with a
    falsy agent_id (e.g. an orphan with no assignedTo) -- no-ops."""
    if not agent_id:
        return
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    mem = state.setdefault('agentFailureMemory', {})
    entry = mem.get(agent_id) or {'count': 0, 'lastFailedAt': 0}
    entry['count'] = entry.get('count', 0) + 1
    entry['lastFailedAt'] = now_ms
    mem[agent_id] = entry


def _agent_failure_score(state, agent_id, now_ms):
    """Exponentially-decayed fault score (half-life FAILURE_COOLDOWN_HALF_LIFE_S)
    -- a trail that isn't reinforced by a fresh failure fades on its own,
    same reinforcement/decay shape real ant pheromone trails follow. 0.0 for
    an agent with no recorded failure."""
    mem = (state.get('agentFailureMemory') or {}).get(agent_id)
    if not mem or 'lastFailedAt' not in mem:
        return 0.0
    age_s = max(0.0, (now_ms - mem['lastFailedAt']) / 1000.0)
    decay = 0.5 ** (age_s / FAILURE_COOLDOWN_HALF_LIFE_S)
    return mem.get('count', 0) * decay
