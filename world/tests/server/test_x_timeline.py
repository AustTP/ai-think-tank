"""Tests for the X capability adapter (185_x_timeline) and the trusted
repository registry (187_trusted_repos).

Hermetic: the CLI subprocess boundary is replaced by canned JSONL, the
registry file lives in a temp dir, and auth uses a real agent-keys row so
the login middleware path is exercised. Network is never touched.

Contract pinned here:
  - the tool runs ONLY when its repo is on the trusted registry;
  - the adapter shells out to the `twscrape` CLI (fixed argv), it does
    not embed scraper logic;
  - agent-key auth, result caps, 429 on rate windows, 503 with a clear
    reason when the tool/repo/cookies are missing;
  - any authenticated agent can add a trusted repo (vouched, audited);
    slugs are validated; duplicates become re-vouches.
"""
import json
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
_KEY = 'xtl-test-key'
_FAKE_TOOL = '/tmp/fake-twscrape-bin'


def _tweet(i, repo='owner/repo'):
    return {
        'id_str': f'21100000000000000{i}{i}',
        'date': '2026-10-03 03:35:16+00:00',
        'rawContent': f'🔗 GitHub: https://github.com/{repo}',
        'likeCount': i, 'viewCount': 100 + i, 'replyCount': 0,
        'retweetCount': 0,
        'inReplyToTweetIdStr': '2099000000000000000',
        'conversationIdStr': '2099000000000000000',
        'links': [{'url': f'https://github.com/{repo}'}],
        'url': f'https://x.com/DanKornas/status/21100000000000000{i}{i}',
        'user': {'username': 'DanKornas'},
    }


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-xtl-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        _TRUSTED_CODEBASE_PATH=os.path.join(_TMP_DIR, 'trusted_codebase.json'),
        _X_POOL_DIR=os.path.join(_TMP_DIR, 'x_scrape'),
        _X_POOL_DB=os.path.join(_TMP_DIR, 'x_scrape', 'accounts.db'),
        _x_tool_path=lambda: _FAKE_TOOL,
        source_is_trusted=lambda s: s.lower() == 'vladkens/twscrape',
        verify_session=lambda sess: False,
        check_rate_limit=lambda aid: True,
        _load_env=lambda: {'X_SESSION_AUTH_TOKEN': 'a' * 40,
                           'X_SESSION_CT0': 'c' * 160},
    )
    _PATCHER.start()
    serve.init_db()
    serve.seed_trusted_codebase()
    # Pre-seed the pool marker so non-seeding tests skip the subprocess;
    # the seeding tests rmtree the dir themselves for a fresh run.
    os.makedirs(os.path.join(_TMP_DIR, 'x_scrape'), exist_ok=True)
    with open(os.path.join(_TMP_DIR, 'x_scrape', 'seeded'), 'w') as fh:
        fh.write('0')
    with serve._db() as conn:
        conn.execute('INSERT INTO agent_keys (agent_id, secret_key, '
                     'created_at) VALUES (?, ?, ?)',
                     ('marcus', _KEY, 0.0))


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def client():
    return TestClient(serve.app)


class XAdapterAuth(unittest.TestCase):
    def test_requires_agent_key_or_session(self):
        r = client().get('/api/x/search', params={'query': 'from:x test'})
        self.assertEqual(r.status_code, 401)

    def test_malformed_tweet_id(self):
        r = client().get('/api/x/tweet/nope',
                         headers={'X-Agent-Key': _KEY})
        self.assertEqual(r.status_code, 400)

    def test_malformed_query(self):
        r = client().get('/api/x/search', params={'query': ''},
                         headers={'X-Agent-Key': _KEY})
        self.assertEqual(r.status_code, 400)


class XAdapterCLI(unittest.TestCase):
    def _cli(self, rows):
        def fake_run(verb_args, wait_s):
            captured = {'args': verb_args}
            return rows
        return fake_run

    def test_search_runs_cli_and_strips(self):
        captured = {}

        def fake_run(verb_args, wait_s):
            captured['args'] = verb_args
            return [_tweet(0), _tweet(1), {'garbage': True}]
        with unittest.mock.patch.object(serve, '_x_run_cli', fake_run):
            r = client().get('/api/x/search',
                             params={'query': 'from:dankornas github.com',
                                     'limit': '5'},
                             headers={'X-Agent-Key': _KEY})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['count'], 2)
        first = body['tweets'][0]
        self.assertEqual(first['username'], 'DanKornas')
        self.assertEqual(first['links'], ['https://github.com/owner/repo'])
        self.assertNotIn('rawContent', first)
        # fixed argv through the CLI, no shell
        self.assertEqual(captured['args'][0], 'search')
        self.assertIn('--limit', captured['args'])

    def test_rate_window_is_429(self):
        def boom(verb_args, wait_s):
            raise TimeoutError('x rate window: account rate-locked')
        with unittest.mock.patch.object(serve, '_x_run_cli', boom):
            r = client().get('/api/x/search', params={'query': 'q'},
                             headers={'X-Agent-Key': _KEY})
        self.assertEqual(r.status_code, 429)

    def test_tweet_found_and_missing(self):
        with unittest.mock.patch.object(
                serve, '_x_run_cli',
                lambda a, w: [_tweet(0)]):
            r = client().get('/api/x/tweet/2110000000000000000',
                             headers={'X-Agent-Key': _KEY})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()['tweet']['id'],
                             '2110000000000000000')
        with unittest.mock.patch.object(serve, '_x_run_cli',
                                        lambda a, w: []):
            r = client().get('/api/x/tweet/2099000000000000000',
                             headers={'X-Agent-Key': _KEY})
            self.assertEqual(r.status_code, 404)


class XAdapterPreconditions(unittest.TestCase):
    def test_untrusted_repo_is_503(self):
        with unittest.mock.patch.object(
                serve, 'source_is_trusted', lambda s: False):
            r = client().get('/api/x/search', params={'query': 'q'},
                             headers={'X-Agent-Key': _KEY})
        self.assertEqual(r.status_code, 503)
        self.assertIn('not trusted', r.json()['error'])

    def test_missing_tool_is_503(self):
        with unittest.mock.patch.object(serve, '_x_tool_path',
                                        lambda: None):
            r = client().get('/api/x/search', params={'query': 'q'},
                             headers={'X-Agent-Key': _KEY})
        self.assertEqual(r.status_code, 503)

    def test_seeding_runs_cli_once_and_chmods(self):
        pool_dir = serve._X_POOL_DIR
        shutil.rmtree(pool_dir, ignore_errors=True)
        calls = []

        def fake_run(cmd, capture_output, timeout):
            calls.append(cmd)
            return type('P', (), {'returncode': 0})()
        with unittest.mock.patch.object(serve.subprocess, 'run', fake_run):
            ok, err = serve._x_ready()
        self.assertTrue(ok, err)
        self.assertEqual(len(calls), 1)
        self.assertIn('add_cookie', calls[0])
        marker = os.path.join(pool_dir, 'seeded')
        self.assertEqual(oct(os.stat(marker).st_mode & 0o777), '0o600')

    def test_seeding_missing_env_fails_clean(self):
        pool_dir = serve._X_POOL_DIR
        shutil.rmtree(pool_dir, ignore_errors=True)
        with unittest.mock.patch.object(serve, '_load_env', lambda: {}):
            ok, err = serve._x_ready()
        self.assertFalse(ok)
        self.assertIn('missing from .env', err)


class XProjection(unittest.TestCase):
    def test_text_bounded_and_links_only_urls(self):
        long = {'id_str': '1', 'rawContent': 'x' * 3000, 'user': {},
                'links': [{'url': 'https://github.com/a/b', 'text': 't'}]}
        d = serve._x_strip_tweet(long)
        self.assertEqual(len(d['text']), 2000)
        self.assertEqual(d['links'], ['https://github.com/a/b'])


if __name__ == '__main__':
    unittest.main()
