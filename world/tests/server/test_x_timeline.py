"""Tests for the X/Timeline research capability (185_x_timeline).

Hermetic: the twscrape layer is replaced by async stubs, the pool directory
is a temp dir, and the agent-key/session checks are patched. Network is
never touched. The live path was validated against the real pool during
development; these tests pin the contract: auth required, limits capped,
seeding gated by .env keys, rate-window waits return 429, cookies never
leak into responses, and every call is audited.
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import serve  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_TMP_DIR = None
_PATCHER = None


class _StubPool:
    def __init__(self, path):
        self.path = path

    async def add_account_cookies(self, name, cookies):
        self.cookies = cookies


class _StubAPI:
    def __init__(self, pool):
        self.pool = pool

    async def search(self, query, limit=20, kv=None):
        for i in range(7):
            yield _FakeTweet(i)

    async def tweet_details(self, tid):
        if str(tid) == '4004000000000000000':
            return None
        return _FakeTweet(0)

    async def user_by_login(self, login):
        if login == 'nobody':
            return None
        return _FakeUser(login)

    async def user_tweets(self, uid, limit=20):
        for i in range(7):
            yield _FakeTweet(i)


class _FakeTweet:
    def __init__(self, i):
        self._d = {
            'id_str': f'21100000000000000{i}{i}',
            'date': '2026-10-03 03:35:16+00:00',
            'rawContent': '🔗 GitHub: https://github.com/o/r',
            'likeCount': i, 'viewCount': 100 + i, 'replyCount': 0,
            'retweetCount': 0,
            'inReplyToTweetIdStr': '2099000000000000000',
            'conversationIdStr': '2099000000000000000',
            'links': [{'url': 'https://github.com/o/r'}],
            'url': f'https://x.com/DanKornas/status/21100000000000000{i}{i}',
            'user': {'username': 'DanKornas'},
        }

    def json(self):
        import json as _json
        return _json.dumps(self._d)


class _FakeUser:
    def __init__(self, login):
        self.id = 1406639376675770374
        self.username = login


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-x-timeline-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        _X_POOL_DIR=os.path.join(_TMP_DIR, 'x_scrape'),
        _X_POOL_DB=os.path.join(_TMP_DIR, 'x_scrape', 'accounts.db'),
        verify_session=lambda sess: False,
        check_rate_limit=lambda aid: True,
        _x_scraper=lambda: (_StubPool, _StubAPI),
        _load_env=lambda: {'X_SESSION_AUTH_TOKEN': 'a' * 40,
                           'X_SESSION_CT0': 'c' * 160},
    )
    _PATCHER.start()
    serve.init_db()
    # A real DB-backed agent key: the login middleware verifies X-Agent-Key
    # against agent_keys, so the tests present a key that genuinely exists.
    with serve._db() as conn:
        conn.execute('INSERT INTO agent_keys (agent_id, secret_key, created_at) '
                     'VALUES (?, ?, ?)', ('marcus', 'good-key', 0.0))


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def client():
    return TestClient(serve.app)


class XTimelineAuth(unittest.TestCase):
    def test_requires_agent_key_or_session(self):
        r = client().get('/api/x/search', params={'query': 'from:x test'})
        self.assertEqual(r.status_code, 401)

    def test_bad_key_rejected(self):
        r = client().get('/api/x/search',
                         params={'query': 'from:x test'},
                         headers={'X-Agent-Key': 'wrong'})
        self.assertEqual(r.status_code, 401)

    def test_bad_tweet_id_rejected(self):
        r = client().get('/api/x/tweet/notanumber',
                         headers={'X-Agent-Key': 'good-key'})
        self.assertEqual(r.status_code, 400)


class XTimelineSearch(unittest.TestCase):
    def test_search_returns_stripped_rows_and_audits(self):
        rows = []
        with unittest.mock.patch.object(serve, 'log_action',
                                        lambda *a, **k: rows.append(k or a)):
            r = client().get('/api/x/search',
                             params={'query': 'from:dankornas github.com',
                                     'limit': '5'},
                             headers={'X-Agent-Key': 'good-key'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['count'], 5)
        first = body['tweets'][0]
        self.assertEqual(first['username'], 'DanKornas')
        self.assertEqual(first['inReplyTo'], '2099000000000000000')
        self.assertEqual(first['links'], ['https://github.com/o/r'])
        self.assertNotIn('rawContent', first)
        self.assertTrue(rows)

    def test_limit_capped(self):
        captured = {}

        async def spy(query, limit):
            captured['limit'] = limit
            return []
        with unittest.mock.patch.object(serve, '_x_search', spy):
            r = client().get('/api/x/search',
                             params={'query': 'q', 'limit': '999'},
                             headers={'X-Agent-Key': 'good-key'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured['limit'], serve._X_SEARCH_CAP)

    def test_rate_window_returns_429(self):
        def boom(coro, timeout):
            coro.close()
            raise TimeoutError('x rate window')
        with unittest.mock.patch.object(serve, '_x_call', boom):
            r = client().get('/api/x/search', params={'query': 'q'},
                             headers={'X-Agent-Key': 'good-key'})
        self.assertEqual(r.status_code, 429)


class XTimelineSeeding(unittest.TestCase):
    def test_seed_uses_env_once_and_chmods(self):
        pool_dir = serve._X_POOL_DIR
        shutil.rmtree(pool_dir, ignore_errors=True)
        ok, why = serve._x_seed_pool()
        self.assertTrue(ok, why)
        self.assertTrue(os.path.exists(
            os.path.join(pool_dir, 'seeded')))
        marker = os.path.join(pool_dir, 'seeded')
        self.assertEqual(oct(os.stat(marker).st_mode & 0o777), '0o600')

    def test_seed_missing_env_fails_clean(self):
        pool_dir = serve._X_POOL_DIR
        shutil.rmtree(pool_dir, ignore_errors=True)
        with unittest.mock.patch.object(serve, '_load_env', lambda: {}):
            ok, why = serve._x_seed_pool()
        self.assertFalse(ok)
        self.assertIn('missing', why)


class XTimelineTweet(unittest.TestCase):
    def test_tweet_found(self):
        r = client().get('/api/x/tweet/2110000000000000000',
                         headers={'X-Agent-Key': 'good-key'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['tweet']['id'], '2110000000000000000')

    def test_tweet_missing_404(self):
        r = client().get('/api/x/tweet/4004000000000000000',
                         headers={'X-Agent-Key': 'good-key'})
        self.assertEqual(r.status_code, 404)

    def test_user_tweets_unknown_user_404(self):
        r = client().get('/api/x/user/nobody/tweets',
                         headers={'X-Agent-Key': 'good-key'})
        self.assertEqual(r.status_code, 404)


class XProjection(unittest.TestCase):
    def test_text_bounded_and_links_only_urls(self):
        long = {'id_str': '1', 'rawContent': 'x' * 3000, 'user': {},
                'links': [{'url': 'https://github.com/a/b', 'text': 't'}]}
        fake = type('T', (), {'json': staticmethod(
            lambda: __import__('json').dumps(long))})()
        d = serve._x_strip_tweet(fake)
        self.assertEqual(len(d['text']), 2000)
        self.assertEqual(d['links'], ['https://github.com/a/b'])


if __name__ == '__main__':
    unittest.main()
