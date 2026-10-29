
# ---------------------------------------------------------------------------
# Entity links: resolving the think tank's own id grammar out of prose.
#
# The audit and calibration layers join ~250k rows of tape and action_log
# prose against structured entities (task refs, agent ids, room names), and
# every join site so far grew its own regex with its own subtle bug class --
# the grade-outcome ledger shipped with a digit-boundary check because
# task-123's grade must not be condemned by task-1234's reject. One shared
# extractor replaces those per-site regexes: same grammar, same boundary
# rule, tested once. v1 covers the three entity families the joins actually
# use; artifact paths were deliberately left out (they over-match prose).
# ---------------------------------------------------------------------------

_TASK_REF_RE = re.compile(r'(?:task|spike-task)-\d+')


def _entity_span_safe(text, start, end):
    """True when the [start, end) span sits on clean token boundaries:
    not glued to another digit (task-123 inside task-1234) and not glued
    to a letter (the 'task-9' inside 'subtask-91')."""
    if start > 0 and (text[start - 1].isdigit() or text[start - 1].isalnum()):
        return False
    if end < len(text) and (text[end].isdigit() or text[end].isalnum()):
        return False
    return True


def _extract_task_refs(text):
    """Every task/spike-task id mentioned in text, in order, with
    digit-boundary guards. Duplicates keep first-seen order."""
    if not text:
        return []
    seen, out = set(), []
    for m in _TASK_REF_RE.finditer(text):
        if not _entity_span_safe(text, m.start(), m.end()):
            continue
        if m.group(0) not in seen:
            seen.add(m.group(0))
            out.append(m.group(0))
    return out


def _extract_agent_refs(text, roster=None):
    """Agent ids whose display names appear as standalone words in text.
    `roster` maps name (lowercase) -> agent id; when absent (pure tape
    scans have no state handy) no agent extraction happens rather than a
    guessed one. Word-boundary matched and case-insensitive; a name that
    is also a common word ('may') still matches -- the roster is small and
    the callers treat this as a hint, not a verdict."""
    if not text or not roster:
        return []
    ids, out = set(), []
    for name, agent_id in roster.items():
        if not name or len(name) < 3:
            continue
        for m in re.finditer(r'(?<![A-Za-z0-9])' + re.escape(name) + r'(?![A-Za-z0-9])',
                             text, re.IGNORECASE):
            if agent_id not in ids:
                ids.add(agent_id)
                out.append(agent_id)
            break
    return out


def _extract_room_refs(text, rooms=None):
    """Room names appearing as standalone words. `rooms` defaults to the
    valued queue rooms -- the same set the queue machinery routes into
    (VALUED_QUEUE_ROOMS in the sim); it is mirrored here because the serve
    namespace never imports the sim and a lazy import would invert the
    dependency direction the codebase keeps (sim imports serve, never the
    reverse). If a room is ever added to the sim's set, update both."""
    if not text:
        return []
    rooms = rooms or frozenset(
        {'observatory', 'pressoffice', 'postoffice', 'bank',
         'weatherstation', 'library', 'media'})
    found, out = set(), []
    for room in sorted(rooms):
        if re.search(r'(?<![A-Za-z0-9])' + re.escape(room) + r'(?![A-Za-z0-9])',
                     text, re.IGNORECASE):
            if room not in found:
                found.add(room)
                out.append(room)
    return out


def _extract_entities(text, roster=None, rooms=None):
    """One call, all v1 families: {'tasks': [...], 'agents': [...],
    'rooms': [...]}. Callers use what they need; absent roster/rooms
    simply skip that family instead of guessing."""
    return {
        'tasks': _extract_task_refs(text),
        'agents': _extract_agent_refs(text, roster),
        'rooms': _extract_room_refs(text, rooms),
    }


def _entity_ref_present(ref, text):
    """The boundary-safe 'does this exact ref appear in this text' check the
    outcome joins need: task-1234's details must not count against
    task-123."""
    if not ref or not text:
        return False
    idx = text.find(ref)
    while idx != -1:
        if _entity_span_safe(text, idx, idx + len(ref)):
            return True
        idx = text.find(ref, idx + 1)
    return False
