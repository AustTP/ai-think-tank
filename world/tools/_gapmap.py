import ast
import json

with open('coverage.json') as f:
    cov = json.load(f)

# Prefer the exact local sim.py key; otherwise fall back to any key whose
# basename matches (preserves the tool across machines without embedding a
# machine-specific absolute path).
sim_key = next((k for k in cov['files'] if k.endswith('sim.py')), None)
sim_data = cov['files'].get(sim_key, {})
missing = set(sim_data['missing_lines'])

src = open('sim.py').read()
tree = ast.parse(src)

def find_ranges(node, ranges):
    for child in ast.iter_child_nodes(node):
        find_ranges(child, ranges)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        ranges.append((node.name, node.lineno, node.end_lineno))

ranges: list[tuple[str, int, int]] = []
find_ranges(tree, ranges)

out = []
for name, start, end in sorted(ranges, key=lambda x: x[1]):
    miss = sorted(l for l in missing if start <= l <= end)
    if miss:
        out.append((name, start, end, miss))

out.sort(key=lambda x: -len(x[3]))
print(f"total missing: {len(missing)}")
for name, start, end, miss in out:
    print(f"{name} ({start}-{end}): {len(miss)} -> {miss}")
