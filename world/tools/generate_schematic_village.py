"""Generate a schematic-native village: collision grid + door triggers.

Layout vocabulary (native px, cell=8; world = *2):
  - Bordered map: walls on all four edges; the north gate is a road opening.
  - Roads: 3-cell-wide corridors; one plaza; buildings as solid footprints.
  - Every building has a door gap on a road-facing wall; its trigger rect
    sits on the walkable ground directly in front of the gap.
  - Flood-fill from spawn must reach every trigger, or generation fails.

Usage: python3 gen_village.py <out_dir>
Writes collision_grid_south.json and door_triggers_south.json.
"""
import json
import sys
from collections import deque

COLS, ROWS, CELL = 86, 48, 8

# Buildings: (room_id, col0, col1, row0, row1, door face, gap cells)
# Faces: 'S' = gap in the bottom wall, trigger on the road below;
#        'N' = gap in the top wall, trigger on the road above;
#        'W' = gap in the left wall, trigger on the ground to the left.
BUILDINGS = [
    ('pressoffice',    8, 17,  9, 16, 'S', (12, 13)),
    ('media',         24, 33,  9, 16, 'S', (28, 29)),
    ('library',       40, 49,  9, 16, 'S', (44, 45)),
    ('townhall',      56, 65,  9, 16, 'S', (60, 61)),
    ('house',         68, 75,  9, 16, 'S', (71, 72)),
    ('observatory',   10, 19, 33, 40, 'N', (14, 15)),
    ('bank',          26, 35, 33, 40, 'N', (30, 31)),
    ('postoffice',    42, 51, 33, 40, 'N', (46, 47)),
    ('weatherstation', 58, 67, 33, 40, 'N', (62, 63)),
    ('phonebooth',    66, 67, 24, 26, 'W', (24, 25)),
]

# Open spaces are legacy of the road-and-plaza draft; the schematic village
# is open ground with building footprints, so nothing needs carving here.
ROADS = []

# GATE/SPAWN in NATIVE px. scenes.json carries the same two in WORLD px
# (x2): gate 584,0,112,20 world == 292,0,56,10 native; spawn 630,68 world
# == 315,34 native. Door triggers are native too (rooms.js scales them).
GATE = {'x': 292, 'y': 0, 'w': 56, 'h': 10}       # cols 36-43, rows 0-1
SPAWN = {'x': 315, 'y': 34}                       # below the gate, facing in


def build_grid():
    # Open ground by default; the only solid cells are the boundary walls,
    # the building footprints, and (via the door gaps) nothing else. Door
    # gaps stay open so each building's interior-facing gap leads out.
    grid = [[0] * COLS for _ in range(ROWS)]
    for _name, c0, c1, r0, r1, face, gap in BUILDINGS:
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                grid[r][c] = 1
        gc0, gc1 = gap
        if face == 'S':
            for c in range(gc0, gc1 + 1):
                grid[r1][c] = 0
        elif face == 'N':
            for c in range(gc0, gc1 + 1):
                grid[r0][c] = 0
        else:  # 'W'
            for r in range(gc0, gc1 + 1):
                grid[r][c0] = 0
    # Border walls last (the gate re-opens the north edge).
    for c in range(COLS):
        grid[0][c] = grid[ROWS - 1][c] = 1
    for r in range(ROWS):
        grid[r][0] = grid[r][COLS - 1] = 1
    gc0 = GATE['x'] // CELL
    gc1 = min((GATE['x'] + GATE['w'] - 1) // CELL, COLS - 1)
    gr1 = max((GATE['y'] + GATE['h'] - 1) // CELL, 1)  # rows 0-1 minimum
    for r in range(0, gr1 + 1):
        for c in range(gc0, gc1 + 1):
            grid[r][c] = 0
    return grid


def triggers():
    out = {}
    for name, c0, c1, r0, r1, face, gap in BUILDINGS:
        gc0, gc1 = gap
        if face == 'S':
            rect = {'x': gc0 * CELL, 'y': (r1 + 1) * CELL, 'w': 2 * CELL,
                    'h': 2 * CELL}
        elif face == 'N':
            rect = {'x': gc0 * CELL, 'y': (r0 - 3) * CELL, 'w': 2 * CELL,
                    'h': 2 * CELL}
        else:
            rect = {'x': (c0 - 2) * CELL, 'y': gc0 * CELL, 'w': 2 * CELL,
                    'h': 2 * CELL}
        out[name] = rect
    return out


def verify(grid, doors):
    """Flood-fill from spawn: every door trigger must sit on reachable open
    ground, and every trigger cell must be open (the player walks ONTO it)."""
    start = (SPAWN['x'] // CELL, SPAWN['y'] // CELL)
    if grid[start[1]][start[0]]:
        raise SystemExit(f'spawn cell {start} is blocked')
    seen = {start}
    q = deque([start])
    while q:
        x, y = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < COLS and 0 <= ny < ROWS and not grid[ny][nx] \
                    and (nx, ny) not in seen:
                seen.add((nx, ny))
                q.append((nx, ny))
    problems = []
    for name, d in doors.items():
        x0, y0 = d['x'] // CELL, d['y'] // CELL
        x1, y1 = (d['x'] + d['w'] - 1) // CELL, (d['y'] + d['h'] - 1) // CELL
        for yy in range(y0, y1 + 1):
            for xx in range(x0, x1 + 1):
                if grid[yy][xx]:
                    problems.append(f'{name}: trigger cell ({xx},{yy}) blocked')
                elif (xx, yy) not in seen:
                    problems.append(f'{name}: trigger cell ({xx},{yy}) unreachable')
    if problems:
        raise SystemExit('\n'.join(problems))
    return len(seen)


def render_ascii(grid, doors):
    canvas = [['.' if v == 0 else '#' for v in row] for row in grid]
    for name, d in doors.items():
        for yy in range(d['y'] // CELL, (d['y'] + d['h'] - 1) // CELL + 1):
            for xx in range(d['x'] // CELL, (d['x'] + d['w'] - 1) // CELL + 1):
                canvas[yy][xx] = name[0].upper()
    for yy in range(0, (GATE['y'] + GATE['h'] - 1) // CELL + 1):
        for xx in range(GATE['x'] // CELL,
                        min((GATE['x'] + GATE['w'] - 1) // CELL, COLS - 1) + 1):
            canvas[yy][xx] = 'G'
    canvas[SPAWN['y'] // CELL][SPAWN['x'] // CELL] = 'S'
    lines = ['   ' + ''.join(str(c // 10 % 10) for c in range(COLS)),
             '   ' + ''.join(str(c % 10) for c in range(COLS))]
    for y, row in enumerate(canvas):
        lines.append(f'{y:2d} ' + ''.join(row))
    return '\n'.join(lines)


def generate(name, out_dir):
    """Generate one schematic-native village named `name`: verify layout
    reachability, then write collision_grid_<name>.json and
    door_triggers_<name>.json into out_dir. Returns the two file paths."""
    grid = build_grid()
    doors = triggers()
    reachable = verify(grid, doors)
    grid_path = f'{out_dir}/collision_grid_{name}.json'
    doors_path = f'{out_dir}/door_triggers_{name}.json'
    with open(grid_path, 'w') as f:
        json.dump({'cols': COLS, 'rows': ROWS, 'cell': CELL, 'grid': grid}, f)
    with open(doors_path, 'w') as f:
        json.dump(doors, f, indent=2)
    return grid_path, doors_path, reachable


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else '.'
    grid_path, doors_path, reachable = generate('south', out_dir)
    print(f'reachability OK: spawn reaches {reachable} cells; '
          f'all door triggers open and reachable')
    print(render_ascii(build_grid(), triggers()))
    print(f'wrote {grid_path}, {doors_path}')


if __name__ == '__main__':
    main()
