#!/usr/bin/env python3
"""Test bridge: evaluate sim.find_path on a JSON request and print the JSON
result, so the JS parity harness (test_find_path_parity.mjs) can feed the JS
and Python engines identical inputs and compare outputs bit-for-bit.

Stdin: one JSON object
  { "start": [x, y], "target": [x, y], "exclude": id,
    "agents": {id: {x, y, visible, pathTarget|null}}, "grid": {...} }
Stdout: JSON -- either null, or a list of {"x":..., "y":...} waypoints.

Floats round-trip through Python json with the shortest repr that recovers the
same IEEE-754 double; the JS side parses this back to identical doubles.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim


def main():
    req = json.load(sys.stdin)
    result = sim.find_path(
        req['start'][0], req['start'][1], req['target'][0], req['target'][1],
        req['exclude'], req['agents'], req['grid'],
    )
    if result is None:
        print('null')
    else:
        print(json.dumps(result))


if __name__ == '__main__':
    main()