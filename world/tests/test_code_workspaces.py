import os
import sys
import unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from code_workspaces import sandbox_for


class WorkspaceTests(unittest.TestCase):
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
