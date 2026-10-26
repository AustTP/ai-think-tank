"""The south village's map data and the sim's per-village loader.

The second village is a separate scene with its own collision grid and door
triggers. The sim must be able to load any village's JSON without changing
movement physics, and the main village's geometry must stay exactly as it was.
"""
import json
import os
import unittest
from collections import deque

import sim

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORLD = os.path.join(ROOT, 'world')


def _json(name):
    with open(os.path.join(WORLD, name), encoding='utf-8') as fh:
        return json.load(fh)


def _reachable_cells(grid, start):
    cols, rows, cells = grid['cols'], grid['rows'], grid['grid']
    seen = {start}
    queue = deque([start])
    while queue:
        x, y = queue.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < cols and 0 <= ny < rows and (nx, ny) not in seen and not cells[ny][nx]:
                seen.add((nx, ny))
                queue.append((nx, ny))
    return seen


class SouthVillageData(unittest.TestCase):
    def test_main_geometry_is_unchanged(self):
        grid, doors = sim._load_outdoor_geometry()
        self.assertEqual((grid['cols'], grid['rows']), (86, 48))
        self.assertIn('pressoffice', doors)
        self.assertIn('south_outlet', doors, 'the main map gains the south outlet')

    def test_south_geometry_loads_from_its_own_files(self):
        grid, doors = sim._load_outdoor_geometry('south')
        self.assertEqual((grid['cols'], grid['rows']), (86, 48))
        self.assertIn('pressoffice', doors)
        self.assertNotIn('south_outlet', doors, 'the south map returns through its north gate')

    def test_south_doors_are_mirrored_from_main(self):
        _grid, main_doors = sim._load_outdoor_geometry()
        _grid, south_doors = sim._load_outdoor_geometry('south')
        for building in ('pressoffice', 'media', 'library', 'townhall'):
            main = main_doors[building]
            south = south_doors[building]
            scaled_width = 688 * 2
            self.assertEqual(south['x'], scaled_width - (main['x'] + main['w']))
            self.assertEqual(south['y'], main['y'])

    def test_map_builder_does_not_mirror_the_main_outlet(self):
        import importlib.util
        path = os.path.join(os.path.dirname(ROOT), 'scripts', 'build_south_map.py')
        spec = importlib.util.spec_from_file_location('build_south_map', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        doors = mod.build_doors()
        self.assertIn('pressoffice', doors)
        self.assertNotIn('south_outlet', doors,
                         're-running the builder must not add a second selector south')

    def test_unknown_village_falls_back_to_main(self):
        main_grid, _ = sim._load_outdoor_geometry()
        fallback_grid, _ = sim._load_outdoor_geometry('not-a-village')
        self.assertEqual(fallback_grid, main_grid)

    def test_south_arrival_road_and_buildings_are_reachable(self):
        grid = sim._load_outdoor_geometry('south')[0]
        cell = grid['cell']
        spawn = (320 // cell, 24 // cell)
        self.assertEqual(grid['grid'][spawn[1]][spawn[0]], 0, 'spawn is walkable')
        reachable = _reachable_cells(grid, spawn)
        self.assertGreater(len(reachable), 500)
        # The north gate corridor is open at the top edge.
        for x in range(292 // cell, 348 // cell + 1):
            self.assertIn((x, 0), reachable, 'the arrival road crosses the north edge')

    def test_every_south_door_tile_sits_near_walkable_ground(self):
        grid = sim._load_outdoor_geometry('south')[0]
        doors = sim._load_outdoor_geometry('south')[1]
        cell = grid['cell']
        for building, rect in doors.items():
            # Door rects are scaled by 2; convert back to native grid cells.
            cx = int((rect['x'] + rect['w'] / 2) / 2 / cell)
            cy = int((rect['y'] + rect['h'] / 2) / 2 / cell)
            near = [(cx + dx, cy + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1, 2)]
            self.assertTrue(
                any(0 <= x < grid['cols'] and 0 <= y < grid['rows'] and not grid['grid'][y][x]
                    for x, y in near),
                f'{building} door has no walkable approach')


if __name__ == '__main__':
    unittest.main()
