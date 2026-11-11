"""Podcast transcript extraction (2026-11-11): thin adapter over the
publisher's own RSS feed -- the podcasting-2.0 `podcast:transcript` tag.
Same design rules as the X reader and the YouTube transcript path: open
data, no metered API, honest errors. The network seam is stubbed; no live
fetch, no DB."""
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import unittest  # noqa: E402

import serve  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

# The app's auth middleware requires a session or a valid agent key; these
# tests exercise the transcript path itself, not auth, so a key is "valid".
# Module lifecycle (not import time): other suites patch the same middleware,
# and a collection-time start would be silently undone by their stop.
_AUTH = None


def setUpModule():
    global _AUTH
    _AUTH = mock.patch.object(serve, '_valid_agent_key_presented',
                              return_value=True)
    _AUTH.start()


def tearDownModule():
    if _AUTH:
        _AUTH.stop()

_FEED = b'''<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:podcast="https://podcastindex.org/namespace/1.0" version="2.0">
<channel><title>Example Show</title>
<item>
  <title>Episode 2: The Follow-up</title>
  <guid>ep-2</guid>
  <enclosure url="https://cdn.example.com/ep2.mp3" type="audio/mpeg"/>
  <podcast:transcript url="https://cdn.example.com/ep2.vtt" type="text/vtt"/>
</item>
<item>
  <title>Episode 1: The Beginning</title>
  <guid>ep-1</guid>
  <enclosure url="https://cdn.example.com/ep1.mp3" type="audio/mpeg"/>
  <podcast:transcript url="https://cdn.example.com/ep1.json"
                      type="application/json"/>
  <podcast:transcript url="https://cdn.example.com/ep1-shownotes.html"
                      type="text/html" rel="shownotes"/>
</item>
</channel></rss>'''

_EP1_JSON = json.dumps({'segments': [{'text': 'Hello '}, {'text': 'world.'}]})
_EP2_VTT = ('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nSecond episode words.\n\n'
            '00:00:03.000 --> 00:00:05.000\nSecond episode words.\n\n'
            '00:00:05.000 --> 00:00:07.000\nMore words here.\n')


class _FakeResponse:
    def __init__(self, body, content_type):
        self._body, self._ct = body, content_type

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getheader(self, name, default=None):
        return self._ct if name.lower() == 'content-type' else default

    @property
    def headers(self):
        return {'Content-Type': self._ct}


def _stub_fetch(routes):
    def fake(url, max_bytes=serve.PODCAST_TRANSCRIPT_MAX_BYTES):
        for key, (body, ct) in routes.items():
            if key in url:
                return body, ct
        raise ValueError(f'unstubbed URL: {url}')
    return fake


class PodcastFeedParsingTests(unittest.TestCase):
    def test_parse_feed_collects_transcript_tags_namespace_agnostic(self):
        items = serve._podcast_parse_feed(_FEED)
        self.assertEqual(2, len(items))
        self.assertEqual('ep-2', items[0]['guid'])  # feed order kept
        self.assertEqual(1, len(items[0]['transcripts']))
        # ep-1 carries a real transcript AND a shownotes file; both collected.
        self.assertEqual(2, len(items[1]['transcripts']))

    def test_parse_feed_rejects_garbage(self):
        with self.assertRaises(ValueError):
            serve._podcast_parse_feed(b'this is not xml <')

    def test_shownotes_never_wins_over_a_real_transcript(self):
        items = serve._podcast_parse_feed(_FEED)
        pick = serve._podcast_pick_transcript(items[1]['transcripts'])
        self.assertEqual('https://cdn.example.com/ep1.json', pick['url'])
        # Format preference: json > plain > srt > vtt > html.
        tags = [{'url': 'a.html', 'type': 'text/html', 'rel': ''},
                {'url': 'b.vtt', 'type': 'text/vtt', 'rel': ''}]
        self.assertEqual('b.vtt', serve._podcast_pick_transcript(tags)['url'])

    def test_episode_matching_guid_audio_then_latest_default(self):
        items = serve._podcast_parse_feed(_FEED)
        self.assertEqual('ep-1', serve._podcast_select_item(items, 'ep-1')['guid'])
        self.assertEqual('ep-1', serve._podcast_select_item(
            items, 'cdn.example.com/ep1.mp3')['guid'])
        self.assertEqual('ep-1', serve._podcast_select_item(
            items, 'Beginning')['guid'])
        self.assertEqual('ep-2', serve._podcast_select_item(items, '')['guid'])
        self.assertIsNone(serve._podcast_select_item(items, 'no-such-episode'))


class PodcastCleanAndFetchTests(unittest.TestCase):
    def test_json_transcript_joins_segments(self):
        text = serve._podcast_clean_transcript(
            _EP1_JSON.encode(), 'application/json')
        self.assertEqual('Hello world.', text)

    def test_vtt_transcript_strips_timing_and_dedupes(self):
        text = serve._podcast_clean_transcript(
            _EP2_VTT.encode(), 'text/vtt', 'https://x/ep2.vtt')
        self.assertIn('Second episode words.', text)
        self.assertIn('More words here.', text)
        self.assertNotIn('-->', text)

    def test_html_transcript_strips_markup(self):
        text = serve._podcast_clean_transcript(
            b'<html><body><p>Shownotes text.</p></body></html>', 'text/html')
        self.assertIn('Shownotes text.', text)
        self.assertNotIn('<p>', text)

    def test_private_hosts_are_refused(self):
        with self.assertRaises(ValueError):
            serve._podcast_fetch('http://127.0.0.1:8937/feed.xml')
        with self.assertRaises(ValueError):
            serve._podcast_fetch('http://user:pass@example.com/feed.xml')
        with self.assertRaises(ValueError):
            serve._podcast_fetch('ftp://example.com/feed.xml')

    def test_long_transcripts_are_capped(self):
        text = serve._podcast_clean_transcript(
            ('word ' * 60000).encode(), 'text/plain')
        self.assertLessEqual(len(text), serve.PODCAST_TRANSCRIPT_MAX_CHARS)


class PodcastTranscriptEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(serve.app)

    def test_latest_episode_via_feed_url(self):
        routes = {
            'example.com/feed.xml': (_FEED, 'application/rss+xml'),
            'ep2.vtt': (_EP2_VTT.encode(), 'text/vtt'),
        }
        with mock.patch.object(serve, '_podcast_fetch',
                               side_effect=_stub_fetch(routes)), \
                mock.patch.object(serve, 'log_action'):
            r = self.client.get('/api/podcast-transcript',
                                params={'url': 'https://example.com/feed.xml'})
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertEqual('Episode 2: The Follow-up', body['episode'])
        self.assertIn('Second episode words.', body['text'])

    def test_episode_selector_and_shownotes_exclusion(self):
        routes = {
            'example.com/feed.xml': (_FEED, 'application/rss+xml'),
            'ep1.json': (_EP1_JSON.encode(), 'application/json'),
        }
        with mock.patch.object(serve, '_podcast_fetch',
                               side_effect=_stub_fetch(routes)), \
                mock.patch.object(serve, 'log_action'):
            r = self.client.get('/api/podcast-transcript',
                                params={'url': 'https://example.com/feed.xml',
                                        'episode': 'ep-1'})
        self.assertEqual(200, r.status_code)
        self.assertEqual('Hello world.', r.json()['text'])

    def test_feed_resolved_from_a_show_page(self):
        page = (b'<html><head><link rel="alternate" type="application/rss+xml" '
                b'href="https://example.com/feed.xml"></head><body>show</body></html>')
        routes = {
            'example.com/show': (page, 'text/html'),
            'example.com/feed.xml': (_FEED, 'application/rss+xml'),
            'ep2.vtt': (_EP2_VTT.encode(), 'text/vtt'),
        }
        with mock.patch.object(serve, '_podcast_fetch',
                               side_effect=_stub_fetch(routes)), \
                mock.patch.object(serve, 'log_action'):
            r = self.client.get('/api/podcast-transcript',
                                params={'url': 'https://example.com/show'})
        self.assertEqual(200, r.status_code)
        self.assertEqual('https://example.com/feed.xml', r.json()['feed'])

    def test_no_transcript_is_an_honest_error_with_the_recipe(self):
        feed = _FEED.replace(
            b'<podcast:transcript url="https://cdn.example.com/ep2.vtt" type="text/vtt"/>',
            b'')
        routes = {
            'example.com/feed.xml': (feed, 'application/rss+xml'),
        }
        with mock.patch.object(serve, '_podcast_fetch',
                               side_effect=_stub_fetch(routes)), \
                mock.patch.object(serve, 'log_action'):
            r = self.client.get('/api/podcast-transcript',
                                params={'url': 'https://example.com/feed.xml'})
        self.assertEqual(404, r.status_code)
        self.assertIn('recipe 2', r.json()['error'])
        self.assertIn('faster-whisper', r.json()['error'])

    def test_unmatched_episode_names_the_feed_size(self):
        with mock.patch.object(serve, '_podcast_fetch', side_effect=_stub_fetch(
                {'example.com/feed.xml': (_FEED, 'application/rss+xml')})), \
                mock.patch.object(serve, 'log_action'):
            r = self.client.get('/api/podcast-transcript',
                                params={'url': 'https://example.com/feed.xml',
                                        'episode': 'missing'})
        self.assertEqual(404, r.status_code)
        self.assertIn('2 episodes', r.json()['error'])


if __name__ == '__main__':
    unittest.main()
