"""A coding turn must publish a real change: no empty tree, no deletion-only
tree. This is the guard that catches an agent deleting its own deliverable."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import content  # noqa: E402


def _manifest(files):
    import hashlib
    return {path: hashlib.sha256(body).hexdigest() for path, body in files.items()}


def test_added_or_modified_file_is_a_deliverable():
    before = _manifest({'a.py': b'one', 'b.py': b'two'})
    after = _manifest({'a.py': b'one changed', 'b.py': b'two'})
    assert content._deliverable_problem(before, after) is None


def test_empty_change_is_refused():
    before = _manifest({'a.py': b'one'})
    assert 'empty' in content._deliverable_problem(before, dict(before))


def test_deletion_only_change_is_refused():
    before = _manifest({'feedback.html': b'page', 'b.py': b'two'})
    after = _manifest({'b.py': b'two'})
    problem = content._deliverable_problem(before, after)
    assert problem and 'deletes' in problem and 'feedback.html' in problem
