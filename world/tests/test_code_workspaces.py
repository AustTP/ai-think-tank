import os
import sys
import unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from code_workspaces import sandbox_for
from code_workspaces import ensure_test_directory, prepare
import tempfile
from pathlib import Path


class WorkspaceTests(unittest.TestCase):
    def test_fresh_and_staged_workspaces_have_tests_without_overwriting_files(self):
        with tempfile.TemporaryDirectory() as root:
            sandbox = prepare({}, {'workOrderId': 'new'}, root)
            self.assertTrue((Path(root) / sandbox / 'tests').is_dir())
            ensure_test_directory(root, 'stage-example')
            saved = Path(root) / 'stage-example' / 'tests' / 'test_existing.py'
            saved.write_text('existing')
            ensure_test_directory(root, 'stage-example')
            self.assertEqual(saved.read_text(), 'existing')

    def test_test_directory_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                ensure_test_directory(root, '../outside')

    def test_reviews_share_project_not_other_projects(self):
        parent = {'id':'a','workOrderId':'request-1','projectLabel':'trading','codeWorkspaceVersion':1}
        state = {'tasks':{'a':parent}}
        chosen = sandbox_for(state,parent)
        self.assertEqual(chosen,sandbox_for(state,{'reviewOf':'a'}))
        self.assertNotEqual(chosen,sandbox_for(state,{'workOrderId':'request-2'}))

    def test_explicit_product_and_legacy(self):
        state = {'products':{'p':{'sandboxId':'product-p'}}}
        self.assertEqual(sandbox_for(state,{'productId':'p'}),'product-p')
        self.assertEqual(sandbox_for({}, {'_sandboxId':'existing'}),'existing')
        self.assertEqual(sandbox_for({}, {}),'workroom-shared')
        old = {'id':'old','projectLabel':'existing'}
        self.assertEqual(sandbox_for({'tasks':{'old':old}},old),'workroom-shared')
