"""Pre-refinement comprehension (ask 2).

Before the refinement decider judges a filed work request,
the scrum master reads it and writes down what they
understood: who asked, what they asked for, the constraints
on it, and whether the request is ready to be carded or
needs clarification first. This module is that read-back
step: it turns a raw backlogRequests record into a
Comprehension, a short structured summary the refinement
ceremony can log and reason over.

The summarizer is deterministic and local by default (a
title plus first-sentence summary, constraint extraction
from the player's pins, readiness from the request's own
fields), so the sim needs no model call to comprehend its
own queue. The reader is injectable so a production wiring
can swap in a Jev-backed reader that rewrites the summary
in prose, exactly like _refinement_decider swaps in the
live Jev gate.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional

SUMMARY_MAX_CHARS = 140
MIN_REASON_CHARS = 24

READY = 'ready'
NEEDS_CLARIFICATION = 'needs-clarification'


@dataclass
class Comprehension:
    """What the facilitator understood about one filed work
    request."""
    request_id: str
    title: str
    summary: str
    requester: str
    room: str
    constraints: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    readiness: str = READY

    def to_dict(self):
        """State-safe form for a pendingRefinements record."""
        return {
            'requestId': self.request_id,
            'title': self.title,
            'summary': self.summary,
            'requester': self.requester,
            'room': self.room,
            'constraints': list(self.constraints),
            'gaps': list(self.gaps),
            'readiness': self.readiness,
        }


def _summarize(title, reason):
    """Title plus the first sentence of the reason, cut to
    SUMMARY_MAX_CHARS on a word boundary."""
    first = ' '.join((reason or '').split())
    dot = first.find('. ')
    if dot != -1:
        first = first[:dot + 1]
    text = f'{(title or "").strip()}: {first}'.strip()
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    cut = text[:SUMMARY_MAX_CHARS]
    while cut and cut[-1] not in ' ,;:/':
        cut = cut[:-1]
    return cut.rstrip(' ,;:/') + '...'


def _constraints(player_constraints):
    """The player's pins as readable constraint lines: the
    same fields _resolve_refinement weaves into the decider
    instructions."""
    pc = player_constraints if isinstance(player_constraints, dict) else {}
    pins = []
    if pc.get('budgetUsd'):
        pins.append(f"total budget ${pc['budgetUsd']:.2f}")
    if pc.get('constraints'):
        pins.append('prohibitions: ' + '; '.join(pc['constraints']))
    if pc.get('roles'):
        pins.append('preferred roles: ' + '; '.join(pc['roles']))
    if pc.get('mustInclude'):
        pins.append('must cover: ' + '; '.join(pc['mustInclude']))
    return pins


def _readiness(title, reason):
    """A request is ready when it names a title and a reason
    with enough substance to judge scope; anything thinner
    needs clarification before it can be carded. Returns
    (readiness, blocking gaps)."""
    if not (title or '').strip():
        return NEEDS_CLARIFICATION, ['title']
    if not (reason or '').strip():
        return NEEDS_CLARIFICATION, ['reason']
    if len(reason.strip()) < MIN_REASON_CHARS:
        return NEEDS_CLARIFICATION, [f'reason under {MIN_REASON_CHARS} chars']
    return READY, []


def comprehend(
    req,
    reader: Optional[Callable[[dict], Optional[dict]]] = None,
):
    """Read one backlogRequests record. Returns a
    Comprehension. `reader`, when given, is consulted for
    the summary and readiness first (a Jev-backed reader in
    production); every field it leaves out falls back to
    the deterministic read."""
    req = req if isinstance(req, dict) else {}
    title = (req.get('title') or '').strip()
    reason = req.get('reason') or ''
    readiness, gaps = _readiness(title, reason)
    summary = _summarize(title, reason)
    override = reader(req) if reader else None
    if isinstance(override, dict):
        summary_text = override.get('summary')
        if isinstance(summary_text, str) and summary_text.strip():
            summary = summary_text.strip()
        readiness_text = override.get('readiness')
        if isinstance(readiness_text, str) and readiness_text.strip():
            readiness = readiness_text.strip()
    if not (req.get('userStory') or '').strip():
        gaps.append('userStory')
    if not (req.get('acceptanceCriteria') or '').strip():
        gaps.append('acceptanceCriteria')
    return Comprehension(
        request_id=req.get('id') or '',
        title=title,
        summary=summary,
        requester=req.get('filedBy') or '',
        room=req.get('room') or '',
        constraints=_constraints(req.get('playerConstraints')),
        gaps=gaps,
        readiness=readiness,
    )


def comprehend_batch(
    reqs,
    reader: Optional[Callable[[dict], Optional[dict]]] = None,
):
    """Comprehend every record in a pending refinement set.
    Non-records and records without an id are skipped: the
    ceremony must not crash on a malformed entry."""
    result = {}
    for req in (reqs or []):
        if not isinstance(req, dict) or not req.get('id'):
            continue
        result[req['id']] = comprehend(req, reader=reader)
    return result


def needs_clarification(comp):
    """True when a comprehension says the request is not
    ready to be carded yet."""
    return comp.readiness != READY
