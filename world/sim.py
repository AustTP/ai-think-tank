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
        if cell_is_free_for_destination(gx, gy):
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
            continue  # genuinely unreachable -> leave to the cancel path, not hudged
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
                # a task's door gets marked working (busy/inRoom/workUntil).
                # (Off-duty is immediate and in-place now -- see
                # send_agent_off_duty -- so there is no off-duty ARRIVAL event;
                # an idle agent vanishes where she stands.) Cancels are still
                # just logged this slice.
                if events:
                    import serve
                    for kind, sub, aid in events:
                        if kind == 'arrive' and sub == 'task':
                            _arrive_at_task(state, aid, now)
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
    state = serve.get_state_from_db()
    if not state:
        return None
    state = _engine.tick(state)
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
    serve.save_state_to_db(state)
    return state


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
    state = serve.get_state_from_db()
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
ROADMAP_CADENCE_MS = 7 * 24 * 3600 * 1000   # weekly silent priority recompute
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
# An on-duty agent that just completed a task in a delegatable room files a
# follow-up work-request when the room's queued/in-flight backlog has at most
# this many items left -- "we finished X, and the room is thinning out". Zero
# turns the signal off entirely (opt-in per deployment).
WORK_REQUEST_ROOM_THIN = 1
# Hard cap of filed-but-not-yet-groomed requests in a single ceremony, so a
# churny think tank can't convene a backlog-refinement meeting over a runaway list.
REFINEMENT_MAX_REQUESTS = 20
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


def _apply_content_result(state, task, result):
    """Merge a completed content-executor result into the durable state, inside
    the task_cycle's single read-modify-write. Writes the research topic's
    grown seenUrls back (so a future run dedups against it) and records the
    agent's real note (mirrors runResearchTask's notes.push). Mutates `state`;
    the note is attached to the task record so the board/UI can surface it."""
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
    # Phase 3 pressoffice: a review/QA pass that found real problems enqueues a
    # follow-up fix task (mirrors runReviewTask's queueWork on an 'actionable'
    # Jev verdict). Must go through the tick's single read-modify-write, not a
    # direct DB write, so queue it here from the executor's result.
    qf = result.get('queueFix')
    if qf:
        # A gate review that must go BACK to the same author + the same gate
        # carries the parent id; a free-form pressoffice fix does not.
        queue_work(state, [qf])
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
            if verdict == 'actionable':
                if not _maybe_escalate_stuck_gate(parent, gate, 'repeated rejections'):
                    gate['approvals'] = 0
                    gate['approvers'] = []
                    _sim_notify_author(state, parent, reviewer)
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
                if verdict == 'actionable':
                    if not _maybe_escalate_stuck_gate(parent, gate, 'repeated red-pipeline rejections'):
                        gate['approvals'] = 0
                        gate['approvers'] = []
                        _sim_notify_author(state, parent, reviewer)
                elif reviewer and reviewer not in gate['approvers']:
                    gate['approvers'].append(reviewer)
                    gate['approvals'] += 1


def _take_content_result(task_id):
    with _content_results_lock:
        return _content_results.pop(task_id, None)


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

    def _run():
        try:
            executor(snapshot, agent_id, task, base_ctx={})
        except Exception as e:  # never let a content failure strand the agent
            _store_content_result(task_id, {'note': f'Content execution failed: {e}', 'seenUrls': (task.get('research') or {}).get('seenUrls') or []})

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
        if not item or not item.get('title') or not item.get('room'):
            continue
        work_queue.append({
            'title': item['title'],
            'room': item['room'],
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
        })
    return len(work_queue)


def queue_spike(state, title, room, budget_ms, now_ms=None, goal=None, instructions=None,
                project_label=None):
    """Phase E2b: enqueue a time-boxed SPIKE (taskType='spike'). A spike is an
    investigation with no committed deliverable: it's LOWEST priority (fills
    gaps, never blocks committed work), carries a hard `budgetMs` for the work
    cycle, and -- because nothing ships -- its completion does NOT open a peer
    gate (see _peer_gated_lane). Produces a findings artifact, not a release.
    Returns the new queue length, or None if the item was rejected (no room or
    title)."""
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
    }])
def think_tank_has_work(state, now_ms):
    """Port of tasks.js thinkTankHasWork: a due queue item, or any agent currently
    task/handoff/pair/busy."""
    work_queue = state.get('workQueue') or []
    if any(is_work_item_due(item, now_ms) for item in work_queue):
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


def on_call_agent(state, director_id, sprint_id=None):
    """Phase E2d: which agent on `director_id`'s team is on-call right now.
    Deterministic per-sprint rotation over the team's non-scrum-master, non-admin
    members -- derived, not stored (same precedent as sprint_progress). The
    rotation uses a stable hash of the sprint id so the same sprint id always
    hands back the same on-call across restarts, and shifts across sprints.
    Among the base pick we prefer an ACTIVE member (on-duty, not off-duty) over a
    resting one; deadfall back to the rotation slot so the role is never empty.
    Returns an agent id or None if the team has no rousable members."""
    pool = _team_oncall_members(state, director_id)
    if not pool:
        return None
    seed = sprint_id or 'default'
    # stable hash across restarts; fold in a running sprint counter so rotations
    # actually advance even if sprint ids were reused.
    counter = len([s for s in (state.get('sprints') or {}).values()
                   if s.get('teamIds') and director_id in s.get('teamIds')])
    h = 0
    for ch in str(seed):
        h = (h * 31 + ord(ch)) & 0xffffffff
    idx = (h + counter) % len(pool)
    pick = pool[idx]
    agents = state.get('agents') or {}
    def _active(aid):
        a = agents.get(aid)
        return bool(a) and not a.get('offDuty')
    # Prefer an active member among the pool before falling back to the bare
    # rotation slot; if the computed slot is off-duty but an active teammate
    # exists, take the active teammate in roster order.
    if not _active(pick):
        actives = [m for m in pool if _active(m)]
        if actives:
            return actives[0]
    return pick


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
    oc = on_call_agent(state, director_id, sprint_id)
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


def pick_next_due_index(work_queue, now_ms, exclude_items):
    """Port of tasks.js _pickNextDueIndex: highest due priority wins; strict >
    preserves arrival order at equal priority; skip items in exclude_items
    (this call's attemptedThisCycle). Returns index or -1."""
    best_index, best_priority = -1, float('-inf')
    for i, item in enumerate(work_queue):
        if not is_work_item_due(item, now_ms):
            continue
        # exclude_items is a Set of QUEUE-ITEM IDENTITIES (id(item)), not the
        # dicts themselves -- queue items are mutable dicts and unhashable.
        if exclude_items and id(item) in exclude_items:
            continue
        priority = item.get('priority', WORK_PRIORITY['normal'])
        if priority > best_priority:
            best_priority, best_index = priority, i
    return best_index


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
    }
    `completing` is the most recent agent who landed a deliverable matching the
    product name/alias -- derived from completedDeliverables. None on-call => no
    way to answer (nobody rousable on the owning team)."""
    products = state.get('products') or {}
    prod = products.get(product_id) or {}
    director_id = prod.get('teamId')
    oc = None
    if director_id:
        oc = on_call_agent(state, director_id, sprint_id)
    completing = _completing_agent_for_product(state, product_id, prod)
    return {
        'onCall': oc,
        'completing': completing,
        'product': prod,
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
                 target_date_ms=None, now_ms=None, team_ids=None):
    """Create a sprint record and append its valid items to the workQueue, each
    tagged with sprintId. `items` are dicts shaped like queue_work items. Only
    items whose room is in `valid_rooms` (the caller supplies the delegatable
    set -- sim.py stays decoupled from serve.py's constant) are kept; a sprint
    with zero retainable items is not created. `team_ids` records which teams
    the sprint touches (the scrum-master gate is enforced by the CALLER before
    calling here; this just persists the association). Returns the sprint
    record dict, or None if nothing was created. Mutates state['sprints'] +
    state['workQueue']."""
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
        'status': 'active',
        'items': [_sprint_item_id(it) for it in kept],
    }
    sprints[sprint_id] = record
    for it in kept:
        it['sprintId'] = sprint_id
    queue_work(state, kept)
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
    still_queued = set(_sprint_item_id(it) for it in (state.get('workQueue') or []))
    done = in_progress = queued = 0
    landed = []
    for pair in items:
        # After a save/load round-trip tuples arrive as JSON lists -- normalize
        # so hashing against the set of (title, room) tuples always works.
        title, room = tuple(pair)
        ident = (title, room)
        if ident in still_queued and not any(
                t.get('status') == 'done' and t.get('title') == title and t.get('room') == room
                for t in open_tasks.values()):
            # Still in the queue, not yet assigned: queued.
            queued += 1
            continue
        matched_done = matched_active = False
        for t in open_tasks.values():
            if t.get('title') != title or t.get('room') != room:
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
        else:
            queued += 1
    total = len(items)
    pct = round((done / total) * 100) if total else 0
    return {'total': total, 'queued': queued, 'inProgress': in_progress,
            'done': done, 'pct': pct, 'landed': landed}


def close_sprint(state, sprint_id):
    """Mark a sprint closed (a container action -- already-queued items finish
    or age out normally; closing doesn't cancel work). Returns the updated
    record, or None if the sprint doesn't exist."""
    sprints = state.get('sprints') or {}
    record = sprints.get(sprint_id)
    if not record:
        return None
    record['status'] = 'closed'
    return record


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
    for sid, record in list(sprints.items()):
        if record.get('status') != 'active':
            continue
        progress = sprint_progress(state, sid)
        if progress and progress.get('total') and progress['done'] >= progress['total']:
            close_sprint(state, sid)
            record['closedAt'] = (time.time() * 1000) if now_ms is None else now_ms
            record['autoClosed'] = True
            closed.append(sid)
    return closed
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
                    edited_at_ms=None):
    """Pure: create or update a wiki page's METADATA + return the lined-up
    record for serve.py to persist (serve writes page['body'] to disk only for
    pre-existing/new pages; the metadata carries version + history). Rejects
    too-long bodies and unknown categories. Returns (record, is_new)."""
    import time as _time
    edited_at_ms = edited_at_ms if edited_at_ms is not None else int(_time.time() * 1000)
    categories = (state.get('wiki') or {}).get('categories') or {}
    if category not in categories:
        return None, False
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


def wiki_read_pages(state, room, max_pages=3):
    """Pure: the subset of wiki pages relevant to a task in `room`, chosen by
    category-room affinity. Only pages affined to THIS room or to the think tank as
    a whole (a None affinity) are injected -- a page affined to a DIFFERENT room
    is never pulled in; "read before acting" means reading the pages about the
    work you're about to do, not a random archive. Returns metadata-only records
    (id/title/category/version) so serve.py can fetch the bodies. Stable order
    (recency, then version) for deterministic injection."""
    pages = ensure_wiki(state)
    scored = []
    for page_id, rec in pages.items():
        affinity = _page_room_affinity(state, rec)
        if affinity != room and affinity is not None:
            continue  # affined to another room -> irrelevant here
        score = 0 if affinity == room else 1  # exact room beats think tank-wide
        scored.append((score, -(int(rec.get('editedAt') or 0)), page_id, rec))
    scored.sort()
    return [r for _s, _a, _pid, r in scored[:max_pages]]


def inject_wiki_context(state, task):
    """Pure: build the 'before you act, here's what the think tank knows' context
    block for a task, from the wiki pages for its room. Returns a non-empty
    string only when there ARE relevant pages; empty when the wiki has none
    (caller still runs the executor with a blank context)."""
    room = (task or {}).get('room')
    if not room:
        return ''
    page_ids = (task or {}).get('wikiPageIds')
    if page_ids:
        pages = [(ensure_wiki(state)).get(pid) for pid in page_ids if (ensure_wiki(state)).get(pid)]
    else:
        pages = wiki_read_pages(state, room)
    if not pages:
        return ''
    lines = ['The think tank knowledge base has these entries relevant to this work:' , '']
    for rec in pages:
        lines.append(f"- {rec.get('title')} (category: {rec.get('category')}, v{rec.get('version')})")
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
        'skillReview': bool((extra or {}).get('skillReview')),
        'distill': bool((extra or {}).get('distill')),
        'budgetMs': (extra or {}).get('budgetMs'),
        'reviewOf': (extra or {}).get('reviewOf'),
        'reviewAuthorId': (extra or {}).get('reviewAuthorId'),
        # CS329A takeaway #2: checklist survives onto the task
        # object itself (see _assign_due_item's extra dict) -- the review
        # executor grades per-requirement from here.
        'checklist': list((extra or {}).get('checklist') or []),
        'incident': bool((extra or {}).get('incident')),
        'productId': (extra or {}).get('productId'),
        # A player-filed card's contract: the user story + acceptance criteria
        # ride onto the task so the coding executor's prompt includes the full
        # spec (content.py folds them into the backlog line it builds from
        # task.instructions).
        'userStory': (extra or {}).get('userStory'),
        'acceptanceCriteria': (extra or {}).get('acceptanceCriteria'),
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
    a['task'] = None
    a['busy'] = False
    a['inRoom'] = None
    a['visible'] = True
    a['approvedCount'] = (a.get('approvedCount') or 0) + 1
    a['weekApprovals'] = (a.get('weekApprovals') or 0) + 1
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
    a['path'] = None
    a['pathIndex'] = 0
    a['pathTarget'] = None
    a['headingOffDuty'] = None
    a['stuckTimer'] = 0
    a['replanCount'] = 0
    a['offDuty'] = True
    a['visible'] = False


MIN_RESEARCH_CADENCE_MS = 5 * 60 * 1000  # floor: a misparsed "every second" can't spam the queue


def next_topic_id(state):
    """Server-side monotonic counter for researchTopics ids (topic-1, topic-2,
    ...), mirroring next_issue_key's cold-starts-at-1/never-collides shape.
    Topics aren't team-scoped, so this is one global counter, not per-prefix."""
    n = (state.get('researchTopicCounter') or 0) + 1
    state['researchTopicCounter'] = n
    return f'topic-{n}'


def add_research_topic(state, topic, start_url, cadence_ms, now_ms=None,
                       link_keyword=None, page_keyword=None):
    """The creator side of the standing research-topic cadence: _check_schedules
    (below) has always been able to FIRE a due topic, but nothing ever appended
    one to state['researchTopics'] -- it was seeded empty at boot and never
    written to again. This is that missing write path.

    Fails closed rather than guessing: rejects an empty topic, a start_url that
    doesn't parse as a real absolute URL (scheme + host), and clamps cadence_ms
    to MIN_RESEARCH_CADENCE_MS so a bad interval can't turn into a queue-flood.
    Returns the new record, or None if rejected. `lastRunAt` starts at 0 so the
    first crawl fires on the very next _check_schedules pass, matching the
    intuitive "start checking X" request rather than waiting a full cadence."""
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
    }
    state.setdefault('researchTopics', []).append(record)
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


def _check_schedules(state, now, now_ms):
    """Port of tasks.js checkResearchSchedule + checkSkillReviewSchedule: queue
    due standing work, stamping lastRunAt/lastSkillReviewAt BEFORE assignment so
    a due topic isn't re-picked. Mutates state['researchTopics'] in place.

    The two standing ceremonies (skill review + distillation) are ALSO content-
    gated: a due cadence only queues work when there is actually something to
    review/merge. With no content the marker is deliberately NOT advanced, so
    the sweep fires on the first later pass where content appears."""
    # Research topics.
    for topic in (state.get('researchTopics') or []):
        if now_ms - (topic.get('lastRunAt') or 0) < topic.get('cadenceMs', 0):
            continue
        previous_run_at = topic.get('lastRunAt') or 0
        topic['lastRunAt'] = now_ms
        queue_work(state, [{
            'title': f"Scheduled research: {topic.get('topic')}",
            'room': 'observatory',
            'instructions': f'Crawl starting from {topic.get("startUrl")} and update the "{topic.get("topic")}" skill file with anything genuinely new since last time.',
            'goal': topic.get('topic'),
            'research': {'topicId': topic.get('id'), 'since': previous_run_at},
        }])
    # Skill review sweep. `_cadence_due` treats the explicit CADENCE_NEVER marker
    # as never-due and normalizes any legacy far-future TEST sentinel (1e18) that
    # leaked into live state, so the sweep can neither be muted by a real date nor
    # disabled by a ghost of the old test macro. Content-gated: nothing waiting in
    # pending_review/skills/ -> no task queued and the marker NOT advanced (the
    # sweep fires on a later pass the moment content appears).
    if _cadence_due(state, 'lastSkillReviewAt', SKILL_REVIEW_CADENCE_MS, now_ms=now_ms) \
            and _skill_review_has_pending():
        state['lastSkillReviewAt'] = now_ms
        queue_work(state, [{
            'title': 'Review pending skill files',
            'room': 'observatory',
            'instructions': 'Review whatever is waiting in pending_review/skills/ and decide, file by file, whether each one is accurate and worth keeping as real reference material.',
            'skillReview': True,
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
            }])

    # Director-gated player-ask / supervisor requirements-met sweeps. Unlike the
    # cadence markers above these run every _check_schedules pass: they spin a
    # PENDING gate (a blocked agent waiting on a verdict), which must resolve as
    # soon as its short window elapses -- not on a 30-minute cadence. Each no-ops
    # (returns None / False) when nothing is due, and only calls Jev when a gate
    # is actually old enough to decide.
    _pending_player_ask_sweep(state, now_ms)
    _supervisor_block_vote_sweep(state, now_ms)


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


def _has_firing_signal(state, agent_id):
    """Firing keys off a real, corroborated personnel
    problem, NOT the composite morale score. Morale folds in neglect (never
    contacted = low score) and approved-work noise that say 'send this agent
    help / they might be underutilized' -- the hiring/load-spreading concern --
    but are NOT grounds to fire someone. The legitimate firing signals are
    (a) a negative report filed against the agent (their peers flagged a real
    problem), and (b) a true drop-off -- they were handed work and dropped more
    of it than a third of what they approved (falling behind, not loafing)."""
    a = (state.get('agents') or {}).get(agent_id)
    if not a:
        return False
    info = _firing_consultation(state, agent_id)
    negative_reports = [r for r in info['reporters'] if _is_negative_severity(r['severity'])]
    if negative_reports:
        return True
    dropped = a.get('droppedCount') or 0
    approved = a.get('approvedCount') or 0
    return dropped > approved * 0.3


def _firing_signal_strength(state, agent_id):
    """Comparable strength of an agent's firing signal for who_needs_review:
    (severity-weighted negative report count, overload). Severe>serious>major
    (3/2/1); overload = dropped / (approved + 1), the same ratio _has_firing_signal
    thresholds at 0.3. Primary sort = the report evidence, secondary = how far
    behind they've fallen -- so a reported+overloaded agent beats a report-only
    one, and an unreported overload alone ranks below any real report."""
    info = _firing_consultation(state, agent_id)
    weights = {'severe': 3, 'serious': 2, 'major': 1}
    score = 0
    for r in info['reporters']:
        for sev in (r.get('severity') or '').split(','):
            score += weights.get(sev.strip().lower(), 0)
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
        if not _has_firing_signal(state, d.get('id')):
            continue
        strength = _firing_signal_strength(state, d.get('id'))
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


def _consultation_blocks_firing(state, candidate_id, now_ms):
    """firing.js consultationBlocksFiring: two independent guardrails against
    premature firing -- an active collaborator, or no corroborated negative
    report (severe alone justifies; a single serious/major defers)."""
    info = _firing_consultation(state, candidate_id)
    if info['coworkers']:
        return True, info
    negatives = [r for r in info['reporters'] if _is_negative_severity(r['severity'])]
    if not negatives:
        return True, info
    severe = any(r['severity'] and any(s.strip().lower() == 'severe' for s in r['severity'].split(','))
                 for r in negatives)
    if severe:
        return False, info
    return len(negatives) < 2, info


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
    instructions = (f'{reviewer1.get("name")} and {reviewer2.get("name")} are jointly reviewing '
                    f'{candidate.get("name")}\'s ({candidate.get("role")}) performance. '
                    f'{reviewer1.get("name")} is the admin; {reviewer2.get("name")} is the senior-most '
                    f'director standing in for the admin. Morale score: {morale}/100. Approved work: '
                    f'{candidate.get("approvedCount")}. Dropped work: {candidate.get("droppedCount")}. '
                    f'Reports filed against them: {report_quotes}. {candidate.get("name")}\'s own manager has '
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
        info = _firing_consultation(state, candidate_def.get('id'))
        has_report = any(_is_negative_severity(r['severity']) for r in info['reporters'])
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
        return (1 if cid not in team_set and not cid_room_contrib else 0,  # stranger loses
                0 if cid in team_set else 1,                               # same-team first
                0 if cid_room_contrib else 1,                              # known-craft next
                0 if a.get('offDuty') else 1,
                a.get('busy') is True,
                roster_order(cid, roster))
    ordered_ids.sort(key=_idle_key)
    return ordered_ids[:2]


def roster_order(cid, roster):
    for i, d in enumerate(roster):
        if d.get('id') == cid:
            return i
    return 0


def _enter_peer_review(state, task, now_ms, preferred=None):
    """Move a deliverable task whose primary content work just finished into the
    peer-approval gate: status -> 'needs_review', pick two same-team reviewers,
    record the gate (approval counter + reviewer pair), and notify each reviewer
    (the 'tell your teammates' step, server-side at the trigger instant). Returns
    the _peerGate dict (or None if the task is misconfigured).

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
    for rid in reviewers:
        _append_mailbox((state.get('agents') or {}).get(rid, {}), {
            'kind': 'peer_review_request',
            'about': task.get('id'),
            'title': task.get('title'),
            'text': f'Work on "{task.get("title")}" is ready for your review. Please review it and approve or send it back.'})
        review_items.append({
            'title': f'Review: {task.get("title")}',
            'room': task.get('room'),
            'instructions': (f'Review the work for "{task.get("title")}" and approve it only if it is genuinely solid '
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


def _sim_notify_author(state, parent, reviewer_id):
    """On an 'actionable' verdict the gate re-opens: notify the author that their
    work was sent back for a fix (and who rejected it). The actual fix task is
    queued by the executor's queueFix; here we just file the mailbox note."""
    author = parent.get('assignedTo')
    if not author:
        return
    _append_mailbox((state.get('agents') or {}).get(author, {}), {
        'kind': 'peer_review_rejected',
        'about': parent.get('id'),
        'title': parent.get('title'),
        'text': f'A reviewer{(" (" + reviewer_id + ")") if reviewer_id else ""} sent your work on "{parent.get("title")}" back -- it needs a fix before it can close. Fix it and it will be reviewed again.'})


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
    if _maybe_escalate_stuck_gate(task, gate, 'quality pipeline keeps failing'):
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
    name = _next_hire_name(state)
    if not name:
        state.pop('_pendingHire', None)
        return None
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
    # Defensive re-check: a director's team may have grown to cap between the
    # hire's approval and this completion pass; never blow past the cap. The
    # hire itself adds one member, so the check accounts for that extra.
    if not _team_under_cap(state, director_id, extra=1):
        state.pop('_pendingHire', None)
        return None
    roster.append({
        'id': new_id, 'name': name, 'color': color, 'role': role, 'model': 'small',
        'director': director_id,   # -> resolves into the team for _derive_team_members
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
    """A deterministic-ish unique first name for a hire. The JS uses an LLM to
    generate + validate a real unused name; here (no network in sim.py) we take
    a small pool of ordinary names not already in the roster, else None."""
    pool = ['maya', 'leo', 'zara', 'owen', 'lyra', 'ida', 'vela']
    used = {d.get('name', '').lower() for d in (state.get('agentRoster') or [])}
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
    _NEW_TEAM_NAME_POOL. Never collides with the existing roster (including
    names already taken from the hire pool). Returns None when exhausted."""
    used = {d.get('name', '').lower() for d in (state.get('agentRoster') or [])}
    for n in _NEW_TEAM_NAME_POOL:
        if n not in used:
            return n
    return None


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


def spawn_new_team_for_request(state, goal, now_ms=None, admin_id=None, employees=None):
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
    rather than waiting for the weekly cadence. Returns the new team record, or
    None if the name pool / agent caps are exhausted."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    roster = state.get('agentRoster') or []
    if len(roster) >= MAX_TOTAL_AGENTS:
        return None
    admin_id = admin_id or next((d.get('id') for d in roster if d.get('isAdmin')), None)
    if not admin_id:
        return None
    director_name = _next_new_team_name(state)
    if not director_name:
        return None
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
        emp_name = _next_new_team_name(state)
        if not emp_name:
            break
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
        # Stage 2 is fully drafted; move into the readiness hold. Stamp when the
        # hold began so the timeout is measured from here, not from hire time.
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
               description='', story_points=None, title=None, now_ms=None):
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
        scrum_master_id = t.get('scrumMasterId') or None
        if not scrum_master_id:
            continue  # no SM designated yet -- defer
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
    if (completed_task.get('taskType') or 'code') in ('spike', 'bug'):
        return
    if _room_backlog_count(state, completed_task['room']) > WORK_REQUEST_ROOM_THIN:
        return
    room_label = completed_task['room']
    title = f"Follow-up: further {room_label} work after completing '{completed_task.get('title') or 'previous task'}'"
    file_work_request(
        state, agent_id, title, room_label,
        reason=f"Completed '{completed_task.get('title') or 'previous task'}' in {room_label} and the remaining backlog there has thinned to {_room_backlog_count(state, room_label)} item(s); this room can absorb another story. {ABSOLUTE_ZERO_SCOPING_GUIDANCE}")


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


def _refinement_attendees(state, req_ids, scrum_master_id):
    """The ceremony's attendees: the scrum master + every AGENT who FILED a
    pending request this round (req_ids are work-request ids; the filers are
    resolved through the backlogRequests records). Non-agent filers (e.g. the
    player filing a JIRA issue) cannot attend a room ceremony and must not
    stall it -- they are dropped from the attendee list, and the scrum master
    still grooms their card. Only a BUSY attendee defers the whole meeting so
    we never pull an agent out of a live collaboration; an off-duty attendee
    is parked and free, and _start_refinement wakes her (snapshot + restore,
    exactly like the Social's off-duty Hangout attendees)."""
    agents = state.get('agents') or {}
    filer_ids = []
    for rid in req_ids:
        r = next((x for x in (state.get('backlogRequests') or []) if x.get('id') == rid), None)
        if r and r.get('filedBy') and r['filedBy'] not in filer_ids \
                and r['filedBy'] in agents:
            filer_ids.append(r['filedBy'])
    ids = [scrum_master_id] + [f for f in filer_ids if f != scrum_master_id]
    for aid in ids:
        a = agents.get(aid)
        if not a or a.get('busy'):
            return None
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
            {'id': 'reject', 'description': "Groom this request out -- it's a duplicate, low-value, trivial/filler, over-ambitious/un-scoped, out-of-scope, or the roadmap already covers it."},
        ]
        choice = decider(instructions, criteria)
        if choice is None:
            # Outage fallback: accept only a request that names a real
            # delegatable-room gap (same determinism as the governance fallback --
            # better to ship one well-scoped card than to silently drop a filed gap).
            choice = 'accept' if req.get('room') in VALUED_QUEUE_ROOMS else 'reject'
        if choice == 'accept':
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
            queue_work(state, [{
                'title': req['title'], 'room': req['room'], 'goal': req.get('title'),
                'instructions': instructions,
                'teamId': req.get('teamId'),
                'userStory': story,
                'acceptanceCriteria': criteria,
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
    # Restore attendees; reset the cadence stamp event-to-event. Like the Social,
    # the work budget given back is the full ceremony length, so the meeting
    # burned none of a mid-task attendee's work time.
    for aid, snap in (pending.get('people') or {}).items():
        _restore_refinement_agent(state, aid, snap, now_ms, REFINEMENT_MEET_MS)
    team_id = pending.get('teamId')
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
    ceremony is the point even on a quiet think tank). Per-team ceremonies now run
    CONCURRENTLY: each team has its OWN ceremony slot
    (`pendingRefinements[team_id]`), so one team no longer blocks another at the
    shared Command Center. A pass (a) advances every in-flight ceremony (embark
    on one pass, RELEASE on the next -- no required fixed hold), and (b) convenes
    a new ceremony for every team whose cadence is due AND has pending requests
    AND a free+on-duty scrum master and filers. Never convenes an empty meeting;
    defers a team if its scrum master or filers are busy/off-duty."""
    pending_map = state.setdefault('pendingRefinements', {})
    # (a) Advance every in-flight ceremony. Embark on one pass, then RELEASE on
    # the immediately-following pass -- the scrum master groomed everything
    # synchronously in the resolve, so there's no clock to wait out.
    for team_id in list(pending_map.keys()):
        pending = pending_map[team_id]
        if not pending.get('embarked'):
            _start_refinement(state, team_id, pending.get('reqIds', []),
                              pending.get('scrumMasterId'), now_ms)
        else:
            _resolve_refinement(state, pending, now_ms, decider=decider)
    # (b) Convene a ceremony for every team that is due and has groomable
    # requests and whose scrum master is free. Each team gets its own slot, so
    # multiple teams can refine in the same pass (spread apart in the room).
    slot = 0
    for team in (state.get('teams') or []):
        team_id = team.get('id') or team.get('directorId')
        if team_id in pending_map:
            continue  # this team already has an in-flight ceremony
        scrum_master_id = _refinement_scrum_master_for_team(state, team_id)
        if not scrum_master_id:
            continue  # this team has no (effective) scrum master designated yet
        if not _refinement_cadence_due_for(state, team_id, now_ms,
                                           legacy=state.get('lastBacklogRefinementAt')):
            continue
        # This team's OWN pending requests -- a ceremony must never groom a
        # request that isn't filed by one of THIS team's members (per-team
        # isolation: dev's scrum master refines only dev's cards).
        req_ids = []
        for r in _pending_work_requests(state):
            t = _refinement_team(state, r)
            if t and (t.get('id') == team_id or t.get('directorId') == team_id):
                req_ids.append(r['id'])
        if not req_ids:
            continue  # nothing to groom -- don't convene an empty meeting
        req_ids = req_ids[:REFINEMENT_MAX_REQUESTS]
        state.setdefault('teamRefinementAt', {})[team_id] = now_ms
        pending_map[team_id] = {
            'at': now_ms + REFINEMENT_MEET_MS, 'embarked': False,
            'scrumMasterId': scrum_master_id, 'reqIds': req_ids,
            'teamId': team_id, 'people': {},
        }
        slot += 1  # only to spread simultaneous ceremonies apart in the room


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
        _write_growth_plan(state, agent_id, room, 'low_grade', now_ms,
                           f"Deliverable '{title}' in {room} graded {grade}/10 (below the {DELIVERABLE_GRADE_FLOOR:.0f} floor).")


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


def _write_growth_plan(state, agent_id, room, kind, now_ms, note):
    """Record a coaching note against an agent (dedup by kind so repeated low
    grades don't spam identical plans). The note is APPENDED once to that
    agent's next assigned task by _coaching_note_for."""
    plans = state.setdefault('growthPlans', {}).setdefault(agent_id, [])
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
    task is revisiting existing work, not starting fresh."""
    pieces = [instructions] if instructions else []
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


def _roadmap_step(state, now_ms):
    """Weekly silent recompute: director-owned roadmap maps each room to a
    priority derived from trailing grades + recent delivery. A room that shipped
    poorly (low grade) or that agents keep flagging gets higher priority so fresh
    work lands where it's weakest. Pure derivation -- no ceremony, no Jev spend."""
    if now_ms - (state.get('lastRoadmapReviewAt') or 0) < ROADMAP_CADENCE_MS:
        return
    state['lastRoadmapReviewAt'] = now_ms
    roadmap = state.setdefault('roadmap', {})
    for room in VALUED_QUEUE_ROOMS:
        gm = _room_trailing_grade(state, room)
        demand = _roadmap_release_demand(state, room)
        prev = roadmap.get(room) or {}
        if gm is None and demand == 0:
            # No history yet -- leave whatever the director set, else default.
            if not prev:
                roadmap[room] = {'priority': 0, 'ownerId': prev.get('ownerId'),
                                 'lastGrade': None, 'demand': 0}
            continue
        # Weak grade and/or low delivery -> higher priority to rebalance.
        priority = 0
        if gm is not None and gm < DELIVERABLE_GRADE_FLOOR:
            priority += 2
        if demand == 0:
            priority += 1           # starved room -- feed it
        elif gm is not None and gm < 7:
            priority += 1
        roadmap[room] = {'priority': priority, 'ownerId': prev.get('ownerId'),
                         'lastGrade': gm, 'demand': demand}


def _refinement_context_for_room(state, room):
    """The roadmap context the refinement groom should weigh: the room's current
    roadmap priority + trailing grade, so a scrum master grooms high-priority /
    weak rooms preferentially. Returns a short string or None."""
    roadmap = (state.get('roadmap') or {}).get(room) or {}
    gm = _room_trailing_grade(state, room)
    bits = []
    if roadmap.get('priority'):
        bits.append(f"roadmap priority {roadmap['priority']}")
    if gm is not None:
        bits.append(f"trailing grade {gm}/10")
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
        agents.pop(pending['candidateId'], None)
        roster = state.get('agentRoster') or []
        state['agentRoster'] = [d for d in roster if d.get('id') != pending['candidateId']]
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

    # Reclaim a 'walking'/'working' task whose assignee no longer
    # holds it (the orphaned-fix wedge). Runs BEFORE the idle gate so a quiet
    # think tank that simply has a stale in-flight task re-issues it rather than
    # treating the think tank as done. Cheap: no-op unless such a task exists.
    _reclaim_orphaned_walking_tasks(state)

    # Weekly cross-team Knowledge Social: convene/resolve the 30-minute Hangout
    # conversation for eligible (week's-work) agents. Runs ungated -- the
    # conversation is the point even on an otherwise-idle think tank, and must not
    # be starved by the work gate below.
    _social_step(state, now, now_ms)

    # Backlog refinement: the scrum master grooms agent-filed work-requests into
    # real stories at the Command Center on a weekly cadence. Runs ungated like
    # the Social (the ceremony is the point even on a quiet think tank) but no-ops
    # when there are no pending requests or no scrum master to run it.
    _refinement_step(state, now, now_ms)

    # Cut 2 roadmap: weekly silent recompute of per-room priority from trailing
    # deliverable grades + delivery volume. Cheap (state-only, no ceremony, no
    # Jev spend); runs before the gate so a quiet think tank still keeps its
    # roadmap current for the next grooming.
    _roadmap_step(state, now_ms)

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
                _apply_content_result(state, task, result)
                # Fail-closed quality gate: a content result that FAILED the
                # quality pipeline (the coding executor stores ok=False on a red
                # flake8/mypy/bandit/pytest-cov run) must NEVER advance to peer
                # review or re-arm a gate. A review executor always reports
                # ok=True (its verdict rides in peerVerdict), so this check never
                # disturbs a normal gate vote.
                result_ok = bool(result.get('ok', True))
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

    # Assignment loop (bounded by roster size, like the JS runTaskCycleBody).
    roster = state.get('agentRoster') or []
    roster_size = sum(1 for d in roster if not d.get('isAdmin'))
    attempted = set()
    for _ in range(max(1, roster_size)):
        due_index = pick_next_due_index(work_queue, now_ms, attempted)
        if due_index == -1:
            break
        pick = work_queue[due_index]
        attempted.add(id(pick))
        # A peer-approval review/fix subtask is pinned to a specific agent (its
        # reviewer/author) and must reach them even when the generic wake rule
        # (wake only enough agents to keep one awake-idle) would skip the second
        # of a pair of off-duty reviewers. So a reviewOf item is ALWAYS
        # wakeable -- its pinned agent is woken inside _assign_due_item.
        _pinned_review = bool(pick.get('reviewOf'))
        can_wake_off_duty = (_pinned_review or bool(pick.get('notBefore')) or _awake_idle_count(state) == 0) \
            and can_activate_another(state)
        if _awake_idle_count(state) == 0 and not (can_wake_off_duty and _any_available_including_off_duty(state)):
            break  # nobody who could take this right now, of any kind
        work_queue.pop(due_index)
        assigned = _assign_due_item(state, pick, can_wake_off_duty, grid, doors, now_ms, task_id_holder)
        if not assigned:
            pick['attempts'] = (pick.get('attempts') or 0) + 1
            # A peer-approval review/fix subtask BLOCKS its whole story: never
            # abandon it on the normal attempt cap (which exists to shed stray
            # normal work). It retries until a reviewer is assignable; the
            # needs_review timeout is the safety net if a reviewer never can be.
            if pick.get('reviewOf') or pick['attempts'] < WORK_ITEM_MAX_ATTEMPTS:
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


def _assign_due_item(state, pick, can_wake_off_duty, grid, doors, now_ms, task_id_holder=None):
    """Deterministic assignment of one due queue item: pick the most-idle
    eligible candidate (round-robin, zero JEV spend), wake her if off-duty,
    assign. Returns the task dict or None. Mirrors assignTaskViaJev's
    fallback-to-first-eligible but replaces JEV with a deterministic pick."""
    agents = state.get('agents') or {}
    candidates = _eligible_candidates(state, can_wake_off_duty)
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
    pinned = pick.get('assignedTo') if (pick.get('reviewOf') or pick.get('incident')
                                         or pick.get('_mailResume') or pick.get('directRoute')) else None
    if pinned and agents.get(pinned):
        pinned_agent = agents.get(pinned)
        if not pinned_agent.get('busy') and not pinned_agent.get('task') and not pinned_agent.get('pairWith'):
            chosen_id = pinned
            if pinned_agent.get('offDuty'):
                # Wake a reviewer at the REVIEW TARGET'S OWN door approach: the reviewer
                # is judging the author's work at that desk, so placing her at
                # the room's own door gives a short, reliable path.
                _spawn_at_room_door(state, pinned, pick.get('room'), agents, grid, doors)
        elif pinned in candidates:
            chosen_id = pinned
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
        idx = pointer % len(ordered)
        chosen_id = ordered[idx]
        rr['task'] = (pointer + 1) % max(1, len(ordered))
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
        # Phase E2b: a SPIKE's advisory time-budget (the executor keeps its
        # single model call short to honor it).
        'budgetMs': pick.get('budgetMs'),
        'reviewOf': pick.get('reviewOf'),
        'reviewAuthorId': pick.get('reviewAuthorId'),
        # Phase E2d: an INCIDENT routes to the owning team's on-call (taskType
        # 'bug' + productId + assignedTo travel so the fix is attributable and
        # the cap can be enforced against the open task set).
        'incident': bool(pick.get('incident')),
        'productId': pick.get('productId'),
        # A player-filed card's contract survives onto the task (see assign_task).
        'userStory': pick.get('userStory'),
        'acceptanceCriteria': pick.get('acceptanceCriteria'),
    }
    # Cut 2 coaching + runbook injection: append the chosen agent's pending
    # growth-plan note (applied once) and any product runbook knowledge to the
    # task's instructions -- manager feedback / prior-incident learning changes
    # THIS task's execution.
    instructions = _augment_task_instructions(
        state, chosen_id, pick.get('productId'), pick.get('instructions'),
        refocus=bool(pick.get('reviewOf') or pick.get('incident')))
    return assign_task(state, chosen_id, pick.get('title'), pick.get('room'),
                       instructions, pick.get('projectLabel') or pick.get('goal'), extra,
                       grid, doors, now_ms, task_id_holder or _TASK_ID_HOLDER)