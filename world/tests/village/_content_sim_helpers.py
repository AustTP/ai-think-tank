"""Shared fixtures for the content/sim integration test modules."""
import contextlib
import unittest
import unittest.mock


@contextlib.contextmanager
def _hermetic_db():
    """A fresh tmp DB for the duration of the block. Tests that exercise the
    groom path with a stub decider need this: the conftest live-DB copy now
    legitimately carries an armed groom cascade, so the classifier may
    correctly answer instead of the decider being reached."""
    import os
    import shutil
    import serve
    import tempfile
    tmp = tempfile.mkdtemp(prefix='hermetic-db-')
    cm = unittest.mock.patch.multiple(
        serve, DB_PATH=os.path.join(tmp, 't.db'), THINK_TANK_DIR=tmp)
    cm.start()
    serve.init_db()
    try:
        yield tmp
    finally:
        cm.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _completion(text):
    return {'choices': [{'message': {'content': text}}], 'usage': {'cost': 0.0}}


def _snapshot():
    return {
        'agentRoster': [{'id': 'cora', 'name': 'Cora'}],
        'agents': {'cora': {'name': 'Cora'}},
    }


class _Base(unittest.TestCase):
    def _patch(self, target, **kwargs):
        p = unittest.mock.patch(target, **kwargs)
        p.start()
        self.addCleanup(p.stop)
        return p

    def _patch_obj(self, obj, attr, **kwargs):
        p = unittest.mock.patch.object(obj, attr, **kwargs)
        p.start()
        self.addCleanup(p.stop)
        return p
