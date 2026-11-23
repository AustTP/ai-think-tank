# Free-source search fan-out (operator, 2026-362-note 2026-11-23): the tank
# rations external page requests (1000/month), and Tavily is a paid lane
# beyond that. arXiv/HN/Wikipedia/GitHub are free, keyless, reliable APIs
# that answer most technical queries; this module lands a free-first search
# lane that answers from them and falls back to Tavily only when they
# return nothing.
import json
import re
import time
import urllib.parse
import urllib.request

_UA = 'Mozilla/5.0 (X11; Linux x86_64) think-tank/1.0 (research agent; austtp25@gmail.com)'
_TIMEOUT = 12
# Per-source result ceiling: agents need breadth, not depth, from a fan-out
# (each source contributes at most N lines to the merged answer).
_PER_SOURCE = 4
_TOTAL_BUDGET_S = 25
_CALLER = 'free-source search'


try:
    import serve
except ImportError:
    serve = None


def _fetch(url, timeout=_TIMEOUT, headers=None):
    """One plain GET with the tank UA. Returns (status:int, body:str);
    (0, error-text) on any failure. Never raises."""
    req = urllib.request.Request(url, headers=headers or {'User-Agent': _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(400000).decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        return 0, f'http {e.code}'
    except Exception as e:
        return 0, str(e)[:200]


def _hn(query, cap=_PER_SOURCE):
    """Hacker News (Algolia): tech discussion, fresh stories. API is free,
    keyless, no page-request budget charged."""
    status, body = _fetch(
        'https://hn.algolia.com/api/v1/search?'
        + urllib.parse.urlencode({'query': query, 'hitsPerPage': cap}))
    if not status:
        return []
    try:
        hits = (json.loads(body).get('hits') or [])
    except Exception:
        return []
    out = []
    for h in hits[:cap]:
        title = h.get('title') or (h.get('story_title') or '(untitled)')
        url = h.get('url') or f'https://news.ycombinator.com/item?id={h.get("objectID")}'
        out.append({'title': title, 'url': url,
                    'snippet': (h.get('story_text') or h.get('comment_text') or '')[:280],
                    'source': 'hn'})
    return out


def _wiki(query, cap=_PER_SOURCE):
    """Wikipedia opensearch: canonical topic lookups. Keyless; polite UA."""
    status, body = _fetch(
        'https://en.wikipedia.org/w/api.php?'
        + urllib.parse.urlencode({'action': 'opensearch', 'search': query,
                                  'limit': cap, 'format': 'json'}))
    if not status:
        return []
    try:
        data = json.loads(body)
        names, urls = data[1], data[3]
    except Exception:
        return []
    out = []
    for name, url in list(zip(names, urls))[:cap]:
        out.append({'title': name, 'url': url, 'snippet': 'wikipedia article',
                    'source': 'wiki'})
    return out


def _github(query, cap=_PER_SOURCE):
    """GitHub repo search: find code/library candidates. Unauth search API
    (10 req/min ceiling, plenty for a fan-out)."""
    status, body = _fetch(
        'https://api.github.com/search/repositories?'
        + urllib.parse.urlencode({'q': query, 'per_page': cap}),
        headers={'User-Agent': _UA, 'Accept': 'application/vnd.github+json'})
    if not status:
        return []
    try:
        items = (json.loads(body).get('items') or [])
    except Exception:
        return []
    out = []
    for r in items[:cap]:
        out.append({'title': r.get('full_name') or '(repo)',
                    'url': r.get('html_url') or '',
                    'snippet': (r.get('description') or '')[:280],
                    'source': 'github'})
    return out


def _arxiv(query, cap=_PER_SOURCE):
    """arXiv atom API: papers. Free keyless; host is slow -- 15s timeout,
    and XML extracted with a tolerant regex (namespace variations)."""
    status, body = _fetch(
        'http://export.arxiv.org/api/query?'
        + urllib.parse.urlencode({'search_query': f'all:{query}',
                                  'max_results': cap}),
        timeout=16)
    if not status:
        return []
    entries = re.findall(r'<entry>(.*?)</entry>', body, re.S)
    out = []
    for entry in entries[:cap]:
        title = re.search(r'<title>\s*(.*?)\s*</title>', entry, re.S)
        link = re.search(r'<id>\s*(http[^<\s]+)', entry)
        summary = re.search(r'<summary>\s*(.*?)\s*</summary>', entry, re.S)
        if not (title and link):
            continue
        out.append({'title': re.sub(r'\s+', ' ', title.group(1))[:160],
                    'url': link.group(1),
                    'snippet': re.sub(r'\s+', ' ', summary.group(1))[:280]
                               if summary else '',
                    'source': 'arxiv'})
    return out


def _merged_search(agent_id, query, per_source=_PER_SOURCE):
    """Fan the query to all four free sources in sequence (HN, wiki,
    GitHub, arXiv), merge, log, return the Tavily-shaped text block:
    lines of `- title (url): snippet`. Empty list => the caller falls
    back to the paid lane."""
    results, notes = [], []
    for fn, label in ((_hn, 'hn'), (_wiki, 'wiki'),
                      (_github, 'github'), (_arxiv, 'arxiv')):
        try:
            got = fn(query)
        except Exception as e:
            got = []
            notes.append(f'{label}: {str(e)[:80]}')
        if got:
            results.extend(got)
        else:
            notes.append(f'{label}: none')
    merged = results[:12]
    if not merged:
        if serve is not None:
            serve.log_action(agent_id, 'free_source_search',
                             {'query': query, 'hit': 0,
                              'notes': notes[:4]},
                             authorized=True)
        return None
    text = '\n'.join(
        f"- {r['title']} ({r['url']}): {r['snippet']}"
        for r in merged)
    if serve is not None:
        serve.log_action(agent_id, 'free_source_search',
                         {'query': query, 'hit': 1, 'counts': {
                             s: sum(1 for r in merged if r['source'] == s)
                             for s in ('hn', 'wiki', 'github', 'arxiv')}},
                         authorized=True)
    return text
