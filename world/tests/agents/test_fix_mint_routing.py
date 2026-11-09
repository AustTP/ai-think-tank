"""A code fix card minted by a gate review must carry the project label.

Without it the workroom dispatcher fell through to the bare-ambient stub
(the shared-tooling health check), which completed with a note and no
artifact; the fail-closed completion gate refused it and the repair loop
churned 81 cards into 'the lane output and the completion gate disagree'
(2026-11-09, 14 refused completions per 37 minutes).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


class FixCardRouting(unittest.TestCase):
    def test_source_contract(self):
        self._mint_source_has_project_label()
        self._dispatch_order_has_stub_last()

    def test_unlabeled_code_fix_routes_to_coding_lane(self):
        # The dispatcher must send a reviewOf+code card to the coding lane
        # even with no project label: the stub path completes note-only and
        # the fail-closed gate refuses it forever.
        import agents as _agents_mod
        root = os.path.dirname(os.path.dirname(os.path.abspath(_agents_mod.__file__)))
        path = os.path.join(root, 'agents', 'content_parts', '80_body_09.py')
        src = open(path, encoding='utf-8').read()
        fn = src[src.index('def _run_workroom_content'):]
        fn = fn[:fn.index('\ndef ', 10)]
        self.assertIn("task.get('reviewOf') and str(task.get('taskType') or '') == 'code'",
                      fn, 'unlabeled code fix cards route to the coding lane')
        self.assertLess(fn.index("task.get('reviewOf') and str"),
                        fn.index('if project_label:'),
                        'the fix-card route comes before the labeled route')

    def _mint_source_has_project_label(self):
        # Contract test on the mint site: the queue_fix dict literal must
        # carry projectLabel for code fixes (is_prose_review-guarded).
        import agents as _agents_mod
        root = os.path.dirname(os.path.dirname(os.path.abspath(_agents_mod.__file__)))
        path = os.path.join(root, 'agents', 'content_parts', '30_body_04.py')
        src = open(path, encoding='utf-8').read()
        mint = src[src.index("queue_fix = {'title'"):
                   src.index("failed_reqs = [g for g")]
        self.assertIn("'projectLabel': project_label", mint,
                      'the minted fix card must carry the project label')
        # And it must be scoped to code fixes only.
        self.assertLess(mint.index("'projectLabel'"),
                        mint.index('if is_prose_review:'))

    def _dispatch_order_has_stub_last(self):
        import agents as _agents_mod
        root = os.path.dirname(os.path.dirname(os.path.abspath(_agents_mod.__file__)))
        # The dispatcher must keep the stub as the last resort: productId,
        # review/qa, then projectLabel, then the ambient stub.
        path = os.path.join(root, 'agents', 'content_parts', '80_body_09.py')
        src = open(path, encoding='utf-8').read()
        fn = src[src.index('def _run_workroom_content'):]
        fn = fn[:fn.index('\ndef ', 10)]
        self.assertLess(fn.index('productId'),
                        fn.index("task.get('taskType') == 'review'"),
                        'productId routes first')
        self.assertLess(fn.index("task.get('taskType') == 'review'"),
                        fn.index('if project_label:'),
                        'the review/qa and coding lanes route before the '
                        'ambient stub')


if __name__ == '__main__':
    unittest.main()
