"""Agent-improvable rule tables (operator direction 2026-11-21): the hot,
hard-coded rule lists that agents keep hitting the edges of move into an
additive overlay file so agents can grow them over time WITHOUT touching
rails and WITHOUT a restart.

world/agent_rules.json (adjacent to model_providers.json) namespaces:
  readonlyBinaries      extra single binaries the command gate may pre-approve
                        on top of 182's built-in read-only set
  readonlyGitSubs       extra `git <sub>` read-only subcommands
  forbiddenFlags        {tool: [flag, ...]} extra write-capable flags the
                        read-only parser must refuse for that tool
  spikeProblemSignals   extra lowercase substrings that make a spike finding
                        eligible for JEV triage (70_body_08's pre-filter)
  internalSignalActions extra action_log kinds surfaced in the VP digest
                        (425's internal-signals section)

EVERY table is additive-only over the code's built-in base: the loader
merges the overlay ON TOP of the shipped constants and can never remove a
rail. Invalid or missing files fail closed to the built-in base, so a
malformed agent edit degrades to today's behavior instead of opening the
gate. Reload is stat-guarded (at most once per second) like
model_providers.json, so an operator or harness-optimizer edit lands within
a second of saving.

The improvement loop: an agent proposes an entry through its director
(or the epistemics/harness-optimizer flow), the change is reviewed like any
code change (BP-60 runway), applied to agent_rules.json, and picked up live.
"""
import json
import os
import threading
import time

# Resolved once at import from ROOT (world/) -- the same directory
# model_providers.json lives in. Kept as this part's own constant so tests
# can repoint it without patching the shared config namespace.
_AGENT_RULES_PATH = os.path.join(ROOT, 'agent_rules.json')
_AGENT_RULES_STAT_INTERVAL_S = 1.0

# Overlay namespaces and their hard caps. Caps bound a runaway agent loop:
# the file grows at most this much per namespace before the validator
# starts refusing new entries.
_AGENT_RULES_MAX = {
    'readonlyBinaries': 120,
    'readonlyGitSubs': 40,
    'forbiddenFlags': 120,      # total flags across all tools
    'spikeProblemSignals': 300,
    'internalSignalActions': 100,
}

# A safe token shape per namespace: binary names and git subs are plain
# words (no paths, no separators); signals/actions are short lowercase
# phrases; flags are dash-led tokens.
_AGENT_RULES_PATTERNS = {
    'readonlyBinaries': r'^[a-zA-Z0-9_.+-]{1,40}$',
    'readonlyGitSubs': r'^[a-zA-Z0-9-]{1,32}$',
    'spikeProblemSignals': r'^[a-z0-9\'\' !-]{2,40}$',
    'internalSignalActions': r'^[a-z0-9_]{2,64}$',
}

_agent_rules_cache = {'mtime': None, 'at': 0.0, 'data': {}}
_agent_rules_lock = threading.Lock()
_agent_rules_reject_logged = False


def _agent_rules_validate(raw):
    """Validate one parsed overlay dict -> clean dict. Unknown namespaces
    and bad entries are dropped silently EXCEPT that any drop invalidates
    nothing else (a half-good file still serves its good entries). Returns
    None when the file itself is unusable (not a dict)."""
    if not isinstance(raw, dict):
        return None
    import re
    out = {}
    flag_budget = _AGENT_RULES_MAX['forbiddenFlags']
    for name, entries in raw.items():
        if name == 'forbiddenFlags':
            if not isinstance(entries, dict):
                continue
            flags = {}
            for tool, fl in entries.items():
                if not isinstance(fl, list):
                    continue
                keep = []
                for f in fl:
                    if (isinstance(f, str) and re.fullmatch(
                            r'^-{1,2}[a-zA-Z0-9][a-zA-Z0-9=-]{0,48}$', f.strip())
                            and flag_budget > 0):
                        keep.append(f.strip())
                        flag_budget -= 1
                if keep:
                    flags[str(tool)[:40]] = keep
            if flags:
                out['forbiddenFlags'] = flags
            continue
        if name not in _AGENT_RULES_MAX or not isinstance(entries, list):
            continue
        pat = re.compile(_AGENT_RULES_PATTERNS.get(name, r'^.{1,64}$'))
        seen = []
        for e in entries:
            if not isinstance(e, str):
                continue
            e = e.strip()
            if e and pat.fullmatch(e) and e not in seen \
                    and len(seen) < _AGENT_RULES_MAX[name]:
                seen.append(e)
        if seen:
            out[name] = seen
    return out


def _agent_rules_table(name):
    """The overlay entries for one namespace (validated). Cached with a
    stat guard; a missing or corrupt file yields an empty overlay (the
    built-in base serves unchanged)."""
    with _agent_rules_lock:
        now = time.time()
        if now - _agent_rules_cache['at'] < _AGENT_RULES_STAT_INTERVAL_S \
                and _agent_rules_cache['data'] is not None:
            return _agent_rules_cache['data'].get(name) or []
        try:
            mtime = os.stat(_AGENT_RULES_PATH).st_mtime
        except OSError:
            mtime = None
        if mtime == _agent_rules_cache['mtime'] \
                and _agent_rules_cache['data'] is not None:
            _agent_rules_cache['at'] = now
            return _agent_rules_cache['data'].get(name) or []
        data = {}
        try:
            with open(_AGENT_RULES_PATH, encoding='utf-8') as fh:
                data = _agent_rules_validate(json.load(fh)) or {}
        except Exception:
            # Corrupt or half-written file: keep the LAST GOOD overlay so a
            # bad edit cannot strip live additions mid-save, and never
            # widen anything. First-load failure is just an empty overlay.
            data = _agent_rules_cache['data'] or {}
            global _agent_rules_reject_logged
            if not _agent_rules_reject_logged:
                _agent_rules_reject_logged = True
                try:
                    log_action(None, 'agent_rules_file_rejected',
                               {'path': _AGENT_RULES_PATH}, authorized=True)
                except Exception:
                    pass
        _agent_rules_cache['mtime'] = mtime
        _agent_rules_cache['at'] = now
        _agent_rules_cache['data'] = data
        return data.get(name) or []


def _agent_rules_extra(name):
    """The overlay entries for one namespace as a set, ready for
    `x in base or x in extra` membership checks."""
    return set(_agent_rules_table(name))


def _agent_rules_forbidden(tool):
    """Extra refused flags for one tool from the forbiddenFlags overlay."""
    return tuple((_agent_rules_table('forbiddenFlags') or {}).get(tool) or ())
