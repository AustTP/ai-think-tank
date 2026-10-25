"""The operator policy data that steers agents or weakens a safety net is
Tier B: editable, but only with the player's signature (selfedit audit tiers)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import selfedit  # noqa: E402

WORLD = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

    MODULES = ('integrations.rss', 'server.ws', 'server.webhooks',
               'integrations.apis', 'core.auth', 'integrations.bank')

    def test_serve_is_the_only_importer_and_is_tolerated(self):
        for modname in self.MODULES:
            with self.subTest(mod=modname):
                self.assertEqual(selfedit._importers_of(modname), ['serve.py'])
                self.assertEqual(selfedit.TOLERATED_IMPORTERS[modname], {'serve.py'})

    def test_generic_refresh_rebinds_serve_names(self):
        import integrations.apis as apis
        import serve
        sentinel = lambda *a, **k: 'patched'  # noqa: E731
        old = apis._api_base_url
        apis._api_base_url = sentinel
        try:
            self.assertIn('_api_base_url', selfedit._refresh_serve_imports('integrations.apis'))
            self.assertIs(serve._api_base_url, sentinel)
        finally:
            apis._api_base_url = old
        selfedit._refresh_serve_imports('integrations.apis')
        self.assertIs(serve._api_base_url, old)

    def test_env_reload_alias_is_rebound(self):
        import core.env_reload as env_reload
        import serve
        old = env_reload.ENV_RELOAD_FIELDS
        env_reload.ENV_RELOAD_FIELDS = {'SENTINEL': lambda e: 1}
        try:
            self.assertIn('_ENV_RELOAD_FIELDS',
                          selfedit._refresh_serve_imports('core.env_reload'))
            self.assertIs(serve._ENV_RELOAD_FIELDS, env_reload.ENV_RELOAD_FIELDS)
        finally:
            env_reload.ENV_RELOAD_FIELDS = old
            selfedit._refresh_serve_imports('core.env_reload')

    def test_db_backed_modules_reload_their_registry(self):
        import serve
        for modname, loaders in selfedit.POST_RELOAD_LOADERS.items():
            with self.subTest(mod=modname):
                for name in loaders:
                    self.assertTrue(callable(getattr(serve, name, None)))


class KernelEditTest(unittest.TestCase):
    """serve.py is the signed kernel: preflight then restart, with the
    original bytes restored when anything fails."""

    def setUp(self):
        import hashlib
        import tempfile
        import types
        from unittest import mock
        self.tmp = tempfile.mkdtemp(prefix='think tank-kernel-test-')
        self.addCleanup(__import__('shutil').rmtree, self.tmp, True)
        self.orig = 'KERNEL = 1\n'
        self.cand = 'KERNEL = 2\n'
        self.target = os.path.join(self.tmp, 'serve.py')
        with open(self.target, 'w', encoding='utf-8') as f:
            f.write(self.orig)
        os.makedirs(os.path.join(self.tmp, '.selfedit_staging'))
        with open(os.path.join(self.tmp, '.selfedit_staging', 'ed-1.cand'),
                  'w', encoding='utf-8') as f:
            f.write(self.cand)
        self.sha = hashlib.sha256(self.orig.encode()).hexdigest()
        self.updates = []
        fake = types.SimpleNamespace(log_action=lambda *a, **k: None)
        self._patches = [
            mock.patch.object(selfedit, '_world', return_value=self.tmp),
            mock.patch.object(selfedit, '_s', return_value=fake),
            mock.patch.object(selfedit, '_update',
                              side_effect=lambda rid, **kw: self.updates.append(kw)),
            mock.patch.object(selfedit, '_notify_agent', return_value=True),
            mock.patch.object(selfedit, '_row_agent', return_value='ben'),
            mock.patch.object(selfedit, 'KERNEL_RESTART_ENABLED', False),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _content(self):
        with open(self.target, encoding='utf-8') as f:
            return f.read()

    def test_kernel_is_signed_readable_and_not_protected(self):
        path = os.path.join(WORLD, 'serve.py')
        self.assertTrue(selfedit.is_kernel(path))
        self.assertFalse(selfedit.is_player_signature(path))
        self.assertFalse(selfedit.is_unreadable(path))
        self.assertFalse(selfedit.is_protected(path))
        self.assertIsNone(selfedit._path_refusal(path, 'world/serve.py'))

    def test_health_url_uses_launch_host_and_port(self):
        argv = ['/usr/bin/python3', '-m', 'uvicorn', 'serve:app',
                '--host', '100.96.104.61', '--port', '8937']
        self.assertEqual(selfedit._kernel_health_url(argv),
                         'http://100.96.104.61:8937/api/rooms')
        self.assertEqual(
            selfedit._kernel_health_url(['x', '--host=0.0.0.0', '--port=9000']),
            'http://127.0.0.1:9000/api/rooms')

    def test_argv_comes_from_ps(self):
        from unittest import mock
        import types
        done = types.SimpleNamespace(stdout=b'/opt/py -m uvicorn serve:app --port 8937\n')
        with mock.patch.object(selfedit.subprocess, 'run', return_value=done):
            self.assertEqual(selfedit._kernel_argv(),
                             ['/opt/py', '-m', 'uvicorn', 'serve:app', '--port', '8937'])

    def test_successful_preflight_writes_candidate(self):
        with unittest.mock.patch.object(selfedit, '_run_kernel_preflight',
                                        return_value=(True, 'ok')):
            self.assertEqual(selfedit._apply_kernel('ed-1', 'world/serve.py', self.sha), 1)
        self.assertEqual(self._content(), self.cand)
        self.assertEqual(self.updates[-1]['status'], 'applied')

    def test_failed_preflight_restores_original(self):
        with unittest.mock.patch.object(selfedit, '_run_kernel_preflight',
                                        return_value=(False, '1 failed')):
            self.assertEqual(selfedit._apply_kernel('ed-1', 'world/serve.py', self.sha), 0)
        self.assertEqual(self._content(), self.orig)
        self.assertEqual(self.updates[-1]['status'], 'failed')

    def test_missing_launch_command_restores_original(self):
        with unittest.mock.patch.object(selfedit, '_run_kernel_preflight',
                                        return_value=(True, 'ok')), \
             unittest.mock.patch.object(selfedit, '_kernel_argv', return_value=None):
            self.assertEqual(selfedit._apply_kernel('ed-1', 'world/serve.py', self.sha), 0)
        self.assertEqual(self._content(), self.orig)
        self.assertEqual(self.updates[-1]['status'], 'failed')

    def test_stale_sha_is_a_conflict(self):
        self.assertEqual(selfedit._apply_kernel('ed-1', 'world/serve.py', '0' * 64), 0)
        self.assertEqual(self._content(), self.orig)
        self.assertEqual(self.updates[-1]['status'], 'conflict')


class KernelTargetedReadTest(unittest.TestCase):
    """A 30k-line kernel cannot be read whole, so selfedit_read supports a
    query window around an exact anchor."""

    def test_query_returns_a_window_with_line_numbers(self):
        ok, out = selfedit.read_module('serve.py', query='def _should_wake',
                                       context=2)
        self.assertTrue(ok)
        self.assertIn('def _should_wake', out['text'])
        self.assertGreaterEqual(out['matchLine'], out['startLine'])
        self.assertLessEqual(out['matchLine'], out['endLine'])
        self.assertFalse(out['truncated'])

    def test_missing_query_is_a_clear_error(self):
        ok, out = selfedit.read_module('serve.py', query='no such anchor xyz')
        self.assertFalse(ok)
        self.assertIn('not found', out['error'])

    def test_head_read_still_works(self):
        ok, out = selfedit.read_module('serve.py')
        self.assertTrue(ok)
        self.assertTrue(out['text'])
        self.assertEqual(out['path'], 'world/serve.py')
