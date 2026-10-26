"""Guard against tests writing the operator's live tree.

The session fixture in conftest.py redirects every writable service path to a
temp root. These assertions fail loudly if a future change re-points one back
at the real repo.
"""
import os

import serve

LIVE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _under(path, root):
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def test_writable_paths_are_not_the_live_tree():
    for name in ('THINK_TANK_DIR', 'SANDBOXES_DIR', 'ESCALATIONS_PATH',
                 'LIBRARY_DIR', 'LIBRARY_ARCHIVE_DIR', 'PASSPORT_PATH',
                 'AGENTS_DIR', 'DB_BACKUP_DIR', 'BROWSE_TRAIL_PATH',
                 'LIBRARY_USAGE_PATH', 'HTTP_CACHE_DIR'):
        path = getattr(serve, name)
        assert not _under(path, LIVE_ROOT), f'{name} still points at the live tree: {path}'


def test_database_is_not_the_live_database():
    assert not _under(serve.DB_PATH, LIVE_ROOT)
