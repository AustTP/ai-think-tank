"""Deterministic geometry for a second village map.

The background may be generated (PixelLab), but collision and doors stay
exact: they are derived from the main map by mirror or copy, then the north
arrival road is carved open. Keeping this in one module means the standalone
builder and the server tool produce byte-identical JSON.
"""
from __future__ import annotations

import json
from pathlib import Path

NATIVE_W, NATIVE_H = 688, 384
CELL = 8
ROAD_X0, ROAD_X1 = 292, 348
ROAD_BOTTOM = 214
PLAZA_Y = 46
SOUTH_OUTLET = 'south_outlet'
SPAWN = {'x': 320 * 2 - 10, 'y': 34 * 2}
GATE = {'x': ROAD_X0 * 2, 'y': 0, 'w': (ROAD_X1 - ROAD_X0) * 2, 'h': 10 * 2}


def mirror_rect(rect: dict) -> dict:
    return {'x': NATIVE_W - (rect['x'] + rect['w']), 'y': rect['y'],
            'w': rect['w'], 'h': rect['h']}


def mirror_doors(main_doors: dict, mirror: bool = True,
                 drop: tuple[str, ...] = (SOUTH_OUTLET,)) -> dict:
    out = {}
    for name, rect in main_doors.items():
        if name in drop:
            continue
        out[name] = mirror_rect(rect) if mirror else dict(rect)
    return out


def mirror_grid(main_grid: dict, mirror: bool = True) -> dict:
    cols, rows = main_grid['cols'], main_grid['rows']
    grid = [list(row) for row in main_grid['grid']]
    if mirror:
        grid = [list(reversed(row)) for row in grid]
    x0, x1 = ROAD_X0 // CELL, ROAD_X1 // CELL
    y_bottom = ROAD_BOTTOM // CELL
    for y in range(0, y_bottom + 1):
        for x in range(x0, x1 + 1):
            if 0 <= x < cols and 0 <= y < rows:
                grid[y][x] = 0
    for y in range(0, PLAZA_Y // CELL):
        for x in range(x0 - 2, x1 + 3):
            if 0 <= x < cols and 0 <= y < rows:
                grid[y][x] = 0
    return {'cols': cols, 'rows': rows, 'cell': main_grid['cell'], 'grid': grid}


def load_main(world_dir: Path) -> tuple[dict, dict]:
    with open(Path(world_dir) / 'collision_grid.json', encoding='utf-8') as fh:
        grid = json.load(fh)
    with open(Path(world_dir) / 'door_triggers.json', encoding='utf-8') as fh:
        doors = json.load(fh)
    return grid, doors


def build(world_dir: Path, mirror: bool = True) -> tuple[dict, dict]:
    grid, doors = load_main(world_dir)
    return mirror_grid(grid, mirror), mirror_doors(doors, mirror)
