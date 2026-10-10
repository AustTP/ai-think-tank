"""RSS aggregator + Kraken market ticker.

RSS: a poll loop fetches curated RSS 2.0/ATOM feeds, a tolerant parser
extracts items, and an in-process ring buffer per feed serves
read_rss_feed / /api/rss/recent. Items are tagged at ingest with any
watchlist research topics whose words appear in the title.

Kraken: a WebSocket collector mirrors the AISStream pattern -- no key, public
market data, latest bid/ask/price per pair in-process, served by
read_market_feed / /api/market/quote.

Hermetic: no live network, no real DB writes (temp dir).
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None

RSS_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Test</title>
<item><title>Big quake near the coast</title><link>http://x.test/1</link>
<description>&lt;b&gt;Strong&lt;/b&gt; shaking reported.</description></item>
<item><title>Second item</title><link>http://x.test/2</link>
<description>No summary.</description></item>
</channel></rss>'''

ATOM_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title>
<entry><title>Space launch tonight</title><link href="http://a.test/1"/>
<summary type="html">&lt;p&gt;Liftoff at 9pm.&lt;/p&gt;</summary></entry>
<entry><title>No link here</title></entry>
</feed>'''


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-rss-market-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class RSSParser(unittest.TestCase):
    def test_parses_rss_and_atom(self):
        rss = serve._parse_rss_atom_xml(RSS_XML, 'test')
        self.assertEqual(len(rss), 2)
        self.assertEqual(rss[0]['title'], 'Big quake near the coast')
        self.assertEqual(rss[0]['link'], 'http://x.test/1')
        self.assertIn('Strong shaking', rss[0]['summary'])
        atom = serve._parse_rss_atom_xml(ATOM_XML, 'test')
        self.assertEqual(atom[0]['link'], 'http://a.test/1')
        self.assertIn('Liftoff', atom[0]['summary'])
        self.assertEqual(atom[1]['title'], 'No link here')  # title-only entry still parsed
        self.assertEqual(atom[1]['link'], '')

    def test_bad_xml_returns_empty(self):
        self.assertEqual(serve._parse_rss_atom_xml(b'<broken', 'test'), [])


class RSSBuffer(unittest.TestCase):
    def tearDown(self):
        with serve._RSS_BUFFER_LOCK:
            serve._RSS_BUFFER.clear()

    def test_ingest_dedups_and_caps(self):
        serve._rss_ingest('test', [{'title': 'a', 'link': 'http://x/1', 'summary': ''}] * 2)
        serve._rss_ingest('test', [{'title': 'a', 'link': 'http://x/1', 'summary': ''}])
        serve._rss_ingest('test', [{'title': 'b', 'link': 'http://x/2', 'summary': ''}])
        with serve._RSS_BUFFER_LOCK:
            self.assertEqual(len(serve._RSS_BUFFER['test']), 2)

    def test_recent_filters_window_and_max(self):
        old = {'ts': int(time.time() * 1000) - 10 * 86400_000,
               'title': 'old', 'link': 'http://x/o', 'summary': '', 'matchedTopics': []}
        fresh = [{'ts': int(time.time() * 1000), 'title': f'n{i}', 'link': f'http://x/{i}',
                  'summary': '', 'matchedTopics': []} for i in range(4)]
        with serve._RSS_BUFFER_LOCK:
            serve._RSS_BUFFER['test'] = [old] + fresh
        out = serve._rss_recent('test', max_items=2)
        self.assertEqual([i['title'] for i in out], ['n2', 'n3'])

    def test_match_topics_tags_ingested_items(self):
        state = {'researchTopics': [{'topic': 'coastal flooding'}]}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state):
            serve._rss_ingest('test', [{'title': 'Coastal flooding risk rises', 'link': 'http://x/f',
                                        'summary': ''}])
        with serve._RSS_BUFFER_LOCK:
            self.assertIn('coastal flooding', serve._RSS_BUFFER['test'][-1]['matchedTopics'])


class RSSEndpoint(unittest.TestCase):
    def test_auth_and_data(self):
        from starlette.testclient import TestClient
        with serve._RSS_BUFFER_LOCK:
            serve._RSS_BUFFER.clear()
            serve._RSS_BUFFER['bbc_world'] = [{
                'ts': int(time.time() * 1000), 'title': 'Headline story',
                'link': 'http://x/1', 'summary': 's', 'matchedTopics': []}]
        c = TestClient(serve.app)
        self.assertEqual(c.get('/api/rss/recent').status_code, 401)
        key = serve.get_or_create_agent_key('ben')
        r = c.get('/api/rss/recent?agentId=ben&feed=bbc_world', headers={'X-Agent-Key': key})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['feeds']['bbc_world']['items'][0]['title'], 'Headline story')
        r = c.get('/api/rss/recent?agentId=ben&feed=nope', headers={'X-Agent-Key': key})
        self.assertEqual(r.status_code, 400)
        with serve._RSS_BUFFER_LOCK:
            serve._RSS_BUFFER.clear()


class KrakenBuffer(unittest.TestCase):
    def tearDown(self):
        with serve._KRAKEN_LOCK:
            serve._KRAKEN_LATEST.clear()
            serve._KRAKEN_HISTORY.clear()

    def test_ingest_latest_per_pair_and_history_cap(self):
        for i in range(serve._KRAKEN_HISTORY_MAX + 10):
            serve._kraken_ingest_tick(i, 'XBT/USD', 100.0 + i, 99.0, 101.0, 5)
        with serve._KRAKEN_LOCK:
            self.assertEqual(len(serve._KRAKEN_HISTORY), serve._KRAKEN_HISTORY_MAX)
            self.assertEqual(serve._KRAKEN_LATEST['XBT/USD']['price'], 100.0 + serve._KRAKEN_HISTORY_MAX + 9)
        q = serve._kraken_quotes(['XBT/USD'])
        self.assertEqual(set(q), {'XBT/USD'})
        self.assertNotIn('ETH/USD', q)

    def test_quote_endpoint(self):
        from starlette.testclient import TestClient
        with serve._KRAKEN_LOCK:
            serve._KRAKEN_LATEST['XBT/USD'] = {'price': 100.0, 'bid': 99.0, 'ask': 101.0,
                                               'volume': 5, 'ts': int(time.time() * 1000)}
        c = TestClient(serve.app)
        self.assertEqual(c.get('/api/market/quote').status_code, 401)
        key = serve.get_or_create_agent_key('ben')
        r = c.get('/api/market/quote?agentId=ben&pair=btc-usd', headers={'X-Agent-Key': key})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('XBT/USD', r.json()['quotes'])
        with serve._KRAKEN_LOCK:
            serve._KRAKEN_LATEST.clear()


class Detectors(unittest.TestCase):
    def setUp(self):
        import content
        self.content = content

    def test_rss_detector(self):
        for q in ('any breaking news right now', 'latest earthquake?', 'new NASA announcements',
                  'recent arxiv papers on LLMs', 'what is happening in the world'):
            self.assertTrue(self.content._spike_wants_rss(q, None), q)
        for q in ('how does the engine work', 'which ships are at sea'):
            self.assertFalse(self.content._spike_wants_rss(q, None), q)

    def test_market_detector_narrow(self):
        for q in ('price of bitcoin', 'what is ETH worth', 'btc to usd?', 'crypto market update'):
            self.assertTrue(self.content._spike_wants_market(q, None), q)
        for q in ('SPY closing price today', 'how much is a gallon of milk'):
            self.assertFalse(self.content._spike_wants_market(q, None), q)


if __name__ == '__main__':
    unittest.main()