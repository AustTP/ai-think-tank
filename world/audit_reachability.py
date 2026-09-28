"""Reachability audit v2: measure what find_path ACTUALLY returns.

The naive audit (compute_reachable_mask) is misleading: find_path relaxes the
target to the nearest free cell (TARGET_RELAX_RADIUS=3 ring), so it reaches
door approach points even when the door's literal center cell can't fit the
agent box. The real, observable cost is: from a realistic agent spawn, can
find_path produce a route to each room door? That's what the sim actually
blocks on. This measures that directly.

For each test agent we spawn at a pick_free_spot result (the hiring.js
mechanism) and attempt find_path to every room door center. We report, per
room, how many spawns failed to reach it -- the per-door unreachability
fraction, across settings that mimic the roster.
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim  # noqa: E402


def spawn_a_batch(grid, n, rng):
    """n spawn points on reachable floor, spaced apart (mimics pickFreeSpot
    with occupied avoidance)."""
    pts = []
    for _ in range(n * 20):
        p = sim.pick_free_spot(
            grid, avoid_points=pts, rnd=rng.random,
            spawn=sim.SPAWN)
        if (p["x"], p["y"]) != (sim.SPAWN["x"], sim.SPAWN["y"]):
            pts.append(p)
        if len(pts) >= n:
            break
    return pts if len(pts) >= n else pts + [{"x": sim.SPAWN["x"], "y": sim.SPAWN["y"]}] * (n - len(pts))


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    grid = json.load(open(os.path.join(root, "collision_grid.json")))
    doors = json.load(open(os.path.join(root, "door_triggers.json")))
    n = 24  # ~ roster headcount
    rng = random.Random(20260921)
    spawns = spawn_a_batch(grid, n, rng)

    # door world targets
    targets = {}
    for b, r in doors.items():
        targets[b] = ((r["x"] + r["w"] / 2) * sim.SCALE,
                      (r["y"] + r["h"] / 2) * sim.SCALE)

    print(f"spawning {len(spawns)} agents on reachable floor "
          f"(mimics hiring.js pickFreeSpot)\n")
    fails = {b: 0 for b in doors}
    for si, (sx, sy) in enumerate([(p["x"], p["y"]) for p in spawns]):
        row = []
        for b, (tx, ty) in targets.items():
            p = sim.find_path(sx, sy, tx, ty, None, {}, grid)
            ok = p is not None
            if not ok:
                fails[b] += 1
            row.append("." if ok else "X")
        print(f"  spawn {si:2d} ({sx:5.0f},{sy:5.0f}): {''.join(row)}")

    print("\nper-room reachability across all spawns:")
    n_bad = 0
    for b in doors:
        f = fails[b]
        frac = 100.0 * f / len(spawns)
        if f:
            n_bad += 1
        print(f"  {b:18s} {len(spawns)-f}/{len(spawns)} reachable "
              f"({frac:.0f}% fail)")
    print(f"\nSUMMARY: {n_bad}/{len(doors)} rooms have >=1 spawn that "
          f"can't path to them.")
    return 0


if __name__ == "__main__":
    main()