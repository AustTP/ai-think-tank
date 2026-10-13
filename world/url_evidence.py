"""Track discovered URLs without allowing models to invent citation paths."""
import json
import re
from urllib.parse import urlsplit


class DiscoveredURLs:
    def __init__(self, initial=''):
        self.urls = set()
        self.observe(initial)

    def observe(self, value):
        text = value if isinstance(value, str) else json.dumps(value)
        for url in re.findall(r'https?://[^\s<>"\x60]+', text):
            url = url.rstrip(').,;\'')
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
