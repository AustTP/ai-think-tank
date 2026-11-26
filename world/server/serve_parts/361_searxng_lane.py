# SearXNG lane (operator, 2026-11-26): a local/self-hosted SearXNG instance
# is the FIRST search the tank tries -- free, keyless, unlimited, and off
# the page-request ledger, ahead of the 362 arXiv/HN/Wikipedia/GitHub
# fan-out and the paid Tavily lane. The instance runs where the tank runs
# (docker searxng/searxng with JSON output enabled); SEARXNG_URL points at
# it and an empty value (or a dead instance) skips the lane silently, so
# every downstream fallback keeps working unchanged. Query-only, fixed
# host from env, no user-supplied URLs -- SSRF discipline is trivial here.
import json
import urllib.parse
import urllib.request

_UA = 'Mozilla/5.0 (X11; Linux x86_64) think-tank/1.0 (research agent; austtp25@gmail.com)'
_TIMEOUT_S = 8
_MAX_RESULTS = 8

try:
    import serve
except ImportError:
    serve = None


def _searxng_url():
    """The configured SearXNG base URL (SEARXNG_URL), defaulting to the
    standard local docker port. Empty string disables the lane."""
    env = serve._load_env() if serve else {}
    return (env.get('SEARXNG_URL')
            or 'http://127.0.0.1:8888').strip().rstrip('/')


def _fetch(url, timeout=_TIMEOUT_S):
    """One plain GET with the tank UA. Returns (status:int, body:str);
    (0, error-text) on any failure. Never raises. Injectable for tests,
    same as the 362 module's _fetch."""
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers={'User-Agent': _UA}),
                timeout=timeout) as resp:
            return resp.status, resp.read(200000).decode('utf-8',
                                                         errors='replace')
    except urllib.error.HTTPError as e:
        return 0, f'http {e.code}'
    except Exception as e:
        return 0, str(e)[:200]


def _searxng_search(agent_id, query, max_results=_MAX_RESULTS):
    """Query the local SearXNG (format=json) and return the Tavily-shaped
    text block: lines of `- title (url): snippet`. None when the lane is
    disabled/unreachable/empty so the caller falls through unchanged.
    Never accrues anything -- a self-hosted instance costs nothing."""
    if not (query or '').strip() or serve is None:
        return None
    url = _searxng_url()
    if not url:
        return None
    status, body = _fetch(url + '/search?' + urllib.parse.urlencode(
        {'q': query, 'format': 'json', 'language': 'en', 'safesearch': 1}))
    if not status:
        return None  # dead instance -> silent fall-through
    try:
        data = json.loads(body)
    except Exception:
        return None
    out = []
    for r in (data.get('results') or []):
        link = r.get('url') or ''
        if not link or link.startswith('data:'):
            continue
        out.append({'title': (r.get('title') or '(untitled)')[:160],
                    'url': link,
                    'snippet': (r.get('content') or '')[:280],
                    'source': 'searxng'})
        if len(out) >= max_results:
            break
    if not out:
        try:
            serve.log_action(agent_id, 'searxng_search',
                             {'query': query, 'hit': 0}, authorized=True)
        except Exception:
            pass
        return None
    text = '\n'.join(f"- {r['title']} ({r['url']}): {r['snippet']}"
                     for r in out)
    try:
        serve.log_action(agent_id, 'searxng_search',
                         {'query': query, 'hit': 1, 'count': len(out)},
                         authorized=True)
    except Exception:
        pass
    return text