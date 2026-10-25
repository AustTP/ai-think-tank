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
