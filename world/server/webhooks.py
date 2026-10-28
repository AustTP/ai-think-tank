"""Inbound webhook intake and its consumer loop.

Extracted from serve.py. Two halves:

  1. Storage (the /api/webhook/{channel} endpoint in serve.py) is a pure sink:
     one JSON line per relayed event, tagged with the channel that produced it.
     No LLM call and no state write, so an inbound burst can never wedge the
     event loop on model latency.

  2. This module consumes those lines. A standing loop tails the JSONL file by
     byte offset and turns each new entry into a large request through
     serve._dispatch_big_task -- the same routing the player's assign-big-task
     intent uses.

Adding an integration is adding one entry to CHANNELS (plus a watcher that
POSTs to /api/webhook/<name>): the goal text, the self-activity gate, and the
env var that switches the channel on are all described as data. The consumer
never contains channel-specific code.

Everything that tests or other modules patch on the serve module (the file
path, the handle, the routing helper, the state loader) is reached through
`_serve.X` at call time, so serve.py re-exports these names unchanged and no
call site moved.
"""

import asyncio
import json

# The channels a webhook can write to, and how each one is turned into an ask.
#   goal.author_template  -- str.format template for the payload author ('' to skip)
#   goal.body_fields      -- first present payload key supplies the body text
#   goal.include_url      -- append the payload url in brackets
#   goal.empty            -- fallback goal text when nothing else is present
#   own_handle_attr       -- serve attribute holding our own account handle; when
#                            it appears in own_handle_fields the entry is our own
#                            activity and must not be delegated (unset -> skip all)
CHANNELS = {
    'github': {
        'label': 'GitHub event',
        # Producer: the tank's own watch loop (195_external_watches) polls
        # the registered GitHub API for CI runs and new issues on the
        # watched repo and appends normalized entries to webhooks.jsonl --
        # no external relay needed. Payload shape: {author, text, url}.
        'goal': {
            'author_template': 'GitHub {author}',
            'body_fields': ('text', 'subject'),
            'include_url': True,
            'empty': 'GitHub event (no text captured)',
        },
        # The producer is the tank's own watch loop; without a repo to watch
        # it has nothing to relay, so the channel stays off.
        'requires_env': 'GITHUB_WATCH_REPO',
    },
    'x-mention': {
        'label': 'X mention',
        'goal': {
            'author_template': 'X mention from {author}',
            'body_fields': ('text', 'subject'),
            'include_url': True,
            'empty': 'X mention (no text captured)',
        },
        'own_handle_attr': 'X_HANDLE',
        'own_handle_fields': ('author', 'subject', 'url'),
    },
}

# Public tuple of valid channel names; serve.py's endpoint validates against it.
WEBHOOK_CHANNELS = tuple(CHANNELS)

_WEBHOOK_SEEN: set = set()
_WEBHOOK_POLL_INTERVAL_S = 10


def _webhook_channel_spec(channel):
    return CHANNELS.get(channel)


def _webhook_channel_enabled(channel):
    """True when the channel has everything it needs to run. A channel gated on
    our own handle stays off until that handle is configured; a channel with a
    requires_env gate stays off until that env value exists (its producer --
    e.g. the GitHub watch loop -- has nothing to watch without it)."""
    import serve as _serve
    spec = CHANNELS.get(channel)
    if spec is None:
        return False
    required = str(spec.get('requires_env') or '')
    if required and not (str((_serve._load_env() or {}).get(required, '') or '').strip()):
        return False
    attr = spec.get('own_handle_attr')
    if not attr:
        return True
    return bool((getattr(_serve, attr, '') or '').strip())


def active_webhook_channels():
    """Channels whose watcher should be consuming right now (config complete)."""
    return tuple(c for c in WEBHOOK_CHANNELS if _webhook_channel_enabled(c))


def _webhook_seen_key(entry):
    payload = entry.get('payload') or {}
    wid = payload.get('id') or payload.get('threadId')
    return f"{entry.get('channel')}:{wid}" if wid else None


def _webhook_seed_seen():
    """Seed the dedupe set from every line of the webhook file -- entries a
    previous process instance already received must not be re-consumed on
    boot (that would re-delegate an old mention)."""
    import serve as _serve
    seen = set()
    try:
        with open(_serve.WEBHOOKS_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                key = _webhook_seen_key(entry) if isinstance(entry, dict) else None
                if key:
                    seen.add(key)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001 -- seeding must never kill the loop
        print(f'[webhook] seed scan failed: {e}', flush=True)
    return seen


def _webhook_goal_text(payload, channel='x-mention'):
    """Turn a relayed event into the ask handed to the tank, following the
    channel's goal descriptor. Unknown channels get the raw payload id-free
    text with the generic label, so a new watcher is usable before it has a
    formatter."""
    payload = payload or {}
    spec = CHANNELS.get(channel) or {}
    goal = spec.get('goal') or {}
    parts = []
    author = str(payload.get('author') or '').strip()
    template = goal.get('author_template')
    if author and template:
        parts.append(template.format(author=author))
    body = ''
    for field in goal.get('body_fields') or ():
        body = str(payload.get(field) or '').strip()
        if body:
            break
    if body:
        parts.append(body)
    text = ' -- '.join(parts) or goal.get('empty') or (spec.get('label') or channel)
    if goal.get('include_url'):
        url = str(payload.get('url') or '').strip()
        if url:
            text = f'{text} [{url}]'
    return text


def _webhook_self_mention(payload, channel='x-mention'):
    """True when the relayed event is OUR OWN account's activity, not someone
    contacting us: watchers relay every notification, and the tank must never
    delegate work because we posted. Fails closed -- an unset handle means
    everything looks like ours, so nothing is delegated."""
    import serve as _serve
    spec = CHANNELS.get(channel) or {}
    attr = spec.get('own_handle_attr')
    if not attr:
        return False
    handle = (getattr(_serve, attr, '') or '').strip().lstrip('@').lower()
    if not handle:
        return True
    payload = payload or {}
    fields = spec.get('own_handle_fields') or ('author', 'subject', 'url')
    blob = ' '.join(str(payload.get(k) or '') for k in fields).lower()
    return handle in blob


def _webhook_read_new(offset):
    """Return the entries appended after byte `offset`, each tagged with the
    position right after its line, so a half-written last line (the endpoint
    appends without a lock) is picked up whole on the next pass."""
    import serve as _serve
    out = []
    try:
        with open(_serve.WEBHOOKS_PATH, 'rb') as f:
            f.seek(offset)
            data = f.read()
    except FileNotFoundError:
        return out, offset
    pos = 0
    while True:
        nl = data.find(b'\n', pos)
        if nl == -1:
            break
        line = data[pos:nl].decode('utf-8', 'replace').strip()
        pos = nl + 1
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if isinstance(entry, dict):
            out.append({'entry': entry, 'end': offset + pos})
    return out, offset + pos


async def _webhook_handle_entry(entry):
    """Route ONE stored entry. Returns the dispatch outcome string, 'dropped',
    'self', 'seen', or 'no-state'. Shared by the poll loop and any future
    replay tool, so consuming an entry never depends on how it was read."""
    import serve as _serve
    channel = entry.get('channel')
    spec = CHANNELS.get(channel)
    if spec is None:
        return 'dropped'
    key = _webhook_seen_key(entry)
    if key is None:
        # Nothing to dedupe on -- a replayed line would re-delegate the same
        # ask, so unidentifiable entries are dropped.
        print(f'[webhook] {channel} entry without an id -- dropped', flush=True)
        return 'dropped'
    if key in _WEBHOOK_SEEN:
        return 'seen'
    _WEBHOOK_SEEN.add(key)
    payload = entry.get('payload') or {}
    if _webhook_self_mention(payload, channel):
        return 'self'
    goal = _webhook_goal_text(payload, channel)
    state = _serve._state_begin()
    if not state:
        print('[webhook] state unavailable -- entry dropped', flush=True)
        return 'no-state'
    try:
        outcome, data = await _serve._dispatch_big_task(state, goal, 'external')
    except Exception as e:  # noqa: BLE001 -- one bad entry must not kill the loop
        print(f'[webhook] dispatch failed: {e}', flush=True)
        return 'failed'
    if outcome == 'staffed':
        print(f"[webhook] filed {channel} with the {data['teamName']} team (request {data['request']})", flush=True)
    elif outcome == 'backlogged':
        print(f"[webhook] {channel} backlogged under feature {data['feature'].get('id')}", flush=True)
    elif outcome == 'new_team':
        print(f"[webhook] created team {data.get('id')} for the {channel}", flush=True)
    else:
        print(f'[webhook] {channel} not filed ({outcome})', flush=True)
    return outcome


async def _webhook_poll_loop():
    """Standing loop (created in _lifespan when any channel is enabled): tail
    the webhook file and route new entries for every enabled channel through
    _dispatch_big_task."""
    _WEBHOOK_SEEN.update(_webhook_seed_seen())
    offset = 0
    while True:
        try:
            found, new_offset = await asyncio.to_thread(_webhook_read_new, offset)
            offset = new_offset
            for item in found:
                entry = item['entry']
                channel = entry.get('channel')
                if channel not in CHANNELS:
                    continue
                if not _webhook_channel_enabled(channel):
                    continue
                await _webhook_handle_entry(entry)
        except Exception as e:  # noqa: BLE001 -- keep tailing no matter what
            print(f'[webhook] loop error: {e}', flush=True)
        await asyncio.sleep(_WEBHOOK_POLL_INTERVAL_S)
