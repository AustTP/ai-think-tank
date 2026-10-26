

# ---------------------------------------------------------------------------
# Backlog refinement (Cut 1). The scrum master -- a standing,
# director-designated per-team role -- is RESPONSIBLE for turning agent-filed
# work into real stories. Agents communicate what needs doing by FILING a
# work-request; a ceremony (`pendingRefinements[team_id]`) convenes the scrum
# master with the filing agents at the Command Center, and at resolve the Jev
# refinement decider grooms each request into a `queue_work` story (accept) or
# back to the requester (reject). Runs ungated like the Social -- the ceremony
# is the point even on a quiet think tank -- but no-ops when there are no pending
# requests or no scrum master to run it, so we never convene an empty meeting.
#
# Human boundaries preserved: ceremony stories enter the workQueue tagged
# source:'refinement' + groomedBy:<scrum-master> at normal priority; the
# existing /api/intent/sprint (director/human) surface and story-reject path
# are untouched, so a real stakeholder keeps full overriding authority.
# ---------------------------------------------------------------------------

# Desk-tested cadence stamps: a stale TEST sentinel far in the future (e.g.
# 1e18) must not permanently silence the ceremony -- same rule as the skill-
# review sweep and the Social (handled inside _refinement_cadence_due_for).

def _pending_work_requests(state):
    """Pending (not-yet-groomed) work-requests a refinement ceremony will read."""
    return [r for r in (state.get('backlogRequests') or []) if r.get('status') == 'pending']


def file_work_request(state, agent_id, title, room, reason=None):
    """An on-duty agent communicates "this work still needs doing" to the scrum
    master by filing a work-request. Pure intake -- never decides, never queues
    a story by itself; a ceremony grooms it. Dedups a near-identical pending
    request so churn can't spam the backlog. Returns the new request dict, or
    None if a matching pending one already exists / the request is malformed."""
    room = (room or '').strip()
    title = (title or '').strip()
    if not room or not title or room not in VALUED_QUEUE_ROOMS:
        return None
    for r in (state.get('backlogRequests') or []):
        if (r.get('status') == 'pending' and r.get('room') == room
                and r.get('title') and title.lower() == r['title'].lower()):
            return None
    req = {
        'id': f"wrq-{len(state.get('backlogRequests') or []) + 1}",
        'filedBy': agent_id, 'title': title, 'room': room,
        'reason': (reason or 'Pending follow-up in a thinning room').strip(),
        'filedAt': int(time.time() * 1000), 'status': 'pending',
    }
    state.setdefault('backlogRequests', []).append(req)
    return req


def file_large_request(state, authority_id, goal, team_id, now_ms=None, constraints=None):
    """File a staffable large ask as a pending BREAKDOWN request for a team's
    breakdown ceremony to card into stories/spikes. The ask is attributed to
    the free authority who chose the team and tagged with the receiving team
    so the breakdown ceremony convenes the RIGHT team's scrum master + workers.
    Never predicts subtasks up front -- the ceremony does that. Returns the new
    request dict, or None if malformed.

    `constraints` is an OPTIONAL dict of player-pinned guidance the filing
    path may attach when the player chooses to steer (all keys optional, all
    absent = the ceremony plans freely, which is the default):
      budgetUsd (number)    -- a total model-spend ceiling the player named
                               for the whole ask. Passed to the breakdown as
                               CONTEXT so the scrum master allocates it across
                               cards with judgment, not a mechanical split.
      constraints (list)    -- hard "never" rules, as context for the team.
      roles (list)          -- preferred skills/roles, as context.
      order (list)          -- sequencing hints, as context.
      mustInclude (list)    -- concrete tasks the player wants covered.
    These are CONTEXT for the agents' decisions, not mechanical overrides --
    the scrum master/director weighs them like any other signal. A malformed
    constraints dict is ignored (the ask still files -- a bad pin never blocks
    the work)."""
    goal = (goal or '').strip()
    if not goal or not team_id:
        return None
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    req = {
        'id': f"wrq-{len(state.get('backlogRequests') or []) + 1}",
        'filedBy': authority_id, 'title': goal[:120], 'room': 'pressoffice',
        'reason': f"Large request: {goal[:200]}", 'goal': goal,
        'filedAt': now_ms, 'status': 'pending', 'teamId': team_id,
        'origin': 'large_request', 'breakdown': True,
    }
    if isinstance(constraints, dict) and constraints:
        pinned = {}
        budget = constraints.get('budgetUsd')
        if isinstance(budget, (int, float)) and budget > 0:
            pinned['budgetUsd'] = float(budget)
        for key in ('constraints', 'roles', 'order', 'mustInclude'):
            val = constraints.get(key)
            if isinstance(val, (list, tuple)):
                cleaned = [str(v).strip()[:200] for v in val if str(v).strip()]
                if cleaned:
                    pinned[key] = cleaned[:20]
        if pinned:
            req['playerConstraints'] = pinned
    state.setdefault('backlogRequests', []).append(req)
    return req


# ---------------------------------------------------------------------------
# JIRA-like issue register. A `state['issues']` dict keyed by a human-readable
# per-team key (TEAM-0128) with a monotonic per-team counter in
# `state['issueCounters']`, mirroring next_sprint_id/next_product_id so a cold
# state starts at 1 and a hot one never collides. Filing an issue ALSO writes a
# backlogRequests record tagged with its issueKey + teamId, routing it into the
# owning team's scrum-master refinement ceremony (the think tank's proven path for
# turning a card into worked agent labor) -- it is not a dead ledger.
# ---------------------------------------------------------------------------

ISSUE_TYPES = ('story', 'spike', 'bug', 'task', 'meeting')
# What the card must actually produce. The completion gate reads this instead
# of guessing from the type or title: a bug that is really a missing report is
# minted as a spike with deliverable='report', and a code bug as 'code'.
ISSUE_DELIVERABLES = ('code', 'report', 'note', 'meeting', 'supervision', 'review')
_DELIVERABLE_BY_ISSUE_TYPE = {
    'meeting': 'meeting',
    'spike': 'report',
    'bug': 'code',
    'story': 'code',
    'task': 'code',
}
# `blocked` is a boolean FIELD on an issue (issue['blocked'], default False), NOT
# a status. Only the owning team's scrum master commits it (via _file_block_change
# + _block_step), so it stays a single player-facing "this card can't be or needn't
# be worked right now" flag. It is EARNED two ways -- requirements-met (supervisor-
# approved) or stuck-on-player (director-approved ask) -- and CLEARED two ways --
# the player responded or the agent self-resolved. Distinct from `status`, which
# tracks the work lifecycle (open/in_progress/done/closed).
ISSUE_STATUSES = ('open', 'in_progress', 'done', 'closed')


def _norm_issue_title(text):
    """Case- and whitespace-insensitive title key for the file_issue same-title
    guard. Exact match only, on purpose: two agents describing the same gap in
    different words stay two cards -- only literal re-filings of the same
    headline ever get the duplicate link."""
    return ' '.join((text or '').split()).casefold()

# Canonical Jira description blocks. A description, when supplied, must follow
# one of the two templates the think tank understands:
#   userStory          "As a <role>, I want to <action>, so that <value>"
#   acceptanceCriteria "Given <context>, When <event>, Then <outcome>"
_STORY_PARTICLES = ('as a', 'i want to', 'so that')
_GWT_PARTICLES = ('given', 'when', 'then')


def _blocks_cover(text, particles, delim=',', start=0):
    """True if every particle in `particles` appears, in order, in `text`."""
    pos = start
    for p in particles:
        i = text.lower().find(p, pos)
        if i < 0:
            return False
        pos = i + len(p)
    return True


def normalize_story_block(text):
    """Normalize a user-story block ("As a..., I want to..., so that...") to a
    canonical joined string, or None if it doesn't contain all three markers."""
    text = (text or '').strip()
    if not text:
        return None
    if not _blocks_cover(text, _STORY_PARTICLES):
        return None
    low = text.lower()
    # Rebuild as "As a <role>, I want to <action>, so that <value>".
    parts = []
    idx_as = low.find('as a')
    idx_iwant = low.find('i want to')
    idx_sothat = low.find('so that')
    parts.append(text[idx_as:idx_iwant].strip())
    parts.append(text[idx_iwant:idx_sothat].strip())
    parts.append(text[idx_sothat:].strip())
    return ', '.join(p.strip().rstrip(',') for p in parts)


def normalize_gwt_block(text):
    """Normalize a Given/When/Then block to a canonical joined string, or None
    if it doesn't contain all three markers."""
    text = (text or '').strip()
    if not text:
        return None
    if not _blocks_cover(text, _GWT_PARTICLES):
        return None
    low = text.lower()
    idx_g = low.find('given')
    idx_w = low.find('when')
    idx_t = low.find('then')
    parts = [text[idx_g:idx_w].strip(),
             text[idx_w:idx_t].strip(),
             text[idx_t:].strip()]
    return ', '.join(p.strip().rstrip(',') for p in parts)


_NEVER_MARKER_RE = re.compile(
    r'^(never|do not|must not|constraints|don.t|under no circumstances)\s*[:.]?\s*$',
    re.IGNORECASE)


def normalize_never_block(value):
    """Normalize a NEVER / constraints block to a canonical bullet list, or
    None when empty. Accepts a list of prohibition strings or a text block.
    Leading marker lines (NEVER:/DO NOT:/CONSTRAINTS:...) and bullet prefixes
    are stripped; empty lines dropped. Content is kept verbatim -- the writer
    owns the wording, and over-validating would silently delete a real
    constraint, the exact failure this field exists to prevent."""
    if isinstance(value, list):
        lines = [str(v) for v in value]
    else:
        lines = (value or '').split('\n')
    out = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if _NEVER_MARKER_RE.match(line):
            continue
        line = re.sub(r'^[-*\d.\s]+', '', line).strip()
        if not line:
            continue
        out.append(line)
    if not out:
        return None
    return '\n'.join(f'- {l}' for l in out)


def _split_never_section(text):
    """Split a plain-string card description into (body, never_text) at the
    first NEVER:/DO NOT:/CONSTRAINTS: marker line. The body keeps everything
    before the marker (the user story / GWT block); never_text is everything
    from the marker on. Returns (text, None) when no marker is present."""
    lines = (text or '').split('\n')
    for i, raw in enumerate(lines):
        if _NEVER_MARKER_RE.match(raw.strip()):
            return '\n'.join(lines[:i]).strip(), '\n'.join(lines[i:]).strip()
    return text, None
def _default_team_prefix(team_id):
    """A deterministic default prefix from a team id slug (dev -> DEV). Only
    auto-derived; an explicit `prefix` on the team record always wins."""
    slug = (team_id or 'TEAM').upper()
    cleaned = ''.join(ch for ch in slug if ch.isalnum())
    return (cleaned or 'TEAM')[:5]


def team_prefix(state, team_id):
    t = _team_row(state, team_id)
    if t and t.get('prefix'):
        prefix = ''.join(ch for ch in str(t['prefix']).upper() if ch.isalnum())
        if prefix:
            return prefix[:5]
    return _default_team_prefix(team_id)


def set_team_prefix(state, team_id, prefix):
    """Set/clear an explicit issue prefix on a team record. Returns the applied
    prefix, or None if the team is unknown or the supplied prefix is invalid
    (must be 1-5 alnum; empty clears back to the derived default)."""
    t = _team_row(state, team_id)
    if not t:
        return None
    prefix = (prefix or '').strip().upper()
    cleaned = ''.join(ch for ch in prefix if ch.isalnum())
    if cleaned and len(cleaned) > 5:
        return None
    t['prefix'] = cleaned or None
    return team_prefix(state, team_id)


def next_issue_key(state, team_id):
    """Server-side monotonic per-team issue key (DEV-1, DEV-2, ...), keyed off
    the team's prefix + counter so a cold state starts at 1 and a hot one never
    collides. No leading zero padding (the player asked for DEV-1, not DEV-0001)."""
    prefix = team_prefix(state, team_id)
    counters = state.setdefault('issueCounters', {})
    n = counters.get(prefix, 0) + 1
    counters[prefix] = n
    return f"{prefix}-{n}"


def _as_block_text(value):
    """Coerce a description block (string, or list of strings as sent by a JSON
    client for acceptanceCriteria) into a single text blob for normalization."""
    if isinstance(value, list):
        return '\n'.join(str(item) for item in value if item is not None)
    return value


def _normalize_description(description):
    """A description dict {userStory, acceptanceCriteria, never} or a plain
    string is split into the three canonical description blocks. Returns
    (userStory, acceptanceCriteria, never) -- each normalized to its template,
    or None when absent/malformed. The think tank understands exactly three
    description shapes: the user story ("As a..., I want to..., so that..."),
    the acceptance criteria ("Given..., When..., Then..."), and the NEVER
    constraints block (hard prohibitions that ride every card onto the
    worker's instructions). `acceptanceCriteria` and `never` may arrive as
    lists of strings (the natural JSON shape); the list is joined into one
    block before normalization."""
    if isinstance(description, dict):
        return (normalize_story_block(_as_block_text(description.get('userStory'))),
                normalize_gwt_block(_as_block_text(description.get('acceptanceCriteria'))),
                normalize_never_block(description.get('never')))
    text = (description or '').strip()
    if not text:
        return None, None, None
    body, never_text = _split_never_section(text)
    never = normalize_never_block(never_text)
    if _groups_look_like_gwt(body):
        return None, normalize_gwt_block(body), never
    return normalize_story_block(body), None, never


def _groups_look_like_gwt(text):
    """Auto-detect a stray plain-string description as Given/When/Then when it
    carries all three markers (else it's treated as a raw user-story block)."""
    low = text.lower()
    return (low.find('given') < low.find('when') < low.find('then')
            and -1 < low.find('given'))


def file_issue(state, team_id, issue_type, summary, feature, reporter_id,
               description='', story_points=None, title=None, now_ms=None,
               assigned_to=None, lane=None, attendees=None,
               facilitator_id=None, approved_tools=None, deliverable=None):
    """File a JIRA-style issue for team_id. Returns the issue record dict, or
    None if a required field is missing (team_id / issue_type / summary /
    feature / reporter_id), the issue type is unknown, or the team is unknown.
    `feature` is the Jira project/area the work belongs to (required).
    `description` is optional but structured when present: pass a dict
    `{'userStory': ..., 'acceptanceCriteria': ..., 'never': [...]}` or a plain
    string (each block, when given, is normalized to "As a..., I want to...,
    so that..." / "Given..., When..., Then..." / a "NEVER:" bullet list and
    dropped if it doesn't follow its template). `never` carries hard
    prohibitions that survive file_issue -> refinement -> queue -> task so the
    worker's instructions always include them.
    `title` is an OPTIONAL one-line headline kept DISTINCT from `summary` so an
    agent scanning a long list can grasp the card without reading the whole
    description/criteria; defaults to `summary` when omitted.
    Human-in-the-loop: `assigned_to='player'` marks the issue as the PLAYER's
    to do -- the ownership survives file_issue -> refinement -> queue, and the
    card is handed to the player (not an agent) at assignment. An agent (the
    scrum master filing the issue) uses this to hand a story/spike to the
    player when the next step genuinely needs human hands.
    MEETING cards: `attendees` is a list of roster ids or a scope string
    ('team'/'all'/'pair' -- resolved to concrete ids at grooming, see
    _resolve_meeting_attendees); `facilitator_id` is the agent who
    convenes the meeting and writes the minutes. Both survive
    file_issue -> refinement -> queue -> task (queue_work whitelists
    them), so the meeting executor can name the participants.
    `deliverable` is what the card must produce: 'code' (a landed change),
    'report' (a saved report/Library artifact), 'note' (recorded findings), or
    the non-gated 'meeting'/'supervision'/'review'. It defaults from the issue
    type and travels file_issue -> refinement -> queue -> task, so the
    completion gate reads the explicit contract instead of guessing by title.
    Duplicate stamp: an exact-title (case- and whitespace-insensitive) match
    against an OPEN issue in the same team sets `duplicateOf` on the new card;
    filing still succeeds -- rejection is the scrum master's call, not the
    filing API's.
    Pure: mutates state['issues'], state['issueCounters'], and appends a
    backlogRequests record (tagged issueKey + teamId) so the owning scrum
    master can groom the card into a sprint -- the think tank does the work."""
    team_id = (team_id or '').strip() if isinstance(team_id, str) else team_id
    issue_type = (issue_type or '').strip().lower()
    summary = (summary or '').strip()
    feature = (feature or '').strip()
    reporter_id = (reporter_id or '').strip()
    if not team_id or not issue_type or not summary or not feature or not reporter_id:
        return None
    if issue_type not in ISSUE_TYPES:
        return None
    if not _team_row(state, team_id):
        return None
    approved_tools = [str(t) for t in (approved_tools or []) if str(t).strip()]
    deliverable = (deliverable or '').strip().lower() or _DELIVERABLE_BY_ISSUE_TYPE.get(issue_type, 'code')
    if deliverable not in ISSUE_DELIVERABLES:
        return None
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    key = next_issue_key(state, team_id)
    story, criteria, never = _normalize_description(description)
    title = (title or '').strip() or summary
    # Same-title guard: an exact title match against an OPEN card in the same
    # team is stamped as a duplicate rather than rejected, so a second agent
    # re-reporting the same gap never silently vanishes -- the record stays,
    # the scrum master sees why, and a decline can name the real key instead
    # of judging from scratch. Cross-team same-title cards are different
    # queues and stay unlinked; closed originals do not suppress new cards.
    duplicate_of = None
    req_title = _norm_issue_title(title)
    for existing in (state.get('issues') or {}).values():
        if (existing.get('teamId') != team_id
                or (existing.get('status') or 'open') not in ('open', 'in_progress')):
            continue
        if _norm_issue_title(existing.get('title') or existing.get('summary')) == req_title:
            duplicate_of = existing.get('key')
            break
    issue = {
        'key': key,
        'teamId': team_id,
        'teamKey': team_prefix(state, team_id),
        'type': issue_type,
        'deliverable': deliverable,
        'summary': summary,
        'title': title,  # one-line scan headline; distinct from summary/description
        'feature': feature,
        'userStory': story,
        'acceptanceCriteria': criteria,
        'never': never,
        'storyPoints': story_points,
        'reporterId': reporter_id,
        'createdAt': now_ms,
        'status': 'open',
        'blocked': False,  # SM-committed boolean field -- see _file_block_change
        'blockedAt': None,
        'blockedBy': None,
        'duplicateOf': duplicate_of,  # same-title open card in this team, or None
        'assignedTo': assigned_to or None,  # 'player' = the human does this one
        # Explicit per-task commit-tool grants (e.g. self_edit and
        # schedule_dormant_apply for a governed change request). A spike is not
        # a trusted code pipeline, so it needs this to use commit tools.
        'approvedTools': approved_tools,
        'lane': normalize_lane(lane),       # attention lane (build/reading/open/parking-lot)
        # MEETING provenance: the attendee spec (roster ids or a
        # 'team'/'all'/'pair' scope, resolved at grooming) and the
        # facilitator who convenes the meeting. Rides the issue ->
        # backlogRequests -> refinement -> queue -> task round trip.
        'attendees': _normalize_attendees(attendees),
        'facilitatorId': (facilitator_id or '').strip() or None,
    }
    state.setdefault('issues', {})[key] = issue
    # Feed the backlog pipe: a backlogRequests record the refinement ceremony
    # grooms. Tags teamId explicitly so a player-filed issue (no director to
    # derive a team from) still routes to the owning team's scrum master.
    req = {
        'id': f"iss-{key}",
        'filedBy': reporter_id,
        'title': f"[{issue_type.upper()}] {summary}",
        # The room is where the work happens, not a scope gate. JIRA cards
        # default to pressoffice for every type, including spikes and research;
        # the refinement prompt now judges the card on readiness and value
        # instead of treating pressoffice as code-only.
        'room': 'pressoffice',
        # Comprehension reads this as the request's rationale: name the card
        # and its intent, not just the key, so a well-specified issue is not
        # read back as 'needs-clarification' for a stub reason.
        'reason': f"JIRA issue {key}: {summary[:200]}",
        'filedAt': now_ms,
        'status': 'pending',
        'origin': 'jira_issue',
        'issueKey': key,
        'teamId': team_id,
        'issueType': issue_type,
        'deliverable': deliverable,
        # Human-in-the-loop: a card filed for the PLAYER stays the player's
        # through refinement (assignedTo survives onto the queued item, and
        # _assign_due_item hands it to the player -- see _resolve_refinement).
        'assignedTo': assigned_to or None,
        'approvedTools': approved_tools,
        # The attention lane the filed card rides -- survives file_issue ->
        # refinement -> queue so a parked or build-lane card keeps its lane
        # (see _resolve_refinement, which forwards req['lane'] onto the queue).
        'lane': normalize_lane(lane),
        # The player-facing contract: the normalized user story + acceptance
        # criteria travel with the backlog request so the refinement ceremony can
        # hand the WORKER the full spec, not just the one-line summary (see
        # _resolve_refinement, which embeds them into the coding task's
        # instructions).
        'userStory': story,
        'acceptanceCriteria': criteria,
        'never': never,
        # MEETING provenance (same round trip as the issue record
        # above): the attendee spec + facilitator the scrum master's
        # grooming ceremony resolves into a concrete queue item (see
        # _resolve_refinement's meeting branch).
        'attendees': _normalize_attendees(attendees),
        'facilitatorId': (facilitator_id or '').strip() or None,
    }
    state.setdefault('backlogRequests', []).append(req)
    # Kick this team's refinement to be immediately due (next pass), not
    # weekly -- a freshly-filed JIRA issue shouldn't wait out the cadence
    # stamp a previous grooming just set. No-op if the team already has an
    # in-flight ceremony (it will groom this card) or its cadence is due.
    kick_refinement_now(state, team_id, now_ms)
    return issue


def list_issues(state, team_id=None):
    """All issues, newest first, optionally filtered to one team (by id).
    Returns a list of issue records in insertion order."""
    issues = (state.get('issues') or {}).values()
    out = list(issues)
    if team_id:
        out = [i for i in out if i.get('teamId') == team_id]
    out.sort(key=lambda i: i.get('createdAt', 0), reverse=True)
    # Defensively surface fields on pre-existing rows (older issues filed before
    # the field existed won't carry it yet).
    for i in out:
        i.setdefault('blocked', False)
        i.setdefault('title', i.get('summary'))
    return out


def set_issue_status(state, key, status):
    """Transition an issue's status (open/in_progress/done/closed) and mirror it
    onto any linked pending backlog request so the refinement pipe stays in
    sync. Terminal states (`done`, `closed`) release the worker by resolving the
    linked request and clear any pending player-input ask (a finished card
    shouldn't keep an agent waiting). Returns the updated issue, or None if
    unknown/invalid. `blocked` is NOT a status here -- it's a boolean field the
    scrum master commits separately (see _file_block_change / _block_step)."""
    status = (status or '').strip().lower()
    if status not in ISSUE_STATUSES:
        return None
    issue = (state.get('issues') or {}).get(key)
    if not issue:
        return None
    previous = issue.get('status')
    issue['status'] = status
    # A terminal status supersedes a pending request to PLAYER input: the agent
    # was waiting on you, but the card is now finished/closed -- stop waiting and
    # drop any outstanding block-change intended to gate it.
    if status in ('done', 'closed') and issue.get('needsInput'):
        issue['needsInput'] = False
        for m in (state.get('playerInbox') or []):
            if m.get('issueKey') == key and m.get('status') == 'awaiting_input':
                m['status'] = 'superseded'
    for r in (state.get('backlogRequests') or []):
        # Mirror onto the linked request whether it's still pending or already
        # accepted (an in_progress -> done hop shouldn't strand it at accepted).
        if r.get('issueKey') == key and r.get('status') in ('pending', 'accepted'):
            if status in ('done', 'closed'):
                r['status'] = 'resolved'
            elif status == 'in_progress':
                r['status'] = 'accepted'
    # BP-62: the owner side changed the card's state -- the reporter learns it
    # through one quiet, no-wake mailbox note (skip no-change transitions).
    mail_kind = _CARD_STATUS_MAIL_KINDS.get(status)
    if mail_kind and previous != status:
        team_name = (_team_row(state, issue.get('teamId')) or {}).get('name')
        _mail_card_update(state, issue.get('reporterId'), mail_kind, key,
                          issue.get('title'), team_name)
    return issue


# ---------------------------------------------------------------------------
# Director-gated player escalation. When an agent decides a task genuinely can't
# proceed without the player, it does NOT message you directly -- it files a
# needs-input request that the owning team's DIRECTOR reviews first (the "do
# they really need my input?" gate). Only if the director returns an `ask_player`
# verdict does a player-inbox message go out; a `resolve_internally` verdict
# drops the block and the agent resumes. An agent blocked on the player carries
# a `_awaitingPlayer` flag so the sim stops assigning it work (WAIT, not
# abandon) but keeps it visible/persisted; a terminal `blocked` issue status
# supersedes the ask and frees the worker. All pure/mutate-state, mirroring the
# escalation-groom ceremony's injectable-decider shape.
# ---------------------------------------------------------------------------
PLAYER_ASK_VALUES = ('ask_player', 'resolve_internally')


def _issue_owning_team(state, issue_key):
    """The team record owning issue_key (by the issue's stored teamId), or None."""
    issue = (state.get('issues') or {}).get(issue_key)
    if not issue:
        return None
    return _team_row(state, issue.get('teamId'))


def _issue_director(state, issue_key):
    """The director id of the team owning issue_key, or None."""
    t = _issue_owning_team(state, issue_key)
    return t.get('directorId') if t else None


PENDING_PLAYER_ASK_BACKLOG_MAX = 20


def request_player_input(state, issue_key, agent_id, question, context=None,
                         now_ms=None):
    """Record that an agent wants the player's input on issue_key, but ONLY
    surface it after the director gate. The first issue occupies
    `_pendingPlayerAsk`; later issues wait in `_pendingPlayerAskBacklog` and
    are promoted in order when the current ask resolves. Nothing is silently
    overwritten. The backlog is bounded, and an overflow is logged as a
    dropped ask. Returns the awaited inbox message id once the gate has run,
    else None (gate pending / issue unknown / issue already needsInput).
    Mutates state only -- the director sweep resolves the pending ask."""
    if _issue_director(state, issue_key) is None:
        return None
    issue = (state.get('issues') or {}).get(issue_key)
    if not issue:
        return None
    if issue.get('needsInput'):
        return None  # already awaiting input -- don't double-ask
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    ask = {
        'issueKey': issue_key,
        'agentId': agent_id,
        'question': (question or '').strip(),
        'context': (context or ''),
        'at': now_ms,
        'directed': False,
    }
    pending = state.get('_pendingPlayerAsk') or {}
    if pending.get('issueKey') == issue_key:
        return None
    backlog = state.setdefault('_pendingPlayerAskBacklog', [])
    if any(item.get('issueKey') == issue_key for item in backlog):
        return None
    if not pending.get('issueKey'):
        state['_pendingPlayerAsk'] = ask
        return None
    backlog.append(ask)
    if len(backlog) > PENDING_PLAYER_ASK_BACKLOG_MAX:
        dropped = backlog.pop(0)
        try:
            from serve import log_action
            log_action(None, 'player_ask_dropped',
                       {'issueKey': dropped.get('issueKey'),
                        'agentId': dropped.get('agentId'),
                        'reason': 'backlog_cap'}, authorized=False)
        except Exception:
            pass
    return None


def _director_gate_due_for(state, issue_key, now_ms, gate_ms=8000):
    """True when issue_key has an undirected pending player-ask old enough for
    the director to consider (a short horizon -- the gate is a judgment, not a
    required wait)."""
    pend = (state.get('_pendingPlayerAsk') or {})
    if pend.get('issueKey') != issue_key or pend.get('directed'):
        return False
    return (now_ms - (pend.get('at') or 0)) >= gate_ms


def request_player_input_verdict(state, issue_key, decider=None):
    """Resolve a pending player-ask via the owning team's director (injectable
    decider, mirroring the escalation groom). Verdict `ask_player` -> deliver
    the player inbox message and block the agent; `resolve_internally` -> drop
    the block, the agent resumes. Returns the delivered inbox message id on
    ask_player, 'internal' on resolve_internally, or None if nothing is due."""
    pend = (state.get('_pendingPlayerAsk') or {})
    if pend.get('issueKey') != issue_key:
        return None
    issue = (state.get('issues') or {}).get(issue_key)
    director_id = _issue_director(state, issue_key)
    if not issue or not director_id:
        return None
    agent_id = pend.get('agentId')
    question = pend.get('question') or f"Can the player unblock work on {issue_key}?"
    decider = decider or _player_ask_decider_default
    verdict = decider(state, issue_key, director_id, question, pend.get('context'))
    del state['_pendingPlayerAsk']
    backlog = state.get('_pendingPlayerAskBacklog') or []
    if backlog:
        state['_pendingPlayerAsk'] = backlog.pop(0)
        state['_pendingPlayerAskBacklog'] = backlog
    if verdict != 'ask_player':
        _unblock_agent(state, agent_id)
        _log_governance(state, director_id or agent_id, 'player_ask',
                        {'action': 'resolve_internally', 'issue': issue_key})
        # Kept the card unblocked (the agent recovered internally): queue the SM's
        # blocked=false so the single field never falsely stays blocked.
        _file_block_change(state, issue_key, False, agent_id,
                           'unblock_resolved', 'agent recovered without the player')
        return 'internal'
    mid = _deliver_player_ask(state, issue_key, agent_id, question, pend.get('context'))
    if mid:
        # The card is now blocked pending the player's answer: queue the SM's
        # blocked=true so the single field reflects "waiting on the player".
        _file_block_change(state, issue_key, True, agent_id,
                           'stuck_player', question)
    return mid


def _player_ask_decider_default(state, issue_key, director_id, question, context):
    """Default director-gate resolver: Jev judges whether the agent is truly
    blocked in front of the player (ask_player) or can resume with internal
    recovery (resolve_internally). Returns 'ask_player' | 'resolve_internally',
    or 'resolve_internally' on a resolver outage (fail safe toward NOT pestering
    the player)."""
    import serve  # noqa
    try:
        data = serve._call_openrouter_decision_sync(
            serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice', 'instructions': (
                f"{director_id} is reviewing whether agent work on {issue_key} "
                f"really needs the player. The agent asked: '{question}'. "
                f"Context: {context or ''}. Choose ask_player ONLY if the work "
                f"is genuinely blocked with no internal recovery possible."),
                'criteria': {
                    'ask_player': "The work is truly stuck and only the player can unblock it.",
                    'resolve_internally': "The agent can resume with existing information or internal recovery."}}})
        choice = serve._jev_choice(data)[0] if serve._jev_choice(data) else None
        if choice in PLAYER_ASK_VALUES:
            return choice
    except Exception:
        pass
    return 'resolve_internally'


# ---------------------------------------------------------------------------
# Real-email outbox. sim.py stays pure -- it only QUEUES a
# notification into state['emailOutbox']; serve.py drains the outbox each loop
# pass and sends via SMTP (vault-backed Gmail app-password). This keeps network
# + a real external credential out of the pure sim. Deduped per-kind so a burst
# of cards blocking at once sends one combined player nudge, not a flood.
# ---------------------------------------------------------------------------
_EMAIL_DEDUP_MS = 30_000


def _queue_player_email(state, kind, subject, body_text, now_ms=None,
                        channels=None):
    """Queue a one-way player notification for the serve loop to email out.
    Pure mutation of state['emailOutbox']. Skips a duplicate of the same `kind`
    within _EMAIL_DEDUP_MS of the last one. `channels` optionally restricts
    delivery (['email'] keeps a chatty kind off Telegram); the default is both
    channels. Returns the outbox entry or None."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    # Successful spikes already leave a Library report. The player asked not to
    # get a push for every completed spike; failures, pauses, and budget stops
    # still use the same kind and still notify.
    if kind == 'spike_done' and str(subject or '').startswith('[AI Think Tank] Spike done:'):
        return None
    outbox = state.setdefault('emailOutbox', [])
    last = state.get('_lastEmailAt') or {}
    if now_ms - (last.get(kind) or 0) < _EMAIL_DEDUP_MS:
        return None
    last[kind] = now_ms
    state['_lastEmailAt'] = last
    entry = {'kind': kind, 'subject': subject, 'body': body_text,
             'queuedAt': now_ms}
    if channels is not None:
        entry['channels'] = list(channels)
    outbox.append(entry)
    return entry


_CARD_FAILURE_DEDUP_MS = 30 * 60 * 1000


def _notify_card_failure(state, task, reason, category='failed', now_ms=None):
    """One Telegram notice per failing card, with the reason and card identity.

    Per-task retries mint new task ids, so the dedup key is the issue key, or
    the room+title for unkeyed cards. The notice is Telegram-only by request;
    successful cards never call this path.
    """
    if not isinstance(task, dict):
        return None
    task_id = task.get('id') or task.get('taskId')
    title = (task.get('title') or task_id or 'untitled card').strip()
    issue = (task.get('issueKey') or '').strip()
    key = issue or f"{task.get('room') or ''}|{title}"
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    seen = state.setdefault('_cardFailureNotified', {})
    if now_ms - float(seen.get(key) or 0) < _CARD_FAILURE_DEDUP_MS:
        return None
    seen[key] = now_ms
    # A spike that already queued its own specific notice (done/stalled/paused)
    # owns the message; do not add a second card-failure copy for the same task.
    if task.get('_notifyQueued'):
        return None
    text = str(reason or 'the card did not complete').strip()
    try:
        from serve import _redact_secrets
        text = _redact_secrets(text)
    except Exception:
        pass
    detail = [
        f'Card: {title}',
        f'Type: {task.get("taskType") or "task"}',
        f'Room: {task.get("room") or "unknown"}',
    ]
    if issue:
        detail.append(f'Issue: {issue}')
    if task.get('assignedTo'):
        detail.append(f'Assigned: {task["assignedTo"]}')
    detail.extend([f'Category: {category}', f'Why: {text[:900]}'])
    if category == 'abandoned':
        detail.append('The card was removed from the queue after repeated assignment failures.')
    elif category == 'run-abandoned':
        detail.append('The content run stopped reporting liveness before it produced a result.')
    elif category == 'auto-closed':
        detail.append('The card was closed without completing because the pipeline kept failing.')
    elif category == 'refused':
        detail.append('Completion was refused because the saved deliverable was missing.')
    else:
        detail.append('The card was sent back or remains available for another attempt.')
    return _queue_player_email(
        state, f'card_failure:{key}',
        f'[AI Think Tank] Card {category}: {title[:120]}',
        '\n'.join(detail), now_ms=now_ms, channels=('telegram',))


def _deliver_card_report(state, task, close_reason, approvals=0):
    """No-op kept for call-site compatibility. A closed card's report
    (story/spike/bug/task) no longer reaches the player on ANY
    channel -- not the player inbox, not email, not Telegram: the
    player asked for the sprint retrospective to be the close signal
    (_resolve_retrospective queues the sprint_retro push with the
    team's START/STOP/CONTINUE feedback). Both the peer-gated story
    close (_close_gated_story) and the universal close (finish_task)
    still call this; the task's `_reportDelivered` flag keeps the
    once-per-card contract should delivery ever be re-enabled.
    Agent-to-agent internals never fired this either: a review/fix
    subtask (reviewOf) reports through its PARENT, a shadow (dry-run)
    card ships nothing, and a player-assigned card was the player's
    own hands."""
    if not task or task.get('_reportDelivered'):
        return
    if task.get('reviewOf') or task.get('shadow') or task.get('assignedTo') == 'player':
        return
    task['_reportDelivered'] = True


def _drain_email_outbox_sync(state):
    """Best-effort synchronous send of every queued player notification on
    this state's outbox, on BOTH channels (was email-only --
    the think tank had no proactive push at all before this, only a reactive
    reply when the player texted in first). Clears the outbox regardless so
    a permanent SMTP/Telegram failure doesn't wedge the queue. Calls into
    serve lazily (side-effect), returns a list of (kind, emailOk, telegramOk)
    results. Pure callers never hit the network."""
    outbox = state.get('emailOutbox') or []
    if not outbox:
        return []
    results = []
    try:
        from serve import send_player_email_sync, send_player_telegram_sync
    except Exception as e:  # pragma: no cover - import failure only
        print(f'[email] drain unavailable: {e}')
        state['emailOutbox'] = []
        return [('import', False, False)]
    for entry in list(outbox):
        # An entry may restrict its delivery channels; the
        # default is both channels.
        channels = entry.get('channels') or ('email', 'telegram')
        email_ok = False
        telegram_ok = False
        if 'email' in channels:
            try:
                email_ok = send_player_email_sync(entry.get('subject'), entry.get('body'))
            except Exception as e:  # pragma: no cover
                email_ok = False
                print(f'[email] send raised: {e}')
        if 'telegram' in channels:
            try:
                telegram_ok = send_player_telegram_sync(entry.get('subject'), entry.get('body'))
            except Exception as e:  # pragma: no cover
                telegram_ok = False
                print(f'[telegram] player push raised: {e}')
        results.append((entry.get('kind'), bool(email_ok), bool(telegram_ok)))
    state['emailOutbox'] = []
    return results


def _deliver_player_ask(state, issue_key, agent_id, question, context):
    """Emit the player-inbox message and block the agent until answered. Returns
    the new inbox message id, or None if the message can't be created."""
    now_ms = time.time() * 1000
    messages = state.setdefault('playerInbox', [])
    mid = f"ask-{int(now_ms)}"
    messages.append({
        'id': mid, 'issueKey': issue_key, 'agentId': agent_id,
        'question': (question or '').strip(), 'context': context or '',
        'status': 'awaiting_input', 'createdAt': now_ms,
    })
    issue = state['issues'].get(issue_key)
    if issue:
        issue['needsInput'] = True
    # Real-email: tell the player an agent is waiting on their answer.
    _queue_player_email(
        state, 'agent_ask',
        f"[AI Think Tank] {agent_id} has a question for you on {issue_key}",
        (f"{agent_id} is waiting on your answer for {issue_key} "
         f"({issue.get('title') or issue.get('summary') or 'unknown story'}):\n\n"
         f"{question}\n\n"
         f"Reply in the think tank player-inbox to unblock them."),
        now_ms=now_ms)
    _block_agent_for_input(state, agent_id, issue_key, now_ms)
    return mid


def _block_agent_for_input(state, agent_id, issue_key, now_ms):
    """Freeze an agent off the sim's assignment path (WAIT, not abandon): mark
    `_awaitingPlayer` + which issue, keep them visible/persisted. They resume via
    resolve_player_ask or a terminal `blocked` status."""
    a = (state.get('agents') or {}).get(agent_id)
    if a:
        a['_awaitingPlayer'] = True
    locked = state.setdefault('_playerLockedAgents', {})
    locked[agent_id] = {'issueKey': issue_key, 'at': now_ms}


def _unblock_agent(state, agent_id):
    """Reverse _block_agent_for_input: clear the wait flag so the sim assigns
    the agent work again."""
    a = (state.get('agents') or {}).get(agent_id)
    if a and a.get('_awaitingPlayer'):
        a.pop('_awaitingPlayer', None)
    (state.get('_playerLockedAgents') or {}).pop(agent_id, None)
