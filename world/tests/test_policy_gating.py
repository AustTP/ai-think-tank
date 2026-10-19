"""The operator policy data that steers agents or weakens a safety net is
Tier B: editable, but only with the player's signature (selfedit audit tiers)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import selfedit  # noqa: E402

WORLD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

POLICY_DATA = (
    'tool_descriptions.json', 'redaction.json', 'actions.json',
    'model_bands.json', 'service_costs.json', 'browse_policy.json',
    'rooms.json', 'seed_profiles.json', 'endpoint_policy.json',
)


class PolicyDataSignatureTest(unittest.TestCase):
    def test_policy_data_requires_a_player_signature(self):
        for name in POLICY_DATA:
            with self.subTest(name=name):
                self.assertIn(name, selfedit.PLAYER_SIGNATURE_BASENAMES)
                self.assertTrue(selfedit.is_player_signature(os.path.join(WORLD, name)))

    def test_agent_visible_policy_stays_unsigned(self):
        # The watchlist has a deliberate village write-back lane, and the
        # plain-writing list is content the agents are told about directly.
        for name in ('watchlist.json', 'plain_writing.json', 'research_desk.json'):
            with self.subTest(name=name):
                self.assertFalse(selfedit.is_player_signature(os.path.join(WORLD, name)))

    def test_gated_data_is_unreadable_to_agents(self):
        for name in POLICY_DATA:
            with self.subTest(name=name):
                self.assertTrue(selfedit.is_unreadable(os.path.join(WORLD, name)))


if __name__ == '__main__':
    unittest.main()


class ExtractedModuleReloadTest(unittest.TestCase):
    """The modules extracted out of serve.py are self-editable: serve's
    importers are tolerated, serve's `from <mod> import` names are rebound by
    the ast-driven refresh, and DB-backed registries re-run their loader."""

    MODULES = ('rss', 'ws', 'webhooks', 'apis', 'auth', 'bank')

    def test_serve_is_the_only_importer_and_is_tolerated(self):
        for modname in self.MODULES:
            with self.subTest(mod=modname):
                self.assertEqual(selfedit._importers_of(modname), ['serve.py'])
                self.assertEqual(selfedit.TOLERATED_IMPORTERS[modname], {'serve.py'})

    def test_generic_refresh_rebinds_serve_names(self):
        import apis
        import serve
        sentinel = lambda *a, **k: 'patched'  # noqa: E731
        old = apis._api_base_url
        apis._api_base_url = sentinel
        try:
            self.assertIn('_api_base_url', selfedit._refresh_serve_imports('apis'))
            self.assertIs(serve._api_base_url, sentinel)
        finally:
            apis._api_base_url = old
        selfedit._refresh_serve_imports('apis')
        self.assertIs(serve._api_base_url, old)

    def test_env_reload_alias_is_rebound(self):
        import env_reload
        import serve
        old = env_reload.ENV_RELOAD_FIELDS
        env_reload.ENV_RELOAD_FIELDS = {'SENTINEL': lambda e: 1}
        try:
            self.assertIn('_ENV_RELOAD_FIELDS',
                          selfedit._refresh_serve_imports('env_reload'))
            self.assertIs(serve._ENV_RELOAD_FIELDS, env_reload.ENV_RELOAD_FIELDS)
        finally:
            env_reload.ENV_RELOAD_FIELDS = old
            selfedit._refresh_serve_imports('env_reload')

    def test_db_backed_modules_reload_their_registry(self):
        import serve
        for modname, loaders in selfedit.POST_RELOAD_LOADERS.items():
            with self.subTest(mod=modname):
                for name in loaders:
                    self.assertTrue(callable(getattr(serve, name, None)))
