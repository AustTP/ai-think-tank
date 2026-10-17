"""Live egress-location intelligence for the sandbox VPN exits.

The exit registry (sandboxes/vpn-sandbox/exits.json) records where a
proxy is SUPPOSED to be (country/city/relay). This module observes
where it actually IS: fetch an IP echo through the exit's proxy,
geolocate the returned address, and cache the snapshot in the sim
state (state['vpnExitLocations']) under a TTL, so /api/vpn-exits can
answer "what ZIP code does this exit appear to be in" with real
evidence instead of the static label. Area codes are not reliably
derivable from free geolocation feeds, so the field is reported as
null rather than guessed.
"""
import ipaddress
import json
import time
import urllib.error
import urllib.parse
import urllib.request

EGRESS_IP_URL = 'https://ifconfig.me/ip'
GEOLOCATION_URL = ('http://ip-api.com/json/{ip}'
                   '?fields=status,message,country,regionName,city,zip,isp,query')
EGRESS_FETCH_TIMEOUT_S = 10.0
GEOLOCATION_TIMEOUT_S = 10.0
LOCATION_TTL_S = 3600.0


def fetch_egress_ip(proxy_url=None, timeout_s=EGRESS_FETCH_TIMEOUT_S):
    """Return the public IP seen through proxy_url (direct when None),
    or None on any failure. The candidate is validated as a real IP so
    a proxy error page can never be stored as an address."""
    handlers = []
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler(
            {'http': proxy_url, 'https': proxy_url}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(EGRESS_IP_URL, timeout=timeout_s) as response:
            candidate = response.read(256).decode('utf-8', 'replace').strip()
        ipaddress.ip_address(candidate)
        return candidate
    except (urllib.error.URLError, OSError, ValueError):
        return None


def geolocate_ip(ip, timeout_s=GEOLOCATION_TIMEOUT_S):
    """Geolocate an IP via ip-api.com (free tier, JSON over HTTP).
    Returns country/region/city/zip/isp, or None when the feed fails
    or rejects the address. 'areaCode' is always None: no free feed
    returns it, and a guessed area code is worse than an honest null."""
    url = GEOLOCATION_URL.format(ip=urllib.parse.quote(ip))
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return None
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:  # nosec B310
            payload = json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get('status') != 'success':
        return None
    return {
        'country': payload.get('country'),
        'region': payload.get('regionName'),
        'city': payload.get('city'),
        'zip': payload.get('zip'),
        'areaCode': None,
        'isp': payload.get('isp'),
    }


def is_fresh(record, now=None, ttl_s=LOCATION_TTL_S):
    """True when a stored location record is younger than ttl_s."""
    if not isinstance(record, dict):
        return False
    fetched_at = record.get('fetchedAt')
    if not isinstance(fetched_at, (int, float)):
        return False
    return (time.time() if now is None else now) - fetched_at <= ttl_s


def location_snapshot(state):
    """The stored {exit name: record} map from a sim state blob."""
    if not isinstance(state, dict):
        return {}
    snapshot = state.get('vpnExitLocations')
    return snapshot if isinstance(snapshot, dict) else {}


def build_record(egress_ip, location):
    return {'egressIp': egress_ip, 'location': location, 'fetchedAt': time.time()}


def record_location(state, name, record):
    """Upsert one exit's record into the state blob (returns the record)."""
    state['vpnExitLocations'] = {**location_snapshot(state), name: record}
    return record


def describe_exit(name, exits, snapshot=None):
    """Registry metadata for one exit plus its last observed location,
    if any. Backward-compatible with the flat registry shape."""
    meta_raw = exits.get(name)
    meta = meta_raw if isinstance(meta_raw, dict) else {}
    record_raw = (snapshot or {}).get(name)
    record = record_raw if isinstance(record_raw, dict) else {}
    location_raw = record.get('location')
    location = location_raw if isinstance(location_raw, dict) else {}
    return {
        'name': name,
        'country': meta.get('country'),
        'city': meta.get('city'),
        'relay': meta.get('relay'),
        'proxy': meta.get('proxy'),
        'useFor': meta.get('useFor'),
        'egressIp': record.get('egressIp'),
        'observedLocation': ({
            'country': location.get('country'),
            'region': location.get('region'),
            'city': location.get('city'),
            'zip': location.get('zip'),
            'areaCode': location.get('areaCode'),
            'isp': location.get('isp'),
        } if location else None),
        'locationFresh': is_fresh(record),
        'fetchedAt': record.get('fetchedAt'),
    }


def refresh_exit(name, exits):
    """Observe one exit now. Returns (record, error): a record exists
    whenever an egress IP was seen (even if geolocation failed, in
    which case error says so); (None, error) when no IP came back."""
    meta_raw = exits.get(name)
    meta = meta_raw if isinstance(meta_raw, dict) else {}
    egress_ip = fetch_egress_ip(meta.get('proxy'))
    if not egress_ip:
        return None, f'no egress IP through {name}'
    location = geolocate_ip(egress_ip)
    record = build_record(egress_ip, location)
    return record, (None if location else 'geolocation unavailable')


def refresh_exits(exits, names=None):
    """Refresh every configured exit (or the named subset). Returns
    {name: {'ok': bool, 'record': record-or-None, 'error': str-or-None}}."""
    selected = list(exits) if names is None else [n for n in names if n in exits]
    results = {}
    for name in selected:
        record, error = refresh_exit(name, exits)
        results[name] = {'ok': record is not None, 'record': record, 'error': error}
    return results
