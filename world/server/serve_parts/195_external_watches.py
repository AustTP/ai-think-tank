# External watch loops (owner expansion, 2026-10-27): the tank's outbound
# integrations are rich but nearly everything is pull or one-way -- nothing
# tells the tank when the world changed BEHIND it (CI failed on its own push,
# a dependency grew a vulnerability, a model provider degraded, a new free
# model appeared). These standing loops close that gap. Each one is small,
# bounded, and fails open: a watch failure logs and retries on the next
# cycle, it never breaks the tank.
#
# Persisted findings live in bounded side-car JSON files under THINK_TANK_DIR
# (the failures.json pattern), NOT in kv_state: these loops run concurrently
# with the sim's state writes, and a read-modify-write race on the big blob
# is a bug class the tank already knows to avoid. Readers (health snapshot,
# selfedit conflicts) read the files directly.
#
# All env gates are re-read every iteration so adding DEADMAN_PING_URL or
# OWNER_CALENDAR_ICS_URL takes effect without a restart.

import asyncio
import json
import os
import time
import urllib.request

WATCH_DEADMAN_INTERVAL_S = 300
WATCH_FREE_MODEL_INTERVAL_S = 6 * 3600
WATCH_OSV_INTERVAL_S = 7 * 86400
WATCH_OSV_BOOT_DELAY_S = 120
WATCH_GITHUB_INTERVAL_S = 600
WATCH_CALENDAR_INTERVAL_S = 6 * 3600
WATCH_HTTP_TIMEOUT_S = 20

_FREE_MODELS_SEEN_MAX = 500
_GITHUB_WATCH_SEEN_MAX = 200
_CALENDAR_EVENTS_MAX = 200
# Only events that could collide with a change window are kept.
_CALENDAR_KEEP_DAYS = 30

WATCH_FREE_MODELS_PATH = os.path.join(THINK_TANK_DIR, 'watches_free_models.json')
WATCH_OSV_PATH = os.path.join(THINK_TANK_DIR, 'watches_osv.json')
WATCH_GITHUB_PATH = os.path.join(THINK_TANK_DIR, 'watches_github.json')
CALENDAR_BLACKOUTS_PATH = os.path.join(THINK_TANK_DIR, 'calendar_blackouts.json')

# Words that indicate a real degradation on a provider status feed. Matched
# case-insensitively against recent item titles/summaries; the feed id must
# start with provider_status_ so ordinary news never trips this.
_PROVIDER_STATUS_KEYWORDS = (
    'incident', 'degraded', 'outage', 'partial outage',
    'elevated error', 'major outage', 'service disruption')


def _load_watch_file(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _save_watch_file(path, data):
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as e:
        log_action(None, 'watch_file_write_failed',
                   {'path': os.path.basename(path), 'error': str(e)[:120]},
                   authorized=False)


def _watch_env(key):
    try:
        return (_load_env() or {}).get(key, '')
    except Exception:
        return ''


def _http_get_json(url, headers=None, timeout=WATCH_HTTP_TIMEOUT_S):
    req = urllib.request.Request(
        url, headers=headers or {'User-Agent': 'AIThinkTankAgent/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 -- fixed https URLs from owner config
        return json.loads(resp.read().decode('utf-8', errors='replace'))


# ---------------------------------------------------------------- deadman --
# All health alerts flow OUT through email/telegram, which fail exactly when
# the tank fails. A dead-man switch inverts the direction: the tank pings an
# external monitor (e.g. healthchecks.io) every 5 minutes, and when the pings
# STOP, the monitor pages the owner. The tank itself does nothing on ping
# failure except log the transition -- the monitor is the alarm.

def _deadman_ping_once(url):
    """One ping. Returns True/False for the outcome, or None when no URL is
    configured (watch disabled -- not a failure)."""
    if not url:
        return None
    try:
        req = urllib.request.Request(
            url, headers={'User-Agent': 'AIThinkTankAgent/1.0'})
        with urllib.request.urlopen(req, timeout=WATCH_HTTP_TIMEOUT_S) as resp:  # nosec B310 -- owner-configured URL
            return 200 <= resp.status < 300
    except Exception:
        return False


async def _deadman_ping_loop():
    last_ok = None
    while True:
        ok = _deadman_ping_once((_watch_env('DEADMAN_PING_URL') or '').strip())
        if ok is False and last_ok is not False:
            log_action(None, 'deadman_ping_failed',
                       {'note': 'the tank could not reach its dead-man '
                                'switch URL -- if this persists, the '
                                'external monitor will page the owner'},
                       authorized=False)
        elif ok is True and last_ok is False:
            log_action(None, 'deadman_ping_recovered', {}, authorized=False)
        last_ok = ok
        await asyncio.sleep(WATCH_DEADMAN_INTERVAL_S)


# ------------------------------------------------- new free-model discovery --
# The free pool is the tank's cost strategy; new :free models appear on
# OpenRouter without announcement. The catalog is already fetched by the tier
# machinery, so the diff is one more consumer of it. New ids are logged and
# surfaced through the health layer -- adding one to the pool stays an owner
# decision (never auto-mutated).

def _free_models_from_catalog(catalog):
    free = []
    for m in catalog or []:
        pricing = m.get('pricing') or {}
        try:
            if (float(pricing.get('prompt') or 1) == 0.0
                    and float(pricing.get('completion') or 1) == 0.0):
                free.append(str(m.get('id') or '').strip())
        except (TypeError, ValueError):
            continue
    return [m for m in free if m]


async def _free_model_discovery_loop():
    while True:
        try:
            await asyncio.to_thread(_free_model_discovery_once)
        except Exception as e:  # noqa: BLE001 -- the watch never kills the loop
            print(f'[free-models] discovery failed: {e}', flush=True)
        await asyncio.sleep(WATCH_FREE_MODEL_INTERVAL_S)


def _free_model_discovery_once():
    catalog = _fetch_openrouter_catalog_sync()
    free = _free_models_from_catalog(catalog)
    data = _load_watch_file(WATCH_FREE_MODELS_PATH, None)
    if data is None and not free:
        # A failed/empty fetch must not become the baseline: it would make
        # the next successful fetch re-announce every existing model.
        return []
    if data is None:
        # First-ever run: baseline the whole catalog silently. Everything
        # already on the catalog is not "news" -- only models that appear
        # AFTER this baseline are.
        _save_watch_file(WATCH_FREE_MODELS_PATH, {
            'updatedAt': time.time(), 'seen': sorted(free), 'new': []})
        return []
    seen = set(data.get('seen') or [])
    fresh = [m for m in free if m not in seen]
    if fresh:
        merged = sorted(set(list(data.get('seen') or []) + free))[-_FREE_MODELS_SEEN_MAX:]
        _save_watch_file(WATCH_FREE_MODELS_PATH, {
            'updatedAt': time.time(), 'seen': merged, 'new': fresh})
        log_action(None, 'free_model_discovered',
                   {'models': fresh[:20], 'note': 'new :free models on the '
                    'OpenRouter catalog -- candidates for the free pool; '
                    'adding them is an owner decision'}, authorized=False)
    return fresh


# --------------------------------------------------- dependency audit (OSV) --
# The tank enforces a 90% test bar on its own code but nothing watched its
# own supply chain. Weekly: parse requirements.txt for exact pins, ask
# OSV.dev (free, no key) which pinned versions have known advisories, record
# the hits. Unpinned requirements are skipped (a floating requirement cannot
# be honestly audited without a lockfile).

def _parse_requirements_pins(path):
    pins = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for raw in f:
                line = raw.split('#', 1)[0].strip()
                if not line or line.startswith(('-', 'git+', 'http')):
                    continue
                spec = line.split(';', 1)[0].strip()
                if '==' not in spec:
                    continue
                name, _, version = spec.partition('==')
                name = name.strip().lower().replace('_', '-')
                version = version.strip()
                if name and version:
                    pins.append({'name': name, 'version': version})
    except OSError:
        pass
    return pins


def _osv_query_sync(pins):
    if not pins:
        return {}
    body = json.dumps({'queries': [
        {'package': {'name': p['name'], 'ecosystem': 'PyPI'},
         'version': p['version']} for p in pins]}).encode('utf-8')
    req = urllib.request.Request(
        'https://api.osv.dev/v1/querybatch', data=body, method='POST',
        headers={'Content-Type': 'application/json',
                 'User-Agent': 'AIThinkTankAgent/1.0'})
    with urllib.request.urlopen(req, timeout=WATCH_HTTP_TIMEOUT_S) as resp:  # nosec B310 -- fixed host
        data = json.loads(resp.read().decode('utf-8', errors='replace'))
    results = data.get('results') or []
    hits = {}
    for p, r in zip(pins, results):
        vulns = (r or {}).get('vulns') or []
        if vulns:
            hits[f"{p['name']}=={p['version']}"] = [
                {'id': v.get('id'), 'summary': (v.get('summary') or '')[:120],
                 'modified': v.get('modified')}
                for v in vulns[:5]]
    return hits


async def _osv_dependency_audit_loop():
    await asyncio.sleep(WATCH_OSV_BOOT_DELAY_S)
    while True:
        try:
            await asyncio.to_thread(_osv_dependency_audit_once)
        except Exception as e:  # noqa: BLE001
            print(f'[osv-audit] audit failed: {e}', flush=True)
        await asyncio.sleep(WATCH_OSV_INTERVAL_S)


def _osv_dependency_audit_once(requirements_path=None):
    # THINK_TANK_DIR is the repo root (think_tank.db, requirements.txt,
    # webhooks.jsonl all live there). Parts exec into serve's namespace, so
    # __file__ here is serve.py's -- never derive repo paths from it.
    path = requirements_path or os.path.join(THINK_TANK_DIR, 'requirements.txt')
    pins = _parse_requirements_pins(path)
    hits = _osv_query_sync(pins)
    _save_watch_file(WATCH_OSV_PATH, {
        'lastRun': time.time(), 'scanned': len(pins), 'hits': hits})
    log_action(None, 'osv_dependency_audit',
               {'scanned': len(pins), 'packagesWithAdvisories': len(hits),
                'note': ('known advisories found -- review watches_osv.json'
                         if hits else 'no known advisories in pinned deps')},
               authorized=False)
    return hits


# -------------------------------------------------------- GitHub CI watcher --
# The self-edit chain ends in a push to main, but the result of the CI run
# that push triggers never comes back. This loop polls the watched repo's
# recent workflow runs and open issues through the already-registered GitHub
# API and appends normalized entries to the webhook sink (webhooks.jsonl),
# where the existing channel pipeline turns them into asks. No new ask
# machinery: github is just a second channel in webhooks.py.

def _github_watch_append(entry):
    entry = {'channel': 'github',
             'receivedAt': time.strftime('%Y-%m-%dT%H:%M:%S+00:00',
                                         time.gmtime()),
             'payload': entry}
    with open(WEBHOOKS_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry) + '\n')


def _github_watch_once(repo):
    data = _load_watch_file(WATCH_GITHUB_PATH, {'runs': [], 'issues': []})
    runs_seen = set(data.get('runs') or [])
    issues_seen = set(data.get('issues') or [])
    runs, _err = _github_call(
        'GET', f'https://api.github.com/repos/{repo}/actions/runs?per_page=10')
    if runs:
        for run in (runs.get('workflow_runs') or []):
            rid = run.get('id')
            if not rid or rid in runs_seen:
                continue
            runs_seen.add(rid)
            if run.get('status') != 'completed':
                continue
            conclusion = run.get('conclusion') or 'unknown'
            if conclusion in ('success', 'skipped'):
                continue
            actor = ((run.get('actor') or {}).get('login')) or 'github-actions'
            _github_watch_append({
                'id': f'run-{rid}',
                'author': actor,
                'text': f'CI run on {run.get("head_branch") or "?"} '
                        f'@ {(run.get("head_sha") or "")[:7]} finished: '
                        f'{conclusion} ({run.get("name") or "workflow"})',
                'url': run.get('html_url') or ''})
    issues, _err = _github_call(
        'GET', f'https://api.github.com/repos/{repo}/issues'
               '?state=open&sort=created&direction=desc&per_page=10')
    if issues:
        for issue in issues:
            iid = issue.get('id')
            if not iid or iid in issues_seen or issue.get('pull_request'):
                continue
            issues_seen.add(iid)
            _github_watch_append({
                'id': f'issue-{iid}',
                'author': ((issue.get('user') or {}).get('login')) or 'unknown',
                'text': f'new issue: {issue.get("title") or "(untitled)"}',
                'url': issue.get('html_url') or ''})
    _save_watch_file(WATCH_GITHUB_PATH, {
        'updatedAt': time.time(),
        'runs': sorted(runs_seen)[-_GITHUB_WATCH_SEEN_MAX:],
        'issues': sorted(issues_seen)[-_GITHUB_WATCH_SEEN_MAX:]})


async def _github_watch_loop():
    while True:
        try:
            repo = (_watch_env('GITHUB_WATCH_REPO') or '').strip()
            if repo and GITHUB_TOKEN:
                await asyncio.to_thread(_github_watch_once, repo)
        except Exception as e:  # noqa: BLE001
            print(f'[github-watch] poll failed: {e}', flush=True)
        await asyncio.sleep(WATCH_GITHUB_INTERVAL_S)


# --------------------------------------------------- owner calendar (ICS) ----
# The change-window system refuses slots that collide with standing tasks and
# other windows, but the OWNER's own calendar was invisible to it. When
# OWNER_CALENDAR_ICS_URL is set, this loop fetches the owner's ICS feed and
# materializes its events as calendar_blackouts.json; selfedit's conflict
# scan reads that file so a change window cannot be scheduled on top of the
# owner's day. ICS is parsed textually: unfolded lines, VEVENT blocks, the
# four fields we need. RRULE series are represented by their first occurrence
# only (a documented limitation -- recurring meetings block only the day the
# series starts, which is conservative for scheduling, not more permissive).

def _ics_dt_to_ts(value, tzid):
    import datetime as _dt
    from zoneinfo import ZoneInfo
    value = value.strip()
    try:
        if value.endswith('Z'):
            dt = _dt.datetime.strptime(value, '%Y%m%dT%H%M%SZ')
            return dt.replace(tzinfo=_dt.timezone.utc).timestamp()
        if 'T' in value:
            dt = _dt.datetime.strptime(value, '%Y%m%dT%H%M%S')
            tz = None
            try:
                tz = ZoneInfo(tzid) if tzid else None
            except Exception:
                tz = None
            return dt.replace(tzinfo=tz or _dt.timezone.utc).timestamp()
        # VALUE=DATE -- an all-day event in the tank's local time.
        dt = _dt.datetime.strptime(value, '%Y%m%d')
        return _dt.datetime.combine(dt.date(), _dt.time(0, 0),
                                    tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return None


def _parse_ics_events(raw):
    """Parse the needed subset of an ICS calendar into
    [{uid, summary, start_ts, end_ts}]. Returns a list; unparseable entries
    are skipped, not fatal."""
    lines = []
    for line in (raw or '').replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if line[:1] in (' ', '\t') and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    events, cur = [], None
    for line in lines:
        name, _, value = line.partition(':')
        key = name.split(';', 1)[0].upper().strip()
        if key == 'BEGIN' and value.strip().upper() == 'VEVENT':
            cur = {}
        elif key == 'END' and value.strip().upper() == 'VEVENT':
            if cur and cur.get('start_ts') is not None:
                end = cur.get('end_ts')
                if end is None or end <= cur['start_ts']:
                    end = cur['start_ts'] + 3600
                events.append({'uid': cur.get('uid') or '',
                               'summary': (cur.get('summary') or '')[:120],
                               'start_ts': cur['start_ts'],
                               'end_ts': end})
            cur = None
        elif cur is not None:
            params = name.split(';', 1)[1] if ';' in name else ''
            if key == 'DTSTART':
                tzid = ''
                for part in params.split(';'):
                    if part.upper().startswith('TZID='):
                        tzid = part.split('=', 1)[1]
                cur['start_ts'] = _ics_dt_to_ts(value, tzid)
            elif key == 'DTEND':
                tzid = ''
                for part in params.split(';'):
                    if part.upper().startswith('TZID='):
                        tzid = part.split('=', 1)[1]
                cur['end_ts'] = _ics_dt_to_ts(value, tzid)
            elif key == 'UID':
                cur['uid'] = value.strip()[:120]
            elif key == 'SUMMARY':
                cur['summary'] = value.strip()
    return events


def _calendar_blackouts():
    """Events the owner's calendar says are booked, from the last successful
    fetch. Stale fetches keep serving the old file -- a change window
    colliding with a KNOWN owner commitment still beats colliding with
    nothing because the fetch failed."""
    data = _load_watch_file(CALENDAR_BLACKOUTS_PATH, {})
    events = data.get('events') or []
    cutoff = time.time() - 86400
    return [e for e in events if e.get('end_ts', 0) > cutoff]


async def _owner_calendar_loop():
    while True:
        try:
            url = (_watch_env('OWNER_CALENDAR_ICS_URL') or '').strip()
            if url:
                await asyncio.to_thread(_owner_calendar_fetch_once, url)
        except Exception as e:  # noqa: BLE001
            print(f'[owner-calendar] fetch failed: {e}', flush=True)
        await asyncio.sleep(WATCH_CALENDAR_INTERVAL_S)


def _owner_calendar_fetch_once(url):
    req = urllib.request.Request(
        url, headers={'User-Agent': 'AIThinkTankAgent/1.0'})
    with urllib.request.urlopen(req, timeout=WATCH_HTTP_TIMEOUT_S) as resp:  # nosec B310 -- owner-configured URL
        raw = resp.read().decode('utf-8', errors='replace')
    now = time.time()
    events = [e for e in _parse_ics_events(raw)
              if e['end_ts'] > now - _CALENDAR_KEEP_DAYS * 86400]
    events.sort(key=lambda e: e['start_ts'])
    _save_watch_file(CALENDAR_BLACKOUTS_PATH, {
        'fetchedAt': now, 'source': 'owner-ics',
        'events': events[:_CALENDAR_EVENTS_MAX]})
    log_action(None, 'owner_calendar_synced', {'events': len(events)},
               authorized=False)
