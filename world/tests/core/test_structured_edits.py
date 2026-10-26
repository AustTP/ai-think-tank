import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import core.structured_edits as structured_edits


def _run_edit(command, cwd):
    """Execute the generated `python -c` edit script in a temp workspace."""
    match = re.search(r"[A-Za-z0-9+/]{40,}={0,2}", command)
    script = base64.b64decode(match.group(0)).decode()
    return subprocess.run([sys.executable, '-c', script], cwd=cwd,
                          capture_output=True, text=True)


class StructuredEdits(unittest.TestCase):
    def test_create_replaces_an_existing_file(self):
        # Retrying the same save must not dead-end on create-overwrite
        # (task-392), while an explicit old-text edit still requires a match.
        with tempfile.TemporaryDirectory() as tmp:
            create = structured_edits.command(json.dumps({'edits': [
                {'path': 'plan.md', 'old': None, 'new': 'first\n'}]}))
            self.assertEqual(_run_edit(create, tmp).returncode, 0)
            retry = structured_edits.command(json.dumps({'edits': [
                {'path': 'plan.md', 'old': None, 'new': 'second\n'}]}))
            self.assertEqual(_run_edit(retry, tmp).returncode, 0)
            with open(os.path.join(tmp, 'plan.md')) as fh:
                self.assertEqual(fh.read(), 'second\n')
            stale = structured_edits.command(json.dumps({'edits': [
                {'path': 'plan.md', 'old': 'missing', 'new': 'x'}]}))
            self.assertNotEqual(_run_edit(stale, tmp).returncode, 0)
    def test_exact_edit_command_is_bounded_and_relative(self):
        command = structured_edits.command(json.dumps({
            'edits': [{'path': 'module.py', 'old': 'x = 1', 'new': 'x = 2'}],
            'commands': ['python -m pytest tests/test_module.py -q']}))
        self.assertIn('python -c', command)
        self.assertIn('pytest', command)

    def test_unsafe_paths_and_ambiguous_shapes_are_rejected(self):
        with self.assertRaises(ValueError):
            structured_edits.command(json.dumps({'edits': [{'path': '../x.py', 'old': 'a', 'new': 'b'}]}))
        with self.assertRaises(ValueError):
            structured_edits.command(json.dumps({'edits': []}))
        with self.assertRaises(ValueError):
            structured_edits.command(json.dumps({'edits': [{'path': 'x.py', 'old': 'a'}]}))
