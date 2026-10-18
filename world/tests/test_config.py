"""Hermetic tests for world/config.py -- the operator-edited policy JSON.

Covers the load discipline (fail-closed defaults on a missing/malformed file,
validation of each shape), the watchlist write-back gate, and the boot seeding
path (serve._seed_watchlist_topics). The shipped world/*.json files are the
real inputs for the "defaults" assertions, so this also proves the shipped
files parse.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


class _TempPolicy(unittest.TestCase):
    """Point config at a temp dir for the duration of the test, then reload the
    real shipped files so other tests see clean state."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-config-test-')
        self._patch = unittest.mock.patch.multiple(
            config,
            PLAIN_WRITING_PATH=os.path.join(self._tmp, 'plain_writing.json'),
            BROWSE_POLICY_PATH=os.path.join(self._tmp, 'browse_policy.json'),
            RESEARCH_DESK_PATH=os.path.join(self._tmp, 'research_desk.json'),
            WATCHLIST_PATH=os.path.join(self._tmp, 'watchlist.json'),
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        config.reload_policy_config()  # restore real shipped-file values
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, name, data):
        with open(os.path.join(self._tmp, name), 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)


class ShippedDefaults(unittest.TestCase):
    def test_shipped_files_parse_to_sane_defaults(self):
        # The shipped world/*.json must load without error and match the
        # in-code fail-closed defaults.
        self.assertEqual(config.PLAIN_WRITING_BAN_LIST,
                         list(config.PLAIN_WRITING_BAN_LIST_DEFAULT))
        self.assertEqual(config.BROWSE_BLOCK_CATEGORIES,
                         list(config.BROWSE_BLOCK_CATEGORIES_DEFAULT))
        self.assertEqual([p['key'] for p in config.RESEARCH_DESK_PASSES],
                         ['announcements', 'practical_examples', 'limitations'])
        self.assertIsInstance(config.RESEARCH_WATCHLIST, list)
        self.assertFalse(config.ALLOW_WATCHLIST_WRITE)  # only the real boot enables it


class LoadValidation(_TempPolicy):
    def test_loads_operator_custom_values(self):
        self._write('plain_writing.json', {'banned': ['word', 'phrase']})
        self._write('browse_policy.json', {'block_categories': ['cat a', 'cat b']})
        self._write('research_desk.json', {'passes': [
            {'key': 'k1', 'question': 'q1'}, {'key': 'k2', 'question': 'q2'}]})
        self._write('watchlist.json', {'topics': [
            {'topic': 'follow widgets', 'startUrl': 'https://example.com', 'cadenceMs': 3600000}]})
        config.reload_policy_config()
        self.assertEqual(config.PLAIN_WRITING_BAN_LIST, ['word', 'phrase'])
        self.assertEqual(config.BROWSE_BLOCK_CATEGORIES, ['cat a', 'cat b'])
        self.assertEqual([p['key'] for p in config.RESEARCH_DESK_PASSES], ['k1', 'k2'])
        self.assertEqual(len(config.RESEARCH_WATCHLIST), 1)
        self.assertEqual(config.RESEARCH_WATCHLIST[0]['startUrl'], 'https://example.com')

    def test_missing_file_falls_back_to_defaults(self):
        config.reload_policy_config()
        self.assertEqual(config.PLAIN_WRITING_BAN_LIST,
                         list(config.PLAIN_WRITING_BAN_LIST_DEFAULT))
        self.assertEqual(config.RESEARCH_WATCHLIST, [])

    def test_malformed_json_falls_back_to_defaults(self):
        with open(config.PLAIN_WRITING_PATH, 'w', encoding='utf-8') as f:
            f.write('{not json')
        config.reload_policy_config()
        self.assertEqual(config.PLAIN_WRITING_BAN_LIST,
                         list(config.PLAIN_WRITING_BAN_LIST_DEFAULT))

    def test_wrong_shapes_drop_invalid_entries(self):
        self._write('plain_writing.json', {'banned': ['ok', '', 42, '  ', 'fine']})
        self._write('research_desk.json', {'passes': [
            {'key': '', 'question': 'no key'},
            {'key': 'only-key'},
            {'key': 'good', 'question': 'kept'},
            'not-a-dict']})
        self._write('watchlist.json', {'topics': [
            {'topic': '', 'startUrl': 'https://example.com'},
            {'topic': 'no url', 'startUrl': ''},
            {'topic': 'bad url', 'startUrl': 'not-a-url'},
            {'topic': 'good', 'startUrl': 'https://example.com/x'}]})
        config.reload_policy_config()
        self.assertEqual(config.PLAIN_WRITING_BAN_LIST, ['ok', 'fine'])
        self.assertEqual([p['key'] for p in config.RESEARCH_DESK_PASSES], ['good'])
        self.assertEqual(len(config.RESEARCH_WATCHLIST), 1)
        self.assertEqual(config.RESEARCH_WATCHLIST[0]['topic'], 'good')

    def test_reload_takes_effect_for_serve_without_reimport(self):
        # serve.py and content.py did `from config import ...` at import: they
        # hold the SAME list objects. A reload must mutate those objects in
        # place so the ban list / block categories / desk passes change without
        # a restart and without re-importing serve.
        import serve
        import content
        self._write('plain_writing.json', {'banned': ['totally-new-word']})
        self._write('browse_policy.json', {'block_categories': ['new category']})
        self._write('research_desk.json', {'passes': [
            {'key': 'only', 'question': 'one new pass'}]})
        config.reload_policy_config()
        self.assertEqual(serve.PLAIN_WRITING_BAN_LIST, ['totally-new-word'])
        self.assertEqual(serve.BROWSE_BLOCK_CATEGORIES, ['new category'])
        self.assertEqual([p['key'] for p in content.RESEARCH_DESK_PASSES], ['only'])


class WatchlistWriteback(_TempPolicy):
    def test_writeback_disabled_by_default(self):
        self._write('watchlist.json', {'topics': []})
        config.reload_policy_config()
        self.assertFalse(config.note_watchlist_topic(
            {'topic': 't', 'startUrl': 'https://example.com'}))
        with open(config.WATCHLIST_PATH, encoding='utf-8') as f:
            self.assertEqual(json.load(f)['topics'], [])

    def test_writeback_appends_once_and_is_idempotent(self):
        self._write('watchlist.json', {'topics': []})
        config.reload_policy_config()
        config.ALLOW_WATCHLIST_WRITE = True
        self.addCleanup(setattr, config, 'ALLOW_WATCHLIST_WRITE', False)
        rec = {'topic': 'follow widgets', 'startUrl': 'https://example.com',
               'cadenceMs': 3600000}
        self.assertTrue(config.note_watchlist_topic(rec))
        self.assertFalse(config.note_watchlist_topic(rec))  # no duplicate
        with open(config.WATCHLIST_PATH, encoding='utf-8') as f:
            topics = json.load(f)['topics']
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]['cadenceMs'], 3600000)

    def test_writeback_is_silent_on_bad_input(self):
        self._write('watchlist.json', {'topics': []})
        config.reload_policy_config()
        config.ALLOW_WATCHLIST_WRITE = True
        self.addCleanup(setattr, config, 'ALLOW_WATCHLIST_WRITE', False)
        self.assertFalse(config.note_watchlist_topic({}))
        self.assertFalse(config.note_watchlist_topic(None))
        self.assertFalse(config.note_watchlist_topic({'topic': '', 'startUrl': 'x'}))


class BootSeeding(unittest.TestCase):
    def test_seed_watchlist_topics_adds_missing_topics(self):
        import serve
        state = {'researchTopics': [], 'agents': {}, 'workQueue': [],
                 'tasks': {}, 'pipelines': [], 'villages': [{'id': 'main'}]}
        topics = [
            {'topic': 'follow widgets', 'startUrl': 'https://example.com', 'cadenceMs': 3600000},
            {'topic': 'follow gadgets', 'startUrl': 'https://example.org', 'cadenceMs': 7200000},
        ]
        with unittest.mock.patch.object(config, 'RESEARCH_WATCHLIST', topics), \
             unittest.mock.patch.object(serve, 'log_action'):
            n = serve._seed_watchlist_topics(state)
        self.assertEqual(n, 2)
        self.assertEqual(len(state['researchTopics']), 2)
        self.assertEqual(state['researchTopics'][0]['startUrl'], 'https://example.com')
        # Idempotent: a second seed adds nothing.
        with unittest.mock.patch.object(config, 'RESEARCH_WATCHLIST', topics), \
             unittest.mock.patch.object(serve, 'log_action'):
            n2 = serve._seed_watchlist_topics(state)
        self.assertEqual(n2, 0)
        self.assertEqual(len(state['researchTopics']), 2)

    def test_seed_skips_invalid_entries(self):
        import serve
        state = {'researchTopics': [], 'agents': {}, 'workQueue': [],
                 'tasks': {}, 'pipelines': [], 'villages': [{'id': 'main'}]}
        topics = [{'topic': 'bad url', 'startUrl': 'not-a-url'},
                  {'topic': 'no start url', 'startUrl': ''},
                  {'topic': 'good', 'startUrl': 'https://example.com'}]
        with unittest.mock.patch.object(config, 'RESEARCH_WATCHLIST', topics), \
             unittest.mock.patch.object(serve, 'log_action'):
            n = serve._seed_watchlist_topics(state)
        self.assertEqual(n, 1)
        self.assertEqual(state['researchTopics'][0]['topic'], 'good')


class _TempOperatorPolicy(unittest.TestCase):
    """Point OPERATOR_POLICY_PATH at a temp file, restore the real shipped
    file after each test (same discipline as _TempPolicy)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-op-policy-test-')
        self._path = os.path.join(self._tmp, 'operator_policy.json')
        self._patch = unittest.mock.patch.object(
            config, 'OPERATOR_POLICY_PATH', self._path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        config.reload_policy_config()  # restore real shipped-file values
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, data):
        with open(self._path, 'w', encoding='utf-8') as f:
            json.dump(data, f)


class OperatorPolicy(_TempOperatorPolicy):
    def test_shipped_json_matches_incode_defaults(self):
        # The shipped operator_policy.json must load and match every default.
        shipped = config._load_operator_policy(
            os.path.join(os.path.dirname(config.__file__), 'operator_policy.json'))
        self.assertEqual(shipped, config.OPERATOR_POLICY_DEFAULT)

    def test_missing_file_falls_back_to_defaults(self):
        # No file written: _load returns the defaults for every key.
        data = config._load_operator_policy(self._path)
        self.assertEqual(data, config.OPERATOR_POLICY_DEFAULT)

    def test_valid_edit_applies(self):
        self._write({'maxTeamMembers': 8, 'simWatchdogIntervalS': 45.0,
                     'peerReviewFileProbability': 0.9})
        config.reload_policy_config()
        self.assertEqual(config.op('maxTeamMembers'), 8)
        self.assertEqual(config.op('simWatchdogIntervalS'), 45.0)
        self.assertEqual(config.op('peerReviewFileProbability'), 0.9)
        # Unedited keys keep their defaults.
        self.assertEqual(config.op('maxActiveAgents'),
                         config.OPERATOR_POLICY_DEFAULT['maxActiveAgents'])

    def test_wrong_type_or_out_of_range_keeps_default(self):
        self._write({'maxTeamMembers': 'bogus',        # wrong type
                     'simWatchdogIntervalS': -5,       # below min
                     'peerReviewFileProbability': True,  # bool is not a float
                     'workItemMaxAttempts': 3.5})      # non-integer float
        config.reload_policy_config()
        self.assertEqual(config.op('maxTeamMembers'),
                         config.OPERATOR_POLICY_DEFAULT['maxTeamMembers'])
        self.assertEqual(config.op('simWatchdogIntervalS'),
                         config.OPERATOR_POLICY_DEFAULT['simWatchdogIntervalS'])
        self.assertEqual(config.op('peerReviewFileProbability'),
                         config.OPERATOR_POLICY_DEFAULT['peerReviewFileProbability'])
        self.assertEqual(config.op('workItemMaxAttempts'),
                         config.OPERATOR_POLICY_DEFAULT['workItemMaxAttempts'])

    def test_unknown_keys_and_notes_ignored(self):
        self._write({'maxTeamMembers': 7, '_notes': ['doc'], 'notARealKey': 1})
        config.reload_policy_config()
        self.assertEqual(config.op('maxTeamMembers'), 7)
        self.assertNotIn('notARealKey', config.OPERATOR_POLICY)

    def test_reload_calls_registered_hooks(self):
        calls = []
        config.ON_RELOAD_HOOKS.append(lambda: calls.append(1))
        self.addCleanup(config.ON_RELOAD_HOOKS.remove, config.ON_RELOAD_HOOKS[-1])
        self._write({'maxTeamMembers': 5})
        config.reload_policy_config()
        self.assertEqual(calls, [1])

    def test_reload_returns_operator_section(self):
        self._write({'maxTeamMembers': 9})
        result = config.reload_policy_config()
        self.assertIn('operator', result)
        self.assertEqual(result['operator']['maxTeamMembers'], 9)
        self.assertEqual(
            sorted(result['operator'].keys()),
            sorted(config.OPERATOR_POLICY_DEFAULT.keys()))


class OperatorPolicySimServeBind(unittest.TestCase):
    """Import-time + reload rebind of the sim.py / serve.py scalar mirrors."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-op-bind-test-')
        self._path = os.path.join(self._tmp, 'operator_policy.json')
        self._patch = unittest.mock.patch.object(
            config, 'OPERATOR_POLICY_PATH', self._path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        config.reload_policy_config()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, data):
        with open(self._path, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def test_reload_rebinds_sim_and_serve_scalars(self):
        import serve
        import sim
        self._write({'maxTeamMembers': 8,
                     'workItemMaxAttempts': 5,
                     'successGradeFloor': 9.5,
                     'peerReviewIntervalS': 120,
                     'simWatchdogIntervalS': 45.0,
                     'moraleNeglectCap': 50})
        config.reload_policy_config()
        self.assertEqual(sim.MAX_TEAM_MEMBERS, 8)
        self.assertEqual(sim.WORK_ITEM_MAX_ATTEMPTS, 5)
        self.assertEqual(sim.SUCCESS_GRADE_FLOOR, 9.5)
        self.assertEqual(sim.MORALE_NEGLECT_CAP, 50)
        self.assertEqual(serve.SUCCESS_GRADE_FLOOR, 9.5)
        self.assertEqual(serve.PEER_REVIEW_INTERVAL_S, 120)
        self.assertEqual(serve._SIM_WATCHDOG_INTERVAL_S, 45.0)

    def test_env_watchdog_overrides_operator_config(self):
        # Env key present in .env wins; when absent, operator config wins.
        # Simulate the env-present path by patching the capture variables serve
        # snapshotted at import.
        import serve
        with unittest.mock.patch.object(serve, '_SimWatchdogIntervalSEnv', '11'):
            self._write({'simWatchdogIntervalS': 60.0})
            config.reload_policy_config()
            self.assertEqual(serve._SIM_WATCHDOG_INTERVAL_S, 11.0)

    def test_hooks_are_registered(self):
        import serve  # noqa: F401  (ensures serve hook is registered)
        import sim    # noqa: F401  (ensures sim hook is registered)
        self.assertIn(sim._apply_operator_policy, config.ON_RELOAD_HOOKS)
        self.assertIn(serve._apply_operator_policy, config.ON_RELOAD_HOOKS)


class LoaderBranches(unittest.TestCase):
    """The fail-closed shapes the reload path itself never produces:
    a present-but-wrong-type container, non-dict entries, a URL that
    makes urlparse itself raise, and the optional watchlist fields."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-config-loaders-')
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def _path(self, name):
        return os.path.join(self._tmp, name)

    def _write(self, name, data):
        with open(self._path(name), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def test_load_list_non_list_value_falls_back(self):
        self._write('plain.json', {'banned': 'not-a-list'})
        out = config._load_list(self._path('plain.json'), ['dflt'],
                                key='banned')
        self.assertEqual(out, ['dflt'])

    def test_load_research_desk_non_list_passes(self):
        self._write('desk.json', {'passes': 'nope'})
        out = config._load_research_desk(self._path('desk.json'))
        self.assertEqual(out,
                         [dict(p) for p in config.RESEARCH_DESK_PASSES_DEFAULT])

    def test_load_watchlist_non_list_topics(self):
        self._write('watch.json', {'topics': 'nope'})
        self.assertEqual(config._load_watchlist(self._path('watch.json')), [])

    def test_load_watchlist_skips_non_dict_entries(self):
        self._write('watch.json', {'topics': ['not-a-dict',
            {'topic': 'good', 'startUrl': 'https://example.com'}]})
        out = config._load_watchlist(self._path('watch.json'))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['topic'], 'good')

    def test_load_watchlist_skips_urlparse_failures(self):
        # An invalid IPv6 literal makes urlparse itself raise.
        self._write('watch.json', {'topics': [
            {'topic': 'bad', 'startUrl': 'http://[::1'},
            {'topic': 'good', 'startUrl': 'https://example.com'}]})
        out = config._load_watchlist(self._path('watch.json'))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['topic'], 'good')

    def test_load_watchlist_keeps_cadence_and_keywords(self):
        self._write('watch.json', {'topics': [
            {'topic': 't', 'startUrl': 'https://example.com',
             'cadenceMs': 60000, 'linkKeyword': ' docs ',
             'pageKeyword': ' guide '}]})
        out = config._load_watchlist(self._path('watch.json'))
        self.assertEqual(out[0]['cadenceMs'], 60000)
        self.assertEqual(out[0]['linkKeyword'], 'docs')
        self.assertEqual(out[0]['pageKeyword'], 'guide')


class GrantsLoaderBranches(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-grants-')
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def _path(self):
        return os.path.join(self._tmp, 'grants.json')

    def _write(self, data):
        with open(self._path(), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def test_corrupt_file_grants_nothing(self):
        with open(self._path(), 'w', encoding='utf-8') as f:
            f.write('{not json')
        self.assertEqual(config._load_grants(self._path()),
                         {'appGrants': [], 'fileGrants': []})

    def test_malformed_entries_are_dropped(self):
        self._write({'appGrants': ['nope',
                                    {'bundleId': 'not a valid bundle id'},
                                    {'id': 'notes', 'bundleId': 'com.apple.Notes'}],
                     'fileGrants': ['nope', {'scope': '   '},
                                    {'id': 'f1', 'scope': '~/notes.txt'}]})
        out = config._load_grants(self._path())
        self.assertEqual(out['appGrants'],
                         [{'id': 'notes', 'bundleId': 'com.apple.Notes',
                           'label': 'com.apple.Notes',
                           'caps': {'use': True}}])
        self.assertEqual(out['fileGrants'],
                         [{'id': 'f1', 'scope': os.path.expanduser('~/notes.txt'),
                           'label': '~/notes.txt',
                           'caps': {'read': True, 'write': False,
                                    'delete': False}}])


class CoerceOperatorValue(unittest.TestCase):
    def test_unknown_key_and_bool_for_int_kind_rejected(self):
        self.assertIsNone(config._coerce_operator_value('notARealKey', 1))
        self.assertIsNone(config._coerce_operator_value('maxTeamMembers', True))


class OperatorPolicyRawShape(_TempOperatorPolicy):
    def test_non_dict_operator_file_keeps_defaults(self):
        with open(self._path, 'w', encoding='utf-8') as f:
            json.dump([1, 2, 3], f)
        self.assertEqual(config._load_operator_policy(self._path),
                         config.OPERATOR_POLICY_DEFAULT)


class LiveReloadMissingFile(unittest.TestCase):
    """A policy file deleted between boots: the mtime probe fails
    closed to the shipped defaults, never a raised OSError."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-missing-')
        self.addCleanup(shutil.rmtree, self._tmp, True)
        config._OPERATOR_CACHE['mtime'] = None
        config._GRANTS_CACHE['mtime'] = None
        self.addCleanup(self._restore)

    def _restore(self):
        config._OPERATOR_CACHE['mtime'] = None
        config._GRANTS_CACHE['mtime'] = None
        config.reload_policy_config()

    def test_get_operator_policy_defaults_when_file_absent(self):
        missing = os.path.join(self._tmp, 'no', 'such', 'operator_policy.json')
        with unittest.mock.patch.object(config, 'OPERATOR_POLICY_PATH', missing):
            self.assertEqual(config.get_operator_policy(),
                             config.OPERATOR_POLICY_DEFAULT)

    def test_get_grants_config_defaults_when_file_absent(self):
        missing = os.path.join(self._tmp, 'no', 'such', 'grants.json')
        with unittest.mock.patch.object(config, 'GRANTS_PATH', missing):
            self.assertEqual(config.get_grants_config(),
                             {'appGrants': [], 'fileGrants': []})


class ResearchWatchlistReload(unittest.TestCase):
    """get_research_watchlist: the live-reload read that reports
    (topics, ok) so a sync can refuse to wipe the standing schedule
    on a transient bad read."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-watchlist-')
        self._path = os.path.join(self._tmp, 'watchlist.json')
        self._patch = unittest.mock.patch.object(
            config, 'WATCHLIST_PATH', self._path)
        self._patch.start()
        config._WATCHLIST_CACHE['mtime'] = None
        self.addCleanup(self._restore)

    def _restore(self):
        self._patch.stop()
        config._WATCHLIST_CACHE['mtime'] = None
        config.reload_policy_config()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_missing_file_reports_not_ok(self):
        topics, ok = config.get_research_watchlist()
        self.assertFalse(ok)
        self.assertEqual(topics, [])

    def test_valid_file_reports_ok_and_refreshes_module_list(self):
        with open(self._path, 'w', encoding='utf-8') as f:
            json.dump({'topics': [
                {'topic': 'follow widgets',
                 'startUrl': 'https://example.com'}]}, f)
        topics, ok = config.get_research_watchlist()
        self.assertTrue(ok)
        self.assertEqual(len(topics), 1)
        self.assertEqual(config.RESEARCH_WATCHLIST[0]['topic'],
                         'follow widgets')

    def test_corrupt_file_reports_not_ok(self):
        with open(self._path, 'w', encoding='utf-8') as f:
            f.write('{not json')
        topics, ok = config.get_research_watchlist()
        self.assertFalse(ok)


class ReloadHookFailure(unittest.TestCase):
    def test_raising_hook_does_not_abort_reload(self):
        def boom():
            raise RuntimeError('hook failed')
        config.ON_RELOAD_HOOKS.append(boom)
        self.addCleanup(config.ON_RELOAD_HOOKS.remove, boom)
        result = config.reload_policy_config()  # must not raise
        self.assertIn('operator', result)


class WatchlistWritebackEdges(_TempPolicy):
    def test_writeback_starts_fresh_when_topics_not_a_list(self):
        self._write('watchlist.json', {'topics': 'not-a-list'})
        config.reload_policy_config()
        config.ALLOW_WATCHLIST_WRITE = True
        self.addCleanup(setattr, config, 'ALLOW_WATCHLIST_WRITE', False)
        rec = {'topic': 'follow widgets', 'startUrl': 'https://example.com'}
        self.assertTrue(config.note_watchlist_topic(rec))
        with open(config.WATCHLIST_PATH, encoding='utf-8') as f:
            topics = json.load(f)['topics']
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]['topic'], 'follow widgets')

    def test_writeback_keeps_keywords(self):
        self._write('watchlist.json', {'topics': []})
        config.reload_policy_config()
        config.ALLOW_WATCHLIST_WRITE = True
        self.addCleanup(setattr, config, 'ALLOW_WATCHLIST_WRITE', False)
        rec = {'topic': 't', 'startUrl': 'https://example.com',
               'linkKeyword': 'docs', 'pageKeyword': 'guide'}
        self.assertTrue(config.note_watchlist_topic(rec))
        with open(config.WATCHLIST_PATH, encoding='utf-8') as f:
            entry = json.load(f)['topics'][0]
        self.assertEqual(entry['linkKeyword'], 'docs')
        self.assertEqual(entry['pageKeyword'], 'guide')

    def test_writeback_silent_when_file_unreadable(self):
        config.ALLOW_WATCHLIST_WRITE = True
        self.addCleanup(setattr, config, 'ALLOW_WATCHLIST_WRITE', False)
        missing = os.path.join(self._tmp, 'no', 'such', 'watchlist.json')
        with unittest.mock.patch.object(config, 'WATCHLIST_PATH', missing):
            self.assertFalse(config.note_watchlist_topic(
                {'topic': 't', 'startUrl': 'https://example.com'}))


if __name__ == '__main__':
    unittest.main()
