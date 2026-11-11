# Podcast transcripts for the tank (2026-11-11).
#
# Same house pattern as the X reader and the YouTube transcript path: a THIN
# adapter over open data, no metered API. Most well-produced podcasts publish
# their transcripts inside their own RSS feed via the Podcasting 2.0
# `podcast:transcript` namespace tag -- a plain file (JSON/SRT/VTT/HTML/txt)
# the publisher already hosts. This module:
#
#   - resolves the feed from a feed URL or a show/episode page (the page's
#     <link rel="alternate" type="application/rss+xml">),
#   - matches the requested episode (guid / audio-URL substring / title
#     substring; default = the latest item),
#   - downloads + cleans the transcript file (format-aware), capped,
#   - refuses anything that is not a publicly routable host (the same SSRF
#     guard the browser tooling uses), and
#   - fails HONESTLY when an episode has no published transcript: the agent
#     follows library/skills/podcasts.md recipe 2 (download the enclosure +
#     faster-whisper in their own sandbox via /api/execute) exactly like
#     captionless YouTube videos follow youtube.md recipe 2.
#
# No Apify, no Colab, no transcript vendor. The ASR leg is the agent's own
# sandbox job, owned by whoever files the work -- the tank only reads
# publishers' own data here.
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

PODCAST_TRANSCRIPT_MAX_CHARS = 200000
PODCAST_FETCH_TIMEOUT_S = 30
PODCAST_TRANSCRIPT_MAX_BYTES = 4 * 1024 * 1024
_PODCAST_UA = ('Mozilla/5.0 (compatible; ai-think-tank/1.0; '
               '+https://github.com/AustTP/ai-think-tank)')

_PODCAST_FORMAT_PREF = [
    'application/json', 'text/plain', 'application/srt',
    'application/x-subrip', 'text/vtt', 'text/html',
]


def _podcast_fetch(url, max_bytes=PODCAST_TRANSCRIPT_MAX_BYTES):
    """GET a public URL, capped. Returns (bytes, content_type) or raises
    ValueError with an honest reason. Private/loopback/reserved hosts are
    refused (SSRF guard, integrations/web_helpers._is_safe_public_host)."""
    from urllib.parse import urlsplit
    p = urlsplit(url or '')
    if p.scheme not in ('http', 'https') or not p.hostname:
        raise ValueError(f'not a usable http(s) URL: {url!r}')
    if p.username or p.password:
        raise ValueError('URLs with embedded credentials are refused')
    import integrations.web_helpers as _wh
    if not _wh._is_safe_public_host(p.hostname):
        raise ValueError(f'host is not publicly routable: {p.hostname}')
    req = urllib.request.Request(url, headers={'User-Agent': _PODCAST_UA})
    try:
        with urllib.request.urlopen(req, timeout=PODCAST_FETCH_TIMEOUT_S) as r:
            return r.read(max_bytes + 1), (r.headers.get('Content-Type') or '')
    except urllib.error.HTTPError as e:
        raise ValueError(f'HTTP {e.code} fetching {url}') from e
    except Exception as e:
        raise ValueError(f'{type(e).__name__} fetching {url}: {e}') from e


def _podcast_looks_like_feed(url, body=b'', content_type=''):
    """Cheap feed sniff: URL shape first, then the body itself."""
    from urllib.parse import urlsplit
    path = (urlsplit(url or '').path or '').lower()
    if path.endswith(('.xml', '.rss')) or '/rss' in path or '/feed' in path:
        return True
    ctype = (content_type or '').lower()
    if 'xml' in ctype:
        return True
    head = (body or b'')[:512].lstrip().lower()
    return head.startswith(b'<?xml') or b'<rss' in head or b'<feed' in head


def _podcast_find_feed_from_page(url):
    """Pull the podcast feed URL out of an HTML page: the standard
    <link rel="alternate" type="application/rss+xml"> declaration, resolved
    against the page URL. Returns the feed URL or None."""
    try:
        body, _ctype = _podcast_fetch(url)
    except ValueError:
        return None
    text = body.decode('utf-8', 'replace')
    for tag in re.findall(r'<link\b[^>]*>', text, re.I):
        low = tag.lower()
        if 'rel="alternate"' not in low and "rel='alternate'" not in low:
            continue
        if 'application/rss+xml' not in low:
            continue
        m = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
        if m and m.group(1):
            from urllib.parse import urljoin
            return urljoin(url, m.group(1))
    return None


def _localname(tag):
    return (tag or '').rsplit('}', 1)[-1].lower()


def _podcast_parse_feed(xml_bytes):
    """Parse a podcast RSS feed into item dicts. Namespace-agnostic on
    purpose: every element is matched by LOCAL name, so podcast:transcript
    tags under either the podcastindex or podcasting2 namespace and plain
    RSS/Atom shapes all work. Returns a list of items (feed order = newest
    first by RSS convention):
      {title, guid, audio, transcripts: [{url, type, rel, language}]}
    Raises ValueError on unparseable XML."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise ValueError(f'feed XML did not parse: {e}') from e
    items = []
    for element in root.iter():
        if _localname(element.tag) != 'item':
            continue
        item = {'title': '', 'guid': '', 'audio': '', 'transcripts': []}
        for child in element.iter():
            name = _localname(child.tag)
            if name == 'title' and not item['title']:
                item['title'] = (child.text or '').strip()
            elif name == 'guid' and not item['guid']:
                item['guid'] = (child.text or '').strip()
            elif name == 'enclosure' and not item['audio']:
                item['audio'] = (child.get('url') or '').strip()
            elif name == 'transcript':
                turl = (child.get('url') or '').strip()
                if not turl:
                    continue
                item['transcripts'].append({
                    'url': turl,
                    'type': (child.get('type') or '').strip().lower(),
                    'rel': (child.get('rel') or '').strip().lower(),
                    'language': (child.get('language') or '').strip().lower(),
                })
        items.append(item)
    return items


def _podcast_pick_transcript(tags):
    """Best transcript file for reading: published spoken-transcript
    (rel != shownotes), preferred format order, then whatever is left."""
    spoken = [t for t in tags if t.get('rel') != 'shownotes']
    for pref in _PODCAST_FORMAT_PREF:
        for t in spoken:
            if t.get('type') == pref:
                return t
    for pref in _PODCAST_FORMAT_PREF:
        for t in tags:
            if t.get('type') == pref:
                return t
    return spoken[0] if spoken else (tags[0] if tags else None)


def _podcast_select_item(items, episode):
    """Choose the episode: guid exact match first, then audio-URL substring,
    then title substring (all case-insensitive). None/'' = the latest item.
    Returns the item or None."""
    if not items:
        return None
    q = (episode or '').strip().lower()
    if not q:
        return items[0]
    for it in items:
        if q and it.get('guid', '').lower() == q:
            return it
    for it in items:
        if q and q in (it.get('audio') or '').lower():
            return it
    for it in items:
        if q and q in (it.get('title') or '').lower():
            return it
    return None


def _podcast_clean_transcript(body, ctype, url=''):
    """Format-aware transcript cleanup, capped. Returns text or None."""
    ctype = (ctype or '').split(';')[0].strip().lower()
    raw = body.decode('utf-8', 'replace')
    text = None
    if ctype == 'application/json' or raw.lstrip()[:1] in ('{', '['):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            segs = data.get('segments')
            if isinstance(segs, list):
                text = ' '.join(str(s.get('text') or '').strip()
                                for s in segs if isinstance(s, dict))
            elif isinstance(data.get('text'), str):
                text = data['text']
        elif isinstance(data, list):
            text = ' '.join(str(s.get('text') or '').strip()
                            for s in data if isinstance(s, dict))
    elif ctype in ('application/srt', 'application/x-subrip', 'text/vtt') or \
            url.lower().endswith(('.srt', '.vtt')):
        with tempfile.NamedTemporaryFile('w', suffix='.vtt',
                                         delete=False, encoding='utf-8') as fh:
            fh.write(raw)
            path = fh.name
        try:
            import serve as _serve
            text = _serve._clean_subtitle_file(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    elif ctype == 'text/html':
        import integrations.web_helpers as _wh
        text = _wh._strip_html_to_text(raw)
    else:
        text = raw
    if not text:
        return None
    text = ' '.join(text.split())
    return text[:PODCAST_TRANSCRIPT_MAX_CHARS] or None


def _podcast_transcript(url, episode=''):
    """Transcript of one podcast episode from the publisher's own feed.

    Returns ({text, feed, episode, transcript_url, format}, None) on success
    or (None, error-string) on failure. Free -- publisher-hosted open data,
    nothing metered."""
    url = (url or '').strip()
    if not url:
        return None, 'a feed or episode URL is required'
    body, ctype = _podcast_fetch(url)
    feed_url = url
    if not _podcast_looks_like_feed(url, body, ctype):
        feed_url = _podcast_find_feed_from_page(url)
        if not feed_url:
            return None, ('could not find a podcast RSS feed from that page '
                          '(no <link rel="alternate" type="application/rss+xml">) '
                          '-- pass the feed URL directly')
        body, ctype = _podcast_fetch(feed_url)
    try:
        items = _podcast_parse_feed(body)
    except ValueError as e:
        return None, str(e)
    if not items:
        return None, 'the feed parsed but carries no episodes'
    item = _podcast_select_item(items, episode)
    if item is None:
        return None, (f'no episode matched {episode!r} in the feed '
                      f'({len(items)} episodes checked)')
    tag = _podcast_pick_transcript(item['transcripts'])
    if tag is None:
        return None, ('this episode publishes no transcript in its feed -- '
                      'follow library/skills/podcasts.md recipe 2 (download '
                      'the enclosure, faster-whisper in your own sandbox '
                      'via /api/execute)')
    try:
        tbody, tctype = _podcast_fetch(tag['url'])
    except ValueError as e:
        return None, f'transcript file failed: {e}'
    text = _podcast_clean_transcript(tbody, tctype, tag['url'])
    if not text:
        return None, 'the transcript file downloaded but produced no readable text'
    return ({'text': text, 'feed': feed_url,
             'episode': item.get('title') or item.get('guid') or '',
             'transcript_url': tag['url'],
             'format': tag.get('type') or tctype}, None)


@app.get('/api/podcast-transcript')
async def api_podcast_transcript(request: Request):
    agent_id = _agent_id_for_key(request.headers.get('X-Agent-Key')) or 'player'
    url = (request.query_params.get('url') or '').strip()
    episode = (request.query_params.get('episode') or '').strip()
    if not url or len(url) > 2000:
        return JSONResponse({'error': 'url required (<=2000 chars)'}, status_code=400)
    result, err = await asyncio.to_thread(_podcast_transcript, url, episode)
    log_action(agent_id, 'podcast_transcript', {
        'url': url[:500], 'episode': episode[:300],
        'ok': bool(result),
        'note': ('' if result else (err or '')[:200]),
    }, authorized=True)
    if err:
        return JSONResponse({'error': err}, status_code=404)
    return JSONResponse(result)
