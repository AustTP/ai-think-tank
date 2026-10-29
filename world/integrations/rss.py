"""Curated RSS/ATOM feed aggregator.

Extracted from serve.py. Same architecture as the AISStream collector, applied
to feeds agents keep asking about (current events, space, hazards, research): a
standing poll loop fetches each feed on its own cadence, a tolerant parser
handles both RSS 2.0 (<item>) and ATOM (<entry>) XML, and the most recent items
per feed live in an in-process ring buffer served by the read_rss_feed tool /
/api/rss/recent. Items are tagged at ingest with any watchlist research topics
whose words appear in the title, so the Research Desk's standing topics surface
straight out of the feed read.

The feed LIST is operator-managed at runtime: the rss_feeds DB table is the
registry, seeded from FEED_SEED on first boot, and /api/rss/{propose, approve,
remove} let directors/admins add or drop feeds without a restart (the poll loop
re-reads the live registry every cycle).

Everything that tests or other modules patch on the serve module is reached
through `_serve.X` at call time (the same lazy pattern auth.py documents), so:
  - a test patching `serve._RSS_FEEDS` / `serve._rss_ingest` / `serve._db` is
    honored by the functions below, and
  - serve.py re-imports these names unchanged, so no call site moved.
"""

import json
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

# The seed now lives in rss_feeds.json (same shape as api_services.json):
# config-not-code for a fresh install; runtime edits stay in the DB registry.
RSS_FEEDS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'rss_feeds.json')
with open(RSS_FEEDS_PATH, encoding='utf-8') as _rss_seed_file:
    _rss_seed_data = json.load(_rss_seed_file)
FEED_SEED = {sid: dict(spec) for sid, spec in (_rss_seed_data.get('feeds') or {}).items()}

# Public aliases used by serve.py's routes and lifespan wiring. The underscore
# names below are the originals the tests and the rest of serve.py already use.
_RSS_FEED_SEED = FEED_SEED
_RSS_FEEDS: dict = {}  # live registry: feed_id -> {'name', 'url', 'interval_s'} (DB-backed)
_RSS_FEEDS_LOAD_ERROR = None
_RSS_BUFFER_MAX_PER_FEED = 50
_RSS_WINDOW_S = 3 * 86400
_RSS_FETCH_TIMEOUT_S = 20
_RSS_HTTP_HEADERS = {'User-Agent': 'AIThinkTankAgent/1.0 (think-tank current-events aggregator)'}

_RSS_BUFFER: dict = {}  # feed_id -> list of {'ts', 'title', 'link', 'summary', 'matchedTopics'}
_RSS_BUFFER_LOCK = threading.Lock()
# feed_id -> {link: True}, FIFO-trimmed at 1000: the ring buffer forgets links
# past _RSS_BUFFER_MAX_PER_FEED, so without this a feed returning more items
# than the buffer re-ingests its evicted links every cycle forever.
_RSS_SEEN_LINKS: dict = {}
_RSS_STATUS: dict = {}  # feed_id -> {'last_fetch_at', 'last_error', 'ok'}

# RSS and Atom feeds are untrusted input. Parse them only with defusedxml,
# which rejects entity-expansion and external-entity attacks. If the package is
# absent, skip XML parsing rather than falling back to the stdlib parser.
try:  # pragma: no cover - the installed package is pinned in requirements.txt
    from defusedxml.ElementTree import fromstring as _xml_fromstring
    from defusedxml.ElementTree import ParseError as _XMLParseError
except ImportError:  # pragma: no cover - optional hardening dependency
    # No stdlib parsing fallback: an installation without defusedxml simply
    # does not parse untrusted RSS/Atom XML.
    _xml_fromstring = None
    from xml.etree.ElementTree import ParseError as _XMLParseError


def _parse_rss_atom_xml(raw, feed_id):  # noqa: ARG001 -- feed_id kept for signature symmetry
    """Parse RSS 2.0 or ATOM XML into a list of {title, link, summary} items.
    Namespace-tolerant (strips '{...}' from tags), handles <item>/<entry> even
    when nested inside a <channel> container, and degrades gracefully on feeds
    that reorder fields or omit summaries."""
    import html as _html
    import re as _re
    items = []
    if _xml_fromstring is None:  # pragma: no cover - defusedxml is pinned
        return items
    try:
        root = _xml_fromstring(raw)
    except _XMLParseError:
        return items
    entries = []
    for node in root.iter():
        tag = _re.sub(r'\{.*?\}', '', node.tag).lower()
        if tag in ('item', 'entry'):
            entries.append(node)
    for entry in entries:
        fields = {}
        for child in entry:
            tag = _re.sub(r'\{.*?\}', '', child.tag).lower()
            if tag in ('title', 'link', 'guid', 'id', 'description', 'summary', 'content'):
                text = (child.text or '').strip()
                if tag == 'link' and not text:
                    text = child.get('href') or ''
                if tag in ('description', 'summary', 'content'):
                    text = _re.sub(r'<[^>]+>', ' ', text)
                    text = _html.unescape(text)
                    text = _re.sub(r'\s+', ' ', text).strip()
                if tag in ('title', 'link', 'guid', 'id'):
                    fields.setdefault(tag, text)
                elif tag in ('description', 'summary', 'content') and not fields.get('summary'):
                    fields['summary'] = text[:500]
        title = fields.get('title') or ''
        link = fields.get('link') or fields.get('guid') or fields.get('id') or ''
        if title or link:
            items.append({'title': title, 'link': link,
                          'summary': fields.get('summary') or ''})
    return items


def _rss_match_topics(title, topics=None):
    """Tag a title with any current watchlist research topics it mentions
    (word-boundary match on topic words, not the full topic string).

    `topics` is an OPTIONAL preloaded list of topic strings: `_rss_ingest`
    loads the state ONCE per batch and shares it across every item, because
    get_state_from_db re-reads the whole (very large) state blob -- and
    deep-copies the entire task archive -- on EVERY call. Per-item loads
    inside one ingest were the event-loop spin that took the live server
    dark under an item-heavy feed cycle."""
    import serve as _serve
    if topics is None:
        try:
            # Alias merge: this reads one key and mutates nothing, so it
            # must not pay the whole-archive deepcopy per call.
            state = _serve.get_state_from_db(archive_merge='alias')
        except Exception:  # noqa: BLE001 -- never let topic tagging break the poller
            state = None
        topics = [(t.get('topic') or '') for t in ((state or {}).get('researchTopics') or [])]
    title_l = f' {title.lower()} '
    matched = []
    for topic in topics:
        if not topic:
            continue
        words = [w for w in topic.lower().split() if len(w) > 2]
        if words and all(f' {w} ' in title_l for w in words):
            matched.append(topic)
    return matched


def _rss_ingest(feed_id, items):
    import serve as _serve
    now = int(time.time() * 1000)
    # Load the watchlist topics ONCE for the whole batch: a per-item
    # get_state_from_db re-reads the entire state blob (and deep-copies the
    # whole task archive) for every headline, which under an item-heavy feed
    # was enough CPU on the event loop to starve every other coroutine.
    # Alias merge: single-key read, mutates nothing.
    try:
        _state = _serve.get_state_from_db(archive_merge='alias')
    except Exception:  # noqa: BLE001 -- never let topic tagging break the poller
        _state = None
    topics = [(t.get('topic') or '') for t in ((_state or {}).get('researchTopics') or [])]
    with _serve._RSS_BUFFER_LOCK:
        # `seen` must survive buffer eviction: the ring buffer keeps only the
        # last _RSS_BUFFER_MAX_PER_FEED links, so a feed that returns more
        # items than that re-presented its evicted links as "fresh" on every
        # cycle -- a permanent re-ingest loop that multiplied the cost above.
        # A bounded per-feed seen-set (FIFO-trimmed well past the buffer size)
        # remembers links across cycles without growing unbounded.
        seen_links = _RSS_SEEN_LINKS.setdefault(feed_id, {})
        seen = {it.get('link') for it in _serve._RSS_BUFFER.get(feed_id, [])}
        seen.update(seen_links)
        fresh = []
        for it in items:
            link = it.get('link') or ''
            if link and link in seen:
                continue
            seen.add(link)
            seen_links[link] = True
            if len(seen_links) > 1000:
                for stale in list(seen_links)[:len(seen_links) - 1000]:
                    seen_links.pop(stale, None)
            fresh.append({'ts': now, 'title': it.get('title') or '',
                          'link': link, 'summary': it.get('summary') or '',
                          'matchedTopics': _serve._rss_match_topics(it.get('title') or '', topics)})
        buf = _serve._RSS_BUFFER.setdefault(feed_id, [])
        buf.extend(fresh)
        if len(buf) > _serve._RSS_BUFFER_MAX_PER_FEED:
            del buf[:len(buf) - _serve._RSS_BUFFER_MAX_PER_FEED]
    try:
        from agents.pulse import observe as _pulse_observe
        for it in fresh:
            _pulse_observe(f'rss:{feed_id}', it.get('title') or '', it,
                           dedupe_key=it.get('link') or str(it))
    except Exception:
        pass
    return len(fresh)


def _rss_recent(feed_id, max_items=10):
    import serve as _serve
    cutoff = int(time.time() * 1000) - _serve._RSS_WINDOW_S * 1000
    with _serve._RSS_BUFFER_LOCK:
        items = [dict(it) for it in _serve._RSS_BUFFER.get(feed_id, []) if it['ts'] >= cutoff]
    return items[-max_items:]


def _rss_feed_spec_to_row(spec, status='active', proposed_by=None):
    now = time.time()
    return {
        'id': spec.get('id'),
        'name': spec.get('name') or spec.get('id'),
        'url': spec.get('url') or '',
        'interval_s': int(spec.get('interval_s') or 600),
        'status': status,
        'proposed_by': proposed_by,
        'created_at': now,
        'updated_at': now,
    }


def _rss_feed_row_to_spec(row):
    return {'id': row['id'], 'name': row['name'], 'url': row['url'],
            'interval_s': int(row['interval_s'] or 600), 'status': row['status'],
            'proposed_by': row.get('proposed_by')}


def _load_rss_feeds():
    """Load the active rss_feeds rows into the live registry. Fail closed like
    _load_api_services: on any DB error the registry stays empty (the poll loop
    then has nothing to fetch, never a crash)."""
    import serve as _serve
    _serve._RSS_FEEDS = {}
    _serve._RSS_FEEDS_LOAD_ERROR = None
    try:
        with _serve._db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, name, url, interval_s, status, proposed_by FROM rss_feeds WHERE status = 'active'"  # nosec B608 -- fixed internal columns
            ).fetchall()
    except Exception as e:
        _serve._RSS_FEEDS_LOAD_ERROR = f'db unavailable: {e}'
        return
    feeds = {}
    for row in rows:
        spec = _serve._rss_feed_row_to_spec(dict(row))
        feeds[spec['id']] = spec
    _serve._RSS_FEEDS = feeds


def _seed_rss_feeds_if_empty():
    """Seed the rss_feeds table from the curated seeds the first time it comes
    up empty, mirroring _seed_api_services_if_empty. New DBs get the five
    curated feeds for free; an existing registry is left untouched."""
    import serve as _serve
    try:
        with _serve._db() as conn:
            count = conn.execute('SELECT COUNT(*) FROM rss_feeds').fetchone()[0]
            if count:
                return
            for sid, feed in _serve._RSS_FEED_SEED.items():
                spec = {'id': sid, 'name': feed.get('name'), 'url': feed.get('url'),
                        'interval_s': feed.get('interval_s') or 600}
                row = _serve._rss_feed_spec_to_row(spec, status='active', proposed_by='seed')
                conn.execute(
                    'INSERT INTO rss_feeds (id, name, url, interval_s, status, proposed_by, created_at, updated_at) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    [row[c] for c in ('id', 'name', 'url', 'interval_s', 'status', 'proposed_by', 'created_at', 'updated_at')])
    except Exception as e:
        print(f'[rss] seed failed: {e}', flush=True)


def _rss_feed_upsert(spec, status='active', proposed_by=None):
    """Persist a feed spec as a row and refresh the live registry. Used by the
    propose flow (status='proposed'), the approval flow (status='active' or
    'denied'), and the remove flow (status='removed')."""
    import serve as _serve
    row = _serve._rss_feed_spec_to_row(spec, status=status, proposed_by=proposed_by)
    with _serve._db() as conn:
        conn.execute(
            'INSERT INTO rss_feeds (id, name, url, interval_s, status, proposed_by, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(id) DO UPDATE SET name=excluded.name, url=excluded.url, '
            'interval_s=excluded.interval_s, status=excluded.status, '
            'proposed_by=excluded.proposed_by, updated_at=excluded.updated_at',
            [row[c] for c in ('id', 'name', 'url', 'interval_s', 'status', 'proposed_by', 'created_at', 'updated_at')])
    _serve._load_rss_feeds()


def _validate_rss_feed_spec(spec):
    """Validate a proposed feed spec. Returns (feed_id, normalized, error) --
    error is None when valid. Security constraints mirror _validate_api_service_spec:
    a public https URL only (the poll loop fetches it, so an internal address
    would be an SSRF hole), a simple alphanumeric slug id, and a sane interval."""
    import serve as _serve
    spec = spec or {}
    fid = str(spec.get('id') or '').strip().lower()
    if not fid or not fid.replace('_', '').replace('-', '').isalnum():
        return None, None, 'feed id must be a simple alphanumeric slug'
    url = str(spec.get('url') or '').strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or not _serve._is_safe_public_host(parsed.hostname):
        return None, None, 'url must be a public https URL'
    interval_s = max(60, min(int(spec.get('interval_s') or 600), 86400))
    name = str(spec.get('name') or '').strip() or fid
    return fid, {'id': fid, 'name': name[:120], 'url': url, 'interval_s': interval_s}, None


def _probe_rss_url(url):
    """Fetch + parse a feed URL ONCE, at propose time, so a garbage or dead URL
    is caught before it is ever approved. Returns {'ok', 'itemCount',
    'sampleTitle', 'error'} -- never raises."""
    import serve as _serve
    try:
        items = _serve._fetch_feed_sync('_probe', url)
        return {'ok': True, 'itemCount': len(items),
                'sampleTitle': (items[0].get('title') if items else '')[:120], 'error': ''}
    except Exception as e:  # noqa: BLE001 -- a failed probe is a result, not a crash
        return {'ok': False, 'itemCount': 0, 'sampleTitle': '', 'error': str(e)[:200]}


def _fetch_feed_sync(feed_id, url):
    """Synchronous feed fetch (poll loop runs in a thread). Returns the parsed
    items or raises -- the loop owns retry/backoff and status tracking."""
    import serve as _serve
    req = urllib.request.Request(url, headers=_serve._RSS_HTTP_HEADERS)
    with _serve._safe_urlopen(req, timeout=_serve._RSS_FETCH_TIMEOUT_S) as resp:
        raw = resp.read(2 ** 20 + 1)
    return _serve._parse_rss_atom_xml(raw, feed_id)


async def _rss_poll_loop():
    """Standing loop (created in serve._lifespan): fetch each curated feed whose
    interval has elapsed, ingest new items into the ring buffer, and surface
    per-feed health in the status map. Polls are serialized through a thread so
    one slow feed can't stall the rest of the event loop. The feed list is
    re-read each cycle (a snapshot of the live registry), so a director/admin
    adding or removing a feed via /api/rss/* goes live with no restart -- and
    the sleep is capped at 30s so a newly-approved feed is fetched promptly
    instead of waiting for the next scheduled pass."""
    import asyncio
    import serve as _serve
    while True:
        feeds = dict(_serve._RSS_FEEDS)
        now = time.time()
        for feed_id, feed in feeds.items():
            last = _serve._RSS_STATUS.get(feed_id, {}).get('last_fetch_at', 0)
            if now - last < (feed.get('interval_s') or 600):
                continue
            url = feed.get('url') or ''
            try:
                items = await asyncio.to_thread(_serve._fetch_feed_sync, feed_id, url)
                n = _serve._rss_ingest(feed_id, items)
                _serve._RSS_STATUS[feed_id] = {'ok': True, 'last_fetch_at': time.time(),
                                               'last_error': '', 'new': n}
            except Exception as e:  # noqa: BLE001 -- a bad feed/network hiccup must never kill the loop
                _serve._RSS_STATUS[feed_id] = {'ok': False, 'last_fetch_at': time.time(),
                                               'last_error': str(e)[:200], 'new': 0}
                print(f'[rss] {feed_id} fetch failed: {e}', flush=True)
            await asyncio.sleep(0.02)
        # Sleep until the soonest next poll across all feeds, capped so a
        # newly-added feed is picked up within ~30s.
        now = time.time()
        due = min((now + (feed.get('interval_s') or 600) - _serve._RSS_STATUS.get(feed_id, {}).get('last_fetch_at', 0)
                   for feed_id, feed in feeds.items()), default=600)
        await asyncio.sleep(max(1.0, min(due, 30)))
