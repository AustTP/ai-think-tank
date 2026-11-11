"""Track discovered URLs without allowing models to invent citation paths."""
import re
from urllib.parse import urlsplit


class DiscoveredURLs:
    def __init__(self, initial=''):
        self.urls = set()
        self.observe(initial)

    def observe(self, value):
        if isinstance(value, dict):
            for item in value.values():
                self.observe(item)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                self.observe(item)
            return
        if not isinstance(value, str):
            return
        text = value
        for url in re.findall(r'https?://[^\s<>"\x60]+', text):
            # Search output renders "Title (URL): snippet". The punctuation
            # belongs to the citation, not the URL. Preserve balanced path
            # parentheses, e.g. an article named Function_(mathematics).
            if url.endswith('):'):
                url = url[:-1]
            url = url.rstrip('.,;\'')
            while url.endswith(')') and url.count(')') > url.count('('):
                url = url[:-1]
            if self.valid(url):
                self.urls.add(url.rstrip('/'))

    @staticmethod
    def valid(url):
        try:
            p = urlsplit(url)
            return p.scheme in ('http','https') and bool(p.hostname) and not p.username and not p.password and not any(c in url for c in ('[REDACTED]', ' ', '\n', '\r'))
        except ValueError:
            return False

    def allows(self, url):
        return self.valid(url) and url.rstrip('/') in self.urls


def extract_urls(text):
    """Every syntactically valid http(s) URL in a report, normalized."""
    return sorted(DiscoveredURLs(text).urls)


# First-party origins whose subpages an agent may browse without prior
# discovery (owner direction 2026-11-11: the tank must always be able to
# reach its own repo and its own control plane). The discovery ledger exists
# to stop a model from inventing THIRD-PARTY citation URLs; a URL inside the
# tank's own GitHub repo or the tank's own server is first-party evidence of
# the tank's own work -- worst case a 404 on itself, never a fabricated
# external source. The GitHub slug matches the repo the change-request
# pipeline manages (AustTP/ai-think-tank).
TANK_GITHUB_REPO = 'AustTP/ai-think-tank'
TANK_GITHUB_ORIGIN = 'https://github.com'
TANK_GITHUB_PATH = '/' + TANK_GITHUB_REPO


def first_party_url(url, own_origins=()):
    """True when `url` points into the tank's own control plane or its own
    GitHub repository (any subpath -- Actions runs, PRs, CI logs). Syntax
    rules still apply: a malformed URL is never first-party. `own_origins`
    are the tank's server roots (scheme://host[:port]) supplied by the
    caller -- url_evidence is core and never imports serve."""
    if not DiscoveredURLs.valid(url):
        return False
    p = urlsplit(url)
    # The tank's own GitHub repo: github.com/<owner>/<repo>[/<anything>].
    if (p.hostname or '').lower() == 'github.com':
        first_two = (p.path or '').split('/', 3)
        return len(first_two) >= 3 and \
            '/'.join(first_two[1:3]).lower() == TANK_GITHUB_REPO.lower()
    # The tank's own server origins: any page the tank itself serves. The
    # control plane hosts the change-request links, the mission-control CI
    # view, and the player inbox -- all first-party.
    origin = f'{p.scheme}://{p.netloc.lower()}'
    return origin in {str(o).rstrip('/').lower() for o in (own_origins or [])}

def citation_errors(text, allowed):
    """Flag report citations that were never discovered by the tools.

    A model may summarize freely; it may not mint a URL path. `allowed` is the
    task's real tool-discovery ledger. An empty ledger is not proof that any
    citation is valid, so callers should pass the task's recorded URLs.
    """
    allowed_urls = {u.rstrip('/') for u in allowed or []
                    if isinstance(u, str) and DiscoveredURLs.valid(u)}
    errors = []
    for url in extract_urls(text):
        if url not in allowed_urls:
            errors.append('Cited URL was not discovered by a tool: ' + url)
    return errors[:20]
