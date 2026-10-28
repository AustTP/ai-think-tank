

def _grade_jev_requirement(req, review):
    """Focused Jev grade for a 'jev'-type checklist requirement -- a per-
    requirement verifier, not the holistic verdict. Returns (verdict,
    confidence). The Jev contract applies: a meets/fails grade is only acted on
    at confidence >= JEV_SAFETY_CONFIDENCE; below it (or a failed call) the
    requirement is insufficient_evidence and surfaces to the player instead of
    driving an automatic revision on weak signal."""
    try:
        decision = _serve._call_openrouter_decision_sync(
            _serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice',
                        'instructions': f'You are grading one specific requirement of a deliverable. '
                                        f'Requirement: "{req.get("question", "")}". '
                                        f'The section being checked is "{req.get("section", "")}". '
                                        f'Relevant part of the deliverable: "{review[:1500]}" '
                                        f'Answer whether THIS requirement is met by THIS deliverable.',
                        'criteria': {GRADE_MEETS: 'The deliverable satisfies this specific requirement.',
                        GRADE_FAILS: 'The deliverable does not satisfy this specific requirement.',
                        GRADE_UNSURE: 'Not enough evidence in the deliverable to judge, or the question cannot be answered from it.'}}})
        choice, confidence, _cost = _serve._jev_choice(decision)
    except Exception:
        return GRADE_UNSURE, 0.0
    if choice in (GRADE_MEETS, GRADE_FAILS) and confidence >= _serve._effective_review_grade_confidence():
        return choice, confidence
    return GRADE_UNSURE, confidence


def _grade_review_checklist(checklist, review, qp, agent_id):
    """Grade every non-'human' checklist requirement against the review, one
    focused check each ('code' = mechanical/pipeline ground truth, 'jev' = a
    confidence-gated focused Jev call). 'human' requirements are never graded
    here -- they surface to the player. Returns a list of grade dicts shaped
    like grading.js's, plus the checklist entries that need a human: {'grades':
    [...], 'escalate': [(req, reason)]}."""
    grades = []
    escalate = []
    jev_graded = 0
    # Judge-vs-anchor calibration (self-deception check, ported in spirit from
    # self-evolve's selfdeception.py): a 'code'-type requirement's verdict is
    # MECHANICAL ground truth (the live quality pipeline), so a section whose
    # code requirements are UNANIMOUS is a real anchor for judging the
    # subjective 'jev' grades in that same section. The Jev grader never sees
    # the pipeline result, so agreement/disagreement is a genuine measurement
    # of the grader, not the grader echoing ground truth back. A section whose
    # code verdicts disagree is ambiguous and is NOT used as an anchor.
    code_anchors = {}
    code_verdicts = {}
    for req in checklist:
        if req.get('type') != 'code':
            continue
        section = req.get('section') or ''
        verdict = _grade_code_requirement(req, qp)
        if verdict in (GRADE_MEETS, GRADE_FAILS):
            code_verdicts.setdefault(section, set()).add(verdict)
    for section, verdicts in code_verdicts.items():
        if len(verdicts) == 1:  # pragma: no cover -- _grade_code_requirement is qp-only, all verdicts in a section are identical
            code_anchors[section] = next(iter(verdicts))
    for req in checklist:
        req_id = req.get('id') or 'req'
        section = req.get('section') or ''
        question = req.get('question') or ''
        rtype = req.get('type')
        if rtype == 'human':
            escalate.append((req, 'review decision for you'))
            continue
        if rtype == 'code':
            grades.append({'id': req_id, 'section': section, 'question': question,
                           'type': 'code', 'verdict': _grade_code_requirement(req, qp),
                           'confidence': 1.0})
        elif rtype == 'jev':
            if jev_graded >= MAX_CHECKLIST_JEV_GRADES:
                grades.append({'id': req_id, 'section': section, 'question': question,
                               'type': 'jev', 'verdict': GRADE_UNSURE,
                               'confidence': None, 'skipped': True})
                escalate.append((req, 'not graded this round (checklist spend cap)'))
                continue
            jev_graded += 1
            verdict, confidence = _grade_jev_requirement(req, review)
            grades.append({'id': req_id, 'section': section, 'question': question,
                           'type': 'jev', 'verdict': verdict, 'confidence': confidence})
            # Record the anchored sample: only definite verdicts count (an
            # UNSURE grade is the judge declining, not a wrong answer), and
            # only when the section has a unanimous mechanical anchor.
            if verdict in (GRADE_MEETS, GRADE_FAILS) and section in code_anchors:
                _serve._insert_review_calibration_sample(
                    section, verdict, confidence, code_anchors[section])
            if verdict == GRADE_UNSURE:
                escalate.append((req, f'uncertain review requirement (Jev confidence {confidence:.2f})'
                                      if confidence else 'uncertain review requirement (Jev call failed)'))
        else:
            # Unknown type -- never auto-decide on it either.
            escalate.append((req, 'review requirement with unrecognized type'))
    return {'grades': grades, 'escalate': escalate}


def _record_classified_failures(agent_id, kind, checklist_grades, escalate, review_text,
                                moonshot=False):
    """The 'sort' step's durable write: every verified-failed checklist
    requirement and every escalated review requirement is classified into the
    four-bucket taxonomy (factual error / client preference / missing
    information / style) and appended to the failure ledger in serve.py, where
    the weekly rule-mining pass turns recurring patterns into operator rule
    proposals. Best-effort: recording must never break the review itself, so a
    ledger failure is swallowed (the review result is unchanged).

    `moonshot`: a protected spare-time lane (a free spike) -- its failures are
    NEVER mined into operator rules. Bell's lesson: you can't have a moonshot
    lane if every failed experiment tightens the rules. The review outcome is
    unchanged; only the rule-mining input is skipped."""
    recorded = 0
    if moonshot:
        return recorded
    for g in (checklist_grades or []):
        if g.get('verdict') != GRADE_FAILS:
            continue
        question = g.get('question') or g.get('id') or 'requirement'
        section = g.get('section') or ''
        try:
            ftype = _serve._classify_failure(f'{question} {section}')
            _serve._record_failure(ftype, question, rule_hint=question, section=section,
                                   input_text=review_text[:800], agent_id=agent_id)
            recorded += 1
        except Exception:
            pass
    for req, _reason in (escalate or [])[:MAX_CHECKLIST_ESCALATIONS]:
        question = req.get('question') or 'requirement'
        section = req.get('section') or ''
        try:
            ftype = _serve._classify_failure(f'{question} {section}')
            _serve._record_failure(ftype, question, rule_hint=question, section=section,
                                   input_text=review_text[:800], agent_id=agent_id)
            recorded += 1
        except Exception:
            pass
    return recorded


def _perception_block(base_ctx):
    """The situational-awareness block (sim's base_ctx, see sim._build_agent_
    perception) folded into an executor's prompt. '' when the sim supplied
    nothing -- keeps prompts byte-identical for the unit tests that call
    executors bare (base_ctx=None)."""
    return f'\n\nContext from the sim:\n{base_ctx}' if base_ctx else ''


def _read_library_document(path):
    """Read a filed Library document for review-time artifact and citation checks."""
    if not isinstance(path, str) or not path.strip():
        return ''
    try:
        target = _serve._safe_library_path(path.strip())
        if not target or not os.path.isfile(target):
            return ''
        with open(target, encoding='utf-8-sig', errors='replace') as stream:
            return stream.read(2_000_000)
    except (OSError, ValueError, TypeError):
        return ''


def _run_review_content(snapshot, agent_id, task, base_ctx=None):
    """Port of tasks.js runReviewTask: a real review/QA pass -- gatherUnifiedContext,
    a skeptical critique call (probe-driven), a real screenshot visual pass, a Jev
    actionable/clean verdict, and on 'actionable' a follow-up fix task returned via
    'queueFix' (which sim._apply_content_result enqueues through the single
    read-modify-write -- never a direct DB write here)."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    is_qa = task.get('taskType') == 'qa'
    project_label = task.get('projectLabel') or ''
    backlog = task.get('title') or ''
    instructions = task.get('instructions')
    backlog_item = f'{backlog} -- {instructions}' if instructions else backlog
    name = _agent_name(snapshot, agent_id)
    # Same per-story worktree the author's coding pass used (walks reviewOf),
    # so the reviewer reads the exact tree the author wrote, not the shared one.
    sandbox_id = _review_sandbox_id(snapshot, task, agent_id)
    review_parent = (snapshot.get('tasks') or {}).get(task.get('reviewOf')) or {}
    context = _gather_unified_context(snapshot, base, key, agent_id, sandbox_id, backlog)
    parent_deliverable = str((review_parent or {}).get('deliverable') or '').strip().lower()
    is_prose_review = (parent_deliverable in ('note', 'report')
                       or (review_parent or {}).get('taskType') in ('spike', 'report', 'task'))
    if is_prose_review:
        prose = (review_parent.get('deliverableText')
                 or review_parent.get('note') or '').strip()
        doc = _read_library_document(review_parent.get('libraryPath')
                                     or review_parent.get('reportPath'))
        if doc:
            prose = (prose + '\n\n' + doc).strip()
        context = ('DELIVERABLE TEXT:\n' + (prose or '(no deliverable text attached)')
                   + '\n\nOTHER CONTEXT:\n' + context)
    # Real review work (quality pipeline + up to 2 probe-round model calls + a
    # screenshot pass + the Jev verdict) can genuinely take longer than the
    # generic 120s content ceiling _dispatch_content_work installs -- widen it
    # here so a slow-but-real verdict is not dropped by that timeout before this
    # thread finishes (a dropped review leaves the parent gate stuck at 0
    # approvals and the stuck-gate watchdog re-queues it forever). Same widening
    # the spike executor applies.
    task['workUntil'] = time.time() + 300
    review_parent = {}

    # Cut 4 -- objective pipeline evidence, run by the sandbox, NOT by reviewer
    # opinion. The approval gate in sim.py refuses to count a clean vote unless
    # this is green; here we surface it to the reviewer and force actionable on
    # a red pipeline no matter what the review text says. Lane-aware: only
    # code-class work runs the real pipeline. A gate review whose parent story
    # is non-code (spike/bug/research) skips the pipeline instead of failing
    # flake8/mypy/pytest on prose; an ad-hoc/workroom review with no parent
    # falls back to the coding-class signals the workroom itself uses.
    if task.get('reviewOf'):
        review_parent = (snapshot.get('tasks') or {}).get(task.get('reviewOf')) or {}
        pipeline_is_code_class = _sim_module._peer_coding_class(review_parent)
        # Scope the review's pipeline to the parent story's own written files
        # (persisted by _apply_content_result from the author/fix result's
        # writtenPy). A gate review must not whole-tree lint every leftover
        # file in the shared sandbox -- that let ONE agent's half-written copy
        # (e.g. a syntax-broken stealthx_brief.py) fail every concurrent
        # review. Ad-hoc reviews with no parent still fall back to the
        # whole-tree signals the workroom itself uses.
        review_scope = review_parent.get('writtenPy') or None
    else:
        pipeline_is_code_class = bool(project_label or task.get('productId')
                                      or task.get('room') == 'pressoffice')
        review_scope = None
    if pipeline_is_code_class and not is_prose_review:
        qp = _run_quality_pipeline(base, key, agent_id, sandbox_id,
                                   f'Quality gate for {("QA" if is_qa else "review")} of: {backlog_item}',
                                   scope_files=review_scope)
    else:
        qp = _skipped_pipeline_result('non-code lane -- quality pipeline skipped')

    tier_slug = _serve._coding_tier_slug() or _serve._mid_tier_slug() or _serve._low_tier_slug()
    if not tier_slug:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Tried to {("QA-test" if is_qa else "review")} "{backlog}", but no model tier is configured yet.',
                                           'ok': False})
        return
    kind = 'QA' if is_qa else 'review'
    if is_qa:
        lead = (f'You are {name}, QA-testing a teammate\'s real work for a project: {project_label}. '
                f'Current code/state:\n{context}\n\nTask: {backlog_item}\n\n'
                'Assess, as a real playtester/QA would, whether this is actually usable end to end for its intended purpose -- '
                'not just "a file that sounds related exists." Call out anything broken, missing, or incomplete, as specifically as you can. '
                'Be honest either way.')
    elif is_prose_review:
        lead = (f'You are {name}, reviewing a teammate\'s written deliverable. '
                f'Current deliverable:\n{context}\n\nTask: {backlog_item}\n\n'
                'Judge the content against the stated task: is it specific, complete, sourced where '
                'required, and actually useful? Name concrete omissions or errors, or say plainly if it '
                'meets the task. Do not ask for a shell command or a code change. Be honest either way.')
    else:
        lead = (f'You are {name}, reviewing a teammate\'s real code for a project: {project_label}. '
                f'Be a genuine, skeptical reviewer, not a rubber stamp. Current code:\n{context}\n\nTask: {backlog_item}\n\n'
                'List real, specific problems you actually see (bugs, broken logic, missing pieces), or say plainly if it genuinely '
                'looks solid. Be honest either way.')
    criteria = _acceptance_criteria(review_parent or task)
    acceptance_clause = ''
    if criteria:
        acceptance_clause = ('\n\nAcceptance criteria to verify:\n'
                             + '\n'.join(f'{cid}: {text}' for cid, text in criteria.items())
                             + '\n\n' + review_quality.ACCEPTANCE_PROMPT)
    if qp.get('skipped'):
        pipeline_clause = (f'Quality pipeline: {qp["note"]}. '
                           'This deliverable is a non-code lane (e.g. research/spike), so there is no lint/type/test bar to apply '
                           '-- judge it on the stated goal and content quality instead.')
    elif not qp['ok']:
        pipeline_clause = (f'Quality pipeline status (run live in the sandbox just now): {qp["note"]}. '
                           'This is a HARD standard the code must meet -- if it is red, that is a real problem you must flag, even if the '
                           'code otherwise looks fine. It is not optional.')
    else:
        pipeline_clause = (f'Quality pipeline status (run live in the sandbox just now): {qp["note"]}. '
                           'This is the bar the code must clear -- taking the pipeline result as given.')
    probe_clause = ''
    if not is_prose_review:
        probe_clause = (' Before answering, you may check real facts about how the ACTUAL running page behaves right now -- what a button '
                        'click or keypress actually does -- instead of guessing from the source alone. To do this, respond with ONLY a JSON '
                        'object, no markdown fences, no explanation, in exactly this shape: '
                        '{"probeRequest": {"path": "index.html", "actions": [{"type":"click","selector":"text=Some Button"}, '
                        '{"type":"keydown","key":"a"}], "probes": ["document.body.className", "typeof window.SomeGlobal"]}}\n'
                        'Action types are click ({selector}), keydown ({key}), wait ({ms}), eval ({code}). '
                        f'You can do this up to {MAX_REVIEW_PROBE_ROUNDS} times if you genuinely need to. ')
    system_prompt = (lead +
                     _perception_block(base_ctx) +
                     f'\n\n{pipeline_clause}' +
                     probe_clause +
                     # Item 6: the reviewer's final answer carries a structured
                     # verdict -- approve/send_back + the WHY (summary/checks/risks)
                     # -- so the gate decision is the reviewer's own explicit call,
                     # not a Jev guess at what the prose implies. The verdict JSON
                     # is the base; a red pipeline and verified checklist failures
                     # still override it (see below). On a probe round the verdict
                     # is requested again with the same wording.
                     'When ready, respond with your final assessment as plain text, then END with a single JSON object '
                     '(no markdown fence) in exactly this shape: '
                     '{"verdict": "approve" or "send_back", "summary": "<one short sentence>", '
                     '"checks": ["<what you actually verified>"], "risks": ["<what remains a risk>"]}. '
                     '"approve" only if the work genuinely looks solid and the quality pipeline passed; '
                     '"send_back" if you found any real, specific problem that should be fixed.'
                     + acceptance_clause
                     + '\n\n' + evidence_policy.INSTRUCTIONS)
    messages = [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': 'Give your assessment.'}]
    review = None
    probe_rounds = 0
    budget_hit = None
    while True:
        r = _serve._http_json('POST', base, '/api/chat',
                       {'model': tier_slug, 'messages': messages,
                        'max_tokens': _REVIEW_MAX_TOKENS, 'agentId': agent_id,
                        'service': project_label or task.get('productId') or '__general__',
                        'taskId': task.get('id')}, key)
        if not isinstance(r, dict) or r.get('error') or not r.get('reply'):
            budget_hit = _chat_error_result(r, f'Tried to {("QA-test" if is_qa else "review")} "{backlog}", but this task used its full model-spend budget and the review call was paused.')
            break
        reply = r['reply'].strip()
        probe_req = _parse_probe_request(reply) if probe_rounds < MAX_REVIEW_PROBE_ROUNDS else None
        if probe_req:
            probe_rounds += 1
            messages.append({'role': 'assistant', 'content': reply})
            probe_data = _serve._http_json('POST', base, '/api/page-probe',
                                    {'agentId': agent_id, 'sandboxId': sandbox_id,
                                     'path': probe_req['path'], 'actions': probe_req['actions'],
                                     'probes': probe_req['probes']}, key)
            feedback = _format_page_probe_result(probe_data)
            if probe_rounds >= MAX_REVIEW_PROBE_ROUNDS:
                feedback += f'\n\nYou have used all {MAX_REVIEW_PROBE_ROUNDS} probe rounds. Respond now with your final assessment as plain text, then the JSON verdict object as instructed.'
            messages.append({'role': 'user', 'content': feedback})
            continue
        review = reply
        break
    if not review:
        if budget_hit:
            _sim_module._store_content_result(task.get('id'), budget_hit)
            return
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Tried to {("QA-test" if is_qa else "review")} "{backlog}", but the model call didn\'t produce anything usable.',
                                           'ok': False})
        return

    if is_prose_review:
        visual = {'ok': True, 'review': '(Skipped: this is a written deliverable, not a rendered page.)'}
    else:
        visual = _review_screenshot(base, key, agent_id, sandbox_id, 'index.html',
                                    f'You\'re reviewing a real screenshot of this project\'s main page. Task: {backlog_item}. '
                                    'Describe what you actually see, and call out anything that looks visually broken -- elements in the '
                                    'wrong place, overlapping, cut off, or missing.',
                                    task_id=task.get('id'))
    visual_budget_hit = bool(visual.get('budgetExhausted'))
    full_review = (f'{review}\n\n## Visual check (real screenshot)\n\n{visual["review"]}' if visual['ok']
                   else f'{review}\n\n## Visual check\n\n(could not complete: {visual["note"]})')

    # File the review into the Library (fire-and-forget, never blocks).
    _serve._http_json('POST', base, '/api/library/file',
               {'agentId': agent_id,
                'path': f"archive/{int(time.time() * 1000)}-{task.get('taskType') or 'review'}-{task.get('id') or 'adhoc'}.md",
                'content': f'# {backlog}\n\nProject: {project_label}\nBy: {name} ({kind})\n\n{full_review}\n',
                'source': 'firsthand'}, key)

    # Item 6: the reviewer's own structured verdict is the base vote. The review
    # ends with {"verdict":"approve"|"send_back", ...}; a clean parse gives the
    # reviewer's EXPLICIT call (send_back -> actionable, approve -> clean) as the
    # primary signal. Jev still runs as the prose cross-check + the hard
    # overrides below (red pipeline, verified checklist failures) still outvote
    # it -- fail-closed stays fail-closed.
    structured_verdict, peer_summary, peer_checks, peer_risks = _parse_review_verdict(review)
    structured_actionable = structured_verdict == 'send_back'
    acceptance_rows = _parse_acceptance_checks(review)
    acceptance_assessment = _assess_acceptance(acceptance_rows, criteria) if criteria else None

    # Jev verdict: actionable -> queue a follow-up fix; clean -> nothing to do.
    verdict = 'clean'
    try:
        decision = _serve._call_openrouter_decision_sync(
            _serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice',
                        'instructions': f'A {kind} just wrote this about "{project_label}": "{full_review[:1500]}" '
                                        f'\n\nQuality pipeline ({"skipped for a non-code lane" if qp.get("skipped") else "run live"}): {qp["note"]}'
                                        f'\nDoes this identify at least one concrete, real, fixable problem (a bug, missing feature, broken integration '
                                        f'or a failed quality pipeline)? '
                                        f'Or does it say things genuinely look solid with nothing actionable?',
                        'criteria': {'actionable': 'Yes -- it names at least one real, specific problem that should be fixed '
                                                   '(including a red flake8/mypy/bandit/pytest-cov pipeline).',
                                     'clean': 'No -- it says things look solid, the quality pipeline is green, and there is nothing '
                                              'a developer needs to act on.'}}})
        verdict, _, _ = _serve._jev_choice(decision)
    except Exception:
        verdict = 'clean'  # deterministic safe fallback: don't queue a fix we can't justify
    # The reviewer's own explicit send_back outvotes a Jev read of the prose; an
    # approve (or a failed JSON parse) leaves Jev's decision as the base.
    if structured_actionable:
        verdict = 'actionable'

    # Cut 4 hard gate: a red quality pipeline can NEVER read as approval. Force
    # the vote to actionable so the story goes back for a fix even if the review
    # text or the Jev call brushed the pipeline failure aside.
    if not qp['ok']:
        verdict = 'actionable'

    # CS329A takeaway #2 -- Weaver-style verifier ensemble: grade the project's
    # checklist requirements with focused per-requirement checks (mechanical for
    # 'code', a confidence-gated Jev call for 'jev'), and let a VERIFIED failure
    # outvote the holistic verdict -- a requirement the verifier caught is a
    # real defect even if the holistic call read clean. Unsure or 'human'
    # requirements surface to the player, never auto-decided.
    checklist_grades = []
    escalated_reqs = []
    checklist = task.get('checklist') or []
    if checklist:
        graded = _grade_review_checklist(checklist, full_review, qp, agent_id)
        checklist_grades = graded['grades']
        if any(g['verdict'] == GRADE_FAILS for g in checklist_grades):
            verdict = 'actionable'
        what_checked = f'Reviewed {len(checklist)} checklist requirements during this {kind} pass'
        for req, reason in graded['escalate'][:MAX_CHECKLIST_ESCALATIONS]:
            try:
                _serve.create_escalation(reason,
                                         f'{req.get("question", "")} ({req.get("section", "") or "deliverable"})',
                                         what_checked=what_checked,
                                         look_first=req.get('question', '') or reason)
                escalated_reqs.append(req.get('id') or 'req')
            except Exception:
                pass
        # The sort step: verified failures + escalated requirements land in the
        # failure ledger (classified into the four buckets) for the weekly
        # rule-mining pass. Best-effort -- never changes the review outcome.
        _record_classified_failures(agent_id, kind, checklist_grades,
                                    graded.get('escalate', []), full_review,
                                    moonshot=bool(task.get('moonshot')))

    review_document = _read_library_document((review_parent or {}).get('libraryPath'))
    review_contract = review_quality.contract_for(review_parent or task)
    queue_fix = None
    suffix = 'nothing actionable found'
    is_gate_review = bool(task.get('reviewOf'))
    if verdict == 'actionable':
        queue_fix = {'title': f'Fix issues found in {kind} testing of "{backlog}"',
                     'room': 'pressoffice',
                     'instructions': (f'A {kind} pass of "{project_label}" found real problems -- see the Library entry just filed for '
                                      f'"{backlog}" -- fix them. Build on the existing files.'
                                      + (f'\n\nQuality pipeline is red: {qp["note"]}' if not qp['ok'] else '')),
                     'goal': project_label,
                     # Bug: this never carried
                     # productId, so every fix cycle after a rejection fell
                     # through _run_workroom_content's dispatcher (which checks
                     # productId FIRST) into the generic _run_coding_content --
                     # which is hardcoded to WORKROOM_SANDBOX_ID regardless of
                     # the product's own configured sandbox. A product with its
                     # own dedicated sandbox (e.g. per-agent-sprites) therefore
                     # only ever got its FIRST attempt built there; every
                     # rejection afterward silently started building in the
                     # shared sandbox instead, contaminating it while the real
                     # project sandbox stayed empty.
                     'productId': task.get('productId')}
        if is_prose_review:
            queue_fix['deliverable'] = parent_deliverable or 'note'
            queue_fix['taskType'] = 'task' if queue_fix['deliverable'] == 'note' else 'report'
            queue_fix['wordLimit'] = (review_parent or {}).get('wordLimit')
        if is_gate_review:
            # A gate review that rejects: the fix must go back to the SAME author
            # and re-open the SAME story (carry reviewOf), not a fresh free task.
            # `task` here is the REVIEW SUBTASK (its assignedTo is the reviewer);
            # the author is threaded in as reviewAuthorId so the fix lands on the
            # worker who BUILT the story, not on the reviewer.
            queue_fix['assignedTo'] = task.get('reviewAuthorId') or task.get('assignedTo')
            queue_fix['reviewOf'] = task.get('reviewOf')
        # The ensemble's verified FAILS are the sharpest signal the fix has --
        # name the top ones explicitly (bounded) so the fix task is steered at
        # the actual requirement it missed, not just pointed at the Library.
        failed_reqs = [g for g in checklist_grades if g.get('verdict') == GRADE_FAILS]
        if failed_reqs:
            reqs_text = '\n'.join(
                f'- {g.get("question") or g.get("id") or "requirement"}'
                + (f' (section: {g.get("section")})' if g.get('section') else '')
                for g in failed_reqs[:MAX_QUEUE_FIX_REQUIREMENTS])
            queue_fix['instructions'] += (
                f'\n\nVerified failing requirements from the review checklist:\n{reqs_text}\n'
                'Fix the work so each of these is met.')
        repairs = _document_review_repairs(
            review, review_document,
            str(review_contract.get('instructions') or '') + '\n'
            + str(review_contract.get('acceptanceCriteria') or '')) if review_document else []
        if repairs:
            queue_fix['instructions'] += (
                '\n\nEvidence-anchored repair stories from the review:\n'
                + '\n'.join(f'- {item["title"]}: {item["instructions"]} '
                             f'(acceptance: {item["acceptanceCriteria"]})'
                             for item in repairs[:4]))
        suffix = 'queued a fix'
    # SWiRL: a review that VERIFIED a checklist requirement failed leaves a
    # process-trace lesson in pending_review/skills/ (bounded + deduped by the
    # helper) so the think tank's skill-review sweep can promote it into reference
    # material -- the review writes its own durable trace, no separate crawl.
    process_trace_count = 0
    if verdict == 'actionable' and any(g.get('verdict') == GRADE_FAILS for g in checklist_grades):
        try:
            process_trace_count = _record_review_process_trace(
                base, key, agent_id, project_label, backlog, kind, name,
                checklist_grades, full_review)
        except Exception:
            process_trace_count = 0
    result = {'note': f'Filed a {kind} on "{backlog}" (text + visual), {suffix}',
              'ok': True,
              'pipelineOk': qp['ok'],
              'pipelineSummary': qp['note'],
              # Item 7: the objective quality-pipeline output rides the result so
              # the evidence-based done gate can verify the code was ACTUALLY
              # exercised by the standard gate -- never just "a reviewer said it
              # looked ok". Empty when the pipeline couldn't run.
              'evidence': _quality_pipeline_evidence(qp)}
    if review_document and not pipeline_is_code_class:
        artifact = review_quality.artifact_checks(
            review_document, review_contract, _serve._safe_library_path)
        diagnostics = result.setdefault('reviewDiagnostics', {})
        diagnostics['artifactChecks'] = artifact
        allowed_urls = (review_parent or {}).get('sourceUrls') or []
        citation_errors = url_evidence.citation_errors(review_document, allowed_urls)
        if citation_errors:
            diagnostics['citationErrors'] = citation_errors
    if checklist_grades:
        result['checklistGrades'] = checklist_grades
    if escalated_reqs:
        result['checklistEscalated'] = escalated_reqs
    if is_gate_review:
        # Relay the vote so _apply_content_result can count it on the parent.
        result['peerVerdict'] = verdict
        result['reviewContract'] = review_quality.contract_for(review_parent or task)
    # Item 6: carry the reviewer's structured verdict (its own explicit call +
    # the WHY -- summary / verified checks / remaining risks) through the result
    # channel so _apply_content_result can fold them onto the parent task and
    # the author/player can see the basis of the vote, not just the binary.
    if peer_summary or peer_checks or peer_risks:
        result['peerSummary'] = peer_summary
        result['peerChecks'] = peer_checks
        result['peerRisks'] = peer_risks
    if acceptance_assessment is not None:
        result['acceptanceAssessment'] = acceptance_assessment
    if visual_budget_hit:
        # The vision pass was refused because the task's model-spend budget is
        # exhausted -- this is NOT a review that found work needing a fix, and
        # queueing a stray fix task here would start ANOTHER unpaid lane. Drop
        # it and fail the card closed so the sim's budget branch (notify +
        # fail-closed) owns the shutdown.
        queue_fix = None
        result['budgetExhausted'] = True
        result['taskSpendUsd'] = visual.get('taskSpendUsd')
        result['taskSpendAttempts'] = visual.get('taskSpendAttempts')
        result['note'] = (f'Review text was filed, but the visual screenshot check was paused because this task used its '
                          f'full model-spend budget ({result.get("note", "")}).')
    if process_trace_count:
        result['processTraceCount'] = process_trace_count
    if queue_fix:
        result['queueFix'] = queue_fix
    _sim_module._store_content_result(task.get('id'), result)


def _wiki_context_for_task(state, task, agent_id=None):
    """Read-before-act: fold the wiki pages relevant to a task into a context
    block (markdown) the executor prepends to its model prompt, scoped to the
    task's agent's VILLAGE so a worker only ever sees their own village's wiki.
    Empty string when the wiki has nothing for this task's room -- the executor
    still runs, just with a blank knowledge context (the pre-existing
    behavior)."""
    try:
        import sim as _sim
        if agent_id:
            village_id = _sim.village_of_agent(state, agent_id)
            return _sim.inject_wiki_context(state, task, village_id=village_id)
        return _sim.inject_wiki_context(state, task)
    except Exception:
        return ''


def _design_context_for_task(state, task, agent_id=None):
    """Read-before-act: the folded design taste doc (+ raw inspiration brief
    pointers) for the project this task targets, scoped to the task's agent's
    VILLAGE. Required context for design work -- a product build or a labeled
    workroom task whose project has a taste.md must reason over it. Empty
    string when the project has no design vocabulary (the executor still runs,
    just without a design reference)."""
    try:
        project = task.get('productId') or task.get('projectLabel')
        if not project:
            return ''
        if agent_id:
            village = _sim_village_of_agent(state, agent_id)
            return _serve._design_context_for_project(project, village=village)
        return _serve._design_context_for_project(project)
    except Exception:
        return ''


def _sim_village_of_agent(state, agent_id):
    """Resolve an agent's village, defaulting to the main village when the sim
    helper or the agent is unavailable."""
    try:
        import sim as _sim
        return _sim.village_of_agent(state, agent_id) if agent_id else _sim.DEFAULT_VILLAGE
    except Exception:
        return 'main'


def _run_product_build_content(snapshot, agent_id, task, base_ctx=None):
    """Phase E: a pressoffice task targeting a PRODUCT (task['productId']).
    Reads the product's catalog record + sandbox, injects the wiki context so
    the build reasons over think tank knowledge first, runs the real coding
    pipeline against the product's sandbox, and on success RELEASES a frozen
    revision (snapshot -> library/projects/<id>/v<N>/ + passport entry). Uses
    the SWE-bench coding tier, exactly like _run_coding_content; passes through
    an optional Phase D capability handle from the product's `handles`."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    product_id = task.get('productId')
    record = (snapshot.get('products') or {}).get(product_id)
    if not record:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Tried to build product "{product_id}", but it is not in the catalog.',
                                           'ok': False})
        return
    sandbox_id = record.get('sandboxId')
    if not sandbox_id or not os.path.isdir(os.path.join(_serve.SANDBOXES_DIR, sandbox_id)):
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Tried to build "{record.get("name")}", but its sandbox ({sandbox_id}) is missing.',
                                           'ok': False})
        return
    project_label = record.get('name') or product_id
    backlog = f'Build work on {project_label} toward its next release -- spec: {record.get("spec") or "(none)"}'
    task_title = task.get('title') or backlog
    instructions = task.get('instructions')
    backlog_item = f'{task_title} -- {instructions}' if instructions else task_title
    context_summary = base_ctx or ''
    if not context_summary:
        ls = _serve._http_json('POST', _serve.SELF_BASE_URL, '/api/execute',
                        {'agentId': agent_id, 'sandboxId': sandbox_id, 'command': 'ls -la'},
                        key)
        if isinstance(ls, dict) and ls.get('allowed') and ls.get('stdout'):
            context_summary = f'Current files in the {project_label} sandbox:\n{ls["stdout"]}'
    # Read-before-act: think tank knowledge for this task's room, scoped to the
    # agent's village.
    wiki_ctx = _wiki_context_for_task(snapshot, task, agent_id)
    if wiki_ctx:
        context_summary = f'{wiki_ctx}\n\n{context_summary}'
    # Read-before-act: the folded design taste doc for this product's project
    # (required context for design work -- build against the player's taste,
    # never against a guessed "modern/clean" default), scoped to the agent's
    # village.
    design_ctx = _design_context_for_task(snapshot, task, agent_id)
    if design_ctx:
        context_summary = f'{design_ctx}\n\n{context_summary}'
    ok = _run_coding_content(snapshot, agent_id,
                             {**task, 'projectLabel': project_label,
                              'title': backlog_item, 'instructions': None,
                              'productId': product_id, 'sandboxId': sandbox_id},
                             base_ctx=context_summary)
    # On a real build success (the coding executor reports ok=True), release a
    # frozen revision. The executor's stored content-result is still consumed by
    # _task_cycle via _apply_content_result -- we do NOT take it here.
    if ok:
        _release_product_from_build(product_id, agent_id, base, key)


def _release_product_from_build(product_id, releasing_agent, base, key):
    """Freeze a released revision of a product from inside the live build flow.
    Snapshots the product's sandbox into library/projects/<id>/v<N>/ + a
    RELEASE.md, flips status -> released, chains product_released into the
    passport. This is the same release the endpoint performs, driven by the
    build itself having succeeded. Always best-effort (never raises)."""
    try:
        state = _serve.get_state_from_db()
        if not state:
            return
        import sim as _sim
        record = (state.get('products') or {}).get(product_id)
        if not record:
            return
        sandbox_id = record.get('sandboxId')
        src = os.path.join(_serve.SANDBOXES_DIR, sandbox_id) if sandbox_id else None
        if not src or not os.path.isdir(src):
            return
        revision_no, _rel = _sim.next_product_revision(state, product_id)
        dest_dir = os.path.join(_serve._product_projects_dir(product_id), f'v{revision_no}')
        os.makedirs(dest_dir, exist_ok=True)
        _serve._copy_release_snapshot(src, dest_dir)
        _serve._write_file(os.path.join(dest_dir, 'RELEASE.md'),
                    f'# {record.get("name")} -- v{revision_no}\n\n'
                    f'Released: {datetime.datetime.utcnow().isoformat()}Z\n'
                    f'By: {releasing_agent or "unknown"}\n\nBuilt and frozen from the {sandbox_id} sandbox.\n')
        _sim.product_release_record(state, product_id, revision_no,
                                    releasing_agent or 'player',
                                    'Auto-released by a successful build.',
                                    target_path=f'v{revision_no}')
        _serve.save_state_to_db(state)
        _serve.log_action(releasing_agent or 'player', 'product_released',
                   {'id': product_id, 'revision': revision_no, 'source': 'build'}, authorized=True)
        _serve._append_passport_decision('product_released', releasing_agent or 'player',
                                  {'id': product_id, 'revision': revision_no,
                                   'path': f'v{revision_no}', 'source': 'build'})
    except Exception:
        # Release on a build is best-effort; never break the task cycle over it.
        return


def _heartbeat_keepalive(task_id):
    """Keep a content run's liveness heartbeat warm across one blocking model
    call. A synthesis completion can outlast TASK_CONTENT_HB_STALE_S with no
    tool calls to bump on, and the sim then abandons a live run empty (real
    2026-10-25 task-278: report saved, 306s silent synthesis, card abandoned).
    Falls back to a no-op context manager."""
    if not task_id:
        import contextlib
        return contextlib.nullcontext()
    try:
        import sim as _sim_module
        return _sim_module.content_heartbeat_keepalive(task_id)
    except Exception:
        import contextlib
        return contextlib.nullcontext()


def _plain_completion(model, messages, max_tokens, service='spike', task_id=None,
                      village_id=None):
    """One non-tool completion call, with the same spend accrual _http_json
    self-loopback calls get from serve.py's own endpoints -- used for the
    plan/synthesize bookends of a spike, which need real (possibly extended-
    reasoning) deliberation but no tool access of their own. Returns stripped
    text, or '' on any failure (never raises -- a spike must always still
    complete via SOME path, per the notifyPlayer-on-every-outcome rule)."""
    # Item 4 gate: the spike lane accrues per-task like every other lane, so a
    # spike over its task budget must not keep paying for plan/synthesize.
    if task_id and _serve._task_budget_exhausted(task_id):
        return ''
    try:
        with _heartbeat_keepalive(task_id):
            data = _serve._call_openrouter_sync(model, messages, max_tokens)
    except Exception as e:
        print(f'[spike] plain completion failed: {e}', flush=True)
        return ''
    if not isinstance(data, dict):
        return ''
    error_detail = _serve._model_error_detail(data)
    if error_detail:
        # A provider-side error in the body is not an empty answer; name it so
        # the caller's note and the log both say what actually failed.
        print(f'[spike] plain completion error: {error_detail}', flush=True)
        return ''
    cost = (data.get('usage') or {}).get('cost', 0.0)
    if isinstance(cost, (int, float)) and cost:
        _serve._accrue_spend(service, cost, village_id=village_id)
        if task_id:
            _serve._accrue_task_spend(task_id, cost)
    try:
        return (data['choices'][0]['message']['content'] or '').strip()
    except (KeyError, IndexError, TypeError):
        return ''


def _parse_reflection(text):
    """Parse a reflection self-check's JSON reply -- same shape: tolerate
    a bare JSON object or one embedded in surrounding prose, fall back
    to a safe default on anything else.
    Confidence defaults to 1.0 (not 0.0 or 0.5) on parse failure -- a
    reflection call that itself failed is not evidence the investigation is
    going badly, and shouldn't inject a confusing nudge on top of a shaky
    signal."""
    text = (text or '').strip()
    data = None
    try:
        if text.startswith('{'):
            data = json.loads(text)
        else:
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                data = json.loads(m.group())
    except (json.JSONDecodeError, TypeError):
        data = None
    if not isinstance(data, dict):
        return 1.0, ''
    confidence = data.get('confidence')
    confidence = float(confidence) if isinstance(confidence, (int, float)) and 0.0 <= confidence <= 1.0 else 1.0
    note = data.get('note') if isinstance(data.get('note'), str) else ''
    return confidence, note


def _document_review_repairs(text, document, requirements, references=None):
    """Only turn anchored, in-scope findings into executable repair stories."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{', text or ''):
        try:
            data, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get('verdict') not in ('approve', 'send_back'):
            continue
        repairs = []
        stories = data.get('repairStories')
        if not isinstance(stories, list):
            return []
        for issue in stories[:6]:
            if not isinstance(issue, dict):
                continue
            issue = dict(issue)
            if references:
                criterion = references['criteria'].get(str(issue.get('criterionId') or ''))
                quote = references['evidence'].get(str(issue.get('evidenceId') or ''))
                if not criterion or not quote:
                    continue
                issue.update(criterion=criterion, evidenceQuote=quote)
            if not issue.get('acceptanceCriteria') and isinstance(issue.get('measurableAcceptanceCriteria'), str):
                issue['acceptanceCriteria'] = issue['measurableAcceptanceCriteria']
            if not issue.get('acceptanceCriteria') and isinstance(issue.get('measurable acceptanceCriteria'), str):
                issue['acceptanceCriteria'] = issue['measurable acceptanceCriteria']
            fields = ('title', 'criterion', 'evidenceQuote', 'instructions', 'acceptanceCriteria')
            if not all(isinstance(issue.get(k), str) and issue[k].strip() for k in fields):
                continue
            if issue['criterion'] not in requirements or issue['evidenceQuote'] not in document:
                continue
            repairs.append({k: issue[k][:4000] for k in fields})
        return repairs
    return []


def _acceptance_criteria(task):
    text = str(task.get('acceptanceCriteria') or '').strip()
    if not text:
        return {}
    lines = [re.sub(r'^\s*(?:[-*]|\d+[.)])\s*', '', line).strip()
             for line in text.splitlines() if line.strip()]
    if len(lines) == 1 and ';' in lines[0]:
        lines = [part.strip() for part in lines[0].split(';') if part.strip()]
    return {f'C{index + 1}': line for index, line in enumerate(lines[:12])}


def _parse_acceptance_checks(text):
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{', text or ''):
        try:
            data, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(data, dict) and data.get('verdict') in ('approve', 'send_back'):
            rows = data.get('acceptanceChecks')
            return [row for row in rows if isinstance(row, dict)][:12] if isinstance(rows, list) else []
    return []


def _assess_acceptance(rows, criteria):
    errors, checks, seen = [], [], set()
    for row in rows:
        cid = str(row.get('criterionId') or '')
        if cid not in criteria or cid in seen:
            errors.append('Unknown or duplicate criterion in acceptanceChecks.')
            continue
        seen.add(cid)
        evidence = row.get('evidenceIds')
        explanation = row.get('explanation')
        valid_evidence = isinstance(evidence, list) and bool(evidence) and all(
            isinstance(item, str) and item.strip() for item in evidence)
        if not valid_evidence:
            errors.append(cid + ' lacks inspected evidence with valid reference IDs.')
        if row.get('status') != 'met':
            errors.append(cid + ' is unmet or unverified.')
        if not isinstance(explanation, str) or not explanation.strip():
            errors.append(cid + ' lacks an explanation of how its evidence satisfies the criterion.')
        checks.append({'criterionId': cid, 'criterion': criteria[cid],
                       'status': row.get('status'), 'evidenceIds': evidence or [],
                       'evidence': evidence or [],
                       'explanation': explanation or ''})
    for cid in criteria.keys() - seen:
        errors.append(cid + ' was not verified.')
    return {'verified': not errors, 'checks': checks, 'errors': errors}


def _parse_review_verdict(text):
    """Item 6: parse the structured verdict block a review ends with --
    {"verdict": "approve"|"send_back", "summary": "...", "checks": [...],
    "risks": [...]}. Returns (verdict_or_None, summary, checks, risks); the
    verdict is the ONLY signal that decides (approve->clean, send_back->
    actionable), the rest is diagnostic carried to the author/player. Tolerates
    the JSON embedded in surrounding prose or wrapped in a code fence; on any
    parse failure returns (None, '', [], []) so the caller falls back to the
    Jev decision exactly as before."""
    text = (text or '').strip()
    data = None
    try:
        if text.startswith('{'):
            data = json.loads(text)
        else:
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                data = json.loads(m.group())
    except (json.JSONDecodeError, TypeError):
        data = None
    if not isinstance(data, dict):
        return None, '', [], []
    verdict = data.get('verdict')
    verdict = verdict if verdict in ('approve', 'send_back') else None
    summary = data.get('summary') if isinstance(data.get('summary'), str) else ''
    checks = data.get('checks') if isinstance(data.get('checks'), list) else []
    checks = [c for c in checks if isinstance(c, str)][:12]
    risks = data.get('risks') if isinstance(data.get('risks'), list) else []
    risks = [r for r in risks if isinstance(r, str)][:12]
    return verdict, summary, checks, risks


def _plan_requires_verification_basis(plan_text):
    """Best-effort detection of whether the PLAN decided a basis (verified/
    estimated) column was needed for a judgment CSV -- used as the trigger
    for the post-hoc disclaimer below, since the plan saying the right thing
    is not the same as the final report actually containing it (real gap
    ."""
    t = (plan_text or '').lower()
    return 'basis' in t and ('verified' in t or 'estimated' in t)


def _finding_shows_basis_column(finding_text):
    t = (finding_text or '').lower()
    return 'basis' in t or ('verified' in t and 'estimated' in t)


_INTERNAL_REVIEW_MARKERS = (
    'our own', 'internal', 'prior research', 'prior investigation', 'prior work',
    'prior spike', 'already found', 'already know', 'already investigated',
    'existing research', 'existing findings', 'existing work', 'previous spike',
    'previous investigation', 'previously found', 'another team', 'other team',
    'a different team', 'before investigating anything new', "what we've",
    "what have we", 'what do we already know', 'built by', 'done by',
)


def _spike_wants_internal_review(backlog, instructions):
    """Best-effort keyword detection of an internal-prior-art-flavored spike
    (e.g. "review Team A's approach before we build something similar") --
    same 'don't reach for a heavier tool than the problem needs' style as
    the other plan-phase detectors above. Used to FORCE search_library as
    the first tool call, not just ask the PLAN prompt to suggest it: real
    gap -- a real spike asked to "review the
    think tank's own prior research on DreyX.com" went straight to browse_page
    and reported "no existing records" despite 7+ real matching entries
    already in the Library, because the PLAN prompt's own "search_library
    first" instruction was never reliably followed. This is the exact same
    failure class already fixed once for search_web (force_first_
    tool=True alone always still reached for browse_page) -- the SAME
    proven fix: force the specific tool by name, don't just ask nicely."""
    text = f'{backlog or ""} {instructions or ""}'.lower()
    return any(marker in text for marker in _INTERNAL_REVIEW_MARKERS)
