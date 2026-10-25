"""Check Python deliverables before invoking Python quality tools."""
import ast
import os
import re
from pathlib import Path

GUIDANCE = ('For Python implementation tasks, create importable .py implementation modules and matching '
            'tests/test_*.py files in this project workspace. HTML, reports, empty files and inspection '
            'commands do not implement a Python task. Inspect current files before editing; use failed '
            'attempt excerpts as reference, not as proof that files already exist. Make small targeted '
            'edits, align imports with actual module paths, then run the full quality pipeline. '
            'Research reports belong in Library as .txt; CSV is only for actual data.')


def required(task):
    import agents.review_quality as review_quality
    contract = review_quality.contract_for(task)
    text = str(task.get('title') or '') + ' ' + contract['instructions'].split('Story:', 1)[-1] + ' ' + contract['acceptanceCriteria']
    return bool(re.search(r'\bpython\b|\b[\w/-]+\.py\b', text, re.I))


def sources(directory):
    root = Path(directory).resolve()
    result = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in ('__pycache__', 'node_modules', 'venv') and not Path(base, d).is_symlink())
        for name in sorted(files):
            path = Path(base, name)
            if name.endswith('.py') and not path.is_symlink():
                result.append(path)
            if len(result) >= 100:
                return result
    return result


def check(directory):
    root = Path(directory).resolve()
    paths = sources(root)
    implementations = [p for p in paths if 'tests' not in p.relative_to(root).parts and not p.name.startswith('test_')]
    substantive = False
    for path in implementations:
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
            substantive = substantive or any(not isinstance(node, (ast.Pass, ast.Import, ast.ImportFrom))
                and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)) for node in tree.body)
        except (SyntaxError, UnicodeError):
            continue
    # pytest discovers test_*.py at the workspace root too. Requiring a literal
    # tests/ directory rejected agents that wrote a valid root-level test file
    # and sent them back to write the same file again.
    tests = [p for p in paths if p.name.startswith('test_')]
    errors = []
    if not substantive:
        errors.append('No substantive Python implementation module exists in the project workspace. Write the required .py implementation; HTML or an empty placeholder does not satisfy this task.')
    if not tests:
        errors.append('No tests/test_*.py files exist. Add tests importing the actual implementation module.')
    return errors


def context(directory, limit=14000):
    root = Path(directory).resolve()
    sections = []
    remaining = limit
    for path in sources(root):
        text = path.read_text(encoding='utf-8', errors='replace')[:min(6000, remaining)]
        sections.append(str(path.relative_to(root)) + ':\n' + text)
        remaining -= len(text)
        if remaining <= 0:
            break
    return '\n\n'.join(sections) or 'No Python source files exist in this workspace.'
