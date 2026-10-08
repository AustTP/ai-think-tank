"""Hermetic unit tests for the village-isolation helpers in serve.py.

Covers the pure logic only (no live server/DB/browser): how a library path's
village is derived, how writes are namespaced, how the requester's scope is
resolved, and how sandboxes / agent-files are remapped per village. Mirrors
test_avatars.py's no-fixture style: patch serve.get_state_from_db where the
helper reads the DB.
"""
import os
import sys
import unittest
import unittest.mock  # noqa: F401 -- unittest.mock.patch used below

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import serve  # noqa: E402


def _state(agents=None, villages=None):
    st = {
        'agents': {
            'maindev': {'villageId': 'main'},
            'winterdev': {'villageId': 'winter'},
            'winterboss': {'villageId': 'winter'},
        },
        'villages': [{'id': 'main', 'name': 'Think Tank'}, {'id': 'winter', 'name': 'Winter Village'}],
    }
    if agents:
        st['agents'].update(agents)
    if villages:
        st['villages'] = villages
    return st


class LibraryPathVillage(unittest.TestCase):
    def test_flat_commons_is_main(self):
        st = _state()
        self.assertEqual(serve._library_path_village('skills/foo.md', st), 'main')
        self.assertEqual(serve._library_path_village('shared/x.txt', st), 'main')
        self.assertEqual(serve._library_path_village('wiki/think_tank/a.md', st), 'main')

    def test_village_prefixed_path(self):
        st = _state()
        self.assertEqual(serve._library_path_village('villages/winter/skills/foo.md', st), 'winter')
        self.assertEqual(serve._library_path_village('villages/main/skills/foo.md', st), 'main')

    def test_downloads_derives_village_from_owner(self):
        st = _state()
        self.assertEqual(serve._library_path_village('downloads/winterdev/a.txt', st), 'winter')
        self.assertEqual(serve._library_path_village('downloads/maindev/a.txt', st), 'main')
        # Unknown owner defaults to main.
        self.assertEqual(serve._library_path_village('downloads/ghost/a.txt', st), 'main')

    def test_design_references_encoded_village(self):
        st = _state()
        self.assertEqual(serve._library_path_village('design-references/winter/x.md', st), 'winter')
        self.assertEqual(serve._library_path_village('design-references/main/x.md', st), 'main')


class RequesterVillages(unittest.TestCase):
    def test_player_and_unknown_cross(self):
        self.assertIsNone(serve._requester_villages(None))
        self.assertIsNone(serve._requester_villages('player'))

    def test_no_state_returns_none(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=None):
            self.assertIsNone(serve._requester_villages('winterdev'))

    def test_admin_crosses(self):
        st = _state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=st), \
             unittest.mock.patch.object(serve, '_is_admin', return_value=True):
            self.assertIsNone(serve._requester_villages('winterboss'))

    def test_agent_scoped_to_own_village(self):
        st = _state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=st), \
             unittest.mock.patch.object(serve, '_is_admin', return_value=False):
            self.assertEqual(serve._requester_villages('winterdev'), {'winter'})
            self.assertEqual(serve._requester_villages('maindev'), {'main'})


class ScopeAllows(unittest.TestCase):
    def test_none_allows_everything(self):
        self.assertTrue(serve._scope_allows(None, 'winter'))
        self.assertTrue(serve._scope_allows(None, 'main'))

    def test_set_restricts(self):
        self.assertTrue(serve._scope_allows({'winter'}, 'winter'))
        self.assertFalse(serve._scope_allows({'winter'}, 'main'))


class NamespaceWrite(unittest.TestCase):
    def test_main_agent_unchanged(self):
        st = _state()
        self.assertEqual(serve._library_namespace_write('maindev', 'skills/foo.md', st), 'skills/foo.md')

    def test_player_and_none_unchanged(self):
        st = _state()
        self.assertEqual(serve._library_namespace_write('player', 'skills/foo.md', st), 'skills/foo.md')
        self.assertEqual(serve._library_namespace_write(None, 'skills/foo.md', st), 'skills/foo.md')

    def test_admin_unchanged(self):
        st = _state()
        with unittest.mock.patch.object(serve, '_is_admin', return_value=True):
            self.assertEqual(serve._library_namespace_write('winterboss', 'skills/foo.md', st), 'skills/foo.md')

    def test_winter_commons_namespaced(self):
        st = _state()
        self.assertEqual(serve._library_namespace_write('winterdev', 'skills/foo.md', st),
                         'villages/winter/skills/foo.md')

    def test_winter_already_scoped_paths_unchanged(self):
        st = _state()
        # A downloads path is already village-scoped by its owning agent.
        self.assertEqual(serve._library_namespace_write('winterdev', 'downloads/winterdev/x.txt', st),
                         'downloads/winterdev/x.txt')
        # Already village-prefixed.
        self.assertEqual(serve._library_namespace_write('winterdev', 'villages/winter/skills/foo.md', st),
                         'villages/winter/skills/foo.md')


class SplitVillagePrefix(unittest.TestCase):
    def test_flat_path(self):
        self.assertEqual(serve._split_village_prefix('pending_review/skills/a.md'), (None, 'pending_review/skills/a.md'))

    def test_prefixed_path(self):
        self.assertEqual(serve._split_village_prefix('villages/winter/pending_review/skills/a.md'),
                         ('winter', 'pending_review/skills/a.md'))


class VillageSandboxId(unittest.TestCase):
    def test_main_player_admin_unchanged(self):
        st = _state()
        self.assertEqual(serve._village_sandbox_id('maindev', 'workroom-shared', st), 'workroom-shared')
        self.assertEqual(serve._village_sandbox_id('player', 'workroom-shared', st), 'workroom-shared')
        with unittest.mock.patch.object(serve, '_is_admin', return_value=True):
            self.assertEqual(serve._village_sandbox_id('winterboss', 'workroom-shared', st), 'workroom-shared')

    def test_no_state_or_agent_unchanged(self):
        self.assertEqual(serve._village_sandbox_id('', 'workroom-shared', None), 'workroom-shared')
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=None):
            self.assertEqual(serve._village_sandbox_id('winterdev', 'workroom-shared'), 'workroom-shared')

    def test_winter_remapped_and_idempotent(self):
        st = _state()
        self.assertEqual(serve._village_sandbox_id('winterdev', 'workroom-shared', st), 'winter-workroom-shared')
        self.assertEqual(serve._village_sandbox_id('winterdev', 'research-shared', st), 'winter-research-shared')
        self.assertEqual(serve._village_sandbox_id('winterdev', 'winter-workroom-shared', st), 'winter-workroom-shared')


class CrossVillageAgentDenied(unittest.TestCase):
    def test_player_and_admin_cross(self):
        st = _state()
        self.assertFalse(serve._cross_village_agent_denied(st, None, 'winterdev'))
        self.assertFalse(serve._cross_village_agent_denied(st, 'player', 'winterdev'))
        with unittest.mock.patch.object(serve, '_is_admin', return_value=True):
            self.assertFalse(serve._cross_village_agent_denied(st, 'winterboss', 'maindev'))

    def test_same_village_allowed(self):
        st = _state()
        self.assertFalse(serve._cross_village_agent_denied(st, 'winterdev', 'winterboss'))

    def test_cross_village_denied(self):
        st = _state()
        self.assertTrue(serve._cross_village_agent_denied(st, 'winterdev', 'maindev'))
        self.assertTrue(serve._cross_village_agent_denied(st, 'maindev', 'winterdev'))


if __name__ == '__main__':
    unittest.main()
