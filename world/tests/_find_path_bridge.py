#!/usr/bin/env python3
"""Test bridge for the find-path parity harness (tests/test_find_path_parity.mjs).

Reads ONE JSON request PER LINE on stdin (so one interpreter serves many cases
-- spawning a fresh Python per case made the 150-case battery shell out ~150
interpreters and took ~20s), runs sim.find_path on each, prints one JSON result
per line on stdout.

Request line:
  { "start": [x, y], "target": [x, y], "exclude": id,
    "agents": {id: {x, y, visible, pathTarget|null}}, "grid": {...} }
Result line: either null, or a list of {"x":..., "y":...} waypoints.

Floats round-trip through Python json with the shortest repr that recovers the
same IEEE-754 double; the JS side parses this back to identical doubles.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim


def handle(req):
    return sim.find_path(
        req['start'][0], req['start'][1], req['target'][0], req['target'][1],
        req['exclude'], req['agents'], req['grid'],
    )


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        result = handle(json.loads(line))
        print('null' if result is None else json.dumps(result), flush=True)


if __name__ == '__main__':
    main()