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
