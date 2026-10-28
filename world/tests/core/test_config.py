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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.config as config  # noqa: E402


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

    def test_reload_rebinds_supervisor_and_calibration_scalars(self):
        import serve
        import sim
        self._write({
            'supervisorBriefIntervalMs': 600000,
            'supervisorCardIntervalMs': 1200000,
            'chiefMonitorIntervalMs': 2400000,
            'supervisorCardBudgetUsd': 0.08,
            'delegateMaxOpen': 5,
            'delegateBudgetCapUsd': 1.25,
            'supervisorBudgetCapUsd': 9.0,
            'jevCalibrationTargetReliability': 0.85,
            'jevCalibrationHysteresis': 0.08,
            'jevCalibrationMinOutcome': 14,
            'jevCalibrationStep': 0.03,
            'reviewGradeCalibrationTarget': 0.88,
            'reviewGradeCalibrationHysteresis': 0.06,
            'reviewGradeCalibrationMinSamples': 12,
            'reviewGradeCalibrationStep': 0.04,
            'calibrationAdjustIntervalS': 900,
        })
        config.reload_policy_config()
        self.assertEqual(sim.SUPERVISOR_BRIEF_INTERVAL_MS, 600000)
        self.assertEqual(sim.SUPERVISOR_CARD_INTERVAL_MS, 1200000)
        self.assertEqual(sim.CHIEF_MONITOR_INTERVAL_MS, 2400000)
        self.assertEqual(sim.SUPERVISOR_CARD_BUDGET_USD, 0.08)
        self.assertEqual(serve.DELEGATE_MAX_OPEN, 5)
        self.assertEqual(serve.DELEGATE_BUDGET_CAP_USD, 1.25)
        self.assertEqual(serve.SUPERVISOR_BUDGET_CAP_USD, 9.0)
        self.assertEqual(serve.JEV_CALIBRATION_TARGET_RELIABILITY, 0.85)
        self.assertEqual(serve.JEV_CALIBRATION_HYSTERESIS, 0.08)
        self.assertEqual(serve.JEV_CALIBRATION_MIN_OUTCOME, 14)
        self.assertEqual(serve.JEV_CALIBRATION_STEP, 0.03)
        self.assertEqual(serve.REVIEW_GRADE_CALIBRATION_TARGET, 0.88)
        self.assertEqual(serve.REVIEW_GRADE_CALIBRATION_HYSTERESIS, 0.06)
        self.assertEqual(serve.REVIEW_GRADE_CALIBRATION_MIN_SAMPLES, 12)
        self.assertEqual(serve.REVIEW_GRADE_CALIBRATION_STEP, 0.04)
        self.assertEqual(serve.CALIBRATION_ADJUST_INTERVAL_S, 900)

    def test_reload_rebinds_avatar_generation_scalars(self):
        import serve
        self._write({
            'avatarTaskIntervalS': 45,
            'avatarTaskBatchMax': 4,
            'avatarTaskRetryCooldownS': 10800,
            'avatarGenerationMinCreditsUsd': 1.5,
        })
        config.reload_policy_config()
        self.assertEqual(serve.AVATAR_TASK_INTERVAL_S, 45)
        self.assertEqual(serve.AVATAR_TASK_BATCH_MAX, 4)
        self.assertEqual(serve.AVATAR_TASK_RETRY_COOLDOWN_S, 10800)
        self.assertEqual(serve.AVATAR_GENERATION_MIN_CREDITS_USD, 1.5)

    def test_env_watchdog_overrides_operator_config(self):
        # Env key present in .env wins; when absent, operator config wins.
        # Simulate the env-present path by patching the capture variables serve
        # snapshotted at import.
        import serve
        with unittest.mock.patch.object(serve, '_SimWatchdogIntervalSEnv', '11'):
            self._write({'simWatchdogIntervalS': 60.0})
            config.reload_policy_config()
            self.assertEqual(serve._SIM_WATCHDOG_INTERVAL_S, 11.0)

    def test_policy_bindings_are_registered(self):
        import serve  # noqa: F401  (ensures serve bindings are registered)
        import sim    # noqa: F401  (ensures sim bindings are registered)
        self.assertIn('sim', config.POLICY_BINDINGS)
        self.assertIn('serve', config.POLICY_BINDINGS)

    def test_apply_bindings_skips_unimported_module(self):
        self.assertEqual(config.apply_policy_bindings('no_such_module_here'), [])
        self.assertEqual(config.apply_policy_bindings('serve'), ['serve'])


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


class ExtractedPolicyFilesTest(unittest.TestCase):
    """The three policy files moved out of serve.py: service costs, model
    bands, and redaction patterns. Each must load from the shipped file,
    fall back per section on garbage, and mutate in place so serve.py's
    imported names stay live."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-policy-test-')
        self._patch = unittest.mock.patch.multiple(
            config,
            BROWSE_POLICY_PATH=os.path.join(self._tmp, 'browse_policy.json'),
            SERVICE_COSTS_PATH=os.path.join(self._tmp, 'service_costs.json'),
            MODEL_BANDS_PATH=os.path.join(self._tmp, 'model_bands.json'),
            REDACTION_PATH=os.path.join(self._tmp, 'redaction.json'),
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        config.reload_policy_config()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, name, data):
        with open(os.path.join(self._tmp, name), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def test_shipped_files_parse_and_match_defaults(self):
        config.reload_policy_config()
        self.assertEqual(config.TREG_ENDPOINT_COSTS,
                         dict(config.TREG_ENDPOINT_COSTS_DEFAULT))
        self.assertEqual(config.MODEL_PRICE_BANDS,
                         dict(config.MODEL_PRICE_BANDS_DEFAULT))
        self.assertEqual(config.BAND_BENCHMARK,
                         dict(config.BAND_BENCHMARK_DEFAULT))
        self.assertEqual(config.STANDING_SAFE_DOMAINS,
                         set(config.STANDING_SAFE_DOMAINS_DEFAULT))
        self.assertEqual([p.pattern for p in config.SECRET_PATTERNS],
                         list(config.SECRET_PATTERN_DEFAULTS))

    def test_missing_files_fall_back_to_defaults(self):
        config.reload_policy_config()
        self.assertEqual(config.TREG_ENDPOINT_COSTS,
                         dict(config.TREG_ENDPOINT_COSTS_DEFAULT))
        self.assertEqual(config.MODEL_BAND_PURPOSE,
                         dict(config.MODEL_BAND_PURPOSE_DEFAULT))
        self.assertEqual([p.pattern for p in config.SECRET_PATTERNS],
                         list(config.SECRET_PATTERN_DEFAULTS))

    def test_edited_cost_table_is_validated_and_applied(self):
        self._write('service_costs.json', {'treg_endpoint_costs': {
            'new.endpoint': 0.5, 'bad': 'free', 'negative': -1, '': 2}})
        config.reload_policy_config()
        self.assertEqual(config.TREG_ENDPOINT_COSTS, {'new.endpoint': 0.5})

    def test_malformed_section_does_not_blank_the_others(self):
        self._write('model_bands.json', {
            'price_bands': 'nope',
            'band_purpose': {'coding': 'Write code.'},
            'band_benchmark': {'coding': ['coding_index']},
            'quality_floor_gap': {'high': 3},
            'tier_gate_criteria': {'high': 'Plan carefully.'},
        })
        config.reload_policy_config()
        self.assertEqual(config.MODEL_PRICE_BANDS,
                         dict(config.MODEL_PRICE_BANDS_DEFAULT))
        self.assertEqual(config.MODEL_BAND_PURPOSE, {'coding': 'Write code.'})
        self.assertEqual(config.BAND_BENCHMARK, {'coding': ('coding_index',)})
        self.assertEqual(config.BAND_QUALITY_FLOOR_GAP, {'high': 3})
        self.assertEqual(config.TIER_GATE_CRITERIA, {'high': 'Plan carefully.'})

    def test_invalid_price_band_is_dropped(self):
        self._write('model_bands.json', {'price_bands': {
            'low': [0, 0.5], 'backwards': [5, 1], 'negative': [-1, 2],
            'notpair': [1], 'mid': [0.5, 5], 'high': [5, 50]}})
        config.reload_policy_config()
        self.assertEqual(set(config.MODEL_PRICE_BANDS), {'low', 'mid', 'high'})

    def test_standing_domains_reject_schemes_and_paths(self):
        self._write('browse_policy.json', {'standing_safe_domains': [
            'example.org', '.Example.ORG', 'https://bad.example', 'bad.example/path',
            'gov', '']})
        config.reload_policy_config()
        self.assertEqual(config.STANDING_SAFE_DOMAINS, {'example.org', 'gov'})

    def test_bad_regex_is_dropped_but_redaction_stays_on(self):
        self._write('redaction.json', {'secret_patterns': ['sk-[a-z]+', '(', '']})
        config.reload_policy_config()
        self.assertEqual([p.pattern for p in config.SECRET_PATTERNS], ['sk-[a-z]+'])

    def test_reload_mutates_in_place_so_imported_names_stay_live(self):
        bands_ref = config.MODEL_PRICE_BANDS
        costs_ref = config.TREG_ENDPOINT_COSTS
        domains_ref = config.STANDING_SAFE_DOMAINS
        patterns_ref = config.SECRET_PATTERNS
        self._write('service_costs.json', {'treg_endpoint_costs': {'only': 1}})
        config.reload_policy_config()
        self.assertIs(config.MODEL_PRICE_BANDS, bands_ref)
        self.assertIs(config.TREG_ENDPOINT_COSTS, costs_ref)
        self.assertIs(config.STANDING_SAFE_DOMAINS, domains_ref)
        self.assertIs(config.SECRET_PATTERNS, patterns_ref)
        self.assertEqual(config.TREG_ENDPOINT_COSTS, {'only': 1})

    def test_serve_reexports_the_live_objects(self):
        import serve
        self.assertIs(serve.MODEL_PRICE_BANDS, config.MODEL_PRICE_BANDS)
        self.assertIs(serve.STANDING_SAFE_DOMAINS, config.STANDING_SAFE_DOMAINS)
        self.assertIs(serve.TREG_ENDPOINT_COSTS, config.TREG_ENDPOINT_COSTS)
        self.assertIs(serve._SECRET_PATTERNS, config.SECRET_PATTERNS)
        self.assertIs(serve._TIER_GATE_CRITERIA, config.TIER_GATE_CRITERIA)


class ToolDescriptionOverridesTest(unittest.TestCase):
    """world/tool_descriptions.json is an override layer over the inline tool
    text in serve.py: editing a description needs no code change, and removing
    an entry restores the shipped text."""

    NAMES = ('browse_page', 'search_web', 'api_call', 'policy_edit')

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-tools-test-')
        self._path = os.path.join(self._tmp, 'tool_descriptions.json')
        self._patch = unittest.mock.patch.object(
            config, 'TOOL_DESCRIPTIONS_PATH', self._path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        config.reload_policy_config()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, descriptions):
        with open(self._path, 'w', encoding='utf-8') as f:
            json.dump({'descriptions': descriptions}, f)

    def _served(self, name):
        import serve
        return next(t['function']['description'] for t in serve.AGENT_ASK_TOOLS
                    if t['function']['name'] == name)

    def test_shipped_file_covers_the_offered_tools(self):
        import serve
        shipped = os.path.join(config.ROOT, 'tool_descriptions.json')
        with open(shipped, encoding='utf-8') as f:
            descriptions = json.load(f)['descriptions']
        offered = {t['function']['name'] for t in serve.AGENT_ASK_TOOLS}
        self.assertLessEqual(offered, set(descriptions))

    def test_override_reaches_the_served_tool_and_removal_restores_it(self):
        import serve
        # Normalize first: the served text is baked at import from the shipped
        # descriptions file, while a reload rebuilds it from the live env --
        # on a machine without the operator's .env the two differ (VPN exit
        # metadata). One reload pins both sides to the same rebuild path.
        config.reload_policy_config()
        original = self._served('browse_page')
        self._write({'browse_page': 'Use this to read one exact page.'})
        config.reload_policy_config()
        self.assertEqual(self._served('browse_page'), 'Use this to read one exact page.')

        self._write({})
        config.reload_policy_config()
        self.assertEqual(self._served('browse_page'), original)

    def test_malformed_entries_are_dropped(self):
        self._write({'browse_page': '', 'search_web': 7, 'api_call': '   '})
        config.reload_policy_config()
        self.assertEqual(config.TOOL_DESCRIPTIONS, {})

    def test_unknown_names_are_harmless(self):
        # Kept in the map (a tool may be added later) but never applied to
        # anything that is offered today.
        import serve
        self._write({'no_such_tool': 'x'})
        config.reload_policy_config()
        self.assertEqual(config.TOOL_DESCRIPTIONS, {'no_such_tool': 'x'})
        self.assertTrue(all(t['function']['name'] != 'no_such_tool'
                            for t in serve.AGENT_ASK_TOOLS))

    def test_missing_file_leaves_inline_text_untouched(self):
        import serve
        # Same normalization as the override test: compare reload-to-reload,
        # not shipped-file text against a reload rebuild.
        config.reload_policy_config()
        original = self._served('browse_page')
        config.reload_policy_config()
        self.assertEqual(self._served('browse_page'), original)

    def test_reload_mutates_the_live_dict_in_place(self):
        ref = config.TOOL_DESCRIPTIONS
        self._write({'browse_page': 'Edited.'})
        config.reload_policy_config()
        self.assertIs(config.TOOL_DESCRIPTIONS, ref)
        self.assertEqual(ref, {'browse_page': 'Edited.'})


class ActionTaxonomyTest(unittest.TestCase):
    """world/actions.json feeds ceremony/progress/rework accounting and the
    /api/log filter. The passport hash-chain list stays in serve.py."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-actions-test-')
        self._path = os.path.join(self._tmp, 'actions.json')
        self._patch = unittest.mock.patch.object(config, 'ACTIONS_PATH', self._path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        config.reload_policy_config()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, data):
        with open(self._path, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def test_shipped_file_matches_defaults(self):
        config.reload_policy_config()
        self.assertEqual(config.CEREMONY_ACTIONS, list(config.CEREMONY_ACTIONS_DEFAULT))
        self.assertEqual(config.PROGRESS_ACTIONS, list(config.PROGRESS_ACTIONS_DEFAULT))
        self.assertEqual(config.REWORK_ACTIONS, list(config.REWORK_ACTIONS_DEFAULT))
        self.assertEqual(config.API_LOG_ALLOWED_ACTIONS,
                         set(config.API_LOG_ALLOWED_ACTIONS_DEFAULT))

    def test_edit_applies_live_to_serve_without_rebinding(self):
        import serve
        ceremony_ref = config.CEREMONY_ACTIONS
        api_log_ref = config.API_LOG_ALLOWED_ACTIONS
        self._write({'ceremony_actions': ['new_ceremony'],
                     'progress_actions': ['new_progress'],
                     'rework_actions': ['new_rework'],
                     'api_log_allowed_actions': ['new_log']})
        config.reload_policy_config()
        self.assertIs(config.CEREMONY_ACTIONS, ceremony_ref)
        self.assertIs(config.API_LOG_ALLOWED_ACTIONS, api_log_ref)
        self.assertEqual(serve._CEREMONY_ACTIONS, ['new_ceremony'])
        self.assertEqual(serve._PROGRESS_ACTIONS, ['new_progress'])
        self.assertEqual(serve._REWORK_ACTIONS, ['new_rework'])
        self.assertEqual(serve._API_LOG_ALLOWED_ACTIONS, {'new_log'})

    def test_empty_section_keeps_its_default(self):
        self._write({'ceremony_actions': [], 'api_log_allowed_actions': ['']})
        config.reload_policy_config()
        self.assertEqual(config.CEREMONY_ACTIONS, list(config.CEREMONY_ACTIONS_DEFAULT))
        self.assertEqual(config.API_LOG_ALLOWED_ACTIONS,
                         set(config.API_LOG_ALLOWED_ACTIONS_DEFAULT))

    def test_hashed_actions_stay_in_the_kernel(self):
        import serve
        config.reload_policy_config()
        self.assertTrue(serve._HASHED_ACTIONS)
        self.assertFalse(hasattr(config, 'HASHED_ACTIONS'))


class VillagePolicyOverlaysTest(unittest.TestCase):
    """endpoint_policy.json, rooms.json and seed_profiles.json are overlay
    files: they extend or override the kernel defaults and can never shrink
    the kernel's dormancy safety set."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-village-test-')
        self._patch = unittest.mock.patch.multiple(
            config,
            ENDPOINT_POLICY_PATH=os.path.join(self._tmp, 'endpoint_policy.json'),
            ROOMS_PATH=os.path.join(self._tmp, 'rooms.json'),
            SEED_PROFILES_PATH=os.path.join(self._tmp, 'seed_profiles.json'),
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        config.reload_policy_config()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, name, data):
        with open(os.path.join(self._tmp, name), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def test_endpoint_extras_are_additive(self):
        import serve
        kernel_reads = set(serve._NON_WAKING_READS_BASE)
        self._write('endpoint_policy.json', {
            'extra_non_waking_reads': ['/api/new-board', 'not-a-path', ''],
            'extra_non_waking_writes': ['/api/new-ping']})
        config.reload_policy_config()
        self.assertLessEqual(kernel_reads, serve._NON_WAKING_READS)
        self.assertIn('/api/new-board', serve._NON_WAKING_READS)
        self.assertNotIn('not-a-path', serve._NON_WAKING_READS)
        self.assertEqual(serve._NON_WAKING_WRITES,
                         {'/api/device/checkin', '/a2a', '/api/new-ping'})

    def test_room_override_merges_and_delegatable_replaces(self):
        import serve
        base = dict(serve._ROOM_DEFINITION_DEFAULTS['observatory'])
        self._write('rooms.json', {
            'definitions': {'observatory': {'purpose': 'Edited purpose.'}},
            'delegatable': ['observatory']})
        config.reload_policy_config()
        self.assertEqual(serve._DEFAULT_ROOM_DEFINITIONS['observatory'],
                         {'label': base['label'], 'purpose': 'Edited purpose.'})
        self.assertEqual(serve._DELEGATABLE_ROOMS, ['observatory'])

    def test_empty_room_file_keeps_kernel_rooms(self):
        import serve
        config.reload_policy_config()
        self.assertEqual(serve._DEFAULT_ROOM_DEFINITIONS,
                         serve._ROOM_DEFINITION_DEFAULTS)
        self.assertEqual(serve._DELEGATABLE_ROOMS, serve._DELEGATABLE_ROOMS_DEFAULTS)

    def test_seed_profile_override_and_default(self):
        import serve
        self._write('seed_profiles.json', {
            'profiles': {'Research': {'mission': 'Edited mission.',
                                      'instructions': ['Do the edited thing.']}},
            'default': {'mission': 'Edited fallback.'}})
        config.reload_policy_config()
        self.assertEqual(serve._SEED_PROFILES['Research'],
                         {'mission': 'Edited mission.',
                          'instructions': ['Do the edited thing.'], 'notes': []})
        self.assertEqual(serve._DEFAULT_PROFILE['mission'], 'Edited fallback.')
        # Untouched roles keep their kernel profile.
        self.assertEqual(serve._SEED_PROFILES['Banking']['mission'],
                         serve._SEED_PROFILE_DEFAULTS['Banking']['mission'])

    def test_empty_seed_file_restores_kernel_profiles(self):
        import serve
        config.reload_policy_config()
        self.assertEqual(serve._SEED_PROFILES, serve._SEED_PROFILE_DEFAULTS)
        self.assertEqual(serve._DEFAULT_PROFILE, serve._DEFAULT_PROFILE_BASE)

    def test_invalid_profile_entries_are_dropped(self):
        self._write('seed_profiles.json', {
            'profiles': {'Research': {'mission': ''}, 'Bad': 'nope'}})
        config.reload_policy_config()
        self.assertEqual(config.SEED_PROFILE_OVERRIDES, {})


class EnvReloadTableTest(unittest.TestCase):
    """The .env reload table lives in env_reload.py; serve imports it and the
    reload loop still honors a patched serve._ENV_RELOAD_FIELDS."""

    def test_table_covers_the_budget_and_memory_keys(self):
        import core.env_reload as env_reload
        for key in ('SPEND_CAP_USD', 'OPENROUTER_API_KEY', 'MEMORY_ENABLED',
                    'BROWSE_ALLOWLIST_DOMAINS', 'TTC_BEST_OF'):
            self.assertIn(key, env_reload.ENV_RELOAD_FIELDS)

    def test_patched_table_is_still_honored_by_serve(self):
        import serve
        with unittest.mock.patch.object(serve, '_ENV_RELOAD_FIELDS',
                                        {'SPEND_CAP_USD': lambda e: 123.0}):
            out = serve._reload_env_config()
        self.assertIn('SPEND_CAP_USD', out['changed'])
        self.assertEqual(serve.SPEND_CAP_USD, 123.0)
