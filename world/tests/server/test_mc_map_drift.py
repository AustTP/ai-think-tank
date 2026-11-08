"""Tank Mission Control map drift check.

The map's layout and wording are hand-authored (world/mc/map.json); what
it may not do is drift from the code. Mission Control's invariant: every
card names the code it covers, and every file the map declares to be part
of the pipeline is covered by exactly one card.
"""
import json
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def _map():
    with open(os.path.join(_ROOT, 'mc', 'map.json'), encoding='utf-8') as fh:
        return json.load(fh)


class MapDriftTests(unittest.TestCase):
    def test_every_covered_path_exists(self):
        m = _map()
        missing = []
        for node in m['nodes']:
            for path in node.get('covers') or []:
                if not os.path.exists(os.path.join(_ROOT, '..', path)):
                    missing.append((node['id'], path))
        self.assertEqual(missing, [], f'map covers paths that do not exist: '
                         f'{missing}')

    def test_every_pipeline_file_is_covered(self):
        m = _map()
        covered = set()
        for node in m['nodes']:
            covered.update(node.get('covers') or [])
        uncovered = [p for p in m.get('pipelineIncludes') or []
                     if p not in covered]
        self.assertEqual(uncovered, [],
                         f'pipeline files no card covers: {uncovered}')

    def test_edges_reference_known_nodes(self):
        m = _map()
        ids = {n['id'] for n in m['nodes']}
        bad = [e for e in m['edges']
               if e['from'] not in ids or e['to'] not in ids]
        self.assertEqual(bad, [], f'edges reference unknown nodes: {bad}')


if __name__ == '__main__':
    unittest.main()
