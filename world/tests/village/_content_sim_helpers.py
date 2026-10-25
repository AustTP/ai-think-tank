"""Shared fixtures for the content/sim integration test modules."""
import unittest
import unittest.mock


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
