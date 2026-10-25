import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import structured_edits


class StructuredEdits(unittest.TestCase):
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
