
# URL reputation wiring (operator direction, 2026-11-01): the composite
# trust score from world/url_trust.py, cached per host in the settings
# table, consulted by the URL gates BEFORE they spend a Jev quorum:
#
#   band 'bad'      -> refuse without Jev (blocklist evidence beats vibes)
#   band 'trusted'  -> ALLOW ordinary reads without Jev (browser goto/read/
#                      screenshot, api-call GET/HEAD) -- the exact class of
#                      legitimate fetch Jev's fail-closed confidence gate
#                      kept strangling; write-shaped actions NEVER auto-
#                      allow on reputation, they keep their Jev gate
#   band unknown/   -> Jev as today, unchanged (the score is evidence,
#   suspect            never the judge, for the ambiguous middle)
#
# The cache lives in the settings table (lock-free, persists across
# restarts) keyed by HOST with a 24h TTL, so a busy host costs one
# reputation lookup per day no matter how many gates consult it. Keys for
# the keyed sources (VirusTotal, Google Safe Browsing) are plain env vars
# (VIRUSTOTAL_API_KEY / SAFEBROWSING_API_KEY), same convention as
# TAVILY_API_KEY -- unset means that source is simply absent and the
# score degrades, it never blocks.
import calendar
import json
import time
import urllib.parse

import url_trust as _url_trust

_URL_TRUST_CACHE_SETTING = 'url_trust_cache'
_URL_TRUST_TTL_S = 24 * 3600


def _url_trust_summary(verdict):
    """One compact line for logs, gates, and escalation notes."""
    if not verdict:
        return 'no reputation data'
    signals = verdict.get('signals') or {}
    age = signals.get('ageDays')
    vt = signals.get('virustotal')
    heur = signals.get('heuristics') or []
    parts = [f"band={verdict.get('band')}"]
    if isinstance(age, int):
        parts.append(f'age {age}d')
    if vt:
        parts.append(f"vt {vt.get('malicious', 0)}m/{vt.get('harmless', 0)}h")
    if signals.get('urlhaus') is True:
        parts.append('urlhaus HIT')
    if signals.get('safebrowsing') is True:
        parts.append('safebrowsing HIT')
    if heur:
        parts.append('flags: ' + '; '.join(heur[:3]))
    errors = signals.get('errors') or []
    if errors:
        parts.append('degraded: ' + '; '.join(errors[:2]))
    return ', '.join(parts)


def _url_trust_verdict(url):
    """Per-host cached composite verdict (or None when the URL has no
    usable host). Cache entries are full score_url dicts stored as JSON in
    the settings table; freshness is the verdict's own checkedAt stamp."""
    try:
        parsed = urllib.parse.urlparse(url if '://' in url else 'https://' + url)
        host = (parsed.hostname or '').strip().lower()
    except ValueError:
        return None
    if not host:
        return None
    try:
        cache = json.loads(_get_setting(_URL_TRUST_CACHE_SETTING, '') or '{}')
    except ValueError:
        cache = {}
    entry = cache.get(host)
    if isinstance(entry, dict):
        try:
            # checkedAt is a UTC stamp; mktime would read it as LOCAL
            # time and a UTC-hosted server would see every fresh entry as
            # TTL-expired. timegm parses it as what it is.
            stamp = calendar.timegm(time.strptime(entry.get('checkedAt', ''),
                                                  '%Y-%m-%dT%H:%M:%SZ'))
            if time.time() - stamp < _URL_TRUST_TTL_S:
                return entry
        except (ValueError, OverflowError, TypeError):
            pass  # stale-shaped entry: re-score
    verdict = _url_trust.score_url(
        url,
        virustotal_api_key=os.environ.get('VIRUSTOTAL_API_KEY') or None,
        safebrowsing_api_key=os.environ.get('SAFEBROWSING_API_KEY') or None)
    slim = dict(verdict)
    slim['signals'] = {k: v for k, v in (verdict.get('signals') or {}).items()}
    cache[host] = slim
    try:
        _set_setting(_URL_TRUST_CACHE_SETTING, json.dumps(cache))
    except Exception as exc:  # noqa: BLE001 -- a failed cache write must not veto a verdict
        print(f'[url-trust] cache write failed: {exc}', flush=True)
    return verdict


def _url_trust_http_gate(verdict, method):
    """The api_call composition: 'allow' skips Jev for a trusted host on a
    READ method, 'deny' refuses a blocklist hit for ANY method, and None
    defers to the existing Jev classify. Reputation never widens write
    access -- a trusted host still needs Jev for POST/PUT/DELETE."""
    if not verdict:
        return None
    band = verdict.get('band')
    if band == 'bad':
        return 'deny'
    if band == 'trusted' and method in ('GET', 'HEAD'):
        return 'allow'
    return None


def _url_trust_browser_gate(verdict, op):
    """The browser_act composition: reads (goto/read/screenshot) may ride a
    trusted verdict; any op is hard-refused on a bad verdict; everything
    else defers to Jev (None)."""
    if not verdict:
        return None
    band = verdict.get('band')
    if band == 'bad':
        return 'deny'
    if band == 'trusted' and op in BROWSER_ACT_READ_OPS:
        return 'allow'
    return None
