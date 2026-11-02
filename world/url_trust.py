"""External URL reputation: a composite trust score for the tank's URL gates.

Why this exists (operator direction, 2026-11-01): the Jev classify gate is
reluctant by design -- it fails closed and needs high confidence, so
ordinary, legitimate URLs stall at "not confident enough". This module
gives the gates OBJECTIVE evidence to act on instead:

- a hard BLOCKLIST hit (URLhaus malware-URL database, Google Safe
  Browsing, VirusTotal engines) is stronger evidence to REFUSE than Jev
  vibes,
- a clean, long-lived, well-formed domain is strong enough evidence to
  ALLOW ordinary READS without a Jev round trip,
- everything in between still goes to Jev -- the score is evidence there,
  never the judge for the ambiguous middle.

Composition (all keyless signals run always; keyed signals run only when
the operator registered a key):

- RDAP domain age (rdap.org bootstrap, no key): young domains are the
  single strongest phishing predictor.
- URLhaus (abuse.ch, no key): curated live malware-URL feed.
- Google Safe Browsing v4 (key): phishing/malware/unwanted-software
  verdicts.
- VirusTotal v3 (key): N-engine aggregate; malicious >= 2 is bad, 1 is
  suspect, and a wide harmless majority supports trusted.
- Local heuristics (no network): IP-literal hosts, punycode IDN
  homographs, suspicious cheap TLDs, brand tokens in odd positions,
  credential-bait path words.

Failure policy: every signal degrades independently -- a down or
rate-limited source becomes a None signal with a noted error, and the
score falls back to the remaining evidence. A fully unavailable
reputation stack yields 'unknown', which is exactly today's behavior
(the gate then asks Jev), so this module can never make the gates MORE
permissive than they already are; it can only add evidence.
"""
import ipaddress
import json
import re
import time
import urllib.parse
import urllib.request

RDAP_TIMEOUT_S = 8
URLHAUS_TIMEOUT_S = 8
KEYED_TIMEOUT_S = 8

# Cheap or free-registration TLDs that dominate abuse statistics. Presence
# alone is only a suspect signal, never a bad verdict -- plenty of honest
# projects live on them.
_SUSPICIOUS_TLDS = frozenset({
    'zip', 'mov', 'tk', 'top', 'gq', 'cf', 'ml', 'ga', 'work', 'click', 'link',
    'fit', 'rest', 'bar', 'surf', 'cam', 'cyou', 'icu', 'buzz',
})

# Brand tokens that phishers love to embed in subdomains or paths of domains
# that have nothing to do with the brand. Only meaningful when the
# registrable domain does NOT itself belong to the brand's real domain.
_BRAND_TOKENS = frozenset({
    'paypal', 'apple', 'icloud', 'microsoft', 'office365', 'outlook', 'google',
    'gmail', 'amazon', 'netflix', 'facebook', 'instagram', 'whatsapp',
    'coinbase', 'binance', 'metamask', 'ledger', 'dhl', 'fedex', 'usps',
})
_BRAND_REAL_DOMAINS = {
    'paypal': 'paypal.com', 'apple': 'apple.com', 'icloud': 'icloud.com',
    'microsoft': 'microsoft.com', 'office365': 'microsoft.com',
    'outlook': 'outlook.com', 'google': 'google.com', 'gmail': 'google.com',
    'amazon': 'amazon.com', 'netflix': 'netflix.com',
    'facebook': 'facebook.com', 'instagram': 'instagram.com',
    'whatsapp': 'whatsapp.com', 'coinbase': 'coinbase.com',
    'binance': 'binance.com', 'metamask': 'metamask.io',
    'ledger': 'ledger.com', 'dhl': 'dhl.com', 'fedex': 'fedex.com',
    'usps': 'usps.com',
}

# Credential-bait words in the PATH of a URL are normal (a real bank has
# /login) and are NOT scored; only brand tokens in the HOST of a
# non-brand domain are the homograph pattern worth flagging.
_CREDENTIAL_PATH_WORDS = frozenset({
    'login', 'signin', 'verify', 'secure', 'account', 'update', 'confirm',
    'recover', 'unlock', 'webscr',
})

_SUBDOMAIN_DEPTH_SUSPECT = 4  # a.b.c.d.example.com -> 4 labels before the domain


def _http_json(url, method='GET', data=None, headers=None, timeout=8):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {'User-Agent': 'think-tank-url-trust/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    try:
        return json.loads(body.decode('utf-8', 'replace'))
    except ValueError:
        return None


def registrable_domain(host):
    """Naive registrable domain: the last two labels. Not a full Public
    Suffix List -- co.uk-style second-level registries will read as
    'co.uk' (age lookup fails, score degrades to other signals). Good
    enough for scoring; never used for security decisions by itself."""
    labels = (host or '').split('.')
    if len(labels) < 2:
        return ''
    return '.'.join(labels[-2:])


def _rdap_domain_age_days(host):
    """Days since the registrable domain's RDAP registration event, or
    None when RDAP does not cover the TLD, the lookup fails, or the
    response is unparseable. rdap.org bootstraps to the authoritative
    registry server."""
    domain = registrable_domain(host)
    if not domain or domain == host and host.count('.') < 1:
        return None
    try:
        data = _http_json('https://rdap.org/domain/' + domain, timeout=RDAP_TIMEOUT_S)
    except Exception:  # noqa: BLE001 -- any RDAP failure is just a missing signal
        return None
    if not isinstance(data, dict):
        return None
    for event in data.get('events') or []:
        if event.get('eventAction') in ('registration', 'created') \
                and event.get('eventDate'):
            try:
                created = time.strptime(str(event['eventDate'])[:10], '%Y-%m-%d')
                return max(0, (time.mktime(time.gmtime()) - time.mktime(created)) // 86400)
            except (ValueError, OverflowError, TypeError):
                return None
    return None


def _urlhaus_hit(url):
    """True only for a URL that is IN the abuse.ch URLhaus malware feed
    (a curated, human-corroborated list). Network or API errors are not
    hits -- they return None so the score degrades instead of hardening."""
    try:
        data = _http_json('https://urlhaus-api.abuse.ch/v1/url/', method='POST',
                          data=urllib.parse.urlencode({'url': url, 'auth': None}).encode(),
                          headers={'Content-Type': 'application/x-www-form-urlencoded'},
                          timeout=URLHAUS_TIMEOUT_S)
    except Exception:  # noqa: BLE001 -- degraded signal, never a false hit
        return None
    if not isinstance(data, dict):
        return None
    if data.get('query_status') == 'ok' and data.get('urlinfo'):
        return True
    if data.get('query_status') == 'no_results':
        return False
    return None


def _safebrowsing_threat(url, api_key):
    """Google Safe Browsing v4 threatMatches.find. Returns True on any
    listed threat, False on a clean lookup, None when unconfigured or
    degraded."""
    if not api_key:
        return None
    body = json.dumps({
        'client': {'clientId': 'think-tank', 'clientVersion': '1.0'},
        'threatInfo': {
            'threatTypes': ['MALWARE', 'SOCIAL_ENGINEERING', 'UNWANTED_SOFTWARE',
                            'POTENTIALLY_HARMFUL_APPLICATION'],
            'platformTypes': ['ANY_PLATFORM'],
            'threatEntryTypes': ['URL'],
            'threatEntries': [{'url': url}],
        },
    }).encode()
    try:
        data = _http_json(
            'https://safebrowsing.googleapis.com/v4/threatMatches:find?key='
            + urllib.parse.quote(api_key), method='POST', data=body,
            headers={'Content-Type': 'application/json'}, timeout=KEYED_TIMEOUT_S)
    except Exception:  # noqa: BLE001 -- degraded signal
        return None
    if not isinstance(data, dict):
        return False  # an empty object {} is the documented clean answer
    return bool(data.get('matches'))


def _virustotal_stats(url, api_key):
    """VirusTotal v3 per-URL analysis stats. Returns
    {malicious, suspicious, harmless} counts, or None when unconfigured,
    rate-limited, or degraded."""
    if not api_key:
        return None
    url_id = urllib.parse.quote(url.encode(), safe='').replace('%', '').upper()
    # v3 identifiers are the URL's urlsafe-base64 WITHOUT padding; the
    # encoding differs from percent-quoting, so build it directly.
    import base64
    url_id = base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')
    try:
        data = _http_json('https://www.virustotal.com/api/v3/urls/' + url_id,
                          headers={'x-apikey': api_key}, timeout=KEYED_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 -- degraded signal
        text = str(exc)
        if '429' in text or 'Too Many Requests' in text:
            return None
        return None
    if not isinstance(data, dict):
        return None
    stats = ((data.get('data') or {}).get('attributes') or {}).get('last_analysis_stats') or {}
    try:
        return {'malicious': int(stats.get('malicious') or 0),
                'suspicious': int(stats.get('suspicious') or 0),
                'harmless': int(stats.get('harmless') or 0)}
    except (TypeError, ValueError):
        return None


def _heuristics(url, host):
    """Deterministic, no-network red flags. Each entry is a short reason
    string; an empty list means the URL LOOKS structurally ordinary."""
    flags = []
    try:
        ip = ipaddress.ip_address(host)
        flags.append(f'ip-literal host {ip}')
    except ValueError:
        pass
    if 'xn--' in host:
        flags.append('punycode IDN host (homograph risk)')
    tld = host.rsplit('.', 1)[-1] if host and '.' in host else ''
    if tld in _SUSPICIOUS_TLDS:
        flags.append(f'suspicious tld .{tld}')
    domain = registrable_domain(host)
    labels_before_domain = host[:-(len(domain) + 1)].split('.') if domain and host.endswith('.' + domain) else []
    if len(labels_before_domain) >= _SUBDOMAIN_DEPTH_SUSPECT:
        flags.append(f'deep subdomain chain ({len(labels_before_domain)} labels)')
    for token in _BRAND_TOKENS:
        if token in host and domain != _BRAND_REAL_DOMAINS.get(token) \
                and not domain.endswith('.' + _BRAND_REAL_DOMAINS.get(token, '\0')):
            flags.append(f'brand token {token!r} in host of unrelated domain {domain}')
            break
    try:
        parsed = urllib.parse.urlparse(url)
        path_words = set(re.findall(r'[a-z]+', parsed.path.lower()))
        if parsed.query and not path_words:
            path_words = set(re.findall(r'[a-z]+', parsed.query.lower()))
        bait = path_words & _CREDENTIAL_PATH_WORDS
        # Credential-bait words only matter in combination with another
        # structural flag -- a real bank's /login is fine.
        if bait and (flags or tld in _SUSPICIOUS_TLDS):
            flags.append('credential-bait path words: ' + ', '.join(sorted(bait)))
    except ValueError:
        pass
    return flags


def score_url(url, virustotal_api_key=None, safebrowsing_api_key=None,
              rdap_age_days=None, urlhaus_hit=None):
    """The composite verdict. rdap_age_days/urlhaus_hit may be injected by
    callers (tests, caches); None means "look it up" for keyless signals.

    Returns a dict: {'url', 'host', 'domain', 'band', 'score', 'signals':
    {'ageDays', 'urlhaus', 'safebrowsing', 'virustotal', 'heuristics',
    'errors'}, 'checkedAt'}.

    Bands: 'bad' (blocklist hit), 'suspect' (strong red flags or young
    domain), 'trusted' (old, clean, well-formed), 'unknown' (insufficient
    evidence -- the gate's cue to ask Jev as before)."""
    checked_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    try:
        parsed = urllib.parse.urlparse(url if '://' in url else 'https://' + url)
        host = (parsed.hostname or '').strip().lower()
    except ValueError:
        host = ''
    verdict = {'url': url, 'host': host, 'domain': registrable_domain(host),
               'band': 'unknown', 'score': None,
               'signals': {'ageDays': rdap_age_days, 'urlhaus': urlhaus_hit,
                           'safebrowsing': None, 'virustotal': None,
                           'heuristics': [], 'errors': []},
               'checkedAt': checked_at}
    if not host:
        verdict['signals']['errors'].append('no usable host')
        return verdict
    verdict['signals']['heuristics'] = _heuristics(url, host)
    if rdap_age_days is None:
        rdap_age_days = _rdap_domain_age_days(host)
        verdict['signals']['ageDays'] = rdap_age_days
    if urlhaus_hit is None:
        urlhaus_hit = _urlhaus_hit(url)
        verdict['signals']['urlhaus'] = urlhaus_hit
    verdict['signals']['safebrowsing'] = _safebrowsing_threat(url, safebrowsing_api_key)
    verdict['signals']['virustotal'] = _virustotal_stats(url, virustotal_api_key)

    hard_bad = (urlhaus_hit is True
                or verdict['signals']['safebrowsing'] is True
                or (verdict['signals']['virustotal'] or {}).get('malicious', 0) >= 2)
    soft_bad = bool(verdict['signals']['heuristics']) \
        or (isinstance(rdap_age_days, int) and rdap_age_days < 30)
    vt = verdict['signals']['virustotal']
    vt_one_hit = bool(vt and vt['malicious'] == 1)
    vt_clean_support = bool(vt and vt['malicious'] == 0 and vt['harmless'] >= 3)
    old_enough = isinstance(rdap_age_days, int) and rdap_age_days >= 365

    if hard_bad:
        verdict['band'] = 'bad'
        verdict['score'] = 0
        return verdict
    if soft_bad or vt_one_hit:
        verdict['band'] = 'suspect'
        verdict['score'] = 25
        return verdict
    if old_enough and vt_clean_support:
        verdict['band'] = 'trusted'
        verdict['score'] = 90
        return verdict
    if old_enough:
        # Old + structurally clean, but no third-party corroboration: trust
        # the age signal on READ paths only -- the gate decides how to use it.
        verdict['band'] = 'trusted'
        verdict['score'] = 70
        return verdict
    verdict['band'] = 'unknown'
    verdict['score'] = 50
    return verdict
