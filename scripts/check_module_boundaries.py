#!/usr/bin/env python3
"""Ratchet the package dependency graph.

Python has no enforced module boundary here yet, and the split packages still
carry legacy edges. This check records the edges that exist today and fails
when a new one appears. New code should use the package's public module or a
port instead of importing a sibling package's internals.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORLD = ROOT / 'world'
PACKAGES = {'core', 'server', 'village', 'agents', 'integrations', 'tools'}

# Exact legacy edges. Each entry is (source package, destination package,
# source file relative to world/, reason). An import from a new file fails.
ALLOWED = {
    ('agents', 'core', '*', 'core is the shared lower layer'),
    ('agents', 'village', 'agents/completion_watchdog.py', 'recovery staffing port'),
    ('core', 'agents', 'core/planner.py', 'story-budget types'),
    ('core', 'integrations', 'core/evidence.py', 'web helper seam'),
    ('core', 'server', 'core/confirmations.py', 'migration schema'),
    ('core', 'village', 'core/planner.py', 'roster lookup'),
    ('core', 'village', 'core/selfedit_parts/20_part.py', 'self-edit target read'),
    ('core', 'integrations', 'core/selfedit_parts/30_part.py', 'self-edit approval seam'),
    ('integrations', 'agents', 'integrations/rss.py', 'pulse types'),
    ('integrations', 'core', '*', 'core is the shared lower layer'),
    ('server', 'agents', '*', 'HTTP and worker surfaces'),
    ('server', 'core', '*', 'HTTP and worker surfaces'),
    ('server', 'integrations', '*', 'HTTP and worker surfaces'),
    ('server', 'tools', '*', 'operator tools surface'),
    ('server', 'village', '*', 'HTTP and worker surfaces'),
    ('village', 'agents', '*', 'legacy sim/content seam'),
    ('village', 'core', '*', 'core is the shared lower layer'),
    ('village', 'server', 'village/sim_parts/00_header.py', 'cron schedule seam'),
}


def _source_package(path: Path, root: Path = WORLD) -> str | None:
    rel = path.relative_to(root)
    first = rel.parts[0] if len(rel.parts) > 1 else None
    return first if first in PACKAGES else None


def _imports(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (OSError, SyntaxError):
        return set()
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split('.')[0])
    return found


def scan(root: Path = WORLD) -> list[str]:
    violations = []
    for path in sorted(root.rglob('*.py')):
        parts = set(path.relative_to(root).parts)
        if parts & {'__pycache__', 'tests'} or '.selfedit' in str(path):
            continue
        src = _source_package(path, root)
        if src is None:
            continue
        rel = path.relative_to(root).as_posix()
        for dst in _imports(path):
            if dst not in PACKAGES or dst == src:
                continue
            allowed = any(
                s == src and d == dst and (f == '*' or f == rel)
                for s, d, f, _ in ALLOWED
            )
            if not allowed:
                violations.append(f'{rel}: {src} may not import {dst}')
    return violations


def main() -> int:
    violations = scan()
    if violations:
        print('Module boundary check failed:')
        for violation in violations:
            print(f'- {violation}')
        return 1
    print('Module boundary check passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
