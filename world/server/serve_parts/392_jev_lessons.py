# Jev lesson store (operator, 2026-11-22): institutional memory for the Jev
# safety gate's uncertain cases, closing a real loop that has been missing
# since the gate shipped. Today, when Jev is unsure about a URL or command,
# _director_second_opinion's escalation chain resolves it once -- and that
# human-quality verdict dies in action_log. The same pattern re-escalates
# forever. Laya runs this exact loop as continuous learning: a teacher
# (director chain / operator) resolves what the decision model does not
# know, the strand is stored, and recall resolves identical cases
# deterministically at zero LLM cost. Over time escalations should decline
# as coverage grows -- that decline is directly measurable and is the point.
#
# Generalization rules (deliberately asymmetric, matching the codebase's
# established deny/allow asymmetry):
#   - DENY generalizes by HOST -- a director who denied one page on
#     evil-cdn.com denied the host for this purpose-class. Rationale: one
#     unwilling page seals the host; re-asking for each page would poke the
#     operator/directors repeatedly for a class of request they already
#     refused. Host-level deny keys are recorded AS host-level.
#   - ALLOW keys are URL-EXACT -- a director approving one URL must not let
#     a different page on the same host through. This preserves the
#     redirect-laundering protection (the re-gate exists precisely because
#     a different page's content is unjudged). An allow then re-escalates
#     every other page of that host -- correct, not an oversight.
#   - EXECUTE lessons normalize WHITESPACE + prefix truncation: the
#     whitelisted read-only lane already covers trivial reads; lessons cover
#     gray-zone commands, so the key is a normalized prefix (first 160 chars
#     after whitespace collapse). For execute, denies generalize like host
#     denies (the command family, approximately), allows are prefix-exact.
#
# Everything is best-effort: a lesson write or read failure never breaks the
# gate. Readers fail open to "no lesson" (the existing gate behavior), the
# conflict path processes marks and reports. Governance writes never gate
# performance.

import json
import os
import re
import threading
import time
import urllib.parse

_JEV_LESSONS_LOCK = threading.Lock()
# Cell-routing slightness guard: a lesson's TTL is 24h; hits beyond that are
# ignored unless 'sticky' (operator-approved).
_JEV_LESSONS_TTL_S = 24 * 3600
_JEV_LESSONS_MAX = 2000          # bounded file, never grows without limit
# Read-decay: lessons the recall surface keeps confirming (hitCount rises)
# stay; unconfirmed entries age out on rewrite. A per-key conflicts tally
# marks entries whose later Jev evidence disagrees -- conflicted lessons are
# skipped by recall and surfaced to the operator instead of auto-resolved.

_URLISH_ACTIONS = frozenset({'browse', 'download', 'curl', 'fetch', 'open'})
_EXECUTE_ACTIONS = frozenset({'execute_classify', 'execute', 'run_shell'})


def _jev_lesson_norm(text):
    return re.sub(r'\s+', ' ', (text or '').strip())


def _jev_lesson_host(url):
    try:
        parsed = urllib.parse.urlparse(url if '://' in url else 'https://' + url)
        host = (parsed.hostname or '').lower().lstrip('.')
        return host or None
    except Exception:
        return None


def _jev_lesson_keys(action, target):
    """The lookup/record key set for one gate case. Returns (deny_key,
    allow_key): deny keys generalize (host for URL targets, first-token
    family for commands, whole normalized target otherwise), allow keys are
    exact. Both carry the action so a learned 'deny git push' can never
    block a browse of the same string."""
    target = _jev_lesson_norm(target)
    if not target:
        return None, None
    host = _jev_lesson_host(target)
    if action in _URLISH_ACTIONS and host:
        deny_key = f'host:{action}:{host}'
    elif action in _EXECUTE_ACTIONS:
        first = target.split(' ')[0].lower()
        deny_key = f'cmd:{action}:{first}'
    else:
        deny_key = f'text:{action}:{target[:160].lower()}'
    allow_key = f'exact:{action}:{target[:240]}'
    return deny_key, allow_key


def _jev_lessons_dir():
    """Where the lesson store lives. JEV_LESSONS_DIR overrides it -- the
    same pattern THINK_TANK_DB uses for test isolation: tests that exercise
    the real escalation chain TEACH (deny verdicts record lessons), and a
    shared on-disk store would let one test's lesson poison another's gate
    (observed under pytest-randomly: seed-dependent pass/fail). Set to a
    temp dir by tests; absent in production, so production uses the
    Library."""
    override = os.environ.get('JEV_LESSONS_DIR')
    return override or _safe_library_path('analysis')


def _jev_lessons_read():
    try:
        path = os.path.join(_jev_lessons_dir(), 'jev_lessons.json')
        if not os.path.isfile(path):
            return {}
        with open(path, encoding='utf-8') as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def _jev_lessons_write(store):
    try:
        base = _jev_lessons_dir()
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, 'jev_lessons.json')
        trimmed = dict(sorted(store.items(),
                              key=lambda kv: -(kv[1].get('hits') or 0))
                       [:_JEV_LESSONS_MAX])
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(trimmed, fh, indent=1)
    except Exception:
        pass


def _jev_lesson_lookup(action, target, verdict):
    """One recall, best-effort. verdict is the lesson polarity to recall:
    'deny' (host/family-general) or 'allow' (exact-only). Returns the
    lesson dict (bumping hits) or None. Conflicted lessons are skipped --
    a disagreement between the teacher and later Jev evidence is surfaced
    for operator review, never silently replayed."""
    deny_key, allow_key = _jev_lesson_keys(action, target)
    key = allow_key if verdict == 'allow' else deny_key
    if not key:
        return None
    try:
        with _JEV_LESSONS_LOCK:
            store = _jev_lessons_read()
            lesson = store.get(key)
            if not lesson or lesson.get('verdict') != verdict:
                return None
            if lesson.get('conflicts'):
                return None
            if time.time() - (lesson.get('created') or 0) > _JEV_LESSONS_TTL_S \
                    and not lesson.get('sticky'):
                return None
            lesson['hits'] = (lesson.get('hits') or 0) + 1
            lesson['lastHit'] = time.time()
            store[key] = lesson
            _jev_lessons_write(store)
            return lesson
    except Exception:
        return None


def _jev_lesson_record(action, target, verdict, source, note='', confidence=None):
    """Persist one teacher verdict as a lesson. An entry for the SAME key
    with a DIFFERENT verdict is a conflict: the newer teacher answer wins
    the polarity, the old one is not silently forgotten -- conflicts are
    tallied on the record so repeated flip-flopping surfaces to review."""
    deny_key, allow_key = _jev_lesson_keys(action, target)
    key = allow_key if verdict == 'allow' else deny_key
    if not key:
        return False
    try:
        with _JEV_LESSONS_LOCK:
            store = _jev_lessons_read()
            prior = store.get(key) or {}
            conflicts = prior.get('conflicts') or 0
            if prior and prior.get('verdict') != verdict:
                conflicts += 1
            store[key] = {
                'verdict': verdict,
                'action': action,
                'source': source,
                'note': _jev_lesson_norm(note)[:200],
                'confidence': confidence,
                'created': prior.get('created') or time.time(),
                'updated': time.time(),
                'hits': prior.get('hits') or 0,
                'conflicts': conflicts,
            }
            _jev_lessons_write(store)
            return True
    except Exception:
        return False


def _jev_lesson_mark_conflict(action, target, verdict, seen_decision):
    """Later Jev evidence disagrees with a stored lesson (e.g. Jev is
    CONFIDENTLY allowing a host the director once denied). The lesson stays
    skipped until the operator reviews it; the tally tells them it happened."""
    deny_key, allow_key = _jev_lesson_keys(action, target)
    key = allow_key if verdict == 'allow' else deny_key
    if not key:
        return
    try:
        with _JEV_LESSONS_LOCK:
            store = _jev_lessons_read()
            lesson = store.get(key)
            if not lesson:
                return
            lesson['conflicts'] = (lesson.get('conflicts') or 0) + 1
            lesson['lastConflict'] = time.time()
            lesson['lastConflictSeen'] = str(seen_decision)[:80]
            store[key] = lesson
            _jev_lessons_write(store)
    except Exception:
        pass
