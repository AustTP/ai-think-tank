#!/usr/bin/env python3
"""Test bridge for the movement-parity harness (tests/test_movement_parity.mjs).

Reads one JSON request on stdin, runs the requested pure function in sim.py,
prints one JSON result on stdout. The JS harness feeds the real JS engine and
this Python engine IDENTICAL inputs and diffs bit-for-bit.

Request shapes:
  { "op": "blocked_at", "grid": G, "box": {x,y,w,h} }            -> bool
  { "op": "cell_fits_agent", "grid": G, "gx": n, "gy": n }       -> bool
  { "op": "is_on_door_tile", "doors": D, "x": n, "y": n }        -> bool
  { "op": "agent_blocked_at", "agents": A, "box": {..},
    "exclude": id|null, "ignore": [ids], "doors": D }            -> bool
  { "op": "reachable_mask", "grid": G, "spawn": {x,y} }          -> [[bool]]
  { "op": "walk", "dt": n, "agents": A, "grid": G, "doors": D,
    "steps": n }                                                 -> { "agents": A, "events": [[kind,id]] }
    (find_path is called internally, same as the JS engine; walk is
     deterministic -- respawn pick_free is stubbed to the fixed
     {"x":0,"y":0} so no RNG divergence is possible.)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim


def build_doors(doors_maybe_native_scaled, scale=None):
    """doors passed from JS is already the ROOM_DOOR_TRIGGERS shape
    (world-space, *SCALE'd). Sim uses it verbatim."""
    return doors_maybe_native_scaled


def main():
    req = json.load(sys.stdin)
    op = req['op']
    grid = req.get('grid')
    if op == 'blocked_at':
        print(json.dumps(sim._blocked_at(req['box'], grid)))
    elif op == 'cell_fits_agent':
        print(json.dumps(sim.cell_fits_agent(req['gx'], req['gy'], grid['cell'], grid)))
    elif op == 'is_on_door_tile':
        print(json.dumps(sim._is_on_door_tile(req['x'], req['y'], req.get('doors'))))
    elif op == 'agent_blocked_at':
        print(json.dumps(sim.agent_blocked_at(
            req['box'], req['agents'], req.get('exclude'),
            set(req.get('ignore') or []), req.get('doors'))))
    elif op == 'reachable_mask':
        mask = sim.compute_reachable_mask(grid, spawn=req.get('spawn'))
        print(json.dumps(mask))
    elif op == 'walk':
        # Deterministic: stub pick_free to a fixed point so respawn can never
        # draw real RNG. findPath runs on the given grid exactly as the JS.
        agents = req['agents']
        doors = req.get('doors')
        events_all = []
        for _ in range(req['steps']):
            ev = sim.step_agent_movement(
                req['dt'], agents, grid, doors=doors,
                pick_free=lambda avoid, ag_map: {'x': 0.0, 'y': 0.0},
            )
            events_all.extend(ev)
        print(json.dumps({'agents': agents, 'events': events_all}))
    else:
        raise SystemExit(f'unknown op {op}')


if __name__ == '__main__':
    main()