

@app.post('/api/state')
async def post_state(request: Request):
    # The whole-think tank state blob is the player's domain: the browser's
    # autosave is the only legitimate writer besides the sim itself. An
    # agent key must not be able to overwrite village state and bypass every
    # escalation/oversight gate, so this is player-session-only.
    if not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return JSONResponse(
            {'error': 'Unauthorized -- please log in'}, status_code=401)
    data = await request.json()
    data.pop('agentKeys', None)  # never persist keys back into the state blob itself -- agent_keys table is the one source of truth
    # The client autosave (agents.js saveState) rebuilds the state blob from a
    # fixed client-owned field list every 5s, so server-owned keys it doesn't
    # send -- templates (director-authored role library), and any future
    # server-only field -- would be silently dropped on the very next autosave.
    # Carry forward any existing top-level key the incoming payload omits. The
    # client always sends the game fields (agents/roster/reports/...), so those
    # are replaced exactly as before; only truly server-owned keys survive, and
    # a brand-new server field never needs a client change to stop vanishing.
    data = _merge_server_owned(get_state_from_db() or {}, data)
    violations = _flat_director_chart_violations(data)
    if violations:
        return JSONResponse(
            {'error': 'flat org chart violated: every director sits directly '
                      'under the admin, never under another director -- '
                      'offending directors: '
                      + ', '.join(str(v) for v in violations)},
            status_code=400)
    save_state_to_db(data)
    # Not logged -- this fires every 5s from the client's own autosave
    # timer (agents.js), and a heartbeat isn't an "action" worth an
    # audit-log row. Real actions (chat, browse, execute, hire, fire,
    # task assignment) are logged at their own call sites instead.
    return PlainTextResponse('saved')


_SERVER_OWNED_AGENT_FIELDS = (
    'x', 'y', 'dir', 'path', 'path_index', 'pathIndex', 'path_target',
    'pathTarget', 'stuckTimer', 'stuck_timer', 'replanCount', 'replan_count',
    'respawnedForTask', 'respawned_for_task',
    # On/off duty is server-authoritative under the flip. The
    # server parks idle wanderers off duty (_park_idle_wanderers) and wakes
    # them on assignment; the client's 5s autosave and reload reload path
    # (`if (!offDuty) visible = true`) must not resurrect an idle agent the
    # server just parked, or the "only scheduled/active agents appear" rule
    # is defeated by the very next autosave. Waking still lands: it goes
    # through server-side intents (_wake_authority_on_request, assignment)
    # which mutate server state directly, so the merge keeps the fresh value.
    'offDuty', 'visible',
)
# Agent fields a client autosave may carry that are authoritative SERVER-owned
# once sim.owner == 'server'. The client is a renderer under the flip; its 5s
# autosave must not clobber positions the server just advanced.


def _merge_server_owned(existing, incoming):
    # Pure helper for the autosave merge: carry forward every top-level key the
    # client's saveState payload omits (templates, future server-only fields),
    # replacing the rest exactly. Extractable so the invariant is testable.
    merged = dict(incoming)
    existing = existing or {}
    for key, value in existing.items():
        if key not in merged:
            merged[key] = value
    # Server-ownership defense: if the existing state already marked the server
    # as the movement owner, the incoming autosave's agent POSITIONS must not
    # overwrite the server's -- that's the whole point of the flip (the server
    # drives, the client renders). We keep incoming NON-spatial agent fields
    # (busy/task/inRoom/offDuty/reports/etc.) so a client-side state change
    # still lands, but x/y/dir/path and the movement bookkeeping are carried
    # forward verbatim from the server's view.
    owner = (existing.get('sim') or {}).get('owner')
    if owner == 'server':
        inc_agents = incoming.get('agents')
        ex_agents = existing.get('agents')
        if isinstance(inc_agents, dict) and isinstance(ex_agents, dict) \
                and isinstance(merged.get('agents'), dict):
            # Rebuild merged.agents with server-owned positions carried forward,
            # without mutating the caller's `incoming` dict (merged is a shallow
            # copy -- nested agent dicts are shared references otherwise).
            merged['agents'] = {
                aid: _merge_server_owned_agent(ex_agents.get(aid), inc_agents.get(aid))
                for aid in set(list(ex_agents.keys()) + list(inc_agents.keys()))
            }
        # Phase 3: under server ownership the server is ALSO the authoritative
        # writer of the task lifecycle -- the workQueue (assign/requeue/abandon)
        # and the durable state['tasks'] mirror (server-in-memory TASKS, carried
        # so a reopened browser sees in-flight tasks instead of abandoning them).
        # The client still sends `workQueue`, but a stale copy must not clobber
        # the server's mid-cycle edits -- the server's queue + task map win
        # when they exist.
        for k in ('workQueue', 'tasks'):
            if k in existing:
                merged[k] = existing[k]
        # Phase 4: governance (auto-hire / auto-firing review) is a server
        # cadence pass that mutates the canonical roster -- a hire appends to it,
        # a fire removes from it. The client's 5s autosave sends ITS stale
        # AGENT_ROSTER copy, which would silently revert a server-side
        # hire/fire within the next autosave. The server's roster is the
        # authoritative one once governance is server-owned, so it wins.
        # # (Client-facing player actions like chat/mailbox still ride on the
        # individual agent records, which the non-spatial merge above carries.)
        if 'agentRoster' in existing:
            merged['agentRoster'] = existing['agentRoster']
        # Director-editable room definitions are also server-owned state -- the
        # client doesn't author them, and its autosave shouldn't revert a
        # director's purpose edit (same stale-copy hazard as the roster).
        if 'roomDefinitions' in existing:
            merged['roomDefinitions'] = existing['roomDefinitions']
        # Teams are server-owned: name/purpose edits and promotions happen via
        # the /api/teams endpoints, so a client autosave must not revert them.
        # Promote-to-director changes the roster's `director` pointers, which
        # agentRoster already protects; this keeps the teams[] record safe too.
        if 'teams' in existing:
            merged['teams'] = existing['teams']
        # Sprints are server-owned: director-authored via /api/intent/sprint, so
        # a client autosave must not revert them (same stale-copy hazard as the
        # roster/teams/roomDefinitions above). Queue items are separately
        # preserved by the workQueue rule, but the sprint CONTAINERS live here.
        if 'sprints' in existing:
            merged['sprints'] = existing['sprints']
        # Phase E: products + wiki are server-owned -- a release flips a
        # product's status and a director's wiki edit bumps a page version, and
        # the client's autosave holds neither, so its stale copies must not
        # revert them (same hazard as roster/teams/sprints above).
        for _k in ('products', 'wiki'):
            if _k in existing:
                merged[_k] = existing[_k]
        # WS-14: features + the shared backlog + retrospectives are server-owned
        # -- a pull assigns teamId/storyKey and a retro lands at sprint close,
        # and the client's autosave holds neither, so its stale copies must not
        # revert them (same hazard as products/wiki/teams above).
        for _k in ('features', 'backlog', 'retrospectives',
                   'pendingRetrospectives', 'pendingSprintRetros',
                   '_oncallOrder', '_oncallServed', '_pendingAsks'):
            if _k in existing:
                merged[_k] = existing[_k]
        # Server cadence stamps are server-authoritative (see the _check_schedules
        # sentinel guard in sim.py). The client's autosave may still carry a stale
        # copy -- most dangerously the 1e18 TEST sentinel a pre-server-owned
        # clients.js used to persist, which would otherwise resurrect itself on
        # every 5s autosave and permanently re-disable the standing skill-review
        # sweep. The server re-owns the stamp on its next cycle; never let a
        # client's copy win here.
        for _c in ('lastSkillReviewAt', 'lastStuckGateSweep', 'lastRunAt',
                   'lastGovernancePass', 'lastAutoHireAt', 'lastFiringReviewAt'):
            if _c in existing:
                merged[_c] = existing[_c]
        # WS-14: monotonic counters for shared-backlog items and features are
        # server-authoritative -- a stale client counter could otherwise make a
        # hot state re-derive an id it already handed out (same hazard as the
        # cadence stamps above).
        for _c in ('featureCounter', 'backlogCounter'):
            if _c in existing:
                merged[_c] = existing[_c]
    return merged


def _merge_server_owned_agent(server_agent, client_agent):
    # Merge one agent for the server-ownership path: client-provided
    # non-spatial fields win, server-owned spatial fields win. Either side may
    # be missing an agent (a fresh hire the server hasn't seen, or an agent
    # only the server tracks).
    if client_agent is None:
        return server_agent
    if server_agent is None:
        return client_agent
    if not isinstance(client_agent, dict) or not isinstance(server_agent, dict):
        return client_agent
    out = dict(client_agent)
    for f in _SERVER_OWNED_AGENT_FIELDS:
        if f in server_agent:
            out[f] = server_agent[f]
    return out


# Actions the browser may report via POST /api/log. Everything in action_log
# feeds the agent MEMORY.md rendering, so this is a strict allowlist, not a
# free-text channel: an arbitrary "action" from any authenticated caller was a
# memory-poisoning vector (hostile text planted in action_log.details would
# surface verbatim in the agent's long-term memory on the next render).


@app.post('/api/mail/send')
@_state_writer
async def api_mail_send(request: Request):
    # The one sanctioned inter-agent mail path, server-validated and
    # server-authoritative. Previously the browser pushed ``fromName: text``
    # straight into the recipient's local mailbox, which autosave then
    # persisted -- an unvalidated, client-controlled write into a surface that
    # feeds model context. This endpoint validates identity (sender/recipient
    # must be real roster agents or the player) and shape (plain bounded
    # string) before appending to the recipient's mailbox in state. Content is
    # still free prose -- hostile mail text is handled as DATA by the
    # EXTERNAL_DATA boundary in context assembly, not by sanitizing it away.
    player = bool(verify_session(request.cookies.get(SESSION_COOKIE_NAME)))
    key_owner = _agent_id_for_key(request.headers.get('X-Agent-Key'))
    if not player and not key_owner:
        return JSONResponse(
            {'error': 'Unauthorized -- please log in'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    # Sender binding: the credential decides the sender, never the
    # body. A presented agent key sends AS its owner (world.js sends
    # the acting agent's own X-Agent-Key, even alongside the player
    # session); without one, a player session sends AS the player.
    # Trusting fromId would let any caller forge any agent's -- or
    # the player's -- mail into a mailbox that feeds model context.
    from_id = key_owner or ('player' if player else None)
    to_id = (body.get('toId') or '').strip()
    text = body.get('text')
    if not isinstance(text, str) or not text.strip():
        return JSONResponse({'error': 'text is required'}, status_code=400)
    text = text.strip()[:2000]
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    roster = state.get('agentRoster') or []
    ids = {d.get('id') for d in roster if isinstance(d, dict)}
    if to_id not in ids:
        return JSONResponse({'error': 'recipient not found'}, status_code=404)
    if from_id != 'player' and from_id not in ids:
        return JSONResponse({'error': 'sender not found'}, status_code=400)
    if from_id == 'player':
        from_name = 'You'
    else:
        names = [d.get('name') or d.get('id') for d in roster
                 if isinstance(d, dict) and d.get('id') == from_id]
        from_name = str(names[0]) if names else str(from_id or '')
    import sim as _sim
    target = (state.get('agents') or {}).get(to_id)
    if not isinstance(target, dict):
        target = {'mailbox': []}
        state.setdefault('agents', {})[to_id] = target
    entry = {'text': f'{from_name}: {text}', 'read': False,
             'ts': int(time.time() * 1000)}
    _sim._append_mailbox(target, entry)
    # BP-62 appeal path: an agent reporter replying about a declined card may
    # re-surface it once. The reply text names the issue key (our decline mail
    # carries it), so an agent-to-SM reply on a declined card registers the
    # appeal -- the card re-enters that team's next refinement ceremony with
    # the reply attached. Bystanders citing the key, replies to anyone else,
    # and player mail are ignored (players re-file cards; the appeal guard
    # lives in _register_card_appeal). Best-effort: a malformed reply must
    # never fail the mail delivery itself.
    if from_id and from_id != 'player' and from_id != to_id and isinstance(text, str):
        try:
            _maybe_register_card_appeal(state, from_id, text, to_id)
        except Exception:  # noqa: BLE001 -- mail must land even if appeals misbehave
            pass
    # Commit the mailbox entry (and any appeal) before routed work runs.
    # A routing lane may save state itself, which releases the whole-blob
    # lock; committing first lets the reply write re-read the latest blob
    # instead of saving a stale snapshot over another writer's changes.
    save_state_to_db(state)
    # Admin messages surface: the chief of staff never runs content tasks, so a mailbox
    # note alone would sit unread forever (verified against the action log --
    # the admin's only model context is governance ceremonies, which never
    # read the mailbox). Player mail to the chief of staff is therefore ALSO pushed
    # through the same request routing the Telegram bridge uses: the chief of staff is
    # dispatched immediately (a live answer for an ask, or the request filed as
    # spike/story/incident work), and the reply lands in the player's inbox.
    # The mailbox entry above stays as the durable record. Parked asks (admin
    # busy) are handled by the standing ask-drain, so nothing is lost.
    if from_id == 'player' and to_id == _admin_agent_id(state):
        # Re-read after the early commit. A routing lane may save state itself;
        # passing the pre-commit snapshot lets that lane write stale tasks and
        # queue over changes the tick made during the classifier call.
        _state_abort()
        state = _state_begin()
        routed = await _route_player_request(state, text)
        if not routed.get('queued'):
            reply = routed.get('reply') or routed.get('error') or ''
            if reply:
                now_ms = int(time.time() * 1000)
                # Drop any hold the early commit (or a mocked commit in tests)
                # left on this thread before taking a fresh snapshot. Nesting
                # two _state_begin calls would let one release strand the other
                # and freeze every later writer.
                _state_abort()
                latest = _state_begin()
                if latest is not None:
                    latest.setdefault('playerInbox', []).append({
                        'id': f'mail-{now_ms}',
                        'agentId': routed.get('agent') or to_id,
                        'question': text[:500],
                        'answer': reply,
                        'status': 'answered',
                        'answeredAt': now_ms,
                        'createdAt': now_ms,
                        'queued': False,
                    })
                    _sim._queue_player_email(
                        latest, 'admin_mail_answered',
                        '[AI Think Tank] Theo replied',
                        (f'You messaged Theo: {text[:300]}\n\nTheo answered:\n{reply}'))
                    save_state_to_db(latest)
                log_action('player', 'admin_mail_answered',
                           {'agent': to_id, 'queued': False},
                           authorized=True)
    log_action(from_id if from_id != 'player' else None, 'mail_sent',
               {'to': to_id, 'text': text}, authorized=True)
    return JSONResponse({'ok': True, 'mail': entry})


_CARD_KEY_RE = re.compile(r'[A-Z][A-Z0-9]{1,3}-\d+')


def _maybe_register_card_appeal(state, from_id, text, to_id):
    """BP-62: find which issue keys an agent's reply names and register an
    appeal for each one the sender actually owns and that is currently
    declined and addressed to the owning team's scrum master. Capped per
    reply; pure state mutation via the sim helper."""
    import sim as _sim_module
    keys = list(dict.fromkeys(_CARD_KEY_RE.findall(text or '')))[:5]
    for key in keys:
        _sim_module._register_card_appeal(state, from_id, key, text, via_sm=to_id)


@app.post('/api/mail/read')
async def api_mail_read(request: Request):
    # The "mark all read" half of the phone-like mail surface: an
    # agent (or the Post Office / a director / the admin, for another
    # agent's pile) clears its unread messages. Agent-key only -- the
    # player's inbox is a different surface (playerInbox), and a
    # player session has no agent mailbox to mark. The requester is
    # the key owner, never a body-supplied id, and the own-vs-other
    # permission lives in _mark_mailbox_read, server-side.
    key_owner = _agent_id_for_key(request.headers.get('X-Agent-Key'))
    if not key_owner:
        return JSONResponse(
            {'error': 'Unauthorized -- agent key required'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    target = (body.get('agentId') or '').strip()
    result = _mark_mailbox_read(key_owner, target)
    if not result.get('ok'):
        return JSONResponse(result, status_code=result.get('status') or 400)
    log_action(key_owner, 'mail_read',
               {'agentId': result.get('agentId'), 'marked': result.get('marked')},
               authorized=True)
    return JSONResponse(result)


# Phone outcomes (ask 13): a policy denial is 403,
# a name that matches no agent or message is 404.
_PHONE_NOT_FOUND_REASONS = {
    'unknown recipient', 'unknown agent',
    'unknown message',
}


def _phone_failure(outcome):
    """The error response for a denied phone outcome,
    or None when the outcome was allowed -- the caller
    then builds its own success body."""
    if outcome.allowed:
        return None
    status = (404 if outcome.reason
              in _PHONE_NOT_FOUND_REASONS else 403)
    return JSONResponse({'error': outcome.reason},
                        status_code=status)


@app.post('/api/phone/send')
@_state_writer
async def phone_send(request: Request):
    """Send one phone message (ask 13) from the
    authenticated agent to another agent. The roster
    policy gates send_message for the sender's role --
    directors and workers may both chat -- and the
    message lands in the recipient's mailbox as a
    phone entry, without waking her: chat is not
    work assignment."""
    agent_id, error = _agent_requester(request)
    if agent_id is None:
        return error
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    if not isinstance(body, dict):
        return JSONResponse(
            {'error': 'body must be a JSON object'},
            status_code=400)
    recipient_id = (body.get('recipientId') or '').strip()
    text = body.get('text')
    if not recipient_id:
        return JSONResponse(
            {'error': '"recipientId" is required'},
            status_code=400)
    if not isinstance(text, str) or not text.strip():
        return JSONResponse(
            {'error': '"text" is required'}, status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse(
            {'error': 'state unavailable'}, status_code=503)
    sent = phone.Phone(_theo_policy).send(
        state, agent_id, recipient_id, text.strip()[:2000])
    failure = _phone_failure(sent)
    if failure is not None:
        return failure
    save_state_to_db(state)
    log_action(agent_id, 'phone_sent',
               {'messageId': sent.message_id,
                'to': recipient_id}, authorized=True)
    return JSONResponse({'ok': True,
                         'messageId': sent.message_id})


@app.get('/api/phone/inbox')
@_state_writer
async def phone_inbox(request: Request):
    """The authenticated agent's unread phone
    messages (ask 13), oldest first."""
    agent_id, error = _agent_requester(request)
    if agent_id is None:
        return error
    state = _state_begin()
    if not state:
        return JSONResponse(
            {'error': 'state unavailable'}, status_code=503)
    box = phone.Phone(_theo_policy).inbox(state, agent_id)
    failure = _phone_failure(box)
    if failure is not None:
        return failure
    return JSONResponse({
        'ok': True,
        'agentId': box.agent_id,
        'messages': [dict(entry) for entry in box.messages],
    })


@app.post('/api/phone/read')
@_state_writer
async def phone_read(request: Request):
    """Mark one phone message read (ask 13), and
    return what is still unread."""
    agent_id, error = _agent_requester(request)
    if agent_id is None:
        return error
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    if not isinstance(body, dict):
        return JSONResponse(
            {'error': 'body must be a JSON object'},
            status_code=400)
    message_id = (body.get('messageId') or '').strip()
    if not message_id:
        return JSONResponse(
            {'error': '"messageId" is required'},
            status_code=400)
    state = _state_begin()
    if not state:
        return JSONResponse(
            {'error': 'state unavailable'}, status_code=503)
    box = phone.Phone(_theo_policy).read(
        state, agent_id, message_id)
    failure = _phone_failure(box)
    if failure is not None:
        return failure
    save_state_to_db(state)
    log_action(agent_id, 'phone_read',
               {'messageId': message_id}, authorized=True)
    return JSONResponse({
        'ok': True,
        'agentId': box.agent_id,
        'messages': [dict(entry) for entry in box.messages],
    })


@app.post('/api/phone/read-all')
@_state_writer
async def phone_read_all(request: Request):
    """Mark every phone message read (ask 13)."""
    agent_id, error = _agent_requester(request)
    if agent_id is None:
        return error
    state = _state_begin()
    if not state:
        return JSONResponse(
            {'error': 'state unavailable'}, status_code=503)
    box = phone.Phone(_theo_policy).mark_all_read(
        state, agent_id)
    failure = _phone_failure(box)
    if failure is not None:
        return failure
    save_state_to_db(state)
    log_action(agent_id, 'phone_read_all',
               authorized=True)
    return JSONResponse({'ok': True, 'agentId': box.agent_id})


@app.post('/api/log')
async def api_log(request: Request):
    # A generic logging endpoint for decisions made entirely in the
    # browser (hiring, firing, task assignment, handoffs) -- those aren't
    # behind their own serve.py route, so without this they'd be invisible
    # to the one activity log. Not itself a sensitive
    # action, just a record of one that already happened client-side.
    # Player-session-only (the browser); an agent key must not be able to
    # write rows into its own memory feed. Action names are allowlisted so a
    # caller can only append rows for real client-side actions.
    if not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    action = body.get('action', 'unknown')
    details = body.get('details')
    if action not in _API_LOG_ALLOWED_ACTIONS:
        return PlainTextResponse('logged')  # unknown actions are dropped, not stored
    agent_id = body.get('agentId')
    log_action(agent_id, action, details)
    # Chain the consequential ones (hire, a fire verdict, big-task
    # delegation) into the passport; routine actions fall through.
    if action in _HASHED_ACTIONS:
        if action == 'firing_review' and isinstance(details, dict) and details.get('decision') != 'fire':
            pass  # a 'keep'/'deferred' review isn't a world-changing fire
        else:
            _append_passport_decision(action, agent_id or 'unknown', details or {})
    return PlainTextResponse('logged')


def _classify_report_severity(quote, note):
    """Deterministic severity classification for a filed report (2026-10-06):
    'small things of no harm' can't be marked as firing-grade negatives. Scans
    quote+note for the most serious keyword signal -- severe (real
    misconduct/sabotage) > serious (real harm, dropped/missed work) > major
    (friction, miscommunication) > minor (default, no harm). Rule-based on
    purpose: severity decides who may be fired, so it must be derivable and
    testable, not a coin flip. Only serious/severe count as firing strikes
    (see sim._is_strike_severity)."""
    text = f'{quote} {note}'.lower()
    severe = ['sabotage', 'fabricat', 'forge', 'harass', 'threaten', 'threat',
              'coerc', 'extort', 'bribe', 'stole', 'steal', 'theft', 'credential',
              'password', 'backdoor', 'exploit', 'malware', 'assault', 'blackmail',
              'lied', 'vandal']
    serious = ['dropped', 'missed', 'unresponsive', 'unreachable', 'ghosted',
               'ignored', 'deadline', 'overdue', 'unreliable', 'incompet',
               'not responding', 'failed to', 'negligent', 'broke the build']
    major = ['miscommunicat', 'confusion', 'confused', 'unclear', 'friction',
             'argument', 'rude', 'dismissive', 'unhelpful', 'overwhelmed',
             'sloppy', 'careless']
    for word in severe:
        if word in text:
            return 'severe'
    for word in serious:
        if word in text:
            return 'serious'
    for word in major:
        if word in text:
            return 'major'
    return 'minor'


@app.post('/api/reports')
@_state_writer
async def post_report(request: Request):
    # Agent-filed report into ANOTHER agent's reports/ directory (your
    # Call: "agents should be able to write reports in the
    # directories of other agents, if they see fit" -- and the flip side,
    # that an agent must not be able to view or modify its OWN reports dir).
    # Attribution-gated: the filer must present a real agent key for its
    # claimed id, and may NOT file a report about itself. A filed report
    # lands in the TARGET agent's reports/ directory (state['reports'] +
    # sync_agent_directories materialize it there), exactly where the
    # subject cannot see it under the self-reports rule.
    body = await request.json()
    about_id = (body.get('aboutId') or '').strip()
    from_id = (body.get('fromId') or '').strip()
    quote = (body.get('quote') or '').strip()
    note = (body.get('note') or '').strip()
    if not about_id or not from_id or not quote or not note:
        return JSONResponse({'error': 'aboutId, fromId, quote, and note are required'}, status_code=400)
    if from_id in ('player', 'unknown'):
        return JSONResponse({'error': 'reports must be filed by a named agent'}, status_code=403)
    authorized = verify_agent_key(from_id, request.headers.get('X-Agent-Key'))
    if authorized is not True:
        return JSONResponse({'error': 'attribution key mismatch'}, status_code=403)
    if about_id == from_id:
        # An agent can't report on itself -- its own reports/ dir is
        # off-limits to it by design, so self-reports have nowhere legal to
        # go and would just be self-referential noise.
        return JSONResponse({'error': 'an agent cannot file a report about itself'}, status_code=403)
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    roster_ids = {d.get('id') for d in state.get('agentRoster', [])}
    if about_id not in roster_ids:
        return JSONResponse({'error': 'unknown subject'}, status_code=404)
    reports = state.get('reports', [])
    report = {
        'id': f'report-{int(time.time() * 1000)}-{about_id}',
        'aboutId': about_id, 'fromId': from_id, 'quote': quote, 'note': note,
        'ts': int(time.time() * 1000),
        # Classified at filing, not left to the client: only a genuine
        # serious/severe report can ever become a firing strike.
        'severity': _classify_report_severity(quote, note),
    }
    reports.append(report)
    state['reports'] = reports
    save_state_to_db(state)  # materializes into agents/<about_id>/reports/
    log_action(from_id, 'report_filed', {'about': about_id}, authorized=True)
    _append_passport_decision('report_filed', from_id, {'about': about_id})
    # Player notification only when the evidence crosses a real bar: a severe
    # review, or the second strike landing inside the grace window. Minor/major
    # noise never pings the player's inbox, so a single honest complaint can't
    # drum up a notification.
    try:
        import sim as _sim
        strikes = _sim._report_strikes(state, about_id, int(time.time() * 1000))
        if strikes['severe'] >= 1 or strikes['count'] >= 2:
            window_days = _sim.REPORT_STRIKE_WINDOW_MS // (24 * 3600 * 1000)
            create_escalation(
                'personnel_strike',
                f'{from_id} filed a serious negative review of {about_id}; '
                f'{strikes["count"]} firing strike(s) on record inside the {window_days}-day grace window.',
                what_checked=f'reports about {about_id} inside the window: {strikes["count"]}',
                look_first=f'{about_id}\'s reports directory')
            log_action('player', 'personnel_strike',
                       {'about': about_id, 'from': from_id,
                        'strikes': strikes['count'], 'severe': strikes['severe']},
                       authorized=True)
    except Exception:
        # Notification is best-effort -- a filing must never fail because the
        # alert couldn't be raised.
        pass
    return JSONResponse({'ok': True, 'id': report['id']})


# ---------------------------------------------------------------------------
# Player intent (Phase 5): the browser is a pure renderer under
# sim.owner=='server', so the player's "delegate a big task" action must NOT
# mutate the client's own WORK_QUEUE -- the server's merge drops client
# workQueue copies outright. Instead the client posts the INTENT here, and the
# server does the assignBigTask decomposition (tasks.js) against the
# authoritative state and enqueues into the server's workQueue through the
# same sim.queue_work the task loop consumes. Mirrors tasks.js assignBigTask
# (tasks.js:908): pick the free authority, decompose via the planning tier,
# whitelist rooms, normalize, queue.
# ---------------------------------------------------------------------------
# Valid rooms and what each actually does -- the planner must be told the real
# current capability of each room, not a name-only guess. `_DELEGATABLE_ROOMS`
# is the code-graph set of walkable room keys the planner may target (agents,
# collision, and _check_schedules all key off these strings, so they are
# source-of-truth and NOT agent-editable). The human-facing `label` and the
# planner-facing `purpose` are seeded defaults that live in DB state
# (state.roomDefinitions) and are director/admin-editable via
# POST /api/rooms/{room}/purpose -- the label is locked there, only the
# description that agents reason about may change.
_DELEGATABLE_ROOMS_DEFAULTS = ['observatory', 'pressoffice', 'postoffice', 'bank',
                             'weatherstation', 'library', 'media']
_DELEGATABLE_ROOMS = list(_DELEGATABLE_ROOMS_DEFAULTS)
_ROOM_DEFINITION_DEFAULTS = {
    'pressoffice': {'label': 'Work Room', 'purpose': 'real sandboxed software development; actually writes and runs code for the SPECIFIC task given (taskType "code"), or reviews/QA-tests an already-built deliverable for real, specific problems (taskType "review" or "qa"). Use this for anything meaning "write/build/fix code" OR "review/test what was built." QUALITY BAR: written code must follow PEP 8 and pass the standard Python toolchain baked into this sandbox -- flake8, mypy, bandit, and pytest with >=90% coverage (the "quality pipeline") -- and work is NOT approved unless that pipeline is green.'},
    'observatory': {'label': 'Research Center', 'purpose': 'makes a real model call reasoning about the SPECIFIC subtask given and files a genuine, findable finding. Use for "research/investigate/write up findings on X."'},
    'weatherstation': {'label': 'Weather Station', 'purpose': 'an open-ended research room with internet access; may investigate or analyze any topic on request. The name is cosmetic and does not limit the topic. Use for "research/investigate/analyze X" on any subject.'},
    'media': {'label': 'Studio', 'purpose': 'digests one of the subscribed feeds (media/feeds.md) into a summary. Use only for "summarize/digest an external source."'},
    'library': {'label': 'Library', 'purpose': 'reference/reading room. No automated work happens here at all; never assign a subtask here that needs a real deliverable.'},
    'postoffice': {'label': 'Post Office', 'purpose': 'no automated work happens here at all; never assign a subtask here that needs a real deliverable.'},
    'bank': {'label': 'Bank', 'purpose': 'no automated work happens here at all; never assign a subtask here that needs a real deliverable.'},
    'hangout': {'label': 'Hangout', 'purpose': 'an empty social room behind the Town Hall door; agents idle and mingle here. NEVER assign a subtask here -- this is not a work room.'},
}
_DEFAULT_ROOM_DEFINITIONS = {}


def _rebuild_room_definitions():
    """Kernel room seeds with rooms.json overrides merged per room. Mutates
    in place so the durable-state backfill and every reader see an edit."""
    _DEFAULT_ROOM_DEFINITIONS.clear()
    for room, base in _ROOM_DEFINITION_DEFAULTS.items():
        entry = dict(base)
        entry.update(ROOM_DEFINITION_OVERRIDES.get(room) or {})
        _DEFAULT_ROOM_DEFINITIONS[room] = entry
    _DELEGATABLE_ROOMS[:] = list(ROOM_DELEGATABLE_OVERRIDES or _DELEGATABLE_ROOMS_DEFAULTS)


def _rebuild_village_policy():
    _rebuild_seed_profiles()
    _rebuild_room_definitions()
    _NON_WAKING_READS.clear()
    _NON_WAKING_READS.update(_NON_WAKING_READS_BASE | NON_WAKING_READS_EXTRA)
    _NON_WAKING_WRITES.clear()
    _NON_WAKING_WRITES.update(_NON_WAKING_WRITES_BASE | NON_WAKING_WRITES_EXTRA)


ON_RELOAD_HOOKS.append(_rebuild_village_policy)
_rebuild_room_definitions()
_big_task_max_tokens = 4000  # tasks.js: the decomposition runs once per request; every downstream call depends on it.


def _room_definitions(state):
    """The durable roomDefinitions map, backfilled from the seed defaults on a
    cold/legacy DB. A director may have amended a `purpose`; the `label` is a
    first-class key on each entry but is NOT writable by agents (only the
    description is). Backfill only fills missing ROOMS -- it never overwrites a
    director-edited purpose."""
    defs = state.setdefault('roomDefinitions', {})
    for room, d in _DEFAULT_ROOM_DEFINITIONS.items():
        existing = defs.get(room)
        if existing is None:
            defs[room] = {'label': d['label'], 'purpose': d['purpose']}
        else:
            # Never clobber a director-edited purpose; repair a missing label on
            # an entry the seed introduced (labels can't be edited, so defaults
            # are authoritative for them).
            if not existing.get('label'):
                existing['label'] = d['label']
            existing.setdefault('purpose', d['purpose'])
    return defs


def _free_authority(state):
    """tasks.js assignBigTask's authority pick: the chief of staff (Theo) if free, else
    the senior-most director (Nora). Works from server-authoritative roster +
    agent busy/offDuty state."""
    roster = state.get('agentRoster') or []
    agents = state.get('agents') or {}
    def _is_free(d):
        a = agents.get(d.get('id'))
        return a is not None and not a.get('busy') and not a.get('offDuty')
    for d in roster:
        if d.get('isAdmin') and _is_free(d):
            return d
    for d in roster:
        if d.get('isDirector') and not d.get('isAdmin') and not d.get('director') and _is_free(d):
            return d
    return None


def _wake_authority_on_request(state):
    """A real request from the player should wake a resting admin/director so
    she can accept it -- tasks.js documents 'a new request from you wakes the
    admin' (tasks.js). Without this an all-off-duty think tank is un-delegable:
    _free_authority needs someone on-duty, but there's no player-facing wake
    control, so a resting authority can never accept the very first task.
    Reach for an off-duty admin, else the senior-most off-duty director, and
    wake her in place (appear_from_outskirts). No-op (returns None) if *all*
    authorities are busy rather than merely resting."""
    for pick in (
        (d for d in state.get('agentRoster') or [] if d.get('isAdmin')),
        (d for d in state.get('agentRoster') or []
         if d.get('isDirector') and not d.get('isAdmin') and not d.get('director')),
    ):
        for d in pick:
            a = (state.get('agents') or {}).get(d.get('id'))
            if a is not None and not a.get('busy') and a.get('offDuty'):
                try:
                    import sim as _sim
                    _sim.appear_from_outskirts(state, d['id'])
                except Exception:
                    # fall back to a direct flag clear if the wake helper can't
                    a['offDuty'] = False
                    a['visible'] = True
                return d
    return None


def _staffing_team_for_authority(state, authority_id):
    """The receiving team for a staffable large ask, picked by the free
    authority from the ask alone: the authority's OWN team when that team is
    free (not in an active sprint), else the first existing team that is free.
    The ask is then filed to that team's breakdown ceremony (scrum master +
    workers card it into stories) -- the authority never predicts subtasks up
    front. Returns the team dict, or None when no team is free."""
    import sim as _sim
    teams = state.get('teams') or []
    own = next((t for t in teams if t.get('directorId') == authority_id), None)
    if own and not _sim._team_in_active_sprint(state, own.get('id') or own.get('directorId')):
        return own
    for t in teams:
        if not _sim._team_in_active_sprint(state, t.get('id') or t.get('directorId')):
            return t
    return None


def _all_teams_busy_in_sprint(state):
    """True when EVERY existing team is tied to an ACTIVE sprint -- i.e. the
    whole think tank is already committed to large-ask work. Used by the large-
    request flow: only when no team is free does a new director +
    team get created to take the request. A team with no active sprint (or no
    sprints at all) counts as free."""
    teams = (state.get('teams') or []) if state else []
    if not teams:
        return False
    sprints = state.get('sprints') or {}
    active = [s for s in sprints.values() if s.get('status') == 'active']
    if not active:
        return False
    active_team_ids = set()
    for s in active:
        for tid in (s.get('teamIds') or []):
            active_team_ids.add(tid)
    for t in teams:
        tid = t.get('id') or t.get('directorId')
        if tid not in active_team_ids:
            return False
    return True
