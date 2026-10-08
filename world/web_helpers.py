"""Pure web/text/path/security helpers extracted from serve.py.

These are the small, self-contained utilities that serve.py used to define
inline -- HTML stripping, link extraction, HTTP-date parsing, SSRF host
checking, filename sanitization, and a couple of hashing/validation helpers.
Every one of them is PURE: no module state, no DB, no serve imports, only
Python stdlib and their own local constants. Extracting them shrinks the
serve.py monolith and makes the helpers independently testable.

Imported back into serve.py as `from web_helpers import ...`, so no call
site changes were needed.
"""

import hashlib
import html
import http.client
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

import datetime
import email.utils

_TAG_RE = re.compile(r'<(script|style)[^>]*>.*?</\1>', re.IGNORECASE | re.DOTALL)
_ANY_TAG_RE = re.compile(r'<[^>]+>')
_LINK_RE = re.compile(r'<a\s+([^>]*)>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(r'href=["\']([^"\']*)["\']', re.IGNORECASE)


def _strip_html_to_text(raw_html):
    # A plain-text reading view, not a rendering engine -- this feeds an
    # agent's own context (and the in-game fake-browser display), neither
    # of which need or should execute page JS/CSS.
    text = _TAG_RE.sub(' ', raw_html)
    text = _ANY_TAG_RE.sub(' ', text)
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def _extract_links(raw_html, base_url):
    # Stripping tags for the reading view (above) throws away every
    # <a href> along with them -- real gap: an agent has no way
    # to "follow a breadcrumb" to a page it doesn't already know the URL
    # for. Each extracted link still goes through the full /api/browse
    # gate independently when followed (classify, SSRF-check, log) -- this
    # only surfaces what's on the page, it doesn't fetch anything itself.
    seen = set()
    links = []
    for m in _LINK_RE.finditer(raw_html):
        attrs, link_text = m.group(1), m.group(2)
        # Gap: a language-alternate link (standard
        # hreflang attribute, not a Wikipedia-specific pattern) filled an
        # entire lower link-extraction budget on a real test page before
        # any actual article-body link was ever reached. hreflang is
        # specifically the HTML spec's own signal for "this is a
        # language alternate, not primary content" -- skipping it is a
        # general fix, not a one-site hack.
        if 'hreflang=' in attrs.lower():
            continue
        href_m = _HREF_RE.search(attrs)
        if not href_m:
            continue
        href = href_m.group(1)
        if not href or href[0] == '#' or href.lower().startswith(('javascript:', 'mailto:', 'tel:')):
            continue
        abs_url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(abs_url)
        if parsed.scheme not in ('http', 'https') or abs_url in seen:
            continue
        seen.add(abs_url)
        clean_text = _ANY_TAG_RE.sub(' ', link_text)
        clean_text = re.sub(r'\s+', ' ', html.unescape(clean_text)).strip()
        links.append({'text': clean_text[:100] or abs_url, 'url': abs_url})
        # Gap: a lower cap (40) was entirely consumed by
        # a link-dense page's own chrome (Wikipedia's nav sidebar plus its
        # ~300-language switcher) before reaching any actual article
        # content, so the multi-hop "follow toward a goal" helper below
        # never even saw the relevant link as a candidate.
        if len(links) >= 200:
            break
    return links


def _parse_http_date_ms(value):
    # "Date-aware" incremental research needs SOME
    # real signal that a page's content actually changed, not just that
    # its URL was already seen -- Last-Modified is the one plain HTTP
    # already carries, imperfect as it is (plenty of sites never set it,
    # or set it to render time rather than true content-modification
    # time). Failing closed to None on anything unparseable -- a caller
    # treating "we don't know" as "assume changed" is the safe default,
    # not this function guessing.
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _resolve_public_ip(hostname):
    # Resolve a hostname to a single public IP, or None. This is the SSRF
    # resolution primitive: it rejects anything that maps to a private,
    # loopback, link-local, reserved, or multicast address. Returns the first
    # public IPv4/IPv6 address string, or None when the name is absent,
    # unresolvable, or only resolves to non-public addresses.
    if not hostname or hostname.lower() in ('localhost', '0.0.0.0'):  # nosec B104 -- this REJECTS localhost/0.0.0.0; it is the SSRF guard, not a bind
        return None
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return None
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            continue
        return addr
    return None


def _is_safe_public_host(hostname):
    # SSRF protection -- this is security hygiene, not a content policy:
    # regardless of what category gate is chosen, this backend must never
    # let a request reach this machine's own network. Resolves the
    # hostname and rejects anything private/loopback/link-local/reserved,
    # in addition to the obvious localhost names. Fails CLOSED on a
    # mixed-resolution host: if ANY resolved address is unsafe, the whole
    # hostname is rejected -- a name that answers public to one resolver and
    # private to another is exactly the DNS-rebinding trick this guard
    # exists to stop, so a host that shows both is never trusted.
    if not hostname or hostname.lower() in ('localhost', '0.0.0.0'):  # nosec B104 -- this REJECTS localhost/0.0.0.0; it is the SSRF guard, not a bind
        return False
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


class _PinnedHTTPConnection(http.client.HTTPConnection):
    # Connects the socket to a pre-resolved IP (passed as `pinned_ip`) instead
    # of re-resolving `self.host`. The hostname is still used for the Host
    # header (http.client sets it from self.host). This is what closes the
    # DNS-rebinding TOCTOU: the SSRF check resolves the name once and the
    # actual connect uses that exact address -- an attacker can no longer
    # hand a public IP to the check and a private one to the connect.

    def __init__(self, *args, pinned_ip=None, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    # Same pinning as _PinnedHTTPConnection, plus TLS: the socket connects to
    # `pinned_ip` while the certificate is validated against, and SNI is sent
    # for, the original hostname (self.host) -- so pinning the IP does not
    # break TLS host verification.

    def __init__(self, *args, pinned_ip=None, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def connect(self):
        sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedIPHandler(urllib.request.HTTPHandler):
    # An HTTPHandler that resolves and pins each request's hostname (including
    # every redirect hop, which arrive here as fresh requests) to a public IP
    # before connecting.

    def do_open(self, http_class, req, **http_conn_args):
        parsed = urllib.parse.urlparse(req.full_url)
        ip = _resolve_public_ip(parsed.hostname)
        if not ip:
            raise urllib.error.URLError(
                'host resolves to a private, internal, or unresolvable address')
        http_conn_args['pinned_ip'] = ip
        return super().do_open(http_class, req, **http_conn_args)


def _safe_urlopen(req, timeout=20):
    """Open `req` (a urllib.request.Request) with SSRF-safe, DNS-rebinding-
    proof address pinning: each hostname is resolved once and the socket
    connects to that exact public IP, so a name that is public at check time
    cannot silently rebind to a private address for the connect. The Host
    header and TLS SNI/cert verification still use the original hostname.
    Raises URLError when the host is not public. `req` may be a string URL or
    a Request."""
    if isinstance(req, str):
        req = urllib.request.Request(req)
    opener = urllib.request.build_opener(_PinnedIPHandler())
    return opener.open(req, timeout=timeout)


def _download_dest_rel_path(scope, agent_id, safe_name):
    # Extracted as its own pure function so the personal-vs-shared routing
    # decision is directly testable without mocking the network fetch and
    # Jev classification call that sit around it in the real endpoint.
    if scope == 'shared':
        return f'pending_review/shared/{safe_name}'
    return f'pending_review/downloads/{agent_id}/{safe_name}'


def _sanitize_download_filename(filename):
    # A safe, flat filename only -- os.path.basename strips any directory
    # component (../../.env style traversal via the filename itself),
    # then anything left that isn't alphanumeric/underscore/dot/hyphen is
    # replaced, and the whole thing is length-capped.
    return re.sub(r'[^A-Za-z0-9_.\-]', '_', os.path.basename(filename))[:200]


def _looks_like_gmail_app_password(pw):
    """A Gmail app-password is exactly 16 chars: XXXX XXXX XXXX XXXX (spaces
    optional). Reject anything else loudly rather than storing a bad secret."""
    pw = (pw or '').strip().replace(' ', '')
    if len(pw) != 16 or not pw.isalnum():
        return False
    return True


def _block_link_value(block):
    # The value a block contributes to the chain link -- what the NEXT block's
    # `prev` must reference. For a promoted-FILE block it is the file's content
    # hash; for a DECISION block it is the re-serialized content hash. Both
    # block kinds share one ledger and link through the same `head`.
    if block.get('kind'):
        return hashlib.sha256(json.dumps(block, sort_keys=True, default=str).encode()).hexdigest()
    return block.get('sha256')


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(64 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()