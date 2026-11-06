

# The Question Registry + Stakeholder Graph + Objective coverage chain.
# Read views are open (the brief is the operator's gap view); write moves are
# authenticated like the rest of the desk (player session or agent key),
# because a compromised agent key must not be able to close questions or
# contradict claims it dislikes.

@app.get('/api/questions')
async def questions_list(request: Request):
    # ?status=unknown|partial|tentatively_answered|answered|contradicted,
    # ?objective=..., ?limit=N (tail). Default view: every question.
    status = request.query_params.get('status')
    if status is not None and status not in QUESTION_STATUSES:
        return JSONResponse({'error': f'status must be one of {sorted(QUESTION_STATUSES)}'}, status_code=400)
    objective = request.query_params.get('objective') or None
    try:
        limit = int(request.query_params.get('limit') or 200)
    except (TypeError, ValueError):
        limit = 200
    if status in OPEN_STATUSES:
        rows = open_questions(objective=objective, limit=limit)
    else:
        rows = [q for q in _questions_read().values()
                if (status is None or q.get('status') == status)
                and (objective is None or q.get('objective') == objective)]
        rows.sort(key=lambda q: q.get('created_ts', 0))
        if limit:
            rows = rows[-limit:]
    return JSONResponse({'questions': rows})


@app.get('/api/questions/brief')
async def questions_brief(request: Request):
    # The living brief: open questions, what changed recently, counts. No
    # fabricated confidence percentages -- counts and statuses only.
    try:
        days = int(request.query_params.get('days') or 7)
    except (TypeError, ValueError):
        days = 7
    return JSONResponse(registry_brief(window_s=max(1, days) * 86400))


@app.post('/api/questions')
async def questions_record(request: Request):
    # Record one question. Player session or agent key. Deduped by text: the
    # same question twice is one question with two askers.
    body = await request.json()
    is_player = _require_player_session(request)
    if is_player:
        actor = 'player'
    else:
        agent_id = (body.get('agentId') or 'unknown')
        if verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is not True:
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        actor = agent_id
    result = record_question(
        body.get('text') or '',
        objective=body.get('objective'),
        people_who_may_know=body.get('peopleWhoMayKnow'),
        evidence_claim_ids=body.get('evidenceClaimIds'),
        related_question_ids=body.get('relatedQuestionIds'),
        confidence=body.get('confidence'),
        source=body.get('source'),
        actor=actor,
    )
    if not result.get('recorded'):
        return JSONResponse({'error': result.get('error') or result.get('reason')}, status_code=400)
    return JSONResponse({'ok': True, 'question': get_question(result['id'])})


@app.post('/api/questions/answer')
async def questions_answer(request: Request):
    # Close a question with an answer. Tentative answers stay visibly soft.
    is_player = _require_player_session(request)
    body = await request.json()
    agent_id = 'player' if is_player else (body.get('agentId') or 'unknown')
    if not is_player and verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        rec = answer_question(
            (body.get('questionId') or '').strip(),
            body.get('answer') or '',
            status=(body.get('status') or 'answered').strip(),
            confidence=body.get('confidence', 1.0),
            evidence_claim_id=body.get('evidenceClaimId'),
            actor=agent_id,
        )
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    if rec is None:
        return JSONResponse({'error': 'unknown question id'}, status_code=404)
    log_action(agent_id, 'question_answer', {'question_id': rec['id']})
    return JSONResponse({'ok': True, 'question': rec})


@app.post('/api/questions/contradict')
async def questions_contradict(request: Request):
    # Flip a question to contradicted: later evidence showed its premise was
    # wrong. Answer is cleared, evidence stays on the record.
    is_player = _require_player_session(request)
    body = await request.json()
    agent_id = 'player' if is_player else (body.get('agentId') or 'unknown')
    if not is_player and verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    rec = contradict_question(
        (body.get('questionId') or '').strip(),
        evidence_claim_id=body.get('evidenceClaimId'),
        reason=body.get('reason'),
        actor=agent_id,
    )
    if rec is None:
        return JSONResponse({'error': 'unknown question id'}, status_code=404)
    log_action(agent_id, 'question_contradict', {'question_id': rec['id']})
    return JSONResponse({'ok': True, 'question': rec})


@app.post('/api/questions/conflict')
async def questions_conflict(request: Request):
    # The reframe move: old question closes as contradicted, new question
    # opens and links back. The chain IS the audit trail of the reframe.
    is_player = _require_player_session(request)
    body = await request.json()
    agent_id = 'player' if is_player else (body.get('agentId') or 'unknown')
    if not is_player and verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        rec = raise_conflict(
            (body.get('questionId') or '').strip(),
            body.get('newText') or '',
            objective=body.get('objective'),
            people_who_may_know=body.get('peopleWhoMayKnow'),
            actor=agent_id,
        )
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    if rec is None:
        return JSONResponse({'error': 'could not raise conflict'}, status_code=400)
    log_action(agent_id, 'question_conflict', {'question_id': rec['id']})
    return JSONResponse({'ok': True, 'question': rec})


@app.get('/api/conversation-brief')
async def conversation_brief(request: Request):
    # Pre-meeting briefing for ?person=Name: known / unknown / do-not-ask,
    # capped asks with attention pricing. The cap is the feature.
    person = (request.query_params.get('person') or '').strip()
    if not person:
        return JSONResponse({'error': 'person query parameter is required'}, status_code=400)
    try:
        max_questions = int(request.query_params.get('max') or 5)
    except (TypeError, ValueError):
        max_questions = 5
    channel = (request.query_params.get('channel') or 'email').strip()
    try:
        brief = prepare_conversation(person, channel=channel,
                                     max_questions=max(1, max_questions))
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    return JSONResponse(brief)


@app.get('/api/stakeholders')
async def stakeholders_list(request: Request):
    # All persons, or ?topic= routed lookup ('who can answer this?').
    topic = (request.query_params.get('topic') or '').strip()
    if topic:
        return JSONResponse({'persons': persons_knowing(topic)})
    return JSONResponse({'persons': all_persons()})


@app.post('/api/stakeholders')
async def stakeholders_record(request: Request):
    # Create or merge one person. Additive merge: a second observation adds
    # capabilities/decisions/topics, it never wipes earlier evidence.
    body = await request.json()
    is_player = _require_player_session(request)
    if is_player:
        actor = 'player'
    else:
        agent_id = (body.get('agentId') or 'unknown')
        if verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is not True:
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        actor = agent_id
    try:
        rec = record_person(
            body.get('name') or '',
            team=body.get('team'),
            capabilities=body.get('capabilities'),
            systems=body.get('systems'),
            decisions=body.get('decisions'),
            known_topics=body.get('knownTopics'),
            notes=body.get('notes'),
            actor=actor,
        )
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    return JSONResponse({'ok': True, 'person': rec})


@app.get('/api/objectives')
async def objectives_list(request: Request):
    # All objectives, or ?id=obj-... / ?title=... for one, each with its
    # computed coverage view (gaps are computed, never asserted).
    ident = (request.query_params.get('id') or '').strip()
    title = (request.query_params.get('title') or '').strip()
    if ident or title:
        obj = get_objective(ident or title)
        if obj is None:
            return JSONResponse({'error': 'unknown objective'}, status_code=404)
        return JSONResponse({'objective': obj,
                             'coverage': objective_coverage(ident or title)})
    return JSONResponse({'objectives': all_objectives()})


@app.post('/api/objectives')
async def objectives_record(request: Request):
    # Create or merge one objective with requirements. Requirements are
    # matched by text; re-recording a fuller version updates in place.
    body = await request.json()
    is_player = _require_player_session(request)
    if is_player:
        actor = 'player'
    else:
        agent_id = (body.get('agentId') or 'unknown')
        if verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is not True:
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        actor = agent_id
    try:
        rec = record_objective(
            body.get('title') or '',
            requirements=body.get('requirements'),
            question_ids=body.get('questionIds'),
            owner=body.get('owner'),
            actor=actor,
        )
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    if rec is None:
        return JSONResponse({'error': 'objective limit reached'}, status_code=400)
    return JSONResponse({'ok': True, 'objective': rec,
                         'coverage': objective_coverage(rec['id'])})
