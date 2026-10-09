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


if __name__ == '__main__':
    unittest.main()
