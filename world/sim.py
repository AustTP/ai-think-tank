# Server-side simulation engine -- Phase 1-rest of PLAN-server-side-simulation.md.
#
# Today the browser (index.html setInterval timers -> tasks.js) is the sole
# writer of simulation state; the server is a passive store. Each phase of
# this plan moves more of that engine server-side so the think tank runs even
# with no browser open. This module is the foundation:
#
#   - `SimEngine.tick()` reads the authoritative kv_state blob and maintains a
#     server-owned `state['sim']` section: a heartbeat (tick + wall-clock) and
#     a per-agent snapshot of the spatial/activity truth (x/y/dir/busy/task/
#     inRoom/offDuty/params). The client gets it through /api/state and must
#     treat it as read-only -- it's the server's record of "where the sim is",
#     so a browser that closes and reopens, or two browsers, converge to the
#     same server truth instead of each racing its own mutations.
#
#   - `_sim_loop()` (registered in serve.py lifespan) advances the tick every
#     SIM_TICK_S regardless of whether any client is connected.
#
#   - `/api/sim/status` lets a client or a "runs closed" probe confirm the
#     loop is alive (tick increasing with no browser attached).
#
# The client still OWNS agent movement until Phase 2; this module only
# *records* the server-observed truth and keeps the heartbeat, so it is safe
# to run alongside the current browser-driven simulation. Phase 2 flips the
# writing of x/y/dir/path onto this engine.
import asyncio
import datetime
import json
import math
import os
import random
import threading
import time
import urllib.parse
from collections import deque

# Pure, self-contained helpers extracted to their own module to
# shrink the sim.py monolith. See sim_helpers.py.
from sim_helpers import (  # noqa: E402, F401
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

# 1.0s (was 2.0s). The server only PUBLISHES new positions ~once
# per tick, and the client renders whatever the server last published -- so the
# visible map animation is gated by SIM_TICK_S regardless of the client's 60fps
# frame loop. Halving it makes agent movement + task/status changes display
# ~2x faster on the map (the "increase display speed" ask) at the cost of 2x
# state-write + governance-loop frequency. Governance cadences (hire/fire
# cooldowns, stuck-gate + skill-review sweeps) all key off wall-time deltas
# (now_ms - lastX >= interval), so they still fire at the same REAL rhythm --
# they're just checked on a finer heartbeat.
SIM_TICK_S = 1.0

# Movement sub-step for the server loop: SIM_TICK_S (now 1s) is far too coarse
# to advance a 60px/s walker in one step (60px >> the 16px grid cell), which
# overshoots waypoints and oscillates. The browser moves per-frame (tiny dt);
# the server chunks each tick into small fixed steps so arrival/sliding resolve
# like per-frame movement. 0.1s -> 6px/step, well under TASK_ARRIVE_DIST.
SIM_SUBSTEP_S = 0.1

# Locate the world files (collision_grid.json, door_triggers.json) relative to
# this module -- serve.py runs from world/ so a plain relative path works, but
# tests import sim from tests/ too, so anchor on this file's directory.
_WORLD_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_outdoor_geometry():
    """Load + scale the outdoor collision grid and door triggers exactly as the
    client does. Returns (grid, doors): grid is the raw collision_grid.json
    {cols,rows,cell,grid}; doors is world-space {building:{x,y,w,h}} formed by
    scaling door_triggers.json rects by SCALE (mirrors rooms.js:103-105). Called
    once and cached -- these are static assets, not re-read every tick."""
    with open(os.path.join(_WORLD_DIR, 'collision_grid.json')) as f:
        grid = json.load(f)
    doors = {}
    try:
        with open(os.path.join(_WORLD_DIR, 'door_triggers.json')) as f:
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
        if _overlaps(start_box, {'x': other['x'], 'y': other['y'], 'w': AGENT_W, 'h': AGENT_H}):
            co_located.add(aid)

    def agent_blocked_ignoring_co_located(box):
        for aid, other in agents.items():
            if aid == exclude_agent_id or aid in co_located:
                continue
            if not other or not other.get('visible'):
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
            if other and other.get('visible') and _overlaps(a_box, {'x': other['x'], 'y': other['y'], 'w': AGENT_W, 'h': AGENT_H}):
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
                if isinstance(v, dict) and v.get('visible')]
    for aid, a in agents.items():
        if not isinstance(a, dict) or not a.get('visible'):
            continue
        # Only idle wanderers -- don't touch anyone mid-task/walk/pair/handoff.
        if a.get('task') or a.get('path') or a.get('pairWith') or a.get('handoff'):
            continue
        x, y = a.get('x'), a.get('y')
        if x is None or y is None:
            continue
        if _blocked_at({'x': x, 'y': y, 'w': AGENT_W, 'h': AGENT_H}, grid):
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


def _reclaim_orphaned_walking_tasks(state):
    """A task left in 'walking'/'working' status whose assignee no
    longer holds it (agent.task != task_id, or the assignee vanished) will never
    resolve -- neither _task_cycle's completion loop (iterates AGENTS, so a task
    with no holder is invisible) nor _repair_stalled_walkers (guards on the agent
    holding the task) can see it. Seen live: a review-spawned fix task (task-4)
    stuck 'walking' forever while its assignee was parked idle. Nothing had
    cleared it, so the fix the review loop trusted as pending never landed.

    Reclaim: re-queue the orphan so a fresh assignment re-issues it (same
    reviewOf/assignedTo/incident pins are honored by assignment, which wakes the
    right agent on demand), then drop the dead task dict so it is not double
    counted. Idempotent, cheap, pure on `state` -- no-op when nothing is
    orphaned. Returns count reclaimed."""
    tasks = state.get('tasks')
    if not isinstance(tasks, dict) or not tasks:
        return 0
    agents = state.get('agents') or {}
    work_queue = state.setdefault('workQueue', [])
    if not isinstance(work_queue, list):
        work_queue = state['workQueue'] = []

    reclaimed = 0
    for task_id, task in list(tasks.items()):
        if not isinstance(task, dict) or task.get('status') not in ('walking', 'working'):
            continue
        # Never yank a task whose holder is at the weekly social -- the holder is
        # legitimately mid-event with its `.task` pointer temporarily detached,
        # and _resolve_social will reclaim and extend its budget on restore.
        if task.get('_inSocial'):
            continue
        # A task parked so a due scheduled item could preempt its holder is also
        # legitimately detached -- _resume_suspended_task reclaims it when the
        # scheduled item completes.
        if task.get('_suspendedForScheduled'):
            continue
        # A task is HELD when any agent points its .task at it -- matched by the
        # assignee id when present, but also by scanning, because a task can be
        # genuinely mid-work yet carry no assignedTo (e.g. a legacy/curated
        # fixture omits it). Only a task NOBODY holds is a true orphan.
        holder = agents.get(task.get('assignedTo')) if task.get('assignedTo') else None
        held = holder is not None and holder.get('task') == task_id
        if not held:
            held = any(a.get('task') == task_id for a in agents.values()
                       if isinstance(a, dict))
        if held:
            continue
        # Don't yank a task whose assignee is mid-anything (busy). A busy-hold on
        # a non-working task is a STALE-BUSY case that _task_cycle's own repair
        # clears in the same pass -- treating it as a lingering orphan here would
        # re-queue work whose holder is actively being reconciled. A true orphan
        # is one whose assignee is gone, parked, or free.
        if holder is not None and holder.get('busy'):
            continue
        # Orphaned: no agent is walking/working this. Re-queue it fresh (the
        # whitelist drops nothing assignment needs -- reviewOf/assignedTo pins a
        # re-opened fix to its author, incident to team on-call).
        # Bound the reclaim re-queue: a task that keeps getting
        # orphaned by its assignee must not be re-issued forever. Carry the
        # task's own attempt history forward and shed the item once it hits the
        # same WORK_ITEM_MAX_ATTEMPTS cap the assignment loop enforces, so a
        # genuinely un-workable task ages out instead of churning the queue.
        attempts = int(task.get('attempts') or 0) + 1
        if attempts >= WORK_ITEM_MAX_ATTEMPTS:
            from serve import log_action
            try:
                log_action(None, 'work_item_abandoned',
                           {'title': task.get('title'), 'room': task.get('room'),
                            'attempts': attempts, 'reason': 'repeatedly orphaned'}, authorized=True)
            except Exception:
                pass
            _on_work_item_abandoned(state, task, now_ms=int(time.time() * 1000))
            del tasks[task_id]
            reclaimed += 1
            continue
        work_queue.append({
            'title': task.get('title'),
            'room': task.get('room'),
            'instructions': task.get('instructions') or None,
            'pair': bool(task.get('pair')),
            'notBefore': task.get('notBefore') or None,
            'priority': normalize_priority(task.get('priority')),
            'goal': task.get('goal') or None,
            'projectLabel': task.get('projectLabel') or None,
            'research': task.get('research') or None,
            'taskType': task.get('taskType') or 'code',
            'skillReview': bool(task.get('skillReview')),
            # Bug: 'distill' was missing from
            # this whitelist -- the THIRD copy of the exact same gap already
            # fixed in _assign_due_item's `extra` dict and assign_task itself.
            # An orphaned distill-sweep task reclaimed through here lost the
            # flag on re-queue, so its re-assignment produced a fresh task
            # with distill=False, letting it back into the peer-review gate
            # and re-triggering the identical infinite reject/re-fix loop
            # (again as task-236, ~35 duplicate "Distill recent
            # think tank knowledge" queue entries). The comment above claiming
            # "the whitelist drops nothing assignment needs" was the same
            # false assumption both earlier instances of this bug shared.
            'distill': bool(task.get('distill')),
            'budgetMs': task.get('budgetMs') or None,
            'assignedTo': task.get('assignedTo') or None,
            'incident': bool(task.get('incident')),
            'productId': task.get('productId') or None,
            'reviewOf': task.get('reviewOf') or None,
            'reviewAuthorId': task.get('reviewAuthorId') or None,
            'checklist': task.get('checklist') or None,
            'pipelineStep': task.get('pipelineStep') or None,
            'lane': task.get('lane') or None,
            # Player-authored provenance survives the orphan-reclaim round trip
            # (see queue_work / _assign_due_item / assign_task) -- the JEV gate's
            # work-context bypass depends on it, so dropping it here would
            # silently downgrade a player-vetted task to agent-authored.
            'playerAuthored': bool(task.get('playerAuthored')),
            'attempts': attempts,
        })
        # Fault-aware routing memory: this agent's work attempt just failed
        # to complete -- steer the next round-robin pick away from it for a
        # bit (see _agent_failure_score / _assign_due_item).
        record_agent_failure(state, task.get('assignedTo'))
        del tasks[task_id]
        reclaimed += 1
    return reclaimed


class SimEngine:
    """Owns the server-side `sim` section of the kv_state blob.

    Pure-ish: constructed with a state dict, `tick()` mutates and returns it.
    The actual DB round-trip is done by the caller (serve.py loop) so this
    stays unit-testable without a database.
    """

    def __init__(self):
        self._last_tick_wall = 0.0
        self._last_task_wall = 0.0
        self._grid = None
        self._doors = None

    def tick(self, state, now=None):
        if not isinstance(state, dict):
            return state
        now = time.time() if now is None else now
        sim = state.setdefault('sim', {})
        sim['tick'] = int(sim.get('tick', 0)) + 1
        sim['lastTickEpochS'] = now
        sim['owner'] = sim.get('owner', 'client')  # 'server' once Phase 2 owns movement

        # Per-tick identity self-heal (see serve._heal_agent_identity): a
        # boot-only migration wasn't durable against a stale client tab's 5s
        # autosave re-POSTing pre-fix agent records (confirmed via a
        # save_state_to_db stack trace), so this runs every tick, same shape
        # as _reconcile_stranded_agents/_repair_stalled_walkers below.
        import serve
        serve._heal_agent_identity(state)

        # Phase-2 flip: when the server owns movement, this tick DRIVES agent
        # positions (mutating state['agents'] in place via the proven pure
        # port step_agent_movement), not just observes them. The snapshot below
        # then mirrors the already-advanced positions, so /api/sim/agents and
        # the client's render both see post-step truth. Until owner is set to
        # 'server', this stays an observer (client keeps driving) -- the two
        # sides agree on authority by keying off this same flag.
        if sim.get('owner', 'client') == 'server':
            if self._grid is None:
                self._grid, self._doors = _load_outdoor_geometry()
            agents_map = state.get('agents')
            if isinstance(agents_map, dict):
                # SUB-STEP the movement, don't do one 2s step. A single
                # SIM_TICK_S step moves a walker 60*2=120px -- far past a 16px
                # grid cell and bigger than TASK_ARRIVE_DIST, so she overshoots
                # her waypoint and permanently bounces (reproduced live: probe
                # oscillated 600<->718 every tick). The browser moves per-frame
                # (tiny dt) so never hit this; the coarse server tick must chunk
                # the interval into small fixed steps (6px at 0.1s) so arrival
                # and sliding resolve exactly like per-frame movement.
                events = []
                for _ in range(max(1, int(SIM_TICK_S / SIM_SUBSTEP_S))):
                    events += step_agent_movement(SIM_SUBSTEP_S, agents_map,
                                                  self._grid, doors=self._doors)
                # Phase-3 dispatch: arrival/cancel events now feed the task
                # lifecycle (the Phase-2 boundary closed). An agent arriving at
                # a task's door gets marked working (busy/inRoom/workUntil);
                # a cancel (stuck, gave up) releases the agent via _cancel_at_task
                # (or _release_stalled_interaction for a client-owned handoff/
                # pair session) so a walk that can't finish never wedges her.
                # (Off-duty is immediate and in-place now -- see
                # send_agent_off_duty -- so there is no off-duty ARRIVAL event;
                # an idle agent vanishes where she stands.)
                if events:
                    import serve
                    for kind, sub, aid in events:
                        if kind == 'arrive' and sub == 'task':
                            _arrive_at_task(state, aid, now)
                        elif kind == 'arrive' and sub == 'pair':
                            # W1: a pair session's navigator reaching the
                            # driver's door resolves here (navigator joins the
                            # driver's workstation). Previously dropped -- the
                            # navigator stood frozen beside the door forever.
                            _arrive_at_pair(state, aid, now)
                        elif kind == 'arrive' and sub == 'handoff':
                            # W1: a handoff walker reaching her recipient
                            # delivers the handoff and clocks off. Previously
                            # dropped -- the walker froze beside the recipient.
                            _arrive_at_handoff(state, aid, now)
                        elif kind == 'cancel':
                            # Dispatch the cancel instead of dropping it on the
                            # floor. A stuck task-walk that was only logged left
                            # the agent holding task/path/respawn forever: she
                            # re-walked the same unwalkable route and emitted a
                            # cancel every tick (seen live: vela, replanCount
                            # 55,350, one sim_cancel row ~every 1.5s for 36h).
                            # tasks.js calls cancelTask()/cancelHandoff() here;
                            # port that release so a gave-up agent frees her
                            # task and goes idle again. Handoff/pair sessions are
                            # client-owned, so a cancel means that client is
                            # gone -- release her the same way
                            # _repair_stalled_interactions does (park off duty).
                            if sub == 'task':
                                _cancel_at_task(state, aid)
                            elif sub in ('handoff', 'pair'):
                                _release_stalled_interaction(state, aid)
                        serve.log_action(None, 'sim_arrive' if kind == 'arrive'
                                         else 'sim_cancel', {'kind': sub, 'agent': aid},
                                         authorized=True)

                # Phase-3 task lifecycle runs IN the same pass as movement (and
                # the arrival dispatch above), NOT in a separate loop -- so both
                # share ONE read-modify-write of `state`. A second independent
                # loop (separate get_state_from_db) would read a stale snapshot
                # and its save could clobber an arrival this pass just marked,
                # permanently stranding an agent at a door whose event never
                # re-fires. Cadence mirrors the browser's 6s runTaskCycle.
                if now - self._last_task_wall >= TASK_CYCLE_S:
                    self._last_task_wall = now
                    _task_cycle(state, now=now, grid=self._grid, doors=self._doors,
                                task_id_holder=_TASK_ID_HOLDER)

                # Heal agents stranded in unreachable outdoor cells
                # (the reachability-gap bug). Runs AFTER movement + task cycle so
                # it only moves genuinely idle, visible agents -- no-op once
                # everyone is reachable.
                _reconcile_stranded_agents(state, self._grid)
                # Covers the inverse stranding: an agent HOLDING a 'walking' task
                # with no path (path cleared, arrival never fired). Movement skips
                # pathless agents, so without this they freeze mid-task forever.
                _repair_stalled_walkers(state, self._grid, self._doors, now)
                # Covers the class _repair_stalled_walkers explicitly skips: a
                # handoff/pairWith session (client-only lifecycle) stalled
                # with no path and no way to ever resume or resolve on its
                # own. Must run BEFORE the door-squatter eviction below so a
                # released agent is parked off duty (invisible) rather than
                # also getting relocated first.
                _repair_stalled_interactions(state)
        # Server-observed snapshot of every agent's spatial/activity truth.
        # Read-only for the client today; becomes the server's authoritative
        # write target in Phase 2. Fields mirror the client's saveState shape
        # so the snapshot is directly comparable to what the browser holds.
        agents = state.get('agents')
        snapshot = {}
        if isinstance(agents, dict):
            for aid, agent in agents.items():
                if not isinstance(agent, dict):
                    continue
                off_duty = bool(agent.get('offDuty'))
                # Invariant: an off-duty agent is never busy. A stale
                # busy+offDuty ghost (an abandoned firing review / assignment
                # marks busy, then the page reloads before finishTask clears it)
                # otherwise wedges ALL delegation and hiring -- the reload
                # reconcile clears busy client-side, but applyServerPositions
                # snaps busy back to the server's word every 1s poll, and
                # send_off_duty refuses to wake a busy agent, so nothing can
                # ever recover. Reporting busy=False for anyone off duty lets
                # the client persist it back and self-heals the wedge.
                snapshot[aid] = {
                    'x': agent.get('x'),
                    'y': agent.get('y'),
                    'dir': agent.get('dir'),
                    'busy': False if off_duty else bool(agent.get('busy')),
                    'task': agent.get('task'),
                    'inRoom': agent.get('inRoom'),
                    'offDuty': off_duty,
                    'pathActive': bool(agent.get('path')),
                    # This snapshot never carried
                    # `visible` at all, so applyServerPositions (sim_bridge.js)
                    # had nothing to sync it from -- a client's copy is set ONCE
                    # from the full /api/state GET at page load and then frozen
                    # for the rest of that browser session. Any agent who goes
                    # invisible afterward (entering a room, being parked off
                    # duty) keeps rendering as a "ghost" at her last known
                    # outdoor position on every client that was already open --
                    # this, not a stalled walk, is why one kept appearing
                    # outside the observatory door even while legitimately
                    # working inside it.
                    'visible': bool(agent.get('visible', True)),
                }
        sim['agents'] = snapshot
        self._last_tick_wall = now
        return state

    def status(self, state):
        sim = state.get('sim', {}) if isinstance(state, dict) else {}
        return {
            'running': bool(sim),
            'tick': sim.get('tick', 0),
            'lastTickEpochS': sim.get('lastTickEpochS'),
            'owner': sim.get('owner', 'client'),
            'ageS': (time.time() - sim.get('lastTickEpochS', 0)) if sim.get('lastTickEpochS') else None,
        }


# Module-level singleton, like serve.py's other loops.
_engine = SimEngine()


def _sim_loop_pass():
    # Returns the updated state after one tick, or None if nothing to do.
    # Runs on a worker thread (the loop calls us via asyncio.to_thread).
    import serve
    # Cheap housekeeping: drop expired external-capability handles on each tick
    # so a stale handle can never be presented. Idempotent; no-op when empty.
    serve._expire_handles()
    # Sleep-not-die gate: while DORMANT (idle auto-sleep armed), do NOT run the
    # engine tick. That single tick drives movement, the task cycle, the content
    # executors, and every model/OpenRouter spend -- skipping it is exactly what
    # "paused while sleeping" means (no bill, no churn), while the process stays
    # alive and port-bound so a remote request can wake it. The DB is left
    # untouched, so the checkpoint backup loop / _expire_handles still run.
    if serve._dormant():
        return None
    try:
        state = serve._state_begin()
        if not state:
            return None
        state = _engine.tick(state)
        # Deliver any finished drained player-ask answers INSIDE this single
        # read-modify-write (same two-phase discipline as _content_results: the
        # model call ran on its own thread and stashed an in-memory result; this
        # pass delivers it durably). No-op when nothing finished.
        try:
            serve._apply_pending_ask_results(state)
        except Exception as e:
            print(f'[sim] ask apply error: {e}', flush=True)
        # Peer reviews run INSIDE the sim's single read-modify-write -- on this
        # same `state` object, right before the one save -- so the report it files
        # can never be clobbered by a concurrent whole-blob save (the race that
        # made freshly-filed reports vanish and the old standalone peer thread
        # re-flag the same worker every 90s forever). Cadence-gated; no-op when not
        # due. serve._peer_review_tick reads only this in-hand state + the
        # action_log (read-only), then mutates state in place.
        try:
            serve._peer_review_tick(state)
        except Exception as e:
            print(f'[sim] peer review tick error: {e}', flush=True)
        # Adversarial (winter) village lifecycle: when both sides have delivered
        # the shared adversarial task, complete + disable (drain the winter team
        # back to the main village). Same in-place mutation inside the single
        # read-modify-write; no-op when disabled.
        try:
            serve._adversarial_village_tick(state)
        except Exception as e:
            print(f'[sim] adversarial village tick error: {e}', flush=True)
        serve.save_state_to_db(state)
        return state
    finally:
        serve._state_abort()


async def _sim_loop():
    while True:
        await asyncio.sleep(SIM_TICK_S)
        try:
            await asyncio.to_thread(_sim_loop_pass)
        except Exception as e:
            print(f'[sim] loop error: {e}', flush=True)
        # Drain any queued player emails OUTSIDE the tick (its own thread) so a
        # slow SMTP round-trip never stalls the think tank tick. Best-effort -- the
        # sender fail-closes and the drain clears the outbox regardless.
        try:
            await asyncio.to_thread(_drain_emails_from_db)
        except Exception as e:
            print(f'[sim] email drain error: {e}', flush=True)


def _drain_emails_from_db():
    """Load state, drain its emailOutbox via the serve sender, save. Runs on its
    own thread (not the tick) so SMTP latency doesn't block the sim. Reads the
    same authoritative DB row, so it sees whatever the tick last persisted."""
    try:
        import serve
    except Exception:
        return []
    try:
        state = serve._state_begin()
        if not state:
            return []
        results = []
        try:
            results = _drain_email_outbox_sync(state)
        except Exception as e:  # pragma: no cover
            print(f'[sim] email drain failed: {e}')
            return []
        if results:
            try:
                serve.save_state_to_db(state)
            except Exception as e:  # pragma: no cover
                print(f'[sim] email drain save failed: {e}')
        return results
    finally:
        serve._state_abort()


# ---------------------------------------------------------------------------
# Phase 3, slice 1: server-owned task lifecycle -- PLANN-phase3-task-lifecycle.md
#
# The Phase-2 flip (sim.owner='server') made the server the authoritative
# SPATIAL writer, but task ASSIGNMENT stayed browser-driven (runTaskCycle
# timer in index.html -> tasks.js). With no browser open, the persistent
# workQueue sat in the DB unattended: nothing picked items off it, nothing
# assigned agents, nothing completed tasks. This section re-homes the LIFE-
# CYCLE core -- queue consumption, deterministic assignment, arrive, complete,
# off-duty -- into pure Python functions driven by a server loop, so the
# think tank does tasks end-to-end with no browser.
#
# Scope discipline (confirmed with user): the per-room CONTENT executors
# (runWorkroomTask/runResearchTask/runMediaDigestTask/...) are the NEXT slice.
# Here, an arriving agent is marked working and, after a short simulated work
# budget, completed -- a stub, replaced by real per-room execution later.
# Assignment is DETERMINISTIC (round-robin over eligible candidates, zero JEV
# spend), matching the "idle think tank spends nothing" ethos.
#
# These are PURE / mutate-state: they take a `state` dict and return/log into
# it, no DB, no serve import -- so they're unit-testable and run on a thread
# via the serve.py wrapper, exactly like _sim_loop_pass/_peer_review_loop_pass.
# ---------------------------------------------------------------------------

# Mirrors tasks.js -- copied constants, not drift-prone redefinitions.
# WORK_PRIORITY / _WORK_PRIORITY_VALUES now live in sim_helpers.py (imported
# at the top of this module) -- see normalize_priority.
WORK_ITEM_MAX_ATTEMPTS = 3
MAX_ACTIVE_AGENTS = 25  # hiring.js:41
MAX_TOTAL_AGENTS = 500  # hiring.js:40 -- total inventory cap, vs MAX_ACTIVE_AGENTS
# Per-team headcount cap: a team may not exceed 6
# members, NOT counting the scrum master. Team membership is DERIVED from the
# `director` graph (see _derive_team_members), so this checks that derived set.
MAX_TEAM_MEMBERS = 6
# Scrum-master scaling: a scrum master is a standing
# facilitator a small team doesn't need yet -- only once a team reaches this
# many workers does a dedicated scrum master become REQUIRED. Below it, the
# team's director stands in as the groomer/facilitator for ceremonies.
SCRUM_MASTER_MIN_TEAM_SIZE = 4


def _team_member_count(state, director_id):
    """The number of non-scrum-master members currently on director_id's team,
    derived from the `director` graph. The scrum master (a standing role on the
    team's record) splits the facilitator off from the work count, per the user's
    "no larger than 6, not including the scrum master" cap."""
    members = _sim_direct_reports(state, director_id)
    # A director is not their own team member (matches _derive_team_members).
    team_scm = None
    for t in (state.get('teams') or []):
        if t.get('directorId') == director_id:
            team_scm = t.get('scrumMasterId')
            break
    out = 0
    for m in members:
        if m != team_scm:
            out += 1
    return out


def _team_under_cap(state, director_id, extra=0):
    """True if adding `extra` more members keeps the team at or under the
    MAX_TEAM_MEMBERS cap (scrum master excluded)."""
    return _team_member_count(state, director_id) + extra <= MAX_TEAM_MEMBERS
SKILL_REVIEW_CADENCE_MS = 30 * 60 * 1000

# Hive mind: how often the think tank distills recent archive findings into its
# wiki. Same cadence as skill-review -- a standing ceremony that runs only when
# the think tank actually has work (the idle gate), costing ~1 mid-tier call each.
DISTILL_CADENCE_MS = 30 * 60 * 1000

# An EXPLICIT "this ceremony is never due" marker. Cadence stamps historically
# used a far-future TEST sentinel (1e18 == SQLite "year 33658") to silence a
# ceremony from tests; that leaked into live state and the code had to GUESS
# "is this a sentinel or a real date?" by magnitude. That guess misfires on any
# genuinely far-future date. Going forward the ONLY way to say "never due" is
# this named constant; see `_cadence_due`.
CADENCE_NEVER = 9e18

# Legacy far-future TEST-sentinel threshold (1e18 stamps leaked into live
# state). Anything this far beyond now is treated as unset -- by _cadence_due
# (a leaked sentinel must not mute a standing ceremony) and by the distill
# content gate's `since` cutoff (a leaked sentinel must not gate the ceremony
# out either). See _check_schedules.
_CADENCE_LEGACY_FAR_FUTURE_MS = 10 * 365 * 24 * 3600 * 1000


def _cadence_due(state, key, cadence_ms, now_ms=None, legacy_far_future_ms=_CADENCE_LEGACY_FAR_FUTURE_MS):
    """Is a cadence ceremony (keyed by `key`) due? A stamp exactly equal to
    CADENCE_NEVER is INTENTIONALLY silent forever (explicit, not inferred). A
    legacy far-future stamp (the old 1e18 TEST macro that leaked into live
    DBs) is normalized to unset so it can't come back and mute the ceremony --
    the same self-heal guarantee `test_self_heal.py` pins, now expressed as an
    explicit migration instead of a magnitude guess. Everything else is a real
    timestamp evaluated against `cadence_ms`. `now_ms` is the caller's injected
    clock (kept for test determinism); falls back to wall clock when absent."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    last = state.get(key)
    if last is None:
        last = 0
    if last == CADENCE_NEVER:
        return False
    if legacy_far_future_ms is not None and last - now_ms > legacy_far_future_ms:
        last = 0
    return now_ms - last >= cadence_ms

# Phase 4 governance cadence + decision constants (ported from hiring.js /
# firing.js / morale.js -- copied constants, not drift-prone redefinitions).
HIRE_COOLDOWN_MS = 45000      # hiring.js:24
HIRE_DURATION_MS = 8000       # hiring.js:25 (review completion now deferral)
HIRE_COLOR_POOL = ['#f6b26b', '#76a5af', '#a4c2f4', '#d5a6bd', '#b6d7a8', '#ffe599']
FIRING_COOLDOWN_MS = 60000    # firing.js:21
FIRING_REVIEW_DURATION_MS = 10000  # deferral between start + resolution
# Personnel-strike grace window (2026-10-06): a serious/severe negative review
# counts toward the two-strike firing bar only when it lands INSIDE this window
# of the moment the evidence is weighed. A single negative review no longer
# hangs over an employee forever -- two honest mistakes spaced months apart
# never accumulate into a firing. 'minor'/'major' reports (friction, no harm,
# underperformance) never count as strikes at all.
REPORT_STRIKE_WINDOW_MS = 14 * 24 * 3600 * 1000  # 14 days
# Phase B: the staging gap between the ceremony embarking the director + the
# employees who'll work with a new hire at Command Center and the first staged
# AGENT.md draft. Long enough to read as a real Town-Hall pre-meeting, short
# enough to clear under the governance idle gate in one or two passes.
ONBOARD_MEET_DURATION_MS = 10000
# Stage-3 readiness hold: onboarding stays open until the new hire has actually
# claimed a real co-work task (a task HE holds, matching their helpFor overflow).
# An idle/empty think tank (workQueue drained) will never assign one, so the hold is
# capped -- after this much wall-time the hire is finalized anyway rather than
# stranded in limbo. 9x the ceremony; live think tanks observe readiness long before,
# offline/empty ones stop lingering within a bounded, testable window.
ONBOARD_READINESS_TIMEOUT_MS = 15 * 60 * 1000

# Weekly cross-team Knowledge Social: a 30-minute scheduled conversation in the
# Hangout (the designated empty social room behind the Town Hall) where every
# agent that PRODUCED an approved deliverable this week gets to talk about it
# with agents from other teams. Non-eligible agents (no week's work) are never
# pulled in. Attendees return exactly to their prior state when it ends: a worker
# returns to (and keeps) their in-flight task with its budget extended, an
# off-duty agent is woken for the event and returned off-duty, an idle on-duty
# one returns to idle. Cadence is event-to-event: `weekApprovals` resets when the
# event resolves, so "worked during the week" is measured between events.
SOCIAL_CADENCE_MS = 7 * 24 * 3600 * 1000      # weekly (stamped + cadence)
SOCIAL_MEET_MS = 30 * 60 * 1000               # 30-minute conversation
# W2: a Knowledge Social 'adopt' carry-away ("I will actually try/apply this")
# is only a logged tape today -- it never changes next execution. A decision of
# 'adopt' at or above this confidence lands as a coaching note routed to that
# worker's NEXT task (via the growth-plan/_coaching_note_for loop), so the
# adoption actually steers behavior instead of staying a digest line.
SOCIAL_ADOPT_CONFIDENCE = 0.6
# Backlog refinement: a weekly ceremony where the team's scrum master grooms
# the work-requests agents filed into real stories. Cadence is event-to-event
# (stamp advances when a ceremony starts), never a wall-clock ipso. `REFINEMENT
# _MEET_MS` is the convene-to-resolve gap (the scrum master reads the requests
# and the Jev refinement decision resolves what becomes a story).
REFINEMENT_CADENCE_MS = 7 * 24 * 3600 * 1000  # weekly
# Different from the Social: refinement is a DECISION, not a conversation, so
# nobody is held for a fixed slot. The scrum master grooms the team's requests
# in one synchronous pass and everyone is released on the immediately-following
# tick -- a team that finishes early leaves early, never waits out a clock. This
# value is just an upper bound / backward-compat fallback, not a required hold.
REFINEMENT_MEET_MS = 10_000                     # brief decision horizon (never a required wait)
# Cut 2 (three missing processes): deliverable grading + roadmap, the coaching
# loop (growth plan -> next task), and incident runbooks. Graded deliveries and
# runbook entries are written FIRE-AND-FORGET on task completion -- they never
# block the completion path. Coaching/runbook notes are appended to a task's
# instructions at ASSIGNMENT, never at grooming.
DELIVERABLE_GRADE_FLOOR = 5.0      # graded below this -> coaching-worthy
# A deliverable graded at/above this is a SUCCESS worth mining (the positive
# half of the rule-mining ledger -- "agents get smarter" by propagating what
# worked, not just by avoiding what failed). Mirrors serve.SUCCESS_GRADE_FLOOR.
SUCCESS_GRADE_FLOOR = 8.0
ROADMAP_CADENCE_MS = 7 * 24 * 3600 * 1000   # weekly silent priority recompute
# Rule mining: weekly silent pass that turns recurring classified draft
# failures (the review ledger) into operator rule PROPOSALS. Same shape as the
# roadmap recompute -- pure derivation, no ceremony, no Jev spend.
RULE_MINE_CADENCE_MS = 7 * 24 * 3600 * 1000   # weekly
# A runbook's most recent entry, once written for a product/room, gets pulled
# into every FUTURE incident task on that product as a "prior incident said:"
# note, so handlers learn instead of re-discovering.
RUNBOOK_MAX_ENTRIES_PER_PRODUCT = 20
# Cut 3: on-call escalation. When the on-call agent CANNOT restore
# a broken product's work -- an incident is repeatedly unassignable, or a picked
# up bug stays open past the restore window -- the escalation hands the problem
# to the owning team's scrum master, who files a backlog STORY (root cause
# scoped) or a SPIKE (root cause unknown) into the pipeline. Composes with the
# Cut-1 refinement ceremony for story creation.
RESTORE_TIMEOUT_MS = 24 * 3600 * 1000   # open bug older than this -> unrestored
ESCALATION_MEET_MS = 10_000             # brief decision horizon (never a required wait)
ESCALATION_MAX_OPEN = 3                 # cap concurrent escalations per owning team
# Stale in-flight work (SM gap 1): a NON-bug task wedged in 'walking'/'working'
# has no age alarm (bugs have RESTORE_TIMEOUT_MS). A coarse sweep re-plans it:
# re-queue once for a fast re-issue, then route the repeat offender to the owning
# team's scrum master (SM gap 1's "SM re-plan") instead of letting it wedge forever.
STALE_WORK_CADENCE_MS = 20_000          # coarse sweep cadence (like STUCK_GATE)
STALE_WORK_TIMEOUT_MS = 3 * 3600 * 1000  # 'walking' older than this -> never arrived
STALE_WORK_BUDGET_GRACE_S = 900         # 'working' still working this far past workUntil -> wedged
STALE_WORK_MAX_REPLANS = 2              # re-queues per (title,room) before the SM re-plan
# Worker stuck/help signal (W4): a worker whose content execution keeps FAILING
# (a content-executor crash, or a red-pipeline ok=False) is genuinely stuck, and
# today has no signal -- a crash is swallowed into a note. Count CONSECUTIVE
# content failures per worker; once the streak crosses this threshold, route a
# help signal to the owning team's scrum master (file_work_request +
# kick_refinement_now, the same SM-routing path as the stale-work sweep) so the
# SM re-plans or helps instead of the worker silently churning. A clean result
# resets the streak. Dovetails with SM gap 1: stale work is a wedged CARD, W4 is
# a wedged WORKER.
WORKER_HELP_AFTER_FAILS = 2
# W1: server-owned pairing/handoff. The client's assignPairTask/attemptHandoff
# own these interactions entirely; server-side, the movement engine emits
# ('arrive','pair'/'handoff') events but the task dispatch dropped them, and a
# `pair` card was assigned SOLO (no navigator ever recruited). Offsets tried
# when walking a navigator/handoff-walker up beside someone, mirrors the
# client's own "try several offsets that clear the walkable strip" loop.
PAIR_WALK_OFFSETS = [10, -10, 6, -6, 0]
# Coaching loop (W5): _write_growth_plan dedups by kind, so a worker who closes
# BELOW the floor repeatedly was coached ONCE and then silently absorbed the
# same note forever -- no re-coach, no escalation. Round-aware re-coaching:
# each low close re-lands a fresh, escalating note (repeat=True) up to
# COACHING_MAX_ROUNDS; past that the problem escalates to the owning director
# (a work-request + governance entry) so a weak worker can't just keep
# absorbing identical coaching -- either the coaching works or it surfaces.
COACHING_MAX_ROUNDS = 3
COACHING_LOOP_CADENCE_MS = 24 * 3600 * 1000  # daily catch-up sweep (like stale work)
# An on-duty agent that just completed a task in a delegatable room files a
# follow-up work-request when the room's queued/in-flight backlog has at most
# this many items left -- "we finished X, and the room is thinning out". Zero
# turns the signal off entirely (opt-in per deployment).
WORK_REQUEST_ROOM_THIN = 1
# Bell-style spare-time lane (2026-10-06): a bounded allowance of FREE SPIKES an
# agent earns per week by finishing real deliverable work. A free spike is
# self-filed, never groomed by refinement, and LOWEST priority (fills gaps,
# never blocks committed work) -- the 'spare time' Bell Labs researchers
# bootlegged. The caps are what keep it from opening the spend flood gates:
# the FIRST real deliverable completion of a week earns one free spike, capped
# per agent AND tank-wide, deterministic (no Jev), and time-boxed.
FREE_SPIKE_ALLOWANCE_PER_WEEK = 1      # free spikes one agent may earn per week
FREE_SPIKE_GLOBAL_CAP_PER_WEEK = 3     # free spikes the whole tank earns per week
FREE_SPIKE_BUDGET_MS = 2 * 60 * 1000   # time-box per free spike (2 minutes)
# Spare time is OPPORTUNISTIC, not a follow-up chore: a free spike becomes
# assignable only after a short deferral, so it fills genuine idle gaps instead
# of instantly re-arming the author the same tick a story reaches review.
FREE_SPIKE_DEFER_MS = 60 * 1000        # 1 sim-minute before a free spike is due
FREE_SPIKE_PREMISE_GUIDANCE = (
    'This is free exploration: your only deliverable is what you actually learn. '
    'QUESTION THE PREMISE first -- state the assumption buried in the question and '
    'ask whether the question is even the right one. The most valuable finding is '
    'often that the frame was wrong.'
)
# Hard cap of filed-but-not-yet-groomed requests in a single ceremony, so a
# churny think tank can't convene a backlog-refinement meeting over a runaway list.
REFINEMENT_MAX_REQUESTS = 20
# Sprint staffing: a staffable large ask is filed as a pending BREAKDOWN request
# for the receiving team's own breakdown ceremony (scrum master + workers) to
# card into stories/spikes on the next pass -- the free authority picks the team
# from the ask alone, never predicting subtasks up front. `BREAKDOWN_MEET_MS` is
# the convene-to-resolve gap (brief decision horizon, never a required wait, like
# refinement); `BREAKDOWN_MAX_REQUESTS` caps how many pending large asks one
# ceremony cards at once.
BREAKDOWN_MEET_MS = 10_000
BREAKDOWN_MAX_REQUESTS = 3
# WS-14 (shared backlog + sprint retrospectives): when every team is committed
# to an active sprint, extra large asks are broken down into stories/spikes in a
# SHARED unassigned backlog under a FEATURE; a team pulls the first eligible item
# (first-come) when a sprint closes, runs a START/STOP/CONTINUE retrospective
# (scrum master + team, director excluded), and refines its backlog at close.
RETRO_MEET_MS = 10_000           # brief decision horizon (never a required wait)
MAX_BACKLOG_ITEMS = 200          # hard cap on shared unassigned stories
BACKLOG_PULLS_PER_CLOSE = 1      # items a closing sprint pulls per team
# SM-committed `blocked` FIELD on issues. The scrum master is the
# single authority who flips issue['blocked']; the judgment ALWAYS happened
# upstream (supervisor/director Jev-pass for a set, or a deterministic
# player-response / self-resolve for an unset), so the SM's commit is MECHANICAL --
# no LLM, no meeting room, just an authoritative field flip + log. One commit per
# server pass (shared-Command-Center convention), gated so a team's SM commits its
# own team's cards and is briefly stamped as the committer.
BLOCK_CHANGE_CADENCE_MS = 5_000          # min gap between SM commits (not a wait)
# Blocked-claims awaiting a supervisor verdict (requirements-met path) are judged
# by the owning director only after this window so the gate isn't hair-trigger.
BLOCK_CLAIM_GATE_MS = 5_000
# Rooms a work-request may target = the delegatable room set (mirrors serve.py's
# _DELEGATABLE_ROOMS so sim.py stays decoupled; keep in sync if the set changes).
VALUED_QUEUE_ROOMS = frozenset(
    {'observatory', 'pressoffice', 'postoffice', 'bank', 'weatherstation', 'library', 'media'})

# Attention lanes (the spine's rule 1: lanes, not sequences). A queue item may
# ride a lane so threads coexist instead of one queue pulling attention away
# from everything else. `parking-lot` is special: the card waits with a
# bookmark and is NEVER auto-assigned -- nothing gets dropped, it just holds
# until someone promotes it. Build > open > reading at equal priority.
LANES = frozenset({'build', 'reading', 'open', 'parking-lot'})
LANE_WEIGHT = {'build': 3, 'open': 2, 'reading': 1}


def normalize_lane(lane):
    """Canonical lane or None (unknown lanes are not a scheduling signal)."""
    if not lane:
        return None
    return lane if lane in LANES else None


def _is_parked(item):
    """A parking-lot card waits with a bookmark -- never auto-assigned."""
    return (item.get('lane') or '') == 'parking-lot'


# Room keywords the charter alignment keys on (the spine's rule 2: point every
# lane at one direction). Each room's craft vocabulary -- if the charter names
# a room's keywords, that room is aligned with the spine.
_ROOM_KEYWORDS = {
    'observatory': ['research', 'science', 'study', 'explore', 'observatory'],
    'pressoffice': ['write', 'writing', 'publish', 'publishing', 'news', 'press', 'article', 'report'],
    'postoffice': ['mail', 'email', 'comms', 'communication', 'outreach', 'post'],
    'bank': ['finance', 'money', 'budget', 'bank', 'treasury', 'fund'],
    'weatherstation': ['forecast', 'weather', 'warning', 'trend', 'signal', 'predict'],
    'library': ['learn', 'learning', 'knowledge', 'book', 'library', 'note', 'teach'],
    'media': ['media', 'video', 'image', 'design', 'creative', 'audio', 'art'],
}
MORALE_APPROVED_WEIGHT = 0.5  # morale.js:16
MORALE_APPROVED_CAP = 15
MORALE_DROPPED_WEIGHT = 6
MORALE_NEGLECT_WEIGHT = 3
MORALE_NEGLECT_CAP = 30
MORALE_DROPPED_DECAY_DAYS = 14  # morale.js:33

# How long an arriving agent "works" before completing, when the server owns
# the lifecycle but the real per-room executor hasn't been ported yet (the
# accepted short-circuit of this slice). Room content execution is next.
TASK_WORK_DURATION_S = 8.0

# Phase 3 slice 2 (research executor): a dispatched content run may take longer
# than the 8s work-budget placeholder -- the JS fires the real async work and
# races it against TASK_DISPATCH_TIMEOUT_MS (the minimum visual floor never
# gates completion, only the work or that ceiling does). Mirror that: an agent
# marked working with a _content_epoch runs up to this budget before the
# completion loop gives up on the content and finalizes the fallback.
TASK_CONTENT_TIMEOUT_S = 120.0

# Server task-loop cadence -- mirrors the browser's runTaskCycle setInterval
# (index.html, 6s). Distinct from the movement tick (SIM_TICK_S=2).
TASK_CYCLE_S = 6.0

# Mailbox retention: mail items live in kv_state, so an unbounded
# mailbox makes every save rewrite a bigger blob forever. Keep the most recent
# entries per agent; anything older is dropped. Generous enough that a long
# player 1:1 thread and pending review requests survive, tight enough that the
# state blob can't grow without bound.
MAILBOX_KEEP_COUNT = 200

# ---------------------------------------------------------------------------
# Item 4: per-task model-spend ceiling. A card gets a hard budgetUsd at
# ASSIGNMENT (stamped onto the durable task). The /api/chat gate (serve.py)
# REFUSES further model calls for the task once its ledger spend crosses the
# budget; the sim loop sees the refusal (result['budgetExhausted']) and fails
# the card closed + notifies the owning director, who can re-open with more
# budget. The budget is set per story/spike during backlog refinement (the
# grooming choice also picks a band) or defaults by task type.
# ---------------------------------------------------------------------------

# USD ceiling per task TYPE (a card of a type with no entry uses the default).
# Coding-class work is the expensive lane; research/observation and media are
# cheap crawls; distill/skillReview are single-call ceremonies. Kept small
# because this is a per-card ceiling, not a daily budget.
_TASK_BUDGET_USD = {
    'code': 0.50,
    'bug': 0.50,
    'review': 0.40,
    'qa': 0.40,
    'spike': 0.30,
    'research': 0.15,
    'media': 0.15,
    'distill': 0.10,
    'skillReview': 0.10,
}
_TASK_BUDGET_USD_DEFAULT = 0.25

# Refinement bands: the groomer's choice scales the type-default budget (a
# HIGH-VALUE story accepted as 'accept_generous' gets twice the standard
# ceiling; 'standard' is 1x). An explicit per-card `budgetUsd` always wins.
_REFINEMENT_BAND_MULTIPLIER = {'standard': 1.0, 'generous': 2.0}


def budget_usd_for_task(task_type, budget_usd=None, budget_band=None):
    """The model-spend ceiling for a task, in USD. Explicit positive
    `budget_usd` wins outright (a player/director can always name a number);
    otherwise the type tier applies, and the refinement `budget_band` scales
    ONLY that tier (a generous-band story gets 2x the type default -- the band
    is a judgment about worth, never a reason to shrink an explicit budget).
    Returns a non-negative float; a card with no ceiling at all is 0.0 (gate
    off)."""
    if isinstance(budget_usd, (int, float)) and budget_usd > 0:
        return float(budget_usd)
    tier = _TASK_BUDGET_USD.get((task_type or '').strip().lower())
    base = _TASK_BUDGET_USD_DEFAULT if tier is None else tier
    mult = _REFINEMENT_BAND_MULTIPLIER.get((budget_band or 'standard').strip().lower(), 1.0)
    return max(0.0, base * mult)

# Server in-memory task-id counter (mirrors tasks.js nextTaskId). Not persisted
# -- on restart it resets; a fresh 'task-N' id is unique for a given server run,
# and the durable state['tasks'] mirror keyed by it is authoritative.
_TASK_ID_HOLDER = [0]

# Phase 3 slice 2: completed content-executor results, keyed by task id. A real
# per-room executor (research for now) runs its NETWORK work on a background
# thread -- it must never write the DB directly (a second writer would clobber
# the movement/arrival pass's in-flight state, the race slice 1's single-pass
# design exists to prevent). Instead it stashes its result here
# ({'note': str, 'seenUrls': [...]}) and the NEXT _task_cycle pass, which owns
# the single read-modify-write, merges it into the durable state and finalizes.
# In-memory like _TASK_ID_HOLDER: a restart mid-content abandons in-flight work
# (same as the browser's in-memory TASKS on page reload).
_content_results = {}
_content_results_lock = threading.Lock()


def _store_content_result(task_id, result):
    with _content_results_lock:
        _content_results[task_id] = dict(result)


# Bounded review-cycle escalation: a promoted
# follow-up story cycled through review->fix->review 45+ times in
# under 20 minutes before settling on its own, with no bound at all. Two
# SEPARATE mechanisms were each re-entering the gate with no shared cap: a
# genuine 'actionable' rejection (_apply_content_result below) and
# _sweep_stuck_gates's own "review vanished with no verdict" rescue -- both
# re-queue another review/fix round indefinitely. One shared counter on the
# gate, checked from both places, bounds the total regardless of WHICH
# mechanism keeps re-triggering it.
MAX_REVIEW_CYCLES = 8


def _maybe_escalate_stuck_gate(parent, gate, reason):
    """Bump the gate's cycle counter; once it crosses MAX_REVIEW_CYCLES,
    freeze the gate (mark it escalated) and notify the player instead of
    continuing to loop. Returns True if the gate is (now, or already)
    frozen -- the caller must skip its normal re-queue action in that case.
    Idempotent: only escalates once per gate (a frozen gate stays frozen;
    it does not re-escalate every subsequent cycle)."""
    if gate.get('escalated'):
        return True
    gate['cycleCount'] = gate.get('cycleCount', 0) + 1
    if gate['cycleCount'] < MAX_REVIEW_CYCLES:
        return False
    gate['escalated'] = True
    title = parent.get('title') or 'a story'
    try:
        from serve import create_escalation
        create_escalation(
            'stuck review loop',
            f'"{title}" has gone through {gate["cycleCount"]} review cycles without closing ({reason}). '
            'Automatic review/fix cycling has been paused for this story -- it needs manual attention '
            '(reject it outright, edit its scope, or otherwise intervene) rather than continuing to loop.',
        )
    except Exception:
        pass
    return True


def _resolve_worker_issue_team(state, wish, task):
    """W3: the owning team RECORD (matched by its `id`) for a spike-filed issue
    wish, or None. `file_issue` requires the team `id` and _team_row only ever
    matches that key, but teams are keyed two ways in the codebase (by `id` and
    by `directorId` -- see _refinement_scrum_master_for_team). Candidate chain:
    the wish's teamId -> the task's teamId -> the task's productId (via
    _product_director) -> the worker's own roster `director` pointer (serve.py
    sets it at roster build; _sim_direct_reports reads it). Each candidate is
    matched against the team list by id OR directorId. Returns the team dict
    (its `id` is what file_issue wants), or None."""
    candidates = []
    if wish.get('teamId'):
        candidates.append(wish['teamId'])
    if task.get('teamId'):
        candidates.append(task['teamId'])
    if task.get('productId'):
        director = _product_director(state, task.get('productId'))
        if director:
            candidates.append(director)
    roster = next((d for d in (state.get('agentRoster') or [])
                   if d.get('id') == task.get('assignedTo')), None)
    if roster and roster.get('director'):
        candidates.append(roster['director'])
    for cand in candidates:
        if not cand:  # pragma: no cover -- all candidates are pre-filtered truthy
            continue
        team = _team_row(state, cand)
        if team:
            return team
        team = next((t for t in (state.get('teams') or [])
                     if t.get('directorId') == cand), None)
        if team:
            return team
    return None


def _file_spike_issue(state, wish, task, now_ms):
    """W3: file the issue a spike worker proposed, against the LIVE state inside
    the tick's single read-modify-write. The executor thread only ever reported
    a WISH (it runs against a snapshot); THIS is where the real filing happens.
    Resolves the owning team from the wish (or the task), dedups against an
    already-open issue by the same reporter + same summary, files via the same
    file_issue the player/telegram endpoints use (which also appends the
    backlogRequests record + kicks refinement now), and logs governance. Fail
    safe to no-filing on any inconsistency (unknown team, malformed wish,
    duplicate) -- a worker's stray idea must never wedge the backlog."""
    reporter = task.get('assignedTo')
    issue_type = (wish.get('issueType') or '').strip().lower()
    summary = (wish.get('summary') or '').strip()
    feature = (wish.get('feature') or '').strip()
    if not reporter or issue_type not in ISSUE_TYPES or not summary or not feature:
        return False
    team = _resolve_worker_issue_team(state, wish, task)
    if not team:
        return False
    team_id = team.get('id')
    # Dedup: don't re-file the same gap every time its spike re-runs. An issue
    # by the same reporter with the same summary that has NOT reached a
    # terminal state is the same finding.
    for issue in (state.get('issues') or {}).values():
        if (issue.get('reporterId') == reporter
                and issue.get('status') not in ('done', 'closed')
                and (issue.get('summary') or '') == summary):
            return False
    issue = file_issue(state, team_id, issue_type, summary, feature, reporter,
                       description=wish.get('description') or '',
                       title=wish.get('title') or None,
                       now_ms=now_ms)
    if not issue:
        return False
    _log_governance(state, reporter, 'worker_filed_issue',
                    {'issue': issue.get('key'), 'team': team_id,
                     'type': issue_type, 'summary': summary[:120],
                     'fromTask': task.get('id')})
    return True


def _peer_coding_class(parent):
    """Item 7: is this card a CODING-CLASS parent -- the lane whose done gate
    needs objective evidence that the quality pipeline really ran on the code?
    A pressoffice story (the workroom delivers code there) or any product-backed
    card (has a productId) counts. Pure-research lanes (observatory, etc.) are
    NOT coding-class: their clean vote rests on pipelineOk alone (they don't run
    the flake8/mypy/bandit/pytest-cov stack the way a code review does)."""
    if not isinstance(parent, dict):
        return False
    return parent.get('room') == 'pressoffice' or bool(parent.get('productId'))


def _apply_content_result(state, task, result, now_ms=None):
    """Merge a completed content-executor result into the durable state, inside
    the task_cycle's single read-modify-write. Writes the research topic's
    grown seenUrls back (so a future run dedups against it) and records the
    agent's real note (mirrors runResearchTask's notes.push). Mutates `state`;
    the note is attached to the task record so the board/UI can surface it.
    `now_ms` is threaded from the caller (the tick already has it); a caller
    that omits it falls back to wall-clock so the function stays standalone."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    note = result.get('note')
    if note:
        task['note'] = note
    # Gap: a spike's FULL findings only ever
    # lived in the Library file -- task.note is deliberately kept lean (a
    # short pointer, "see the Library entry just filed"), by design (see the
    # data-minimization comment in content.py's spike executor). But
    # /api/intent/spike/{id}/promote (turning a spike into a real followup
    # story) only ever had task.note to work with, so promoting a spike
    # handed the new story's author a vague pointer, never the actual
    # research (source lists, CSVs, feasibility data). Recording the exact
    # path here (not a task-id glob -- task ids get REUSED across unrelated
    # investigations, confirmed, so a glob could match the wrong
    # finding) lets promotion pull the real content forward.
    library_path = result.get('libraryPath')
    if library_path:
        task['libraryPath'] = library_path
    # Merge grown seenUrls back into the topic (dedup is stored per-topic).
    seen = result.get('seenUrls')
    if seen:
        for t in (state.get('researchTopics') or []):
            if t.get('id') == (task.get('research') or {}).get('topicId'):
                existing = t.setdefault('seenUrls', [])
                for u in seen:
                    if u not in existing:
                        existing.append(u)
                break
    # Mirror runResearchTask's notes.push onto the agent's profile.
    aid = task.get('assignedTo')
    if aid and note:
        a = (state.get('agents') or {}).get(aid)
        if a and isinstance(a.get('profile'), dict):
            notes = a['profile'].setdefault('notes', [])
            notes.append(note)
            if len(notes) > 5:
                del notes[:-5]
    # Item 2: feedback injection. A content executor reports `feedback` when its
    # run hit a block or failure (a blocked command, a red pipeline -- see
    # content.py's coding executor); it rides the result channel into the tick's
    # single read-modify-write here, where `state` is live, so it lands in the
    # author's feedback buffer (_build_agent_perception renders it on the next
    # dispatch). Never written from the executor thread itself, which only has a
    # snapshot.
    fb = result.get('feedback')
    if fb and aid:
        if isinstance(fb, str):
            _append_feedback(state, aid, fb, source='executor')
        elif isinstance(fb, dict):
            _append_feedback(state, aid, fb.get('text') or '', source=fb.get('source') or 'executor')
    # Phase 3 pressoffice: a review/QA pass that found real problems enqueues a
    # follow-up fix task (mirrors runReviewTask's queueWork on an 'actionable'
    # Jev verdict). Must go through the tick's single read-modify-write, not a
    # direct DB write, so queue it here from the executor's result.
    qf = result.get('queueFix')
    if qf:
        # A gate review that must go BACK to the same author + the same gate
        # carries the parent id; a free-form pressoffice fix does not.
        queue_work(state, [qf])
    # W3: a worker's spike can PROPOSE an issue (a Jev verdict that the spike's
    # finding names a real, actionable gap). The executor reports a WISH -- the
    # same safe indirection as queueFix/notifyPlayer: it runs off-thread against
    # a read-only snapshot, so it must never call file_issue directly. Filing
    # happens HERE, inside the tick's single read-modify-write, where `state`
    # is live and the owning team is resolved fresh (wish teamId -> task teamId
    # -> product director -> roster director, each matched by id OR directorId).
    wish = result.get('fileIssue')
    if wish:
        _file_spike_issue(state, wish, task, now_ms)
    # A content executor (currently just spikes)
    # can ask to notify the player when it lands, same safe indirection as
    # queueFix above -- executors run off-thread with only a read-oriented
    # snapshot, so they report the WISH here rather than calling
    # _queue_player_email directly; this runs inside the tick's real
    # read-modify-write, where state is actually the live, mutable object.
    notify = result.get('notifyPlayer')
    if notify and notify.get('kind') and notify.get('subject') and notify.get('body'):
        _queue_player_email(state, notify['kind'], notify['subject'], notify['body'])
    # Peer-approval gate (Phase E addendum): when the completing task is a
    # review subtask carrying a `reviewOf` pointer, fold its Jev verdict into
    # the PARENT task's gate -- clean = +1 approval (distinct reviewers only),
    # actionable = reset the gate + requeue a fix to the author. The parent is
    # never finished here; _task_cycle decides close (it needs `now`).
    if task.get('reviewOf'):
        parent = _resolve_review_parent(state, task)
        if parent is not None:
            verdict = result.get('peerVerdict')
            reviewer = task.get('assignedTo')
            gate = parent['_peerGate']
            # Item 6: fold the reviewer's structured verdict onto the parent so
            # the author and player see WHY the vote went the way it did, not
            # just the binary -- the summary, the checks actually verified, and
            # the remaining risks. Best-effort; a missing structured block
            # changes nothing.
            if result.get('peerSummary') or result.get('peerChecks') or result.get('peerRisks'):
                parent.setdefault('peerReviews', []).append({
                    'reviewer': reviewer,
                    'verdict': verdict,
                    'summary': result.get('peerSummary') or '',
                    'checks': list(result.get('peerChecks') or []),
                    'risks': list(result.get('peerRisks') or []),
                    'atMs': now_ms,
                })
            if verdict == 'actionable':
                if not _maybe_escalate_stuck_gate(parent, gate, 'repeated rejections'):
                    gate['approvals'] = 0
                    gate['approvers'] = []
                    _sim_notify_author(state, parent, reviewer, rationale=task.get('note'))
            elif verdict == 'clean':
                # Cut 4 hard gate: a review that finds the work clean can only
                # count as an APPROVAL if its quality pipeline objectively
                # passed (flake8/mypy/bandit/pytest-cov all green). A reviewer's
                # "looks solid" cannot approve code that fails the standard -- so
                # a clean verdict with a red or missing pipeline is downgraded to
                # the actionable path (reset the gate + send it back to the
                # author) exactly as if the reviewer had rejected it.
                if not result.get('pipelineOk'):
                    verdict = 'actionable'
                # Item 7 evidence-based done gate: for a CODING-CLASS parent (a
                # pressoffice story or any product-backed card), a clean vote also
                # needs OBJECTIVE evidence that the quality pipeline actually ran
                # and exercised the code -- the executor files the real per-step
                # output (coverage line, flake8/mypy/bandit tails) in
                # result['evidence']. A 'looks solid' with no evidence cannot
                # approve code any more than a red pipeline can; without this a
                # hollow clean vote would close a card that was never really
                # tested. Non-coding lanes (observatory etc.) stay on the
                # pipelineOk check alone -- deliberately: they can
                # legitimately conclude with no deliverable (see
                # library/design-references/stop-condition.md).
                elif _peer_coding_class(parent) and not (result.get('evidence') or '').strip():
                    verdict = 'actionable'
                if verdict == 'actionable':
                    if not _maybe_escalate_stuck_gate(parent, gate, 'repeated red-pipeline rejections'):
                        gate['approvals'] = 0
                        gate['approvers'] = []
                        _sim_notify_author(state, parent, reviewer, rationale=task.get('note'))
                elif reviewer and reviewer not in gate['approvers']:
                    gate['approvers'].append(reviewer)
                    gate['approvals'] += 1


def _take_content_result(task_id):
    with _content_results_lock:
        return _content_results.pop(task_id, None)


# Item 2: per-agent feedback buffer -- the durable 'what came back at me'
# channel _build_agent_perception renders into the next dispatch's context.
# Capped so it stays a recent-memory window, not a ledger.
_FEEDBACK_MAX = 10


def _append_feedback(state, agent_id, text, source=None):
    """Record one feedback entry for `agent_id` (a rejection rationale, a
    pipeline failure, or an executor-reported block). Pure state mutation;
    JSON-safe (a list of small dicts). Written ONLY inside the tick's single
    read-modify-write (or a pure helper it calls) -- never from an executor
    thread, which has only a snapshot."""
    text = (text or '').strip()
    if not agent_id or not text:
        return
    bucket = state.setdefault('_feedback', {}).setdefault(agent_id, [])
    entry = {'text': text[:500]}
    if source:
        entry['source'] = source
    bucket.append(entry)
    if len(bucket) > _FEEDBACK_MAX:
        del bucket[:len(bucket) - _FEEDBACK_MAX]


def _acquaintance_set(state, agent_id):
    """The ids `agent_id` is acquainted with. Stored as JSON-safe lists (never
    sets), so `_acquaintances` survives the kv_state blob round trip."""
    return (state.get('_acquaintances') or {}).get(agent_id) or []


def _mark_acquaintance(state, a, b):
    """Record that `a` and `b` met, symmetrically (co-location at a social
    ceremony is the marker -- see the social sweep). A self-introduction or a
    missing id is a no-op. Pure state mutation; item 8's gate."""
    if not a or not b or a == b:
        return
    acq = state.setdefault('_acquaintances', {})
    for x, y in ((a, b), (b, a)):
        lst = acq.setdefault(x, [])
        if y not in lst:
            lst.append(y)


def _are_acquainted(state, a, b):
    """True when `a` and `b` have been co-located at a social ceremony. Nobody
    is a stranger to themselves."""
    if not a or not b:
        return False
    if a == b:
        return True
    return b in _acquaintance_set(state, a)


def _describe_agent(state, agent_id, viewer_id, fallback_name=None):
    """How `viewer_id` should refer to `agent_id`: by name once they are
    acquainted, otherwise by role ('the Research analyst') -- a stranger's name
    is not a name the viewer can honestly use. Falls back to the raw id."""
    roster = {d.get('id'): d for d in (state.get('agentRoster') or [])
              if isinstance(d, dict)}
    rec = roster.get(agent_id) or {}
    name = rec.get('name') or fallback_name or agent_id
    role = rec.get('role') or ''
    if _are_acquainted(state, viewer_id, agent_id):
        return name + (f' (the {role})' if role else '')
    if role:
        return f'the {role}'
    return 'a colleague you have not met'


def _build_agent_perception(state, agent_id, task=None):
    """Compose the agent's situational awareness into a compact markdown block
    (base_ctx) folded into the content executor's system prompt: who am I, where
    am I, who's around me, what am I working on, and what feedback came back at
    me -- so a worker reasons like a teammate walking into the room, not a
    stateless API call. Pure read of `state` (a frozen snapshot in production,
    so no race with the sync pass); returns '' when nothing coherent can be
    said, and executors treat a falsy base_ctx as 'no perception' so their
    existing unit-test prompts stay byte-identical."""
    agents = state.get('agents') or {}
    roster = {d.get('id'): d for d in (state.get('agentRoster') or [])
              if isinstance(d, dict)}
    me = agents.get(agent_id) or {}
    rec = roster.get(agent_id) or {}
    parts = []
    identity = f'You are {rec.get("name") or me.get("name") or agent_id}.'
    role = rec.get('role') or ''
    if role:
        identity += f' Your role on the team: {role}.'
    parts.append(identity)
    room = me.get('inRoom') or (task or {}).get('room') or ''
    if room:
        parts.append(f'You are currently in the {room}.')
    present = [a for a in agents.values()
               if a and a.get('id') != agent_id and a.get('inRoom') == room
               and not a.get('offDuty')]
    if present:
        who = ', '.join(_describe_agent(state, a['id'], agent_id)
                        for a in present[:8])
        parts.append(f'People in the room with you: {who}.')
    if task:
        title = (task.get('title') or '').strip()
        if title:
            line = f'Your current task: {title}'
            instructions = task.get('instructions')
            if instructions:
                line += f' -- {instructions}'
            parts.append(line + '.')
        ac = task.get('acceptanceCriteria')
        if ac:
            parts.append(f'Acceptance criteria: {ac}')
        if task.get('reviewOf'):
            parts.append(f'This is a peer-review of task {task["reviewOf"]} '
                         f'-- your verdict counts on the parent story.')
    feedback = (state.get('_feedback') or {}).get(agent_id) or []
    if feedback:
        lines = []
        for f in feedback[-5:]:
            if isinstance(f, dict):
                text = f.get('text') or f.get('guidance') or ''
            else:
                text = str(f)
            if text:
                lines.append(f'- {text}')
        if lines:
            parts.append('Feedback on your recent work:\n' + '\n'.join(lines))
    return '\n\n'.join(p for p in parts if p)


# The content executor for the CURRENT task, set by serve.py so sim.py stays
# pure/unit-testable (no network, no serve import). Signature:
#   executor(state_snapshot, agent_id, task, topic, base_ctx) -> None
# It runs its network work (browse/chat/library), then calls
# _store_content_result(task_id, {'note': str, 'seenUrls': [...]}). None =
# no real content executor ported for this room -> the workUntil fallback.
_content_executor = None


def _dispatch_content_work(executor, state, agent_id, task, now):
    """Fire the registered content executor on a background thread for this
    arriving agent's task, if one applies. Returns True if real content work was
    dispatched (so the task gets the long content timeout instead of the workUntil
    placeholder), else False (slice-1 short-circuit). The executor runs against a
    snapshot copy of `state` so its long network calls never race our sync pass;
    results are merged via _content_results by the next _task_cycle.

    Room-agnostic: `executor` is a router (serve.py provides it) who decides by
    task.room/task flags which real work applies and stores the result itself.
    The generic long budget is installed HERE; an executor that needs a
    different ceiling shortens task['workUntil'] in its snapshot. A timeout here
    is a hard floor, not special-cased per room."""
    task_id = task.get('id')
    if not task_id or not executor:
        return False
    # Give the content run a generous ceiling; mark the task so the completion
    # loop knows real work is in flight (not just the placeholder).
    task['workUntil'] = now + TASK_CONTENT_TIMEOUT_S
    task['_contentInFlight'] = True
    snapshot = json.loads(json.dumps(state))
    # Item 1: situational awareness. Build the perception block here, on the
    # main thread, from the FROZEN snapshot + the live task dict -- never inside
    # the worker thread, where a read of the still-mutating live `state` would
    # race the sync pass. Falsy (an empty perception) keeps the executor's
    # pre-existing prompt exactly.
    perception = _build_agent_perception(snapshot, agent_id, task)

    def _run():
        try:
            executor(snapshot, agent_id, task, base_ctx=perception)
        except Exception as e:  # never let a content failure strand the agent
            # W4: a crashed content run is a FAILURE, not a silent success.
            # Mark ok=False so the fail-closed quality gate treats it like a red
            # pipeline (card sent back / fix re-issued), and the worker-stuck
            # help signal can see it -- a crash must never complete as 'done'.
            _store_content_result(task_id, {'ok': False, 'note': f'Content execution failed: {e}', 'seenUrls': (task.get('research') or {}).get('seenUrls') or []})

    threading.Thread(target=_run, daemon=True).start()
    return True


def room_desk_capacity(collision):
    """Port of tasks.js _roomDeskCapacity: number of interactables for a room's
    collision layout. Only counts feed overflow resolution (assignTask), so the
    exact counts are what matter, not the desk geometry. Mirrors the actual
    ROOM_COLLISIONS array lengths in rooms.js/terminals.js. Unknown layout ->
    no invented limit (Infinity), same as the JS."""
    counts = {
        'workstations': 6,   # ROOM_COLLISIONS.workstations
        'library': 6,        # ROOM_COLLISIONS.library
        'bank': 3,           # ROOM_COLLISIONS.bank
        'postoffice': 1,     # ROOM_COLLISIONS.postoffice
    }
    return counts.get(collision, float('inf'))


# ROOMS collision key per building -- mirrors tasks.js reach into rooms.js.
_ROOM_COLLISION = {
    'pressoffice': 'workstations', 'media': 'workstations',
    'weatherstation': 'workstations', 'observatory': 'workstations',
    'library': 'library', 'bank': 'bank', 'postoffice': 'postoffice',
}
# The single overflow pair -- tasks.js ROOM_OVERFLOW_TARGET: Work Room overflows
# into Research Center when the Work Room's six desks are full.
_ROOM_OVERFLOW_TARGET = {'pressoffice': 'observatory'}


def room_occupancy(state, room):
    """Port of tasks.js _roomOccupancy: how many busy agents are in this room."""
    n = 0
    for a in (state.get('agents') or {}).values():
        if a and a.get('busy') and a.get('inRoom') == room:
            n += 1
    return n


def resolve_room_with_overflow(state, room):
    """Port of tasks.js _resolveRoomWithOverflow: redirect to the overflow target
    only when THIS room is full AND the target has a free desk; otherwise stays.
    Idempotent."""
    overflow_to = _ROOM_OVERFLOW_TARGET.get(room)
    if not overflow_to:
        return room
    if room_occupancy(state, room) < room_desk_capacity(_ROOM_COLLISION.get(room)):
        return room
    if room_occupancy(state, overflow_to) < room_desk_capacity(_ROOM_COLLISION.get(overflow_to, 'workstations')):
        return overflow_to
    return room
def queue_work(state, items):
    """Port of tasks.js queueWork: whitelist each item's fields into a WORK_QUEUE
    entry. Mutates state['workQueue'] (creates it if absent). Returns new length."""
    work_queue = state.setdefault('workQueue', [])
    if not isinstance(work_queue, list):
        work_queue = state['workQueue'] = []
    for item in items:
        # A room-less card is legitimate: a shared-backlog card is room-free on
        # purpose, and the ASSIGNED agent resolves the room at assignment
        # (_assign_due_item). Every other caller supplies a room.
        if not item or not item.get('title'):
            continue
        work_queue.append({
            'title': item['title'],
            'room': item.get('room') or None,
            'instructions': item.get('instructions') or f"Pick whoever is best suited for: {item['title']}",
            # A player-filed JIRA card's contract: the normalized user story +
            # acceptance criteria survive the queue round trip onto the real task
            # (see _resolve_refinement -> _assign_due_item -> assign_task), so the
            # coding executor's backlog line carries the full spec, not just the
            # one-line summary. Same class of gap as 'checklist'/'distill' below:
            # a whitelist that silently drops a field its consumer needs.
            'userStory': item.get('userStory') or None,
            'acceptanceCriteria': item.get('acceptanceCriteria') or None,
            'pair': bool(item.get('pair')),
            'notBefore': item.get('notBefore') or None,
            'priority': normalize_priority(item.get('priority')),
            # A breakdown story's size estimate (S/M/L) survives the queue round
            # trip onto the real task and orders same-priority work (an L story
            # starts before an S story -- see pick_next_due_index). Same class
            # of gap as 'userStory'/'distill' below: a whitelist that silently
            # dropped a field its consumer needs.
            'sizeEstimate': normalize_size_estimate(item.get('sizeEstimate')),
            'goal': item.get('goal') or None,
            'projectLabel': item.get('projectLabel') or None,
            'research': item.get('research') or None,
            'taskType': item.get('taskType') or 'code',
            'skillReview': bool(item.get('skillReview')),
            # Hive mind: a distillation content-task that merges recent archive
            # findings into the think tank wiki (see _check_schedules's distill sweep).
            'distill': bool(item.get('distill')),
            'distillSince': item.get('distillSince') or 0,
            # Phase E2b: a SPIKE's work-cycle time budget (ms). A spike has no
            # deliverable -- it completes when the budget elapses, landing a
            # findings artifact instead of a peer-reviewed story.
            'budgetMs': item.get('budgetMs') or None,
            # Phase E addendum: a peer-rejected fix returns to the original author.
            'assignedTo': item.get('assignedTo') or None,
            # Human-in-the-loop: a JIRA issue filed FOR the player (assignedTo
            # 'player') travels with its issueKey so the player task records its
            # provenance -- same class of whitelist as 'distill'/'checklist'.
            'issueKey': item.get('issueKey') or None,
            # Phase E2d: an INCIDENT (bug) is pinned to the owning team's on-call
            # and pin-woken even though it carries no reviewOf.
            'incident': bool(item.get('incident')),
            # Theo routing: a spike the classifier could attribute
            # to a specific team is pinned to that team's on-call worker, same
            # pin-and-wake treatment as an incident -- see _assign_due_item.
            'directRoute': bool(item.get('directRoute')),
            # Gap: filing a story against a specific
            # team never meant it would be WORKED by that team -- assignment
            # (_assign_due_item) is think tank-wide round robin with zero team
            # awareness, only reviewer selection (_pick_reviewer_ids) ever
            # preferred same-team. teamId is the director id owning this item
            # (the queue-item-level analog of a backlogRequest's teamId), used
            # by _assign_due_item to PREFER that team the same soft way
            # _pick_reviewer_ids already does -- never a hard lock like an
            # incident's on-call pin, since a small think tank can't afford to
            # starve one team's queue while another sits idle.
            'teamId': item.get('teamId') or None,
            'sprintId': item.get('sprintId') or None,
            # Adversarial (winter) village: tags every card a side's director
            # files for the shared adversarial task, so completion detection
            # (serve._adversarial_village_tick) can tell when a side has
            # delivered. Same whitelist contract as 'distill'/'checklist'.
            'adversarialTaskId': item.get('adversarialTaskId') or None,
            'villageId': item.get('villageId') or None,
            # WS-14: a story pulled from the shared backlog keeps its provenance
            # -- the FEATURE it belongs to and the SHARED-BACKLOG item id it was
            # claimed from -- so the sprint digest and backlog board can read
            # completion back off the durable task mirror (same whitelist
            # contract as 'distill'/'checklist': silently dropping a field its
            # consumer needs breaks the pull loop).
            'featureId': item.get('featureId') or None,
            'backlogItemId': item.get('backlogItemId') or None,
            # Phase E: a pressoffice task may target a product (its build
            # releases the artifact) and carry optional pre-injected wiki pages.
            'productId': item.get('productId') or None,
            'wikiPageIds': list(item.get('wikiPageIds') or []),
            # Phase E addendum: a task may be a peer-approval review subtask (a
            # vote on a parent story) or a fix subtask (re-opening one). The
            # parent task id travels in `reviewOf`.
            'reviewOf': item.get('reviewOf') or None,
            # Phase E3: the story's original author id (see reviewAuthorId on the
            # review-subtask creation) -- lets an actionable review pin the fix
            # back to the worker who built it.
            'reviewAuthorId': item.get('reviewAuthorId') or None,
            # CS329A takeaway #2: the review checklist -- code/jev/
            # human-typed requirement entries (see content.py's ensemble grading) --
            # must survive the queue round trip or the Python review path can't
            # grade per-requirement. Same class of gap as 'distill' below: a
            # whitelist that silently drops a field its consumer needs.
            'checklist': list(item.get('checklist') or []),
            # Ordered-pipeline marker (see _check_pipelines / add_pipeline): which
            # pipeline + step index this work item belongs to, so the strict-order
            # sweep can read completion back off the durable task mirror. Must
            # survive the round trip or _pipeline_step_task can never match the
            # step and the pipeline wedges on step 0 forever -- the exact same
            # class of whitelist gap 'distill'/'checklist' both hit.
            'pipelineStep': item.get('pipelineStep') or None,
            # Phase E3.6: a task's knowledge-base class. `changes_how_we_work`
            # marks the standing ceremonies whose WHOLE job is maintaining the
            # shared knowledge base (research crawl -> skill file, skill-review
            # curation, distillation -> wiki). Only THAT class carries a KB-write
            # mandate; ordinary deliverables are required to land a completion
            # NOTE (evidence of what was done) but never a KB write.
            'kbClass': item.get('kbClass') or None,
            # Dependency cascade (ripple re-review): the task A this card is
            # blocked on (issue['dependsOnTask'], filed via
            # request_block_dependency). When A is re-opened (player veto) the
            # 'done' dependents carry this onto their task so the cascade can
            # re-review them too.
            'dependsOn': item.get('dependsOn') or None,
            # Bot Ops / shadow mode: a SHADOW work item does the work but changes
            # nothing -- on completion its outcome is captured to the append-only
            # state['shadowLedger'] draft instead of shipping (no peer gate, no
            # approvals/credits/completedDeliverables). Survives the queue round
            # trip so the real task knows it's a dry run (same whitelist contract
            # as 'distill'/'checklist').
            'shadow': bool(item.get('shadow')),
            # Item 4: the per-task spend ceiling (USD) + the refinement band
            # ('standard'/'generous') set during grooming survive the queue round
            # trip so the ASSIGNED task carries them (see assign_task). Same
            # whitelist contract as 'distill'/'checklist': silently dropping them
            # would leave the task without the budget the groomer judged it worth.
            'budgetUsd': item.get('budgetUsd') or None,
            'budgetBand': item.get('budgetBand') or None,
            # Bell-style spare-time lane: a MOONSHOT item is a protected
            # exploration lane -- never enters the peer gate (spikes are already
            # non-gated) and its failures are NOT mined into operator rules
            # (see _record_classified_failures). Explicit flag so the protection
            # is auditable, not implicit.
            'moonshot': bool(item.get('moonshot')),
            # Attention lane (build/reading/open/parking-lot): threads coexist
            # instead of one queue pulling. parking-lot cards are never auto-
            # assigned (see _is_parked / pick_next_due_index). Same whitelist
            # contract as 'distill'/'checklist': a lane silently dropped here
            # would let a parked card leak into assignment.
            'lane': normalize_lane(item.get('lane')),
            # Player-authored provenance: True only when the PLAYER wrote the
            # work text (player chat lane / player-filed JIRA issue), never an
            # agent. Rides the queue -> assignment -> task round trip (see
            # _assign_due_item / assign_task) so the JEV gate can tell a task
            # the player vetted from one an agent wrote (an agent-authored
            # story must not be able to manufacture its own work-context
            # bypass by naming a URL in its own text).
            'playerAuthored': bool(item.get('playerAuthored')),
        })
    return len(work_queue)


def queue_spike(state, title, room, budget_ms, now_ms=None, goal=None, instructions=None,
                project_label=None, moonshot=False, notBefore=None, assigned_to=None,
                lane=None, player_authored=False):
    """Phase E2b: enqueue a time-boxed SPIKE (taskType='spike'). A spike is an
    investigation with no committed deliverable: it's LOWEST priority (fills
    gaps, never blocks committed work), carries a hard `budgetMs` for the work
    cycle, and -- because nothing ships -- its completion does NOT open a peer
    gate (see _peer_gated_lane). Produces a findings artifact, not a release.
    `moonshot` marks a protected spare-time spike (see FREE_SPIKE_*): its
    failures are never mined into operator rules. `notBefore` (epoch ms) gates
    when the spike becomes assignable (spare-time work is deferred). Returns
    the new queue length, or None if the item was rejected (no room or title).
    Human-in-the-loop: `assigned_to='player'` hands the spike to the PLAYER
    instead of an agent -- the player completes it via the player task
    complete endpoint, and anything queued `depends_on_task` on it resumes the
    moment it ships (see _assign_player_task).
    `lane` (build/reading/open/parking-lot) rides the card so it can coexist
    with other threads instead of one queue pulling all the attention (see
    LANES)."""
    return queue_work(state, [{
        'title': title,
        'room': room,
        'instructions': instructions or (f'Spike: investigate and report on: {title}. '
                                          f'Explore the approach, run a short experiment, and '
                                          f'write up concrete findings. Time-boxed to ~{int((budget_ms or 60_000) / 1000)}s.'),
        'goal': goal,
        'projectLabel': project_label,
        'priority': WORK_PRIORITY['low'],
        'taskType': 'spike',
        'budgetMs': budget_ms or 60_000,
        'moonshot': bool(moonshot),
        'notBefore': notBefore,
        'assignedTo': assigned_to or None,
        'lane': lane,
        # Player-authored provenance: True only when the PLAYER filed this spike
        # (the player chat lane) -- see queue_work's whitelist comment.
        'playerAuthored': bool(player_authored),
    }])
def queue_once(state, title, at_ms, room=None, instructions=None, task_type=None,
               goal=None, priority=None, project_label=None, depends_on_task=None,
               assigned_to=None, lane=None):
    """Queue a single work item to fire ONCE at an absolute wall-clock time
    (`at_ms`, epoch milliseconds). The item rides the same `notBefore` gate
    the rest of the queue already honors (see is_work_item_due): it sits in
    the work queue untouched until `at_ms`, then runs through the normal
    assign -> work -> complete lifecycle exactly once. `depends_on_task` (a
    task id) adds the dependency gate: even once `at_ms` passes, the item is
    NOT assigned until that task reaches 'done' (see _work_item_dependency_met)
    -- the composed "run X once the dependency lands" scheduling lane. Returns
    the queued work item dict, or None if rejected (no title, or `at_ms` is
    not a positive future epoch-ms).
    Human-in-the-loop: `assigned_to='player'` hands the item to the PLAYER
    instead of an agent -- combined with `depends_on_task` this is the
    "give the player X once story T lands" lane (see _assign_player_task).
    `lane` (build/reading/open/parking-lot) rides the card so threads coexist
    (see LANES); `parking-lot` defers it with a bookmark -- never auto-assigned."""
    at_ms = int(at_ms or 0)
    if not title or at_ms <= 0:
        return None
    item = {
        'title': title,
        'room': room or None,
        'instructions': instructions or f'Handle this one-off request once: {title}',
        'notBefore': at_ms,
        'taskType': task_type or None,
        'goal': goal or None,
        'priority': normalize_priority(priority),
        'projectLabel': project_label or None,
        'dependsOn': depends_on_task or None,
        'assignedTo': assigned_to or None,
        'lane': lane,
    }
    queue_work(state, [item])
    return item


def think_tank_has_work(state, now_ms):
    """Port of tasks.js thinkTankHasWork: a due queue item, or any agent currently
    task/handoff/pair/busy. A due item whose `dependsOn` dependency hasn't
    landed is NOT work yet -- it waits (no agent spend) until the dependency
    ships, same gate pick_next_due_index applies at assignment.
    Human-in-the-loop: a due PLAYER card (assignedTo 'player') counts as work
    too -- the idle gate must not short-circuit a pass that is supposed to
    deliver a card into the player's hands (see _assign_player_task)."""
    work_queue = state.get('workQueue') or []
    if any(is_work_item_due(item, now_ms) and _work_item_dependency_met(state, item)
           and not _is_parked(item)
           for item in work_queue):
        return True
    for a in (state.get('agents') or {}).values():
        if a and (a.get('task') or a.get('handoff') or a.get('pairWith') or a.get('busy')):
            return True
    return False


def _team_oncall_members(state, director_id):
    """The pool an owning team can draw an on-call from: the director's direct
    reports, minus the standing scrum master (who facilitates, not works -- the
    same exclusion `_team_member_count` applies). Admins are also excluded so the
    on-call is a real worker. Returns a list of agent ids in stable roster order."""
    scm = None
    for t in (state.get('teams') or []):
        if t.get('directorId') == director_id:
            scm = t.get('scrumMasterId')
            break
    members = _sim_direct_reports(state, director_id)
    roster = {d.get('id'): d for d in (state.get('agentRoster') or [])}
    pool = []
    for m in members:
        if m == scm:
            continue
        if (roster.get(m) or {}).get('isAdmin'):
            continue
        pool.append(m)
    return pool


def _oncall_order(state, director_id):
    """The persistent on-call rotation ORDER for director_id's team. Seeded from
    the derived roster order on first use; a backup who actually served a sprint
    is rotated to the END of this order at sprint close (_rotate_served_oncalls)
    so she does not get paged again next sprint before the rotation catches up.
    New hires are appended at the end (they start as backups); fired agents are
    dropped. Mutates state['_oncallOrder'] only on seed/reconcile."""
    orders = state.setdefault('_oncallOrder', {})
    order = orders.get(director_id)
    pool = _team_oncall_members(state, director_id)
    if not order:
        orders[director_id] = list(pool)
        return orders[director_id]
    alive = [a for a in order if a in (state.get('agents') or {})]
    for a in pool:
        if a not in alive:
            alive.append(a)
    orders[director_id] = alive
    return alive


def _rotate_served_oncalls(state, team_ids):
    """At sprint close: any on-call BACKUP who actually served this sprint's
    pages is rotated to the END of her team's on-call queue, so a backup who
    covered does not get paged again next sprint before the rotation catches up.
    Mutates state['_oncallOrder'] and clears state['_oncallServed'].
    A team is matched by its `id` or its director's id (the two keyings teams
    records use in the codebase); a sprint record's teamIds may carry either."""
    teams = {t.get('id'): t for t in (state.get('teams') or [])}
    served = state.get('_oncallServed') or {}
    orders = state.setdefault('_oncallOrder', {})
    for tid in team_ids or []:
        t = teams.get(tid)
        if t is None:
            t = next((x for x in (state.get('teams') or [])
                      if x.get('directorId') == tid), None)
        if not t:
            continue
        director_id = t.get('directorId') or t.get('id')
        if not director_id:
            continue
        for aid in served.get(director_id) or []:
            order = orders.get(director_id)
            if order and aid in order:
                order.remove(aid)
                order.append(aid)
        served[director_id] = []


def on_call_agent(state, director_id, sprint_id=None, now_ms=None):
    """Phase E2d: which agent on `director_id`'s team is on-call right now.
    Deterministic per-sprint rotation over the team's non-scrum-master, non-admin
    members -- derived from the persistent on-call ORDER (_oncall_order, seeded
    from roster order), so the same sprint id always hands back the same on-call
    across restarts and the order shifts across sprints. A backup who actually
    served a sprint is rotated to the end of the order at sprint close
    (_rotate_served_oncalls) rather than paged again next sprint.

    Within a rotation the pager prefers an AVAILABLE member (on-duty and not
    mid-work): if the primary is busy, the BACKUP is the next slot in rotation
    order and the second backup the slot after that -- so an on-call who is
    already working hands the pager to her backup instead of double-booking
    her. A question/incident NOT tied to a sprint has no natural advance point
    (the sprint counter is static), so the slot also shifts with the UTC day --
    the pager actually moves even on a sprint-less team.

    Deadfalls back to the primary slot when the whole team is unavailable, so
    the pin still names a concrete owner (assignment re-derives if it can and
    otherwise falls back to the team's best free worker).
    Returns an agent id or None if the team has no rousable members."""
    pool = _oncall_order(state, director_id)
    if not pool:
        return None
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    seed = sprint_id or 'default'
    # stable hash across restarts; fold in a running sprint counter so rotations
    # actually advance even if sprint ids were reused.
    counter = len([s for s in (state.get('sprints') or {}).values()
                   if s.get('teamIds') and director_id in s.get('teamIds')])
    h = 0
    for ch in str(seed):
        h = (h * 31 + ord(ch)) & 0xffffffff
    idx = (h + counter) % len(pool)
    if not sprint_id:
        # No sprint anchor (player question, unscheduled incident) -> shift the
        # slot by the UTC day so the pager moves even when a team's sprint list
        # is static. Still derived: a pure function of state + now_ms.
        idx = (idx + now_ms // 86400000) % len(pool)
    agents = state.get('agents') or {}
    def _available(aid):
        a = agents.get(aid)
        return bool(a) and not a.get('offDuty') and not a.get('busy') \
            and not a.get('task') and not a.get('pairWith')
    # Prefer an available member in ROTATION order: the primary slot, then its
    # backup (next slot), then the second backup (the one after that), ...
    chosen = None
    for i in range(len(pool)):
        cand = pool[(idx + i) % len(pool)]
        if _available(cand):
            chosen = cand
            break
    if chosen is None:
        chosen = pool[idx]
    # Track a BACKUP who actually served a sprint-anchored page, so the sprint-
    # close ceremony can rotate her to the end of the queue.
    if sprint_id and chosen != pool[idx]:
        served = state.setdefault('_oncallServed', {}).setdefault(director_id, [])
        if chosen not in served:
            served.append(chosen)
    return chosen


def queue_bug(state, product_id, title, now_ms=None, room=None, reported_by=None,
              sprint_id=None, instructions=None):
    """Phase E2d: file a BREAKAGE report (taskType='bug') for a product and route
    it to the owning team's current on-call agent as a pinned, high-priority,
    NON-GATED incident. product_id resolves to its owning team (product.teamId ->
    director_id) -> on_call_agent(...). Returns the new queue length, or None if
    the incident can't be routed (no owning team, no on-call, or the team already
    has an open bug -- the one-in-flight cap)."""
    products = state.get('products') or {}
    prod = products.get(product_id)
    if not prod:
        return None
    director_id = prod.get('teamId')
    if not director_id:
        return None
    oc = on_call_agent(state, director_id, sprint_id, now_ms)
    if not oc:
        return None
    # One-in-flight cap per owning team + product: a gutted on-call must not
    # accumulate incidents while a downstream team floods.
    for item in (state.get('workQueue') or []):
        if item.get('taskType') == 'bug' and item.get('productId') == product_id:
            return None
    for task in (state.get('tasks') or {}).values():
        if task.get('taskType') == 'bug' and task.get('productId') == product_id:
            return None
    # A broken product is software: the fix lands in Press Office (the only
    # room whose executor actually writes code), so the bug produces a real fix
    # artifact rather than a chore. Products carry no room field today.
    room = room or 'pressoffice'
    instructions = instructions or (f'INCIDENT: {title}. A downstream team reports '
                                    f'this product is broken. You are on-call. Fix it, '
                                    f'land the fix artifact, and close it. No peer review '
                                    f'needed -- this is incident response, not feature work.')
    return queue_work(state, [{
        'title': title,
        'room': room,
        'instructions': instructions,
        'goal': None,
        'projectLabel': None,
        'priority': WORK_PRIORITY['high'],
        'taskType': 'bug',
        'productId': product_id,
        'assignedTo': oc,
        'incident': True,
        'sprintId': sprint_id,
    }])


def _dependency_landed(state, task_id):
    """True when the dependency task `task_id` has reached 'done' in the durable
    task mirror. A dependency only 'lands' when a real done task exists under
    that id (old done tasks persist in state, so this stays true after
    completion). Absent / walking / working / needs_review -> not landed."""
    t = (state.get('tasks') or {}).get(task_id)
    return isinstance(t, dict) and t.get('status') == 'done'


def _work_item_dependency_met(state, item):
    """True when a queue item's dependency (item['dependsOn']) has landed --
    i.e. the referenced task reached 'done' in the durable mirror. An item
    with no dependency is always met. pick_next_due_index / think_tank_has_work
    use this to keep a gated card IN the queue (never assigned, never abandoned
    on the attempts cap) until its dependency actually ships."""
    dep = item.get('dependsOn')
    if not dep:
        return True
    return _dependency_landed(state, dep)


def pick_next_due_index(work_queue, now_ms, exclude_items, state=None):
    """Port of tasks.js _pickNextDueIndex: highest due priority wins; at EQUAL
    priority, the larger size estimate (L > M > S) starts first -- an L story
    needs more wall-time than an S story, so it is pulled ahead of same-priority
    siblings (effort is a tiebreak, never an urgency override). strict >
    preserves arrival order at equal priority + equal size; skip items in
    exclude_items (this call's attemptedThisCycle). When `state` is supplied
    (the live assignment loop), an item whose `dependsOn` dependency hasn't
    landed is skipped too -- a gated card waits for its dependency, never
    leaks into assignment. Returns index or -1.

    Attention lanes (see LANES): a `parking-lot` card is never auto-picked --
    it waits with a bookmark, so no booking pulls it in. The lane weight
    (build 3 / open 2 / reading 1 / none 0) breaks priority ties, never
    overrides urgency: urgent always beats normal regardless of lane. With
    `state`, a `reading` card is rate-limited to ONE active per room -- the
    slow lane moves one thread at a time, not a flood."""
    best_index, best_score = -1, None
    for i, item in enumerate(work_queue):
        if not is_work_item_due(item, now_ms):
            continue
        # exclude_items is a Set of QUEUE-ITEM IDENTITIES (id(item)), not the
        # dicts themselves -- queue items are mutable dicts and unhashable.
        if exclude_items and id(item) in exclude_items:
            continue
        # Parking-lot: the bookmark lane. Skip on EVERY call (with or without
        # state) -- an auto-pick must never leak a parked card into assignment.
        if _is_parked(item):
            continue
        if state is not None and not _work_item_dependency_met(state, item):
            continue
        if state is not None and _reading_lane_busy(state, item):
            continue
        priority = item.get('priority', WORK_PRIORITY['normal'])
        lane_weight = LANE_WEIGHT.get(item.get('lane') or '', 0)
        score = (priority, lane_weight, size_estimate_weight(item.get('sizeEstimate')))
        if best_score is None or score > best_score:
            best_score, best_index = score, i
    return best_index


def _reading_lane_busy(state, item):
    """The reading lane is rate-limited to one ACTIVE card per room -- a slow
    lane moves one chapter/paper thread at a time (see LANES). True when
    another reading-lane task is currently walking/working in the item's room,
    so a fresh reading card waits its turn instead of stacking on top."""
    if (item.get('lane') or '') != 'reading':
        return False
    room = item.get('room')
    for t in (state.get('tasks') or {}).values():
        if not isinstance(t, dict):
            continue
        if t.get('lane') != 'reading':
            continue
        if t.get('status') not in ('walking', 'working'):
            continue
        if room and t.get('room') != room:
            continue
        if t.get('id') == item.get('id'):
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# Clarify router (player -> admin -> on-call -> KB-first -> completing agent).
# The player asks the tank about how completed work was done; we route to the
# owning team's current ON-CALL agent (whether they worked on it or not), who
# answers KNOWLEDGE-BASE-FIRST -- search the library for the product + question
# before ever reaching the completing agent. If the on-call genuinely cannot
# answer from the KB, the plan escalates to the completing agent. Pure/derived
# (no serve import, no model call): the caller performs the actual KB search +
# LLM call. Mirrors on_call_agent's derived-not-stored precedent.
# ---------------------------------------------------------------------------
def clarify_router_plan(state, product_id, question, sprint_id=None):
    """Resolve who should answer the player's clarifying question about a
    product's completed work. Returns a plan dict or None if nothing routable:

        {'onCall': agent_id,            # answers first (KB-first)
         'completing': agent_id|None,   # fallback if on-call can't answer
         'product': {...}               # the product record (may be {})
         'onCallFallback': bool         # True when the director answered in
                                        # the on-call's place (empty pool)
    }
    `completing` is the most recent agent who landed a deliverable matching the
    product name/alias -- derived from completedDeliverables. None on-call => no
    way to answer (nobody rousable on the owning team)."""
    products = state.get('products') or {}
    prod = products.get(product_id) or {}
    director_id = prod.get('teamId')
    oc = None
    fallback = False
    if director_id:
        oc = on_call_agent(state, director_id, sprint_id)
    if not oc and director_id:
        # Last resort: nobody on the owning team is rousable for the standing
        # rotation (e.g. a team of just an admin + scrum master). A PLAYER
        # question is worth the team's own director answering even though
        # admins never join the rotation -- admins don't do the work, but an
        # unanswered question is worse than an admin answer. Incident routing
        # keeps the stricter rule (queue_bug never falls back like this).
        director = (state.get('agents') or {}).get(director_id)
        if director and not director.get('offDuty'):
            oc = director_id
            fallback = True
    completing = _completing_agent_for_product(state, product_id, prod)
    return {
        'onCall': oc,
        'completing': completing,
        'product': prod,
        'onCallFallback': fallback,
    }


def _product_aliases(prod):
    """Terms a clarify question about `prod` should match against in the
    knowledge base + completing-agent history: the product id itself, its name,
    and any name keeps. Lowercased, minimal."""
    terms = set()
    pid = (prod.get('id') or '').strip()
    name = (prod.get('name') or '').strip()
    if pid:
        terms.add(pid.lower())
    if name:
        terms.add(name.lower())
    for keep in (prod.get('nameKeeps') or []):
        k = (keep or '').strip().lower()
        if k:
            terms.add(k)
    return terms


def _text_matches_any(text, terms):
    text = (text or '').lower()
    return any(t and t in text for t in terms)


def _completing_agent_for_product(state, product_id, prod=None):
    """Most recent agent who landed a completed deliverable associated with
    this product -- the completing agent a clarify can fall back to. Matches
    completedDeliverables entries whose title/room mentions the product id,
    name, or an alias; most recently graded (gradedAt) wins. Returns the agent
    id or None (nothing completed for it yet)."""
    prod = prod if prod is not None else (state.get('products') or {}).get(product_id)
    terms = _product_aliases(prod)
    if not terms:
        return None
    best, best_at = None, -1
    for d in (state.get('completedDeliverables') or []):
        title = (d.get('title') or '')
        room = (d.get('room') or '')
        if not _text_matches_any(f'{title} {room}', terms):
            continue
        at = d.get('gradedAt') or 0
        if at >= best_at:
            best_at, best = at, d.get('agentId')
    return best


# ---------------------------------------------------------------------------
# Phase C: sprints + task feed. A sprint is a director-authored, time-boxed body
# of work whose items are TAGGED QUEUE ENTRIES -- each item also lands in the
# shared state['workQueue'] carrying its sprintId, so assignment reuses the
# proven _assign_due_item/_task_cycle path untouched. Progress is DERIVED, not
# stored: resolved by matching a sprint item's title+room against live task
# status + queue presence. Pure state mutation (no serve import at call site),
# unit-testable, run through the serve.py wrapper like everything else.
# ---------------------------------------------------------------------------
def queue_sprint(state, sprint_id, name, goal, owner_id, items, valid_rooms,
                 target_date_ms=None, now_ms=None, team_ids=None, worker_count=None):
    """Create a sprint record and append its valid items to the workQueue, each
    tagged with sprintId. `items` are dicts shaped like queue_work items. Only
    items whose room is in `valid_rooms` (the caller supplies the delegatable
    set -- sim.py stays decoupled from serve.py's constant) are kept; a sprint
    with zero retainable items is not created. `team_ids` records which teams
    the sprint touches (the scrum-master gate is enforced by the CALLER before
    calling here; this just persists the association). `worker_count` is the
    director's chosen sprint headcount (1..MAX_TEAM_MEMBERS): the pool of
    people ALLOWED to work this sprint's cards, enforced strictly at assignment
    (_assign_due_item) -- no pool fallback. Absent/legacy -> the team's full
    complement. Returns the sprint record dict, or None if nothing was created.
    Mutates state['sprints'] + state['workQueue']."""
    valid = {r for r in (valid_rooms or [])}
    kept = []
    for it in items or []:
        if not it or not it.get('title') or not it.get('room'):
            continue
        if it['room'] not in valid:
            continue
        kept.append(dict(it))
    if not kept:
        return None
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    sprints = state.setdefault('sprints', {})
    record = {
        'id': sprint_id,
        'name': name or goal or 'Sprint',
        'goal': goal or '',
        'ownerId': owner_id,
        'createdAt': now_ms,
        'targetDate': target_date_ms or None,
        'teamIds': list(dict.fromkeys(team_ids or [])),
        # The director-chosen sprint headcount (1..MAX_TEAM_MEMBERS). Strictly
        # staffed: only this many workers may take this sprint's cards, and
        # only from the sprint's teams (see _sprint_worker_pool).
        'workerCount': max(1, min(MAX_TEAM_MEMBERS, int(worker_count or MAX_TEAM_MEMBERS))),
        'status': 'active',
        'items': [_sprint_item_id(it) for it in kept],
    }
    sprints[sprint_id] = record
    for it in kept:
        it['sprintId'] = sprint_id
    queue_work(state, kept)
    # Sprint rollover: carry the prior closed sprint's unfinished cards into the
    # new sprint (re-tag their queue items + fold their ids into this sprint's
    # items) so the un-landed scope explicitly continues.
    _carry_over_sprint_work(state, record, team_ids)
    return record


def sprint_progress(state, sprint_id):
    """Derive a sprint's live progress: total items, and counts of queued /
    in-progress / done, resolved by matching each stored (title, room) pair
    against the shared workQueue (still waiting) and the durable tasks map
    (walking / working / done). Also returns `landed`: the titles, in sprint-item
    order, of items that are done -- the "what landed" digest a closed sprint
    reads back as (and an active one shows so far). Returns a dict, or None if
    unknown."""
    record = (state.get('sprints') or {}).get(sprint_id)
    if not record:
        return None
    items = record.get('items') or []  # list of (title, room) tuples
    open_tasks = state.get('tasks') or {}
    # Bot Ops / shadow mode: shadow (dry-run) work must not count toward a
    # sprint -- it ships nothing. Exclude shadow queue items from the still-queued
    # set and shadow tasks from the done/in-progress match.
    still_queued = set(_sprint_item_id(it) for it in (state.get('workQueue') or [])
                       if not it.get('shadow'))
    # Bot Ops / shadow mode: a purely-shadow (dry-run) item must not count toward
    # the sprint at all -- not queued, not in-progress, not done. Track which item
    # ids have ONLY a shadow representation so the fallback below can skip them.
    shadow_ids = set(_sprint_item_id(it) for it in (state.get('workQueue') or [])
                     if it.get('shadow'))
    for t in (state.get('tasks') or {}).values():
        if t.get('shadow'):
            shadow_ids.add((t.get('title') or '', t.get('room') or ''))
    done = in_progress = queued = 0
    landed = []
    for pair in items:
        # After a save/load round-trip tuples arrive as JSON lists -- normalize
        # so hashing against the set of (title, room) tuples always works.
        title, room = tuple(pair)
        ident = (title, room)
        if ident in still_queued and not any(
                t.get('status') == 'done' and t.get('title') == title and t.get('room') == room
                and not t.get('shadow')
                for t in open_tasks.values()):
            # Still in the queue, not yet assigned: queued.
            queued += 1
            continue
        matched_done = matched_active = False
        for t in open_tasks.values():
            if t.get('title') != title or t.get('room') != room:
                continue
            if t.get('shadow'):
                continue
            if t.get('status') == 'done':
                matched_done = True
            elif t.get('status') in ('walking', 'working'):
                matched_active = True
        if matched_done:
            done += 1
            landed.append(title)
        elif matched_active:
            in_progress += 1
        elif ident in shadow_ids:
            # The only representation of this item is shadow (dry-run) work --
            # it ships nothing, so it is invisible to the sprint.
            continue
        else:
            queued += 1
    total = len(items)
    pct = round((done / total) * 100) if total else 0
    return {'total': total, 'queued': queued, 'inProgress': in_progress,
            'done': done, 'pct': pct, 'landed': landed}


# The sprint-close velocity signal is a trailing per-team history: how much of
# a just-closed sprint actually landed (count + size points, when estimated).
# Capped so the digest stays a rolling window, not a whole-of-history ledger.
TEAM_VELOCITY_MAX = 20


def close_sprint(state, sprint_id, now_ms=None):
    """Mark a sprint closed (a container action -- already-queued items finish
    or age out normally; closing doesn't cancel work). Sprint ROLLOVER: any card
    still waiting in the queue (tagged with this sprint, not yet done) is
    recorded on the record as `rolledOver` (id + title), so a follow-up sprint
    created for the same team carries it over explicitly instead of silently
    losing the un-landed scope. Sprint VELOCITY: on the active->closed
    transition the record gains `velocity` (landed/total/pct/rolledOver/points/
    elapsedMs) and is folded into the per-team trailing history
    state['teamVelocity'] -- the signal a director reads to size the next
    sprint. Idempotent: re-closing an already-closed sprint recomputes nothing
    (no double velocity, no closedAt overwrite). Returns the updated record,
    or None if the sprint doesn't exist."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    sprints = state.get('sprints') or {}
    record = sprints.get(sprint_id)
    if not record:
        return None
    if record.get('status') != 'closed':
        record['rolledOver'] = [
            {'id': _sprint_item_id(it), 'title': it.get('title')}
            for it in _unfinished_sprint_items(state, sprint_id)]
        record['closedAt'] = now_ms
        _record_sprint_velocity(state, record, now_ms)
    record['status'] = 'closed'
    return record


def _sprint_item_estimate(state, sprint_id, title, room):
    """The size-estimate WEIGHT of one sprint item, looked up from the live
    state (an unassigned card still in the queue carries it; an assigned one
    rides on its durable task). 0 when the card has no numeric estimate --
    'not estimated' is honest, never guessed as a small size."""
    for it in (state.get('workQueue') or []):
        if it.get('sprintId') == sprint_id and it.get('title') == title \
                and (it.get('room') or None) == room:
            return size_estimate_weight(it.get('sizeEstimate'))
    for t in (state.get('tasks') or {}).values():
        if t.get('shadow'):
            continue
        if t.get('title') == title and (t.get('room') or None) == room:
            return size_estimate_weight(t.get('sizeEstimate'))
    return 0


def _sprint_item_done(state, title, room):
    """True when a sprint item has a matching DONE, non-shadow task -- the same
    'landed' test sprint_progress derives (a peer-gated story counts only once
    its gate closed)."""
    return any(t.get('status') == 'done' and t.get('title') == title
               and (t.get('room') or None) == room and not t.get('shadow')
               for t in (state.get('tasks') or {}).values())


def _record_sprint_velocity(state, record, now_ms):
    """Compute the velocity snapshot of a sprint at close and fold it into the
    per-team trailing history (state['teamVelocity'], capped at
    TEAM_VELOCITY_MAX per team). `landed`/`total`/`pct` mirror sprint_progress;
    `rolledOver` mirrors the close record; `pointsLanded`/`pointsTotal` sum the
    size-estimate weights (S=1 / M=2 / L=3) and are None when the sprint carried
    no numeric estimates at all. `elapsedMs` is wall-time since creation. Pure
    state mutation; called once from close_sprint on the active->closed
    transition (idempotent -- a re-close never re-folds)."""
    items = record.get('items') or []
    points_total = 0
    points_estimated = False
    landed_points = 0
    landed = 0
    for pair in items:
        title, room = tuple(pair)
        w = _sprint_item_estimate(state, record.get('id'), title, room)
        if w:
            points_estimated = True
            points_total += w
        if _sprint_item_done(state, title, room):
            landed += 1
            if w:
                landed_points += w
    total = len(items)
    pct = round((landed / total) * 100) if total else 0
    velocity = {
        'landed': landed,
        'total': total,
        'pct': pct,
        'rolledOver': len(record.get('rolledOver') or []),
        'pointsLanded': landed_points if points_estimated else None,
        'pointsTotal': points_total if points_estimated else None,
        'elapsedMs': max(0, int(now_ms) - int(record.get('createdAt') or 0)),
    }
    record['velocity'] = velocity
    history = state.setdefault('teamVelocity', {})
    for tid in (record.get('teamIds') or []):
        bucket = history.setdefault(tid, [])
        bucket.append(velocity)
        if len(bucket) > TEAM_VELOCITY_MAX:
            del bucket[:len(bucket) - TEAM_VELOCITY_MAX]
    return velocity


def _unfinished_sprint_items(state, sprint_id):
    """The sprint's cards still waiting in the queue (not yet done): queued
    workQueue items tagged with this sprint that have no matching 'done' task.
    These are exactly what a sprint close carries over into the next sprint
    (in-flight cards keep flowing -- closing never cancels work -- and are not
    re-planned)."""
    tasks = state.get('tasks') or {}
    unfinished = []
    for it in (state.get('workQueue') or []):
        if it.get('sprintId') != sprint_id:
            continue
        if any(t.get('status') == 'done' and t.get('title') == it.get('title')
               and t.get('room') == it.get('room')
               for t in tasks.values()):
            continue
        unfinished.append(it)
    return unfinished


def _carry_over_sprint_work(state, record, team_ids):
    """Sprint rollover: when a new sprint is created, carry the PRIOR closed
    sprint's unfinished cards (its `rolledOver` set) into the new sprint. Their
    queued items are re-tagged with the new sprint_id and their ids are added to
    the new sprint's `items`, so the next sprint's scope and progress include the
    work that didn't land last time. First-come: the most recent closed sprint
    whose teams overlap the new sprint is the carry source. Pure state mutation;
    returns the count of cards carried over."""
    if not team_ids:
        return 0
    prior = None
    for s in (state.get('sprints') or {}).values():
        if s.get('id') == record.get('id') or s.get('status') != 'closed':
            continue
        if not (s.get('teamIds') or []) or not any(t in s['teamIds'] for t in team_ids):
            continue
        if prior is None or (prior.get('createdAt') or 0) < (s.get('createdAt') or 0):
            prior = s
    if not prior:
        return 0
    rolled = {r.get('id') for r in (prior.get('rolledOver') or [])}
    if not rolled:
        return 0
    items = record.setdefault('items', [])
    carried = 0
    for it in (state.get('workQueue') or []):
        if it.get('sprintId') != prior.get('id'):
            continue
        ident = _sprint_item_id(it)
        if ident not in rolled:
            continue
        it['sprintId'] = record['id']
        if ident not in items:
            items.append(ident)
        carried += 1
    return carried


def _auto_close_completed_sprints(state, now_ms=None):
    """Close any ACTIVE sprint whose entire body of work is done AND approved.
    'Done and approved' is exactly what sprint_progress counts as `done`: for a
    peer-gated story that means 2 clean peer approvals (or 1 + timeout) have
    closed its gate (_close_gated_story flips status to 'done' only on that),
    and for a non-gated task it means finish_task completed it. So done == total
    is the "fully completed and approved for the entire sprint" signal, and an
    active sprint at that point is automatically closed instead of waiting for
    the player to tap close. Returns the list of sprint ids closed."""
    sprints = state.get('sprints') or {}
    closed = []
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    for sid, record in list(sprints.items()):
        if record.get('status') != 'active':
            continue
        progress = sprint_progress(state, sid)
        if progress and progress.get('total') and progress['done'] >= progress['total']:
            close_sprint(state, sid, now_ms)
            record['closedAt'] = now_ms
            record['autoClosed'] = True
            closed.append(sid)
            # WS-14 sprint-close ceremony: retrospective + shared-backlog pull +
            # refinement kick happen the moment the sprint fully lands.
            _on_sprint_closed(state, record, now_ms)
    return closed
# ---------------------------------------------------------------------------
# WS-14: shared backlog + features. When every team is already committed to an
# active sprint, extra large asks are broken down by an authority into STORIES /
# SPIKES filed in a SHARED unassigned backlog under a FEATURE (created when none
# exists). Any team is eligible to pull at sprint close (first-come); the pull
# claims the item for the team with a team-scoped story key (reusing the JIRA
# issue counters) and queues it as real work. Pure state mutation -- serve.py
# does the model-call breakdown and keeps the decoupled constants.
# ---------------------------------------------------------------------------


def _next_feature_id(state):
    """Monotonic feature id (feat-1, feat-2, ...), so a cold state starts at 1
    and a hot one never collides (mirrors next_sprint_id / next_product_id)."""
    n = int(state.get('featureCounter') or 0) + 1
    state['featureCounter'] = n
    return f'feat-{n}'


def _next_backlog_id(state):
    """Monotonic shared-backlog item id (bl-1, bl-2, ...)."""
    n = int(state.get('backlogCounter') or 0) + 1
    state['backlogCounter'] = n
    return f'bl-{n}'


def _find_feature(state, name):
    """The feature whose name matches `name` (case-insensitive), or None."""
    name = (name or '').strip()
    if not name:
        return None
    for f in (state.get('features') or {}).values():
        if f.get('name') and f['name'].lower() == name.lower():
            return f
    return None


def create_or_reuse_feature(state, name, now_ms, quarter=None, target_date_ms=None,
                            created_by=None):
    """Pure: the feature record for `name`, reusing the existing one when a
    feature by that name is already on the board (a feature groups the backlog
    stories of one initiative; breakdowns of the same initiative share it).
    Returns the record. Mutates state['features'] + state['featureCounter']."""
    f = _find_feature(state, name)
    if f:
        return f
    fid = _next_feature_id(state)
    f = {
        'id': fid,
        'name': (name or '').strip() or f'Feature {fid}',
        'quarter': quarter or None,
        'targetDate': target_date_ms or None,
        'status': 'open',  # open / in_progress / done
        'storyIds': [],
        'createdAt': now_ms,
        'createdBy': created_by or None,
    }
    state.setdefault('features', {})[fid] = f
    return f


def add_backlog_item(state, title, feature_id, created_by, now_ms,
                     item_type='story', acceptance_criteria=None,
                     size_estimate=None, goal=None, instructions=None,
                     blocked_by=None):
    """Pure: file one unassigned STORY/SPIKE in the SHARED backlog under
    `feature_id`. Returns the item dict, or None if the cap is reached or the
    item is malformed. Mutates state['backlog'] + state['backlogCounter'] +
    the feature's storyIds. Unassigned until a team pulls it (teamId + storyKey
    are assigned at pickup).

    A card carries NO room: the buildings are shared across teams, so the room
    is an EXECUTION detail -- the agent who picks the card up figures out where
    the work needs to happen (_assign_due_item resolves it at assignment)."""
    title = (title or '').strip()
    if not title:
        return None
    backlog = state.setdefault('backlog', [])
    if len(backlog) >= MAX_BACKLOG_ITEMS:
        return None
    bid = _next_backlog_id(state)
    item = {
        'id': bid,
        'title': title,
        'type': 'spike' if item_type == 'spike' else 'story',
        'featureId': feature_id or None,
        'status': 'blocked' if blocked_by else 'ready',  # ready / blocked / picked
        'blockedBy': blocked_by or None,
        'acceptanceCriteria': acceptance_criteria or None,
        'sizeEstimate': size_estimate or None,
        'goal': goal or None,
        'instructions': instructions or None,
        'createdBy': created_by or None,
        'createdAt': now_ms,
        'teamId': None,   # assigned at pickup
        'storyKey': None,  # assigned at pickup (DEV-1, ...)
    }
    backlog.append(item)
    if item['featureId']:
        f = (state.get('features') or {}).get(item['featureId'])
        if f:
            f.setdefault('storyIds', []).append(bid)
    return item


def backlog_ready_items(state):
    """Eligible shared-backlog items: READY (not blocked-by-dependency) and
    unassigned (no teamId yet), in FIFO order. A blocked item waits for its
    dependency to clear."""
    return [b for b in (state.get('backlog') or [])
            if b.get('status') == 'ready' and not b.get('teamId')]


def pull_backlog_item(state, team_id, now_ms):
    """Atomic first-come pull for `team_id`: claim the oldest eligible item,
    assign teamId + a team-scoped story key, flip status -> 'picked'. Returns
    the claimed item, or None if nothing is eligible."""
    ready = backlog_ready_items(state)
    if not ready:
        return None
    item = ready[0]
    item['teamId'] = team_id
    item['storyKey'] = next_issue_key(state, team_id)
    item['status'] = 'picked'
    item['pickedAt'] = now_ms
    return item


def _resolve_assignment_room(pick):
    """The room a ROOM-LESS work card is routed to when an agent picks it up.
    A shared-backlog card is room-free on purpose (the buildings are shared; the
    room is an execution detail), so the ASSIGNED AGENT figures out where the
    work needs to happen -- realized deterministically by the card's nature: an
    investigation spike goes to the observatory, a deliverable story to the
    press office (real files get written there)."""
    return 'observatory' if pick.get('taskType') == 'spike' else 'pressoffice'


def _pull_backlog_for_team(state, team_id, now_ms):
    """Pull the first eligible shared-backlog card for `team_id` and queue it as
    real work, tagged with its feature + backlog item + team so the sprint digest
    and backlog board can read completion back off the durable task mirror. The
    card is queued ROOM-LESS on purpose: the agent who picks it up figures out
    where the work needs to happen (_assign_due_item resolves the room at
    assignment). Returns the pulled card, or None when nothing is eligible."""
    item = pull_backlog_item(state, team_id, now_ms)
    if not item:
        return None
    is_spike = item.get('type') == 'spike'
    instructions = item.get('instructions')
    if not instructions:
        instructions = (f"({item.get('storyKey') or item['id']}) {item.get('title')}")
        if item.get('acceptanceCriteria'):
            instructions += f"\n\nAcceptance criteria:\n{item['acceptanceCriteria']}"
    queue_work(state, [{
        'title': item['title'],
        'room': None,  # the assigned agent decides where the work happens
        'instructions': instructions,
        'goal': item.get('goal'),
        'teamId': team_id,
        'taskType': 'spike' if is_spike else 'code',
        'budgetMs': 60_000 if is_spike else None,
        # A breakdown story's size estimate survives the shared backlog onto the
        # pulled card (same contract as _resolve_breakdown's direct queue path).
        'sizeEstimate': item.get('sizeEstimate'),
        'featureId': item.get('featureId'),
        'backlogItemId': item['id'],
    }])
    _log_governance(state, team_id, 'backlog_pull',
                    {'item': item['id'], 'storyKey': item.get('storyKey'),
                     'feature': item.get('featureId'), 'team': team_id})
    return item


def _team_in_active_sprint(state, team_id):
    """True when `team_id` is tied to any ACTIVE sprint. WS-14: a team refines
    its backlog at sprint close, never mid-sprint -- the active sprint is the
    committed work, and refinement waits until the team is free."""
    for s in (state.get('sprints') or {}).values():
        if s.get('status') == 'active' and team_id in (s.get('teamIds') or []):
            return True
    return False


def _on_sprint_closed(state, record, now_ms):
    """WS-14 sprint-close ceremony, run the moment a sprint auto-closes: queue
    the team's START/STOP/CONTINUE retrospective (a ceremony on a later pass),
    pull the first eligible shared-backlog item for each team that sprint
    touched (first-come), and kick each team's backlog refinement so the scrum
    master re-plans at close. Any on-call backup who actually SERVED pages this
    sprint is rotated to the end of her team's on-call queue so she isn't paged
    again next sprint before the rotation catches up. Pure state mutation;
    nothing here blocks."""
    team_ids = [t for t in (record.get('teamIds') or []) if _team_row(state, t)]
    _rotate_served_oncalls(state, team_ids)
    retros = state.setdefault('pendingSprintRetros', [])
    if record['id'] not in retros:
        retros.append(record['id'])
    for tid in team_ids:
        # Cross-team borrowing: loans to this team end at sprint close unless
        # the team still has pending work (the feature-need case keeps them).
        _end_loans_for_team(state, tid, now_ms)
        for _ in range(BACKLOG_PULLS_PER_CLOSE):
            _pull_backlog_for_team(state, tid, now_ms)
        kick_refinement_now(state, tid, now_ms)
    # Director gap 1: team-health review at sprint close -- each director grades
    # the trailing health of the workers under them and coaches anyone below the
    # delivery floor (never directors). Pure, cheap, idempotent.
    _team_health_review(state, team_ids, now_ms)


# ---------------------------------------------------------------------------
# Phase E: products + wiki. A PRODUCT is a named, director-authored artifact
# with a spec/owner/contributors/revision log whose "release" freezes its
# sandbox repo into library/projects/<id>/v<N>/ and flips status to
# 'released'. Products + wiki are both SERVER-OWNED state (like teams/sprints)
# so the client's 5s autosave can never revert a release or a wiki edit.
# sim.py stays pure (no filesystem, no serve import at call site): these
# helpers mutate the state CATALOG; the serve.py wrapper does the actual
# sandbox snapshot + hashed-passport chaining around them. The revision CONTENT
# lives on disk; state[products][..]['revisions'] is the findable catalog.
# ---------------------------------------------------------------------------

PRODUCT_STATUSES = ('draft', 'in_progress', 'review', 'released')
def create_product(state, product_id, name, summary, spec, owner_id, sandbox_id,
                   team_id=None, contributor_ids=None, handles=None):
    """Create a product catalog entry. Pure: returns the record dict, or None
    if an id/owner/sandbox/name clash means nothing was created. Sandbox
    existence is validated by the CALLER (serve.py knows the real dirs); this
    just persists the association. Mutates state['products']."""
    products = state.setdefault('products', {})
    if product_id in products:
        return None
    name = (name or '').strip()
    if not name:
        return None
    record = {
        'id': product_id,
        'name': name,
        'summary': (summary or '').strip(),
        'spec': (spec or '').strip(),
        'ownerId': owner_id or None,
        'sandboxId': sandbox_id or None,
        'teamId': team_id or None,
        'contributorIds': list(contributor_ids or []),
        'handles': list(handles or []),
        'status': 'draft',
        'revisions': [],
        'nextRevision': 1,
    }
    products[product_id] = record
    return record


def next_product_revision(state, product_id):
    """Pure: the next (revision_no, relative_path) for a product, WITHOUT
    mutating. Serve.py lays the snapshot down at library/projects/<id>/<path>,
    then calls product_release_record to catalog it. Returns (n, relpath) or
    (None, None) for an unknown product."""
    record = (state.get('products') or {}).get(product_id)
    if not record:
        return None, None
    n = int(record.get('nextRevision') or 1)
    return n, f'v{n}'


def product_release_record(state, product_id, revision_no, released_by,
                           note=None, now_ms=None, target_path=None):
    """Pure: append a revision to a product's catalog and flip status ->
    'released'. Called by serve.py AFTER the sandbox snapshot landed on disk,
    so a filesystem/passport failure never half-catalogs. Returns the appended
    revision record, or None for an unknown product. Mutates state['products']."""
    import time as _time
    record = (state.get('products') or {}).get(product_id)
    if not record:
        return None
    now_ms = now_ms if now_ms is not None else int(_time.time() * 1000)
    rev = {
        'n': revision_no,
        'releasedBy': released_by or None,
        'at': now_ms,
        'note': (note or '').strip(),
        'path': target_path or f'v{revision_no}',
    }
    record.setdefault('revisions', []).append(rev)
    record['nextRevision'] = revision_no + 1
    record['status'] = 'released'
    return rev


def set_product_status(state, product_id, status):
    """Pure: transition a product between draft/in_progress/review. 'released'
    is NOT reachable here -- it only flips via product_release_record. Returns
    the updated record, or None for unknown product / invalid status."""
    if status not in PRODUCT_STATUSES or status == 'released':
        return None
    record = (state.get('products') or {}).get(product_id)
    if not record:
        return None
    record['status'] = status
    return record


# --- Wiki: structured, director-structurable knowledge ----------------------
# Pages carry category + version + history; the BODY is markdown written to
# library/wiki/<category>/<id>.md by serve.py. Reads are open; writes bump the
# version and append to history. state['wiki']['pages'] holds metadata only --
# never the body -- so the catalog stays lightweight and the merge stays safe.

WIKI_CATEGORY_ROOMS = {  # default category -> room affinity for read-before-act
    'workroom': 'pressoffice',
    'research': 'observatory',
    'ops': 'bank',
    'think_tank': None,
    'product': None,
}


def wiki_write_page(state, page_id, title, category, body, edited_by,
                    edited_at_ms=None, village_id=None, owner=None,
                    clock=SLOW_CLOCK, review_after_ms=None,
                    source=None, scope='team'):
    """Pure: create or update a wiki page's METADATA + return the lined-up
    record for serve.py to persist (serve writes page['body'] to disk only for
    pre-existing/new pages; the metadata carries version + history). Rejects
    too-long bodies and unknown categories. Returns (record, is_new).

    Memory-engineering fields: owner (who is accountable for the claim),
    clock (SLOW/FAST -- fast-clock pages must be re-fetched, not trusted),
    review_after_ms (expiry -- an expired page is no longer injected), source
    (where the claim can be verified), scope (who may reuse it). These are the
    review-card fields that make a stale or wrong memory findable and
    correctable."""
    import time as _time
    edited_at_ms = edited_at_ms if edited_at_ms is not None else int(_time.time() * 1000)
    categories = (state.get('wiki') or {}).get('categories') or {}
    if category not in categories:
        return None, False
    if clock not in MEMORY_CLOCKS:
        clock = SLOW_CLOCK
    body = body or ''
    if len(body) > 200_000:
        return None, False
    pages = ensure_wiki(state)
    prev = pages.get(page_id)
    version = (int(prev.get('version') or 0)) + 1 if prev else 1
    history = list((prev or {}).get('history') or [])
    if prev:
        history = history + [{'version': prev.get('version'), 'editedBy': prev.get('editedBy'),
                              'editedAt': prev.get('editedAt')}]
    record = {
        'id': page_id,
        'title': title or page_id,
        'category': category,
        'villageId': village_id or DEFAULT_VILLAGE,
        'clock': clock,
        'owner': owner or edited_by or None,
        'scope': scope,
        'source': source or None,
        'reviewAfterMs': int(review_after_ms) if review_after_ms else None,
        'version': version,
        'editedBy': edited_by or None,
        'editedAt': edited_at_ms,
        'history': history,
    }
    pages[page_id] = record
    return record, prev is None


def _page_room_affinity(state, page):
    """A task room matches a page's category via the default affinity map
    (overridable per-category in state), else the page is think tank/product-wide."""
    cat = (page or {}).get('category')
    mapping = (state.get('wiki') or {}).get('categoryRooms') or WIKI_CATEGORY_ROOMS
    return mapping.get(cat)


def wiki_read_pages(state, room, max_pages=3, village_id=DEFAULT_VILLAGE, now_ms=None):
    """Pure: the subset of wiki pages relevant to a task in `room`, chosen by
    category-room affinity AND the reader's village. Only pages affined to THIS
    room (or to the think tank as a whole -- a None affinity) AND written by
    THIS village are injected; a page affined to a different room, or from a
    different village, is never pulled in. "Read before acting" means reading
    the pages about the work you're about to do in your own village, not a
    random archive or a neighbor's memory. Returns metadata-only records
    (id/title/category/version/villageId/clock) so serve.py can fetch the
    bodies. Stable order (recency, then version) for deterministic injection.
    A page whose reviewAfterMs has passed is treated as expired: it is NOT
    injected as trusted context (the "recheck before acting" gate)."""
    pages = ensure_wiki(state)
    now_ms = now_ms if now_ms is not None else _time_ms()
    scored = []
    for page_id, rec in pages.items():
        if (rec.get('villageId') or DEFAULT_VILLAGE) != village_id:
            continue  # a different village's memory is not this reader's
        review_after = rec.get('reviewAfterMs')
        if review_after and int(review_after) < now_ms:
            continue  # expired -- do not inject as trusted knowledge
        affinity = _page_room_affinity(state, rec)
        if affinity != room and affinity is not None:
            continue  # affined to another room -> irrelevant here
        score = 0 if affinity == room else 1  # exact room beats think tank-wide
        scored.append((score, -(int(rec.get('editedAt') or 0)), page_id, rec))
    scored.sort()
    return [r for _s, _a, _pid, r in scored[:max_pages]]


def _time_ms():
    import time as _t
    return int(_t.time() * 1000)


def _page_is_expired(rec, now_ms):
    review_after = rec.get('reviewAfterMs')
    return bool(review_after and int(review_after) < now_ms)


def record_memory_provenance(state, actor, page_id, title, claim, source,
                             scope, clock, review_after_ms, village_id):
    """Pure: append a memory-write provenance entry to state['memoryLedger'].
    This is the audit trail (rari's external ledger) that answers "what did the
    team learn, where, who owns it, and when to recheck" -- independent of the
    wiki body so a stale or wrong memory can be found and corrected later.
    Best-effort; a ledger append never blocks the write."""
    entry = {
        'ts': _time_ms(),
        'actor': actor,
        'pageId': page_id,
        'title': title,
        'claim': (claim or '')[:2000],
        'source': source or None,
        'owner': None,
        'scope': scope or 'team',
        'clock': clock,
        'villageId': village_id,
        'reviewAfterMs': int(review_after_ms) if review_after_ms else None,
    }
    state.setdefault('memoryLedger', []).append(entry)
    return entry


def resolve_memory_fields(state, body, actor):
    """Resolve the memory-engineering fields (clock, owner, scope, source,
    review_after) for a wiki/taste write from a request body + the acting
    agent. Defaults: slow clock, actor as owner, team scope, default review
    window. Returns a dict of kwargs for the write."""
    import time as _t
    clock = (body.get('clock') or '').strip().lower()
    if clock not in MEMORY_CLOCKS:
        clock = SLOW_CLOCK
    owner = (body.get('owner') or '').strip() or actor
    scope = (body.get('scope') or '').strip() or 'team'
    source = (body.get('source') or '').strip() or None
    review_days = body.get('reviewAfterDays')
    if review_days is None:
        review_days = DEFAULT_REVIEW_DAYS
    try:
        review_days = max(0, int(review_days))
    except (TypeError, ValueError):
        review_days = DEFAULT_REVIEW_DAYS
    review_after_ms = int(_t.time() * 1000) + int(review_days * 24 * 3600 * 1000)
    return {'clock': clock, 'owner': owner, 'scope': scope,
            'source': source, 'review_after_ms': review_after_ms}


def inject_wiki_context(state, task, village_id=None, now_ms=None):
    """Pure: build the 'before you act, here's what the think tank knows' context
    block for a task, from the wiki pages for its room. Returns a non-empty
    string only when there ARE relevant pages; empty when the wiki has none
    (caller still runs the executor with a blank context).

    Expiry: a page whose reviewAfterMs has passed is skipped (recheck before
    acting). Two-clock: a FAST-clock page is still injected (so the agent knows
    the knowledge exists) but explicitly flagged to re-fetch the live source
    rather than trust the copy -- the "fast clock should be fetched" rule."""
    room = (task or {}).get('room')
    if not room:
        return ''
    now_ms = now_ms if now_ms is not None else _time_ms()
    if village_id is None:
        # Resolve the reader's village from the task's agent so a worker only
        # ever sees their own village's memory.
        village_id = village_of_agent(state, (task or {}).get('agentId'))
    page_ids = (task or {}).get('wikiPageIds')
    if page_ids:
        pages = [(ensure_wiki(state)).get(pid) for pid in page_ids
                 if (ensure_wiki(state)).get(pid)
                 and ((ensure_wiki(state)).get(pid).get('villageId') or DEFAULT_VILLAGE) == village_id
                 and not _page_is_expired((ensure_wiki(state)).get(pid), now_ms)]
    else:
        pages = wiki_read_pages(state, room, village_id=village_id, now_ms=now_ms)
    if not pages:
        return ''
    lines = ['The think tank knowledge base has these entries relevant to this work:', '']
    for rec in pages:
        line = f"- {rec.get('title')} (category: {rec.get('category')}, v{rec.get('version')})"
        if (rec.get('clock') or SLOW_CLOCK) == FAST_CLOCK:
            line += ' [LIVE DATA: re-check the current source before acting on this]'
        lines.append(line)
    return '\n'.join(lines) + '\n'


def release_uses_handle(state, product_id):
    """Pure: does this product's build carry a Phase D capability handle? The
    build executor uses it to call an external service without ever holding the
    raw secret. Returns the first handle (a nonce string) or None."""
    record = (state.get('products') or {}).get(product_id)
    if not record:
        return None
    handles = record.get('handles') or []
    return handles[0] if handles else None


def active_agent_count(state):
    """Port of tasks.js activeAgentCount: agents not off-duty (admins included)."""
    count = 0
    for aid, a in (state.get('agents') or {}).items():
        if a and not a.get('offDuty'):
            count += 1
    return count


def can_activate_another(state):
    """Port of tasks.js canActivateAnother."""
    return active_agent_count(state) < MAX_ACTIVE_AGENTS


def _awake_idle_count(state):
    """Port of tasks.js runTaskCycleBody._awakeIdleCount: non-admin, on-duty,
    not busy/task/pairWith/offDuty."""
    roster = state.get('agentRoster') or []
    agents = state.get('agents') or {}
    n = 0
    for d in roster:
        if d.get('isAdmin'):
            continue
        a = agents.get(d.get('id'))
        if a and not a.get('busy') and not a.get('task') and not a.get('pairWith') and not a.get('offDuty'):
            n += 1
    return n


def _any_available_including_off_duty(state):
    """Port of tasks.js runTaskCycleBody._anyAvailableIncludingOffDuty: a non-admin
    agent not busy/task/pairWith; if off-duty, only counts when the active ceiling
    has room to wake her."""
    roster = state.get('agentRoster') or []
    agents = state.get('agents') or {}
    for d in roster:
        if d.get('isAdmin'):
            continue
        a = agents.get(d.get('id'))
        if not a or a.get('busy') or a.get('task') or a.get('pairWith'):
            continue
        if a.get('offDuty'):
            if not can_activate_another(state):
                continue
        return True
    return False


def _eligible_candidates(state, include_off_duty):
    """Port of tasks.js assignTaskViaJev's candidate filter: non-admin, not
    busy/task/pairWith, and (unless include_off_duty) not off-duty.
    Returns list of agent ids in roster order (deterministic base for RR)."""
    roster = state.get('agentRoster') or []
    agents = state.get('agents') or {}
    out = []
    for d in roster:
        if d.get('isAdmin'):
            continue
        a = agents.get(d.get('id'))
        if not a or a.get('busy') or a.get('task') or a.get('pairWith'):
            continue
        if not include_off_duty and a.get('offDuty'):
            continue
        out.append(d['id'])
    return out


def appear_from_outskirts(state, agent_id, doors=None, grid=None):
    """Wake an off-duty agent ANYWHERE there's free, reachable space. An agent
    waking up appears at a random free outdoor spot reachable from SPAWN
    (pick_free_spot's reachability guarantee) -- the outskirts trailhead doors
    are gone, so she rests/wakes in the main think tank. `doors` is retained for
    call-compat (`_spawn_at_room_door` still prefers a room door for short
    reviewer walks); `grid` may be supplied or loaded lazily."""
    a = (state.get('agents') or {}).get(agent_id)
    if not a:
        return
    a['offDuty'] = False
    a['visible'] = True
    a['inRoom'] = None
    # An agent being woken is, by definition, newly ready for work: clear any
    # stale busy/task so a ghost (busy+offDuty, e.g. theo) can't wake up still
    # flagged busy -- which would make her visible but ineligible for assignment.
    a['busy'] = False
    a['task'] = None
    if grid is None:
        try:
            grid, _ = _load_outdoor_geometry()
        except Exception:
            grid = None
    if grid is not None:
        agents_map = state.get('agents') or {}
        occupied = [{'x': v['x'], 'y': v['y']} for v in agents_map.values()
                    if isinstance(v, dict) and v.get('visible') and v.get('id') != agent_id]
        spot = pick_free_spot(grid, avoid_points=occupied)
        a['x'], a['y'] = spot['x'], spot['y']


def _spawn_at_room_door(state, agent_id, room, agents, grid, doors):
    """Wake an off-duty agent (a gate reviewer) at the APPROACH of the target
    room's door, standing clear of the building edge -- the same spot assign_task
    walks an assigned agent to. Using the room's own door keeps the reviewer's
    walk short and reliable regardless of the think tank's reachability gaps. Falls
    back to appear_from_outskirts if the room has no door geometry."""
    a = (state.get('agents') or {}).get(agent_id)
    if not a:
        return
    door = (doors or {}).get(room)
    if not door:
        appear_from_outskirts(state, agent_id, doors)
        return
    a['offDuty'] = False
    a['visible'] = True
    a['inRoom'] = None
    a['busy'] = False
    a['task'] = None
    # Nudge each spawned reviewer a few px off the exact door center (deterministic
    # on her id) so the TWO reviewers of a gate don't land on the identical cell,
    # where neither can complete a walk-to due to self-collision.
    _off = AGENT_W + 8
    _nudge = sum(ord(ch) for ch in str(agent_id)) % 3
    a['x'] = door['x'] + door['w'] / 2 + _nudge * _off
    a['y'] = door['y'] + door['h'] + 4


def assign_task(state, agent_id, title, room, instructions, project_label,
                extra, grid, doors, now_ms, task_id_holder):
    """Port of tasks.js assignTask (deterministic -- no JEV, the chosen agent is
    passed in by the caller; this is the walk+bookkeeping half). Validates the
    agent/room, resolve overflow, finds a door-front path, commits the agent's
    task/path and the durable state['tasks'] mirror, logs task_assigned.
    Returns the task dict, or None on failure (-> caller requeues/attempt++).
    `task_id_holder` is a mutable init([count]) carrying the server's
    nextTaskId counter across ticks (server in-memory, like the JS module var)."""
    room = resolve_room_with_overflow(state, room)
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    door = (doors or {}).get(room)
    if not a or a.get('busy') or a.get('task') or not door:
        return None

    target_x = door['x'] + door['w'] / 2
    target_y = door['y'] + door['h'] + 4
    path = find_path(a['x'], a['y'], target_x, target_y, agent_id, agents, grid)
    if not path or len(path) == 0:
        return None

    task_id_holder[0] += 1
    task_id = 'task-' + str(task_id_holder[0])
    rest_x = path[-1]['x']
    rest_y = path[-1]['y']
    task = {
        'id': task_id,
        'title': title,
        'room': room,
        'instructions': instructions,
        'projectLabel': project_label,
        'research': (extra or {}).get('research'),
        'taskType': (extra or {}).get('taskType', 'code'),
        # A scheduled item's notBefore rides onto the task (see _assign_due_item)
        # so the preemption picker can recognize TIME-CRITICAL work: a card that
        # was scheduled for a specific time must not be yanked off the agent for
        # a one-off. Standing cadence cards already mark themselves via
        # research/skillReview/distill; notBefore covers queue_once one-offs.
        'notBefore': (extra or {}).get('notBefore'),
        'skillReview': bool((extra or {}).get('skillReview')),
        'distill': bool((extra or {}).get('distill')),
        'kbClass': (extra or {}).get('kbClass') or None,
        'dependsOn': (extra or {}).get('dependsOn') or None,
        'budgetMs': (extra or {}).get('budgetMs'),
        'sizeEstimate': (extra or {}).get('sizeEstimate'),
        'reviewOf': (extra or {}).get('reviewOf'),
        'reviewAuthorId': (extra or {}).get('reviewAuthorId'),
        # CS329A takeaway #2: checklist survives onto the task
        # object itself (see _assign_due_item's extra dict) -- the review
        # executor grades per-requirement from here.
        'checklist': list((extra or {}).get('checklist') or []),
        'incident': bool((extra or {}).get('incident')),
        'productId': (extra or {}).get('productId'),
        # Adversarial (winter) village tags (see queue_work / _assign_due_item)
        # -- stamped so serve._adversarial_village_tick can detect a side done.
        'adversarialTaskId': (extra or {}).get('adversarialTaskId'),
        'villageId': (extra or {}).get('villageId'),
        # A player-filed card's contract: the user story + acceptance criteria
        # ride onto the task so the coding executor's prompt includes the full
        # spec (content.py folds them into the backlog line it builds from
        # task.instructions).
        'userStory': (extra or {}).get('userStory'),
        'acceptanceCriteria': (extra or {}).get('acceptanceCriteria'),
        'pipelineStep': (extra or {}).get('pipelineStep'),
        # Bot Ops / shadow mode: a dry-run task (see queue_work). Stamped so the
        # completion path (_task_cycle) captures to the shadow ledger instead of
        # shipping.
        'shadow': bool((extra or {}).get('shadow')),
        # Item 4: the per-task model-spend ceiling, resolved AT ASSIGNMENT and
        # stamped onto the durable task. Explicit budgetUsd wins; else the task
        # type's tier; else the type default, scaled by the refinement band. The
        # /api/chat gate (serve.py) reads THIS number as the ceiling; the sim
        # loop's budgetExhausted branch is the enforcement that fails the card
        # closed + notifies the owning director. budgetUsd 0.0 = no ceiling.
        'budgetBand': (extra or {}).get('budgetBand') or 'standard',
        'budgetUsd': budget_usd_for_task(
            (extra or {}).get('taskType', 'code'),
            (extra or {}).get('budgetUsd'),
            (extra or {}).get('budgetBand')),
        # Bell-style spare-time lane: the protected-exploration flag (a free
        # spike's failures are never mined into operator rules).
        'moonshot': bool((extra or {}).get('moonshot')),
        # Attention lane (build/reading/open/parking-lot) -- see LANES. Rides
        # onto the durable task so the board can show it and the reading
        # rate-limit can count it as the room's one active slow thread.
        'lane': (extra or {}).get('lane'),
        # Player-authored provenance: True only when the PLAYER wrote this
        # task's work text (see queue_work / _assign_due_item). The JEV gate's
        # work-context bypass trusts only these tasks; an agent-authored task
        # must never be able to manufacture a bypass by naming a URL in its
        # own story text.
        'playerAuthored': bool((extra or {}).get('playerAuthored')),
        'assignedTo': agent_id,
        'status': 'walking',
        'createdAt': now_ms,
        'entryX': rest_x,
        'entryY': rest_y,
    }
    state.setdefault('tasks', {})[task_id] = task
    a['task'] = task_id
    a['path'] = path
    a['pathIndex'] = 0
    a['pathTarget'] = {'x': target_x, 'y': target_y}
    a['stuckTimer'] = 0
    a['replanCount'] = 0
    a['respawnedForTask'] = False
    from serve import log_action
    log_action(agent_id, 'task_assigned',
               {'taskId': task_id, 'title': title, 'room': room}, authorized=True)
    return task


def _cancel_at_task(state, agent_id):
    """Port of tasks.js cancelTask (tasks.js:1197): an agent whose task-walk is
    genuinely impossible gives up -- task marked 'cancelled', agent fully
    released (task/path/respawn cleared) so she goes idle and is available for
    the next assignment instead of looping forever on a route that can't work.
    This is the dispatch half of the movement 'cancel' event (previously logged
    and dropped, leaving the agent stuck -- vela, replanCount 55,350), and the
    terminal release for _repair_stalled_walkers' unreachable cases. Unlike
    finish_task/_release_agent_gated it does NOT bump approvals or record a
    completed room -- a gave-up walk is not shipped work. Mirrors the JS: the
    cancelled task is NOT requeued (a human can re-file it; requeueing an
    unwalkable task would just churn agents into the same wall). Idempotent,
    pure on `state`. Returns the cancelled task (or None)."""
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not a:
        return None
    task_id = a.get('task')
    tasks = state.get('tasks') or {}
    task = tasks.get(task_id) if task_id else None
    if task:
        task['status'] = 'cancelled'
    a['task'] = None
    a['path'] = None
    a['pathIndex'] = 0
    a['pathTarget'] = None
    a['stuckTimer'] = 0
    a['replanCount'] = 0
    a['respawnedForTask'] = False
    return task


def _arrive_at_task(state, agent_id, now=None):
    """Port of tasks.js arriveAtTask's positioning/lifecycle half (no per-room
    content dispatch -- that's the next slice, short-circuited via workUntil)."""
    now = time.time() if now is None else now
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not a:
        return
    task = (state.get('tasks') or {}).get(a.get('task'))
    a['path'] = None
    a['pathIndex'] = 0
    a['pathTarget'] = None
    a['stuckTimer'] = 0
    a['replanCount'] = 0
    a['respawnedForTask'] = False
    if not task:
        return
    a['visible'] = False
    a['busy'] = True
    a['inRoom'] = task['room']
    # Position at the room's first interactable, else room center -- ROOM_NATIVE
    # coordinates. First workstations interactable zone is the top-left desk's
    # front strip: {x:84, y:210, w:120, h:15} (desk0 {84,105,120,105} +
    # TERMINAL_ZONE_DEPTH=15). Mirrors tasks.js arriveAtTask's
    # ROOM_INTERACTABLES[collision][0].zone logic. Stage X: enough to be inside
    # the room at a desk; exact per-room desk data is data-only, not behavioral.
    zone_x, zone_w = 84, 120      # first workstations desk x/w (ROOM_COLLISIONS)
    zone_y_top = 105 + 105        # desk0.y + desk0.h  -> zone top = 210
    a['roomX'] = zone_x + zone_w / 2
    a['roomY'] = zone_y_top - 10
    a['dir'] = 'south'
    task['status'] = 'working'
    # Content dispatch. Phase 3 slice 2: for a room with a ported content
    # executor registered on the engine (research for now), fire the REAL async
    # work on a background thread and give it TASK_CONTENT_TIMEOUT_S rather than
    # the slice-1 placeholder budget; its completed result is merged by the next
    # task_cycle pass. Rooms without an executor keep the workUntil
    # short-circuit (an agent "works" the budget then completes -- the accepted
    # interim behavior, slice 1). Uses the SAME clock as the tick/completion
    # loop (caller's `now`): a mixed clock would never let a task complete.
    # `workUntil` is ALWAYS set: the content path installs its own (longer)
    # budget, everything else the short placeholder.
    dispatched = _dispatch_content_work(_content_executor, state, agent_id, task, now)
    if not dispatched:
        task['workUntil'] = now + TASK_WORK_DURATION_S


def _arrive_at_pair(state, agent_id, now=None):
    """W1: server-owned pair arrival. The movement engine already emits
    ('arrive','pair',aid) when a navigator holding `pairWith` reaches her
    path's end -- but the server dispatch dropped it (only ('arrive','task')
    resolved), so a pair session's navigator simply never joined the driver:
    she stood at the door-front forever, invisible to the park/heal sweeps
    (which skip pairWith holders). Mirrors the client's arriveAtPair: the
    navigator arrives at the shared workstation, becomes busy beside the
    driver, and the driver's task is what she rides along on. Idempotent,
    pure on `state`. Returns True when the pair session actually resolved."""
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not a:
        return False
    driver_id = a.get('pairWith')
    task_id = a.get('pairTaskId')
    task = (state.get('tasks') or {}).get(task_id) if task_id else None
    driver = agents.get(driver_id) if driver_id else None
    a['path'] = None
    a['pathIndex'] = 0
    a['pathTarget'] = None
    a['stuckTimer'] = 0
    a['replanCount'] = 0
    a['respawnedForTask'] = False
    if not driver or not task:
        # The session's other half is gone (driver released / task vanished);
        # release the navigator cleanly instead of parking her forever.
        _release_stalled_interaction(state, agent_id)
        return False
    a['visible'] = False
    a['busy'] = True
    a['inRoom'] = task.get('room')
    # Right beside the driver at the same desk -- mirrors arriveAtPair's own
    # "nav.roomX = driver.roomX + 25" convention.
    a['roomX'] = (driver.get('roomX') or 0) + 25
    a['roomY'] = driver.get('roomY') or 0
    a['dir'] = 'south'
    try:
        from serve import log_action
        log_action(agent_id, 'pair_arrived', {'taskId': task_id, 'driver': driver_id},
                   authorized=True)
    except Exception:
        pass
    return True


def _arrive_at_handoff(state, agent_id, now=None):
    """W1: server-owned handoff arrival. The movement engine already emits
    ('arrive','handoff',aid) when a walker holding `handoff` reaches her
    recipient -- but the server dispatch dropped it, so a client-started (or
    server-started) handoff walk arriving server-side was never delivered: the
    walker froze beside the recipient, invisible to the heal sweeps (which skip
    handoff holders). Mirrors the client's arriveAtHandoff's release half:
    the walker delivers the handoff (recorded, the finished-title line as the
    message), then clocks off duty. Idempotent, pure on `state`. Returns True
    when the handoff was actually delivered."""
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not a:
        return False
    handoff = a.get('handoff')
    a['path'] = None
    a['pathIndex'] = 0
    a['pathTarget'] = None
    a['stuckTimer'] = 0
    a['replanCount'] = 0
    a['respawnedForTask'] = False
    a['handoff'] = None
    a['dir'] = 'south'
    # Clocking off is what finishTask defers to let the handoff play out --
    # happens regardless of whether the recipient is still around.
    a['offDuty'] = True
    a['visible'] = False
    if not isinstance(handoff, dict):
        return False
    recipient_id = handoff.get('toId')
    title = handoff.get('title') or 'handoff'
    recipient = agents.get(recipient_id) if recipient_id else None
    # The delivered line is the finished-title (the client would run a model
    # call to phrase it; the server records the deterministic same fact, so the
    # dependency IS the message -- no invented phrasing).
    delivered = {
        'fromId': agent_id, 'toId': recipient_id, 'title': title,
        'ts': int((now if now is not None else time.time()) * 1000),
    }
    state.setdefault('handoffs', []).append(delivered)
    if recipient and isinstance(recipient, dict):
        recipient['contactedAt'] = int((now if now is not None else time.time()) * 1000)
    try:
        from serve import log_action
        log_action(agent_id, 'handoff_delivered',
                   {'to': recipient_id, 'title': (title or '')[:120]}, authorized=True)
    except Exception:
        pass
    return True




def _revoke_task_access(task_id):
    """Best-effort per-story capability-grant cleanup: when a story ships, the
    temp access grant tied to it dies with it. Mirrors _revoke_agent_credentials'
    lazy serve import so sim.py stays unit-testable offline; a DB failure must
    never block the completion path."""
    if not task_id:
        return
    try:
        import serve
        serve.revoke_task_access(task_id)
    except Exception:
        pass


def finish_task(state, agent_id, grid=None):
    """Port of tasks.js finishTask: clear task/busy/inRoom, return to entry point
    (with the full-agent-width step-aside deadlock avoidance), bump approvedCount,
    log. The caller then sends the agent off duty (send_agent_off_duty), like the
    JS finishTask does."""
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not a:
        return
    task = (state.get('tasks') or {}).get(a.get('task'))
    if task:
        task['status'] = 'done'
        # A landed task satisfies any dependency-block keyed on it: queue the
        # SM's auto-unblock so the cards that were waiting on this work free up.
        _auto_clear_dependency_blocks(state, task.get('id'),
                                      now_ms=int(time.time() * 1000))
    # W1: a PAIR task's navigator rides along on the driver's task -- when the
    # driver finishes, release her too (pairWith/pairTaskId/busy/inRoom) so she
    # doesn't sit busy at the shared desk forever after the work shipped.
    if task and task.get('pairWith'):
        _release_pair_navigator(state, task.get('pairWith'), task.get('id'))
    a['task'] = None
    a['busy'] = False
    a['inRoom'] = None
    a['visible'] = True
    a['approvedCount'] = (a.get('approvedCount') or 0) + 1
    a['weekApprovals'] = (a.get('weekApprovals') or 0) + 1
    # The story shipped: any per-story capability grant dies with it.
    _revoke_task_access(task.get('id') if task else None)
    _note_completed_room(state, agent_id, task)
    # Backlog refinement intake: the agent just shipped a real deliverable and
    # saw whether its room still has work. If the room is thinning, file a
    # follow-up work-request so the scrum master can turn the observed gap into
    # a story at the next ceremony.
    _maybe_file_followup(state, agent_id, task, int(time.time() * 1000))
    if task and task.get('entryX') is not None:
        ex, ey = task['entryX'], task['entryY']
        for oid, other in agents.items():
            if oid == agent_id:
                continue
            if other and other.get('visible') and other.get('x') == ex and other.get('y') == ey:
                ex += AGENT_W + 8
                break
        a['x'], a['y'] = ex, ey
    else:
        if grid is not None:
            occupied = [{'x': v['x'], 'y': v['y']} for v in agents.values()
                        if v.get('visible') and v.get('id') != agent_id]
            spot = pick_free_spot(grid, occupied, spawn=SPAWN,
                                  ground_w=GROUND_W, ground_h=GROUND_H)
        else:
            spot = {'x': SPAWN['x'], 'y': SPAWN['y']}
        a['x'], a['y'] = spot['x'], spot['y']
    from serve import log_action
    log_action(agent_id, 'task_completed', {'taskId': task['id'] if task else None,
                                            'title': task['title'] if task else None},
               authorized=True)


def send_agent_off_duty(state, agent_id, doors, grid):
    """Send an agent off duty. Refuse if busy/task/pair/handoff (safe to call on
    anyone). An agent VANISHES WHERE SHE STANDS -- the outskirts trailhead doors
    are gone, so resting is immediate and in-place. The `doors`/`grid` args are
    kept for call-compat but off-duty needs neither."""
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not a:
        return
    if a.get('busy') or a.get('task') or a.get('pairWith') or a.get('handoff'):
        return
    # Scheduled-item preemption: every task-completion path funnels here to go
    # off duty -- if this agent's own task was suspended so a due scheduled item
    # could fire, reclaim the parked task instead of resting (the scheduled
    # item just finished and handed the agent back).
    if a.get('_suspendedTask'):
        if _resume_suspended_task(state, agent_id, a, grid):
            return
    a['path'] = None
    a['pathIndex'] = 0
    a['pathTarget'] = None
    a['headingOffDuty'] = None
    a['stuckTimer'] = 0
    a['replanCount'] = 0
    a['offDuty'] = True
    a['visible'] = False


def _complete_shadow_task(state, agent_id, task, now_ms, grid=None):
    """Bot Ops / shadow mode completion: the dry-run task did its work, so capture
    its outcome to the append-only state['shadowLedger'] draft -- then release the
    agent WITHOUT any real-world side effect. Nothing ships: no peer gate, no
    approvedCount/weekApprovals bump, no completedDeliverables entry, no
    completedRooms/grade/runbook, no follow-up filing, no dependency unblock. The
    work happened; the world didn't move. Returns the ledger entry."""
    task['status'] = 'done'
    task['shadowDoneAt'] = now_ms
    entry = {
        'title': task.get('title'),
        'room': task.get('room'),
        'instructions': task.get('instructions'),
        'projectLabel': task.get('projectLabel'),
        'taskType': task.get('taskType'),
        'note': task.get('note'),
        'libraryPath': task.get('libraryPath'),
        'agentId': agent_id,
        'completedAt': now_ms,
        'promoted': False,
    }
    state.setdefault('shadowLedger', []).append(entry)
    a = (state.get('agents') or {}).get(agent_id)
    if a:
        a['task'] = None
        a['busy'] = False
        a['inRoom'] = None
        a['visible'] = True
    try:
        from serve import log_action
        log_action(agent_id, 'shadow_task_completed',
                   {'taskId': task.get('id'), 'title': task.get('title')}, authorized=True)
    except Exception:
        pass
    return entry


MIN_RESEARCH_CADENCE_MS = 5 * 60 * 1000  # floor: a misparsed "every second" can't spam the queue
MIN_PIPELINE_CADENCE_MS = 60 * 60 * 1000  # floor for an ordered pipeline: a misparsed "every second" can't spam the queue

# Bell-style long-horizon problem (2026-10-06): one standing research topic per
# village whose crawl cadence is far in the future -- the think tank always has
# something on the table that is NOT due this sprint, so the org keeps a horizon
# beyond the 6s tick and the weekly ceremonies. Configurable via .env
# (LONG_HORIZON_TOPIC / LONG_HORIZON_TOPIC_URL / LONG_HORIZON_CADENCE_DAYS);
# the defaults are a safe self-questioning frame.
LONG_HORIZON_TOPIC_DEFAULT = 'Re-examine our own assumptions: what does the think tank believe, and what would falsify it'
LONG_HORIZON_TOPIC_URL_DEFAULT = 'https://en.wikipedia.org/wiki/Falsifiability'
LONG_HORIZON_CADENCE_DAYS_DEFAULT = 30.0


def next_topic_id(state):
    """Server-side monotonic counter for researchTopics ids (topic-1, topic-2,
    ...), mirroring next_issue_key's cold-starts-at-1/never-collides shape.
    Topics aren't team-scoped, so this is one global counter, not per-prefix."""
    n = (state.get('researchTopicCounter') or 0) + 1
    state['researchTopicCounter'] = n
    return f'topic-{n}'


def add_research_topic(state, topic, start_url, cadence_ms, now_ms=None,
                       link_keyword=None, page_keyword=None, depends_on_task=None):
    """The creator side of the standing research-topic cadence: _check_schedules
    (below) has always been able to FIRE a due topic, but nothing ever appended
    one to state['researchTopics'] -- it was seeded empty at boot and never
    written to again. This is that missing write path.

    Fails closed rather than guessing: rejects an empty topic, a start_url that
    doesn't parse as a real absolute URL (scheme + host), and clamps cadence_ms
    to MIN_RESEARCH_CADENCE_MS so a bad interval can't turn into a queue-flood.
    `depends_on_task` (a task id) adds the dependency gate: the topic does not
    fire until that task reaches 'done' -- and its cadence marker is NOT
    advanced while it waits, so the crawl fires on the first pass after the
    dependency lands. Returns the new record, or None if rejected. `lastRunAt`
    starts at 0 so the first crawl fires on the very next _check_schedules pass,
    matching the intuitive "start checking X" request rather than waiting a
    full cadence."""
    topic = (topic or '').strip()
    start_url = (start_url or '').strip()
    if not topic or not start_url:
        return None
    parsed = urllib.parse.urlparse(start_url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None
    cadence_ms = max(int(cadence_ms or 0), MIN_RESEARCH_CADENCE_MS)
    record = {
        'id': next_topic_id(state),
        'topic': topic,
        'startUrl': start_url,
        'cadenceMs': cadence_ms,
        'lastRunAt': 0,
        'seenUrls': [],
        'linkKeyword': link_keyword or None,
        'pageKeyword': page_keyword or None,
        'dependsOnTask': depends_on_task or None,
    }
    state.setdefault('researchTopics', []).append(record)
    return record


def next_pipeline_id(state):
    """Server-side monotonic counter for pipelines (pl-1, pl-2, ...), mirroring
    next_topic_id's cold-starts-at-1/never-collides shape. Pipelines aren't
    team-scoped, so this is one global counter, not per-prefix."""
    n = (state.get('pipelineCounter') or 0) + 1
    state['pipelineCounter'] = n
    return f'pl-{n}'


def add_pipeline(state, name, cadence_ms, steps, now_ms=None, depends_on_task=None):
    """The creator side of an ORDERED pipeline: a named sequence of steps that
    fires in strict order, each step gated on its predecessor's completion (the
    player-facing scheduling lane -- see _check_pipelines below). This is the
    write path _check_schedules has always been able to FIRE for (the
    research-topic lane) but nothing ever appended for a multi-step sequence.

    Fails closed rather than guessing: rejects an empty name, a non-list or
    empty `steps`, and any step without a title/room, and clamps cadence_ms to
    MIN_PIPELINE_CADENCE_MS so a bad interval can't turn into a queue-flood.
    Each step may carry {title, room, offsetMs, instructions, tool, args};
    offsetMs is a minimum delay AFTER the previous step's completion (not from
    the run's start -- strict ordering dominates timing). `depends_on_task` (a
    task id) adds the dependency gate: the run does not START until that task
    reaches 'done' (a mid-run sequence keeps firing its remaining steps once it
    has started). Returns the new record, or None if rejected. `lastRunAt`
    starts at 0 so the first step fires on the very next _check_schedules pass."""
    name = (name or '').strip()
    if not name or not isinstance(steps, list) or not steps:
        return None
    clean_steps = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        title = (s.get('title') or '').strip()
        room = (s.get('room') or '').strip()
        if not title or not room:
            continue
        try:
            offset_ms = max(0, int(s.get('offsetMs') or 0))
        except (TypeError, ValueError):
            offset_ms = 0
        clean_steps.append({
            'title': title,
            'room': room,
            'offsetMs': offset_ms,
            'instructions': (s.get('instructions') or '').strip() or title,
            'tool': (s.get('tool') or '').strip() or None,
            'args': dict(s.get('args') or {}),
        })
    if not clean_steps:
        return None
    cadence_ms = max(int(cadence_ms or 0), MIN_PIPELINE_CADENCE_MS)
    record = {
        'id': next_pipeline_id(state),
        'name': name,
        'cadenceMs': cadence_ms,
        'lastRunAt': 0,
        'createdAt': now_ms if now_ms is not None else int(time.time() * 1000),
        'steps': clean_steps,
        'runId': 0,          # incremented at each run start; disambiguates the
                             # same stepIndex across runs (old done tasks persist)
        'runStepIndex': 0,   # next step index to fire in the current run
        'lastStepCompletedAt': None,
        'dependsOnTask': depends_on_task or None,
    }
    state.setdefault('pipelines', []).append(record)
    return record


_DEVICE_CHECKIN_HISTORY_MAX = 50  # bounded ring buffer -- no consumer yet, but never unbounded growth


def record_device_checkin(state, fields, now_ms=None):
    """Record one check-in from the player's phone (POST /api/device/checkin,
    serve.py). Pure state mutation, no consumer wired up yet --
    the whole point was landing a clean, generic ingestion pipe first and
    deciding what to build on top once data is actually flowing. `fields`
    is whatever the caller received (location/battery/focus/wifi/trigger
    today, deliberately open to more later); None values are dropped rather
    than stored as noise. Keeps only the last _DEVICE_CHECKIN_HISTORY_MAX
    entries -- state['deviceCheckins'] lives in the same hot kv_state blob
    every other piece of state does, so this must never grow unbounded."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    entry = {k: v for k, v in (fields or {}).items() if v is not None}
    entry['receivedAt'] = now_ms
    bucket = state.setdefault('deviceCheckins', {'last': None, 'history': []})
    bucket['last'] = entry
    history = bucket.setdefault('history', [])
    history.append(entry)
    if len(history) > _DEVICE_CHECKIN_HISTORY_MAX:
        del history[:-_DEVICE_CHECKIN_HISTORY_MAX]
    return entry


def _skill_review_has_pending():
    """Content gate for the standing skill-review sweep: is anything actually
    waiting under library/pending_review/skills/? Mirrors the executor's own
    determinism (content.py `_run_skill_review_content` lists /api/library and
    keeps paths whose first segment is pending_review/skills). When nothing is
    waiting, the 30-minute ceremony must not queue a task -- picking an agent
    and burning a Jev grade call against an empty queue is the waste this gate
    closes. Traversal stays under pending_review/skills/ only (review-trace/,
    the failed-review lessons, are included exactly as the executor includes
    them)."""
    import serve
    pending_root = os.path.join(serve.LIBRARY_DIR, 'pending_review', 'skills')
    if not os.path.isdir(pending_root):
        return False
    for _root, _dirs, filenames in os.walk(pending_root):
        if any(fn for fn in filenames if not fn.startswith('.')):
            return True
    return False


def _skill_review_in_flight(state):
    """Stagger gate for the standing skill-review ceremony: at most ONE
    skill-review task may be queued or in progress at a time, so the ceremony
    never stacks overlapping reviews for the whole think tank at once. Any
    queued work item carrying `skillReview`, or any live task (not 'done')
    carrying `skillReview`, blocks the next sweep."""

    def _active(t):
        return bool(t.get('skillReview')) and t.get('status') != 'done'

    for item in (state.get('workQueue') or []):
        if item.get('skillReview'):
            return True
    for t in (state.get('tasks') or {}).values():
        if _active(t):
            return True
    return False


def _distill_has_new_archives(since_ms):
    """Content gate for the standing distillation sweep: are there archive
    findings newer than `since_ms`? Mirrors the executor's own archive scan
    (content.py `_run_distill_content`: only plain files directly in
    LIBRARY_ARCHIVE_DIR, strictly newer than the last distillation). Nothing
    new since the last pass -> the ceremony stays quiet instead of re-picking
    an agent to fold nothing in."""
    import serve
    archive_dir = serve.LIBRARY_ARCHIVE_DIR
    if not os.path.isdir(archive_dir):
        return False
    try:
        for fn in os.listdir(archive_dir):
            full = os.path.join(archive_dir, fn)
            if not os.path.isfile(full):
                continue
            if int(os.path.getmtime(full) * 1000) > since_ms:
                return True
    except OSError:
        return False
    return False


def _ensure_long_horizon_topic(state, now_ms):
    """Bell-style long-horizon problem: seed ONE standing research topic per
    village whose crawl cadence is far in the future (LONG_HORIZON_CADENCE_DAYS),
    so the think tank always has something on the table that is NOT due this
    sprint -- a horizon beyond the 6s tick and the weekly ceremonies. Idempotent
    and self-cleaning: `state['longHorizonSeeded']` records which villages were
    seeded, so a topic deleted by the player is never silently re-created, while
    a partially-failed seed (bad .env URL) falls back to the safe defaults and is
    retried next pass. `lastRunAt` is seeded to now so the first crawl fires one
    full cadence out, not on the next pass. Pure state mutation, no Jev spend."""
    villages = state.get('villages') or []
    if not villages:
        return
    try:
        import serve as _serve_mod
        env = _serve_mod._load_env()
        topic = (env.get('LONG_HORIZON_TOPIC') or '').strip() or LONG_HORIZON_TOPIC_DEFAULT
        url = (env.get('LONG_HORIZON_TOPIC_URL') or '').strip() or LONG_HORIZON_TOPIC_URL_DEFAULT
        cadence_days = float(env.get('LONG_HORIZON_CADENCE_DAYS') or 0) or LONG_HORIZON_CADENCE_DAYS_DEFAULT
    except Exception:
        topic, url, cadence_days = (LONG_HORIZON_TOPIC_DEFAULT,
                                    LONG_HORIZON_TOPIC_URL_DEFAULT,
                                    LONG_HORIZON_CADENCE_DAYS_DEFAULT)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        url = LONG_HORIZON_TOPIC_URL_DEFAULT
    cadence_ms = max(int(cadence_days * 24 * 3600 * 1000), MIN_RESEARCH_CADENCE_MS)
    seeded = state.setdefault('longHorizonSeeded', {})
    for v in villages:
        vid = v.get('id') or DEFAULT_VILLAGE
        if seeded.get(vid):
            continue
        state.setdefault('researchTopics', []).append({
            'id': next_topic_id(state),
            'topic': topic,
            'startUrl': url,
            'cadenceMs': cadence_ms,
            'lastRunAt': now_ms,
            'seenUrls': [],
            'villageId': vid,
            'longHorizon': True,
        })
        seeded[vid] = True
        _log_governance(state, None, 'long_horizon_topic_seeded',
                        {'villageId': vid, 'topic': topic, 'cadenceDays': cadence_days})


def _check_schedules(state, now, now_ms):
    """Port of tasks.js checkResearchSchedule + checkSkillReviewSchedule: queue
    due standing work, stamping lastRunAt/lastSkillReviewAt BEFORE assignment so
    a due topic isn't re-picked. Mutates state['researchTopics'] in place.

    The two standing ceremonies (skill review + distillation) are ALSO content-
    gated: a due cadence only queues work when there is actually something to
    review/merge. With no content the marker is deliberately NOT advanced, so
    the sweep fires on the first later pass where content appears."""
    import serve
    # Research topics.
    # Long-horizon seeding first: a village's standing far-future topic is
    # created before due topics fire, so a fresh village always has its horizon
    # problem on the table from its first pass.
    _ensure_long_horizon_topic(state, now_ms)
    for topic in (state.get('researchTopics') or []):
        if now_ms - (topic.get('lastRunAt') or 0) < topic.get('cadenceMs', 0):
            continue
        # Composed schedule+dependency: a topic with a `dependsOnTask` waits for
        # that task to reach 'done'. The marker is deliberately NOT advanced
        # while it waits, so the crawl fires on the first pass AFTER the
        # dependency lands (same marker-held-while-gated rule as the content
        # gates below) instead of silently burning a cadence cycle.
        if topic.get('dependsOnTask') and not _dependency_landed(state, topic['dependsOnTask']):
            continue
        previous_run_at = topic.get('lastRunAt') or 0
        topic['lastRunAt'] = now_ms
        # The Research Desk's 24h overlap: search the period since the last
        # successful run MINUS the overlap, so a discovery that landed just
        # after the previous window is caught on this run instead of waiting a
        # full cadence. First run (previous_run_at 0) searches the whole
        # window. Mirrors evidence.search_since_ms / serve.RESEARCH_OVERLAP_MS.
        since_ms = serve.search_since_ms(previous_run_at,
                                         serve.RESEARCH_OVERLAP_MS)
        queue_work(state, [{
            'title': f"Scheduled research: {topic.get('topic')}",
            'room': 'observatory',
            'instructions': (f'Crawl starting from {topic.get("startUrl")} and update the '
                             f'"{topic.get("topic")}" skill file with anything genuinely new '
                             f'since last time (searching a 24h-overlapped window for late '
                             f'discoveries). Run three search passes and answer each one: '
                             f'announcements (what changed), practical examples (someone '
                             f'actually using it, with inspectable material), and limitations '
                             f'(documented restrictions, corrections, availability problems). '
                             f'Separate what a source supports from our inference. A repost is '
                             f'not a second independent source. Do not manufacture a finding '
                             f'when there is none.'),
            'goal': topic.get('topic'),
            'research': {'topicId': topic.get('id'), 'since': since_ms},
            'kbClass': 'changes_how_we_work',
        }])
    # Skill review sweep. `_cadence_due` treats the explicit CADENCE_NEVER marker
    # as never-due and normalizes any legacy far-future TEST sentinel (1e18) that
    # leaked into live state, so the sweep can neither be muted by a real date nor
    # disabled by a ghost of the old test macro. Content-gated: nothing waiting in
    # pending_review/skills/ -> no task queued and the marker NOT advanced (the
    # sweep fires on a later pass the moment content appears).
    if _cadence_due(state, 'lastSkillReviewAt', SKILL_REVIEW_CADENCE_MS, now_ms=now_ms) \
            and _skill_review_has_pending() and not _skill_review_in_flight(state):
        state['lastSkillReviewAt'] = now_ms
        queue_work(state, [{
            'title': 'Review pending skill files',
            'room': 'observatory',
            'instructions': 'Review whatever is waiting in pending_review/skills/ and decide, file by file, whether each one is accurate and worth keeping as real reference material.',
            'skillReview': True,
            'kbClass': 'changes_how_we_work',
        }])
    # Hive-mind distillation sweep. Same shape as the skill-review sweep: stamp
    # the marker BEFORE assignment (a due run mustn't be re-picked) and let the
    # task-cycle's idle gate decide whether the think tank is even working right
    # now (no spend when idle). `since` is the previous run's stamp, so the
    # executor only folds in findings archived after the last distillation.
    # Also content-gated: due + nothing new since the last pass -> quiet, marker
    # kept (a leaked legacy 1e18 TEST sentinel is normalized to 0 so it doesn't
    # poison the gate's comparison window and block the first real distillation).
    if _cadence_due(state, 'lastDistillAt', DISTILL_CADENCE_MS, now_ms=now_ms):
        previous_distill_at = state.get('lastDistillAt') or 0
        if previous_distill_at - now_ms > _CADENCE_LEGACY_FAR_FUTURE_MS:
            previous_distill_at = 0
        if _distill_has_new_archives(previous_distill_at):
            state['lastDistillAt'] = now_ms
            queue_work(state, [{
                'title': 'Distill recent think tank knowledge',
                'room': 'observatory',
                'instructions': ('Merge the findings archived since the last distillation into the '
                                 'think tank wiki, removing redundancy and extracting what the think tank '
                                 'now knows as a body.'),
                'distill': True,
                'distillSince': previous_distill_at,
                'kbClass': 'changes_how_we_work',
            }])

    # Ordered pipelines (the player-facing scheduling lane): fire the next step
    # of each due pipeline in strict sequence, gating step N+1 on step N's
    # completion. Stamps the run's cadence marker at the START of a run (not per
    # step) so a multi-step sequence gets its full cadence window before re-running.
    _check_pipelines(state, now_ms)

    # Director-gated player-ask / supervisor requirements-met sweeps. Unlike the
    # cadence markers above these run every _check_schedules pass: they spin a
    # PENDING gate (a blocked agent waiting on a verdict), which must resolve as
    # soon as its short window elapses -- not on a 30-minute cadence. Each no-ops
    # (returns None / False) when nothing is due, and only calls Jev when a gate
    # is actually old enough to decide.
    _pending_player_ask_sweep(state, now_ms)
    _supervisor_block_vote_sweep(state, now_ms)


def _pipeline_step_task(state, pipeline_id, run_id, step_index):
    """The live task mirror carrying this pipeline's step marker, or None.
    `run_id` disambiguates the same stepIndex across runs (old 'done' tasks
    persist in state, so a fresh run must not mistake a previous run's task for
    its own)."""
    for t in (state.get('tasks') or {}).values():
        marker = t.get('pipelineStep') or {}
        if (marker.get('pipelineId') == pipeline_id
                and marker.get('runId') == run_id
                and marker.get('stepIndex') == step_index):
            return t
    return None


def _check_pipelines(state, now_ms):
    """The creator-visible scheduled-pipeline sweep, ported into the same
    _check_schedules pass as the research-topic cadence. Each pipeline is an
    ORDERED sequence: step N+1 fires only after step N's task reaches 'done'
    (strict ordering -- a pending/active predecessor suppresses every later
    step, never skipped). Steps ride the normal queue_work -> assign -> content
    lifecycle; completion is read back from the durable task mirror by matching
    the pipelineStep marker each step was queued with.

    Cadence: `lastRunAt` is stamped at the START of a run (when step 0 fires,
    or when a completed run re-arms), so a multi-step pipeline gets a full
    cadenceMs window between runs -- the run start, not each step, advances the
    marker. `runStepIndex` tracks the next step to fire; a pipeline whose steps
    are all done waits out the remainder of the window.

    Offset: each step's offsetMs is a MINIMUM delay -- for step 0, from the
    run's start; for every later step, after the PREVIOUS step's completion
    (tracked in `lastStepCompletedAt`). Strict ordering dominates timing -- a
    large offset can delay a later step, but never lets it leapfrog an
    unfinished predecessor."""
    for p in (state.get('pipelines') or []):
        steps = p.get('steps') or []
        if not steps:
            continue
        cadence_ms = max(p.get('cadenceMs') or 0, MIN_PIPELINE_CADENCE_MS)
        step_index = p.get('runStepIndex') or 0
        run_id = p.get('runId') or 0
        # Composed schedule+dependency: a pipeline with a `dependsOnTask` gates
        # at the RUN BOUNDARY only -- a never-started pipeline (no lastRunAt)
        # and a completed run waiting to re-arm both hold until the dependency
        # lands (no marker advance, so the run fires on the first pass after it
        # does). A MID-RUN pipeline keeps firing its remaining steps: the
        # dependency gates the run's start, not its tail.
        if p.get('dependsOnTask') and not _dependency_landed(state, p['dependsOnTask']) \
                and (not p.get('lastRunAt') or step_index >= len(steps)):
            continue
        # Pipeline run complete (all steps fired) and waiting out the cadence
        # window before the next run -- idle, no work.
        if step_index >= len(steps):
            if now_ms - (p.get('lastRunAt') or 0) < cadence_ms:
                continue
            # Re-arm the next run: advance the run counter so this run's step
            # markers can never be confused with a previous run's 'done' tasks.
            run_id += 1
            p['runId'] = run_id
            p['runStepIndex'] = 0
            p['lastRunAt'] = now_ms
            p['lastStepCompletedAt'] = None
            step_index = 0
        else:
            # Running (or about to begin). A never-started pipeline is due
            # immediately: stamp the run's start (and its run id) so step 0's
            # own offset floor is measured from now, not from the epoch.
            if not p.get('lastRunAt'):
                p['lastRunAt'] = now_ms
            if not run_id:
                run_id += 1
                p['runId'] = run_id
        # The current step may already be queued/active (queued last pass).
        task = _pipeline_step_task(state, p.get('id'), run_id, step_index)
        if task is not None:
            if task.get('status') != 'done':
                continue  # predecessor in flight -> hold the sequence
            # Done: advance past this step and start the next one's offset
            # clock from this completion.
            step_index += 1
            p['runStepIndex'] = step_index
            p['lastStepCompletedAt'] = now_ms
            if step_index >= len(steps):
                continue  # run complete; next pass gates the cadence window
            task = _pipeline_step_task(state, p.get('id'), run_id, step_index)
        if task is not None:
            continue  # next step already queued/active
        step = steps[step_index]
        # Offset floor: step 0 from the run's start, later steps from the
        # predecessor's completion.
        base_at = p.get('lastRunAt') if step_index == 0 else (p.get('lastStepCompletedAt') or p.get('lastRunAt'))
        if now_ms - (base_at or now_ms) < (step.get('offsetMs') or 0):
            continue
        queue_work(state, [{
            'title': f"[{p.get('name')}] {step.get('title')}",
            'room': step.get('room'),
            'instructions': step.get('instructions') or step.get('title'),
            'pipelineStep': {'pipelineId': p.get('id'), 'runId': run_id, 'stepIndex': step_index},
        }])


# ---------------------------------------------------------------------------
# Phase 4: server-owned governance (auto-hire + auto-firing review).
#
# The browser used to run two setInterval loops (index.html main():
# attemptAutoHire from hiring.js, attemptAutoFiringReview/finishFiringReview
# from firing.js). With no browser open those governance decisions -- who to
# hire for, who to fire -- silently died. This section re-homes them into a
# single cadence pass, `_governance_pass`, folded into the SAME read-modify-
# write as _task_cycle (called from _task_cycle, so it inherits the server-
# owner gate, the think_tank_has_work idle gate, and the process's single pass).
# Pure state mutation only; the Jev decision is resolved through an injectable
# `_governance_decider` so sim.py stays unit-testable with zero network.
# ---------------------------------------------------------------------------
def reports_about(state, about_id):
    """Port of reports.js reportsAbout: all reports filed against an agent."""
    return [r for r in (state.get('reports') or []) if r.get('aboutId') == about_id]


def morale_for(state, agent_id, now_ms=None):
    """Port of morale.js moraleFor -- a pure, 0-100 score. Reads the agent's
    approved/dropped/neglect signals off the durable state so future
    passes re-derive it (no stored score to go stale). Mirrors the reference
    video: the meter reacts to approved work, dropped work, and being spoken
    to -- NOT to reports/notes (those feed firing review, not morale).
    `now_ms` injectable for deterministic tests (falls back to wall-clock)."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    a = (state.get('agents') or {}).get(agent_id)
    if not a:
        return None
    approved_bonus = min((a.get('approvedCount') or 0) * MORALE_APPROVED_WEIGHT, MORALE_APPROVED_CAP)
    days_since_hire = days_since(a.get('hiredAt'), now)
    drop_decay = max(0.0, 1 - days_since_hire / MORALE_DROPPED_DECAY_DAYS)
    dropped_penalty = (a.get('droppedCount') or 0) * MORALE_DROPPED_WEIGHT * drop_decay
    last_contacted = a.get('lastContactedAt')
    neglect_penalty = (min(days_since(last_contacted, now) * MORALE_NEGLECT_WEIGHT, MORALE_NEGLECT_CAP)
                       if last_contacted else MORALE_NEGLECT_CAP)
    raw = 100 + approved_bonus - dropped_penalty - neglect_penalty
    return int(max(0, min(100, round(raw))))


def _governance_decider_default(state, instructions, candidates):
    """Default Jev resolver for governance, late-importing serve.py so sim.py
    stays importable/unit-testable offline. `candidates` is a list of
    {id, description}; returns a choice id (or None on a Jev outage)."""
    import serve
    try:
        data = serve._call_openrouter_decision_sync(
            serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice', 'instructions': instructions,
                        'criteria': {c['id']: c['description'] for c in candidates}}})
        choice, _, _ = serve._jev_choice(data)
        return choice if any(c['id'] == choice for c in candidates) else None
    except Exception:
        return None


# Injectable so tests substitute a deterministic decider; the live loop uses
# the default. Mirrors how _content_executor is injected.
_governance_decider = _governance_decider_default


def _senior_director(state):
    """firing.js firingReviewers: the senior-most director -- a roster entry
    that isDirector, not the admin, and has no `director` of its own (the top
    of the chain that stands in for the admin on approvals)."""
    roster = state.get('agentRoster') or []
    for d in roster:
        if d.get('isDirector') and not d.get('isAdmin') and not d.get('director'):
            return d
    return None


def _firing_reviewers(state):
    admin = None
    for d in (state.get('agentRoster') or []):
        if d.get('isAdmin'):
            admin = d
            break
    senior = _senior_director(state)
    if not admin or not senior:
        return []
    return [admin, senior]


def _review_is_stale(state, agent_id, now_ms):
    """firing.js reviewIsStale: a previous 'keep' verdict skips re-reviewing
    the EXACT same evidence until morale or the report count actually move."""
    a = (state.get('agents') or {}).get(agent_id)
    last = a and a.get('lastFiringReview')
    if not last or last.get('verdict') != 'keep':
        return False
    same_morale = morale_for(state, agent_id, now_ms) == last.get('morale')
    same_reports = len(reports_about(state, agent_id)) == last.get('reportCount')
    return same_morale and same_reports


def _has_firing_signal(state, agent_id, now_ms=None):
    """Firing keys off a real, corroborated personnel
    problem, NOT the composite morale score. Morale folds in neglect (never
    contacted = low score) and approved-work noise that say 'send this agent
    help / they might be underutilized' -- the hiring/load-spreading concern --
    but are NOT grounds to fire someone. The legitimate firing signals are
    (a) two serious/severe negative reviews that landed INSIDE the strike
    grace window (REPORT_STRIKE_WINDOW_MS), or a single severe one -- their
    peers flagged a real problem, and it's recent enough to matter -- and (b) a
    true drop-off -- they were handed work and dropped more of it than a third
    of what they approved (falling behind, not loafing). A lone negative review
    is not a signal: it may expire out of the window, and it is never grounds
    to convene a firing review by itself."""
    a = (state.get('agents') or {}).get(agent_id)
    if not a:
        return False
    strikes = _report_strikes(state, agent_id, now_ms)
    if strikes['count'] >= 2 or strikes['severe'] >= 1:
        return True
    dropped = a.get('droppedCount') or 0
    approved = a.get('approvedCount') or 0
    return dropped > approved * 0.3


def _firing_signal_strength(state, agent_id, now_ms=None):
    """Comparable strength of an agent's firing signal for who_needs_review:
    (severity-weighted strike count within the grace window, overload).
    Severe=3, serious=2 per strike; overload = dropped / (approved + 1), the
    same ratio _has_firing_signal thresholds at 0.3. Primary sort = the report
    evidence, secondary = how far behind they've fallen -- so a
    reported+overloaded agent beats a report-only one, and an unreported
    overload alone ranks below any real report. 'minor'/'major' reports carry
    no firing weight (see _report_strikes)."""
    strikes = _report_strikes(state, agent_id, now_ms)
    score = strikes['severe'] * 3 + (strikes['count'] - strikes['severe']) * 2
    a = (state.get('agents') or {}).get(agent_id) or {}
    dropped = a.get('droppedCount') or 0
    approved = a.get('approvedCount') or 0
    return score, dropped / (approved + 1)


def who_needs_review(state, now_ms):
    """firing.js whoNeedsReview: the worker (non-admin, non-director) with the
    strongest firing signal -- a negative report or a real drop-off -- who is
    not already settled. Re-keyed OFF the raw morale score (a
    neglected-but-otherwise-fine agent is a hiring/help target, not a firing
    one); see _has_firing_signal. Among multiple signal-holders the review
    targets the strongest evidence first (_firing_signal_strength), not roster
    order. Same idle guard -- never pick someone mid-task/pair/handoff, so a
    fire can't strand a live collaboration ref."""
    best = None
    best_strength = None
    for d in (state.get('agentRoster') or []):
        if d.get('isAdmin') or d.get('isDirector'):
            continue
        if _review_is_stale(state, d.get('id'), now_ms):
            continue
        a = (state.get('agents') or {}).get(d.get('id'))
        if not a or a.get('busy') or a.get('task') or a.get('pairWith') or a.get('handoff'):
            continue
        if not _has_firing_signal(state, d.get('id'), now_ms):
            continue
        strength = _firing_signal_strength(state, d.get('id'), now_ms)
        if best is None or strength > best_strength:
            best = d
            best_strength = strength
    return best


def _firing_consultation(state, candidate_id):
    """firing.js firingConsultation: the people with real firsthand knowledge of
    the candidate -- distinct agents who filed reports against them, plus anyone
    currently paired with them or handing off work to them."""
    reports = reports_about(state, candidate_id)
    roster = state.get('agentRoster') or []
    agents = state.get('agents') or {}
    reported_by = set(r.get('fromId') for r in reports if r.get('fromId') and r.get('fromId') != 'player')
    reporters = []
    for rid in reported_by:
        rdef = next((x for x in roster if x.get('id') == rid), None)
        own = [r for r in reports if r.get('fromId') == rid]
        reporters.append({
            'id': rid, 'name': (rdef or {}).get('name') or rid,
            'quote': ' | '.join(r.get('quote') or '' for r in own),
            'note': ' | '.join(r.get('note') or '' for r in own),
            'severity': ','.join(r.get('severity') or 'unclassified' for r in own),
        })
    coworkers = []
    for other in roster:
        if other.get('id') == candidate_id:
            continue
        o = agents.get(other.get('id'))
        if not o:
            continue
        if o.get('pairWith') == candidate_id or o.get('handoff') == candidate_id:
            coworkers.append({'id': other.get('id'), 'name': other.get('name') or other.get('id')})
    return {'reporters': reporters, 'coworkers': coworkers}


def _is_negative_severity(severity):
    s = (severity or '').lower()
    return 'severe' in s or 'serious' in s or 'major' in s


def _is_strike_severity(severity):
    """A report severity that COUNTS toward the two-strike firing bar. Only
    serious/severe -- real harm or real misconduct. 'minor' (no harm) and
    'major' (friction, underperformance) never count as a strike, so small
    things can't snowball into a firing; underperformance is a drop-off signal
    and a help target, not a strike."""
    s = (severity or '').lower()
    return 'severe' in s or 'serious' in s


def _report_strikes(state, agent_id, now_ms=None):
    """The firing-relevant review evidence for an agent, windowed: serious and
    severe negative reports that landed INSIDE REPORT_STRIKE_WINDOW_MS of the
    moment the evidence is weighed. A report without a `ts` is treated as fresh
    (state-seeded reports and pre-window records both count), so a hard fire
    never slips through a missing timestamp. Returns {'count', 'severe',
    'freshest'} where 'count' is the total strikes in the window and 'severe'
    the subset that were severe. `now_ms` injectable for deterministic tests."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    strikes = []
    severe = 0
    freshest = 0
    for r in reports_about(state, agent_id):
        if not _is_strike_severity(r.get('severity')):
            continue
        ts = r.get('ts')
        if ts is not None and now - ts > REPORT_STRIKE_WINDOW_MS:
            continue
        strikes.append(r)
        if _is_severe(r.get('severity')):
            severe += 1
        freshest = max(freshest, ts or 0)
    return {'count': len(strikes), 'severe': severe, 'freshest': freshest}


def _is_severe(severity):
    s = (severity or '').lower()
    return 'severe' in s


def _consultation_blocks_firing(state, candidate_id, now_ms):
    """firing.js consultationBlocksFiring: two independent guardrails against
    premature firing -- an active collaborator, or no corroborated strike
    evidence inside the grace window (a severe strike alone justifies; two
    serious/serious+ strikes are required otherwise)."""
    info = _firing_consultation(state, candidate_id)
    if info['coworkers']:
        return True, info
    strikes = _report_strikes(state, candidate_id, now_ms)
    if not strikes['count']:
        return True, info
    if strikes['severe']:
        return False, info
    return strikes['count'] < 2, info


def _fire_decision(state, reviewers, candidate_def, now_ms, decider=None):
    """firing.js finishFiringReview's Jev verdict + deterministic fallback:
    'fire' or 'keep'. Pure decision; the consultation + idle guards are applied
    by the caller around it. `decider` injectable for tests (defaults to Jev)."""
    candidate = (state.get('agents') or {}).get(candidate_def.get('id'))
    morale = morale_for(state, candidate_def.get('id'), now_ms)
    reports = reports_about(state, candidate_def.get('id'))
    report_quotes = (' | '.join(f'"{r.get("quote")}" -- {r.get("note")} (severity: {r.get("severity") or "unclassified"})'
                                for r in reports)) or 'none filed'
    info = _firing_consultation(state, candidate_def.get('id'))
    consultant_line = (
        ' | '.join([*(f'{r.get("name")} (reported them: "{r.get("quote")}"'
                     f'{(" -- " + r.get("note")) if r.get("note") else ""})' for r in info['reporters']),
                    *(f'{c.get("name")} (currently working directly with {candidate.get("name")})'
                      for c in info['coworkers'])]) or 'no one reported them and no one is currently working directly with them')
    reviewer1, reviewer2 = reviewers
    strikes = _report_strikes(state, candidate_def.get('id'), now_ms)
    window_days = REPORT_STRIKE_WINDOW_MS // (24 * 3600 * 1000)
    instructions = (f'{reviewer1.get("name")} and {reviewer2.get("name")} are jointly reviewing '
                    f'{candidate.get("name")}\'s ({candidate.get("role")}) performance. '
                    f'{reviewer1.get("name")} is the admin; {reviewer2.get("name")} is the senior-most '
                    f'director standing in for the admin. Morale score: {morale}/100. Approved work: '
                    f'{candidate.get("approvedCount")}. Dropped work: {candidate.get("droppedCount")}. '
                    f'Reports filed against them: {report_quotes}. '
                    f'Firing-signal strikes inside the {window_days}-day grace window: {strikes["count"]} '
                    f'({strikes["severe"]} severe). '
                    f'{candidate.get("name")}\'s own manager has '
                    f'already been notified of these reports. People consulted who have worked with or reported '
                    f'{candidate.get("name")}: {consultant_line}. Decide whether to fire them or keep them on.')
    candidates = [
        {'id': 'fire', 'description': f"End {candidate.get('name')}'s role in the think tank -- performance does not justify keeping them on, and the people who work with them don't outweigh the evidence."},
        {'id': 'keep', 'description': f"Keep {candidate.get('name')} on -- performance is acceptable, improving, or the evidence or the people who work with them don't support firing."},
    ]
    decision = (decider(state, instructions, candidates) if decider
                else _governance_decider(state, instructions, candidates))
    if not decision:
        # Jev-outage fallback re-keyed off the firing signal, not
        # the raw morale score. Firing requires a real corroborated problem:
        # a negative report AND a genuine drop-off (handed work, dropped more
        # than a third of what they approved). Either alone stays 'keep' --
        # a report-only or drop-off-only agent is a help/hiring target, not a
        # firing one (see _has_firing_signal).
        strikes = _report_strikes(state, candidate_def.get('id'), now_ms)
        has_report = strikes['count'] >= 2 or strikes['severe'] >= 1
        dropped = candidate.get('droppedCount') or 0
        approved = candidate.get('approvedCount') or 0
        decision = ('fire' if has_report and dropped > approved * 0.3
                    else 'keep')
    return decision, morale


def _sim_direct_reports(state, director_id):
    """Direct reports of a director, from the `director` pointer already on
    every roster entry (purely structural -- no serve.py import needed)."""
    return [d.get('id') for d in (state.get('agentRoster') or [])
            if d.get('director') == director_id]


# Peer-approval gate (Phase E addendum): a deliverable-room task's story is not
# 'done' when its primary work finishes -- it moves to 'needs_review' and waits
# for TWO distinct same-team peers to approve it. Only rooms with real content
# executors gate; chores complete directly.
PEER_REVIEW_TIMEOUT_MS = 15 * 60 * 1000  # sim-ms: waiting window after which one clean vote suffices
# Phase E3.1: stuck-at-review watchdog. A gated story's reviewer pool is locked at
# entry; the watchdog guarantees that pool stays capable of producing a vote (or
# widens it), so a story can't deadlock on an unreachable/inert locked pair.
STUCK_GATE_CADENCE_MS               = 20_000
STUCK_GATE_GRACE_MS                 = 5 * 60 * 1000   # no widening before this age
STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS = 30 * 60 * 1000  # min gap between re-picks
# State-blob pruning: the whole kv_state blob is autosaved every ~1s, so
# unbounded append-only structures make that write slower and slower. The
# prune pass bounds the historical TAIL of each (live/open/pending entries are
# always kept); `tasks` is intentionally left alone (it backs per-agent history).
_STATE_PRUNE_CADENCE_MS = 60 * 60 * 1000  # hourly
_TAIL_COMPLETED_DELIVERABLES = 500
_TAIL_BACKLOG_RESOLVED = 200
_TAIL_PLAYER_INBOX_ANSWERED = 100
_TAIL_ISSUES_CLOSED = 100
_TAIL_GROWTH_PLANS_APPLIED = 50
# Non-gated work lanes (Phase E2b/E2d): a SPIKE is a time-boxed investigation
# with no committed deliverable; a BUG is incident response on a live product
# (see the on-call phase). Neither ships a peer-reviewed story -- both land a
# short artifact instead.
NON_GATED_LANES = frozenset({'spike', 'bug'})


def _peer_gated_lane(task):
    """True when a task's deliverable work should wait for peer approval -- i.e.
    it's in a deliverable room AND it's a normal (non-spike/bug) lane. A
    spike/bug in a deliverable room completes directly (its author is released
    but no gate is opened; nothing to vote on).

    skillReview/distill/research are standing housekeeping sweeps
    (_check_schedules), not authored stories -- there is no real "fix" for a
    reviewer to send back against "review whatever is in
    pending_review/skills/" or "crawl this site and update its skill file",
    so a reject just spawns a fix subtask whose completion unconditionally
    re-arms the SAME gate (_task_cycle's reviewOf/fix branch has no
    _peer_gated_lane check, unlike the initial-entry branch).
    One skill-review sweep, routed through this same gate as
    real coding/research work, looped reviewer-reject/re-fix for over 20
    minutes straight, flooding the workQueue with 50+ duplicate 'Review:
    Review pending skill files' entries and sending the same handful of
    agents back to the observatory door over and over -- exempted then, but
    a scheduled research crawl (task['research'], also queued by
    _check_schedules, also landing in the observatory -- a deliverable room)
    was missed. It recurred: a single "AI regulation
    news" schedule spiraled into 3,917 task assignments and 26,687
    escalations over one evening -- review found the crawl "actionable" (a
    scheduled crawl obviously has no passing flake8/mypy/pytest-cov suite to
    fail cleanly), queued a code fix for something that was never a coding
    deliverable, that fix's own review found it just as unfixable, forever.
    Exempting all three here (same shape as NON_GATED_LANES) stops them from
    ever entering the gate at all, so there is no vote to reject and nothing
    to re-arm."""
    if task.get('skillReview') or task.get('distill') or task.get('research'):
        return False
    return _deliverable_room(task.get('room')) and task.get('taskType') not in NON_GATED_LANES


def _pick_reviewer_ids(state, author_id, task_room=None, preferred=None):
    """Two reviewers for a gate. Under Phase E2 cross-assignment the author may
    pick up work outside her own team, so the ideal reviewer is one who shares
    the author's team OR has actually completed work in the story's target room
    (the known-craft signal, `completedRooms` -- see _note_completed_room).
    Candidate order = the author's team first, then every non-admin non-author
    agent. Prefer (0) on-team OR room-contributor over a stranger, (1) same-team,
    (2) room-contributor, (3) idle, (4) stable roster order. Same-team members
    still win the first two slots when the team can supply them. Never returns
    duplicate or author reviewers.

    `preferred`: a prior reviewer-pair (e.g. from a rejected gate). A rejection
    is a request to *verify the flagged problem was actually addressed*, which a
    stranger cold-reviewing cannot do -- so the rejecting reviewers are kept at
    the head of the candidate order. If they're gone/unreachable the pick falls
    back to the general widening below."""
    agents = state.get('agents') or {}
    roster = state.get('agentRoster') or []
    author_roster = next((d for d in roster if d.get('id') == author_id), None)
    team = []
    if author_roster and author_roster.get('director'):
        team = _sim_direct_reports(state, author_roster['director'])
    # Candidates = preferred pair first, then team (excl author), then widen to
    # all non-author.
    order = list(preferred or []) + team + [d.get('id') for d in roster if not d.get('isAdmin') and d.get('id') != author_id]
    seen = set()
    ordered_ids = []
    for cid in order:
        # A candidate whose agent record was deleted is a ghost -- never enqueue
        # a review to an id that can't actually review (this can otherwise leak
        # in from the team/widen portions too, not just `preferred`).
        if cid != author_id and cid not in seen and agents.get(cid):
            seen.add(cid)
            ordered_ids.append(cid)
    team_set = set(team)

    def _idle_key(cid):
        a = agents.get(cid) or {}
        cid_room_contrib = bool(task_room) and task_room in (a.get('completedRooms') or [])
        return (0 if preferred and cid in preferred else 1,  # prior reviewers stay at the head
                1 if cid not in team_set and not cid_room_contrib else 0,  # stranger loses
                0 if cid in team_set else 1,                               # same-team first
                0 if cid_room_contrib else 1,                              # known-craft next
                0 if a.get('offDuty') else 1,
                a.get('busy') is True,
                roster_order(cid, roster))
    ordered_ids.sort(key=_idle_key)
    if not preferred:
        # Bell-style cross-pollination (2026-10-06): when the tank can supply
        # both an on-team AND an off-team reviewer, force the pair to span
        # teams -- a reviewer who doesn't share the author's team's normalized
        # assumptions catches what the team itself has learned to ignore.
        # Off-team means a worker reporting to a DIFFERENT team director than
        # the author's (the author's own director standing in is not
        # cross-pollination, and a bare director with no team of their own is
        # not either). Falls back to the pure quality ordering when the pool
        # can't span (a tiny think tank) or when re-using a prior pair (a
        # rejection must be re-verified by the same reviewer who flagged it).
        team_directors = {t.get('directorId') for t in (state.get('teams') or []) if t.get('directorId')}
        author_director = (author_roster or {}).get('director')
        off_team = []
        for c in ordered_ids:
            if c in team_set:
                continue
            cd = next((d.get('director') for d in roster if d.get('id') == c), None)
            if cd and cd != author_director and cd in team_directors:
                off_team.append(c)
        same_team = [c for c in ordered_ids if c in team_set]
        if same_team and off_team:
            return [same_team[0], off_team[0]]
    return ordered_ids[:2]


def roster_order(cid, roster):
    for i, d in enumerate(roster):
        if d.get('id') == cid:
            return i
    return 0


def _enter_peer_review(state, task, now_ms, preferred=None):
    """Move a deliverable task whose primary content work just finished into the
    peer-approval gate: status -> 'needs_review', pick two reviewers (see
    _pick_reviewer_ids -- one is forced cross-team when the tank can supply it),
    record the gate (approval counter + reviewer pair), and notify each reviewer
    (the 'tell your teammates' step, server-side at the trigger instant). Returns
    the _peerGate dict (or None if the task is misconfigured).

    Adversarial split (2026-10-06): the FIRST reviewer is designated the CRITIC
    -- instructed to prove the work wrong, not rubber-stamp it -- so a gate
    approval means the work survived hostile scrutiny, and a rubber-stamp pair
    can no longer pass a weak deliverable on 2 clean votes. The other reviewer
    keeps the standard skeptical-review framing. Same spend, stronger signal.

    `preferred` (prior reviewer pair): a re-open (reviewer rejection of the fix,
    or a player veto re-opening a closed story) should be re-verified by the
    reviewer(s) who already have the context -- the person who flagged a problem
    is the one who can confirm it was fixed. Fall back to a fresh pick if the
    prior pair is unavailable."""
    author = task.get('assignedTo')
    prior = task.get('_peerGate') or {}
    if preferred is None:
        preferred = prior.get('reviewerIds') or None
    reviewers = _pick_reviewer_ids(state, author, task_room=task.get('room'), preferred=preferred)
    if not reviewers:
        return None
    gate = {
        'room': task.get('room'),
        'approvals': 0,
        'approvers': [],
        'reviewerIds': reviewers,
        'enteredMs': now_ms,
        # review subtask ids appended as they're enqueued (for board surfacing)
        'reviewedTaskIds': [],
        # Fail-closed gate: a story that has already cycled (a rejected review
        # or a failed-pipeline send-back) carries its prior cycle count + freeze
        # flag into the fresh gate, so the shared MAX_REVIEW_CYCLES cap is never
        # reset by a re-entry -- the total review/fix loops stay bounded no
        # matter WHICH mechanism keeps re-arming the gate. A frozen (escalated)
        # gate stays frozen: it re-arms here only as a fresh needs_review entry
        # for display, never to restart the loop (callers guard on `escalated`).
        'cycleCount': prior.get('cycleCount') or 0,
        'escalated': bool(prior.get('escalated')),
    }
    task['status'] = 'needs_review'
    task['_peerGate'] = gate
    # Peer review is agent-to-agent -- the two
    # reviewers picked below (mailbox 'peer_review_request') handle it, so the
    # player doesn't need an email for every routine story entering review,
    # only for things that actually need player input (agent_ask, card_blocked
    # below). This used to also email the player here; removed on request.
    review_items = []
    for i, rid in enumerate(reviewers):
        # Adversarial split: reviewer[0] is the CRITIC, briefed to find the
        # flaw; reviewer[1] keeps the standard skeptical frame.
        critic = (i == 0)
        _append_mailbox((state.get('agents') or {}).get(rid, {}), {
            'kind': 'peer_review_request',
            'about': task.get('id'),
            'title': task.get('title'),
            'text': (f'Work on "{task.get("title")}" is ready for your review. '
                     + ('As the CRITIC, your job is to find what is wrong with it -- assume it has a hidden flaw until you prove it does not, and send it back if you find a concrete, fixable problem.'
                        if critic else
                        'Please review it and approve or send it back.'))})
        review_items.append({
            'title': f'Review: {task.get("title")}',
            'room': task.get('room'),
            'instructions': (f'You are the CRITIC on this review. Review the work for "{task.get("title")}" '
                             'as an adversary: assume it has a hidden flaw until you have proven it does not. '
                             'Check the claims, the numbers, and the reasoning. Approve ONLY if the work survives '
                             'your scrutiny; if you find a concrete, fixable problem, send it back.'
                             if critic else
                             f'Review the work for "{task.get("title")}" and approve it only if it is genuinely solid '
                             'with no concrete, fixable problems. Be a skeptical reviewer, not a rubber stamp.'),
            'goal': task.get('projectLabel') or task.get('goal'),
            'taskType': 'review',
            # projectLabel + taskType=review is what routes pressoffice work to the
            # review executor; reviewOf links this vote back to the story.
            'projectLabel': task.get('projectLabel') or task.get('title'),
            'assignedTo': rid,
            'reviewOf': task.get('id'),
            # Phase E3: the ORIGINAL AUTHOR of the story (the parent's
            # assignedTo). A gate review that rejects must hand the fix back to
            # the worker who built the story -- NOT to the reviewer -- and the
            # review executor needs this id on the subtask to pin that fix.
            'reviewAuthorId': task.get('assignedTo'),
        })
    queue_work(state, review_items)
    return gate


def _cascade_rereview(state, reopened_task_id, now_ms=None):
    """Todo: ripple re-review via the dependency cascade. When a DELIVERED
    story is re-opened (player veto -- the only path a 'done' story can return
    to review), any story that DEPENDED on it (task['dependsOn'] = the reopened
    id, plumbed from the blocked card's issue) was built on the now-questioned
    output. A shipped dependent is sent back through the SAME peer gate --
    preferring the reviewers who already know it -- so the ripple gets
    re-verified too, not just the directly-flagged story. Fail-closed guards:
    only gated lanes, never an escalated (frozen) gate, and a dependent with no
    eligible reviewers right now stays 'done' rather than dropping to limbo.
    Returns the number of dependents re-opened."""
    if not reopened_task_id:
        return 0
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    reopened = 0
    for task in (state.get('tasks') or {}).values():
        if not isinstance(task, dict) or task.get('dependsOn') != reopened_task_id:
            continue
        if task.get('status') != 'done':
            continue
        if not _peer_gated_lane(task):
            continue
        prior = task.get('_peerGate') or {}
        if prior.get('escalated'):
            continue
        if _enter_peer_review(state, task, now_ms) is not None:
            reopened += 1
    return reopened


def _resolve_review_parent(state, review_task):
    """The parent task a gate-review subtask voted on (by its `reviewOf` id), or
    None. A parent already closed or no longer gated is skipped."""
    pid = review_task.get('reviewOf')
    if not pid:
        return None
    parent = (state.get('tasks') or {}).get(pid)
    if not parent or not parent.get('_peerGate'):
        return None
    return parent


def _sim_notify_author(state, parent, reviewer_id, rationale=None):
    """On an 'actionable' verdict the gate re-opens: notify the author that their
    work was sent back for a fix (and who rejected it). The actual fix task is
    queued by the executor's queueFix; here we just file the mailbox note. W6:
    the rejection RATIONALE also lands as a growth-plan note (kind
    'review_denial', repeat=True) routed to the author's NEXT task, so a peer
    denial changes the author's next execution -- not just a one-time mailbox
    message."""
    author = parent.get('assignedTo')
    if not author:
        return
    _append_mailbox((state.get('agents') or {}).get(author, {}), {
        'kind': 'peer_review_rejected',
        'about': parent.get('id'),
        'title': parent.get('title'),
        'text': f'A reviewer{(" (" + reviewer_id + ")") if reviewer_id else ""} sent your work on "{parent.get("title")}" back -- it needs a fix before it can close. Fix it and it will be reviewed again.'})
    # W6: a denial is actionable coaching, not just a message. The reviewer's
    # rationale (the review subtask's note) is carried onto the author's next
    # task so the fix starts from the actual problem raised.
    if rationale:
        _write_growth_plan(
            state, author, parent.get('room') or 'pressoffice',
            'review_denial', int(time.time() * 1000),
            f"Peer review of '{parent.get('title')}' was sent back for a fix. "
            f"What the reviewer said: {(rationale or '')[:400]}. Address this on "
            f"your next pass.", repeat=True)
    # Item 2: the rejection itself lands in the author's feedback buffer, so the
    # NEXT dispatch's perception block shows what came back at them (same W6
    # intent -- coaching that changes the next execution -- but visible in the
    # worker's situational awareness, not just the growth-plan loop).
    _append_feedback(state, author,
                     f"Peer review of '{parent.get('title')}' was sent back for "
                     f"a fix. What the reviewer said: {(rationale or 'no reason given')[:400]}",
                     source='peer_review')


def _sim_notify_author_failed(state, task):
    """Fail-closed send-back signal: tell the author their deliverable was sent
    back because it FAILED the quality pipeline (a red flake8/mypy/bandit/
    pytest-cov run -- see content.py's coding executor), and a fix is queued
    back to them. Mirrors _sim_notify_author's mailbox shape."""
    author = task.get('assignedTo')
    if not author:
        return
    _append_mailbox((state.get('agents') or {}).get(author, {}), {
        'kind': 'peer_review_rejected',
        'about': task.get('id'),
        'title': task.get('title'),
        'text': f'Your work on "{task.get("title")}" failed the quality pipeline and was sent back -- it needs a fix before it can be reviewed. Fix it and it will be reviewed again.'})
    # Item 2: the pipeline failure also lands in the author's feedback buffer, so
    # the next dispatch's perception block names the failing task.
    _append_feedback(state, author,
                     f"Your deliverable '{task.get('title')}' failed the quality "
                     f"pipeline and was sent back. It will be reviewed again after "
                     f"you fix it.",
                     source='pipeline')


def _log_quality_gate_reject(state, task, note, escalated):
    """red_pipeline telemetry: a durable, queryable marker for every fail-closed
    quality-gate send-back (a red flake8/mypy/bandit/pytest-cov content result
    that was CAUGHT and never marked done). Mirrors the selfProposedRejected
    marker pattern (_resolve_refinement) so the health check can count rejects
    vs. escapes with a JSON-substring LIKE instead of a JSON query. The gate is
    fail-closed, so `redPipelineEscaped` is always false today -- the counter's
    job is to trip loudly if a regression ever lets a red result complete."""
    details = {'taskId': task.get('id'),
               'author': task.get('assignedTo'),
               'title': (task.get('title') or '')[:120],
               'note': (note or '')[:120],
               'redPipelineEscaped': False,
               'redPipelineEscalated': bool(escalated)}
    _log_governance(state, task.get('assignedTo'), 'quality_gate_reject', details)


def _send_back_after_failure(state, task, fail_note=None):
    """Fail-closed quality-gate send-back. A deliverable (or a fix of one) whose
    content result failed the pipeline is marked `failed` -- deliberately NOT
    'needs_review', because _sweep_stuck_gates only processes needs_review and
    would otherwise re-pin fresh reviewers onto a card whose pipeline is red.
    The author is notified (mailbox) -- this is the one send-back path that
    MUST tell the worker, whether the story can be fixed or has escalated.
    Under the shared review-cycle cap a fix is queued back to the original
    author (pinned + high priority); once the cap is crossed the story
    escalates (create_escalation) and stays frozen -- no further fix cycling.
    Pure state mutation; called from _task_cycle's content-result branch. The
    prior gate (if any) is kept so _maybe_escalate_stuck_gate's shared cap and
    _enter_peer_review's carry-over both see it."""
    author = task.get('assignedTo')
    note = (fail_note or task.get('note') or 'the pipeline did not pass').strip()
    _sim_notify_author_failed(state, task)
    # Feedback injection (brunnfeld): a concrete failure reason must change the
    # author's NEXT execution, not just land as a mailbox message. Same W6
    # pattern as peer-review denial (_sim_notify_author): the pipeline failure
    # reason is routed through the growth-plan loop so it is injected once into
    # the author's next task via _coaching_note_for/_augment_task_instructions.
    if author:
        _write_growth_plan(
            state, author, task.get('room') or 'pressoffice',
            'pipeline_feedback', int(time.time() * 1000),
            f"Your deliverable '{task.get('title')}' failed the quality pipeline. "
            f"What failed: {(note or '')[:400]}. Address this on your next pass.",
            repeat=True)
    if not task.get('_peerGate'):
        # Seed a real (empty) gate: _resolve_review_parent requires a truthy
        # _peerGate to resolve a later fix's completion, and the cycle cap needs
        # somewhere to count.
        task['_peerGate'] = {'approvals': 0, 'approvers': [],
                             'reviewerIds': [], 'enteredMs': 0}
    gate = task['_peerGate']
    task['status'] = 'failed'
    task['failedAt'] = int(time.time() * 1000)
    task['failNote'] = note[:300]
    escalated = _maybe_escalate_stuck_gate(task, gate, 'quality pipeline keeps failing')
    _log_quality_gate_reject(state, task, note, escalated=escalated)
    if escalated:
        return  # frozen -- escalated + player notified; no further fix cycling
    instructions = (f"Your work on '{task.get('title')}' failed the quality pipeline: "
                    f"{note}. Fix it so the pipeline passes clean, then it will be re-reviewed.")
    if task.get('userStory'):
        instructions += f"\n\nUser story: {task['userStory']}"
    if task.get('acceptanceCriteria'):
        instructions += f"\n\nAcceptance criteria:\n{task['acceptanceCriteria']}"
    queue_work(state, [{
        'title': f'Fix: {task.get("title")}',
        'room': task.get('room'),
        'instructions': instructions,
        'goal': task.get('projectLabel') or task.get('goal'),
        'projectLabel': task.get('projectLabel') or task.get('title'),
        'taskType': 'code',
        'reviewOf': task.get('id'),
        'reviewAuthorId': author,
        'assignedTo': author,
        'productId': task.get('productId'),
        'userStory': task.get('userStory'),
        'acceptanceCriteria': task.get('acceptanceCriteria'),
        'priority': WORK_PRIORITY['high'],
    }])


def _peer_gate_should_close(gate, now_ms, entered_ms):
    """Close rule: two distinct clean approvals, OR -- after the review-timeout
    window has elapsed -- a single clean approval. Guards against a small think tank
    deadlocking on a second unreachable approver."""
    if gate['approvals'] >= 2:
        return True
    if gate['approvals'] >= 1 and entered_ms and (now_ms - entered_ms) >= PEER_REVIEW_TIMEOUT_MS:
        return True
    return False


def _note_completed_room(state, agent_id, task):
    """Record a room an agent has actually completed work in (deliverable work
    bumps it, whether the story closes via finish_task or the peer gate's
    _release_agent_gated). Drives two Phase E2 surfaces: the reviewer-widening
    signal ("knows the room's craft") and the cross-learning affordance
    ('roomsLearned'). Idempotent -- a room is tracked once. Cut 2 also fires
    the fire-and-forget deliverable grader + incident runbook writer here, so
    both completion paths grade/learn exactly once."""
    if not task or not task.get('room'):
        return
    a = (state.get('agents') or {}).get(agent_id)
    if not a:
        return
    rooms = a.setdefault('completedRooms', [])
    if task.get('room') not in rooms:
        rooms.append(task.get('room'))
    now_ms = int(time.time() * 1000)
    _grade_completed_task(state, agent_id, task, now_ms)
    _runbook_task(state, task, now_ms)


def _release_agent_gated(state, agent_id, grid):
    """Release an agent whose deliverable work just finished but whose STORY stays
    open in 'needs_review' (as opposed to finish_task, which marks it 'done').
    Clears the agent's task/busy/inRoom + bumps approvedCount, leaves the task
    status untouched, and sends the agent off duty (vanishes in place)."""
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not a:
        return
    completed = (state.get('tasks') or {}).get(a.get('task'))  # capture before clearing
    a['task'] = None
    a['busy'] = False
    a['inRoom'] = None
    a['visible'] = True
    a['approvedCount'] = (a.get('approvedCount') or 0) + 1
    a['weekApprovals'] = (a.get('weekApprovals') or 0) + 1
    _note_completed_room(state, agent_id, completed)
    _maybe_file_followup(state, agent_id, completed, int(time.time() * 1000))


def _release_agent_after_failure(state, agent_id, task):
    """Release an agent whose deliverable work FAILED the quality pipeline (or
    whose fix subtask failed it). Unlike _release_agent_gated / finish_task this
    does NOT bump approvals, record a completed room, grade the deliverable, or
    file a follow-up -- a red-pipeline result is not shipped work and must not
    count as one (a fabricated 0.0 trailing grade would otherwise pollute the
    roadmap signal and a follow-up would propose more work off a failure). The
    completed task is marked 'done' so _reclaim_orphaned_walking_tasks never
    re-issues it. Pure state mutation."""
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not a:
        return
    if task:
        task['status'] = 'done'
    a['task'] = None
    a['busy'] = False
    a['inRoom'] = None
    a['visible'] = True


def _gate_reviewer_reachable(state, rid, gate, now_ms):
    """Can this locked reviewer actually produce a vote? True when she is ON-DUTY
    and idlable (free to take her review subtask next cycle), OR when her review
    subtask is still live in the queue (so the existing pin-wake can bring her --and
    the gate a healthy chance). A reviewer who is off-duty-but-present is reachable
    too (she wakes on click); one who is *gone* (fired, or perpetually busy with
    other pinned work, or her vote subtask died) is not."""
    if not rid:
        return False
    a = (state.get('agents') or {}).get(rid)
    if a is None:
        return False  # fired -- an id in the gate but no live agent
    # A live agent is reachable if she isn't entrenched in non-review other work
    # (off-duty is wakeable; busy-on-own-task is not, though she may free soon --
    # count a still-queued review subtask pinned to her as reachable below).
    if not a.get('busy') and not a.get('task'):
        return True
    for item in (state.get('workQueue') or []):
        if item.get('reviewOf') and item.get('assignedTo') == rid:
            return True
    return False


def _prune_state_blob(state, now_ms):
    """Bound the append-only tails inside the kv_state blob. The whole blob is
    serialized + autosaved every ~1s, so anything that grows without bound keeps
    growing that write forever (the 'save takes a second' slowdown). Every
    structure here is a bounded TAIL: live/open/pending entries are ALWAYS kept,
    only the historical tail is capped. `tasks` is deliberately untouched -- it
    backs per-agent history and counters, and a hard cap there would silently
    corrupt them. Pure; mutates `state` only. Called from _task_cycle on a coarse
    cadence."""
    cd = state.get('completedDeliverables')
    if isinstance(cd, list) and len(cd) > _TAIL_COMPLETED_DELIVERABLES:
        state['completedDeliverables'] = cd[-_TAIL_COMPLETED_DELIVERABLES:]
    br = state.get('backlogRequests')
    if isinstance(br, list):
        keep = [r for r in br if (r or {}).get('status') == 'pending']
        tail = [r for r in br if (r or {}).get('status') != 'pending']
        if len(tail) > _TAIL_BACKLOG_RESOLVED:
            tail = tail[-_TAIL_BACKLOG_RESOLVED:]
        state['backlogRequests'] = keep + tail
    pi = state.get('playerInbox')
    if isinstance(pi, list):
        keep = [m for m in pi if not (m or {}).get('answered')]
        tail = [m for m in pi if (m or {}).get('answered')]
        if len(tail) > _TAIL_PLAYER_INBOX_ANSWERED:
            tail = tail[-_TAIL_PLAYER_INBOX_ANSWERED:]
        state['playerInbox'] = keep + tail
    issues = state.get('issues')
    if isinstance(issues, dict):
        open_entries = [e for e in issues.values() if (e or {}).get('status') not in ('resolved', 'closed')]
        closed_entries = [e for e in issues.values() if (e or {}).get('status') in ('resolved', 'closed')]
        if len(closed_entries) > _TAIL_ISSUES_CLOSED:
            closed_entries = closed_entries[-_TAIL_ISSUES_CLOSED:]
        state['issues'] = {e.get('key') or i: e for i, e in enumerate(open_entries + closed_entries)}
    gp = state.get('growthPlans')
    if isinstance(gp, list):
        keep = [p for p in gp if (p or {}).get('status') != 'applied']
        tail = [p for p in gp if (p or {}).get('status') == 'applied']
        if len(tail) > _TAIL_GROWTH_PLANS_APPLIED:
            tail = tail[-_TAIL_GROWTH_PLANS_APPLIED:]
        state['growthPlans'] = keep + tail


def _sweep_stuck_gates(state, now_ms):
    """Phase E3.1: stuck-at-review escalation watchdog. For every story sitting in
    `needs_review`, if its LOCKED reviewer pair cannot produce a vote (both
    reviewers unreachable/inert) but the pool COULD be widened, re-pick a fresh
    reachable pair and re-pin the pending `reviewOf` subtasks so the gate gains a
    viable path (the existing _assign_due_item pin-wake then wakes the new
    reviewer for free). Bounded: never widens a gate younger than
    STUCK_GATE_GRACE_MS, and at most one re-pick per gate per
    STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS. Called from _task_cycle on a cadence.
    Pure; mutates `state` only. Never auto-closes -- an unreachable gate is made
    REACHABLE, and done stays defined by _peer_gate_should_close."""
    for task in (state.get('tasks') or {}).values():
        gate = task.get('_peerGate')
        if not gate or task.get('status') != 'needs_review':
            continue
        if gate.get('escalated'):
            continue  # frozen -- _maybe_escalate_stuck_gate already notified the player
        entered = gate.get('enteredMs') or 0
        if now_ms - entered < STUCK_GATE_GRACE_MS:
            continue  # young gate -- reviewer 1 may still be about to vote
        last = gate.get('stuckRescueTs') or 0
        if now_ms - last < STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS:
            continue  # already rescued too recently -- bounded, no re-pick storm
        locked = gate.get('reviewerIds') or []
        # Phase E3 recovery: a review that completed WITHOUT folding a vote (e.g.
        # under the old router it ran the research executor, or a content timeout
        # dropped it) leaves the gate with NO pending review to pin. The pair may
        # even be fully reachable -- `_gate_reviewer_reachable` is true -- yet
        # there is nothing to do, so the existing widen branch takes the
        # "still advance" continue and the story sits in needs_review forever
        # with reviewedTaskIds growing and approvals stuck at 0. Detect that
        # exact state: reachable reviewers but NO pending reviewOf work item.
        pending_reviews = [q for q in (state.get('workQueue') or [])
                           if q.get('reviewOf') == task.get('id')]
        if not pending_reviews:
            if any(_gate_reviewer_reachable(state, rid, gate, now_ms) for rid in locked):
                # Reviewers are free but the review work silently vanished
                # (consumed with no verdict, or never enqueued): re-queue a fresh
                # review to the SAME (reachable) pair so the gate gains a vote
                # path back. Bounded by the rescue cooldown above + grace, AND
                # by the shared cycle cap -- this rescue path used to be able
                # to re-enter indefinitely (once per cooldown window, forever)
                # with no overall bound at all.
                if _maybe_escalate_stuck_gate(task, gate, 'repeated failed review attempts'):
                    continue
                repl = _reenter_gate_review(state, task, locked)
                if repl:
                    gate['stuckRescueTs'] = now_ms
                    gate['enteredMs'] = now_ms  # restart the timeout window fairly
                    try:
                        from serve import log_action
                        log_action(None, 'task_review_requeued',
                                   {'taskId': task.get('id'), 'title': task.get('title'),
                                    'to': repl}, authorized=True)
                    except Exception:
                        pass
                continue
        if any(_gate_reviewer_reachable(state, rid, gate, now_ms) for rid in locked):
            continue  # the locked pair can still advance -- leave it alone
        # Locked pair is unreachable; widen. Prefer on-team + room-contributors,
        # excluding the locked pair, any non-author non-admin.
        author = task.get('assignedTo')
        fresh = _pick_reviewer_ids(state, author, task_room=task.get('room'))
        fresh = [rid for rid in fresh if rid not in locked and rid != author]
        # Top up to a full pair from the broader reachable non-admin roster when
        # the team-widened pool came up short (the locked filter can thin it to
        # one). The rescue must yield a viable PAIR, not leave the gate one vote
        # short of the 2-clean rule.
        for d in (state.get('agentRoster') or []):
            if len(fresh) >= 2:
                break
            if d.get('isAdmin') or d.get('id') == author or d.get('id') in locked:
                continue
            if d.get('id') in fresh:
                continue
            if _gate_reviewer_reachable(state, d.get('id'), gate, now_ms):
                fresh.append(d.get('id'))
        if not fresh:
            continue  # genuinely nobody left (author-only think tank) -- nothing to do
        # Re-pin the still-queued review subtasks to the fresh reviewers, and re-
        # pin any already-assigned reviewer; then record the gate as rescued.
        repl = iter(fresh)
        replaced = 0
        for item in (state.get('workQueue') or []):
            if item.get('reviewOf') == task.get('id'):
                item['assignedTo'] = next(repl, item.get('assignedTo'))
                replaced += 1
        gate['reviewerIds'] = fresh[:2]
        gate['stuckRescueTs'] = now_ms
        gate['enteredMs'] = now_ms  # restart the timeout window fairly
        if replaced:
            try:
                from serve import log_action
                log_action(None, 'task_peer_widened',
                           {'taskId': task.get('id'), 'title': task.get('title'),
                            'from': locked, 'to': fresh[:2]}, authorized=True)
            except Exception:
                pass


def _reenter_gate_review(state, parent, reviewer_ids):
    """Recover a peer-gated story whose review work was silently consumed without
    a verdict (the gate's reviews are all done but approvals is still 0 and no
    reviewOf item remains queued). Enqueue a FRESH review subtask to each of the
    (reachable) `reviewer_ids` so the gate regains a vote path. Mirrors the
    review item _enter_peer_review builds, minus re-picking. Returns the list of
    reviewers re-enqueued (empty if none)."""
    reviewers = [rid for rid in reviewer_ids
                 if (state.get('agents') or {}).get(rid) is not None]
    if not reviewers:
        return []
    items = []
    for rid in reviewers:
        _append_mailbox((state.get('agents') or {}).get(rid, {}), {
            'kind': 'peer_review_request',
            'about': parent.get('id'),
            'title': parent.get('title'),
            'text': f'A previous review of "{parent.get("title")}" was lost without a decision. Work on it is ready for your review again -- please review and approve or send it back.'})
        items.append({
            'title': f'Review: {parent.get("title")}',
            'room': parent.get('room'),
            'instructions': (f'Review the work for "{parent.get("title")}" and approve it only if it is genuinely solid '
                             'with no concrete, fixable problems. Be a skeptical reviewer, not a rubber stamp.'),
            'goal': parent.get('projectLabel') or parent.get('goal'),
            'taskType': 'review',
            'projectLabel': parent.get('projectLabel') or parent.get('title'),
            'assignedTo': rid,
            'reviewOf': parent.get('id'),
            'reviewAuthorId': parent.get('assignedTo'),
        })
    queue_work(state, items)
    return reviewers


def _close_gated_story(state, parent):
    """Close a peer-gated story once its gate is satisfied. The parent is not on
    any agent (its author was released when primary work finished), so this is a
    direct status flip + log rather than finish_task, which needs a working
    agent. Returns True on close."""
    review_counts = parent.get('_peerGate') or {}
    review_counts['closed'] = True
    parent['status'] = 'done'
    # The gated story shipped: its per-story capability grant dies with it.
    _revoke_task_access(parent.get('id'))
    try:
        from serve import log_action
        log_action(None, 'task_peer_approved',
                   {'taskId': parent.get('id'), 'title': parent.get('title'),
                    'approvals': review_counts.get('approvals', 0),
                    'approvers': review_counts.get('approvers', [])}, authorized=True)
    except Exception:
        pass
    # Entering review deliberately stays quiet
    # (peer review is agent-to-agent) but a story
    # actually SHIPPING is exactly the "you'll want to know this" moment the
    # player was asking about -- called directly (not via the notifyPlayer
    # indirection content executors need) since this already runs inside the
    # tick's real read-modify-write with live, mutable `state` in hand.
    title = parent.get('title') or parent.get('projectLabel') or 'a story'
    _queue_player_email(state, 'story_done', f'[AI Think Tank] Shipped: {title[:80]}',
                        f'"{title}" just landed after {review_counts.get("approvals", 0)} peer approval(s).')
    return True


def _parent_close_from_vote(state, parent, now_ms):
    """After a clean review vote lands, decide whether the parent's gate is now
    satisfied (2 distinct clean, or 1 + timeout) and close it if so. Mirrors the
    caller's needs: called from _task_cycle when a review/fix subtask completes."""
    gate = parent.get('_peerGate')
    if not gate:
        return False
    if _peer_gate_should_close(gate, now_ms, gate.get('enteredMs')):
        _close_gated_story(state, parent)
        return True
    return False


def _team_member_candidates(state, director_id, member_ids, now_ms):
    """Team-scoped whoNeedsHelp: the members of ONE director's team (their
    direct reports), described for Jev by morale + workload signals. A hire
    adds overflow help to an overloaded teammate, never outside the team."""
    agents = state.get('agents') or {}
    roster = {d.get('id'): d for d in (state.get('agentRoster') or [])}
    out = []
    for member_id in member_ids:
        a = agents.get(member_id)
        if not a:
            continue
        # Display name from the roster ideal (capitalized), fall back to live.
        display = roster.get(member_id, {}).get('name') or a.get('name') or member_id
        morale = morale_for(state, member_id, now_ms)
        out.append({'id': member_id, 'name': display,
                    'description': f"{display}, {a.get('role')}, on the {director_id} team. "
                                   f"Morale: {morale}/100. Approved work: {a.get('approvedCount')}. "
                                   f"Dropped work: {a.get('droppedCount')}. "
                                   f"Reports filed against them: {len(reports_about(state, member_id))}."})
    return out


def _free_outdoor_spot(state, grid, rnd=random.random):
    """Place a new hire on foot. Mirrors the JS finishHire: a free outdoor spot
    (pick_free_spot, reachable from SPAWN). The outskirts trailhead doors are
    gone, so a new hire simply appears at a free think tank spot."""
    agents = (state.get('agents') or {})
    occupied = [{'x': a.get('x'), 'y': a.get('y')} for a in agents.values() if a.get('visible')]
    spot = pick_free_spot(grid, avoid_points=occupied, rnd=rnd)
    return spot


def _borrow_inactive_agent_for_team(state, borrower_director_id, now_ms=None):
    """Cross-team borrowing: when a team needs help, borrow an INACTIVE
    (off-duty, dormant-inventory) agent from ANOTHER team for the borrower
    team's sprint instead of hiring a brand-new clone. The loan is recorded on
    the roster entry (`loan: {teamId, since, reason}`) and ends when the
    borrower's sprint closes -- unless the borrower still has pending work (a
    queued large-request breakdown or any pending backlog request), in which
    case the loan persists: that's the 'feature need -> agent may change teams'
    case. The borrowed agent keeps her home director and authority chain
    ('teams stay somewhat consistent'); only her work-assignment preference
    shifts to the borrower team for the loan's duration (see _assign_due_item's
    soft team preference). Returns the borrowed agent id, or None when no
    inactive agent from another team is available."""
    roster = state.get('agentRoster') or []
    agents = state.get('agents') or {}
    borrower_members = set(_sim_direct_reports(state, borrower_director_id))
    idle_home = []
    busy_home = []
    for d in roster:
        if d.get('id') in borrower_members:
            continue  # already on the borrower team
        if d.get('isAdmin'):
            continue  # the admin is never loaned out
        if d.get('loan'):
            continue  # already on loan elsewhere
        a = agents.get(d.get('id'))
        if not a or not a.get('offDuty'):
            continue  # only INACTIVE (off-duty, dormant) agents are borrowed
        home = d.get('director')
        if not home or home == borrower_director_id:
            continue
        # Least-disruptive first: borrow from a home team with no active sprint
        # (its people aren't committed to anything right now).
        (busy_home if _team_in_active_sprint(state, home) else idle_home).append(d)
    pick = (idle_home or busy_home or [None])[0]
    if not pick:
        return None
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    loan_id = pick['id']
    pick['loan'] = {'teamId': borrower_director_id, 'since': now_ms, 'reason': 'sprint_borrow'}
    a = agents[loan_id]
    a['offDuty'] = False
    a['visible'] = True
    _log_governance(state, borrower_director_id, 'team_borrow',
                    {'borrowed': loan_id, 'team': borrower_director_id,
                     'home': pick.get('director'), 'action': 'loaned'})
    return loan_id


def _end_loans_for_team(state, team_id, now_ms=None):
    """End cross-team loans to a team whose sprint just closed -- unless the
    borrower still has pending work (a queued large-request breakdown or any
    pending backlog request), in which case the loan persists: that's the
    'feature need -> agent may change teams' case, and the need is ongoing. The
    borrowed agent keeps her home director; only the loan tag is cleared, so
    her assignment preference returns to her home team."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    pending = [r for r in (state.get('backlogRequests') or [])
               if r.get('status') == 'pending' and r.get('teamId') == team_id]
    ongoing = bool(pending)
    for d in (state.get('agentRoster') or []):
        loan = d.get('loan') or {}
        if loan.get('teamId') != team_id:
            continue
        if ongoing:
            loan['since'] = now_ms  # refresh the loan while the need persists
            _log_governance(state, team_id, 'team_borrow',
                            {'borrowed': d['id'], 'team': team_id,
                             'home': d.get('director'), 'action': 'extended'})
            continue
        d.pop('loan', None)
        _log_governance(state, team_id, 'team_borrow',
                        {'borrowed': d['id'], 'team': team_id,
                         'home': d.get('director'), 'action': 'returned'})


def _dormant_workers_will_be_woken(state, now_ms):
    """True when an off-duty (dormant) worker is about to be re-awakened for
    pending or scheduled work, so the active slot she vacated isn't actually
    free for a new hire. Mirrors the assignment loop's wake rule (see
    _task_cycle): a due, unparked, dependency-met queue item wakes a dormant
    agent when it is pinned/scheduled (reviewOf/sprintId/notBefore) or when no
    awake-idle agent is around to take it, and only while the active ceiling
    has room (can_activate_another). Standing cadences (research topics, skill
    review, distill, pipelines) queue their work in _check_schedules BEFORE
    governance runs, so the workQueue already reflects what will wake them."""
    roster = state.get('agentRoster') or []
    agents = state.get('agents') or {}
    # Any dormant (off-duty) worker at all? No dormant workers, no wake risk.
    has_dormant = any(
        not d.get('isAdmin')
        and isinstance(agents.get(d.get('id')), dict)
        and agents[d['id']].get('offDuty')
        for d in roster)
    if not has_dormant:
        return False
    if not can_activate_another(state):
        return False  # at/over the ceiling no dormant agent can be woken
    for item in (state.get('workQueue') or []):
        if not is_work_item_due(item, now_ms):
            continue
        if _is_parked(item) or not _work_item_dependency_met(state, item):
            continue
        if item.get('reviewOf') or item.get('sprintId') or item.get('notBefore'):
            return True  # pinned/scheduled work wakes regardless of idle count
        if _awake_idle_count(state) == 0:
            return True  # no awake-idle agent -> a dormant one gets woken
    return False


def _start_auto_hire(state, now_ms, grid, decider):
    """hiring.js attemptAutoHire, team-scoped: each TEAM DIRECTOR hires for
    their own team (not one global admin hiring anyone). If a director is free
    + under cap + someone on THEIR team needs help, busy that director at
    Command Center and defer the actual agent creation HIRE_DURATION_MS via a
    durable `_pendingHire` record the next pass completes. Returns True if a
    hire started."""
    roster = state.get('agentRoster') or []
    if len(roster) >= MAX_TOTAL_AGENTS:
        state['_hireBlockedAtCapNote'] = len(roster)
        return False
    # Active-ceiling gate: stop spawning at MAX_ACTIVE_AGENTS (25). When a
    # sprint ends and workers go dormant, active_agent_count drops below the
    # ceiling and this gate reopens on its own. `lastHireAt` is deliberately
    # NOT advanced, so the next governance pass retries the moment a slot frees
    # (the note records WHY a blocked hire was blocked, for the UI).
    if active_agent_count(state) >= MAX_ACTIVE_AGENTS:
        state['_hireBlockedAtActiveCapNote'] = active_agent_count(state)
        return False
    # Dormant-wake refinement: workers going dormant only frees a slot for a
    # new hire if those dormant workers have no scheduled task or work they
    # will be re-awakened for. If a dormant worker is about to be woken by due
    # scheduled/pinned work (or by a due queue item with no awake-idle agent),
    # the slot is already spoken for -- hiring on top would overshoot the
    # ceiling once she wakes.
    if _dormant_workers_will_be_woken(state, now_ms):
        state['_hireBlockedDormantWakeNote'] = True
        return False
    agents = state.get('agents') or {}

    # Directors are anyone with >=1 direct report (the `director` pointer is
    # already on every roster entry). The admin/stand-in is included if they
    # lead a team; otherwise each team has its own director.
    director_ids = []
    for d in roster:
        if _sim_direct_reports(state, d.get('id')):
            director_ids.append(d.get('id'))

    # Candidate pools, grouped by hiring director: only that director's own
    # direct reports may be hired-for, so the new assistant lands in the team.
    # A director chooses which of THEIR members needs overflow help.
    pool_by_director = {}
    for did in director_ids:
        dir_agent = agents.get(did)
        if not dir_agent or dir_agent.get('busy') or dir_agent.get('offDuty'):
            continue  # this director can't run a hire right now
        members = [m for m in _sim_direct_reports(state, did)
                   if agents.get(m)]
        if not members:
            continue
        # Team-size cap: a team may not grow past 6 members
        # (scrum master excluded). A hire will add one worker, so the check must
        # account for that extra member -- a team already at 6 cannot hire.
        if not _team_under_cap(state, did, extra=1):
            continue
        pool_by_director[did] = _team_member_candidates(state, did, members, now_ms)

    if not pool_by_director:
        state['lastHireAt'] = 0  # nothing to decide
        return False

    # Cross-team borrowing comes FIRST: before a director spends a hire (a Jev
    # decision + a brand-new clone's onboarding), try to borrow an INACTIVE
    # agent from another team for the borrower team's sprint. Only when no
    # inactive agent is available do we fall through to hiring a new assistant.
    for did in pool_by_director:
        if _borrow_inactive_agent_for_team(state, did, now_ms):
            state['lastHireAt'] = now_ms
            return True

    state['lastHireAt'] = now_ms
    # Flat candidate list for the decider: each entry says which team + member.
    flat = []
    for did, cands in pool_by_director.items():
        for c in cands:
            c['directorId'] = did
            flat.append(c)
    pick = decider(state, 'Pick whoever most needs additional help right now -- weigh morale, workload signals (approved vs. dropped work), and any reports filed against them. You may only assign help WITHIN a team: choose a director who is free, then one of their own overloaded direct reports to add an assistant to. Someone already busy or off-duty cannot run a hire.', flat)
    picked = next((c for c in flat if c['id'] == pick), None) if pick else None
    if not picked:
        # Fallback: the lowest-morale report across all available teams.
        worst, worst_score = None, float('inf')
        for c in flat:
            s = morale_for(state, c['id'], now_ms)
            if s is not None and s < worst_score:
                worst_score, worst = s, c
        picked = worst
    if not picked:
        state['lastHireAt'] = 0
        return False
    director_id = picked.get('directorId') or picked.get('id')
    director = agents.get(director_id)
    if not director or director.get('busy'):
        state['lastHireAt'] = 0
        return False
    director_def = next((d for d in roster if d.get('id') == director_id), {})
    director['busy'] = True
    director['visible'] = False
    director['inRoom'] = 'commandcenter'
    director['roomX'] = 315
    director['roomY'] = 155
    director['dir'] = 'south'
    state['_pendingHire'] = {
        'adminId': director_id, 'adminName': director_def.get('name') or director_id,
        'directorId': director_id,
        'helpForId': picked['id'], 'helpForName': picked.get('name') or picked['id'],
        'at': now_ms + HIRE_DURATION_MS,
    }
    return True


def _complete_auto_hire(state, pending, grid, now_ms):
    """hiring.js finishHire: actually add the new agent to the roster + agents
    once HIRE_DURATION_MS has elapsed. Idempotent -- a restart mid-hire just
    re-runs this with the next present agents. Returns the new agent id or None."""
    admin = (state.get('agents') or {}).get(pending['adminId'])
    if admin:
        admin['busy'] = False
        admin['visible'] = True
        admin['inRoom'] = None
    help_for = (state.get('agents') or {}).get(pending['helpForId']) or {}
    # Every hire reports to a TEAM DIRECTOR (pending['directorId']), so the new
    # agent lands inside that director's team + shared dir the moment they're
    # created. Falls back to the hiring admin id only if not stamped.
    director_id = pending.get('directorId') or pending.get('adminId')
    role = f"Assistant to {pending['helpForName']}"
    # The hiring DIRECTOR (pending['adminName']) names the new employee via a
    # real generative call -- no predetermined names. All-time uniqueness is
    # enforced against _all_used_names (roster + every name ever reserved), and
    # a model outage or invalid candidate falls back to the fixed pool, which
    # itself skips all-time-used names.
    name = _hire_name_chooser(
        state, pending.get('adminName') or pending.get('adminId'),
        _all_used_names(state), role)
    # Defense in depth: the default chooser validates uniqueness itself, but a
    # substituted/test chooser must not be able to collide -- re-check against
    # the all-time set before trusting it, else fall back to the pool.
    if not name or name.lower() in _all_used_names(state):
        name = _next_hire_name(state)
    if not name:
        state.pop('_pendingHire', None)
        return None
    _remember_name(state, name)
    color_pool = HIRE_COLOR_POOL
    color = color_pool[random.randrange(len(color_pool))]
    access_grant = f"Read/write access to {pending['helpForName']}'s {help_for.get('role', '')} files and tooling."
    profile = {
        'mission': f"Support the {director_id} team by picking up overflow work for {pending['helpForName']} ({help_for.get('role', '')}).",
        'instructions': [
            f"Report to {pending['helpForName']} -- pick up whatever they flag as overloaded.",
            f"Your director is {pending['adminName']}; your work lands in the {director_id} team's shared space.",
            "Hired via Command Center -- elevated access is scoped to what you're helping with, not think tank-wide.",
        ],
        'notes': [f"Hired by {pending['adminName']} (director of the {director_id} team)."],
    }
    new_id = name.lower()
    roster = state.setdefault('agentRoster', [])
    # Reentry guard: a fired employee can never come back. Refuse the hire and
    # drop the pending record so the next pass stages a fresh (different) hire
    # instead of retrying the same forbidden one forever.
    if _is_fired(state, new_id, name):
        state.pop('_pendingHire', None)
        return None
    # Defensive re-check: a director's team may have grown to cap between the
    # hire's approval and this completion pass; never blow past the cap. The
    # hire itself adds one member, so the check accounts for that extra.
    if not _team_under_cap(state, director_id, extra=1):
        state.pop('_pendingHire', None)
        return None
    roster.append({
        'id': new_id, 'name': name, 'color': color, 'role': role, 'model': 'small',
        'director': director_id,   # -> resolves into the team for _derive_team_members
        'villageId': village_of_agent(state, director_id),
        'approvedCount': 0, 'droppedCount': 0, 'weekApprovals': 0,
        'mailbox': [f"Welcome aboard -- you're here to help {pending['helpForName']} with {help_for.get('role', '').lower()} work."],
        'elevatedAccess': True, 'accessGrant': access_grant, 'profile': profile,
    })
    spot = _free_outdoor_spot(state, grid)
    # A hire joins active (on foot at a free spot) only if there's room under
    # the ACTIVE ceiling; otherwise she joins the inventory dormant (off-duty),
    # exactly hiring.js finishHire's canActivateAnother() fork -- a large total
    # inventory is fine, but only MAX_ACTIVE_AGENTS are online at once.
    can_activate = can_activate_another(state)
    agents = state.setdefault('agents', {})
    agents[new_id] = {
        'id': new_id, 'name': name, 'color': color, 'role': role, 'profile': profile,
        'model': 'small', 'approvedCount': 0, 'droppedCount': 0, 'weekApprovals': 0,
        'mailbox': [{'text': f"Welcome aboard -- you're here to help {pending['helpForName']} with {help_for.get('role', '').lower()} work.", 'read': False, 'ts': now_ms}],
        'conversationLog': [], 'lastContactedAt': None, 'hiredAt': now_ms,
        'elevatedAccess': True, 'accessGrant': access_grant,
        'inRoom': None, 'roomX': None, 'roomY': None,
        'x': spot['x'], 'y': spot['y'], 'dir': 'south',
        'visible': can_activate, 'busy': False, 'meetingId': None,
        'offDuty': not can_activate,
    }
    state.pop('_pendingHire', None)
    # Phase B: the moment a hire exists, enqueue its onboarding ceremony -- the
    # director + the coworkers who'll work with it convene at Command Center,
    # then stage the new agent's AGENT.md progressively. Stamped here so a
    # _governance_pass after this hire starts the Town-Hall meeting. Colleagues
    # are the hire's intended collaborator set (its `helpFor`, plus any team
    # member already on the roster for that director).
    coworkers = [c for c in _sim_direct_reports(state, director_id) if c != new_id]
    if pending.get('helpForId') and pending['helpForId'] not in coworkers:
        coworkers.append(pending['helpForId'])
    state['_pendingOnboard'] = {
        'agentId': new_id,
        'directorId': director_id,
        'directorName': pending.get('adminName') or director_id,
        'helpForId': pending.get('helpForId'),
        'helpForName': pending.get('helpForName') or pending.get('helpForId'),
        'coworkerIds': coworkers,
        'stage': 0,
        'at': now_ms + ONBOARD_MEET_DURATION_MS,
    }
    agents[new_id]['profile'] = dict(profile)
    agents[new_id]['profile']['onboarding'] = {'stage': 0, 'directorId': director_id}
    return new_id


def _next_hire_name(state):
    """A deterministic-ish unique first name for a hire, used only as a
    fail-closed FALLBACK when the director's generative name chooser is
    unavailable. The live path is _hire_name_chooser (a real LLM call that
    makes the hire feel directed); this pool only steps in on a model outage.
    Never collides with any name the think tank has ever used, else None."""
    pool = ['maya', 'leo', 'zara', 'owen', 'lyra', 'ida', 'vela']
    used = _all_used_names(state)
    for n in pool:
        if n not in used:
            return n
    return None


# Larger pool for spawning a BRAND-NEW team's director + members.
# Distinct from _next_hire_name's small assistant pool so a think tank whose hire
# pool is exhausted can still spin up a new team for an incoming large request.
_NEW_TEAM_NAME_POOL = [
    'aria', 'briar', 'cairo', 'dune', 'elio', 'fawn', 'gull', 'hale',
    'indie', 'jove', 'kestrel', 'lian', 'marlow', 'niso', 'orion', 'penn',
    'quill', 'rune', 'sable', 'taro', 'umi', 'vireo', 'wren', 'ximena',
    'yarrow', 'zephyr', 'astra', 'baxter', 'cedar', 'dax', 'ember', 'fox',
]


def _next_new_team_name(state):
    """A unique name for a new-team director or member, drawn from
    _NEW_TEAM_NAME_POOL. Never collides with any name the think tank has ever
    used (_all_used_names includes retired/fired names, so a new team never
    re-takes one). Returns None when exhausted."""
    used = _all_used_names(state)
    for n in _NEW_TEAM_NAME_POOL:
        if n not in used:
            return n
    return None


def _all_used_names(state):
    """Every first name the think tank has EVER used: the current roster plus
    `_usedNames`, the durable all-time accumulation (fired/retired agents keep
    their name reserved forever, so a director never re-hires a 'jane' after
    jane was let go). Lowered for collision-safe comparisons."""
    used = {str(d.get('name', '')).lower() for d in (state.get('agentRoster') or [])}
    used.update(state.get('_usedNames') or [])
    return used


def _remember_name(state, name):
    """Durably reserve a first name forever (all-time uniqueness). Called at
    every server-side creation AND when an agent is fired, so a retired name is
    never offered to a director again. Roster-only checks would forget a fired
    agent's name the moment it left the roster -- this list does not."""
    if not name:
        return
    low = name.strip().lower()
    if not low:
        return
    state.setdefault('_usedNames', [])
    if low not in state['_usedNames']:
        state['_usedNames'].append(low)


def _is_fired(state, agent_id, name=None):
    """True when `agent_id` (or its name, when given) is on the durable
    fired-employee blocklist. The reentry guard checked at every agent-creation
    gate: a fired employee can never reenter the think tank, no matter which
    hire path (auto-hire, large-request team spawn) or generative name chooser
    is involved. The blocklist itself is written at the moment of firing (see
    _resolve_firing_review) and survives the fired agent leaving the roster."""
    fired = state.get('firedAgents') or {}
    if agent_id in fired:
        return True
    if name:
        low = name.strip().lower()
        return any(str(v.get('name') or '').strip().lower() == low
                   for v in fired.values())
    return False


def _name_chooser_default(state, chooser_name, used_names, for_role):
    """Default generative name chooser for a hire, late-importing serve.py so
    sim.py stays unit-testable offline. The hiring DIRECTOR (`chooser_name`,
    persona) picks a real, ordinary first name for the new agent (`for_role`),
    told exactly which names are already taken all-time. "Asked nicely" isn't
    "verified": the returned name is only trusted if it is a plain ASCII
    alphabetic word AND genuinely absent from `used_names` -- anything else
    returns None so the caller falls back to a pool name instead of risking an
    id collision. Mirrors hiring.js generateHireProfile's shape, but name-only
    (no profile fields) so a hire stays one cheap small-tier call. The call's
    cost is accrued to its own service bucket like every other model call."""
    import serve
    try:
        model = serve._low_tier_slug()
        listed = ', '.join(sorted(str(u) for u in used_names)) or '(none)'
        prompt = (
            f"You are {chooser_name}, a director at a think tank who just hired "
            f"someone as \"{for_role}\". Pick a real, ordinary first name for them "
            f"-- it must NOT be any of these already-used names: {listed}. "
            "Respond with ONLY the name: a single word, alphabetic characters only, no punctuation."
        )
        data = serve._call_openrouter_sync(
            model,
            [{'role': 'system', 'content': prompt},
             {'role': 'user', 'content': 'Pick the name.'}],
            max_tokens=10)
        cost = (data.get('usage') or {}).get('cost', 0.0)
        if isinstance(cost, (int, float)) and cost:
            serve._accrue_spend('__hire_names__', cost)
        reply = ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        token = reply.strip().strip('"').split()[0] if reply.strip() else ''
        token = token.strip('.,;:!?')
        if token.isalpha() and token.isascii():
            name = token.capitalize()
            if name.lower() not in used_names:
                return name
        return None
    except Exception:
        return None


# Injectable so tests substitute a deterministic chooser; the live loop uses
# the default (the hiring director names the hire via a real model call).
# Mirrors how _governance_decider is injected.
_hire_name_chooser = _name_chooser_default


def _spawn_team_agent(state, name, role, director_id, now_ms, grid, is_director=False,
                      elevated=False):
    """Create one brand-new agent record (roster entry + live agent) for a team
    spawned on a large-request. Mirrors _complete_auto_hire's
    structure: joins on foot at a free outdoor spot if the ACTIVE ceiling has
    room, else joins the dormant inventory. Returns the new agent id, or None
    if the name pool is exhausted."""
    new_id = name.lower()
    color_pool = HIRE_COLOR_POOL
    color = color_pool[random.randrange(len(color_pool))]
    # Reentry guard: a fired employee can never reenter under any hire path.
    if _is_fired(state, new_id, name):
        return None
    profile = {
        'mission': f"Part of the {director_id} team, taking on the think tank's newest large request.",
        'instructions': [
            f"Report to {director_id}; your work lands in the {director_id} team's shared space.",
            "You were brought in when every existing team was busy in a sprint -- your team owns the incoming large request.",
        ],
        'notes': ["Created 2026-09-27 as part of a new team for an incoming large request."],
    }
    roster_entry = {
        'id': new_id, 'name': name, 'color': color, 'role': role, 'model': 'small',
        'director': None if is_director else director_id,
        'villageId': village_of_agent(state, director_id),
        'approvedCount': 0, 'droppedCount': 0, 'weekApprovals': 0,
        'mailbox': [f"Welcome aboard -- you're on the new {director_id} team."],
        'elevatedAccess': elevated, 'accessGrant': None, 'profile': profile,
    }
    if is_director:
        roster_entry['isDirector'] = True
        roster_entry['directorSince'] = time.time()
    state.setdefault('agentRoster', []).append(roster_entry)
    can_activate = can_activate_another(state)
    spot = _free_outdoor_spot(state, grid)
    agents = state.setdefault('agents', {})
    agents[new_id] = {
        'id': new_id, 'name': name, 'color': color, 'role': role, 'profile': profile,
        'model': 'small', 'approvedCount': 0, 'droppedCount': 0, 'weekApprovals': 0,
        'villageId': village_of_agent(state, director_id),
        'mailbox': [{'text': f"Welcome aboard -- you're on the new {director_id} team.", 'read': False, 'ts': now_ms}],
        'conversationLog': [], 'lastContactedAt': None, 'hiredAt': now_ms,
        'elevatedAccess': elevated, 'accessGrant': None,
        'inRoom': None, 'roomX': None, 'roomY': None,
        'x': spot['x'], 'y': spot['y'], 'dir': 'south',
        'visible': can_activate, 'busy': False, 'meetingId': None,
        'offDuty': not can_activate,
    }
    return new_id


def _estimate_employees_for_request(goal):
    """How many employees a new team should start with for a large request
    Heuristic, not a science: breadth-of-ask signals (word count,
    explicit scoping verbs like 'build/create/platform/website') scale the
    starting headcount so a genuinely large ask isn't bottlenecked by a single
    hire. Bounded to a sane small-team range (1..3) -- a big team grows via the
    normal auto-hire loop rather than spawning a dozen agents at once."""
    text = (goal or '').strip()
    if not text:
        return 1
    words = len(text.split())
    scope_verbs = ('build ', 'create ', 'develop ', 'platform', 'website', 'app ',
                   'system', 'suite', 'full ', 'complete ', 'end-to-end')
    hits = sum(1 for v in scope_verbs if v in text.lower())
    n = 1
    if words >= 40 or hits >= 2:
        n = 2
    if words >= 120 or hits >= 4:
        n = 3
    return n


def spawn_new_team_for_request(state, goal, now_ms=None, admin_id=None, employees=None,
                               chooser=None):
    """Create a brand-new team to take an incoming large request when every
    existing team is already busy in a sprint. Spawns a new
    DIRECTOR (reporting to the admin), a fresh team record, one EMPLOYEE (or
    `employees` if more help is needed), and leaves the scrum-master role to
    the size rule: a brand-new team is small, so the DIRECTOR stands in as the
    effective scrum master (_refinement_scrum_master_for_team falls back to the
    director below SCRUM_MASTER_MIN_TEAM_SIZE) -- the employee and director
    handle ceremonies until the team grows. Then files the goal as a pending
    backlog request for the new team and kicks its refinement to be due on the
    very next pass, so the new team starts refining the large ask immediately
    rather than waiting for the weekly cadence. Names come from `chooser`
    (defaults to _hire_name_chooser): the ADMIN names the new director, and the
    new DIRECTOR names each employee -- no predetermined names, all-time
    unique. Returns the new team record, or None if the name pool / agent caps
    are exhausted."""
    chooser = chooser or _hire_name_chooser
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    roster = state.get('agentRoster') or []
    if len(roster) >= MAX_TOTAL_AGENTS:
        return None
    admin_id = admin_id or next((d.get('id') for d in roster if d.get('isAdmin')), None)
    if not admin_id:
        return None
    admin_def = next((d for d in roster if d.get('id') == admin_id), {})
    admin_name = admin_def.get('name') or admin_id
    director_name = chooser(state, admin_name, _all_used_names(state), 'Director') or _next_new_team_name(state)
    if not director_name:
        return None
    _remember_name(state, director_name)
    director_id = director_name.lower()
    grid, _doors = _load_outdoor_geometry()
    _spawn_team_agent(state, director_name, 'Director', director_id, now_ms, grid,
                      is_director=True, elevated=True)
    # New director reports to the admin so authority still resolves upward.
    dir_roster = next((d for d in state['agentRoster'] if d.get('id') == director_id), None)
    if dir_roster:
        dir_roster['director'] = admin_id
    # Team record -- the director is the id (matches _promote_to_director).
    teams = state.setdefault('teams', [])
    team = {
        'id': director_id,
        'name': f"{director_name.title()}'s Crew",
        'directorId': director_id,
        'purpose': "New team created on 2026-09-27 to take a large request while existing teams were busy in sprints.",
        'members': [],
        'createdAt': time.time(),
        'createdForRequest': (goal or '')[:200],
    }
    teams.append(team)
    member_ids = []
    emp_count = _estimate_employees_for_request(goal) if employees is None else max(1, int(employees or 1))
    for i in range(emp_count):
        # The new DIRECTOR names each employee (they're the authority for the
        # team) -- chooser falls back to the pool on a model outage.
        emp_name = chooser(state, director_name, _all_used_names(state), 'Engineer') or _next_new_team_name(state)
        if not emp_name:
            break
        _remember_name(state, emp_name)
        emp_id = _spawn_team_agent(state, emp_name, 'Engineer', director_id, now_ms, grid)
        member_ids.append(emp_id)
    team['members'] = member_ids
    # File the goal as this new team's first backlog request.
    req = {
        'id': f"wrq-{len(state.get('backlogRequests') or []) + 1}",
        'filedBy': director_id, 'title': (goal or 'Large request')[:120],
        'room': 'pressoffice',
        'reason': f"Incoming large request -- new team {director_id} was spun up to take it.",
        'filedAt': now_ms, 'status': 'pending',
        'origin': 'large_request', 'teamId': director_id,
    }
    state.setdefault('backlogRequests', []).append(req)
    # Kick this team's refinement to be immediately due (next pass), not weekly.
    kick_refinement_now(state, director_id, now_ms)
    _log_governance(state, director_id, 'new_team_for_request',
                    {'team': director_id, 'members': member_ids, 'request': req['id']})
    return team


# Phase B: the onboard ceremony mirrors the firing ceremony's convene-then-
# resolve shape (look at _start_auto_firing_review/_resolve_firing_review) but is
# deterministic -- it stages the new agent's AGENT.md progressively rather than
# asking Jev for a verdict. Stages:
#   0 -> the hire exists; the director + who'll work with them haven't started
#        meeting (embarked by _start_onboard_meeting).
#   1 -> meeting underway; director has shared the draft role / who they report
#        to / who they co-work with.
#   2 -> director has shared the concrete work to pick up.
#   3 -> readiness hold: onboarding is NOT done until the hire has actually
#        claimed a real task (observed), or a generous wall-time timeout elapses
#        so an idle/empty think tank never strands a hire unfinalized.
#   done -> profile['onboarding'] removed; AGENTS.md finalized on next sync.
_ONBOARD_STAGES = (1, 2, 3)


def _onboard_coworker_defs(state, onboard):
    """The employees who'll work with the new hire: the hire's intended `helpFor`
    plus any of the director's other direct reports. Mirrors the "people with
    firsthand knowledge" spirit of _firing_consultation."""
    agents = state.get('agents') or {}
    roster = {d.get('id'): d for d in (state.get('agentRoster') or [])}
    out = []
    for cid in onboard.get('coworkerIds') or []:
        a = agents.get(cid)
        if not a:
            continue
        out.append({'id': cid, 'name': roster.get(cid, {}).get('name') or a.get('name') or cid})
    return out


def _start_onboard_meeting(state, onboard, now_ms):
    """Convene the Town Hall: the director + the employees who'll work with the
    new hire meet at Command Center before the AGENT.md is finalized. Only if the
    director and every intended coworker are free and on-duty (same guard the
    firing reviewers use) -- if someone is mid-task/pair/handoff, defer the embark
    and re-attempt next pass rather than strand a live collaboration reference."""
    agents = state.get('agents') or {}
    director = agents.get(onboard.get('directorId'))
    if not director or director.get('busy') or director.get('offDuty'):
        return False
    coworkers = _onboard_coworker_defs(state, onboard)
    for c in coworkers:
        a = agents.get(c['id'])
        if not a or a.get('busy') or a.get('offDuty'):
            return False
    # Healthy -- embark the meeting. The director + coworkers convene at the
    # same Command Center staging the firing reviewers use (spread roomXY).
    participants = [director] + [agents[c['id']] for c in coworkers]
    for i, a in enumerate(participants):
        a['busy'] = True
        a['visible'] = False
        a['inRoom'] = 'commandcenter'
        a['dir'] = 'south'
        a['roomX'] = 315 + (i * 85)
        a['roomY'] = 155
    # Mark the meeting held on the pending record so the governance gate proceeds
    # straight to staging instead of re-embarking a fresh meeting, and push the
    # first resolve a full meet-duration past the embark (the "share the details"
    # gap before the first draft).
    onboard['embarked'] = True
    onboard['at'] = max(onboard.get('at') or 0, now_ms) + ONBOARD_MEET_DURATION_MS
    _log_governance(state, onboard['directorId'], 'onboard',
                    {'about': onboard['agentId'], 'action': 'meeting_start',
                     'directorId': onboard['directorId'],
                     'coworkers': [c['id'] for c in coworkers]})
    return True


def _onboard_hold_claimed_task(state, onboard):
    """Readiness gate: has the hire actually claimed a real co-work task yet? A
    hire is 'ready for work' the moment it holds a live task -- an agent.task
    pointer to a task in walking/working status that the hire itself still holds
    (the same 'held, not orphaned/reclaimed' notion _reclaim_orphaned_walking_tasks
    uses), and which is not simultaneously held by a different, more senior agent
    (a task someone else genuinely owns is not the hire picking up overflow).
    Pure; never decides, just reports."""
    agents = state.get('agents') or {}
    tasks = state.get('tasks') or {}
    new_id = onboard.get('agentId')
    new_agent = agents.get(new_id)
    if not new_agent:
        return False
    tid = new_agent.get('task')
    if not tid:
        return False
    task = tasks.get(tid) if isinstance(tasks, dict) else None
    if not isinstance(task, dict) or task.get('status') not in ('walking', 'working'):
        return False
    # The hire must be the one holding it (not mid-pair/handoff, not a reclaimed
    # corpse another agent took over).
    if new_agent.get('handoff') or new_agent.get('pairWith'):
        return False
    # If the task names a different assignee who still points at it, that agent
    # (not the hire) is the holder -- this isn't the hire's claimed work.
    assignee = task.get('assignedTo')
    if assignee and assignee != new_id:
        holder = agents.get(assignee)
        if holder and isinstance(holder, dict) and holder.get('task') == tid:
            return False
    return True


def _complete_onboarding(state, onboard, reason):
    """Finalize a hire: drop the `onboarding` marker + the pending record so the
    next sync_agent_directories writes AGENTS.md. `reason` is logged on the
    governance log ('claimed' when the hire proved readiness with a real task,
    'timeout' when an idle/empty think tank hit the readiness cap)."""
    agents = state.get('agents') or {}
    profile = (agents.get(onboard.get('agentId')) or {}).get('profile') or {}
    profile.pop('onboarding', None)
    _log_governance(state, onboard['directorId'], 'onboard',
                    {'about': onboard['agentId'], 'action': 'done',
                     'reason': reason, 'directorId': onboard['directorId'],
                     'coworkers': [c['id'] for c in _onboard_coworker_defs(state, onboard)]})
    state.pop('_pendingOnboard', None)


def _sim_admin_id(state):
    """The admin (the single roster entry with isAdmin=True), or None."""
    for d in (state.get('agentRoster') or []):
        if d.get('isAdmin'):
            return d.get('id')
    return None


def _work_agreement_text(state, agent):
    """The NEW HIRE drafts their own work agreement -- their words about how
    they'll work, derived from THEIR OWN profile (role, mission, access grant),
    NOT a director's instructions. This is the user's todo-17 shape: the work
    agreement is drafted BY the agents (the new hire), then EMPOWERED by the
    admin. Cheap + deterministic (no model call), mirroring the staged AGENT.md
    drafting. Returns a short plain-text agreement or None if the hire has no
    self-description to build from."""
    if not isinstance(agent, dict):
        return None
    profile = agent.get('profile') or {}
    role = agent.get('role') or ''
    mission = (profile.get('mission') or '').strip()
    grant = (agent.get('accessGrant') or '').strip()
    parts = []
    if role:
        parts.append(f"I am the {role}.")
    if mission:
        parts.append(f"My mission: {mission}")
    if grant:
        parts.append(f"My access: {grant}")
    if not parts:
        return None
    parts.append("I agree to work inside this scope: support my team's shared "
                 "space, surface what I learn, and hand work back cleanly.")
    return ' '.join(parts)


def _empower_work_agreement(state, onboard, now_ms):
    """The admin empowers the hire's drafted work agreement. Recorded on the
    hire's profile (workAgreement) + the governance log so the empower is
    durable and attributable. Returns (text, empowered_by) or (None, None) if
    there is no hire or no admin."""
    agents = state.get('agents') or {}
    new_agent = agents.get(onboard.get('agentId'))
    if not isinstance(new_agent, dict):
        return None, None
    admin_id = _sim_admin_id(state)
    text = _work_agreement_text(state, new_agent)
    if not text:
        return None, None
    profile = new_agent.setdefault('profile', {})
    profile['workAgreement'] = {
        'text': text,
        'draftedBy': onboard.get('agentId'),
        'empoweredBy': admin_id,
        'empoweredAt': now_ms,
    }
    _log_governance(state, onboard.get('agentId'), 'onboard',
                    {'about': onboard['agentId'], 'action': 'agreement_drafted',
                     'directorId': onboard['directorId']})
    if admin_id:
        _log_governance(state, admin_id, 'onboard',
                        {'about': onboard['agentId'], 'action': 'agreement_empowered',
                         'directorId': onboard['directorId']})
    return text, admin_id


def _resolve_onboard_meeting(state, onboard, now_ms):
    """After the Town-Hall duration elapses, the director stages the new agent's
    AGENT.md progressively. Everyone returns to duty; the new agent's profile is
    appended to stage by stage; stage 3 is a readiness hold that waits for the
    hire to actually claim a real co-work task before the `onboarding` marker is
    removed and the next sync_agent_directories finalizes AGENTS.md. Idempotent
    if the hire vanished."""
    agents = state.get('agents') or {}
    director = agents.get(onboard.get('directorId'))
    coworkers = _onboard_coworker_defs(state, onboard)
    for a in [director] + [agents.get(c['id']) for c in coworkers]:
        if a:
            a['busy'] = False
            a['visible'] = True
            a['inRoom'] = None
    new_agent = agents.get(onboard.get('agentId'))
    if not new_agent:
        state.pop('_pendingOnboard', None)
        return
    profile = new_agent.setdefault('profile', {})
    onboarding = profile.setdefault('onboarding', {})
    stage = onboarding.get('stage', 0)
    # The intended collaborator (the hire's `helpFor`) is who the drafts center
    # on; fall back to the first coworker, else a generic team reference.
    help_for = agents.get(onboard.get('helpForId') or '') or (
        agents.get((onboard.get('coworkerIds') or [0])[0]) if (onboard.get('coworkerIds') or []) else None)
    help_name = help_for.get('name') if help_for else onboard.get('helpForName') or 'the team'
    if stage == 0:
        onboarding['stage'] = 1
        profile.setdefault('instructions', []).insert(0,
            f"[Onboard stage 1/3 -- drafted by {onboard.get('directorName')} after a Town-Hall with the team] "
            f"You report to {onboard.get('directorName')}; your place is on their team, working with {help_name}.")
    elif stage == 1:
        onboarding['stage'] = 2
        profile.setdefault('instructions', []).insert(0,
            f"[Onboard stage 2/3] Pick up overflow work for {help_name}"
            f"{((' (' + help_for.get('role', '') + ')') if help_for and help_for.get('role') else '')}: "
            "that is your concrete assignment from the meeting.")
    elif stage == 2:
        # Stage 2 is fully drafted; the NEW HIRE drafts their own work agreement
        # (their words from their own profile -- not the director's), and the
        # ADMIN empowers it, before moving into the readiness hold. Stamp when
        # the hold began so the timeout is measured from here, not from hire
        # time.
        _empower_work_agreement(state, onboard, now_ms)
        onboarding['stage'] = 3
        onboarding['holdSince'] = now_ms
        _log_governance(state, onboard['directorId'], 'onboard',
                        {'about': onboard['agentId'], 'action': 'staged',
                         'stage': 3, 'directorId': onboard['directorId']})
        return
    else:  # stage 3 -- readiness hold
        held = _onboard_hold_claimed_task(state, onboard)
        elapsed = now_ms - (onboarding.get('holdSince') or now_ms)
        if held:
            _complete_onboarding(state, onboard, 'claimed')
        elif elapsed >= ONBOARD_READINESS_TIMEOUT_MS:
            # No task appeared in time -- idle/empty think tank. Finalize anyway so
            # the hire isn't stranded; they'll take upstream work as it arrives.
            _complete_onboarding(state, onboard, 'timeout')
        else:
            # Still waiting on real work. Do NOT advance `at` to a premature done;
            # leave it so the governance pass re-checks every tick. Log the wait
            # once, on entering the hold, not on every tick (would spam the log).
            if not onboarding.get('holdLogged'):
                onboarding['holdLogged'] = True
                _log_governance(state, onboard['directorId'], 'onboard',
                                {'about': onboard['agentId'], 'action': 'readiness_hold',
                                 'directorId': onboard['directorId']})
        return
    # Stage 0 -> 1 / 1 -> 2 progressive drafting: each stage waits its own gap so
    # the director's AGENT.md lands in stages, not all at once.
    onboard['at'] = now_ms + ONBOARD_MEET_DURATION_MS
    _log_governance(state, onboard['directorId'], 'onboard',
                    {'about': onboard['agentId'], 'action': 'staged',
                     'stage': onboarding.get('stage'), 'directorId': onboard['directorId']})


# ---------------------------------------------------------------------------
# Weekly cross-team Knowledge Social.
#
# A scheduled 30-minute conversation in the Hangout where every agent that
# produced an approved deliverable THIS WEEK gets to talk about it with agents
# from other teams. It's the one channel the task graph never opens: different
# teams work disjoint queue items, so their accumulated know-how is otherwise
# siloed. The event is the carrier that fans real completed work across team
# boundaries. Non-eligible agents (no week's work) are never pulled in.
#
# Mirrors the onboard ceremony's convene-then-resolve shape but with two sharp
# differences that carry the user's intent:
#   - Stateful restore: every attendee returns EXACTLY to their prior state on
#     resolve. A mid-task worker keeps her task (budget extended so the 30 min
#     doesn't burn it); an off-duty agent is woken for the event and returned
#     off-duty; an idle on-duty one returns to idle.
#   - Weekly eligibility: `weekApprovals` > 0 (bumped with approvedCount at
#     both completion paths) and reset to 0 when the event resolves, so
#     eligibility is measured event-to-event.
#
# Runs UNGATED (from _task_cycle, not _governance_pass) because the conversation
# is the point even on an otherwise-idle think tank.
# ---------------------------------------------------------------------------
def _social_decider_default(instructions, criteria):
    """Default Jev resolver for a single Knowledge Social 'what should I carry
    away' decision. Mirrors _governance_decider_default (late-import serve so
    sim.py stays importable offline); returns (choice, confidence) or (None,
    1.0) on a Jev outage so an event never blocks on the network."""
    import serve
    try:
        data = serve._call_openrouter_decision_sync(
            serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice', 'instructions': instructions,
                        'criteria': {c['id']: c['description'] for c in criteria}}})
        choice, confidence, _ = serve._jev_choice(data)
        return (choice, confidence) if any(c['id'] == choice for c in criteria) else (None, confidence or 1.0)
    except Exception:
        return None, 1.0


# Injectable so tests substitute a deterministic decider; the live loop uses Jev.
_social_decider = _social_decider_default


def _social_attendees(state, pending):
    """The eligible, physically-present agents who'll join the Hangout: those
    with week work (weekApprovals > 0) who are not already mid-ceremony and not
    the admin in the middle of something. Excludes anyone currently inside a
    pair/handoff/firing-review/onboard so we never strand a live collaboration
    reference. Returns {agent_id: True} for those pulled in."""
    agents = state.get('agents') or {}
    out = {}
    for aid, a in (agents or {}).items():
        if not isinstance(a, dict):
            continue
        if (a.get('weekApprovals') or 0) <= 0:
            continue  # didn't work this week -- not eligible
        if a.get('handoff') or a.get('pairWith'):
            continue  # mid-a different live collaboration -- don't yank
        out[aid] = True
    return out


def _convene_social(state, pending, now_ms):
    """Pull every eligible present agent into the Hangout and snapshot their
    prior state so resolve can restore it exactly. Workers keep their task claim
    (flagged on the task so the orphan-reclaim pass won't re-issue it mid-event)
    but are detached from busy/inRoom/position for the conversation. Idempotent:
    only convenes once (pending['embarked'])."""
    agents = state.get('agents') or {}
    attendees = _social_attendees(state, pending)
    tasks = state.get('tasks') or {}
    people = {}
    spread = 0
    for aid in attendees:
        a = agents.get(aid)
        if not isinstance(a, dict):
            continue
        tid = a.get('task')
        # Snapshot the pre-event state. For a worker, remember the task and mark
        # it as inside the social so the orphan-reclaim pass skips it; extend its
        # workUntil later so the meet doesn't eat the budget.
        snapshot = {
            'offDuty': bool(a.get('offDuty')),
            'visible': bool(a.get('visible')),
            'x': a.get('x'), 'y': a.get('y'), 'dir': a.get('dir'),
            'task': tid,
            'busy': bool(a.get('busy')),
            'inRoom': a.get('inRoom'),
        }
        if tid and isinstance(tasks, dict):
            t = tasks.get(tid)
            if isinstance(t, dict):
                snapshot['workUntil'] = t.get('workUntil')
                t['_inSocial'] = now_ms
        people[aid] = snapshot
        # Bring them into the Hangout (visible + recessed so nothing re-parks or
        # re-assigns a woken off-duty attendee mid-event).
        a['busy'] = True
        a['visible'] = True
        a['offDuty'] = False
        a['task'] = None
        a['inRoom'] = 'hangout'
        a['dir'] = 'south'
        a['roomX'] = 100 + (spread * 55)
        a['roomY'] = 200
        spread += 1
    pending['people'] = people
    pending['embarked'] = True
    # Item 8: co-location at the social ceremony is the marker that two agents
    # actually met -- every pair brought into the Hangout becomes acquainted
    # (symmetric, idempotent, JSON-safe). This is what lets the perception
    # present-list name them: a stranger stays "the Banking", an attendee you
    # shared a ceremony with becomes "Ben (the Banking)".
    met = [aid for aid in attendees if aid in people]
    for i, a in enumerate(met):
        for b in met[i + 1:]:
            _mark_acquaintance(state, a, b)
    # The conversation runs 30 minutes from convene (convene is the true start; a
    # schedule-time preliminary `at` is superseded rather than compounded).
    pending['at'] = now_ms + SOCIAL_MEET_MS
    _log_governance(state, None, 'social',
                    {'action': 'meeting_start', 'attendees': sorted(people)})
    return state


def _restore_social_agent(state, aid, snapshot, now_ms, duration_ms):
    """Restore one attendee to exactly the state _convene_social snapshotted.
    Workers get their task claim + position + busy back, and the task's workUntil
    is extended by the social duration so the conversation burned none of their
    budget. Off-duty agents return to off-duty (vanish). Idle on-duty ones return
    to idle. Best-effort if the agent vanished."""
    a = (state.get('agents') or {}).get(aid)
    if not isinstance(a, dict):
        return
    tid = snapshot.get('task')
    if snapshot.get('offDuty'):
        # Was off-duty -- return to off-duty in place.
        a['busy'] = False
        a['visible'] = False
        a['offDuty'] = True
        a['inRoom'] = None
        a['task'] = None
    elif tid:
        # Was working -- reclaim the task, restore position + busy + inRoom, and
        # give back the budget the meet consumed.
        a['task'] = tid
        a['busy'] = True
        a['inRoom'] = snapshot.get('inRoom')
        tasks = state.get('tasks') or {}
        t = tasks.get(tid)
        if isinstance(t, dict):
            t.pop('_inSocial', None)
            su = snapshot.get('workUntil')
            if t.get('status') in ('walking', 'working') and su:
                # workUntil is in seconds; give back the exact minutes the meet
                # took so no work budget was burned by the conversation.
                t['workUntil'] = (su or 0) + (duration_ms // 1000)
            a['visible'] = True
    else:
        # Was idle on-duty -- back to idle.
        a['busy'] = False
        a['visible'] = True
        a['offDuty'] = False
        a['task'] = None
        a['inRoom'] = None
    a['x'] = snapshot.get('x', a.get('x'))
    a['y'] = snapshot.get('y', a.get('y'))
    a['dir'] = snapshot.get('dir', a.get('dir'))


def _task_is_time_critical(t):
    """True when an in-flight task is TIME-CRITICAL -- i.e. it was itself
    scheduled for a specific time or runs on a standing cadence -- so it must
    never be yanked off the agent for a one-off request. Scheduled cards stamp
    `notBefore` at assignment (see _assign_due_item / assign_task); standing
    cadence cards (research/skillReview/distill) already mark themselves.
    Ordinary queue work and sprint cards are NOT time-critical: a one-off may
    interrupt them. The callers keep the sim's existing carve-outs (peer
    review/incident/shadow/social) as hard never-suspend rules regardless."""
    return bool(t.get('notBefore') or t.get('research')
                or t.get('skillReview') or t.get('distill'))


def _suspend_busy_agent_for_scheduled(state, grid, doors, now_ms,
                                      skip_time_critical=False):
    """Preemption for an item that must interrupt a busy agent. A due SCHEDULED
    item (queue_once notBefore) fires at its specified time even when every
    agent is busy -- typically at the active ceiling, where can_activate_another
    is False so no off-duty agent can be woken. A ONE-OFF request may also
    interrupt -- but ONLY ordinary/sprint work: with `skip_time_critical=True`
    it never yanks an agent off a scheduled/standing (time-critical) card. Pick
    ONE busy on-duty agent whose current task is safely suspendable (status
    'working', not a peer review/incident/shadow, not at the social, not already
    suspended), snapshot + park her task (flagged _suspendedForScheduled so the
    orphan-reclaim and stale-work sweeps skip it), and free her. The snapshot
    lives on the agent as `_suspendedTask`; task completion resumes it (see
    send_agent_off_duty / _resume_suspended_task). Returns the freed agent id,
    or None when nothing is safely suspendable."""
    agents = state.get('agents') or {}
    roster = state.get('agentRoster') or []
    tasks = state.get('tasks') or {}
    best = None
    best_remaining = -1.0
    for d in roster:
        if d.get('isAdmin'):
            continue
        aid = d.get('id')
        a = agents.get(aid)
        if not isinstance(a, dict) or a.get('offDuty') or not a.get('busy'):
            continue
        if a.get('pairWith') or a.get('handoff') or a.get('_suspendedTask'):
            continue
        tid = a.get('task')
        t = tasks.get(tid) if isinstance(tasks, dict) else None
        if not isinstance(t, dict) or t.get('status') != 'working':
            continue
        if t.get('_inSocial') or t.get('_suspendedForScheduled'):
            continue
        if t.get('reviewOf') or t.get('incident') or t.get('shadow'):
            continue
        if skip_time_critical and _task_is_time_critical(t):
            continue
        # Prefer the agent furthest from completion: interrupting a task about
        # to finish would cost the most, and we give every parked task its full
        # budget back on resume anyway.
        remaining = (t.get('workUntil') or 0) - now_ms / 1000.0
        if remaining > best_remaining:
            best_remaining = remaining
            best = (aid, a, tid, t)
    if best is None:
        return None
    aid, a, tid, t = best
    a['_suspendedTask'] = {
        'task': tid,
        'workUntil': t.get('workUntil'),
        'x': a.get('x'), 'y': a.get('y'), 'dir': a.get('dir'),
        'inRoom': a.get('inRoom'),
        'at': now_ms,
    }
    t['_suspendedForScheduled'] = now_ms
    a['task'] = None
    a['busy'] = False
    a['inRoom'] = None
    a['visible'] = True
    a['path'] = None
    a['pathIndex'] = 0
    a['pathTarget'] = None
    return aid


def _resume_suspended_task(state, agent_id, a, grid=None):
    """Restore an agent whose task was suspended so a due scheduled item could
    fire: reclaim the task claim + position + busy + inRoom, clear the parked
    flag, and give back the exact time the scheduled item consumed (workUntil
    extended) so the preemption burned none of the parked task's budget. Mirrors
    _restore_social_agent. Returns True when resumed; False when there was no
    snapshot (or the parked task was reclaimed/abandoned) -- the caller then
    proceeds to go off duty."""
    if not isinstance(a, dict):
        return False
    snap = a.pop('_suspendedTask', None)
    if not snap:
        return False
    tid = snap.get('task')
    tasks = state.get('tasks') or {}
    t = tasks.get(tid) if isinstance(tasks, dict) else None
    if not isinstance(t, dict):
        return False  # parked task was reclaimed/abandoned -- nothing to return to
    a['task'] = tid
    a['busy'] = True
    a['inRoom'] = snap.get('inRoom')
    a['offDuty'] = False
    a['visible'] = True
    t.pop('_suspendedForScheduled', None)
    su = snap.get('workUntil')
    elapsed_s = max(0, int(time.time() * 1000) - (snap.get('at') or 0)) // 1000
    if t.get('status') in ('walking', 'working') and su:
        t['workUntil'] = (su or 0) + elapsed_s
    a['x'] = snap.get('x', a.get('x'))
    a['y'] = snap.get('y', a.get('y'))
    a['dir'] = snap.get('dir', a.get('dir'))
    return True


def _resolve_social(state, pending, now_ms, decider=None):
    """End the conversation: each attendee returns to their prior state,
    `weekApprovals` resets for the next weekly window, and a short cross-team
    digest (each attendee's latest approved deliverable) is written to the shared
    library social log -- the concrete evidence that the knowledge actually fanned
    out. Idempotent; the pending record is dropped.

    Decision tape (the Jev-article lesson): before restore, each attendee answers
    one typed decision -- what they'll DO with what they saw -- and the (choice,
    confidence) is logged to action_log. That turns the conversation from a digest
    of titles into a tape of decisions that can steer downstream behavior, and
    gives the falsifiable signal: if a team actually ADOPTS something they learned
    (vs. noting/skipping), the event has real impact. Never load-bearing: a Jev
    outage yields (None, 1.0) and is logged as such, never blocking the restore."""
    agents = state.get('agents') or {}
    roster = {d.get('id'): d for d in (state.get('agentRoster') or []) if isinstance(d, dict)}
    people = pending.get('people') or {}
    decider = decider or _social_decider
    # Build a short prompt of who was there + what work they'd done (from the
    # live roster names + their week tally), so the decision is grounded in the
    # actual cross-team surface, not a generic "did you enjoy it".
    present = []
    for aid in sorted(people):
        name = roster.get(aid, {}).get('name') or (agents.get(aid) or {}).get('name') or aid
        tally = (agents.get(aid) or {}).get('weekApprovals') or 0
        present.append(f"{name} (delivered {tally} approved work this week)")
    for aid in sorted(people):
        a = agents.get(aid)
        if not isinstance(a, dict):
            continue
        name = roster.get(aid, {}).get('name') or a.get('name') or aid
        instructions = (
            f"You're {name}, at a cross-team Knowledge Social. You saw real work "
            f"from other teams: {', '.join(p for p in present if not p.startswith(name.split()[0])) or 'your own team'}. "
            "One typed decision: what will you carry away from what others shared?"
        )
        criteria = [
            {'id': 'adopt', 'description': 'I will actually try/apply this in my own work (a concrete technique, tool, or approach I did not use before).'},
            {'id': 'note', 'description': 'Interesting but uncertain; I will keep it in mind / research it before committing.'},
            {'id': 'skip', 'description': 'Nothing here applies to my work; no change.'},
        ]
        choice, confidence = decider(instructions, criteria)
        record = {k: v for k, v in (a.get('profile', {}) or {}).items() if k in ('mission', 'instructions', 'notes')}
        state.setdefault('_socialDecisions', []).append({
            'agentId': aid, 'name': name, 'choice': choice, 'confidence': confidence,
            'at': now_ms, 'attendees': sorted(people),
            'weekApprovals': a.get('weekApprovals') or 0,
            'profile': record,
        })
        # The durable tape: one action_log row per decision (like the article's
        # logged typed calls) so adoptions are queryable later.
        _log_governance(state, aid, 'social_carryaway',
                        {'choice': choice, 'confidence': confidence,
                         'attendees': sorted(people), 'weekApprovals': a.get('weekApprovals') or 0})
        # W2: an 'adopt' carry-away with real confidence must LAND -- it changes
        # the worker's NEXT execution, not just a digest line. Route a coaching
        # note through the existing growth-plan loop (repeat=True: each fresh
        # weekly adopt is a new commitment, so it lands even if a prior one of
        # the same kind is still queued/applied).
        if choice == 'adopt' and isinstance(confidence, (int, float)) \
                and confidence >= SOCIAL_ADOPT_CONFIDENCE:
            _write_growth_plan(
                state, aid, 'hangout', 'social_adopt', now_ms,
                f"Knowledge Social adopt (confidence {confidence:.2f}): you committed to "
                f"actually trying/applying what you saw cross-team. Do that on this "
                f"task, not just note it.", repeat=True)
    duration = SOCIAL_MEET_MS
    for aid, snapshot in people.items():
        _restore_social_agent(state, aid, snapshot, now_ms, duration)
    # Roll the week window: everyone's next-week tally starts at zero.
    for a in (agents or {}).values():
        if isinstance(a, dict):
            a['weekApprovals'] = 0
    try:
        from serve import log_social_digest
        log_social_digest(state, people, state.get('_socialDecisions') or [])
    except Exception:
        pass
    _log_governance(state, None, 'social', {'action': 'done'})
    state.pop('_pendingSocial', None)


def _social_step(state, now, now_ms, decider=None):
    """One weekly-social pass, called from _task_cycle (ungated so it fires even
    on an idle think tank). Schedules the next event on cadence; convenes eligible
    present attendees when due; resolves when the 30 minutes elapse. `decider`
    injectable for tests (defaults to the Jev-backed _social_decider)."""
    pending = state.get('_pendingSocial')
    if pending is not None:
        if not pending.get('embarked'):
            _convene_social(state, pending, now_ms)
        elif now_ms >= pending.get('at', 0):
            _resolve_social(state, pending, now_ms, decider=decider)
        return
    last = state.get('lastSocialAt') or 0
    if now_ms - last >= SOCIAL_CADENCE_MS:
        state['lastSocialAt'] = now_ms
        state['_pendingSocial'] = {
            'at': now_ms + SOCIAL_MEET_MS,
            'embarked': False,
            'people': {},
        }


# ---------------------------------------------------------------------------
# Backlog refinement (Cut 1). The scrum master -- a standing,
# director-designated per-team role -- is RESPONSIBLE for turning agent-filed
# work into real stories. Agents communicate what needs doing by FILING a
# work-request; a ceremony (`pendingRefinements[team_id]`) convenes the scrum
# master with the filing agents at the Command Center, and at resolve the Jev
# refinement decider grooms each request into a `queue_work` story (accept) or
# back to the requester (reject). Runs ungated like the Social -- the ceremony
# is the point even on a quiet think tank -- but no-ops when there are no pending
# requests or no scrum master to run it, so we never convene an empty meeting.
#
# Human boundaries preserved: ceremony stories enter the workQueue tagged
# source:'refinement' + groomedBy:<scrum-master> at normal priority; the
# existing /api/intent/sprint (director/human) surface and story-reject path
# are untouched, so a real stakeholder keeps full overriding authority.
# ---------------------------------------------------------------------------

# Desk-tested cadence stamps: a stale TEST sentinel far in the future (e.g.
# 1e18) must not permanently silence the ceremony -- same rule as the skill-
# review sweep and the Social (handled inside _refinement_cadence_due_for).

def _pending_work_requests(state):
    """Pending (not-yet-groomed) work-requests a refinement ceremony will read."""
    return [r for r in (state.get('backlogRequests') or []) if r.get('status') == 'pending']


def file_work_request(state, agent_id, title, room, reason=None):
    """An on-duty agent communicates "this work still needs doing" to the scrum
    master by filing a work-request. Pure intake -- never decides, never queues
    a story by itself; a ceremony grooms it. Dedups a near-identical pending
    request so churn can't spam the backlog. Returns the new request dict, or
    None if a matching pending one already exists / the request is malformed."""
    room = (room or '').strip()
    title = (title or '').strip()
    if not room or not title or room not in VALUED_QUEUE_ROOMS:
        return None
    for r in (state.get('backlogRequests') or []):
        if (r.get('status') == 'pending' and r.get('room') == room
                and r.get('title') and title.lower() == r['title'].lower()):
            return None
    req = {
        'id': f"wrq-{len(state.get('backlogRequests') or []) + 1}",
        'filedBy': agent_id, 'title': title, 'room': room,
        'reason': (reason or 'Pending follow-up in a thinning room').strip(),
        'filedAt': int(time.time() * 1000), 'status': 'pending',
    }
    state.setdefault('backlogRequests', []).append(req)
    return req


def file_large_request(state, authority_id, goal, team_id, now_ms=None):
    """File a staffable large ask as a pending BREAKDOWN request for a team's
    breakdown ceremony to card into stories/spikes. The ask is attributed to
    the free authority who chose the team and tagged with the receiving team
    so the breakdown ceremony convenes the RIGHT team's scrum master + workers.
    Never predicts subtasks up front -- the ceremony does that. Returns the new
    request dict, or None if malformed."""
    goal = (goal or '').strip()
    if not goal or not team_id:
        return None
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    req = {
        'id': f"wrq-{len(state.get('backlogRequests') or []) + 1}",
        'filedBy': authority_id, 'title': goal[:120], 'room': 'pressoffice',
        'reason': f"Large request: {goal[:200]}", 'goal': goal,
        'filedAt': now_ms, 'status': 'pending', 'teamId': team_id,
        'origin': 'large_request', 'breakdown': True,
    }
    state.setdefault('backlogRequests', []).append(req)
    return req


# ---------------------------------------------------------------------------
# JIRA-like issue register. A `state['issues']` dict keyed by a human-readable
# per-team key (TEAM-0128) with a monotonic per-team counter in
# `state['issueCounters']`, mirroring next_sprint_id/next_product_id so a cold
# state starts at 1 and a hot one never collides. Filing an issue ALSO writes a
# backlogRequests record tagged with its issueKey + teamId, routing it into the
# owning team's scrum-master refinement ceremony (the think tank's proven path for
# turning a card into worked agent labor) -- it is not a dead ledger.
# ---------------------------------------------------------------------------

ISSUE_TYPES = ('story', 'spike', 'bug', 'task')
# `blocked` is a boolean FIELD on an issue (issue['blocked'], default False), NOT
# a status. Only the owning team's scrum master commits it (via _file_block_change
# + _block_step), so it stays a single player-facing "this card can't be or needn't
# be worked right now" flag. It is EARNED two ways -- requirements-met (supervisor-
# approved) or stuck-on-player (director-approved ask) -- and CLEARED two ways --
# the player responded or the agent self-resolved. Distinct from `status`, which
# tracks the work lifecycle (open/in_progress/done/closed).
ISSUE_STATUSES = ('open', 'in_progress', 'done', 'closed')

# Canonical Jira description blocks. A description, when supplied, must follow
# one of the two templates the think tank understands:
#   userStory          "As a <role>, I want to <action>, so that <value>"
#   acceptanceCriteria "Given <context>, When <event>, Then <outcome>"
_STORY_PARTICLES = ('as a', 'i want to', 'so that')
_GWT_PARTICLES = ('given', 'when', 'then')


def _blocks_cover(text, particles, delim=',', start=0):
    """True if every particle in `particles` appears, in order, in `text`."""
    pos = start
    for p in particles:
        i = text.lower().find(p, pos)
        if i < 0:
            return False
        pos = i + len(p)
    return True


def normalize_story_block(text):
    """Normalize a user-story block ("As a..., I want to..., so that...") to a
    canonical joined string, or None if it doesn't contain all three markers."""
    text = (text or '').strip()
    if not text:
        return None
    if not _blocks_cover(text, _STORY_PARTICLES):
        return None
    low = text.lower()
    # Rebuild as "As a <role>, I want to <action>, so that <value>".
    parts = []
    idx_as = low.find('as a')
    idx_iwant = low.find('i want to')
    idx_sothat = low.find('so that')
    parts.append(text[idx_as:idx_iwant].strip())
    parts.append(text[idx_iwant:idx_sothat].strip())
    parts.append(text[idx_sothat:].strip())
    return ', '.join(p.strip().rstrip(',') for p in parts)


def normalize_gwt_block(text):
    """Normalize a Given/When/Then block to a canonical joined string, or None
    if it doesn't contain all three markers."""
    text = (text or '').strip()
    if not text:
        return None
    if not _blocks_cover(text, _GWT_PARTICLES):
        return None
    low = text.lower()
    idx_g = low.find('given')
    idx_w = low.find('when')
    idx_t = low.find('then')
    parts = [text[idx_g:idx_w].strip(),
             text[idx_w:idx_t].strip(),
             text[idx_t:].strip()]
    return ', '.join(p.strip().rstrip(',') for p in parts)
def _default_team_prefix(team_id):
    """A deterministic default prefix from a team id slug (dev -> DEV). Only
    auto-derived; an explicit `prefix` on the team record always wins."""
    slug = (team_id or 'TEAM').upper()
    cleaned = ''.join(ch for ch in slug if ch.isalnum())
    return (cleaned or 'TEAM')[:5]


def team_prefix(state, team_id):
    t = _team_row(state, team_id)
    if t and t.get('prefix'):
        prefix = ''.join(ch for ch in str(t['prefix']).upper() if ch.isalnum())
        if prefix:
            return prefix[:5]
    return _default_team_prefix(team_id)


def set_team_prefix(state, team_id, prefix):
    """Set/clear an explicit issue prefix on a team record. Returns the applied
    prefix, or None if the team is unknown or the supplied prefix is invalid
    (must be 1-5 alnum; empty clears back to the derived default)."""
    t = _team_row(state, team_id)
    if not t:
        return None
    prefix = (prefix or '').strip().upper()
    cleaned = ''.join(ch for ch in prefix if ch.isalnum())
    if cleaned and len(cleaned) > 5:
        return None
    t['prefix'] = cleaned or None
    return team_prefix(state, team_id)


def next_issue_key(state, team_id):
    """Server-side monotonic per-team issue key (DEV-1, DEV-2, ...), keyed off
    the team's prefix + counter so a cold state starts at 1 and a hot one never
    collides. No leading zero padding (the player asked for DEV-1, not DEV-0001)."""
    prefix = team_prefix(state, team_id)
    counters = state.setdefault('issueCounters', {})
    n = counters.get(prefix, 0) + 1
    counters[prefix] = n
    return f"{prefix}-{n}"


def _as_block_text(value):
    """Coerce a description block (string, or list of strings as sent by a JSON
    client for acceptanceCriteria) into a single text blob for normalization."""
    if isinstance(value, list):
        return '\n'.join(str(item) for item in value if item is not None)
    return value


def _normalize_description(description):
    """A description dict {userStory, acceptanceCriteria} or a plain string is
    split into the two canonical description blocks. Returns
    (userStory, acceptanceCriteria) -- each normalized to its template, or None
    when absent/malformed. The think tank understands exactly two Jira description
    shapes: the user story ("As a..., I want to..., so that...") and the
    acceptance criteria ("Given..., When..., Then..."). `acceptanceCriteria`
    may arrive as a list of criteria strings (the natural JSON shape); the list
    is joined into one block before normalization."""
    if isinstance(description, dict):
        return (normalize_story_block(_as_block_text(description.get('userStory'))),
                normalize_gwt_block(_as_block_text(description.get('acceptanceCriteria'))))
    text = (description or '').strip()
    if not text:
        return None, None
    if _groups_look_like_gwt(text):
        return None, normalize_gwt_block(text)
    return normalize_story_block(text), None


def _groups_look_like_gwt(text):
    """Auto-detect a stray plain-string description as Given/When/Then when it
    carries all three markers (else it's treated as a raw user-story block)."""
    low = text.lower()
    return (low.find('given') < low.find('when') < low.find('then')
            and -1 < low.find('given'))


def file_issue(state, team_id, issue_type, summary, feature, reporter_id,
               description='', story_points=None, title=None, now_ms=None,
               assigned_to=None, lane=None):
    """File a JIRA-style issue for team_id. Returns the issue record dict, or
    None if a required field is missing (team_id / issue_type / summary /
    feature / reporter_id), the issue type is unknown, or the team is unknown.
    `feature` is the Jira project/area the work belongs to (required).
    `description` is optional but structured when present: pass a dict
    `{'userStory': ..., 'acceptanceCriteria': ...}` or a plain string (each
    block, when given, is normalized to "As a..., I want to..., so that..." /
    "Given..., When..., Then..." and dropped if it doesn't follow its template).
    `title` is an OPTIONAL one-line headline kept DISTINCT from `summary` so an
    agent scanning a long list can grasp the card without reading the whole
    description/criteria; defaults to `summary` when omitted.
    Human-in-the-loop: `assigned_to='player'` marks the issue as the PLAYER's
    to do -- the ownership survives file_issue -> refinement -> queue, and the
    card is handed to the player (not an agent) at assignment. An agent (the
    scrum master filing the issue) uses this to hand a story/spike to the
    player when the next step genuinely needs human hands.
    Pure: mutates state['issues'], state['issueCounters'], and appends a
    backlogRequests record (tagged issueKey + teamId) so the owning scrum
    master can groom the card into a sprint -- the think tank does the work."""
    team_id = (team_id or '').strip() if isinstance(team_id, str) else team_id
    issue_type = (issue_type or '').strip().lower()
    summary = (summary or '').strip()
    feature = (feature or '').strip()
    reporter_id = (reporter_id or '').strip()
    if not team_id or not issue_type or not summary or not feature or not reporter_id:
        return None
    if issue_type not in ISSUE_TYPES:
        return None
    if not _team_row(state, team_id):
        return None
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    key = next_issue_key(state, team_id)
    story, criteria = _normalize_description(description)
    title = (title or '').strip() or summary
    issue = {
        'key': key,
        'teamId': team_id,
        'teamKey': team_prefix(state, team_id),
        'type': issue_type,
        'summary': summary,
        'title': title,  # one-line scan headline; distinct from summary/description
        'feature': feature,
        'userStory': story,
        'acceptanceCriteria': criteria,
        'storyPoints': story_points,
        'reporterId': reporter_id,
        'createdAt': now_ms,
        'status': 'open',
        'blocked': False,  # SM-committed boolean field -- see _file_block_change
        'blockedAt': None,
        'blockedBy': None,
        'assignedTo': assigned_to or None,  # 'player' = the human does this one
        'lane': normalize_lane(lane),       # attention lane (build/reading/open/parking-lot)
    }
    state.setdefault('issues', {})[key] = issue
    # Feed the backlog pipe: a backlogRequests record the refinement ceremony
    # grooms. Tags teamId explicitly so a player-filed issue (no director to
    # derive a team from) still routes to the owning team's scrum master.
    req = {
        'id': f"iss-{key}",
        'filedBy': reporter_id,
        'title': f"[{issue_type.upper()}] {summary}",
        'room': 'pressoffice',
        'reason': f"JIRA issue {key}",
        'filedAt': now_ms,
        'status': 'pending',
        'origin': 'jira_issue',
        'issueKey': key,
        'teamId': team_id,
        'issueType': issue_type,
        # Human-in-the-loop: a card filed for the PLAYER stays the player's
        # through refinement (assignedTo survives onto the queued item, and
        # _assign_due_item hands it to the player -- see _resolve_refinement).
        'assignedTo': assigned_to or None,
        # The attention lane the filed card rides -- survives file_issue ->
        # refinement -> queue so a parked or build-lane card keeps its lane
        # (see _resolve_refinement, which forwards req['lane'] onto the queue).
        'lane': normalize_lane(lane),
        # The player-facing contract: the normalized user story + acceptance
        # criteria travel with the backlog request so the refinement ceremony can
        # hand the WORKER the full spec, not just the one-line summary (see
        # _resolve_refinement, which embeds them into the coding task's
        # instructions).
        'userStory': story,
        'acceptanceCriteria': criteria,
    }
    state.setdefault('backlogRequests', []).append(req)
    # Kick this team's refinement to be immediately due (next pass), not
    # weekly -- a freshly-filed JIRA issue shouldn't wait out the cadence
    # stamp a previous grooming just set. No-op if the team already has an
    # in-flight ceremony (it will groom this card) or its cadence is due.
    kick_refinement_now(state, team_id, now_ms)
    return issue


def list_issues(state, team_id=None):
    """All issues, newest first, optionally filtered to one team (by id).
    Returns a list of issue records in insertion order."""
    issues = (state.get('issues') or {}).values()
    out = list(issues)
    if team_id:
        out = [i for i in out if i.get('teamId') == team_id]
    out.sort(key=lambda i: i.get('createdAt', 0), reverse=True)
    # Defensively surface fields on pre-existing rows (older issues filed before
    # the field existed won't carry it yet).
    for i in out:
        i.setdefault('blocked', False)
        i.setdefault('title', i.get('summary'))
    return out


def set_issue_status(state, key, status):
    """Transition an issue's status (open/in_progress/done/closed) and mirror it
    onto any linked pending backlog request so the refinement pipe stays in
    sync. Terminal states (`done`, `closed`) release the worker by resolving the
    linked request and clear any pending player-input ask (a finished card
    shouldn't keep an agent waiting). Returns the updated issue, or None if
    unknown/invalid. `blocked` is NOT a status here -- it's a boolean field the
    scrum master commits separately (see _file_block_change / _block_step)."""
    status = (status or '').strip().lower()
    if status not in ISSUE_STATUSES:
        return None
    issue = (state.get('issues') or {}).get(key)
    if not issue:
        return None
    issue['status'] = status
    # A terminal status supersedes a pending request to PLAYER input: the agent
    # was waiting on you, but the card is now finished/closed -- stop waiting and
    # drop any outstanding block-change intended to gate it.
    if status in ('done', 'closed') and issue.get('needsInput'):
        issue['needsInput'] = False
        for m in (state.get('playerInbox') or []):
            if m.get('issueKey') == key and m.get('status') == 'awaiting_input':
                m['status'] = 'superseded'
    for r in (state.get('backlogRequests') or []):
        # Mirror onto the linked request whether it's still pending or already
        # accepted (an in_progress -> done hop shouldn't strand it at accepted).
        if r.get('issueKey') == key and r.get('status') in ('pending', 'accepted'):
            if status in ('done', 'closed'):
                r['status'] = 'resolved'
            elif status == 'in_progress':
                r['status'] = 'accepted'
    return issue


# ---------------------------------------------------------------------------
# Director-gated player escalation. When an agent decides a task genuinely can't
# proceed without the player, it does NOT message you directly -- it files a
# needs-input request that the owning team's DIRECTOR reviews first (the "do
# they really need my input?" gate). Only if the director returns an `ask_player`
# verdict does a player-inbox message go out; a `resolve_internally` verdict
# drops the block and the agent resumes. An agent blocked on the player carries
# a `_awaitingPlayer` flag so the sim stops assigning it work (WAIT, not
# abandon) but keeps it visible/persisted; a terminal `blocked` issue status
# supersedes the ask and frees the worker. All pure/mutate-state, mirroring the
# escalation-groom ceremony's injectable-decider shape.
# ---------------------------------------------------------------------------
PLAYER_ASK_VALUES = ('ask_player', 'resolve_internally')


def _issue_owning_team(state, issue_key):
    """The team record owning issue_key (by the issue's stored teamId), or None."""
    issue = (state.get('issues') or {}).get(issue_key)
    if not issue:
        return None
    return _team_row(state, issue.get('teamId'))


def _issue_director(state, issue_key):
    """The director id of the team owning issue_key, or None."""
    t = _issue_owning_team(state, issue_key)
    return t.get('directorId') if t else None


def request_player_input(state, issue_key, agent_id, question, context=None,
                         now_ms=None):
    """Record that an agent wants the player's input on issue_key, but ONLY
    surface it after the director gate. If no director-gate is already pending
    for this issue, seeds `state['_pendingPlayerAsk']` (the sweep hands it to
    the director). Returns the awaited inbox message id once the gate has run,
    else None (gate still pending / issue unknown / issue already needsInput).
    Mutates state only -- the director sweep resolves the pending ask."""
    if _issue_director(state, issue_key) is None:
        return None
    issue = (state.get('issues') or {}).get(issue_key)
    if not issue:
        return None
    if issue.get('needsInput'):
        return None  # already awaiting input -- don't double-ask
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    pending = state.setdefault('_pendingPlayerAsk', {})
    if pending.get('issueKey') == issue_key:
        return None
    pending.update({
        'issueKey': issue_key,
        'agentId': agent_id,
        'question': (question or '').strip(),
        'context': (context or ''),
        'at': now_ms,
        'directed': False,
    })
    return None


def _director_gate_due_for(state, issue_key, now_ms, gate_ms=8000):
    """True when issue_key has an undirected pending player-ask old enough for
    the director to consider (a short horizon -- the gate is a judgment, not a
    required wait)."""
    pend = (state.get('_pendingPlayerAsk') or {})
    if pend.get('issueKey') != issue_key or pend.get('directed'):
        return False
    return (now_ms - (pend.get('at') or 0)) >= gate_ms


def request_player_input_verdict(state, issue_key, decider=None):
    """Resolve a pending player-ask via the owning team's director (injectable
    decider, mirroring the escalation groom). Verdict `ask_player` -> deliver
    the player inbox message and block the agent; `resolve_internally` -> drop
    the block, the agent resumes. Returns the delivered inbox message id on
    ask_player, 'internal' on resolve_internally, or None if nothing is due."""
    pend = (state.get('_pendingPlayerAsk') or {})
    if pend.get('issueKey') != issue_key:
        return None
    issue = (state.get('issues') or {}).get(issue_key)
    director_id = _issue_director(state, issue_key)
    if not issue or not director_id:
        return None
    agent_id = pend.get('agentId')
    question = pend.get('question') or f"Can the player unblock work on {issue_key}?"
    decider = decider or _player_ask_decider_default
    verdict = decider(state, issue_key, director_id, question, pend.get('context'))
    del state['_pendingPlayerAsk']
    if verdict != 'ask_player':
        _unblock_agent(state, agent_id)
        _log_governance(state, director_id or agent_id, 'player_ask',
                        {'action': 'resolve_internally', 'issue': issue_key})
        # Kept the card unblocked (the agent recovered internally): queue the SM's
        # blocked=false so the single field never falsely stays blocked.
        _file_block_change(state, issue_key, False, agent_id,
                           'unblock_resolved', 'agent recovered without the player')
        return 'internal'
    mid = _deliver_player_ask(state, issue_key, agent_id, question, pend.get('context'))
    if mid:
        # The card is now blocked pending the player's answer: queue the SM's
        # blocked=true so the single field reflects "waiting on the player".
        _file_block_change(state, issue_key, True, agent_id,
                           'stuck_player', question)
    return mid


def _player_ask_decider_default(state, issue_key, director_id, question, context):
    """Default director-gate resolver: Jev judges whether the agent is truly
    blocked in front of the player (ask_player) or can resume with internal
    recovery (resolve_internally). Returns 'ask_player' | 'resolve_internally',
    or 'resolve_internally' on a resolver outage (fail safe toward NOT pestering
    the player)."""
    import serve  # noqa
    try:
        data = serve._call_openrouter_decision_sync(
            serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice', 'instructions': (
                f"{director_id} is reviewing whether agent work on {issue_key} "
                f"really needs the player. The agent asked: '{question}'. "
                f"Context: {context or ''}. Choose ask_player ONLY if the work "
                f"is genuinely blocked with no internal recovery possible."),
                'criteria': {
                    'ask_player': "The work is truly stuck and only the player can unblock it.",
                    'resolve_internally': "The agent can resume with existing information or internal recovery."}}})
        choice = serve._jev_choice(data)[0] if serve._jev_choice(data) else None
        if choice in PLAYER_ASK_VALUES:
            return choice
    except Exception:
        pass
    return 'resolve_internally'


# ---------------------------------------------------------------------------
# Real-email outbox. sim.py stays pure -- it only QUEUES a
# notification into state['emailOutbox']; serve.py drains the outbox each loop
# pass and sends via SMTP (vault-backed Gmail app-password). This keeps network
# + a real external credential out of the pure sim. Deduped per-kind so a burst
# of cards blocking at once sends one combined player nudge, not a flood.
# ---------------------------------------------------------------------------
_EMAIL_DEDUP_MS = 30_000


def _queue_player_email(state, kind, subject, body_text, now_ms=None):
    """Queue a one-way player notification for the serve loop to email out.
    Pure mutation of state['emailOutbox']. Skips a duplicate of the same `kind`
    within _EMAIL_DEDUP_MS of the last one. Returns the outbox entry or None."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    outbox = state.setdefault('emailOutbox', [])
    last = state.get('_lastEmailAt') or {}
    if now_ms - (last.get(kind) or 0) < _EMAIL_DEDUP_MS:
        return None
    last[kind] = now_ms
    state['_lastEmailAt'] = last
    entry = {'kind': kind, 'subject': subject, 'body': body_text,
             'queuedAt': now_ms}
    outbox.append(entry)
    return entry


def _drain_email_outbox_sync(state):
    """Best-effort synchronous send of every queued player notification on
    this state's outbox, on BOTH channels (was email-only --
    the think tank had no proactive push at all before this, only a reactive
    reply when the player texted in first). Clears the outbox regardless so
    a permanent SMTP/Telegram failure doesn't wedge the queue. Calls into
    serve lazily (side-effect), returns a list of (kind, emailOk, telegramOk)
    results. Pure callers never hit the network."""
    outbox = state.get('emailOutbox') or []
    if not outbox:
        return []
    results = []
    try:
        from serve import send_player_email_sync, send_player_telegram_sync
    except Exception as e:  # pragma: no cover - import failure only
        print(f'[email] drain unavailable: {e}')
        state['emailOutbox'] = []
        return [('import', False, False)]
    for entry in list(outbox):
        try:
            email_ok = send_player_email_sync(entry.get('subject'), entry.get('body'))
        except Exception as e:  # pragma: no cover
            email_ok = False
            print(f'[email] send raised: {e}')
        try:
            telegram_ok = send_player_telegram_sync(entry.get('subject'), entry.get('body'))
        except Exception as e:  # pragma: no cover
            telegram_ok = False
            print(f'[telegram] player push raised: {e}')
        results.append((entry.get('kind'), bool(email_ok), bool(telegram_ok)))
    state['emailOutbox'] = []
    return results


def _deliver_player_ask(state, issue_key, agent_id, question, context):
    """Emit the player-inbox message and block the agent until answered. Returns
    the new inbox message id, or None if the message can't be created."""
    now_ms = time.time() * 1000
    messages = state.setdefault('playerInbox', [])
    mid = f"ask-{int(now_ms)}"
    messages.append({
        'id': mid, 'issueKey': issue_key, 'agentId': agent_id,
        'question': (question or '').strip(), 'context': context or '',
        'status': 'awaiting_input', 'createdAt': now_ms,
    })
    issue = state['issues'].get(issue_key)
    if issue:
        issue['needsInput'] = True
    # Real-email: tell the player an agent is waiting on their answer.
    _queue_player_email(
        state, 'agent_ask',
        f"[AI Think Tank] {agent_id} has a question for you on {issue_key}",
        (f"{agent_id} is waiting on your answer for {issue_key} "
         f"({issue.get('title') or issue.get('summary') or 'unknown story'}):\n\n"
         f"{question}\n\n"
         f"Reply in the think tank player-inbox to unblock them."),
        now_ms=now_ms)
    _block_agent_for_input(state, agent_id, issue_key, now_ms)
    return mid


def _block_agent_for_input(state, agent_id, issue_key, now_ms):
    """Freeze an agent off the sim's assignment path (WAIT, not abandon): mark
    `_awaitingPlayer` + which issue, keep them visible/persisted. They resume via
    resolve_player_ask or a terminal `blocked` status."""
    a = (state.get('agents') or {}).get(agent_id)
    if a:
        a['_awaitingPlayer'] = True
    locked = state.setdefault('_playerLockedAgents', {})
    locked[agent_id] = {'issueKey': issue_key, 'at': now_ms}


def _unblock_agent(state, agent_id):
    """Reverse _block_agent_for_input: clear the wait flag so the sim assigns
    the agent work again."""
    a = (state.get('agents') or {}).get(agent_id)
    if a and a.get('_awaitingPlayer'):
        a.pop('_awaitingPlayer', None)
    (state.get('_playerLockedAgents') or {}).pop(agent_id, None)


def resolve_player_ask(state, message_id, answer):
    """The player's reply to an awaiting inbox message. Marks it answered,
    clears the issue's needsInput, unblocks the agent, and stamps the reply onto
    the agent's task so it resumes with the answer as context. Returns the
    updated inbox message, or None if unknown/not-awaiting."""
    messages = state.get('playerInbox') or []
    m = next((x for x in messages if x.get('id') == message_id), None)
    if not m or m.get('status') != 'awaiting_input':
        return None
    m['status'] = 'answered'
    m['answer'] = (answer or '').strip()
    m['answeredAt'] = int(time.time() * 1000)
    issue = (state.get('issues') or {}).get(m.get('issueKey'))
    if issue:
        issue['needsInput'] = False
        agent_id = m.get('agentId')
        a = (state.get('agents') or {}).get(agent_id)
        if a and isinstance(a.get('profile'), dict):
            notes = a['profile'].setdefault('notes', [])
            notes.append(f"Player on {m['issueKey']} ({issue.get('summary', '')}): {m['answer']}")
        _unblock_agent(state, agent_id)
        # Wake-on-mail: the card's agent must act on the answer. _deliver_mail
        # wakes her if she is off-duty (a woken-but-idle mail agent is then
        # routed by _mail_action_step); if she's already busy, this is a no-op
        # mailbox note and she resumes through the normal locked-task path.
        _deliver_mail(state, agent_id, 'player_answer',
                      {'issueKey': m['issueKey'], 'answer': m['answer'],
                       'text': f"Player answered on {m['issueKey']}: {m['answer']}"})
        # The player answered, so the single blocked field now clears: queue the
        # SM's blocked=false (deterministic -- no Jev needed on the player reply).
        _file_block_change(state, m['issueKey'], False, agent_id,
                           'unblock_response', m['answer'])
    return m


def _pending_player_ask_sweep(state, now_ms, decider=None):
    """Cadence sweep (called from _check_schedules): spin any due pending
    player-ask through the director gate. Returns the delivered inbox id /
    'internal' if it advanced, else None if nothing due."""
    pend = state.get('_pendingPlayerAsk') or {}
    issue_key = pend.get('issueKey')
    if not issue_key:
        return None
    if not _director_gate_due_for(state, issue_key, now_ms):
        return None
    return request_player_input_verdict(state, issue_key, decider=decider)


def complete_player_task(state, task_id, now_ms=None):
    """Human-in-the-loop: the player marks an assigned-to-them task done. The
    task (a 'needs_player' card minted by _assign_player_task) flips to 'done',
    which makes the existing dependency machinery release anything queued
    behind it:
      - agent cards queued depends_on_task=<this id> become assignable
        (_dependency_landed is now true) on the next task cycle;
      - any ISSUE committed blocked on this task is auto-unblocked and the
        waiting agent is woken (_auto_clear_dependency_blocks).
    The player-inbox card is marked done, a confirmation email is queued, and
    the action is logged. Returns the completed task dict, or None if the task
    id is unknown / not a player-owned card / already done."""
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    tasks = state.get('tasks') or {}
    task = tasks.get(task_id)
    if not isinstance(task, dict):
        return None
    if task.get('assignedTo') != 'player':
        return None
    if task.get('status') != 'needs_player':
        return None
    task['status'] = 'done'
    task['completedAt'] = now_ms
    # Any issue committed blocked on this task frees up, and its waiting agent
    # is woken -- the exact same release finish_task triggers when an agent's
    # story ships.
    _auto_clear_dependency_blocks(state, task_id, now_ms=now_ms)
    # Mark the player-inbox card done so the UI stops listing it as waiting.
    mid = task.get('playerInboxId')
    if mid:
        for m in (state.get('playerInbox') or []):
            if m.get('id') == mid:
                m['status'] = 'done'
                m['answeredAt'] = now_ms
                break
    title = task.get('title') or task_id
    _queue_player_email(
        state, 'player_task_done',
        f"[AI Think Tank] Your task is complete: {title}",
        (f"You marked '{title}' done. The think tank has picked the work back up: "
         f"anything that was queued behind your task resumes now."),
        now_ms=now_ms)
    from serve import log_action
    log_action('player', 'task_completed',
               {'taskId': task_id, 'title': title}, authorized=False)
    return task


# ---------------------------------------------------------------------------
# SM-committed `blocked` field. issue['blocked'] is a single
# player-facing boolean ONLY the owning team's scrum master flips. Every set and
# clear funnels through a block-change REQUEST the SM ceremony commits:
#   set (requirements-met) : agent claims -> supervisor Jev approves -> SM commits True
#   set (stuck-on-player)  : director ask_player verdict -> SM commits True (card
#                            is blocked pending the player's answer)
#   unset (player answered / agent self-resolved): deterministic -> SM commits False
# The SM's commit is MECHANICAL (judgment is always upstream), so _block_step
# never spends Jev or convenes a meeting -- it validates the request, flips the
# field, and logs. Mirrors the escalation-groom's injectable-decider shape only
# for the requirements-met supervisor vote.
# ---------------------------------------------------------------------------
BLOCK_CHANGE_KINDS = (
    'requirements_met',     # agent says spec satisfied; supervisor must confirm
    'stuck_player',         # director already approved asking the player
    'stuck_on_agent',       # agent blocked on another agent's task (no gate)
    'unblock_response',     # player answered the inbox ask
    'unblock_resolved',     # agent/self resolved without the player
    'unblock_landed',       # the dependency task A was waiting on completed
)


def _file_block_change(state, issue_key, wanted, requester_id, kind, reason,
                       depends_on_task=None, now_ms=None):
    """Queue a request for the scrum master to set issue['blocked'] = `wanted`.
    Dedupes: no second live request for the same issue while one is pending.
    Returns the request id, or None if the issue is unknown or a live request
    already exists. Pure mutation of state['_pendingBlockChanges']. A dependency
    block (`stuck_on_agent`) carries `depends_on_task` so completion auto-clears
    it (see _auto_clear_dependency_blocks)."""
    issue = (state.get('issues') or {}).get(issue_key)
    if not issue:
        return None
    if kind not in BLOCK_CHANGE_KINDS:
        return None
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    for r in (state.get('_pendingBlockChanges') or []):
        if r.get('issueKey') == issue_key and r.get('state') == 'pending':
            return None  # already queued; another gate/path must not double-file
    team_id = issue.get('teamId')
    rid = f"blk-{issue_key}-{int(now_ms)}"
    state.setdefault('_pendingBlockChanges', []).append({
        'id': rid, 'issueKey': issue_key, 'teamId': team_id,
        'wanted': bool(wanted), 'requesterId': requester_id,
        'kind': kind, 'reason': (reason or '').strip(),
        'dependsOnTask': depends_on_task or None,
        'at': now_ms, 'state': 'pending', 'committed': False,
    })
    return rid


def _append_mailbox(agent, entry):
    """Append one entry to an agent's mailbox and trim to MAILBOX_KEEP_COUNT.
    Every mailbox write goes through here so retention is enforced on ALL
    append paths (peer-review requests, rejected-author notes, gate re-picks,
    wake-on-mail) -- not just _deliver_mail, which is what MAILBOX_KEEP_COUNT
    originally forgot (audit)."""
    mailbox = agent.setdefault('mailbox', [])
    mailbox.append(entry)
    if len(mailbox) > MAILBOX_KEEP_COUNT:
        agent['mailbox'] = mailbox[-MAILBOX_KEEP_COUNT:]


def _deliver_mail(state, agent_id, kind, payload=None):
    """File one action-needed mail item into an agent's mailbox. If the
    recipient is OFF-DUTY, wake her (appear_from_outskirts -- lazily loads
    geometry, clears stale busy/task) and set `_mailAwake` so the park-idle pass
    does not re-park her before she has acted on the mail. Pure: mutates
    `state` only. Returns True when dispatched, False for an unknown agent."""
    now_ms = int(time.time() * 1000)
    a = (state.get('agents') or {}).get(agent_id)
    if not isinstance(a, dict):
        return False
    entry = {
        'kind': kind,
        'read': False,
        'ts': now_ms,
        'text': (payload or {}).get('text', ''),
    }
    if payload:
        for k in ('issueKey', 'taskId', 'about'):
            if k in payload:
                entry[k] = payload[k]
    _append_mailbox(a, entry)
    if a.get('offDuty'):
        # Wake the affected agent to act on the mail. Waking from an
        # off-duty rest is the whole point of wake-on-mail.
        appear_from_outskirts(state, agent_id)
        a['_mailAwake'] = {'kind': kind, 'entry': entry, 'at': now_ms}
    return True


def _mail_action_step(state, now, now_ms):
    """Called from _task_cycle (next to _block_step): for each agent holding an
    unacted `_mailAwake` marker, route her to the work the mail references so she
    is assigned and can act, then clear the marker once she is busy/working.
    Without a task, a woken mail agent would be parked by _park_idle_wanderers in
    the same tick (fully_idle) -- the marker is what keeps her awake until acted.
    Returns the number of agents routed. Pure state mutation; no Jev."""
    routed = 0
    agents = state.get('agents') or {}
    for aid, a in list(agents.items()):
        if not isinstance(a, dict) or not a.get('_mailAwake'):
            continue
        marker = a['_mailAwake']
        if a.get('busy') or a.get('task') or a.get('handoff') or a.get('pairWith'):
            # She is now acting (or already working); hand off to normal flow.
            a.pop('_mailAwake', None)
            routed += 1
            continue
        kind = marker.get('kind') or ''
        entry = marker.get('entry') or {}
        payload = entry
        # (Re)queue the referenced work so she is assigned it.
        queued = _queue_mail_work(state, aid, kind, payload, now_ms)
        if queued:
            a.pop('_mailAwake', None)
            routed += 1
    return routed


def _queue_mail_work(state, agent_id, kind, payload, now_ms):
    """Push a workQueue item so `aid` acts on the mail's referenced card/task.
    Menu of kinds: `player_answer` / `dependency_landed` re-arm the card's task
    for its owner; `peer_review_request` / `peer_review_rejected` re-arm the
    target task for a (re)review. Returns True if an item was queued."""
    issue_key = (payload or {}).get('issueKey')
    task_id = (payload or {}).get('taskId') or (payload or {}).get('about')
    if issue_key:
        return _requeue_card_task(state, agent_id, issue_key, now_ms)
    if task_id:
        return _requeue_task_for_agent(state, agent_id, task_id, now_ms)
    return False


def _requeue_card_task(state, agent_id, issue_key, now_ms):
    """Re-arm the workQueue for a card so its owning agent (or the woken agent)
    is assigned it next pass. If the owner is already busy, no-op (she's acting).
    Pure; returns True if an item was queued."""
    issue = (state.get('issues') or {}).get(issue_key)
    if not issue:
        return False
    queue = state.get('workQueue')
    if not isinstance(queue, list):
        return False
    title = issue.get('title') or issue.get('summary') or 'Resume card work'
    queue.append({
        'title': title,
        'room': issue.get('feature') or 'townsquare',
        'instructions': f"Resume work on {issue_key} now that you are unblocked: {issue.get('summary', '')}",
        'goal': issue_key,
        'taskType': issue.get('type'),
        'issueKey': issue_key,
        'assignedTo': agent_id,  # wake-on-mail pin: THIS agent resumes the card
        '_mailResume': True,
        'notBefore': now_ms,
    })
    return True


def _requeue_task_for_agent(state, agent_id, task_id, now_ms):
    """Re-arm an existing task (e.g. a peer review) so `agent_id` is assigned it
    again next pass. Returns True on success."""
    queue = state.get('workQueue')
    if not isinstance(queue, list):
        return False
    queue.append({
        'title': f'Act on mail ({task_id})',
        'room': 'townsquare',
        'instructions': f'Follow up on {task_id} now that you have been notified.',
        'goal': task_id,
        'reviewOf': task_id,
        'assignedTo': agent_id,  # wake-on-mail pin: THIS agent acts on the mail
        '_mailResume': True,
        'notBefore': now_ms,
    })
    return True


def _block_step(state, now, now_ms):
    """The SM-commit pass, called from _task_cycle (mirrors _escalation_step).
    Commits ONE pending block-change whose owning team's scrum master is free +
    on-duty: flips issue['blocked'], stamps blockedAt/blockedBy/unblockedAt, and
    releases any agent parked waiting on it. Mechanical (no LLM). Returns the
    committed request dict, or None if nothing was due."""
    changes = state.get('_pendingBlockChanges') or []
    pending = [c for c in changes if c.get('state') == 'pending']
    if not pending:
        return None
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    # Respect a per-team cadence so one SM isn't flipping cards every tick, and
    # only ONE request per pass (others wait until a later pass).
    for c in pending:
        issue = (state.get('issues') or {}).get(c.get('issueKey'))
        team_id = c.get('teamId')
        t = _team_row(state, team_id)
        if not t:
            continue
        # The commit is MECHANICAL (judgment is always upstream), so ANY effective
        # scrum master may sign it -- for a small team that is the director
        # standing in, the same fallback refinement uses. Without even that, defer.
        scrum_master_id = _refinement_scrum_master_for_team(state, team_id)
        if not scrum_master_id:
            continue  # no effective scrum master yet -- defer
        a = (state.get('agents') or {}).get(scrum_master_id)
        if not a or a.get('busy') or a.get('offDuty'):
            continue  # SM busy/off-duty -- defer to a later pass
        stamps = (state.get('teamBlockAt') or {})
        if now_ms - (stamps.get(team_id) or 0) < BLOCK_CHANGE_CADENCE_MS:
            continue
        if not issue:
            continue
        # Commit. The judgment (supervisor/director approval or a deterministic
        # player-response/self-resolve) already happened upstream -- the SM only
        # makes the authoritative field change.
        c['state'] = 'committed'
        c['committed'] = True
        c['committedAt'] = now_ms
        c['committedBy'] = scrum_master_id
        wanted = c['wanted']
        issue['blocked'] = wanted
        if wanted:
            issue['blockedAt'] = now_ms
            issue['blockedBy'] = scrum_master_id
            issue['unblockedAt'] = None
            # Real-email: tell the player a story/spike they care about is blocked.
            _queue_player_email(
                state, 'card_blocked',
                f"[AI Think Tank] {c['issueKey']} is blocked",
                (f"{c['issueKey']} ({issue.get('title') or issue.get('summary') or 'unknown story'}) "
                 f"is blocked: {issue.get('blockedKind') or 'blocked'}.\n"
                 f"- Agent: {c.get('requesterId') or 'unknown'}\n"
                 f"- Reason: {c.get('reason') or 'no reason given'}\n"
                 f"Take a look in the think tank issue register if this needs your input."),
                now_ms=now_ms)
            # Store the block's provenance so a review detail can explain WHY the
            # card is blocked. `stuck_on_agent` also carries the dependency task id
            # the auto-clear path keys on. If the card is ALREADY blocked for a
            # different reason, keep that reason (a dependency set on an existing
            # requirements-met card shouldn't wipe the card's true reason).
            if not issue.get('blockedKind'):
                issue['blockedKind'] = c.get('kind')
            if c.get('dependsOnTask'):
                issue['dependsOnTask'] = c.get('dependsOnTask')
        else:
            issue['unblockedAt'] = now_ms
            issue['blockedBy'] = None
            issue['blockedKind'] = None
            issue['dependsOnTask'] = None
        # A `stuck_player` commit means the agent is parked waiting on the player;
        # an unblock (player answered or self-resolved) absolutely must free them.
        if not wanted:
            _unblock_agent(state, c.get('requesterId'))
        _log_block_commit(state, c, issue, now_ms)
        state.setdefault('teamBlockAt', {})[team_id] = now_ms
        return c
    return None


def _log_block_commit(state, c, issue, now_ms):
    """Governance log row for an SM block-field commit."""
    try:
        entries = state.setdefault('actionLog', [])
        entries.append({
            'ts': now_ms, 'actor': c.get('committedBy'),
            'action': 'issue_block',
            'data': {
                'issueKey': c.get('issueKey'),
                'blocked': issue.get('blocked'),
                'kind': c.get('kind'),
                'requester': c.get('requesterId'),
                'reason': c.get('reason'),
            },
        })
    except Exception:
        pass


def request_block_dependency(state, issue_key, agent_id, depends_on_task_id,
                             reason='', now_ms=None):
    """Agent A files that it's blocked because another agent's task (`depends_on
    task_id`) hasn't landed. Per the user's decision there's NO Jev gate on this
    path -- naming the dependency task is objective enough -- so this directly
    queues an SM commit of blocked=true, tagged `stuck_on_agent` + the task it's
    waiting on. Returns the request id, or None if the issue is unknown/there's
    already a live request (or it can't reference a task that doesn't exist yet
    is allowed -- the dependency may be in progress)."""
    if _issue_director(state, issue_key) is None:
        return None  # no owning team to route the SM commit through
    issue = (state.get('issues') or {}).get(issue_key)
    if not issue:
        return None
    return _file_block_change(state, issue_key, True, agent_id, 'stuck_on_agent',
                              (reason or f"Blocked waiting on task {depends_on_task_id}").strip(),
                              depends_on_task=depends_on_task_id, now_ms=now_ms)


def _auto_clear_dependency_blocks(state, completed_task_id, now_ms=None):
    """Deterministic auto-clear: when a task lands, any card blocked (or waiting
    on a pending block-change) in a `stuck_on_agent` state whose dependency was
    that task gets an `unblock_landed` request, so the SM commits blocked=false on
    the next _block_step. Agent A resumes with its dependency satisfied. Returns
    the number of unblocks filed."""
    if not completed_task_id:
        return 0
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    clears = 0
    # The completed task's STORY identity, so a woken agent knows what freed them
    # up ("task T landed"), not just an opaque task id.
    landed_title = None
    landed_task = (state.get('tasks') or {}).get(completed_task_id)
    if isinstance(landed_task, dict):
        landed_title = landed_task.get('title') or None
    # (a) Cards already committed blocked on this dependency.
    for issue in (state.get('issues') or {}).values():
        if (issue.get('blocked') and issue.get('blockedKind') == 'stuck_on_agent'
                and issue.get('dependsOnTask') == completed_task_id):
            if _file_block_change(state, issue['key'], False, 'auto',
                                  'unblock_landed',
                                  f"dependency task {completed_task_id} landed",
                                  now_ms=now_ms):
                clears += 1
                # Wake-on-mail: the agent waiting on this dependency must resume
                # her card now that it can move. Recover the requester from the
                # block-change that originally filed the stuck_on_agent.
                waiting_agent = _dependency_requester(state, issue['key'],
                                                      completed_task_id)
                if not waiting_agent:
                    continue
                # LOOP GUARD: don't wake an agent whose own card just freed if she
                # is STILL blocked on ANOTHER story's dependency. Waking her would
                # be churn -- she can't roll that other card forward and it would
                # fire its own wake when its dependency lands. (She'll resume via
                # the normal assignment path once genuinely unblocked everywhere.)
                if _agent_still_blocked(state, waiting_agent, except_task_id=completed_task_id):
                    continue
                what = landed_title or f"task {completed_task_id}"
                _deliver_mail(
                    state, waiting_agent, 'dependency_landed',
                    {'issueKey': issue['key'], 'taskId': completed_task_id,
                     'text': (f"{what} landed; {issue['key']} is unblocked. "
                              f"Resume your work on that card.")})
    # (b) Pending (not-yet-committed) dependency blocks on this task -- drop the
    # queue entry entirely; no need to set the field it never got to set.
    changes = state.get('_pendingBlockChanges') or []
    for c in list(changes):
        if (c.get('state') == 'pending'
                and c.get('kind') == 'stuck_on_agent'
                and c.get('dependsOnTask') == completed_task_id):
            changes.remove(c)
            clears += 1
    return clears


def _pending_block_changes(state, issue_key):
    """Live (uncommitted) block-change requests for one issue, newest first."""
    out = [c for c in (state.get('_pendingBlockChanges') or [])
           if c.get('issueKey') == issue_key and c.get('state') == 'pending']
    return out


def _dependency_requester(state, issue_key, completed_task_id):
    """Recover the agent who originally filed the `stuck_on_agent` block on
    `issue_key` against `completed_task_id`, so wake-on-mail can route the resume
    to the right robot. Looks across committed and pending block-changes; returns
    an agent id or None."""
    for c in (state.get('_pendingBlockChanges') or []):
        if (c.get('issueKey') == issue_key
                and c.get('kind') == 'stuck_on_agent'
                and c.get('dependsOnTask') == completed_task_id
                and c.get('requesterId') and c.get('requesterId') != 'auto'):
            return c['requesterId']
    return None


def _agent_still_blocked(state, agent_id, except_task_id=None):
    """LOOP GUARD for wake-on-mail: True when `agent_id` is still waiting on
    ANOTHER story's unmet dependency (a committed `stuck_on_agent` block on some
    OTHER dependency task). Waking such an agent is pointless churn -- she can't
    roll that other card forward, and it will fire its own wake when ITS
    dependency lands. `except_task_id` skips the dependency that just landed.
    Returns False when the agent is not waiting on any (other) dependency."""
    for issue in (state.get('issues') or {}).values():
        if not (issue.get('blocked')
                and issue.get('blockedKind') == 'stuck_on_agent'):
            continue
        dep = issue.get('dependsOnTask')
        if dep == except_task_id:
            continue
        requester = _dependency_requester(state, issue.get('key'), dep)
        if requester == agent_id:
            return True
    return False


# --- requirements-met supervisor vote ---------------------------------------
# The one Jev-gated judgment in the blocked-field model. Agent work on a story is
# claimed requirements-met; the owning DIRECTOR (supervisor) must confirm the spec
# is genuinely satisfied before a block-change request is filed. Mirrors the
# escalation-groom / refinement decider shape: injectable decider, fail safe to
# NOT blocking (a leftover claim just doesn't get set) on any outage.


def _supervisor_block_vote(state, issue_key, decider=None):
    """Have the owning director judge a pending requirements-met claim on
    `issue_key`. The caller (a cadence sweep) only invokes this once the claim's
    gate window has elapsed. On approval files the block-change request and
    clears the claim; on rejection logs and clears without blocking. Returns
    True (approved), False (rejected / no claim / no director / outage)."""
    claim = (state.get('_pendingBlockClaim') or {})
    if claim.get('issueKey') != issue_key:
        return False
    state.pop('_pendingBlockClaim', None)  # consumed either way
    director_id = _issue_director(state, issue_key)
    issue = (state.get('issues') or {}).get(issue_key)
    if not director_id or not issue:
        return False
    agent_id = claim.get('agentId')
    question = claim.get('reason') or f"Is {issue_key} requirements-met?"
    decider = decider or _supervisor_vote_decider_default
    approved = bool(decider(state, issue_key, director_id, agent_id,
                            question, claim.get('context')))
    if approved:
        _file_block_change(state, issue_key, True, director_id,
                           'requirements_met', question)
    _log_governance(state, director_id, 'issue_block_vote',
                    {'issue': issue_key, 'approved': approved})
    return approved


def _supervisor_vote_decider_default(state, issue_key, director_id, agent_id,
                                     question, context):
    """Default supervisor resolver: Jev judges whether the agent's claim that the
    spec is met is credible. Returns a bool. Fail safe to False (don't block a
    card on an unverified claim) on any resolver outage."""
    import serve  # noqa
    try:
        data = serve._call_openrouter_decision_sync(
            serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice', 'instructions': (
                f"{director_id} is reviewing whether agent {agent_id}'s claim on "
                f"{issue_key} that the player's requirements are met is credible. "
                f"The agent asserted: '{question}'. Context: {context or ''}. "
                f"Approve ONLY if the acceptance criteria genuinely appear met."),
                'criteria': {
                    'approve': "The requirements appear genuinely met or very close.",
                    'reject': "The claim is unverified, partial, or out of scope."}}})
        choice = serve._jev_choice(data)[0] if serve._jev_choice(data) else None
        if choice == 'approve':
            return True
        if choice == 'reject':
            return False
    except Exception:
        pass
    return False


def request_block_claim_met(state, issue_key, agent_id, context=None, now_ms=None):
    """Seed the supervisor vote for a requirements-met claim by an agent. Returns
    True if the claim was recorded (deduped), False if one is already pending or
    the issue/director is unknown."""
    if _issue_director(state, issue_key) is None:
        return False
    issue = (state.get('issues') or {}).get(issue_key)
    if not issue:
        return False
    claim = (state.get('_pendingBlockClaim') or {})
    if claim.get('issueKey') == issue_key:
        return False  # a vote is already in flight
    # A card already blocked needs an unblock, not another met-claim.
    if issue.get('blocked'):
        _file_block_change(state, issue_key, False, agent_id,
                           'unblock_resolved',
                           'card already blocked; agent moving on')
        return False
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    state['_pendingBlockClaim'] = {
        'issueKey': issue_key, 'agentId': agent_id,
        'context': context or '', 'at': now_ms,
    }
    return True


def _supervisor_block_vote_sweep(state, now_ms, decider=None):
    """Cadence sweep (called from _check_schedules, like _pending_player_ask_sweep):
    spin a due requirements-met claim through the supervisor gate. Returns True on
    an approved commit, False on rejection, None if nothing was due."""
    claim = (state.get('_pendingBlockClaim') or {})
    issue_key = claim.get('issueKey')
    if not issue_key:
        return None
    # The claim's own gate: give the agent a short window to be questioned if
    # needed, then let the supervisor decide. (Distinct from the player-ask gate --
    # this reads _pendingBlockClaim, not _pendingPlayerAsk.)
    if (now_ms - (claim.get('at') or 0)) < BLOCK_CLAIM_GATE_MS:
        return None
    return _supervisor_block_vote(state, issue_key, decider=decider)


def _room_backlog_count(state, room):
    """How much of `room`'s work is currently queued / in-flight (the 'thin room'
    signal: too few items left means a finishing agent's follow-up is worth
    filing, because otherwise the room starves)."""
    queued = sum(1 for it in (state.get('workQueue') or []) if it.get('room') == room)
    in_flight = sum(1 for t in (state.get('tasks') or {}).values()
                    if t.get('room') == room and t.get('status') in ('walking', 'working'))
    return queued + in_flight


# Absolute Zero scoping guidance: when the think tank proposes its
# OWN next work (an agent filing a follow-up, and the grooming ceremony deciding
# whether to accept it), the default failure mode is proposing TRIVIAL work --
# cards easy to write and worth nothing. Guidance steers every self-proposed
# card toward MEDIUM difficulty: a real, well-scoped story that materially
# advances the room. Not a hard rule (the decider still owns accept/reject) --
# it just stops the easy/hard extremes from being the default.
ABSOLUTE_ZERO_SCOPING_GUIDANCE = (
    "Scoping guidance: prefer a MEDIUM-difficulty story -- a real, well-scoped "
    "piece of work that materially advances the room. Reject trivial/filler "
    "proposals and over-ambitious un-scoped ones alike."
)


def _free_spike_week(now_ms):
    """The weekly bucket a free-spike allowance is counted against -- an integer
    ISO-ish week number derived from the wall clock, so the allowance rolls by
    itself (no ceremony dependency, unlike weekApprovals which the Social
    resets)."""
    return int((now_ms or int(time.time() * 1000)) // (7 * 24 * 3600 * 1000))


def _file_free_spike(state, agent_id, completed_task, now_ms=None):
    """The spare-time lane: an agent that just finished REAL deliverable work may
    earn one FREE SPIKE this week -- a self-filed, never-groomed, lowest-priority
    investigation of its own choosing. Deterministic intake (no Jev), bounded by
    the per-agent weekly allowance AND the tank-wide weekly cap, and time-boxed.
    A free spike is tagged `moonshot`: protected from rule mining (its failures
    never become operator rules) and already non-gated (spikes don't ship a
    peer-reviewed deliverable). Returns 1 when a free spike was queued, else 0.
    Dedups against an identical pending free spike in the same room."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    room = (completed_task or {}).get('room')
    if not room or room not in VALUED_QUEUE_ROOMS:
        return 0
    a = (state.get('agents') or {}).get(agent_id)
    if not a or a.get('offDuty') or a.get('busy') or a.get('task'):
        return 0
    week = _free_spike_week(now_ms)
    used_map = state.setdefault('freeSpikeUsed', {})
    if not isinstance(used_map, dict):
        used_map = state['freeSpikeUsed'] = {}
    used_week, used = used_map.get(agent_id, (week, 0))
    if used_week != week:
        used = 0
    if used >= FREE_SPIKE_ALLOWANCE_PER_WEEK:
        return 0
    global_map = state.setdefault('freeSpikeGlobal', {})
    if not isinstance(global_map, dict):
        global_map = state['freeSpikeGlobal'] = {}
    # Roll the tank-wide week bucket + prune stale buckets (bounded growth).
    for stale in [k for k in global_map if k < week - 1]:
        del global_map[stale]
    tank_used = global_map.get(week, 0)
    if tank_used >= FREE_SPIKE_GLOBAL_CAP_PER_WEEK:
        return 0
    # One free exploration per room per week: a pending moonshot spike already
    # filed in this room THIS week is the same question -- don't double-file.
    rooms_map = state.setdefault('freeSpikeRooms', {})
    if not isinstance(rooms_map, dict):
        rooms_map = state['freeSpikeRooms'] = {}
    for stale in [k for k in rooms_map if k < week - 1]:
        del rooms_map[stale]
    if room in (rooms_map.get(week) or []):
        return 0
    global_map[week] = tank_used + 1
    used_map[agent_id] = (week, used + 1)
    rooms_map.setdefault(week, []).append(room)
    title = f"Free exploration: question an assumption about {room}"
    instructions = (
        f"{FREE_SPIKE_PREMISE_GUIDANCE} You just completed real work in {room} "
        f"('{completed_task.get('title') or 'previous task'}'). This is your spare "
        f"time: pick ONE question about {room} that the committed work does not cover "
        f"-- a technique, a method, or an assumption your team holds that you want to "
        f"challenge or understand better. Investigate honestly and write up what you "
        f"found."
    )
    queue_spike(state, title, room, FREE_SPIKE_BUDGET_MS, now_ms=now_ms,
                instructions=instructions, goal=f"Free exploration in {room}",
                moonshot=True, notBefore=now_ms + FREE_SPIKE_DEFER_MS)
    _log_governance(state, agent_id, 'free_spike_filed', {
        'room': room, 'week': week, 'agentUsed': used + 1,
        'tankUsed': tank_used + 1, 'after': (completed_task or {}).get('id'),
    })
    return 1


def _maybe_file_followup(state, agent_id, completed_task, now_ms):
    """Deterministic signal generator: an on-duty agent that just COMPLETED a
    task in a delegatable room files a follow-up work-request if that room's
    backlog is thin. Honest, derivable 'I saw a real gap' -- no fake dialogue.
    Guards against churn: never fires for a room with real remaining work, never
    fires off-duty / mid-handoff, and a spike/bug completion doesn't double-file
    (it already IS the incident response)."""
    if not completed_task or completed_task.get('room') not in VALUED_QUEUE_ROOMS:
        return
    a = (state.get('agents') or {}).get(agent_id)
    if not a or a.get('offDuty') or a.get('busy') or a.get('task'):
        return
    if (completed_task.get('taskType') or 'code') in ('spike', 'bug', 'review'):
        # spike/bug already IS the incident response; a review is meta-process
        # work, not a fresh deliverable -- none of them should propose the next
        # story (that's the recursion: 'Follow-up ... after completing Review:
        # Follow-up ...' cards grew indefinitely).
        return
    # A follow-up to a follow-up: a completed card that was ITSELF a follow-up
    # (taskType 'code', title prefixed 'Follow-up:') must not propose yet another
    # follow-up. Otherwise each completed follow-up nests its own title into the
    # next one and the backlog churns self-referential cards that mostly get
    # groomed out. Only a REAL deliverable completion proposes the next story.
    prev_title = (completed_task.get('title') or '').strip()
    if prev_title.lower().startswith('follow-up:'):
        return
    room_label = completed_task['room']
    # Measure the room's backlog BEFORE the spare-time lane files anything -- a
    # just-filed free spike must not count against the room's "thin" check (it
    # is spare time, not committed work).
    backlog = _room_backlog_count(state, room_label)
    # Spare-time lane: a finished REAL deliverable earns the agent's weekly free
    # spike (independent of room backlog -- exploration isn't gated on the room
    # thinning). Bounded by _file_free_spike's own allowance/cap.
    _file_free_spike(state, agent_id, completed_task, now_ms)
    if backlog > WORK_REQUEST_ROOM_THIN:
        return
    title = f"Follow-up: further {room_label} work after completing '{completed_task.get('title') or 'previous task'}'"
    file_work_request(
        state, agent_id, title, room_label,
        reason=f"Completed '{completed_task.get('title') or 'previous task'}' in {room_label} and the remaining backlog there has thinned to {backlog} item(s); this room can absorb another story. {ABSOLUTE_ZERO_SCOPING_GUIDANCE}")


def _refinement_decider_default(instructions, criteria):
    """Default Jev resolver for a single work-request groom: accept (it becomes
    a story) or reject (groomed out). Late-imports serve.py so sim.py stays
    importable/unit-testable offline. Returns a choice id, or None on outage."""
    import serve
    try:
        data = serve._call_openrouter_decision_sync(
            serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice', 'instructions': instructions,
                        'criteria': {c['id']: c['description'] for c in criteria}}})
        choice, _, _ = serve._jev_choice(data)
        return choice if any(c['id'] == choice for c in criteria) else None
    except Exception:
        return None


# Injectable for tests (mirrors _governance_decider / _social_decider); the live
# loop uses the Jev-backed default. The signal generator (_maybe_file_followup)
# is deliberately NOT decider-gated -- filing is deterministic intake, grooming
# is the decision.
_refinement_decider = _refinement_decider_default


def _refinement_team(state, req):
    """Which team owns `req`. Requests filed from a JIRA issue carry an explicit
    `teamId` (the filer may be the player, who reports to nobody) -- honor it
    first so the owning team's scrum master grooms the card. Otherwise fall
    back to the filer's reporting tree (a work-request an agent filed). Returns
    the team dict, or None if the filer isn't a member of any team."""
    if req.get('teamId'):
        t = _team_row(state, req.get('teamId'))
        if t:
            return t
    filed_by = req.get('filedBy')
    for t in (state.get('teams') or []):
        if filed_by in _sim_direct_reports(state, t.get('directorId')):
            return t
    return None


def _refinement_scrum_master_for_team(state, team_id):
    """The effective scrum master of `team_id`: the designated scrum master, or
    -- for a team below SCRUM_MASTER_MIN_TEAM_SIZE workers -- the team director
    standing in (a small team doesn't need a dedicated facilitator yet, by design). Returns None only for a big team that genuinely
    lacks a designated scrum master. Per-team ceremonies: only a team's OWN
    scrum master grooms that team's requests, never a cross-team stand-in
    (matches a real org where each team refines its own backlog)."""
    for t in (state.get('teams') or []):
        if t.get('id') == team_id or t.get('directorId') == team_id:
            if t.get('scrumMasterId'):
                return t['scrumMasterId']
            director_id = t.get('directorId')
            if director_id and _team_member_count(state, director_id) < SCRUM_MASTER_MIN_TEAM_SIZE:
                return director_id
            return None
    return None


def _ceremony_facilitator(state, team_id):
    """A FREE, ON-DUTY facilitator for `team_id`'s ceremony -- the one change
    that makes scrum-master absence never block a ceremony. Mirrors the retro's
    fallback ladder (_retro_scrum_master): the effective scrum master when
    free+on-duty (a small team's own director standing in, per
    _refinement_scrum_master_for_team), else the team's OWN director, else a
    non-busy director borrowed from ANOTHER team (the OWN director is preferred
    over a borrowed one -- a team's grooming should be run by someone it works
    with). Returns None only when no facilitator can be found at all (the
    ceremony then waits for a later pass)."""
    teams = {t.get('id'): t for t in (state.get('teams') or [])}
    agents = state.get('agents') or {}

    def _free(aid):
        a = agents.get(aid)
        return bool(a) and not a.get('busy') and not a.get('offDuty')

    sm = _refinement_scrum_master_for_team(state, team_id)
    if sm and _free(sm):
        return sm
    did = (teams.get(team_id) or {}).get('directorId')
    if did and _free(did):
        return did
    # Borrow a free director from another team rather than leaving the team's
    # grooming (or breakdown) stranded on a busy/off-duty own facilitator.
    for t in (state.get('teams') or []):
        other = t.get('directorId')
        if not other or other == did:
            continue
        if _free(other):
            return other
    return None


def _refinement_attendees(state, req_ids, scrum_master_id):
    """The ceremony's attendees: the scrum master + every AGENT who FILED a
    pending request this round (req_ids are work-request ids; the filers are
    resolved through the backlogRequests records). Non-agent filers (e.g. the
    player filing a JIRA issue) cannot attend a room ceremony and must not
    stall it -- they are dropped from the attendee list, and the scrum master
    still grooms their card. ONLY the scrum master must be free to convene: a
    BUSY filer's card is still groomed this round but she does not attend (her
    card's acceptance never needed her in the room), so one busy worker can no
    longer stall the whole team's grooming. An off-duty attendee is parked and
    free, and _start_refinement wakes her (snapshot + restore, exactly like the
    Social's off-duty Hangout attendees)."""
    agents = state.get('agents') or {}
    sm = agents.get(scrum_master_id)
    if not sm or sm.get('busy'):
        return None  # the facilitator must be free to run the meeting
    filer_ids = []
    for rid in req_ids:
        r = next((x for x in (state.get('backlogRequests') or []) if x.get('id') == rid), None)
        if r and r.get('filedBy') and r['filedBy'] not in filer_ids \
                and r['filedBy'] in agents:
            filer_ids.append(r['filedBy'])
    ids = [scrum_master_id] + [f for f in filer_ids if f != scrum_master_id
                               and not (agents.get(f) or {}).get('busy')]
    return ids


def _start_refinement(state, team_id, req_ids, scrum_master_id, now_ms, pos_offset=0):
    """Convene the backlog-refinement ceremony at the Command Center: snapshot
    each attendee's prior state into `pending['people']`, mark them busy at a
    spread, and schedule the resolve REFINEMENT_MEET_MS later. Returns True if
    convened (attendees healthy), False to defer and retry next pass.

    `pos_offset` spreads simultaneous per-team ceremonies apart at the Command
    Center (each team is its own ceremony slot; the shared room must not fully
    overlap its attendees' positions -- see _refinement_step)."""
    ids = _refinement_attendees(state, req_ids, scrum_master_id)
    if not ids:
        return False
    agents = state.get('agents') or {}
    snap = {}
    base_x = 315 + (pos_offset * 460)
    for i, aid in enumerate(ids):
        a = agents.get(aid)
        snap[aid] = {k: a.get(k) for k in ('offDuty', 'visible', 'x', 'y', 'dir',
                                           'task', 'busy', 'inRoom', 'pairWith', 'handoff', 'workUntil')}
        a['busy'] = True
        a['visible'] = True
        a['offDuty'] = False
        a['task'] = None
        a['inRoom'] = 'commandcenter'
        a['dir'] = 'south'
        a['roomX'] = base_x + (i * 85)
        a['roomY'] = 155
    state.setdefault('pendingRefinements', {})[team_id] = {
        'at': now_ms + REFINEMENT_MEET_MS, 'embarked': True,
        'scrumMasterId': scrum_master_id, 'reqIds': req_ids, 'people': snap,
        'teamId': team_id,
    }
    _log_governance(state, scrum_master_id, 'refinement',
                    {'action': 'meeting_start', 'requests': req_ids})
    return True


def _restore_refinement_agent(state, aid, snap, now_ms, duration_ms):
    """Return an attendee to their pre-ceremony state. A mid-task worker resumes
    the same task with its work budget extended by the ceremony length; an
    off-duty agent returns to off-duty (vanished); an idle on-duty one returns to
    idle. Mirrors _restore_social_agent exactly."""
    agents = state.get('agents') or {}
    a = agents.get(aid)
    if not a:
        return
    a['dir'] = snap.get('dir')
    a['x'] = snap.get('x'); a['y'] = snap.get('y')
    if snap.get('offDuty'):
        a['offDuty'] = True; a['visible'] = False
        a['task'] = None; a['busy'] = False; a['inRoom'] = None
        return
    if snap.get('task'):
        t = (state.get('tasks') or {}).get(snap['task'])
        a['task'] = snap['task']; a['busy'] = True; a['inRoom'] = snap.get('inRoom')
        a['visible'] = snap.get('visible')
        if t and t.get('status') in ('walking', 'working'):
            t['workUntil'] = (t.get('workUntil') or 0) + (duration_ms // 1000)
        return
    a['task'] = None; a['busy'] = False; a['inRoom'] = None
    a['visible'] = snap.get('visible'); a['offDuty'] = snap.get('offDuty')


def _reassign_rolled_over_cards(state, team_id):
    """Refinement re-plan (sprint rollover): a card that rolled over from one of
    THIS team's closed sprints (still queued, tagged with a closed sprint whose
    teamIds include this team) is re-assigned to the team's least-loaded free
    member -- the carried-over work gets a fresh, explicit owner for the next
    sprint instead of silently resuming with whoever the global round-robin last
    pointed it at. The pin is soft (see _assign_due_item): honored while the
    target is free, else the generic pool picks up. Deterministic, no model
    call. Returns the count of cards re-planned."""
    team_director = None
    for t in (state.get('teams') or []):
        if t.get('id') == team_id or t.get('directorId') == team_id:
            team_director = t.get('directorId')
            break
    members = _sim_direct_reports(state, team_director) if team_director else []
    agents = state.get('agents') or {}
    members = [m for m in members if agents.get(m)]
    if not members:
        return 0
    closed_ids = {s.get('id') for s in (state.get('sprints') or {}).values()
                  if s.get('status') == 'closed' and team_id in (s.get('teamIds') or [])}
    if not closed_ids:
        return 0
    tasks = state.get('tasks') or {}
    picked = []
    for it in (state.get('workQueue') or []):
        if it.get('sprintId') not in closed_ids:
            continue
        if any(t.get('status') == 'done' and t.get('title') == it.get('title')
               and t.get('room') == it.get('room')
               for t in tasks.values()):
            continue
        picked.append(it)
    if not picked:
        return 0
    free = [m for m in members
            if not (agents[m].get('busy') or agents[m].get('offDuty'))]
    if not free:
        return 0
    # Least-loaded first: fewest open tasks, tie-break by roster order.
    def _load(m):
        return sum(1 for t in tasks.values()
                   if t.get('assignedTo') == m
                   and t.get('status') in ('walking', 'working'))
    free.sort(key=lambda m: (_load(m), members.index(m)))
    target = free[0]
    for it in picked:
        it['_reassignedTo'] = target
    return len(picked)


def _resolve_refinement(state, pending, now_ms, decider=None):
    """When the ceremony window elapses, the scrum master grooms every pending
    request: the injectable refinement decider picks accept/reject per request,
    accepted ones become real `queue_work` stories (source:'refinement' +
    groomedBy), and everyone returns to their prior state. Marks the requests
    accepted/rejected, resets the cadence stamp, logs the digest, pops the
    pending record. Idempotent if a request vanished."""
    decider = decider or _refinement_decider
    agents = state.get('agents') or {}
    scrum_master_id = pending.get('scrumMasterId')
    scrum_master = (agents.get(scrum_master_id) or {})
    sm_def = next((d for d in (state.get('agentRoster') or []) if d.get('id') == scrum_master_id), {})
    sm_name = sm_def.get('name') or scrum_master.get('name') or scrum_master_id
    accepted = []
    for req in (state.get('backlogRequests') or []):
        if req.get('id') not in pending.get('reqIds', []):
            continue
        if req.get('status') != 'pending':
            continue
        filed_by = req.get('filedBy')
        author = (agents.get(filed_by) or {})
        instructions = (f"{sm_name} is the scrum master running backlog refinement for {filed_by}'s "
                        f"({author.get('name') or filed_by} / {author.get('role') or 'worker'}) work request "
                        f"in the {req.get('room')} room. Requested story: '{req.get('title')}'. Reason: "
                        f"{req.get('reason')}.")
        ctx = _refinement_context_for_room(state, req.get('room'))
        if ctx:
            instructions += f" Roadmap context for this room: {ctx}."
        instructions += (" Turn it into a real story (accept) or groom it back to the "
                         "requester because it's not ready / not needed / doesn't fit the roadmap (reject). "
                         + ABSOLUTE_ZERO_SCOPING_GUIDANCE)
        criteria = [
            {'id': 'accept', 'description': f"Create '{req.get('title')}' as a real story in {req.get('room')} -- it's a genuine, well-scoped, MEDIUM-difficulty gap worth a card."},
            {'id': 'accept_generous', 'description': f"Create '{req.get('title')}' as a real story in {req.get('room')} -- it's a genuine, well-scoped, HIGH-VALUE gap worth a card AND a generous spend budget (a bigger investigation or deliverable than the standard card)."},
            {'id': 'reject', 'description': "Groom this request out -- it's a duplicate, low-value, trivial/filler, over-ambitious/un-scoped, out-of-scope, or the roadmap already covers it."},
        ]
        choice = decider(instructions, criteria)
        if choice is None:
            # Outage fallback: accept only a request that names a real
            # delegatable-room gap (same determinism as the governance fallback --
            # better to ship one well-scoped card than to silently drop a filed gap).
            choice = 'accept' if req.get('room') in VALUED_QUEUE_ROOMS else 'reject'
        # Item 4: the grooming choice also picks the spend band. 'accept_generous'
        # (a HIGH-VALUE story) doubles the type-default ceiling; 'accept' is the
        # standard band; the rejected branch never touches it. An explicit
        # req['budgetUsd'] (player-named, from file_issue) always wins over the
        # band -- the band only scales the TYPE default, never an explicit number.
        band = 'generous' if choice == 'accept_generous' else 'standard'
        req['budgetBand'] = band
        req['budgetUsd'] = budget_usd_for_task(
            req.get('taskType') or 'code',
            req.get('budgetUsd'),
            band)
        if choice in ('accept', 'accept_generous'):
            req['status'] = 'accepted'
            # queue_work whitelists fields, so the provenance (filer/groomer/
            # room/title) lives on the durable `backlogRequests` record + the
            # digest -- not on the queue item. teamId is the one exception
            # _assign_due_item needs it to PREFER this team at
            # assignment time (see queue_work's own comment on the field) --
            # req['teamId'] is already known here (the grooming ceremony
            # itself is per-team), it just never survived onto the real task.
            # A player-filed JIRA card (req carries the normalized userStory +
            # acceptanceCriteria from file_issue) is handed to the WORKER as the
            # full contract: the story and criteria are embedded in the task's
            # instructions (the coding executor builds its backlog line from
            # them), and forwarded as structured fields through queue_work /
            # _assign_due_item / assign_task so they survive onto the task.
            story = req.get('userStory')
            criteria = req.get('acceptanceCriteria')
            instructions = (f"Filed by {filed_by} during backlog refinement. {req.get('reason')}")
            if story:
                instructions += f"\n\nUser story: {story}"
            if criteria:
                instructions += f"\n\nAcceptance criteria:\n{criteria}"
            # Dependency cascade provenance: a card whose ISSUE was filed blocked
            # on another task (issue['dependsOnTask']) carries that dependency
            # onto its real task, so re-opening the dependency ripples a
            # re-review to this story once it has shipped (see _cascade_rereview).
            depends_on = None
            linked_issue = (state.get('issues') or {}).get(req.get('issueKey'))
            if linked_issue and linked_issue.get('dependsOnTask'):
                depends_on = linked_issue['dependsOnTask']
            queue_work(state, [{
                'title': req['title'], 'room': req['room'], 'goal': req.get('title'),
                'instructions': instructions,
                'teamId': req.get('teamId'),
                'userStory': story,
                'acceptanceCriteria': criteria,
                'dependsOn': depends_on,
                'issueKey': req.get('issueKey') or None,
                # Human-in-the-loop: a card the filer marked for the PLAYER
                # (assignedTo 'player') stays the player's -- _assign_due_item
                # hands it to the player instead of an agent. Any other value is
                # dropped (agents are picked, not named, on the refinement path).
                'assignedTo': req.get('assignedTo') if req.get('assignedTo') == 'player' else None,
                # The card's attention lane survives grooming onto the queue
                # (queue_work normalizes), so a build/reading/parking-lot card
                # keeps its lane through refinement.
                'lane': req.get('lane'),
                # Item 4: the groomer-judged spend ceiling + band ride the queue
                # item (whitelisted) so the ASSIGNED task carries them.
                'budgetUsd': req.get('budgetUsd'),
                'budgetBand': req.get('budgetBand'),
                # Player-authored provenance: a backlog request the PLAYER filed
                # (filedBy 'player' -- the player chat lane, or a player-filed
                # JIRA issue) is player-vetted; every agent-filed request is
                # not. Threaded so the JEV gate's work-context bypass trusts
                # only these tasks.
                'playerAuthored': bool(filed_by == 'player'),
            }])
            accepted.append(req)
        else:
            req['status'] = 'rejected'
        details = {'request': req['id'], 'choice': choice, 'filedBy': filed_by,
                   'room': req['room'], 'title': req['title'][:120]}
        if choice == 'reject':
            # Absolute Zero rejection signal: a self-proposed work-request was
            # groomed OUT. Durable, queryable (the health check counts these) --
            # a rising reject rate is the think tank's own early warning that its
            # proposals are trending trivial/ill-scoped.
            details['selfProposedRejected'] = True
        _log_governance(state, scrum_master_id, 'refinement_carryaway', details)
    # Sprint rollover re-plan: cards that rolled over from this team's closed
    # sprint are re-assigned to a fresh, least-loaded free member so the carried
    # work has an explicit owner for the next sprint.
    team_id = pending.get('teamId')
    if team_id:
        replanned = _reassign_rolled_over_cards(state, team_id)
        if replanned:
            _log_governance(state, scrum_master_id, 'refinement_carryaway',
                            {'action': 'rollover_reassign', 'team': team_id,
                             'reassigned': replanned})
    # Restore attendees; reset the cadence stamp event-to-event. Like the Social,
    # the work budget given back is the full ceremony length, so the meeting
    # burned none of a mid-task attendee's work time.
    for aid, snap in (pending.get('people') or {}).items():
        _restore_refinement_agent(state, aid, snap, now_ms, REFINEMENT_MEET_MS)
    if team_id:
        state.setdefault('teamRefinementAt', {})[team_id] = now_ms
    else:
        state['lastBacklogRefinementAt'] = now_ms
    from serve import log_refinement_digest
    try:
        log_refinement_digest(state, agents, {'scrumMasterId': scrum_master_id, 'accepted': accepted})
    except Exception:
        pass
    state.setdefault('pendingRefinements', {}).pop(team_id, None) if team_id else state.pop('_pendingRefinement', None)


def _refinement_cadence_due_for(state, team_id, now_ms, legacy=None):
    """Is team `team_id`'s weekly refinement window due? Reads the per-team stamp
    map (or the legacy flat stamp while converting a cold state). Treats a
    far-future stale TEST sentinel as unset."""
    stamps = state.get('teamRefinementAt') or {}
    last = stamps.get(team_id)
    if last is None:
        last = legacy if legacy is not None else 0
    if last - now_ms > 10 * 365 * 24 * 3600 * 1000:
        last = 0
    return now_ms - last >= REFINEMENT_CADENCE_MS


def kick_refinement_now(state, team_id, now_ms=None):
    """Make a team's backlog refinement due on the VERY next pass instead of
    waiting for the weekly cadence -- large requests kick refinement
    the moment they arrive). Safe against the kick + weekly-cadence double-groom
    trap: it is a no-op if the team already has an in-flight ceremony (that
    ceremony will groom the new request) or its cadence is already due (the next
    pass will pick it up anyway). Returns True if the kick armed, False if it
    was already covered."""
    if not team_id:
        return False
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    # In-flight ceremony already covers this team -- don't stack a second one.
    if team_id in (state.get('pendingRefinements') or {}):
        return False
    if _refinement_cadence_due_for(state, team_id, now_ms):
        return False  # already due -- next pass picks it up
    state.setdefault('teamRefinementAt', {})[team_id] = 0
    return True


def _refinement_step(state, now, now_ms, decider=None):
    """One backlog-refinement pass, called from _task_cycle ungated (each team's
    ceremony is the point even on a quiet think tank). EVENT-DRIVEN: there is no
    weekly cadence gate -- a ceremony convenes as soon as a team has pending
    requests, a free+on-duty facilitator (see _ceremony_facilitator), and no
    active sprint. Per-team ceremonies run CONCURRENTLY: each team has its OWN
    ceremony slot (`pendingRefinements[team_id]`), so one team no longer blocks
    another at the shared Command Center. A pass (a) advances every in-flight
    ceremony (embark on one pass, RELEASE on the next -- no required fixed
    hold; the facilitator is RE-RESOLVED at embark so a stored id that has gone
    busy never blocks), and (b) convenes a new ceremony for every team with
    pending requests AND a free facilitator. Never convenes an empty meeting;
    defers a team in an active sprint (it refines at close)."""
    pending_map = state.setdefault('pendingRefinements', {})
    # (a) Advance every in-flight ceremony. Embark on one pass, then RELEASE on
    # the immediately-following pass -- the scrum master groomed everything
    # synchronously in the resolve, so there's no clock to wait out. The
    # facilitator is re-resolved HERE so a stored scrum master who has gone
    # busy/off-duty is swapped for the team's own/borrowed director instead of
    # stranding the embarked meeting.
    for team_id in list(pending_map.keys()):
        pending = pending_map[team_id]
        if not pending.get('embarked'):
            facilitator = _ceremony_facilitator(state, team_id)
            if not facilitator:
                continue  # no free+on-duty facilitator right now -- defer a pass
            _start_refinement(state, team_id, pending.get('reqIds', []),
                              facilitator, now_ms)
        else:
            _resolve_refinement(state, pending, now_ms, decider=decider)
    # (b) Convene a ceremony for every team that has groomable requests and a
    # free facilitator. Each team gets its own slot, so multiple teams can
    # refine in the same pass (spread apart in the room).
    slot = 0
    for team in (state.get('teams') or []):
        team_id = team.get('id') or team.get('directorId')
        if team_id in pending_map:
            continue  # this team already has an in-flight ceremony
        facilitator = _ceremony_facilitator(state, team_id)
        if not facilitator:
            continue  # this team has no free+on-duty facilitator right now
        if _team_in_active_sprint(state, team_id):
            # WS-14: a team in an active sprint refines at CLOSE, never
            # mid-sprint -- the sprint is the committed work.
            continue
        # This team's OWN pending requests -- a ceremony must never groom a
        # request that isn't filed by one of THIS team's members (per-team
        # isolation: dev's scrum master refines only dev's cards).
        req_ids = []
        for r in _pending_work_requests(state):
            if r.get('breakdown'):
                # Large-request breakdowns are groomed by the BREAKDOWN ceremony
                # (the receiving team's scrum master + workers plan the stories),
                # never by this refinement groom -- two ceremonies, two kinds.
                continue
            t = _refinement_team(state, r)
            if t and (t.get('id') == team_id or t.get('directorId') == team_id):
                req_ids.append(r['id'])
        if not req_ids:
            continue  # nothing to groom -- don't convene an empty meeting
        req_ids = req_ids[:REFINEMENT_MAX_REQUESTS]
        state.setdefault('teamRefinementAt', {})[team_id] = now_ms
        pending_map[team_id] = {
            'at': now_ms + REFINEMENT_MEET_MS, 'embarked': False,
            'scrumMasterId': facilitator, 'reqIds': req_ids,
            'teamId': team_id, 'people': {},
        }
        slot += 1  # only to spread simultaneous ceremonies apart in the room


# ---------------------------------------------------------------------------
# Sprint staffing: the breakdown ceremony. A staffable large ask is filed as a
# pending breakdown request (origin:'large_request', breakdown:True) by the free
# authority, tagged with the receiving team. That team's OWN breakdown ceremony
# -- scrum master + the team's workers -- meets at the Command Center and cards
# the ask into concrete stories/spikes, which are queued as REAL work tagged
# with the team + the goal. No sprint record is created up front and no subtasks
# are predicted synchronously: the team plans its own work, like a real org.
# Same convene-then-resolve ceremony shape as refinement; the decider is
# injectable like every other Jev decision, and the ask is never dropped -- an
# outage degrades to a single card titled the goal.
# ---------------------------------------------------------------------------


def _pending_breakdown_requests(state, team_id):
    """Pending large-request breakdowns targeted at `team_id` (breakdown:True
    records only) -- the breakdown ceremony's intake. Distinct from the
    refinement ceremony's `_pending_work_requests`, which must never groom
    these: the two ceremonies own different request kinds."""
    return [r for r in (state.get('backlogRequests') or [])
            if r.get('status') == 'pending' and r.get('breakdown')
            and r.get('teamId') == team_id]


def _breakdown_attendees(state, req_ids, scrum_master_id, team_id):
    """The breakdown ceremony's attendees: the team's scrum master + its
    non-director workers (the people who will WORK the stories -- a breakdown is
    a team planning session). The director attends only when she IS the
    effective scrum master (small teams where the director stands in). A busy
    attendee defers the whole ceremony (never pull an agent out of a live
    collaboration); an off-duty one is parked and free, and _start_breakdown
    wakes her (snapshot + restore, exactly like refinement). Returns the
    attendee ids, or None if any attendee is busy."""
    agents = state.get('agents') or {}
    ids = [scrum_master_id]
    t = _team_row(state, team_id) or {}
    director_id = t.get('directorId')
    for aid in _sim_direct_reports(state, director_id):
        if aid not in ids and aid != director_id:
            ids.append(aid)
    for aid in ids:
        a = agents.get(aid)
        if not a or a.get('busy'):
            return None
    return ids


def _start_breakdown(state, team_id, req_ids, scrum_master_id, now_ms, pos_offset=0):
    """Convene the breakdown ceremony at the Command Center: snapshot each
    attendee's prior state into `pending['people']`, mark them busy at a spread,
    and schedule the resolve BREAKDOWN_MEET_MS later. Returns True if convened
    (attendees healthy), False to defer and retry next pass."""
    ids = _breakdown_attendees(state, req_ids, scrum_master_id, team_id)
    if not ids:
        return False
    agents = state.get('agents') or {}
    snap = {}
    base_x = 315 + (pos_offset * 460)
    for i, aid in enumerate(ids):
        a = agents.get(aid)
        snap[aid] = {k: a.get(k) for k in ('offDuty', 'visible', 'x', 'y', 'dir',
                                           'task', 'busy', 'inRoom', 'pairWith', 'handoff', 'workUntil')}
        a['busy'] = True
        a['visible'] = True
        a['offDuty'] = False
        a['task'] = None
        a['inRoom'] = 'commandcenter'
        a['dir'] = 'south'
        a['roomX'] = base_x + (i * 85)
        a['roomY'] = 155
    state.setdefault('pendingBreakdowns', {})[team_id] = {
        'at': now_ms + BREAKDOWN_MEET_MS, 'embarked': True,
        'scrumMasterId': scrum_master_id, 'reqIds': req_ids,
        'teamId': team_id, 'people': snap,
    }
    _log_governance(state, scrum_master_id, 'breakdown',
                    {'action': 'meeting_start', 'requests': req_ids})
    return True


def _breakdown_decider_default(state, instructions, goal):
    """Default resolver for the breakdown ceremony: a high-tier (JEV-gated)
    chat call returns a JSON list of stories/spikes for the ask, accrued to its
    own spend bucket. Returns the parsed dict, or None on a model outage (the
    resolve then queues a single card titled the goal -- the ask is never
    dropped)."""
    try:
        import serve
    except Exception:
        return None
    try:
        model = serve._resolve_model_tier(
            f'Breaking a large request into worker-sized stories: {goal[:200]}',
            allow_high=True)
        if not model:
            return None
        data = serve._call_openrouter_sync(
            model,
            [{'role': 'system', 'content': instructions},
             {'role': 'user', 'content': f"Break down this large request: {goal}"}],
            max_tokens=serve._big_task_max_tokens)
        cost = (data.get('usage') or {}).get('cost', 0.0)
        if isinstance(cost, (int, float)) and cost:
            serve._accrue_spend('__breakdowns__', cost)
        reply = ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        start = reply.find('{')
        end = reply.rfind('}')
        if start != -1 and end > start:
            reply = reply[start:end + 1]
        parsed = json.loads(reply)
        if isinstance(parsed, dict) and (parsed.get('items') or []):
            return parsed
    except Exception:
        pass
    return None


# Injectable for tests (mirrors _refinement_decider / _retro_decider); the live
# loop uses the high-tier default. The breakdown NEVER blocks the staffing reply
# -- the ask is filed pending and carded on a later pass, so an outage costs
# nothing but a single card titled the goal.
_breakdown_decider = _breakdown_decider_default


def _resolve_breakdown(state, pending, now_ms, decider=None):
    """When the breakdown window elapses, the scrum master cards each pending
    large-request into concrete stories/spikes via the injectable decider; the
    pieces are queued as REAL work tagged with the receiving team + the goal
    (room-less -- the agent who picks the work up resolves the room at
    assignment, exactly like a shared-backlog card). On an outage a single
    pressoffice card titled the goal is queued so the ask is never silently
    dropped. Restores attendees, records the carryaway, pops the pending
    record. Idempotent if a request vanished."""
    decider = decider or _breakdown_decider
    agents = state.get('agents') or {}
    scrum_master_id = pending.get('scrumMasterId')
    sm_def = next((d for d in (state.get('agentRoster') or []) if d.get('id') == scrum_master_id), {})
    sm_name = sm_def.get('name') or (agents.get(scrum_master_id) or {}).get('name') or scrum_master_id
    team_id = pending.get('teamId')
    for req in (state.get('backlogRequests') or []):
        if req.get('id') not in pending.get('reqIds', []):
            continue
        if req.get('status') != 'pending':
            continue
        goal = req.get('goal') or req.get('title') or 'untitled large request'
        author = agents.get(req.get('filedBy')) or {}
        instructions = (
            f"{sm_name} is the scrum master of the receiving team breaking down a large request "
            f"filed by {author.get('name') or req.get('filedBy')}: '{goal[:200]}'. "
            "Break it into as many concrete stories (or spikes for investigation-first work) as the work actually "
            "requires -- at least one, never a fixed count: a small ask may be a single card, a sprawling one may need many. "
            "Do NOT assign a room to a card -- the buildings are shared and the agent who picks the work up figures out "
            "where it needs to happen. Keep every title to ONE short sentence. For each card set \"type\" to "
            "\"story\" (deliverable work) or \"spike\" (an investigation with no committed deliverable), "
            "a one-line \"acceptanceCriteria\" when the story has a clear test of done, and a \"sizeEstimate\" of S/M/L. "
            "Respond with ONLY valid JSON, no other text, no markdown fences, in exactly this shape: "
            '{"items":[{"title":"short title","type":"story|spike","acceptanceCriteria":"one line or omitted","sizeEstimate":"S|M|L"}]}'
        )
        data = decider(state, instructions, goal)
        items = []
        if isinstance(data, dict):
            items = [s for s in (data.get('items') or [])
                     if isinstance(s, dict) and (s.get('title') or '').strip()]
        if not items:
            # Outage / unusable plan: never drop the ask -- queue a single card
            # titled the goal so it stays in the pipeline for a worker to pick up.
            items = [{'title': goal[:120], 'type': 'story'}]
        for s in items:
            title = (s.get('title') or '').strip()[:120]
            task_type = 'spike' if (s.get('type') or 'story').strip().lower() == 'spike' else 'code'
            ac = (s.get('acceptanceCriteria') or '').strip()
            instructions_text = (f"Filed by {req.get('filedBy')} during a large-request breakdown by "
                                 f"{sm_name}. Requested goal: {goal[:200]}")
            if ac:
                instructions_text += f"\n\nAcceptance criteria:\n{ac}"
            queue_work(state, [{
                'title': title, 'goal': goal[:200], 'instructions': instructions_text,
                'teamId': req.get('teamId'), 'taskType': task_type,
                # The decider was told to size each story S/M/L (see the
                # breakdown prompt); carry it so the queued item + task keep the
                # estimate and same-priority work is ordered largest-first.
                'sizeEstimate': s.get('sizeEstimate'),
            }])
        req['status'] = 'accepted'
        req['brokenDown'] = True
        req['stories'] = items
        _log_governance(state, scrum_master_id, 'breakdown_carryaway',
                        {'request': req['id'], 'goal': goal[:120], 'stories': len(items)})
    # Restore attendees; like refinement, the meeting burned none of a mid-task
    # attendee's work time (the full ceremony length is given back).
    for aid, snap in (pending.get('people') or {}).items():
        _restore_refinement_agent(state, aid, snap, now_ms, BREAKDOWN_MEET_MS)
    state.setdefault('pendingBreakdowns', {}).pop(team_id, None) if team_id else state.pop('_pendingBreakdown', None)


def _breakdown_step(state, now, now_ms, decider=None):
    """One large-request breakdown pass, called from _task_cycle ungated (a
    staffed large ask is the point even on a quiet think tank). A pass
    (a) advances every in-flight breakdown ceremony (embark on one pass,
    RELEASE on the next -- no required fixed hold; the facilitator is
    RE-RESOLVED at embark so a stored id that has gone busy never blocks), and
    (b) convenes a breakdown for every team that has pending large-request
    breakdowns AND a free+on-duty facilitator (see _ceremony_facilitator) +
    healthy attendees. Never convenes an empty meeting; a team in an active
    sprint is deferred (the sprint is the committed work, and the pending
    breakdown request is carded once the team is free)."""
    pending_map = state.setdefault('pendingBreakdowns', {})
    for team_id in list(pending_map.keys()):
        pending = pending_map[team_id]
        if not pending.get('embarked'):
            facilitator = _ceremony_facilitator(state, team_id)
            if not facilitator:
                continue  # no free+on-duty facilitator right now -- defer a pass
            _start_breakdown(state, team_id, pending.get('reqIds', []),
                             facilitator, now_ms)
        else:
            _resolve_breakdown(state, pending, now_ms, decider=decider)
    slot = 0
    for team in (state.get('teams') or []):
        team_id = team.get('id') or team.get('directorId')
        if team_id in pending_map:
            continue  # this team already has an in-flight breakdown ceremony
        req_ids = [r['id'] for r in _pending_breakdown_requests(state, team_id)]
        if not req_ids:
            continue  # nothing to plan -- don't convene an empty meeting
        if _team_in_active_sprint(state, team_id):
            # The team is committed to a sprint; the pending breakdown request
            # stays queued and is carded once the team is free (same rule as a
            # team in an active sprint not refining mid-sprint).
            continue
        facilitator = _ceremony_facilitator(state, team_id)
        if not facilitator:
            continue  # this team has no free+on-duty facilitator right now
        req_ids = req_ids[:BREAKDOWN_MAX_REQUESTS]
        if _start_breakdown(state, team_id, req_ids, facilitator, now_ms, pos_offset=slot):
            slot += 1  # only to spread simultaneous ceremonies apart in the room


# ---------------------------------------------------------------------------
# WS-14: sprint retrospective (START / STOP / CONTINUE). When a sprint closes,
# its team meets at the Command Center -- the scrum master + the team's
# non-director members, with the DIRECTOR explicitly excluded (a retrospective
# is the team's own reflection, per the player's design) -- and records what to
# start, stop, and keep doing. Same convene-then-resolve ceremony shape as
# backlog refinement; the decider is injectable like every other Jev decision.
# ---------------------------------------------------------------------------


def _retro_scrum_master(state, team_ids):
    """The agent who facilitates a sprint's retrospective: the first team in the
    sprint's team list with a NON-DIRECTOR effective scrum master; else -- for a
    team whose only scrum master is its own director (small teams below
    SCRUM_MASTER_MIN_TEAM_SIZE, per the player's design the director already
    serves as the scrum master) -- that team's OWN director stands in as
    facilitator. The OWN director is preferred over any borrowed one: a retro is
    the team's own reflection and should be run by someone the team actually
    works with, not an outsider. Only when the own director is unavailable
    (busy/off-duty) is a non-busy director from ANOTHER team borrowed in.
    Returns None only when no facilitator can be found at all (the retro then
    waits for a later pass)."""
    teams = {t.get('id'): t for t in (state.get('teams') or [])}
    for tid in team_ids or []:
        sm = _refinement_scrum_master_for_team(state, tid)
        if sm and sm != (teams.get(tid) or {}).get('directorId'):
            return sm
    # No non-director scrum master among the sprint teams: the team's OWN
    # director stands in (a small team's director already serves as its scrum
    # master). Must be free + on-duty to convene; else defer to a later pass.
    for tid in team_ids or []:
        did = (teams.get(tid) or {}).get('directorId')
        if not did:
            continue
        a = (state.get('agents') or {}).get(did)
        if a and not a.get('busy') and not a.get('offDuty'):
            return did
    # Own director busy/off-duty: fall back to borrowing a non-busy director
    # from another team rather than leaving the team without a retro.
    sprint_directors = {(teams.get(tid) or {}).get('directorId') for tid in (team_ids or [])}
    agents = state.get('agents') or {}
    for t in (state.get('teams') or []):
        did = t.get('directorId')
        if not did or did in sprint_directors:
            continue
        a = agents.get(did)
        if a and not a.get('busy') and not a.get('offDuty'):
            return did
    return None


def _retro_attendees(state, team_ids, scrum_master_id):
    """The retrospective's attendees: the scrum master + every non-director
    member of the sprint's teams, with the DIRECTOR excluded. A busy attendee
    defers the whole meeting (never pull an agent out of a live collaboration);
    an off-duty attendee is parked and free, and the convene wakes her. Returns
    the attendee ids, or None if any attendee is busy."""
    agents = state.get('agents') or {}
    ids = [scrum_master_id]
    for tid in team_ids or []:
        t = next((x for x in (state.get('teams') or []) if x.get('id') == tid), None)
        if not t:
            continue
        director_id = t.get('directorId')
        for aid in _sim_direct_reports(state, director_id):
            if aid not in ids and aid != director_id:
                ids.append(aid)
    for aid in ids:
        a = agents.get(aid)
        if not a or a.get('busy'):
            return None
    return ids


def _start_retrospective(state, sprint_id, team_ids, scrum_master_id, now_ms, pos_offset=0):
    """Convene the sprint retrospective at the Command Center: snapshot each
    attendee's prior state into `pending['people']`, mark them busy at a spread,
    and schedule the resolve RETRO_MEET_MS later. Returns True if convened
    (attendees healthy), False to defer and retry next pass."""
    ids = _retro_attendees(state, team_ids, scrum_master_id)
    if not ids:
        return False
    agents = state.get('agents') or {}
    snap = {}
    base_x = 315 + (pos_offset * 460)
    for i, aid in enumerate(ids):
        a = agents.get(aid)
        snap[aid] = {k: a.get(k) for k in ('offDuty', 'visible', 'x', 'y', 'dir',
                                           'task', 'busy', 'inRoom', 'pairWith', 'handoff', 'workUntil')}
        a['busy'] = True
        a['visible'] = True
        a['offDuty'] = False
        a['task'] = None
        a['inRoom'] = 'commandcenter'
        a['dir'] = 'south'
        a['roomX'] = base_x + (i * 85)
        a['roomY'] = 155
    state.setdefault('pendingRetrospectives', {})[sprint_id] = {
        'at': now_ms + RETRO_MEET_MS, 'embarked': True,
        'sprintId': sprint_id, 'scrumMasterId': scrum_master_id,
        'teamIds': team_ids, 'people': snap,
    }
    _log_governance(state, scrum_master_id, 'retrospective',
                    {'action': 'meeting_start', 'sprint': sprint_id})
    return True


def _resolve_retrospective(state, pending, now_ms, decider=None):
    """When the retrospective window elapses, the scrum master records the
    just-closed sprint's START / STOP / CONTINUE reflections via the injectable
    decider, everyone returns to their prior state, and the record lands in
    state['retrospectives'][sprint_id]. Idempotent if the sprint vanished."""
    decider = decider or _retro_decider
    sprint_id = pending.get('sprintId')
    agents = state.get('agents') or {}
    scrum_master_id = pending.get('scrumMasterId')
    scrum_master = (agents.get(scrum_master_id) or {})
    sm_def = next((d for d in (state.get('agentRoster') or []) if d.get('id') == scrum_master_id), {})
    sm_name = sm_def.get('name') or scrum_master.get('name') or scrum_master_id
    record = (state.get('sprints') or {}).get(sprint_id) or {}
    progress = sprint_progress(state, sprint_id) or {}
    landed = progress.get('landed') or []
    landed_text = ', '.join(landed) if landed else '(nothing landed)'
    velocity = record.get('velocity') or {}
    velocity_line = ''
    if velocity:
        pts = ''
        if velocity.get('pointsTotal') is not None:
            pts = (f", {velocity.get('pointsLanded')} of "
                   f"{velocity.get('pointsTotal')} points shipped")
        velocity_line = (f" Sprint velocity: {velocity.get('landed')} of "
                         f"{velocity.get('total')} items landed ({velocity.get('pct')}%), "
                         f"{velocity.get('rolledOver')} carried over{pts}.")
    instructions = (
        f"{sm_name} is the scrum master facilitating the retrospective for the just-closed sprint "
        f"'{record.get('name') or sprint_id}' (goal: {record.get('goal') or 'none'}). "
        f"What landed this sprint: {landed_text}.{velocity_line} "
        "Name concrete, honest START / STOP / CONTINUE items for the team. START = something the team "
        "should begin doing (a new habit). STOP = something that wasted effort or didn't work. "
        "CONTINUE = something that worked and should be kept. 2-4 terse items per bucket; every item must be "
        "a specific behavior the team controls, never a vague compliment. Respond with ONLY valid JSON, no "
        "markdown fences: {\"start\":[\"...\"],\"stop\":[\"...\"],\"continue\":[\"...\"]}"
    )
    data = decider(state, instructions, sprint_id, landed)
    buckets = {'start': [], 'stop': [], 'continue': []}
    if isinstance(data, dict):
        for k in buckets:
            v = data.get(k)
            if isinstance(v, list):
                buckets[k] = [str(x).strip()[:160] for x in v if str(x).strip()][:4]
    # Restore attendees; the meeting burned none of a mid-task attendee's time.
    for aid, snap in (pending.get('people') or {}).items():
        _restore_refinement_agent(state, aid, snap, now_ms, RETRO_MEET_MS)
    state.setdefault('retrospectives', {})[sprint_id] = {
        'id': sprint_id,
        'sprintId': sprint_id,
        'sprintName': record.get('name') or sprint_id,
        'landed': landed,
        'velocity': record.get('velocity') or None,
        'teamIds': pending.get('teamIds') or [],
        'attendees': list((pending.get('people') or {}).keys()),
        'completedAt': now_ms,
        'start': buckets['start'],
        'stop': buckets['stop'],
        'continue': buckets['continue'],
    }
    _log_governance(state, scrum_master_id, 'retrospective',
                    {'action': 'meeting_end', 'sprint': sprint_id,
                     'start': len(buckets['start']), 'stop': len(buckets['stop']),
                     'continue': len(buckets['continue'])})
    state.setdefault('pendingRetrospectives', {}).pop(sprint_id, None)


def _retro_decider_default(state, instructions, sprint_id, landed):
    """Default resolver for a sprint retrospective: a low-tier chat call that
    returns a START / STOP / CONTINUE JSON object, accrued to its own spend
    bucket. Returns the parsed dict, or an empty dict on a model outage (an
    empty retro is recorded rather than blocking the sprint's close)."""
    try:
        import serve
    except Exception:
        return {}
    try:
        model = serve._low_tier_slug()
        data = serve._call_openrouter_sync(
            model,
            [{'role': 'system', 'content': instructions},
             {'role': 'user', 'content': f"Run the retrospective for sprint {sprint_id}."}],
            max_tokens=200)
        cost = (data.get('usage') or {}).get('cost', 0.0)
        if isinstance(cost, (int, float)) and cost:
            serve._accrue_spend('__retrospectives__', cost)
        reply = ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        start = reply.find('{')
        end = reply.rfind('}')
        if start != -1 and end > start:
            reply = reply[start:end + 1]
        parsed = json.loads(reply)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}


# Injectable for tests (mirrors _refinement_decider / _governance_decider); the
# live loop uses the low-tier default. The retro NEVER holds the sprint's close
# -- it is a ceremony on a later pass, so an outage costs nothing but the text.
_retro_decider = _retro_decider_default


def _retro_step(state, now, now_ms, decider=None):
    """One sprint-retrospective pass, called from _task_cycle ungated (the retro
    is the point even on a quiet think tank, and must not be starved by the work
    gate). A pass (a) advances every in-flight retrospective (embark on one pass,
    RELEASE on the next -- same no-required-wait as refinement), and (b) convenes
    a retrospective for every sprint queued in pendingSprintRetros whose team has
    a non-director scrum master + healthy attendees; a sprint whose team isn't
    ready yet stays queued for the next pass."""
    pending_map = state.setdefault('pendingRetrospectives', {})
    for sid in list(pending_map.keys()):
        pending = pending_map[sid]
        if not pending.get('embarked'):
            _start_retrospective(state, sid, pending.get('teamIds', []),
                                 pending.get('scrumMasterId'), now_ms)
        else:
            _resolve_retrospective(state, pending, now_ms, decider=decider)
    slot = 0
    queued = state.get('pendingSprintRetros') or []
    for sid in list(queued):
        if sid in pending_map:
            continue  # this sprint already has an in-flight retrospective
        record = (state.get('sprints') or {}).get(sid) or {}
        scrum_master_id = _retro_scrum_master(state, record.get('teamIds') or [])
        if not scrum_master_id:
            continue  # no facilitator available (own director busy, no other free director) -- retro waits
        team_ids = [t for t in (record.get('teamIds') or []) if _team_row(state, t)]
        if _start_retrospective(state, sid, team_ids, scrum_master_id, now_ms, pos_offset=slot):
            queued.remove(sid)
            slot += 1


# ---------------------------------------------------------------------------
# Cut 2: three missing team processes -- deliverable grading +
# director roadmap, the coaching loop (growth plan -> next task), and incident
# runbooks. All follow the established pattern: injectable Jev decision +
# deterministic outage fallback, fire-and-forget (never block the completion
# path), pure state mutation. Grade + runbook write on COMPLETION; coaching +
# runbook notes are APPENDED to instructions at ASSIGNMENT.
# ---------------------------------------------------------------------------

_GRADE_BUCKETS = {
    '0': 'Useless or broken -- does not meet the spec at all; no real value shipped.',
    '2': 'Barely functional -- major gaps, mostly missing the spec, would need to be redone.',
    '4': 'Weak -- meets a small part of the spec; needs significant rework before it is usable.',
    '6': 'Adequate -- meets the core spec with real rough edges or gaps in polish.',
    '8': 'Solid -- meets the spec well; only minor polish or edge cases remain.',
    '10': 'Excellent -- fully meets the spec, no real gaps, cleanly serves the roadmap.',
}


def _grading_decider_default(state, instructions, task_title, room):
    """Default Jev resolver for grading a landed deliverable (0-10 score).
    Returns a float score or None on a Jev outage (-> deterministic fallback).

    Bug fixed: Jev's real API (POST /api/alpha/decisions) is
    a TYPED MULTIPLE-CHOICE system -- every other real call site in this file
    passes several named options and Jev picks one; it has no "give me a free
    number" mode. The original version here passed exactly ONE option keyed
    '0-10' with instructions asking for "a plain integer" -- Jev can only ever
    hand that single key back verbatim, so `choice` was always the STRING
    '0-10', `isinstance(choice, (int, float))` was always False, and grading
    silently fell back to the deterministic default on every single call --
    every deliverable graded exactly 5.0. Fixed by actually
    giving Jev several real, named score buckets to choose among (matching
    every other working choice call in this file), which restores real
    variance instead of a permanent, invisible fallback."""
    try:
        import serve
    except Exception:
        return None
    try:
        data = serve._call_openrouter_decision_sync(
            serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice', 'instructions': instructions,
                        'criteria': dict(_GRADE_BUCKETS)}})
        choice, _, _ = serve._jev_choice(data)
        if choice in _GRADE_BUCKETS:
            return float(choice)
    except Exception:
        pass
    return None


_grading_decider = _grading_decider_default


def _runbook_decider_default(state, instructions, product_id, room, title, agent_id=None):
    """Default resolver for an incident runbook: one line "what broke + how it
    was fixed". Returns a string, or None on a call failure -> fallback.

    Bug fixed: this used to route through Jev's typed
    multiple-choice decision endpoint with a single named option ('summary'),
    the same malformed shape _grading_decider_default had. Jev could only ever
    hand that one key back verbatim -- but here the `isinstance(choice, str)`
    check doesn't reject a bare string, so it silently accepted the literal
    word "summary" as the runbook content on every call (a real, findable bug:
    grep runbooks for the word "summary" verbatim). This was never actually a
    CHOICE task (pick one of several named options) -- it's free-text
    generation, which Jev's decisions endpoint cannot do at all. Fixed by
    routing it through a real /api/chat completion instead, the same
    self-loopback pattern every content executor already uses, attributed to
    the agent who actually handled the incident."""
    try:
        import serve
    except Exception:
        return None
    try:
        base = serve.SELF_BASE_URL
        key = serve.get_or_create_agent_key(agent_id) if agent_id else None
        tier_slug = serve._resolve_model_tier('Write one runbook line: what broke in this incident and how it was fixed')
        if not tier_slug:
            return None
        r = serve._http_json('POST', base, '/api/chat',
                             {'model': tier_slug,
                              'messages': [{'role': 'system', 'content': instructions},
                                           {'role': 'user', 'content': 'Write the runbook line.'}],
                              'max_tokens': 80, 'agentId': agent_id,
                              'service': product_id or '__general__'}, key)
        reply = (r.get('reply') or '').strip() if isinstance(r, dict) and not r.get('error') else ''
        if reply:
            return reply[:300]
    except Exception:
        pass
    return None


_runbook_decider = _runbook_decider_default


def _room_trailing_grade(state, room):
    """Trailing-window mean grade for a room's recent completed deliverables
    (the signal the roadmap recompute keys on, and this room's own fallback
    when Jev can't grade a NEW deliverable). Returns a float or None.

    Bug fixed (Hermes Town comparison -- "never invent an
    outcome, keep it visibly unresolved instead"): a fabricated fallback grade
    used to carry no marker distinguishing it from a real Jev judgment, so a
    string of Jev outages could compound -- each fallback's flat/room-mean
    grade fed straight into the NEXT outage's trailing mean, quietly drifting
    the roadmap signal on manufactured data. `gradeIsReal` (see
    _grade_completed_task) lets this exclude fabricated grades from the
    average; legacy rows written before this field existed have no key at
    all and are treated as real (`.get(..., True)`) rather than silently
    dropped, since there's no way to know which they were retroactively."""
    grades = [d.get('grade') for d in (state.get('completedDeliverables') or [])
              if d.get('room') == room and isinstance(d.get('grade'), (int, float))
              and d.get('gradeIsReal', True)]
    if not grades:
        return None
    return round(sum(grades) / len(grades), 1)


def _roadmap_release_demand(state, room):
    """How much a room has shipped recently -- the 'demand' half of the roadmap
    feedback (rooms that keep delivering stay workstreams; ones that stall on
    weak grades get reprioritized by the director). Count of recent completions."""
    return sum(1 for d in (state.get('completedDeliverables') or [])
               if d.get('room') == room)


def _agent_trailing_grade(state, agent_id):
    """Trailing-window mean grade for ONE agent's real completed deliverables
    (the per-agent health signal the director's team-health review keys on).
    Only real Jev grades count -- fabricated fallbacks (gradeIsReal=False) would
    otherwise pull a weak agent's mean toward 5.0 and hide the signal. Returns a
    float or None when the agent has no real graded deliverable yet."""
    grades = [d.get('grade') for d in (state.get('completedDeliverables') or [])
              if d.get('agentId') == agent_id and isinstance(d.get('grade'), (int, float))
              and d.get('gradeIsReal', True)]
    if not grades:
        return None
    return round(sum(grades) / len(grades), 1)


def _team_health_review(state, team_ids, now_ms):
    """Director gap 1: team-health review at sprint close. Each director reviews
    the health of the WORKERS under them (never directors -- see the worker-only
    judgment directive): an agent whose real trailing grade sits below the
    delivery floor gets a coaching growth-plan note routed to their NEXT task
    (via the existing _write_growth_plan/_coaching_note_for loop), so a weak
    close turns into changed next execution rather than silent drift. Workers
    with no real graded work yet are not judged. Pure state mutation, no Jev
    spend -- the trailing mean IS the ground truth signal. Idempotent per agent:
    _write_growth_plan dedups by kind, so a repeated low close re-coaches (the
    coaching loop, W5) without spamming identical notes."""
    if not team_ids:
        return 0
    reviewed = 0
    for tid in team_ids:
        team = _team_row(state, tid)
        if not team:
            continue
        director_id = team.get('directorId') or team.get('id')
        # Worker-only judgment: a director's own team-health review must never
        # grade another director (or the admin), matching who_needs_review.
        for worker_id in _sim_direct_reports(state, director_id):
            wdef = next((d for d in (state.get('agentRoster') or [])
                         if d.get('id') == worker_id), None)
            if not wdef or wdef.get('isDirector') or wdef.get('isAdmin'):
                continue
            grade = _agent_trailing_grade(state, worker_id)
            if grade is None or grade >= DELIVERABLE_GRADE_FLOOR:
                continue
            _write_growth_plan(
                state, worker_id, team.get('room') or 'pressoffice',
                'team_health', now_ms,
                f"Sprint close team-health review: trailing grade {grade}/10 is "
                f"below the {DELIVERABLE_GRADE_FLOOR:.0f} delivery floor. Focus this "
                f"sprint's work on meeting the spec and room standards end-to-end.",
                repeat=True)
            reviewed += 1
    return reviewed


def _grade_completed_task(state, agent_id, task, now_ms):
    """Fire-and-forget grader for a landed deliverable. Never blocks completion;
    on outage records a deterministic default (room's trailing mean, else mid-
    5.0). Also triggers a growth-plan entry if graded below the floor."""
    if not task or task.get('taskType') in ('spike', 'bug') or task.get('reviewOf'):
        return  # no committed deliverable -> nothing to grade
    room = task.get('room')
    if not room or room not in VALUED_QUEUE_ROOMS:
        return
    title = task.get('title') or 'untitled deliverable'
    a = (state.get('agents') or {}).get(agent_id) or {}
    instructions = (f"Grade the deliverable '{title}' landed in the {room} room by "
                    f"{a.get('name') or agent_id} ({a.get('role') or 'worker'}). "
                    f"Did it meet the roadmap and the room's spec? Give a 0-10 score.")
    real_grade = _grading_decider(state, instructions, title, room)
    grade_is_real = real_grade is not None
    if real_grade is None:
        real_grade = _room_trailing_grade(state, room)
        if real_grade is None:
            real_grade = 5.0
    grade = real_grade
    state.setdefault('completedDeliverables', []).append({
        'id': task.get('id'), 'room': room, 'title': title,
        'agentId': agent_id, 'grade': grade,
        # Hermes Town lesson: a fabricated fallback grade must be
        # distinguishable from a real Jev judgment, not silently identical --
        # see _room_trailing_grade's own comment for why this also matters
        # beyond just labeling (it stops fallback grades compounding into
        # future fallbacks).
        'gradeIsReal': grade_is_real, 'gradedAt': now_ms,
        # Knowledge-rot/freshness signal (external-eval port): the in-document
        # "Last reviewed" date is authoritative for aging -- mtime alone is a weak
        # signal (resets on copy). Consumers can treat a deliverable newer than
        # its lastReviewed date as awaiting (re)review.
        'lastReviewed': datetime.datetime.utcnow().isoformat() + 'Z',
        'source': 'refinement' if (task.get('instructions') or '').find('backlog refinement') >= 0 else 'other',
    })
    if grade < DELIVERABLE_GRADE_FLOOR:
        _coach_low_grade(state, agent_id, room, grade, now_ms, title)
    elif grade >= SUCCESS_GRADE_FLOOR:
        # Positive learning loop (ChatDev ECL): a deliverable graded at/above
        # the success floor is the raw material for the weekly success-mining
        # pass -- "what made this good" should propagate, not just "what went
        # wrong". Best-effort: a ledger failure is swallowed so grading never
        # blocks completion (same contract as _rule_mine_step's failure path).
        try:
            import serve as _serve_mod
            _serve_mod._record_success(room, title, grade, agent_id=agent_id)
        except Exception:
            pass


def _coach_low_grade(state, agent_id, room, grade, now_ms, title):
    """W5: round-aware low-grade coaching. A single _write_growth_plan deduped by
    kind coached a weak worker ONCE; a worker who keeps closing below the floor
    then absorbed the identical note forever. Each low close re-lands a fresh,
    escalating coaching note (repeat=True) up to COACHING_MAX_ROUNDS, then the
    problem escalates to the owning director (a work-request + governance entry)
    -- either the coaching works, or it surfaces. Bounded: escalation fires once
    per round-cap, and the work-request dedups."""
    plans = state.setdefault('growthPlans', {}).setdefault(agent_id, [])
    rounds = sum(1 for p in plans if p.get('kind') == 'low_grade')
    if rounds >= COACHING_MAX_ROUNDS:
        _escalate_coaching_loop(state, agent_id, room, grade, now_ms, title)
        return
    _write_growth_plan(
        state, agent_id, room, 'low_grade', now_ms,
        f"Deliverable '{title}' in {room} graded {grade}/10 (below the "
        f"{DELIVERABLE_GRADE_FLOOR:.0f} floor). Coaching round {rounds + 1} of "
        f"{COACHING_MAX_ROUNDS}: you've been coached on this before and the grade "
        f"hasn't recovered -- meet the spec and room standards end-to-end this time.",
        repeat=True)


def _escalate_coaching_loop(state, agent_id, room, grade, now_ms, title):
    """Terminal coaching-loop escalation: a worker who has closed below the
    floor COACHING_MAX_ROUNDS times without improvement is surfaced to the
    owning team's scrum master as a work-request (the same SM-routing as the
    stale-work/help sweeps), so the SM can help, re-plan, or reassign -- a
    bounded end to the loop, never an infinite re-coach. Logs governance."""
    def _director_for(aid):
        for d in (state.get('agentRoster') or []):
            if d.get('id') == aid:
                return d.get('director')
        return None
    director_id = _director_for(agent_id) or room  # fall back to room-keyed team
    team = None
    for t in (state.get('teams') or []):
        if t.get('directorId') == director_id or t.get('room') == room:
            team = t
            break
    filer = None
    if team:
        filer = _escalation_scrum_master_for(state, team.get('directorId') or team.get('id'))
        if not filer:
            filer = team.get('directorId') or team.get('id')
    routed = False
    if filer:
        routed = bool(file_work_request(
            state, filer,
            (f'Help: {agent_id} keeps closing below the delivery floor')[:120],
            room,
            reason=(f'{agent_id} has closed below the {DELIVERABLE_GRADE_FLOOR:.0f} floor '
                    f'{COACHING_MAX_ROUNDS}+ times (latest: {title}, {grade}/10). Coaching is '
                    f'not working; the scrum master decides whether to help, re-plan, or reassign.')))
    _log_governance(state, 'admin', 'coaching_loop_escalated',
                    {'agent': agent_id, 'title': (title or '')[:120], 'room': room,
                     'grade': grade, 'routed': routed})


def _runbook_task(state, task, now_ms):
    """Fire-and-forget runbook writer for a completed INCIDENT (bug): one line
    captured for the owning product so future handlers learn, not re-discover."""
    if not task or task.get('taskType') != 'bug':
        return
    product_id = task.get('productId')
    if not product_id:
        return
    title = task.get('title') or 'incident'
    a = (state.get('agents') or {}).get(task.get('assignedTo')) or {}
    instructions = (f"Write one line for the runbook: what broke in this incident "
                    f"('{title}') and how {a.get('name') or task.get('assignedTo')} fixed it.")
    real_summary = _runbook_decider(state, instructions, product_id, task.get('room'), title,
                                    agent_id=task.get('assignedTo'))
    summary_is_real = bool(real_summary)
    summary = real_summary or f"{title} -- fixed by {a.get('name') or task.get('assignedTo')}"
    rb = state.setdefault('runbooks', {}).setdefault(product_id, [])
    # Hermes Town lesson: mark a fallback template line as such --
    # a reader shouldn't mistake a generic "X -- fixed by Y" filler for a real,
    # model-written account of what actually broke and how.
    rb.append({'ts': now_ms, 'room': task.get('room'), 'summary': summary, 'summaryIsReal': summary_is_real})
    if len(rb) > RUNBOOK_MAX_ENTRIES_PER_PRODUCT:
        del rb[:-RUNBOOK_MAX_ENTRIES_PER_PRODUCT]


def _write_growth_plan(state, agent_id, room, kind, now_ms, note, repeat=False):
    """Record a coaching note against an agent (dedup by kind so repeated low
    grades don't spam identical plans). The note is APPENDED once to that
    agent's next assigned task by _coaching_note_for. `repeat=True` bypasses the
    dedup (a NEW commitment each event -- e.g. a fresh Knowledge Social adopt --
    must land even when an older one of the same kind already queued/applied)."""
    plans = state.setdefault('growthPlans', {}).setdefault(agent_id, [])
    if not repeat:
        for p in plans:
            if p.get('kind') == kind:
                return  # already coaching on this axis
    plans.append({'kind': kind, 'ts': now_ms, 'room': room, 'note': note, 'applied': False})


def _coaching_note_for(state, agent_id):
    """Pop the oldest un-applied growth plan for an agent and return it as a
    coaching line to append to their next task; marks it applied. Returns None
    if none pending. This is the 'manager feedback changes the next execution'
    step -- each note is injected exactly once."""
    plans = (state.get('growthPlans') or {}).get(agent_id)
    if not plans:
        return None
    for p in plans:
        if not p.get('applied'):
            p['applied'] = True
            return p.get('note')
    return None


def _runbook_note_for(state, product_id):
    """The most recent runbook entry for a product, as a one-line coaching-style
    note for a new incident task. Returns None if the product has no history."""
    entries = (state.get('runbooks') or {}).get(product_id) or []
    if not entries:
        return None
    last = entries[-1]
    return f"Prior incident in this product was: {last.get('summary')}"


def _refocus_note_for(state, product_id):
    """One-line reminder of the product's stated purpose (name + summary),
    for a task that's REVISITING already-done work -- a review/revision or
    an incident -- rather than starting fresh. That's the moment a series of
    locally-defensible additions has the most room to have already compounded
    into scope nobody asked for (each individual review comment made sense;
    the sum of them may not still serve what the product was for). Returns
    None when there's no product record or no summary to anchor against, so
    this never fabricates a "why" that isn't actually on file.
    Added after comparing this think tank's own accumulated review/
    escalation machinery (see _CEREMONY_ACTIONS in serve.py) to a pattern
    seen elsewhere -- checking alignment against the stated goal specifically
    at the re-review moment, not on every fresh task, which would just be
    more ceremony."""
    if not product_id:
        return None
    prod = (state.get('products') or {}).get(product_id)
    if not prod:
        return None
    summary = (prod.get('summary') or '').strip()
    if not summary:
        return None
    return (f'Refocus before continuing: this task is part of "{prod.get("name")}", '
            f'whose stated purpose is: {summary}. Confirm what you\'re about to do still '
            f'serves that -- not just what looks like a natural next addition from here.')


def _augment_task_instructions(state, agent_id, product_id, instructions, refocus=False):
    """Append coaching + runbook knowledge to a task's instructions BEFORE
    assignment. Pure: returns the augmented instructions (or the original).
    A pending growth plan is injected once (marking it applied); a product's
    runbook history is pulled into a new incident on that product; a refocus
    reminder (see _refocus_note_for) is added only when the caller says this
    task is revisiting existing work, not starting fresh. The hive-mind
    consensus relay (see _consensus_relay_step) is READ FIRST -- a one-line
    'current direction' the next cycle acts on."""
    pieces = [instructions] if instructions else []
    # The spine FIRST: the charter goal opens every task's instructions so the
    # work a room does points at the player's direction (see _charter_note).
    charter_line = _charter_note(state)
    if charter_line:
        pieces.insert(0, charter_line)
    relay = _consensus_relay_note(state)
    if relay:
        pieces.insert(0, relay)
    note = _coaching_note_for(state, agent_id)
    if note:
        pieces.append(note)
    if product_id:
        rb = _runbook_note_for(state, product_id)
        if rb:
            pieces.append(rb)
    if refocus:
        rf = _refocus_note_for(state, product_id)
        if rf:
            pieces.append(rf)
    if not pieces:
        return instructions
    return '\n'.join(p for p in pieces if p)


def set_charter(state, goal, interests=None, notes='', now_ms=None):
    """The player's charter -- the tank-level SPINE (the article's rule 2: one
    direction every lane points at). Player-owned: the player decides, the tank
    plans against it. `goal` is the north-star statement; `interests` are the
    free-form threads (crafts, topics, rooms) that goal is built from; `notes`
    is anything else the directors should weigh. Stores on state['charter'] and
    returns the record, or None when no goal is given (a vague goal gets a
    vague plan -- the tank never invents a spine for you)."""
    goal = (goal or '').strip()
    if not goal:
        return None
    interests = [str(i).strip() for i in (interests or []) if str(i).strip()]
    charter = {
        'goal': goal,
        'interests': interests,
        'notes': (notes or '').strip(),
        'updatedAt': int(time.time() * 1000) if now_ms is None else int(now_ms),
    }
    state['charter'] = charter
    return charter


def get_charter(state):
    """The player's charter record, or None (a fresh tank has no spine until
    the player sets one -- the tank never fabricates a direction)."""
    ch = state.get('charter')
    if not isinstance(ch, dict) or not (ch.get('goal') or '').strip():
        return None
    return ch


def _charter_alignment(state, room):
    """Is `room` aligned with the charter's spine? True when the charter's goal
    or interests name any of the room's craft keywords (see _ROOM_KEYWORDS) --
    the article's rule 2 made real: fragmented interests that share a spine
    stop competing. Returns True/False, or None when no charter exists."""
    ch = get_charter(state)
    if not ch:
        return None
    text = ' '.join([ch.get('goal') or '', ' '.join(ch.get('interests') or [])]).lower()
    if not text:
        return False
    return any(k in text for k in (_ROOM_KEYWORDS.get(room) or []))


def _charter_note(state):
    """One line the tank prepends to every task's instructions: the spine the
    work points at. Returns None when no charter exists."""
    ch = get_charter(state)
    if not ch:
        return None
    return f"Tank charter: {ch['goal']}"


def _roadmap_step(state, now_ms):
    """Weekly silent recompute: director-owned roadmap maps each room to a
    priority derived from trailing grades + recent delivery. A room that shipped
    poorly (low grade) or that agents keep flagging gets higher priority so fresh
    work lands where it's weakest. Pure derivation -- no ceremony, no Jev spend.

    The spine: when the player has set a charter, a room aligned with it gets a
    priority boost -- and a FRESH room (no grade, no delivery yet) defaults to
    aligned>0 instead of 0, so the charter decides a new tank's direction
    instead of every room starting flat. The charter aligns, the grades steer."""
    if now_ms - (state.get('lastRoadmapReviewAt') or 0) < ROADMAP_CADENCE_MS:
        return
    state['lastRoadmapReviewAt'] = now_ms
    roadmap = state.setdefault('roadmap', {})
    for room in VALUED_QUEUE_ROOMS:
        gm = _room_trailing_grade(state, room)
        demand = _roadmap_release_demand(state, room)
        aligned = _charter_alignment(state, room)
        prev = roadmap.get(room) or {}
        if gm is None and demand == 0:
            # No history yet -- leave whatever the director set, else default.
            # The charter decides a fresh room's starting priority: aligned
            # rooms get a baseline so the spine (not the void) picks direction.
            if not prev:
                roadmap[room] = {'priority': 1 if aligned else 0,
                                 'ownerId': prev.get('ownerId'),
                                 'lastGrade': None, 'demand': 0,
                                 'charterAligned': aligned}
            continue
        # Weak grade and/or low delivery -> higher priority to rebalance.
        priority = 0
        if gm is not None and gm < DELIVERABLE_GRADE_FLOOR:
            priority += 2
        if demand == 0:
            priority += 1           # starved room -- feed it
        elif gm is not None and gm < 7:
            priority += 1
        if aligned:
            priority += 1           # on the spine -- keep its thread alive
        roadmap[room] = {'priority': priority, 'ownerId': prev.get('ownerId'),
                         'lastGrade': gm, 'demand': demand,
                         'charterAligned': aligned}


def _consensus_relay_step(state, now_ms):
    """Weekly silent recompute of the hive-mind consensus relay (auto-co-meta's
    'read consensus -> act -> update -> repeat'). One small shared state note
    the next cycle reads FIRST (_augment_task_instructions prepends it) and
    that this step updates LAST, so a think tank's direction survives between
    cycles and across restarts (state is persisted). Derives the current
    consensus from the roadmap the director just recomputed: the highest-
    priority room and why it is weak/starved. Pure derivation -- no ceremony,
    no Jev spend. A no-op until a roadmap exists, so a fresh think tank never
    fabricates a consensus that isn't grounded."""
    roadmap = state.get('roadmap') or {}
    ranked = sorted(((roadmap[r].get('priority') or 0, r) for r in roadmap),
                    reverse=True)
    if not ranked:
        return
    priority, room = ranked[0]
    gm = _room_trailing_grade(state, room)
    reasons = []
    if gm is not None and gm < DELIVERABLE_GRADE_FLOOR:
        reasons.append(f'weak trailing grade ({gm}/10)')
    if _roadmap_release_demand(state, room) == 0:
        reasons.append('no recent delivery')
    why = ', '.join(reasons) if reasons else 'prioritized by the director'
    # The spine (when the player set one): the charter goal leads the consensus
    # so the direction every task acts on names the GOAL, not just the weakest
    # room. "point them at one thing, and they all turn into one direction."
    charter = get_charter(state)
    lead = f"Current direction: {charter['goal']}. " if charter else ''
    state['consensusRelay'] = {
        'room': room,
        'consensus': f"{lead}Focus next work on {room} ({why}).",
        'updatedAt': now_ms,
    }


def _consensus_relay_note(state):
    """Read the consensus relay FIRST -- the one-line 'current direction' the
    next cycle acts on. Returns None when no consensus has been derived yet."""
    relay = state.get('consensusRelay')
    if not relay:
        return None
    return relay.get('consensus')


def _rule_mine_step(state, now_ms):
    """Weekly silent pass: mine recurring classified failures (the review
    ledger in serve.py) into operator rule proposals -- the 'write a rule' +
    'add a test' steps of the draft-review loop. Mirrors _roadmap_step: pure
    derivation, no ceremony, no Jev spend. Cadence-gated on the state stamp;
    the mining itself lives in serve.py (file-backed ledger + proposals).
    Nothing is auto-applied -- a proposal surfaces to the operator (governance
    log + the /api/rule-proposals surface) and becomes a real rule only when
    the operator encodes it. Best-effort: a ledger failure just skips this
    week's mine, never blocks the task cycle."""
    if now_ms - (state.get('lastRuleMineAt') or 0) < RULE_MINE_CADENCE_MS:
        return
    state['lastRuleMineAt'] = now_ms
    try:
        import serve as _serve_mod
        created = _serve_mod._mine_rule_proposals()
    except Exception:
        return
    for p in created:
        _log_governance(state, None, 'rule_proposal', {
            'proposalId': p.get('id'),
            'type': p.get('type'),
            'rule': p.get('rule'),
            'count': p.get('count'),
            'dedupeKey': p.get('dedupeKey'),
        })


def _archive_distill_step(state, now_ms):
    """Weekly silent pass: fold the ARCHIVED decision tape (kept permanently --
    see serve._prune_logs's decision_archive table) into a short 'decision
    archive' wiki page, so the think tank can reason over what it has decided as
    a body instead of pruning the memory away. Pure derivation + one wiki write
    (server-authority path, actor 'distill'), no ceremony, no Jev spend.
    Cadence-gated like rule mining; best-effort -- a DB or write failure just
    skips the week."""
    if now_ms - (state.get('lastArchiveDistillAt') or 0) < RULE_MINE_CADENCE_MS:
        return
    state['lastArchiveDistillAt'] = now_ms
    try:
        import serve as _serve_mod
        since_ms = state.get('lastArchiveDistillWindowAt') or (now_ms - RULE_MINE_CADENCE_MS)
        summary = _serve_mod._decision_archive_summary(since_ms / 1000.0)
        if not summary or not summary.get('total'):
            return
        state['lastArchiveDistillWindowAt'] = now_ms
        week_label = datetime.datetime.utcfromtimestamp(now_ms / 1000.0).strftime('%Y-%m-%d')
        section = [f'## {week_label}', '']
        for k in summary.get('kinds') or []:
            section.append(f"- {k['kind']}: {k['count']} decision(s), {k['ok']} ok, ${k['cost']:.4f}")
        section.append(f"- total: {summary.get('total')} decision(s), {summary.get('failed') or 0} failed, ${summary.get('cost') or 0.0:.4f}")
        # Read the existing page (if any) and keep only the last 8 weekly
        # sections so the archive page stays bounded as the years accumulate.
        existing = _serve_mod._wiki_page_body('decision-archive', 'think_tank') or ''
        body = '\n'.join(section) + '\n'
        if existing.strip():
            kept = existing.split('\n## ')[:8]
            body = '\n## '.join(kept).strip('\n') + '\n\n' + body
        _serve_mod._write_wiki_server('decision-archive', 'Decision Archive',
                                      'think_tank', body)
    except Exception:
        return


def _success_mine_step(state, now_ms):
    """Weekly silent pass: mine recurring high-graded deliverables (the success
    ledger in serve.py) into operator success-lesson proposals -- the positive
    half of the rule-mining loop, so 'agents get smarter' by propagating what
    WORKED, not just by avoiding what failed (ChatDev ECL: acquisition,
    utilization, propagation, elimination). Same shape as _rule_mine_step:
    pure derivation, no ceremony, no Jev spend, cadence-gated on the state
    stamp. Nothing is auto-applied -- a proposal surfaces to the operator
    (governance log + the /api/success-proposals surface) and becomes a real
    standard only when the operator encodes it. Best-effort: a ledger failure
    just skips this week's mine, never blocks the task cycle."""
    if now_ms - (state.get('lastSuccessMineAt') or 0) < RULE_MINE_CADENCE_MS:
        return
    state['lastSuccessMineAt'] = now_ms
    try:
        import serve as _serve_mod
        created = _serve_mod._mine_success_proposals()
    except Exception:
        return
    for p in created:
        _log_governance(state, None, 'success_proposal', {
            'proposalId': p.get('id'),
            'room': p.get('room'),
            'lesson': p.get('lesson'),
            'count': p.get('count'),
            'dedupeKey': p.get('dedupeKey'),
        })


def _refinement_context_for_room(state, room):
    """The roadmap context the refinement groom should weigh: the room's current
    roadmap priority + trailing grade, so a scrum master grooms high-priority /
    weak rooms preferentially. A charter (the spine) is weighed too -- an
    aligned room is worth grooming in, an off-spine one is easier to park.
    Returns a short string or None."""
    roadmap = (state.get('roadmap') or {}).get(room) or {}
    gm = _room_trailing_grade(state, room)
    bits = []
    if roadmap.get('priority'):
        bits.append(f"roadmap priority {roadmap['priority']}")
    if gm is not None:
        bits.append(f"trailing grade {gm}/10")
    aligned = _charter_alignment(state, room)
    if aligned is not None:
        bits.append('charter aligned' if aligned else 'charter: off-spine')
    return ('; '.join(bits)) or None


# ---------------------------------------------------------------------------
# Cut 3: on-call escalation. The on-call agent tried to restore a
# broken product and COULDN'T (assignment-abandoned, or the bug stayed open past
# the restore window). Instead of the incident dying silently, the owning team's
# scrum master grooms it into the backlog: a STORY when the root cause is scoped
# enough to card, a SPIKE when it isn't. Composes with the Cut-1 refinement
# ceremony (stories go through the same backlogRequests pipeline the SM owns).
# ---------------------------------------------------------------------------

def _escalation_decider_default(state, instructions, product_id, title):
    """Default Jev resolver for the on-call escalation groom: is this broken
    product's failure scoped enough to card a STORY, or is the root cause
    unknown enough that we need a SPIKE investigation first? Returns
    'story' | 'spike', or None on a Jev outage (-> spike fallback)."""
    try:
        import serve
    except Exception:
        return None
    try:
        data = serve._call_openrouter_decision_sync(
            serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice', 'instructions': instructions,
                        'criteria': {
                            'story': "The failing behavior is understood and scoped -- a team can card a concrete story to restore it.",
                            'spike': "The root cause is unknown / not yet reproducible -- file an investigation spike before any story can be scoped."}}})
        choice, _, _ = serve._jev_choice(data)
        if choice in ('story', 'spike'):
            return choice
    except Exception:
        pass
    return None


_escalation_decider = _escalation_decider_default


def _escalation_scrum_master_for(state, director_id):
    """The scrum master of an incident's owning team, or None. The SM is who
    owns story creation (Cut 1), so the escalation works THROUGH them -- we
    never bypass the SM to push a card in ourselves."""
    for t in (state.get('teams') or []):
        if t.get('directorId') == director_id and t.get('scrumMasterId'):
            return t['scrumMasterId']
    return None


def _escalated_product_pending(state, product_id):
    """Whether a product already has an in-flight/unresolved escalation for the
    same open incident -- dedup so a prolonged outage doesn't fire a new
    escalation every restore-window tick."""
    pend = state.get('_pendingEscalation')
    if pend and pend.get('productId') == product_id:
        return True
    return product_id in (state.get('_escalatedProducts') or {})


def _start_escalation(state, product_id, director_id, title, source, now_ms):
    """File a pending-escalation record for the owning team's scrum master to
    groom on the next pass. Cheap + idempotent (dedup by product while the
    incident stays unresolved). Returns the scrum master id, or None if the
    team has no SM yet (nothing to escalate to)."""
    if _escalated_product_pending(state, product_id):
        return None
    sm = _escalation_scrum_master_for(state, director_id)
    if not sm:
        return None
    # Cap concurrent escalations per owning team before opening a new one.
    open_count = len([p for p in (state.get('_escalatedProducts') or {}).values()
                      if p.get('directorId') == director_id and p.get('resolved') is not True])
    if open_count >= ESCALATION_MAX_OPEN:
        return None
    state['_pendingEscalation'] = {
        'at': now_ms, 'embarked': False, 'scrumMasterId': sm,
        'directorId': director_id, 'productId': product_id,
        'title': title, 'source': source,
    }
    state.setdefault('_escalatedProducts', {})[product_id] = {
        'at': now_ms, 'directorId': director_id, 'resolved': False,
    }
    return sm


def _escalation_convener(state, product_id, scrum_master_id):
    """The escalation ceremony's attendee: the scrum master alone (this is a
    groom decision, not a meeting). Must be free + on-duty to convene; a
    busy/off-duty SM defers to the next pass."""
    a = (state.get('agents') or {}).get(scrum_master_id)
    if not a or a.get('busy') or a.get('offDuty'):
        return None
    return [scrum_master_id]


def _embark_escalation(state, scrum_master_id, now_ms):
    """Convene the escalation: snapshot the SM's prior state, mark them busy at
    the Command Center, schedule the resolve. Mirrors _start_refinement."""
    ids = _escalation_convener(state, None, scrum_master_id)
    if not ids:
        return False
    a = (state.get('agents') or {}).get(scrum_master_id)
    pend = state['_pendingEscalation']
    pend['people'] = {scrum_master_id: {k: a.get(k) for k in
        ('offDuty', 'visible', 'x', 'y', 'dir', 'task', 'busy', 'inRoom',
         'pairWith', 'handoff', 'workUntil')}}
    a['busy'] = True; a['visible'] = True; a['offDuty'] = False
    a['task'] = None; a['inRoom'] = 'commandcenter'; a['dir'] = 'south'
    a['roomX'] = 315; a['roomY'] = 155
    pend['embarked'] = True
    pend['at'] = now_ms + ESCALATION_MEET_MS
    _log_governance(state, scrum_master_id, 'oncall_escalation',
                    {'action': 'groom_start', 'product': pend.get('productId'),
                     'source': pend.get('source')})
    return True


def _resolve_escalation(state, pending, now_ms, decider=None):
    """When the escalation window elapses, the scrum master grooms the incident:
    injectable decider picks story|spike. A story is filed as a backlogRequests
    record (origin:'oncall_escalation') for the SM's OWN refinement ceremony to
    card -- never bypassing Cut-1 story creation. A spike is queued directly
    (taskType:'spike', non-gated lane). On outage, default to a spike. Restores
    the SM's prior state, records the outcome, clears the pending record."""
    decider = decider or _escalation_decider
    scrum_master_id = pending.get('scrumMasterId')
    sm_def = next((d for d in (state.get('agentRoster') or []) if d.get('id') == scrum_master_id), {})
    sm_name = sm_def.get('name') or scrum_master_id
    product_id = pending.get('productId')
    title = pending.get('title') or 'broken product'
    instructions = (f"{sm_name} is the scrum master for an on-call incident on "
                    f"the {product_id} product ('{title}' -- the on-call agent could "
                    f"not restore it: {pending.get('source')}). Groom it into the backlog: "
                    f"is the failing behavior scoped enough to card a STORY, or is the "
                    f"root cause unknown (file a SPIKE investigation first)?")
    choice = decider(state, instructions, product_id, title)
    if choice not in ('story', 'spike'):
        choice = 'spike'  # outage / unknown -> an investigation card is safer than a guess
    if choice == 'story':
        # File a backlog WORK-REQUEST the SM's refinement ceremony will card.
        # The SM is the filer so per-team grooming keeps it in the right team.
        req_id = f"esc-{int(time.time() * 1000)}"
        (state.setdefault('backlogRequests', [])).append({
            'id': req_id, 'filedBy': scrum_master_id, 'title': title,
            'room': 'pressoffice', 'reason': f"On-call could not restore {product_id}",
            'filedAt': now_ms, 'status': 'pending', 'origin': 'oncall_escalation',
            'productId': product_id,
        })
        outcome = 'story'
    else:
        queue_work(state, [{
            'title': f"SPIKE: root cause of '{title}' in {product_id} unknown",
            'room': 'pressoffice',
            'goal': f"Investigate the {product_id} failure the on-call agent could not restore",
            'instructions': (f"Filed by the {sm_name} scrum-master escalation for {product_id}: "
                             f"the on-call could not restore '{title}' ({pending.get('source')}). "
                             f"Spike to find the root cause."),
            'taskType': 'spike',
        }])
        outcome = 'spike'
    # The incident is now back in the pipeline (as a backlog item or a spike),
    # so this escalation is resolved; clear before restoring the SM so a fresh
    # restore failure can file a NEW escalation later.
    _escalatedProducts = state.get('_escalatedProducts') or {}
    record = _escalatedProducts.get(product_id)
    if record:
        record['resolved'] = True
        record['outcome'] = outcome
    state['_pendingEscalation'] = None
    _restore_escalation_agent(state, scrum_master_id, (pending.get('people') or {}).get(scrum_master_id), now_ms)
    _log_escalation_digest(state, pending, outcome)
    _log_governance(state, scrum_master_id, 'oncall_escalation',
                    {'action': 'groom_resolve', 'product': product_id, 'outcome': outcome})


def _restore_escalation_agent(state, aid, snap, now_ms):
    """Return the scrum master to their pre-escalation state. A mid-task SM
    resumes the same task with its work budget extended; an off-duty one returns
    off-duty (vanished). Mirrors _restore_refinement_agent."""
    agents = state.get('agents') or {}
    a = agents.get(aid)
    if not a or not snap:
        return
    a['dir'] = snap.get('dir'); a['x'] = snap.get('x'); a['y'] = snap.get('y')
    if snap.get('offDuty'):
        a['offDuty'] = True; a['visible'] = False
        a['task'] = None; a['busy'] = False; a['inRoom'] = None
        return
    if snap.get('task'):
        t = (state.get('tasks') or {}).get(snap['task'])
        a['task'] = snap['task']; a['busy'] = True; a['inRoom'] = snap.get('inRoom')
        a['visible'] = snap.get('visible')
        if t and t.get('status') in ('walking', 'working'):
            t['workUntil'] = (t.get('workUntil') or 0) + (ESCALATION_MEET_MS // 1000)
        return
    a['task'] = None; a['busy'] = False; a['inRoom'] = None
    a['visible'] = snap.get('visible'); a['offDuty'] = snap.get('offDuty')


def _log_escalation_digest(state, pending, outcome):
    """Write a short digest of an on-call escalation to library/escalations/ --
    the SM, the product, why it escalated, and the filed story/spike. Matches
    the refinement/social digest precedent (best-effort, never load-bearing)."""
    try:
        import serve
        serve.log_escalation_digest(state, pending, outcome)
    except Exception:
        pass


def _escalate_oncall_failure(state, product_id, director_id, title, source, now_ms):
    """Shared entry point for BOTH triggers: file a pending escalation to the
    product's owning team SM. Returns the SM id or None (no SM / capped / dup).
    Called from the assignment-abandonment hook and the unrestored sweep."""
    sm = _start_escalation(state, product_id, director_id, title, source, now_ms)
    return sm


def _escalation_step(state, now, now_ms, decider=None):
    """Ungated server-owned pass (like refinement/social/roadmap): (a) advance
    an in-flight escalation ceremony, (b) sweep open incident tasks older than
    the restore window and escalate the ones no one restored. Deterministic,
    state-only. Called from _task_cycle. `decider` injectable for tests
    (mirrors _refinement_step's threading)."""
    # (a) Advance the single in-flight escalation: embark then resolve.
    pend = state.get('_pendingEscalation')
    if pend is not None:
        if not pend.get('embarked'):
            _embark_escalation(state, pend.get('scrumMasterId'), now_ms)
        else:
            _resolve_escalation(state, pend, now_ms, decider=decider)
        return
    # (b) Sweep: any open incident task past its restore window escalates once.
    timeout = RESTORE_TIMEOUT_MS
    for tid, t in (state.get('tasks') or {}).items():
        if t.get('taskType') != 'bug':
            continue
        if t.get('status') not in ('walking', 'working'):
            continue
        # openedAt set at creation; fall back to a created/since marker.
        opened = t.get('openedAt') or t.get('assignedAt') or t.get('createdAt')
        if opened is None:
            continue
        if now_ms - opened < timeout:
            continue
        product_id = t.get('productId')
        if not product_id:
            continue
        # Resolve the owning team -> SM via the product.
        director_id = _product_director(state, product_id)
        if not director_id:
            continue
        _escalate_oncall_failure(state, product_id, director_id,
                                 t.get('title') or 'unrestored incident',
                                 'unrestored', now_ms)


def _product_director(state, product_id):
    """Owning team (director id) for a product, or None. Mirrors queue_bug's
    product.teamId lookup so the timeout sweep routes like the initial report."""
    prod = (state.get('products') or {}).get(product_id)
    if not prod:
        return None
    return prod.get('teamId')


def _governance_pass(state, now=None, now_ms=None, grid=None, decider=None):
    """Phase 4: ONE cadence pass over server-owned governance. Fires an auto-
    hire (hiring.js attemptAutoHire + finishHire) and an auto-firing review
    (firing.js attemptAutoFiringReview + finishFiringReview), gated on the
    same idle rule as everything else: no work, no governance, no Jev spend.
    Mutates `state` only; pure. Called from _task_cycle so it shares the
    single read-modify-write. `decider` injectable (defaults to Jev)."""
    sim = state.get('sim') or {}
    if sim.get('owner') != 'server':
        return state
    now = time.time() if now is None else now
    now_ms = int(now * 1000) if now_ms is None else now_ms
    # Idle-quiet: both browser loops bailed early on !thinkTankHasWork() and only
    # made their Jev call after. Mirror that so an idle think tank spends nothing.
    if not think_tank_has_work(state, now_ms):
        return state
    decider = decider or _governance_decider
    if grid is None:
        grid, _ = _load_outdoor_geometry()

    # All-time name reserve: backfill once from the roster so every pre-existing
    # name (including any a browser-client hired before this shipped) is
    # reserved forever, not only while that agent is employed.
    if not state.get('_usedNames'):
        for _d in (state.get('agentRoster') or []):
            _remember_name(state, _d.get('name'))

    # --- Auto-hire: complete a pending hire whose duration elapsed, then start
    # a new one if eligible.
    pending = state.get('_pendingHire')
    if pending and now_ms >= pending['at']:
        _complete_auto_hire(state, pending, grid, now_ms)
        pending = None
    if state.get('_pendingHire') is None:
        if now_ms - (state.get('lastHireAt') or 0) >= HIRE_COOLDOWN_MS:
            _start_auto_hire(state, now_ms, grid, decider)

    # --- Phase B onboard ceremony: a fresh hire with a _pendingOnboard has its
    # Town-Hall meeting start (director + coworkers convene at Command Center)
    # if not already underway; once the meet duration elapses, stage the new
    # agent's AGENT.md progressively until final.
    onboard = state.get('_pendingOnboard')
    if onboard is not None:
        if not onboard.get('embarked'):
            _start_onboard_meeting(state, onboard, now_ms)
        elif now_ms >= onboard.get('at', 0):
            _resolve_onboard_meeting(state, onboard, now_ms)

    # --- Auto-firing review: resolve a pending review whose duration elapsed,
    # then start a new one if due.
    pending_review = state.get('_pendingFiringReview')
    if pending_review and now_ms >= pending_review['at']:
        _resolve_firing_review(state, pending_review, now_ms, decider)
        pending_review = None
    if state.get('_pendingFiringReview') is None:
        if now_ms - (state.get('lastFiringReviewAt') or 0) >= FIRING_COOLDOWN_MS:
            _start_auto_firing_review(state, now_ms, decider)

    return state


def _start_auto_firing_review(state, now_ms, decider):
    """firing.js attemptAutoFiringReview: BOTH approvers (admin + senior-most
    director) must exist, be on-duty, and be free; then a real review starts --
    reviewers convene at Command Center and the candidate is looked at
    FIRING_REVIEW_DURATION_MS later by _resolve_firing_review."""
    reviewers = _firing_reviewers(state)
    if len(reviewers) < 2:
        return False
    agents = state.get('agents') or {}
    r1 = agents.get(reviewers[0]['id'])
    r2 = agents.get(reviewers[1]['id'])
    if not r1 or not r2 or r1.get('busy') or r2.get('busy') or r1.get('offDuty') or r2.get('offDuty'):
        return False
    candidate = who_needs_review(state, now_ms)
    if not candidate:
        return False
    state['lastFiringReviewAt'] = now_ms
    for r in (r1, r2):
        r['busy'] = True
        r['visible'] = False
        r['inRoom'] = 'commandcenter'
        r['dir'] = 'south'
    r1['roomX'] = 315
    r1['roomY'] = 155
    r2['roomX'] = 485
    r2['roomY'] = 155
    state['_pendingFiringReview'] = {
        'reviewer1Id': reviewers[0]['id'], 'reviewer2Id': reviewers[1]['id'],
        'candidateId': candidate.get('id'), 'at': now_ms + FIRING_REVIEW_DURATION_MS,
    }
    return True


def _resolve_firing_review(state, pending, now_ms, decider=None):
    """firing.js finishFiringReview: the Jev fire/keep verdict, the anti-
    premature consultation guard, and the delete-or-keep outcome. Idempotent
    on a candidate that vanished or became busy mid-review."""
    agents = state.get('agents') or {}
    reviewer1 = agents.get(pending['reviewer1Id'])
    reviewer2 = agents.get(pending['reviewer2Id'])
    for r in (reviewer1, reviewer2):
        if r:
            r['busy'] = False
            r['visible'] = True
            r['inRoom'] = None
    candidate = agents.get(pending['candidateId'])
    if not candidate:
        state.pop('_pendingFiringReview', None)
        return
    reviewers = [next((d for d in (state.get('agentRoster') or []) if d.get('id') == pending['reviewer1Id']), {}),
                 next((d for d in (state.get('agentRoster') or []) if d.get('id') == pending['reviewer2Id']), {})]
    candidate_def = next((d for d in (state.get('agentRoster') or []) if d.get('id') == pending['candidateId']), None)
    if not candidate_def:
        state.pop('_pendingFiringReview', None)
        return
    decision, morale = _fire_decision(state, reviewers, candidate_def, now_ms, decider)
    if decision == 'fire':
        blocks, info = _consultation_blocks_firing(state, pending['candidateId'], now_ms)
        if blocks:
            # Deferred for consultation -- don't mark reviewed so a real change
            # (collaboration ends, evidence grows) re-opens it.
            candidate['lastFiringReview'] = None
            reviewer1_id = pending['reviewer1Id']
            _log_governance(state, reviewer1_id, 'firing_review',
                            {'about': pending['candidateId'], 'decision': 'deferred_for_consultation',
                             'morale': morale,
                             'reviewers': [pending['reviewer1Id'], pending['reviewer2Id']],
                             'consulted': {'reporters': [r['id'] for r in info['reporters']],
                                           'coworkers': [c['id'] for c in info['coworkers']]}})
            state.pop('_pendingFiringReview', None)
            return
        # Same idle guard re-checked after the Jev await: don't delete someone
        # who got picked up by a task/pair/handoff while the review ran.
        if candidate.get('busy') or candidate.get('task') or candidate.get('pairWith') or candidate.get('handoff'):
            _log_governance(state, pending['reviewer1Id'], 'firing_review',
                            {'about': pending['candidateId'], 'decision': 'deferred',
                             'reason': 'candidate became busy mid-review'})
            state.pop('_pendingFiringReview', None)
            return
        # Fire: remove from both the live map and the roster.
        _remember_name(state, candidate_def.get('name'))
        agents.pop(pending['candidateId'], None)
        roster = state.get('agentRoster') or []
        state['agentRoster'] = [d for d in roster if d.get('id') != pending['candidateId']]
        # Durable blocklist: a fired employee can never reenter the think tank.
        # The id (and the name, via _usedNames) is reserved forever and the hire
        # gate refuses it, so no ceremony, generative name chooser, or re-seed
        # can bring the same person back under a new cover.
        state.setdefault('firedAgents', {})[pending['candidateId']] = {
            'name': candidate_def.get('name'),
            'role': candidate_def.get('role'),
            'at': now_ms,
            'firedBy': pending['reviewer1Id'],
            'reviewers': [pending['reviewer1Id'], pending['reviewer2Id']],
        }
        # Revoke every standing credential the fired agent held (capability
        # handles + attribution key + temp grants) -- a fired agent must lose
        # all of it, not just its roster entry.
        _revoke_agent_credentials(pending['candidateId'])
        _log_governance(state, pending['reviewer1Id'], 'firing_review',
                        {'about': pending['candidateId'], 'decision': 'fire', 'morale': morale,
                         'reviewers': [pending['reviewer1Id'], pending['reviewer2Id']]})
    else:
        # Keep: record the verdict on the agent so _review_is_stale skips re-
        # litigating this exact evidence until morale/report count move.
        candidate['lastFiringReview'] = {
            'morale': morale,
            'reportCount': len(reports_about(state, pending['candidateId'])),
            'verdict': 'keep',
            'at': now_ms,
        }
        _log_governance(state, pending['reviewer1Id'], 'firing_review',
                        {'about': pending['candidateId'], 'decision': 'keep', 'morale': morale,
                         'reviewers': [pending['reviewer1Id'], pending['reviewer2Id']]})
    state.pop('_pendingFiringReview', None)


def _log_governance(state, agent_id, action, details):
    """Append to the durable state's activity log, the same place the browser's
    logThinkTankAction wrote. No DB here -- state mutation only."""
    try:
        import serve
        serve.log_action(agent_id, action, details, authorized=True)
    except Exception:
        # Couldn't log (e.g. removing the logging agent); governance still
        # proceeds -- logging is best-effort, not load-bearing.
        pass


def _task_owner_director(state, task):
    """The director who owns the team working `task`, for budget-exhausted
    escalation. Candidate chain: the task's teamId -> its productId (via
    _product_director) -> the assigned agent's roster `director` pointer.
    Returns a director id (as state['agents'] keys it) or None."""
    if task.get('teamId'):
        team = _team_row(state, task['teamId'])
        if team:
            return team.get('directorId') or team.get('id')
    if task.get('productId'):
        director = _product_director(state, task.get('productId'))
        if director:
            return director
    roster = next((d for d in (state.get('agentRoster') or [])
                   if d.get('id') == task.get('assignedTo')), None)
    if roster and roster.get('director'):
        return roster['director']
    return None


def _notify_task_budget_exhausted(state, task, spent=None, attempts=None):
    """One-time director notification when a task's model-spend ceiling is hit
    (see the budgetExhausted content result). Fires once per task (stamped
    `_budgetExhaustedNotified`); the owning director gets a NON-DISRUPTIVE
    mailbox note (kind task_budget_exhausted, no wake -- the card is already
    closed, waking her to act would be churn) + a governance log row carrying
    the diagnostic (budget / actual spend / attempt count / author) so she can
    investigate and re-open the card with more budget. Pure state mutation."""
    if task.get('_budgetExhaustedNotified'):
        return
    task['_budgetExhaustedNotified'] = True
    director_id = _task_owner_director(state, task)
    title = task.get('title') or 'a task'
    budget = task.get('budgetUsd')
    budget_line = f" of ${budget:.2f}" if isinstance(budget, (int, float)) else ''
    spent_line = f" after spending ${spent:.4f}" if isinstance(spent, (int, float)) else ''
    attempts_line = f" across {int(attempts)} attempt(s)" if isinstance(attempts, (int, float)) else ''
    text = (f'The task "{title}" exhausted its model-spend budget{budget_line}'
            f"{spent_line}{attempts_line} and was closed as failed. Investigate the "
            f"task and either grant it more budget and re-open it, or close the work.")
    director = (state.get('agents') or {}).get(director_id) if director_id else None
    if director:
        _append_mailbox(director, {
            'kind': 'task_budget_exhausted',
            'taskId': task.get('id'),
            'title': title,
            'text': text,
        })
    _log_governance(state, director_id or task.get('assignedTo'),
                    'task_budget_exhausted',
                    {'taskId': task.get('id'), 'title': title[:120],
                     'budgetUsd': budget, 'spentUsd': spent,
                     'attempts': attempts, 'assignedTo': task.get('assignedTo')})


def _director_grant_budget_reopen(state, task_id, new_budget_usd, director_id=None, now_ms=None):
    """Item 4 runtime override: a director grants a budget-exhausted card more
    model-spend money and re-opens the work. Refuses unless the card is actually
    budget-failed (status 'failed' + budgetExhausted) and the new ceiling is a
    real raise past what was already spent -- otherwise the /api/chat gate would
    still refuse the very next call. Re-queues the card (same title/goal/room/
    product/instructions, pinned to its original author) carrying the raised
    budgetUsd, so the assignment loop works it again under the new ceiling. The
    failed record is kept and stamped with the raised budget (the audit trail);
    the re-opened card is a fresh task under the new budget. Pure state
    mutation; returns {'ok': ...}."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    tasks = state.get('tasks') or {}
    task = tasks.get(task_id) if task_id else None
    if not isinstance(task, dict):
        return {'ok': False, 'error': 'unknown task'}
    if task.get('status') != 'failed' or not task.get('budgetExhausted'):
        return {'ok': False, 'error': 'only a budget-exhausted (failed) card can be re-opened with more budget'}
    if not isinstance(new_budget_usd, (int, float)) or new_budget_usd <= 0:
        return {'ok': False, 'error': 'new budget must be a positive USD amount'}
    import serve as _serve_mod
    spent, attempts = _serve_mod._task_budget_spent(task_id)
    if float(new_budget_usd) <= spent:
        return {'ok': False,
                'error': f'new budget ${float(new_budget_usd):.4f} does not exceed the ${spent:.4f} already spent on this task -- the gate would refuse the next call anyway'}
    budget = float(new_budget_usd)
    # Audit trail on the failed record + the authoritative ceiling for the
    # re-opened card (queue_work whitelists budgetUsd onto the assigned task).
    task['budgetUsd'] = budget
    task['budgetGrantedAt'] = now_ms
    task['budgetGrantedBy'] = director_id
    fail_note = (task.get('failNote') or '').strip()
    queue_work(state, [{
        'title': task.get('title') or 'Re-opened task',
        'room': task.get('room'),
        'instructions': (f'This card was re-opened by {director_id or "the director"} with more model-spend budget '
                         f'(${budget:.2f} total) after exhausting its previous ceiling.'
                         + (f' Previous failure: {fail_note}' if fail_note else '')
                         + (f'\n\n{task.get("instructions")}' if task.get('instructions') else '')),
        'goal': task.get('projectLabel') or task.get('goal'),
        'projectLabel': task.get('projectLabel') or task.get('title'),
        'productId': task.get('productId'),
        'taskType': task.get('taskType') or 'code',
        'assignedTo': task.get('assignedTo'),  # pin back to the original author
        'budgetUsd': budget,
        'budgetBand': task.get('budgetBand') or 'standard',
        'userStory': task.get('userStory'),
        'acceptanceCriteria': task.get('acceptanceCriteria'),
    }])
    _log_governance(state, director_id or task.get('assignedTo'), 'task_budget_granted',
                    {'taskId': task_id, 'title': (task.get('title') or '')[:120],
                     'newBudgetUsd': budget, 'spentUsd': spent, 'attempts': attempts})
    return {'ok': True, 'taskId': task_id, 'budgetUsd': budget,
            'spentUsd': spent, 'attempts': attempts}


def _revoke_agent_credentials(agent_id):
    """Best-effort revocation of EVERY standing credential a fired agent held:
    the external-capability handles (Phase D), the per-agent attribution secret
    (agent_keys -- else a fired agent could keep signing requests it was never
    re-authorized for), and any temporary capability grants (temp_access_grants).
    Mirrors _log_governance's lazy serve import so sim.py stays unit-testable
    offline; a DB failure must never block the fire itself."""
    try:
        import serve
        serve.revoke_agent_credentials(agent_id)
    except Exception:
        pass


def _on_work_item_abandoned(state, pick, now_ms):
    """Trigger 1 for the Cut-3 escalation: a work item was permanently abandoned
    (assignment-attempt cap reached). If it's an incident/bug, hand the failed
    restore to the product's owning team scrum master so it becomes a backlog
    story/spike instead of dying silently."""
    if not (pick.get('taskType') == 'bug' or pick.get('incident')):
        return
    product_id = pick.get('productId')
    if not product_id:
        return
    director_id = _product_director(state, product_id)
    if not director_id:
        return
    _escalate_oncall_failure(state, product_id, director_id,
                             pick.get('title') or 'abandoned incident',
                             'assignment_abandoned', now_ms)


def _stale_work_step(state, now, now_ms):
    """SM gap 1: stale in-flight work oversight. Bugs already have a restore
    alarm (RESTORE_TIMEOUT_MS via _escalation_step); a NON-bug task wedged in
    'walking'/'working' had NO age alarm -- if the completion loop and the
    orphan/stall repairs all miss it, it holds its worker forever.

    A coarse-cadence sweep finds tasks whose in-flight age is definitively past
    any legitimate budget:
      - 'working': still 'working' long after workUntil elapsed (the completion
        loop finishes every held working task within a pass, so still-working at
        workUntil + grace means the wedge won self-resolve);
      - 'walking': walking for longer than a generous ceiling (never arrived).
    A first staleness re-queues the card fresh (fast re-issue, priority bump,
    holder released). A repeat offender -- re-planned STALE_WORK_MAX_REPLANS
    times for the same (title, room) -- is routed to the owning team's scrum
    master as a work-request (the "SM re-plan"): refinement's accept/reject
    decides whether the work survives, so the loop is bounded and never spins.
    The replan counter lives in `_staleWorkReplans` keyed by a JSON-safe repr
    of (title, room): the map is part of kv_state, which is serialized on every
    save, so a tuple key would make the whole save fail.
    Never touches bugs (own alarm), shadow dry-runs, or review/fix subtasks
    (their deadlock lives in the stuck-gate watchdog). Pure state mutation;
    runs before the idle gate so a quiet tank still re-plans a wedged card.
    Returns the number of cards re-planned (requeued + SM-routed)."""
    if now_ms - (state.get('lastStaleWorkSweep') or 0) < STALE_WORK_CADENCE_MS:
        return 0
    state['lastStaleWorkSweep'] = now_ms
    tasks = state.get('tasks')
    if not isinstance(tasks, dict) or not tasks:
        return 0
    agents = state.get('agents') or {}
    replan_map = state.setdefault('_staleWorkReplans', {})
    replanned = 0
    for tid, task in list(tasks.items()):
        if not isinstance(task, dict):
            continue
        if task.get('status') not in ('walking', 'working'):
            continue
        if task.get('taskType') == 'bug' or task.get('incident') or task.get('shadow'):
            continue  # bugs own an alarm; shadows never ship; reviews -> stuck-gate
        if task.get('reviewOf'):
            continue  # review/fix subtasks are the stuck-gate watchdog's domain
        if task.get('_suspendedForScheduled'):
            continue  # parked for a scheduled item -- budget restored on resume
        opened = task.get('openedAt') or task.get('assignedAt') or task.get('createdAt')
        if opened is None:
            continue
        if task.get('status') == 'walking':
            if now_ms - opened < STALE_WORK_TIMEOUT_MS:
                continue
        else:  # 'working'
            work_until = task.get('workUntil') or 0
            if now <= work_until + STALE_WORK_BUDGET_GRACE_S:
                continue
        # Genuinely wedged non-bug in-flight card. Release any holder so the
        # worker is never pinned by a task that will no longer resolve.
        for aid, a in list(agents.items()):
            if isinstance(a, dict) and a.get('task') == tid:
                a['task'] = None
                a['busy'] = False
                a['inRoom'] = None
                break
        key = repr((task.get('title') or '', task.get('room') or ''))
        replans = replan_map.get(key, 0)
        if replans < STALE_WORK_MAX_REPLANS:
            replan_map[key] = replans + 1
            _requeue_stale_work(state, task)
            _log_governance(state, 'admin', 'stale_work_requeued',
                            {'task': tid, 'title': task.get('title')[:120],
                             'room': task.get('room'), 'replans': replans + 1})
        else:
            # Terminal re-plan: hand to the owning team's scrum master.
            if _sm_replan_stale_work(state, task, now_ms):
                _log_governance(state, 'admin', 'stale_work_sm_replan',
                                {'task': tid, 'title': task.get('title')[:120],
                                 'room': task.get('room'), 'replans': replans + 1})
            else:
                _log_governance(state, 'admin', 'stale_work_dropped',
                                {'task': tid, 'title': task.get('title')[:120],
                                 'room': task.get('room')})
        del tasks[tid]
        replanned += 1
    return replanned


def _requeue_stale_work(state, task):
    """Fast-path re-plan: put the swept card back on the work queue so a fresh
    assignment re-issues it, bumped to at least 'high' priority (it already
    waited long enough once) and marked so a future sweep can find it fast."""
    room = task.get('room') or _resolve_assignment_room(task)
    queue_work(state, [{
        'title': task.get('title'),
        'room': room,
        'instructions': (
            (task.get('instructions') or '') +
            '\n\n[re-planned] This card was swept as stale in-flight work and re-issued '
            'so it is not lost.'
            if task.get('instructions') else
            f"Re-issued stale work: {task.get('title')}"
        ),
        'goal': task.get('goal'),
        'taskType': task.get('taskType') or 'code',
        'sizeEstimate': task.get('sizeEstimate'),
        'teamId': task.get('teamId'),
        'productId': task.get('productId'),
        'featureId': task.get('featureId'),
        'backlogItemId': task.get('backlogItemId'),
        'sprintId': task.get('sprintId'),
        'priority': WORK_PRIORITY['high'],
    }])


def _sm_replan_stale_work(state, task, now_ms):
    """Terminal re-plan: route a repeatedly-stale card to the owning team's
    scrum master as a work-request, and make refinement due on the next pass so
    the SM re-plans it promptly (accept -> new story, reject -> dropped, both
    bounded). Returns True when the request was filed for a real team."""
    room = task.get('room') or _resolve_assignment_room(task)
    if room not in VALUED_QUEUE_ROOMS:
        return False
    team_id = task.get('teamId')
    if not team_id and task.get('productId'):
        team_id = _product_director(state, task.get('productId'))
    team = _team_row(state, team_id) if team_id else None
    if not team:
        return False
    # Attribute the re-plan request to the owning team's SM (or its director),
    # so _refinement_team resolves it back to this team for grooming.
    filer = _escalation_scrum_master_for(state, team.get('directorId') or team.get('id'))
    if not filer:
        filer = team.get('directorId') or team.get('id')
    reason = ('Swept as stale in-flight work after repeated re-issues; the scrum '
              'master decides whether this still needs doing. '
              'ABS_REPLAN: this card no longer resolves on its own.')
    if not file_work_request(state, filer, (task.get('title') or 'stale work')[:120],
                             room, reason=reason):
        return False
    kick_refinement_now(state, team.get('directorId') or team.get('id'), now_ms)
    return True


def _sm_help_stuck_worker(state, task, now_ms):
    """W4: route a worker-stuck help signal to the owning team's scrum master.
    A worker whose content execution keeps FAILING is genuinely stuck; the SM
    gets a work-request so they can help/re-plan (accept -> new story, reject ->
    dropped, both bounded). Mirrors _sm_replan_stale_work's routing; returns
    True when the request was filed for a real team."""
    room = task.get('room') or _resolve_assignment_room(task)
    if room not in VALUED_QUEUE_ROOMS:
        return False
    team_id = task.get('teamId')
    if not team_id and task.get('productId'):
        team_id = _product_director(state, task.get('productId'))
    team = _team_row(state, team_id) if team_id else None
    if not team:
        return False
    filer = _escalation_scrum_master_for(state, team.get('directorId') or team.get('id'))
    if not filer:
        filer = team.get('directorId') or team.get('id')
    reason = ('A worker is stuck: repeated content-execution failures on this card. '
              'The scrum master decides whether this still needs doing, or whether '
              'the worker needs help / a different assignment.')
    if not file_work_request(state, filer, (task.get('title') or 'stuck work')[:120],
                             room, reason=reason):
        return False
    kick_refinement_now(state, team.get('directorId') or team.get('id'), now_ms)
    return True


def _worker_stuck_help_signal(state, agent_id, task, result_ok, now_ms):
    """W4: worker stuck/help signal. Tracks CONSECUTIVE content-execution
    failures per worker (a content result with ok=False -- a crash, or a red
    pipeline). A clean result resets the streak. Once the streak crosses
    WORKER_HELP_AFTER_FAILS, route a help signal to the owning team's scrum
    master (_sm_help_stuck_worker) and log governance, then reset the streak so
    the signal fires once per consecutive-failure run (the SM request dedups
    anyway, so no spam). Pure state mutation; called from the content-result
    branch of _task_cycle."""
    if result_ok:
        a = (state.get('agents') or {}).get(agent_id)
        if a:
            a['_contentFailStreak'] = 0
        return
    a = (state.get('agents') or {}).get(agent_id)
    if not a:
        return
    streak = a.get('_contentFailStreak', 0) + 1
    a['_contentFailStreak'] = streak
    if streak < WORKER_HELP_AFTER_FAILS:
        return
    a['_contentFailStreak'] = 0  # re-arm only after a fresh clean result
    routed = _sm_help_stuck_worker(state, task, now_ms)
    _log_governance(state, 'admin', 'worker_stuck_help',
                    {'agent': agent_id, 'task': task.get('id'),
                     'title': (task.get('title') or '')[:120],
                     'failures': streak, 'routed': routed})


def _coaching_loop_step(state, now_ms):
    """W5: daily coaching catch-up sweep. _coach_low_grade only fires when a
    low-graded deliverable actually lands; a worker who then goes quiet could
    absorb one coaching note and never improve, with the loop never checking
    back. On a coarse cadence, this sweep re-checks every agent who HAS a
    coaching plan (low_grade or team_health) applied: if their real trailing
    grade is STILL below the floor, the coaching hasn't landed. Re-coaches
    (round-aware, same cap as _coach_low_grade) or escalates through the same
    bounded path -- a worker can't silently absorb an ignored plan forever.
    Bounded: the cap limits rounds, and escalation dedups via the SM work-
    request, so the sweep can't spin. Pure state mutation; runs before the
    idle gate on the cadence, no-op otherwise."""
    if now_ms - (state.get('lastCoachingLoop') or 0) < COACHING_LOOP_CADENCE_MS:
        return 0
    state['lastCoachingLoop'] = now_ms
    plans = state.get('growthPlans') or {}
    checked = 0
    for agent_id, agent_plans in list(plans.items()):
        if not isinstance(agent_plans, list):
            continue
        if not any(p.get('kind') in ('low_grade', 'team_health') for p in agent_plans):
            continue
        grade = _agent_trailing_grade(state, agent_id)
        if grade is None or grade >= DELIVERABLE_GRADE_FLOOR:
            continue
        a = (state.get('agents') or {}).get(agent_id) or {}
        room = (a.get('room') or 'pressoffice') if isinstance(a.get('room'), str) else 'pressoffice'
        if room not in VALUED_QUEUE_ROOMS:
            room = 'pressoffice'
        _coach_low_grade(state, agent_id, room, grade, now_ms, 'follow-up review')
        checked += 1
    return checked


def _task_cycle(state, now=None, grid=None, doors=None, task_id_holder=None):
    """The server-side heart of Phase 3 slice 1: one consumption pass over the
    persistent workQueue, mirroring the browser's runTaskCycleBody (tasks.js:350),
    PLUS the completion/off-duty ISSUING that the browser's finishTask/
    sendAgentOffDuty handle. Pure: mutates `state`, no DB, no serve import at
    call site (imports serve lazily only for logging drive).

    ARRIVAL is handled by SimEngine.tick (movement): when step_agent_movement
    emits ('arrive','task',aid) that tick calls `_arrive_at_task` (marks busy/
    inRoom/working, sets the work budget). (Off-duty is immediate and in-place --
    send_agent_off_duty -- so there is no off-duty ARRIVAL event.) Movement and
    lifecycle run on SEPARATE threads (sim loop 2s, task loop 6s), so this pass
    never sees the walk itself -- only its post-arrival state (task.status==
    'working' + workUntil set). This pass: schedules standing work,
    completes tasks whose budget elapsed, issues off-duty walks after completion,
    and consumes the queue.

    Returns state. Idle gate: no due work AND nobody active -> returns with
    zero mutations (zero spend, mirroring the JS live-return)."""
    now = time.time() if now is None else now
    now_ms = int(now * 1000)
    sim = state.get('sim') or {}
    if sim.get('owner') != 'server':
        return state  # browser still drives; stay inert (revert = set owner back)

    agents = state.get('agents') or {}
    _check_schedules(state, now, now_ms)
    # Phase 4: server-owned governance (auto-hire + auto-firing review) runs in
    # the same single read-modify-write, gated on the server-owner + idle rule
    # inside _governance_pass itself.
    _governance_pass(state, now=now, now_ms=now_ms, grid=grid)

    # Phase E3.1: stuck-at-review escalation watchdog, on its own coarse cadence.
    # A gated story whose locked reviewer pair can't advance is re-picked to a
    # REACHABLE pair so the gate always has a viable vote path. Bounded internals
    # (grace + rescue cooldown) live inside _sweep_stuck_gates.
    if now_ms - (state.get('lastStuckGateSweep') or 0) >= STUCK_GATE_CADENCE_MS:
        state['lastStuckGateSweep'] = now_ms
        _sweep_stuck_gates(state, now_ms)

    # Bound the append-only tails of the kv_state blob (completedDeliverables,
    # resolved backlogRequests, answered playerInbox, closed issues, applied
    # growthPlans) on a coarse hourly cadence so the 1s autosave write doesn't
    # grow without bound. Live/open/pending entries are always kept.
    if now_ms - (state.get('lastStatePrune') or 0) >= _STATE_PRUNE_CADENCE_MS:
        state['lastStatePrune'] = now_ms
        _prune_state_blob(state, now_ms)

    # Reclaim a 'walking'/'working' task whose assignee no longer
    # holds it (the orphaned-fix wedge). Runs BEFORE the idle gate so a quiet
    # think tank that simply has a stale in-flight task re-issues it rather than
    # treating the think tank as done. Cheap: no-op unless such a task exists.
    _reclaim_orphaned_walking_tasks(state)

    # SM gap 1: age-based stale in-flight work sweep. A held non-bug card
    # wedged past any legitimate budget (walking forever, or still working long
    # after workUntil) is re-queued once, then routed to the owning scrum master
    # to re-plan. Runs before the idle gate on a coarse cadence (no-op unless a
    # card is genuinely wedged).
    _stale_work_step(state, now, now_ms)

    # W5: daily coaching catch-up sweep. Re-checks agents with an applied
    # coaching plan whose real trailing grade is still below the floor, and
    # re-coaches/escalates (bounded) -- coaching can't be absorbed once and
    # ignored forever. Runs before the idle gate on a coarse cadence (no-op
    # unless someone is still under the floor).
    _coaching_loop_step(state, now_ms)

    # Backlog refinement: the scrum master grooms agent-filed work-requests into
    # real stories at the Command Center. EVENT-DRIVEN (no cadence): convenes
    # whenever a team has pending requests, a free+on-duty facilitator, and no
    # active sprint. Runs ungated like the other ceremonies (the grooming is the
    # point even on a quiet think tank) but no-ops when there's nothing to groom
    # or no facilitator to run it.
    _refinement_step(state, now, now_ms)

    # Sprint staffing: the breakdown ceremony -- a staffable large ask filed by
    # the free authority is carded into stories/spikes by the receiving team's
    # scrum master + workers. Runs ungated like its ceremony siblings (a staffed
    # large ask is the point even on a quiet think tank); no-ops with nothing
    # pending or no scrum master to run it, and defers a team in an active
    # sprint until it's free.
    _breakdown_step(state, now, now_ms)

    # WS-14: sprint retrospectives -- a just-closed sprint's team meets to capture
    # START / STOP / CONTINUE (director excluded) on a later pass. Runs ungated
    # like the Social / refinement, so a closed sprint never strands its retro.
    _retro_step(state, now, now_ms)

    # Cut 2 roadmap: weekly silent recompute of per-room priority from trailing
    # deliverable grades + delivery volume. Cheap (state-only, no ceremony, no
    # Jev spend); runs before the gate so a quiet think tank still keeps its
    # roadmap current for the next grooming.
    _roadmap_step(state, now_ms)

    # Consensus relay: weekly silent update of the one-line 'current direction'
    # the next cycle reads first (auto-co-meta's read -> act -> update -> repeat).
    # Derives from the roadmap just recomputed; runs after it so the relay is
    # always the LATEST consensus, not a stale one.
    _consensus_relay_step(state, now_ms)

    # Weekly rule mining: recurring classified failures become operator rule
    # proposals. Same shape as the roadmap step -- state stamp + file-backed
    # derivation, no ceremony, no Jev spend.
    _rule_mine_step(state, now_ms)

    # Weekly archive distill: fold the archived decision tape into the wiki so
    # the think tank's institutional memory compounds instead of being pruned.
    _archive_distill_step(state, now_ms)

    # Weekly success mining: recurring high-graded deliverables become operator
    # success-lesson proposals (the positive mirror of rule mining -- propagate
    # what worked, not just what failed). Same cadence/shape as _rule_mine_step.
    _success_mine_step(state, now_ms)

    # Cut 3 on-call escalation: advance an in-flight escalation ceremony, or
    # sweep open incident tasks past the restore window and hand the ones no
    # one could restore to the product team's scrum master. Ungated like its
    # ceremony siblings -- an unrecovered outage is the point on an idle think tank.
    _escalation_step(state, now, now_ms)

    # SM-committed `blocked` field: the scrum master flips issue['blocked'] for
    # any queued block-change (requirements-met / stuck-on-player / dependency).
    # Mechanical (no LLM, no meeting) -- all judgment is upstream. One per pass.
    _block_step(state, now, now_ms)

    # Wake-on-mail: route an off-duty agent woken by action-needed mail to the
    # work it references, so she acts then returns offline normally (instead of
    # being parked-idle in this same tick before she can process it).
    _mail_action_step(state, now, now_ms)

    work_queue = state.get('workQueue')
    if not isinstance(work_queue, list):
        work_queue = state['workQueue'] = []
    # Repair stale busy tokens BEFORE the idle gate short-circuits.
    # A `busy` flag is a hold on an IN-FLIGHT ('working') task; if the referenced
    # task already ended (done / needs_review) or is missing, or the agent is
    # busy with no task at all, the hold is stale and -- left alone -- wedges the
    # agent permanently (the sim's read-side invariant in the snapshot builder
    # only HIDES this; it must be repaired, or the roster bleeds idle workers over
    # time). Server restarts mid-flight + task-completion-out-of-band are the
    # usual culprits: an agent's busy persists in the DB while its task's status
    # moved to done independently. Clearing a non-'working' hold is safe: a
    # 'working' task is the only legitimate reason to be busy.
    for aid, a in list(agents.items()):
        if not isinstance(a, dict) or not a.get('busy'):
            continue
        tid = a.get('task')
        tasks = state.get('tasks') or {}
        # W1: a PAIR NAVIGATOR is busy with no task of her own -- she rides
        # along on the driver's (pairTaskId). Clearing her busy as a stale
        # token would yank her out of a live pair session mid-work. Only a
        # true stale hold (busy referencing a gone/non-working task, or busy
        # with no task AND no pair session) is a legitimate repair target.
        nav_tid = a.get('pairTaskId')
        if (nav_tid and not a.get('task')
                and (tasks.get(nav_tid) or {}).get('status') == 'working'):
            continue  # a paired navigator mid-session -- leave the hold alone
        if tid and (tasks.get(tid) or {}).get('status') in ('working',):
            continue  # legitimately mid-task -- leave the hold alone
        a['busy'] = False
        a['task'] = None
        a['inRoom'] = None
    # Park fully-idle on-duty wanderers once the queue has no work
    # to hand them. Nothing queued (or only not-yet-due work) + nobody active is
    # exactly the residue case: a woken-but-never-assigned agent lingers visible
    # with nothing to do, and the player just asked that only scheduled/active
    # agents appear. Runs BEFORE the idle gate so a quiet think tank still re-parking
    # its wanderers converges to zero visible idle sprites (assignment re-wakes
    # on demand, so this cannot deadlock the queue).
    if not work_queue:
        _park_idle_wanderers(state)
    if not work_queue and not _any_agent_active(agents):
        # Nothing queued and nobody's mid-task: idle, don't even scan.
        return state
    if not think_tank_has_work(state, now_ms):
        return state

    # Load geometry once per call for this pass (cheap; the movement tick has
    # its own cached copy via self in SimEngine.tick).
    if grid is None:
        grid, doors = _load_outdoor_geometry()
    if task_id_holder is None:
        task_id_holder = _TASK_ID_HOLDER

    # Completion first (a task that finishes frees a worker the assignment loop
    # below can then use). Arrival is guaranteed because movement set
    # status='working' + workUntil; a 'walking' task just keeps walking.
    # After completion the agent goes off duty in place (send_agent_off_duty).
    for aid, a in list(agents.items()):
        if not isinstance(a, dict):
            continue
        if a.get('task'):
            task = (state.get('tasks') or {}).get(a['task'])
            if not task or task.get('status') != 'working' or not task.get('workUntil'):
                continue
            # Phase 3 slice 2: if real content work is in flight (research),
            # merge its completed result (note + seenUrls) as soon as it lands,
            # finalizing before the timeout; if it already exhausted the budget,
            # fall through to the fallback completion. If a content result
            # arrived, turn OFF the early-no-wait so the note is real.
            result = _take_content_result(task['id']) if task.get('_contentInFlight') else None
            if result is not None:
                # Content landed -- mirror the JS Promise.race: finalize as soon
                # as the real work resolves (the min-visual floor is satisfied by
                # the network round-trips), not when the timeout ceiling hits.
                _apply_content_result(state, task, result, now_ms)
                # Bot Ops / shadow mode: a dry-run task ships nothing. Capture the
                # outcome to the shadow ledger and release the agent, skipping the
                # peer gate / quality gate / credits entirely.
                if task.get('shadow'):
                    _complete_shadow_task(state, aid, task, now_ms, grid)
                    send_agent_off_duty(state, aid, doors, grid)
                    continue
                # Item 4: FAIL-CLOSED budget-exhausted branch. The /api/chat gate
                # (serve.py) refuses the task's model calls once its ledger spend
                # crosses the ceiling and folds that refusal into its content
                # result (budgetExhausted + taskSpendUsd/taskSpendAttempts). Here
                # the card is closed as 'failed' -- NOT sent back / re-fixed: a fix
                # would re-hit the same empty budget and loop forever. The agent is
                # released INLINE (a budget-exhausted card must not bump approvals
                # or ship anything), the owning director is notified once with the
                # diagnostic, and the card waits for her to re-open it with more
                # budget or close the work. Never touches a review/fix subtask's
                # parent -- the gate refuses the task whose budget ran out.
                if result.get('budgetExhausted'):
                    task['budgetExhausted'] = True
                    task['status'] = 'failed'
                    task['failedAt'] = now_ms
                    task['failNote'] = (result.get('note') or task.get('note')
                                        or 'task model-spend budget exhausted').strip()[:300]
                    a['task'] = None
                    a['busy'] = False
                    a['inRoom'] = None
                    a['visible'] = True
                    _notify_task_budget_exhausted(
                        state, task,
                        spent=result.get('taskSpendUsd'),
                        attempts=result.get('taskSpendAttempts'))
                    send_agent_off_duty(state, aid, doors, grid)
                    continue
                # Fail-closed quality gate: a content result that FAILED the
                # quality pipeline (the coding executor stores ok=False on a red
                # flake8/mypy/bandit/pytest-cov run) must NEVER advance to peer
                # review or re-arm a gate. A review executor always reports
                # ok=True (its verdict rides in peerVerdict), so this check never
                # disturbs a normal gate vote.
                result_ok = bool(result.get('ok', True))
                # W4: worker stuck/help signal. A content result with ok=False
                # (a content-executor crash, or a red pipeline) is a FAILURE --
                # track consecutive failures per worker and route a help signal
                # to the owning team's scrum master once the streak crosses the
                # threshold, so a wedged worker is surfaced, not silently
                # churning. Runs for every landed content result (reviews always
                # report ok=True, so they never trip it).
                _worker_stuck_help_signal(state, aid, task, result_ok, now_ms)
                # Completion evidence is mandatory for ALL content work: a result
                # that reports success but carries NO completion note (no evidence
                # of what was actually done, or where it lives) is not completed.
                # The card goes back to its author as a gap (see
                # _send_back_after_failure) -- never a silent empty 'done'. Review
                # subtasks are exempt (their verdict folds in during
                # _apply_content_result and an empty note there is not a missing
                # deliverable).
                if result_ok and not (task.get('note') or '').strip() \
                        and task.get('_contentInFlight') and not task.get('reviewOf'):
                    _release_agent_after_failure(state, aid, task)
                    _send_back_after_failure(
                        state, task,
                        fail_note='missing completion evidence: the work reported success but produced no note of what was done or where it lives')
                    send_agent_off_duty(state, aid, doors, grid)
                    continue
                # Phase E addendum: completion is gated for deliverable rooms.
                # A review/fix subtask (has reviewOf) folds its vote already in
                # _apply_content_result; a primary deliverable task that has NEVER
                # been gated enters the peer gate instead of completing.
                if task.get('reviewOf'):
                    parent = _resolve_review_parent(state, task)
                    if parent is not None and task.get('taskType') == 'review':
                        # Vote worker finished; close the parent if 2 clean votes
                        # (or 1 + timeout) are in.
                        _parent_close_from_vote(state, parent, now_ms)
                    elif parent is not None and _peer_gated_lane(parent) and result_ok:
                        # A fix subtask finished its (re)work and the pipeline is
                        # clean -> re-arm the same gate so the fixed story gets
                        # re-review, not a done. Never re-arms a FROZEN (already
                        # escalated) gate -- an escalated story stays paused for
                        # manual attention, not silently re-entered.
                        if not (parent.get('_peerGate') or {}).get('escalated'):
                            _enter_peer_review(state, parent, now_ms)
                    elif parent is not None and _peer_gated_lane(parent):
                        # Fail-closed: the FIX itself failed the pipeline. Do NOT
                        # re-arm the gate; the story stays 'failed' and another
                        # fix is queued back to the author (or it escalates once
                        # the shared cycle cap is hit).
                        _release_agent_after_failure(state, aid, task)
                        _send_back_after_failure(state, parent, fail_note=task.get('note'))
                        send_agent_off_duty(state, aid, doors, grid)
                        continue
                    elif parent is not None:
                        # Parent isn't supposed to be gated (skillReview/distill)
                        # but somehow already has a stale _peerGate -- clear it
                        # instead of perpetuating the loop, and let it close
                        # normally next pass.
                        parent['_peerGate'] = None
                        if parent.get('status') == 'needs_review':
                            parent['status'] = 'working'
                    finish_task(state, aid, grid)
                    send_agent_off_duty(state, aid, doors, grid)
                elif _peer_gated_lane(task) and not task.get('_peerGate') and result_ok:
                    gate = _enter_peer_review(state, task, now_ms)
                    if gate:
                        # Story now waits on peer approval; release the author
                        # but do NOT mark it done.
                        _release_agent_gated(state, aid, grid)
                        send_agent_off_duty(state, aid, doors, grid)
                    else:
                        finish_task(state, aid, grid)
                        send_agent_off_duty(state, aid, doors, grid)
                elif _peer_gated_lane(task) and not task.get('_peerGate'):
                    # Fail-closed: a red-pipeline primary deliverable must NOT
                    # enter the peer gate. Release the agent (no approval/grade)
                    # and send the story back -- or escalate once the shared
                    # cycle cap is hit. (_send_back_after_failure notifies the
                    # author.)
                    _release_agent_after_failure(state, aid, task)
                    _send_back_after_failure(state, task, fail_note=task.get('note'))
                    send_agent_off_duty(state, aid, doors, grid)
                else:
                    finish_task(state, aid, grid)
                    send_agent_off_duty(state, aid, doors, grid)
            elif now >= task['workUntil']:
                # No content result within the budget (timeout OR the slice-1
                # placeholder) -- fallback completion. Same gate branching.
                # Bot Ops / shadow mode: capture the dry-run outcome to the shadow
                # ledger and release, never entering a gate.
                if task.get('shadow'):
                    _complete_shadow_task(state, aid, task, now_ms, grid)
                    send_agent_off_duty(state, aid, doors, grid)
                    continue
                if task.get('reviewOf') or not _peer_gated_lane(task):
                    finish_task(state, aid, grid)
                    send_agent_off_duty(state, aid, doors, grid)
                else:
                    gate = _enter_peer_review(state, task, now_ms)
                    if gate:
                        _release_agent_gated(state, aid, grid)
                        send_agent_off_duty(state, aid, doors, grid)
                    else:
                        finish_task(state, aid, grid)
                        send_agent_off_duty(state, aid, doors, grid)
        # (Off-duty walks that already ARRIVED were finalized by the movement
        # tick's ('arrive','offduty') dispatch; nothing left to do here.)

    # A sprint whose every item is done AND approved closes itself --
    # no need for the player to tap close once the whole sprint has landed.
    _auto_close_completed_sprints(state, now_ms)

    # Sprint staffing expansion: an active sprint below the six-worker cap whose
    # cards are queued while every pool member is busy grows its staff by one
    # (the director's "agents need help" signal, deterministic -- see
    # _sprint_staffing_step). Runs before assignment so the grown pool can take
    # cards this same pass.
    _sprint_staffing_step(state, now_ms)

    # Assignment loop (bounded by roster size, like the JS runTaskCycleBody).
    roster = state.get('agentRoster') or []
    roster_size = sum(1 for d in roster if not d.get('isAdmin'))
    attempted = set()
    for _ in range(max(1, roster_size)):
        due_index = pick_next_due_index(work_queue, now_ms, attempted, state)
        if due_index == -1:
            break
        pick = work_queue[due_index]
        attempted.add(id(pick))
        # A peer-approval review/fix subtask is pinned to a specific agent (its
        # reviewer/author) and must reach them even when the generic wake rule
        # (wake only enough agents to keep one awake-idle) would skip the second
        # of a pair of off-duty reviewers. So a reviewOf item is ALWAYS
        # wakeable -- its pinned agent is woken inside _assign_due_item.
        # Sprint staffing: a sprint card is likewise always wakeable -- its pool
        # is a specific set (see _sprint_worker_pool), and off-duty pool members
        # must be woken to staff the sprint even when the generic wake rule
        # would otherwise keep the think tank thin.
        _pinned_review = bool(pick.get('reviewOf') or pick.get('sprintId'))
        can_wake_off_duty = (_pinned_review or bool(pick.get('notBefore')) or _awake_idle_count(state) == 0) \
            and can_activate_another(state)
        # A PLAYER card needs no awake agent at all -- the assign step delivers
        # it into the player's inbox. Only the agent-work guard below can stop
        # it, so the wake-guard break must never fire for one.
        preempted_for_scheduled = None
        if not (pick.get('assignedTo') or '') == 'player' \
                and _awake_idle_count(state) == 0 \
                and not (can_wake_off_duty and _any_available_including_off_duty(state)):
            # Interrupt a busy agent so this due item can land. A SCHEDULED item
            # (queue_once notBefore) must still fire at its specified time even
            # when every agent is busy -- typically at the active ceiling, where
            # can_activate_another is False so no off-duty agent can be woken.
            # A ONE-OFF may also interrupt -- but ONLY ordinary/sprint work:
            # skip_time_critical refuses to yank an agent off a scheduled/
            # standing (time-critical) card (see _task_is_time_critical). In
            # both cases the parked task resumes when the interrupting item
            # completes (see _suspend_busy_agent_for_scheduled /
            # send_agent_off_duty).
            skip_time_critical = not bool(pick.get('notBefore'))
            preempted_for_scheduled = _suspend_busy_agent_for_scheduled(
                state, grid, doors, now_ms, skip_time_critical=skip_time_critical)
            if preempted_for_scheduled is None:
                break  # nobody who could take this right now, of any kind
        work_queue.pop(due_index)
        assigned = _assign_due_item(state, pick, can_wake_off_duty, grid, doors, now_ms, task_id_holder)
        if not assigned:
            # A preemption that didn't land: put the parked task straight back so
            # an agent is never left floating free with a suspended task hanging.
            if preempted_for_scheduled is not None:
                a2 = (state.get('agents') or {}).get(preempted_for_scheduled)
                if isinstance(a2, dict) and a2.get('_suspendedTask'):
                    _resume_suspended_task(state, preempted_for_scheduled, a2, grid)
            pick['attempts'] = (pick.get('attempts') or 0) + 1
            # A peer-approval review/fix subtask BLOCKS its whole story: never
            # abandon it on the normal attempt cap (which exists to shed stray
            # normal work). It retries until a reviewer is assignable; the
            # needs_review timeout is the safety net if a reviewer never can be.
            # Sprint staffing: a sprint card is STRICTLY staffed -- it waits
            # (forever, if need be) for a member of its chosen pool to free up
            # and is never shed to the think-tank-wide fallback or abandoned.
            # Human-in-the-loop: a PLAYER card is likewise never abandoned --
            # it waits for the player, not for an agent, and the delivery path
            # has no agent-availability failure to shed it for.
            if pick.get('reviewOf') or pick.get('sprintId') \
                    or (pick.get('assignedTo') or '') == 'player' \
                    or pick['attempts'] < WORK_ITEM_MAX_ATTEMPTS:
                work_queue.append(pick)
            else:
                from serve import log_action
                log_action(None, 'work_item_abandoned',
                           {'title': pick.get('title'), 'room': pick.get('room'),
                            'attempts': pick['attempts']}, authorized=True)
                _on_work_item_abandoned(state, pick, now_ms)
    return state


def _any_agent_active(agents):
    for a in agents.values():
        if a and (a.get('task') or a.get('handoff') or a.get('pairWith') or a.get('busy')):
            return True
    return False
def _park_idle_wanderers(state):
    """An agent is only woken by task assignment or the waking
    intents -- but a woken agent who is never *assigned* (e.g. the queue
    emptying between its wake and assignment, or a reload force-shadowing an
    on-duty agent via agents.js's `if (!offDuty) visible = true`) lingers
    on-duty and visible forever with nothing to do. Per the player's rule
    ("unless he has scheduled work or is active, an agent should not appear"),
    park every fully-idle, on-duty, non-admin agent back off duty so it
    vanishes in place. Assignment still wakes it on demand, so this cannot
    strand the think tank. Pure/mutating on `state`; no-op when nothing to park.
    Returns the number parked."""
    agents = state.get('agents')
    if not isinstance(agents, dict) or not agents:
        return 0
    roster = state.get('agentRoster') or []
    parked = 0
    for d in roster:
        if d.get('isAdmin'):
            continue
        a = agents.get(d.get('id'))
        if not isinstance(a, dict) or a.get('offDuty'):
            continue
        room_def = a.get('inRoom')
        if not _is_fully_idle(a, room_def):
            continue
        # Wake-on-mail: an off-duty agent woken to act on action-needed mail must
        # not be parked in the same tick before she has routed to the work
        # (see _mail_action_step, which clears _mailAwake once she's acting).
        if a.get('_mailAwake'):
            continue
        a['offDuty'] = True
        a['visible'] = False
        a['path'] = None
        a['pathIndex'] = 0
        a['pathTarget'] = None
        a['pathActive'] = False
        a['stuckTimer'] = 0
        a['replanCount'] = 0
        parked += 1
    return parked


def _feature_affinity(state, agent_id, pick):
    """Feature affinity score: how many PRIOR tasks `agent_id` completed that
    match the picked work's feature signature -- same room, same feature (the
    task's projectLabel, which assignment also folds the goal into), or same
    product. A cheap counter over the durable tasks map, no model call. The
    assignment loop uses it as a SOFT pull toward the person who already knows
    the corner of the think tank this item belongs to."""
    tasks = state.get('tasks') or {}
    room = pick.get('room')
    feature = pick.get('projectLabel') or pick.get('goal')
    product_id = pick.get('productId')
    score = 0
    for t in tasks.values():
        if t.get('status') != 'done' or t.get('assignedTo') != agent_id:
            continue
        if room and t.get('room') == room:
            score += 1
        if feature and (t.get('projectLabel') == feature or t.get('goal') == feature):
            score += 1
        if product_id and t.get('productId') == product_id:
            score += 1
    return score


# A tiny stopword list for onboarding description-affinity: enough to strip
# connective noise so the overlap counter keys on real content words. A fuller
# NLP approach is overkill -- this is a SOFT pull, never a lock.
_DESCRIPTION_STOPWORDS = frozenset({
    'the', 'a', 'an', 'and', 'or', 'to', 'of', 'for', 'with', 'on', 'in',
    'at', 'by', 'your', 'their', 'our', 'you', 'them', 'from', 'into', 'via',
    'this', 'that', 'they', 'work', 'who', 'what', 'pick', 'up', 'picking',
})


def _description_affinity(state, agent_id, pick):
    """Onboarding affinity: how well `agent_id`'s DESCRIPTION matches the picked
    work. A brand-new hire has no completed-work history, so feature affinity
    (_feature_affinity) can never route them -- they fall to pure round-robin.
    This scores the agent's role / mission / profile instructions / access grant
    against the task's own text (title + instructions + goal + projectLabel +
    room) by meaningful-token overlap, so a new team member is handed the work
    that fits their description instead of being chosen last. Cheap + deterministic
    (no model call); the assignment loop uses it as a soft pull exactly like
    feature affinity."""
    roster_def = next((d for d in (state.get('agentRoster') or [])
                       if d.get('id') == agent_id), None)
    if not roster_def:
        return 0
    desc_parts = [
        roster_def.get('role') or '',
        roster_def.get('description') or '',
        roster_def.get('specialty') or '',
    ]
    profile = roster_def.get('profile') or {}
    if isinstance(profile, dict):
        desc_parts.append(profile.get('mission') or '')
        instr = profile.get('instructions')
        if isinstance(instr, list):
            desc_parts.extend(str(i) for i in instr)
        elif instr:
            desc_parts.append(str(instr))
    desc_tokens = set()
    for part in desc_parts:
        desc_tokens.update(_description_tokens(part))
    if not desc_tokens:
        return 0
    task_tokens = set()
    for field in ('title', 'instructions', 'goal', 'projectLabel'):
        task_tokens.update(_description_tokens(pick.get(field)))
    task_tokens.update(_description_tokens(pick.get('room')))
    if not task_tokens:
        return 0
    return len(task_tokens & desc_tokens)


def _description_tokens(text):
    if not text:
        return []
    out = []
    for tok in str(text).lower().split():
        clean = ''.join(ch for ch in tok if ch.isalnum())
        if clean and clean not in _DESCRIPTION_STOPWORDS and len(clean) > 2:
            out.append(clean)
    return out


def _recruit_pair_navigator(state, driver_id, task, can_wake_off_duty, grid, doors, now_ms):
    """W1: server-owned pair recruitment. The client's assignPairTask recruits a
    navigator and walks her over; the server previously assigned a `pair` card
    SOLO (no navigator ever joined). Recruits the next most-idle eligible
    candidate deterministically (round-robin, zero JEV spend, mirrors
    _assign_due_item), walks her to a free spot beside the driver's door, and
    marks her as the task's navigator (pairWith/pairTaskId/path). The driver's
    own walk was already issued by the caller. Returns the navigator id, or
    None when no second eligible hand exists (the driver proceeds solo, the
    same graceful degradation the client's assignPairTask has when the pool is
    too thin)."""
    agents = state.get('agents') or {}
    candidates = _eligible_candidates(state, can_wake_off_duty)
    ordered = [cid for cid in candidates if cid != driver_id]
    if not ordered:
        return None
    # Round-robin over eligible ids (deterministic, stable roster order).
    rr = state.setdefault('sim', {}).setdefault('rr', {})
    pointer = rr.get('pair', 0)
    idx = pointer % len(ordered)
    nav_id = ordered[idx]
    rr['pair'] = (pointer + 1) % max(1, len(ordered))
    nav = agents.get(nav_id)
    if not nav:
        return None
    if nav.get('offDuty'):
        appear_from_outskirts(state, nav_id, doors)
    room = task.get('room')
    door = (doors or {}).get(room)
    if not door:
        return None
    base_x = door['x'] + door['w'] / 2
    base_y = door['y'] + door['h'] + 4
    # Try several offsets beside the door (mirrors assignPairTask's own
    # "try offsets that clear the walkable strip" loop).
    for dx in PAIR_WALK_OFFSETS:
        path = find_path(nav['x'], nav['y'], base_x + dx, base_y, nav_id, agents, grid)
        if path and len(path) > 0:
            nav['pairWith'] = driver_id
            nav['pairTaskId'] = task['id']
            nav['path'] = path
            nav['pathIndex'] = 0
            nav['pathTarget'] = {'x': base_x + dx, 'y': base_y}
            nav['stuckTimer'] = 0
            nav['replanCount'] = 0
            nav['respawnedForTask'] = False
            task['pairWith'] = nav_id
            return nav_id
    return None


def _sprint_worker_pool(state, sprint_id, worker_count=None):
    """The STRICT assignment pool for a sprint's cards: the non-admin members of
    the sprint's teams -- roster order, deduped across teams -- capped at the
    sprint's `workerCount` (the director chose 1-6 workers; the cap picks which
    of a team's members staff THIS sprint). The designated scrum master is
    excluded from the pool when the chosen headcount is below
    SCRUM_MASTER_MIN_TEAM_SIZE (on a lean sprint the facilitator is not a
    counted worker); at or above that size the scrum master IS a working member
    of the pool. Legacy sprints (no workerCount) default to the team's full
    complement with the scrum master counted. Returns None when `sprint_id` is
    absent (a non-sprint card has no pool), else the pool list -- possibly
    empty, which makes the sprint's cards WAIT (strict staffing: no fallback to
    the think tank at large). `worker_count` overrides the record's stored
    value (used by the staffing-expansion probe to preview a larger pool)."""
    if not sprint_id:
        return None
    record = (state.get('sprints') or {}).get(sprint_id)
    if not record:
        return []
    if worker_count is None:
        worker_count = int(record.get('workerCount') or MAX_TEAM_MEMBERS)
    worker_count = max(1, min(MAX_TEAM_MEMBERS, worker_count))
    # Team ids on a sprint record may be keyed by the team's `id` OR its
    # director's id (the two keyings team records use) -- resolve each to a
    # director id so _sim_direct_reports can list its members.
    directors = []
    for tid in (record.get('teamIds') or []):
        t = _team_row(state, tid)
        directors.append(t.get('directorId') if t else tid)
    if not directors:
        return []
    # A lean sprint (below the SM-size floor) is worked by the team's workers,
    # never the dedicated facilitator -- she runs ceremonies, not cards. A
    # sprint at/above the floor counts the scrum master as a working member.
    excluded = set()
    if worker_count < SCRUM_MASTER_MIN_TEAM_SIZE:
        for d in directors:
            t = next((x for x in (state.get('teams') or []) if x.get('directorId') == d), None)
            if t and t.get('scrumMasterId'):
                excluded.add(t['scrumMasterId'])
    pool = []
    for d in (state.get('agentRoster') or []):
        if d.get('isAdmin'):
            continue
        if d.get('director') not in directors:
            continue
        if d['id'] in excluded:
            continue
        pool.append(d['id'])
    # The headcount cap picks WHICH members staff the sprint, stable roster
    # order (deterministic -- same sprint always names the same pool).
    return pool[:worker_count]


def _sprint_staffing_step(state, now_ms=None):
    """Sprint staffing expansion: a director grows an ACTIVE sprint's staff when
    the agents need help. "Needs help" is modeled deterministically (no JEV
    spend, per the keep-deterministic-things-deterministic rule): a sprint below
    the six-worker cap whose cards are STILL queued while every current pool
    member is busy (task/pair/busy -- even a member who could otherwise be woken)
    gets ONE more worker, on each pass it stays saturated, until the queue drains
    or the cap is hit. The next member in roster order joins the pool (at the
    SCRUM_MASTER_MIN_TEAM_SIZE threshold the scrum master becomes a counted
    worker). A team with fewer real members than the target can't expand further.
    Pure state mutation; returns the sprint ids expanded this pass."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    sprints = state.get('sprints') or {}
    agents = state.get('agents') or {}
    expanded = []
    for sid, record in sprints.items():
        if record.get('status') != 'active':
            continue
        wc = int(record.get('workerCount') or MAX_TEAM_MEMBERS)
        if wc >= MAX_TEAM_MEMBERS:
            continue  # already at the six-worker cap -- nothing to add
        # Can the pool actually grow? The next count must name a real member
        # beyond the current pool (a team with only 3 members can't staff 6).
        next_pool = _sprint_worker_pool(state, sid, wc + 1)
        if not next_pool or len(next_pool) <= len(_sprint_worker_pool(state, sid, wc)):
            continue
        # Sprint cards still waiting to be handed out (done cards left the
        # queue; sprint-tagged queue entries are exactly the unfinished set).
        if not [it for it in (state.get('workQueue') or []) if it.get('sprintId') == sid]:
            continue
        # "Agents need help": every current pool member is occupied. Off-duty
        # members still count as available (a sprint card is always wakeable),
        # so expansion only fires when nobody in the pool can take work at all.
        available = [m for m in _sprint_worker_pool(state, sid, wc)
                     if not (agents.get(m) or {}).get('busy')
                     and not (agents.get(m) or {}).get('task')
                     and not (agents.get(m) or {}).get('pairWith')]
        if available:
            continue
        record['workerCount'] = wc + 1
        _log_governance(state, record.get('ownerId') or sid, 'sprint_staffing',
                        {'sprint': sid, 'workers': wc + 1, 'action': 'expanded'})
        expanded.append(sid)
    return expanded


def _assign_player_task(state, pick, now_ms, task_id_holder=None):
    """Assign one due queue item to the PLAYER (a human-in-the-loop card): mint
    the durable state['tasks'] record with status 'needs_player' and delivered
    to the player's inbox + email, instead of walking any agent. Returns the
    task dict, or None on failure (-> caller requeues/attempt++).

    The player card is a REAL task in the durable mirror, so the existing
    dependency machinery works unchanged in BOTH directions:
      - an agent card queued `depends_on_task=<this id>` stays unassigned until
        the player marks it done (see _work_item_dependency_met / _dependency_landed);
      - this card's own `dependsOn` (an agent story the player's work waits on)
        is honored by pick_next_due_index, so it is not even delivered until
        that story lands.
    `task_id_holder` is the mutable init([count]) nextTaskId counter, shared
    with assign_task so ids never collide."""
    title = (pick.get('title') or '').strip()
    if not title:
        return None
    task_id_holder[0] += 1
    task_id = 'task-' + str(task_id_holder[0])
    task = {
        'id': task_id,
        'title': title,
        'room': pick.get('room') or None,
        'instructions': pick.get('instructions') or '',
        'projectLabel': pick.get('projectLabel') or pick.get('goal') or None,
        'taskType': pick.get('taskType') or 'code',
        'assignedTo': 'player',
        'status': 'needs_player',  # the player's to do; -> 'done' via complete_player_task
        'createdAt': now_ms,
        'dependsOn': pick.get('dependsOn') or None,
        'userStory': pick.get('userStory') or None,
        'acceptanceCriteria': pick.get('acceptanceCriteria') or None,
        'issueKey': pick.get('issueKey') or None,
        'budgetMs': pick.get('budgetMs') or None,
        'lane': pick.get('lane') or None,
    }
    state.setdefault('tasks', {})[task_id] = task
    # Deliver to the player: an inbox card (rendered by the UI with a Mark-done
    # button) + a real email so the wait is visible even when the player isn't
    # looking at the sim.
    mid = f'ptask-{int(now_ms)}'
    task['playerInboxId'] = mid
    inbox = state.setdefault('playerInbox', [])
    inbox.append({
        'id': mid, 'kind': 'player_task', 'taskId': task_id,
        'title': title, 'instructions': task['instructions'],
        'status': 'needs_player', 'createdAt': now_ms,
        'dependsOn': task['dependsOn'], 'issueKey': task['issueKey'],
        'lane': task['lane'],
    })
    depends_line = ''
    if task['dependsOn']:
        depends_line = f"\n\nNote: this task waits on '{task['dependsOn']}' being done before it starts."
    _queue_player_email(
        state, 'player_task',
        f"[AI Think Tank] A task is waiting on YOU: {title}",
        (f"The think tank has handed you this task and is waiting on your hands before "
         f"it resumes the work queued behind it:\n\n"
         f"{title}\n\n{task['instructions'] or '(no further instructions)'}\n\n"
         f"Mark it done in the player-inbox when you've completed it."
         f"{depends_line}"),
        now_ms=now_ms)
    from serve import log_action
    log_action('player', 'task_assigned',
               {'taskId': task_id, 'title': title, 'room': task['room']}, authorized=False)
    return task


def _assign_due_item(state, pick, can_wake_off_duty, grid, doors, now_ms, task_id_holder=None):
    """Deterministic assignment of one due queue item: pick the most-idle
    eligible candidate (round-robin, zero JEV spend), wake her if off-duty,
    assign. Returns the task dict or None. Mirrors assignTaskViaJev's
    fallback-to-first-eligible but replaces JEV with a deterministic pick."""
    agents = state.get('agents') or {}
    # Human-in-the-loop: a card pinned to the PLAYER (assignedTo 'player') is
    # never an agent's job. Assign it to the player (durable 'needs_player'
    # task + inbox/email delivery) and return -- no door path, no roster pick,
    # no spend. Anything queued depends_on_task on it resumes when the player
    # marks it done.
    if (pick.get('assignedTo') or '') == 'player':
        return _assign_player_task(state, pick, now_ms, task_id_holder or _TASK_ID_HOLDER)
    # A shared-backlog card is queued ROOM-LESS on purpose: the ASSIGNED agent
    # figures out where the work needs to happen. Resolve the room here, at the
    # moment of assignment, and persist it onto the pick so the walk/gate/grade
    # lifecycle (which routes by task['room']) sees a concrete room.
    if not pick.get('room'):
        pick['room'] = _resolve_assignment_room(pick)
    candidates = _eligible_candidates(state, can_wake_off_duty)
    # Sprint staffing: a sprint card is assigned ONLY to the sprint's chosen
    # worker pool (the director's 1-6). This is a HARD restriction, not the
    # soft team preference below -- a lean sprint's cards wait (empty pool ->
    # None -> the caller requeues) rather than leaking to the think tank at
    # large, by design. `sprint_pool` is None for non-sprint cards (no pool).
    sprint_pool = _sprint_worker_pool(state, pick.get('sprintId'))
    if sprint_pool is not None:
        candidates = [cid for cid in candidates if cid in sprint_pool]
        if not candidates:
            return None
    # Phase E addendum: a re-opened fix may be pinned to its original author, and
    # a gate review is pinned to its reviewer (pick['assignedTo']). Honor that --
    # and WAKE an off-duty pinned agent even when the generic wake rule (which
    # wakes only enough agents to keep one awake-idle) would otherwise skip them.
    # Without this, the SECOND reviewer of a pair (both off-duty) never gets
    # woken and the gate deadlocks at one approval.
    # Phase E2d: an INCIDENT (bug) is likewise pinned to the owning team's
    # on-call -- same wake semantics as a reviewer pin, even with no reviewOf.
    # Theo routing: directRoute is the same pin/wake treatment for
    # a spike the classifier attributed to a specific team's worker.
    # Sprint rollover: a card re-planned by refinement carries `_reassignedTo`,
    # a fresh explicit owner for the next sprint -- honored as a (soft) pin.
    pin_kind = (pick.get('reviewOf') or pick.get('incident')
                or pick.get('_mailResume') or pick.get('directRoute')
                or pick.get('_reassignedTo'))
    pinned = (pick.get('assignedTo') or pick.get('_reassignedTo')) if pin_kind else None
    # Sprint staffing: a pin to someone OUTSIDE the sprint's worker pool is
    # dropped -- strict staffing wins over the soft reassignment pin (a card
    # re-planned by refinement pins to the team's least-loaded member, but if
    # that member isn't on this sprint's roster, the card waits for the pool).
    if pinned and sprint_pool is not None and pinned not in sprint_pool:
        pinned = None
    if pinned and agents.get(pinned):
        pinned_agent = agents.get(pinned)
        if not pinned_agent.get('busy') and not pinned_agent.get('task') and not pinned_agent.get('pairWith'):
            chosen_id = pinned
            # A reviewer/incident pin wakes AT THE TARGET ROOM'S DOOR (short,
            # reliable walk to judge the work); a refinement re-plan pin is not
            # tied to a specific desk, so it wakes via the generic outskirts
            # placement (see appear_from_outskirts below) -- same reachability
            # guarantee, no door-geometry coupling.
            if pinned_agent.get('offDuty') and not pick.get('_reassignedTo'):
                # Wake a reviewer at the REVIEW TARGET'S OWN door approach: the reviewer
                # is judging the author's work at that desk, so placing her at
                # the room's own door gives a short, reliable path.
                _spawn_at_room_door(state, pinned, pick.get('room'), agents, grid, doors)
        elif pinned in candidates:
            chosen_id = pinned
        elif pick.get('incident'):
            # The on-call is mid-work (busy/task/pairWith) so the pin can't be
            # honored. Re-derive the owning team's current on-call -- the
            # rotation already fell through to the backup / second backup -- so
            # the incident stays on the owning team instead of leaking to a
            # global round-robin pick on an unrelated team. Falls to None
            # (generic round-robin) only when the whole team is unavailable.
            chosen_id = None
            director_id = _product_director(state, pick.get('productId'))
            if director_id:
                backup = on_call_agent(state, director_id, pick.get('sprintId'), now_ms)
                if backup and backup != pinned and agents.get(backup):
                    chosen_id = backup
                    if agents.get(backup).get('offDuty'):
                        _spawn_at_room_door(state, backup, pick.get('room'), agents, grid, doors)
        else:
            chosen_id = None
    else:
        chosen_id = None
    if chosen_id is None:
        if not candidates:
            return None
        # Round-robin over eligible ids (deterministic, stable order of roster).
        rr = state.setdefault('sim', {}).setdefault('rr', {})
        pointer = rr.get('task', 0)
        # First eligible index at/after the pointer.
        ordered = [cid for cid in candidates if cid in agents]
        # Gap: a story filed against a specific team
        # (pick['teamId']) had zero team preference at assignment -- only
        # reviewer selection (_pick_reviewer_ids) ever preferred same-team.
        # SOFT preference only, same shape as that precedent: narrow to the
        # team's own free members when any exist, else fall back to the full
        # think tank pool unchanged. Never a hard lock (unlike an incident's
        # on-call pin) -- a small think tank can't afford to starve one team's
        # queue while another sits idle just because its own members are busy.
        team_id = pick.get('teamId')
        if team_id:
            team_members = set(_sim_direct_reports(state, team_id))
            # Cross-team borrowing: an agent loaned to this team (roster `loan`
            # tag) is part of the assignment pool for the loan's duration, so
            # the borrower team's sprint items are soft-preferred to them too.
            for d in (state.get('agentRoster') or []):
                if d.get('loan') and d['loan'].get('teamId') == team_id:
                    team_members.add(d['id'])
            team_ordered = [cid for cid in ordered if cid in team_members]
            if team_ordered:
                ordered = team_ordered
        if not ordered:
            return None
        # Fault-aware routing: soft-prefer a candidate with no
        # recent unfaded failure signal over the plain round-robin order --
        # same "soft preference, never a hard lock" shape as the team
        # preference just above. If EVERY eligible candidate is currently
        # cooling down, fall back to the full ordered list rather than ever
        # blocking a real assignment over a transient signal.
        cool = [cid for cid in ordered if _agent_failure_score(state, cid, now_ms) < FAILURE_COOLDOWN_THRESHOLD]
        if cool:
            ordered = cool
        # Feature affinity: SOFT pull toward an agent who has completed similar
        # work before (same room / feature / product), so a team's backlog items
        # gravitate to the person who already knows that corner of the tank.
        # Same "soft preference, never a hard lock" shape: only narrow when
        # someone has a nonzero history, ties preserve roster order, and a
        # candidate with no matching history is never forced off the queue.
        scored = [(cid, _feature_affinity(state, cid, pick)) for cid in ordered]
        best_affinity = max((s for _, s in scored), default=0)
        if best_affinity > 0:
            ordered = [cid for cid, s in scored if s == best_affinity]
        else:
            # Onboarding: with nobody having prior work that matches this item
            # (a brand-new team member has NO history, so feature affinity can
            # never route to them -- they'd fall to pure round-robin), soft-pull
            # toward the candidate whose DESCRIPTION fits the work. Same soft,
            # never-a-lock shape: only narrow when someone's description actually
            # matches, and ties keep roster order.
            desc = [(cid, _description_affinity(state, cid, pick)) for cid in ordered]
            best_desc = max((s for _, s in desc), default=0)
            if best_desc > 0:
                ordered = [cid for cid, s in desc if s == best_desc]
        idx = pointer % len(ordered)
        chosen_id = ordered[idx]
        rr['task'] = (pointer + 1) % max(1, len(ordered))
    # Sprint staffing invariant: whoever was chosen must be inside the sprint's
    # pool. Candidates were already narrowed above, so this only trips for a
    # path that bypassed the narrowed list (e.g. an incident backup re-derive);
    # returning None makes the card wait rather than hand a sprint card to a
    # non-staffed worker.
    if sprint_pool is not None and chosen_id not in sprint_pool:
        return None
    chosen = agents.get(chosen_id)
    if chosen and chosen.get('offDuty'):
        appear_from_outskirts(state, chosen_id, doors)
    extra = {
        'research': pick.get('research'),
        'taskType': pick.get('taskType'),
        'skillReview': pick.get('skillReview'),
        # CS329A takeaway #2: thread the review checklist through
        # to the assigned task so the Python review executor can grade each
        # requirement (code/jev/human) instead of a single all-or-nothing
        # verdict. Same class of gap as 'distill' two lines down.
        'checklist': pick.get('checklist'),
        # Bug: 'distill' was missing from this
        # whitelist entirely, same class of gap as skillReview -- a distill
        # sweep task assigned through here never carried the flag onto the
        # real task object, so _peer_gated_lane's distill exemption (added to
        # fix the exact same infinite-review-loop bug skillReview already hit)
        # silently never applied. See assign_task's own 'distill' field below,
        # which was equally missing.
        'distill': pick.get('distill'),
        'kbClass': pick.get('kbClass'),
        'dependsOn': pick.get('dependsOn'),
        # Phase E2b: a SPIKE's advisory time-budget (the executor keeps its
        # single model call short to honor it).
        'budgetMs': pick.get('budgetMs'),
        # A breakdown story's size estimate rides onto the task so executors and
        # dashboards can see effort (see queue_work / assign_task).
        'sizeEstimate': pick.get('sizeEstimate'),
        'reviewOf': pick.get('reviewOf'),
        'reviewAuthorId': pick.get('reviewAuthorId'),
        # Phase E2d: an INCIDENT routes to the owning team's on-call (taskType
        # 'bug' + productId + assignedTo travel so the fix is attributable and
        # the cap can be enforced against the open task set).
        'incident': bool(pick.get('incident')),
        'productId': pick.get('productId'),
        # Adversarial (winter) village tags thread onto the task (see
        # assign_task / queue_work) so completion can be read per side.
        'adversarialTaskId': pick.get('adversarialTaskId'),
        'villageId': pick.get('villageId'),
        # A player-filed card's contract survives onto the task (see assign_task).
        'userStory': pick.get('userStory'),
        'acceptanceCriteria': pick.get('acceptanceCriteria'),
        # Ordered-pipeline marker (see queue_work / assign_task): threads the
        # pipelineStep onto the real task so the strict-order sweep can read
        # completion back (and the content dispatcher can route the step).
        'pipelineStep': pick.get('pipelineStep'),
        # Bot Ops / shadow mode: thread the dry-run flag onto the assigned task
        # (see assign_task) -- same class of whitelist as 'distill'/'checklist'.
        'shadow': pick.get('shadow'),
        # Item 4: the groomer-judged spend ceiling + band survive onto the task
        # (see assign_task, which resolves the budget). Same whitelist class as
        # 'distill'/'checklist': dropping them would leave the task unbudgeted.
        'budgetUsd': pick.get('budgetUsd'),
        'budgetBand': pick.get('budgetBand'),
        # Bell-style spare-time lane: the protected-exploration flag rides onto
        # the assigned task (see assign_task) so review/failure paths can honor
        # it -- same whitelist contract as 'distill'/'checklist'.
        'moonshot': bool(pick.get('moonshot')),
        # Attention lane (build/reading/open/parking-lot) rides onto the task so
        # the board and the reading rate-limit can see it (see assign_task).
        'lane': pick.get('lane'),
        # Player-authored provenance rides onto the assigned task (see
        # assign_task) -- the JEV gate's work-context bypass trusts only tasks
        # the PLAYER wrote, so dropping this here would silently downgrade a
        # player-vetted task to agent-authored.
        'playerAuthored': pick.get('playerAuthored'),
        # A scheduled item's notBefore rides onto the assigned task so the
        # preemption picker (_suspend_busy_agent_for_scheduled) can tell
        # TIME-CRITICAL work (this card was scheduled for a specific time; the
        # agent must not be yanked off it for a one-off) from ordinary/sprint
        # work. Same whitelist contract as 'distill'/'checklist'.
        'notBefore': pick.get('notBefore'),
    }
    # Cut 2 coaching + runbook injection: append the chosen agent's pending
    # growth-plan note (applied once) and any product runbook knowledge to the
    # task's instructions -- manager feedback / prior-incident learning changes
    # THIS task's execution.
    instructions = _augment_task_instructions(
        state, chosen_id, pick.get('productId'), pick.get('instructions'),
        refocus=bool(pick.get('reviewOf') or pick.get('incident')))
    task = assign_task(state, chosen_id, pick.get('title'), pick.get('room'),
                       instructions, pick.get('projectLabel') or pick.get('goal'), extra,
                       grid, doors, now_ms, task_id_holder or _TASK_ID_HOLDER)
    # W1: a `pair` card recruits a navigator server-side (deterministic
    # round-robin, zero JEV). The driver keeps the solo walk when no second
    # eligible hand exists -- graceful degradation, never a stuck card.
    if task and pick.get('pair') and not pick.get('reviewOf') and not pick.get('incident'):
        _recruit_pair_navigator(state, chosen_id, task, can_wake_off_duty,
                                grid, doors, now_ms)
    return task