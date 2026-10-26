#!/usr/bin/env python3
"""Build the south village map data from the current village art.

The owner asked for a second village reached by the unused south path. PixelLab
art generation is metered and gated on an explicit budget approval, so this
script derives a distinct, zero-cost placeholder from the existing map:

* background: the current village mirrored horizontally, with a new dirt road
  cut from the north edge down to the central road (the arrival seam);
* collision grid: the same mirror, with that road carved walkable and the north
  edge opened where the road crosses it;
* door triggers: the same mirror, so every building's door still lands on its
  painted door.

Run from the repo root:  python3 scripts/build_south_map.py
It rewrites world/assets/think_tank_south.png, world/collision_grid_south.json,
and world/door_triggers_south.json. It does not touch the main map.
"""
import json
import os
import random

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORLD = os.path.join(ROOT, 'world')
NATIVE_W, NATIVE_H = 688, 384
CELL = 8

# The north arrival road, in native pixels. The main map's south outlet path
# crosses the bottom edge at x 448..536; the south map receives the player at
# its own north edge, so the road runs from the top down to the central road.
ROAD_X0, ROAD_X1 = 292, 348
ROAD_BOTTOM = 214
PLAZA_Y = 46


def _mirror_rect(rect):
    return {'x': NATIVE_W - (rect['x'] + rect['w']), 'y': rect['y'],
            'w': rect['w'], 'h': rect['h']}


def build_doors():
    with open(os.path.join(WORLD, 'door_triggers.json'), encoding='utf-8') as fh:
        main = json.load(fh)
    out = {name: _mirror_rect(rect) for name, rect in main.items() if name != 'south_outlet'}
    # The south arrival/return gate sits on the north edge road. It is handled
    # by the scene transition, not by the room-entry loop, so keep it out of
    # this file: world.js owns SOUTH_GATE in world coordinates. Mirroring the
    # main map's south_outlet would put a second selector on the south map's
    # bottom edge and let the player "travel" to the map they are already in.
    out.pop('south_outlet', None)
    return out


def build_grid():
    with open(os.path.join(WORLD, 'collision_grid.json'), encoding='utf-8') as fh:
        main = json.load(fh)
    cols, rows = main['cols'], main['rows']
    grid = [list(row) for row in main['grid']]
    # Mirror columns to match the mirrored background.
    grid = [list(reversed(row)) for row in grid]
    # Open the north border and carve the arrival road down to the central road.
    x0, x1 = ROAD_X0 // CELL, ROAD_X1 // CELL
    y_bottom = ROAD_BOTTOM // CELL
    for y in range(0, y_bottom + 1):
        for x in range(x0, x1 + 1):
            grid[y][x] = 0
    # A little arrival plaza so the gate does not feel like a corridor mouth.
    for y in range(0, PLAZA_Y // CELL):
        for x in range(x0 - 2, x1 + 3):
            if 0 <= x < cols:
                grid[y][x] = 0
    return {'cols': cols, 'rows': rows, 'cell': main['cell'], 'grid': grid}


def _path_patch(src):
    """A clean stretch of the main road, tiled to paint the new road."""
    return src.crop((330, 190, 370, 230))


def build_background():
    src = Image.open(os.path.join(WORLD, 'assets', 'think_tank_background.png')).convert('RGB')
    out = src.transpose(Image.FLIP_LEFT_RIGHT)
    patch = _path_patch(src)
    rnd = random.Random(51)
    # Paint the road with the sampled dirt, with a soft darker edge so it
    # reads as a path rather than a rectangle.
    for y in range(0, ROAD_BOTTOM + 1):
        t = y / max(1, ROAD_BOTTOM)
        half = int((ROAD_X1 - ROAD_X0) * (0.9 + 0.25 * (1 - t))) // 2
        cx = (ROAD_X0 + ROAD_X1) // 2 + int(3 * (t * t - t))
        left, right = cx - half, cx + half
        for x in range(left, right + 1):
            if not (0 <= x < NATIVE_W and 0 <= y < NATIVE_H):
                continue
            color = patch.getpixel(((x - left) % 40, y % 40))
            jitter = rnd.randint(-6, 6)
            out.putpixel((x, y), tuple(max(0, min(255, c + jitter)) for c in color))
        for edge in (left - 1, right + 1):
            if 0 <= edge < NATIVE_W:
                r, g, b = out.getpixel((edge, y))
                out.putpixel((edge, y), (max(0, r - 28), max(0, g - 24), max(0, b - 12)))
    out.save(os.path.join(WORLD, 'assets', 'think_tank_south.png'))
    return out


def main():
    build_background()
    with open(os.path.join(WORLD, 'collision_grid_south.json'), 'w', encoding='utf-8') as fh:
        json.dump(build_grid(), fh, separators=(',', ':'))
    with open(os.path.join(WORLD, 'door_triggers_south.json'), 'w', encoding='utf-8') as fh:
        json.dump(build_doors(), fh, indent=2)
        fh.write('\n')
    print('wrote think_tank_south.png, collision_grid_south.json, door_triggers_south.json')


if __name__ == '__main__':
    main()
