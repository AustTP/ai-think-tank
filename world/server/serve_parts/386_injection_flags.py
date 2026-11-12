
# ---------------------------------------------------------------------------
# Injection flagging for externally-sourced library content.
#
# The tank's provenance rule already quarantines imported content by PATH
# (source='external' is forced into pending_review/) -- but pending_review
# is a holding area, not a detector. An agent that later reads a quarantined
# page still reads whatever instructions the page was written to plant, and
# the agents act in sandboxes with real tools. This part adds a cheap,
# lexical+structural detector run at ingest time on external content:
# instruction-override phrasing, roleplay hijacks, prompt-template markers,
# credential-fishing vocabulary, and command/URL payloads. It decides
# nothing -- it FLAGS: the flags land in the action_log and the passport
# chain entry for the write, and in the endpoint's response so the writing
# agent sees them immediately. Reviewers looking at pending_review/ get a
# concrete reason to look harder.
#
# Deliberately heuristic: zero model calls, no dependency, and biased
# toward recall over precision at the flag level (a false flag costs one
# extra human glance; a missed planted instruction costs an agent's next
# hundred decisions). v1 covers the /api/library/file external door; the
# download door shares the same detector where it writes.
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS = (
    ('instruction-override',
     r'ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier|earlier)\s+instructions?'),
    ('instruction-override', r'disregard\s+(?:all\s+|the\s+|your\s+)?(?:previous|prior|above)\s+(?:instructions?|prompts?|rules?)'),
    ('instruction-override', r'new\s+instructions?\s*:'),
    ('roleplay-hijack', r'you\s+are\s+now\s+(?:a|an|the)\b'),
    ('roleplay-hijack', r'\bassistant\s*:\s'),
    ('prompt-templates', r'</?system>'),
    ('prompt-templates', r'<\|[a-z_]+\|>'),
    ('credential-fishing', r'\b(?:api[_\s-]?key|secret[_\s-]?key|access[_\s-]?token|password)\b\s*[:=]'),
    ('command-payload', r'\b(?:curl|wget)\b\s+(?:-\S+\s+)*https?://'),
    ('command-payload', r'\brm\s+-rf\b'),
    ('command-payload', r'\bsudo\b'),
)

# The pattern list is DATA (config-not-code, same pattern as redaction.json):
# injection_flags.json overrides the shipped defaults; an unreadable file or a
# single invalid regex falls back to the shipped pattern, so a broken edit can
# only restore today's behavior, never turn the detector off.
INJECTION_FLAGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'injection_flags.json')
_INJECTION_FLAGS_CACHE = {'mtime': None, 'patterns': None}


def _injection_patterns_effective():
    """The live (name, compiled-regex) pairs, from injection_flags.json when
    it reads, else the shipped _INJECTION_PATTERNS."""
    global _INJECTION_FLAGS_CACHE
    mtime = None
    try:
        mtime = os.path.getmtime(INJECTION_FLAGS_PATH)
    except OSError:
        pass
    if (_INJECTION_FLAGS_CACHE['patterns'] is None
            or _INJECTION_FLAGS_CACHE['mtime'] != mtime):
        import re as _re
        import json as _json
        pairs = None
        if mtime is not None:
            try:
                with open(INJECTION_FLAGS_PATH, encoding='utf-8') as fh:
                    raw = _json.load(fh).get('patterns')
            except Exception as exc:  # noqa: BLE001 -- corrupt file falls back
                print(f'[injection-flags] file unreadable, using shipped '
                      f'patterns: {exc}', flush=True)
                raw = None
            if isinstance(raw, list):
                pairs = []
                for spec in raw:
                    if not isinstance(spec, dict):
                        continue
                    name = str(spec.get('name') or '').strip()[:40]
                    pattern = spec.get('regex')
                    if not name or not isinstance(pattern, str) or not pattern:
                        continue
                    try:
                        pairs.append((name, _re.compile(pattern)))
                    except _re.error as exc:
                        print(f'[injection-flags] pattern "{name}" ignored '
                              f'(invalid regex: {exc})', flush=True)
                if not pairs:
                    pairs = None  # an empty file section keeps the shipped set
        _INJECTION_FLAGS_CACHE = {
            'mtime': mtime,
            'patterns': pairs or [(n, _re.compile(p))
                                  for n, p in _INJECTION_PATTERNS],
        }
    return _INJECTION_FLAGS_CACHE['patterns']


def _external_content_flags(text):
    """Lexical+structural injection scan. Returns {'flags': [names...],
    'score': n} -- names deduplicated in first-seen order, score = total
    pattern hits. Empty text scans clean."""
    import re as _re
    flags, score = [], 0
    if not text:
        return {'flags': flags, 'score': score}
    lowered = str(text).lower()
    for name, pattern in _injection_patterns_effective():
        hits = len(_re.findall(pattern, lowered))
        if hits:
            score += hits
            if name not in flags:
                flags.append(name)
    return {'flags': flags, 'score': score}


def _log_library_injection_flags(agent_id, path, text, authorized):
    """Record injection flags for an external library write: action_log row
    (queryable, surfaces in audits) and the flag names in the response the
    caller sees. Returns the flags dict. A failure here must never block
    the write it is inspecting."""
    try:
        flags = _external_content_flags(text)
        if flags['score']:
            log_action(agent_id, 'library_injection_flag',
                       {'path': path, 'flags': flags['flags'],
                        'score': flags['score']}, authorized=authorized)
        return flags
    except Exception:
        return {'flags': [], 'score': 0}
