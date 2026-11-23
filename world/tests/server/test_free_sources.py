# Tests for the free-source search lane (362): adapter parsing, merged
# fan-out, standalone-shim, and the 240 free-first wiring -- NO network
# in tests; all source calls faked.
import os
import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

import serve  # noqa: E402


def _hit(source='hn'):
    return {'title': f'{source} hit', 'url': 'https://example.com/x',
            'snippet': 's', 'source': source}


class FreeSourceAdapters(unittest.TestCase):
    def test_wiki_opensearch_parse(self):
        payload = ('["q",["A","B"],["",""],'
                   '["https://en.wikipedia.org/wiki/A",'
                   '"https://en.wikipedia.org/wiki/B"]]')
        with mock.patch.object(serve, '_fetch', return_value=(200, payload)):
            hits = serve._wiki('q')
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0]['source'], 'wiki')
        self.assertTrue(hits[0]['url'].startswith('https://en.wikipedia.org/'))

    def test_hn_parse(self):
        payload = ('{"hits":[{"title":"t","objectID":"1","url":"https://x.com/a"},'
                   '{"title":"u","objectID":"2"}]}')
        with mock.patch.object(serve, '_fetch', return_value=(200, payload)):
            hits = serve._hn('q')
        self.assertEqual(len(hits, ), 2)
        self.assertIn('news.ycombinator.com/item?id=2', hits[1]['url'])
    def test_arxiv_parse(self):
        payload = ('<entry><title>A Title</title>'
                   '<id>http://arxiv.org/abs/1</id>'
                   '<summary>Sum</summary></entry>')
        with mock.patch.object(serve, '_fetch', return_value=(200, payload)):
            hits = serve._arxiv('q')
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['source'], 'arxiv')

    def test_adapter_failures_return_empty(self):
        for fn in (serve._hn, serve._wiki, serve._github, serve._arxiv):
            with mock.patch.object(serve, '_fetch',
                                   return_value=(0, 'boom')):
                self.assertEqual(fn('q'), [])


class MergedSearch(unittest.TestCase):
    def test_merge_caps_at_twelve(self):
        hits = [_hit('hn')] * 20
        with mock.patch.object(serve, '_hn', lambda q: hits), \
             mock.patch.object(serve, '_wiki', lambda q: []), \
             mock.patch.object(serve, '_github', lambda q: []), \
             mock.patch.object(serve, '_arxiv', lambda q: []), \
             mock.patch.object(serve, 'log_action'):
            out = serve._merged_search('a1', 'q')
        self.assertEqual(out.count('- '), 12)

    def test_merge_returns_none_when_all_fail(self):
        def boom(q):
            raise Exception('down')
        with mock.patch.object(serve, '_hn', boom), \
             mock.patch.object(serve, '_wiki', boom), \
             mock.patch.object(serve, '_github', boom), \
             mock.patch.object(serve, '_arxiv', boom), \
             mock.patch.object(serve, 'log_action'):
            self.assertIsNone(serve._merged_search('a1', 'q'))

    def test_standalone_shim_tolerates_no_serve(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'fs_standalone',
            os.path.join(os.path.dirname(os.path.abspath(serve.__file__)),
                'server', 'serve_parts', '362_free_sources.py'))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        with mock.patch.object(m, '_hn', lambda q: []), \
             mock.patch.object(m, '_wiki', lambda q: []), \
             mock.patch.object(m, '_github', lambda q: []), \
             mock.patch.object(m, '_arxiv', lambda q: []):
            self.assertIsNone(m._merged_search(None, 'q'))


class FreeFirstWiring(unittest.TestCase):
    """search_web in the shared web-tools executor must prefer the free
    lane and only fall through to Tavily when it returns nothing."""

    def _ex(self):
        return serve._make_web_tools_executor('oliver', 'test')

    def test_free_first_precedence(self):
        with mock.patch.object(serve, '_merged_search',
                               return_value='- HN hit (https://example.com/h): s'), \
             mock.patch.object(serve, '_api_execute') as api:
            out = self._ex()('search_web', {'query': 'q'})
        self.assertIn('HN hit', out)
        api.assert_not_called()

    def test_tavily_fallback_when_free_empty(self):
        with mock.patch.object(serve, '_merged_search', return_value=None), \
             mock.patch.object(serve, '_api_execute',
                               return_value={'ok': True,
                                             'modelInstruction': 'mi',
                                             'textForModel': 'txt'}):
            out = self._ex()('search_web', {'query': 'q'})
        self.assertEqual(out, 'mi\n\ntxt')
