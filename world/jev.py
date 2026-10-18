"""JEV decision engine (policy-as-code classifier plumbing).

Extracted from serve.py (Phase 0c, self-modification split): this module owns
the MODIFIABLE half of the JEV contract -- the quorum sampling policy, the
choice parser, and the per-escalation-kind floor table. serve.py keeps the
DEPLOY-coupled half: the OpenRouter call + decision tape
(_call_openrouter_decision_sync), the live model chain (_jev_model and
_decision_model_chain, DB `settings` backed), and the live calibration floor
(_effective_safety_confidence).

Call/patch contract (must hold for every future edit of this file):
  * serve.py re-exports every public name defined here, so `serve.X` keeps
    resolving and every existing test patching serve.X keeps working.
  * Inside this module, always reach serve-side state through a lazy
    `import serve as _serve` at CALL time (the bank.py/evidence.py pattern):
    serve is not importable while this module is being imported, and the
    call-time lookup is what makes "edit-in-place applies without restart"
    work for serve-owned knobs (settings rows, model chain).
  * The scalars and the floor table below are OWNED here: jev-internal code
    reads its own globals, so a self-edit of this file takes effect when this
    module is reloaded -- the self_edit apply flow (re)imports jev and then
    refreshes serve's re-exports while the tank is dormant.
"""

import asyncio

# Any safety gate that gets a Jev "allow/approve" below this confidence is
# treated as "unsure" and escalated to a human instead of acted on -- the
# intended Jev contract. Only safety gates use this; routine decisions just
# record confidence for observability.
JEV_SAFETY_CONFIDENCE = 0.6

# Quorum sensing, ported from real Temnothorax ant nest-site
# selection: colonies pool multiple independent scouts' judgments SPECIFICALLY
# to overcome errors inherent in any one individual's decision -- and this is
# a documented, LIVE-confirmed problem here, not a hypothetical one: the same
# DreyX URL got a confident 0.87 allow on one run and a low-confidence 0.56
# escalate-and-deny on another, pure classifier variance. Unlike a real ant,
# every extra sample costs real money (see serve.SPEND_CAP_USD), so this only
# re-samples when the FIRST call is already ambiguous (a low-confidence
# allow) -- a confident allow or any firm block is trusted on one sample,
# exactly as before. On ambiguity, up to QUORUM_SAMPLE_SIZE total independent
# samples are taken; allow wins only with QUORUM_MIN_AGREEING agreeing votes.
QUORUM_SAMPLE_SIZE = 3
QUORUM_MIN_AGREEING = 2


def _jev_choice(data):
    ans = (data or {}).get('answers', {})
    q = next(iter(ans.values())) if ans else {}
    choice = q.get('choice') if isinstance(q, dict) else None
    confidence = q.get('confidence') if isinstance(q, dict) else None
    usage = (data or {}).get('usage', {}) or {}
    if isinstance(confidence, (int, float)) and 0.0 <= confidence <= 1.0:
        confidence = float(confidence)
    else:
        # Fail closed: a missing, non-numeric, or out-of-range confidence is
        # treated as ZERO trust, not full trust. A malformed or truncated model
        # response (or an injected one that omits the field) must not sail past
        # the low-confidence human-escalation floors in _jev_safety_gate and
        # the director gate by looking maximally confident.
        confidence = 0.0
    cost = usage.get('cost', 0.0)
    return choice, confidence, float(cost) if isinstance(cost, (int, float)) else 0.0


async def _jev_quorum_decision(instructions, criteria):
    """Shared by every Jev safety-gated call site (browse, download, curl,
    sandbox_download, sandbox_save_page, access_request, execute/pipeline's
    _classify_command) -- replaces each site's own single
    _call_openrouter_decision_sync + _jev_choice call. Returns (decision,
    confidence, total_cost, trace_id) -- the same tuple shape _jev_choice
    returns plus the decision's trace_id (so the caller can stamp
    it on the resulting action_log rows, making the decision chain
    queryable), so every existing call site's downstream _jev_safety_gate
    call is unchanged except threading that trace_id. Fails closed exactly
    as every call site already did: an unreachable classifier is not consent
    to skip the gate."""
    import serve as _serve

    async def sample():
        data = await asyncio.to_thread(
            _serve._call_openrouter_decision_sync, _serve._jev_model(),
            {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice', 'instructions': instructions, 'criteria': criteria}},
        )
        decision, confidence, cost = _serve._jev_choice(data)
        # trace_id only exists on a real decision dict; a mocked/broken caller
        # (tests, an upstream shape change) must never hand the gate a
        # non-bindable trace_id -- None stamps nothing and still logs fine.
        trace_id = data.get('trace_id') if isinstance(data, dict) else None
        return decision, confidence, cost, trace_id

    try:
        decision, confidence, cost, trace_id = await sample()
    except Exception:
        return None, 1.0, 0.0, None

    if decision not in ('allow', 'approve') or confidence >= _serve._effective_safety_confidence():
        return decision, confidence, cost, trace_id  # firm block, or already-confident -- one sample is enough

    total_cost = cost
    votes = [(decision, confidence)]
    for _ in range(QUORUM_SAMPLE_SIZE - 1):
        try:
            d, c, extra_cost, _ = await sample()
        except Exception:
            continue  # a failed re-sample just isn't a vote either way
        total_cost += extra_cost
        votes.append((d, c))

    agreeing = [c for d, c in votes if d in ('allow', 'approve')]
    if len(agreeing) >= QUORUM_MIN_AGREEING:
        # The strongest agreeing confidence, not a diluted average with the
        # votes that disagreed -- a real quorum forms around its scouts'
        # own assessed quality, not a blend against the ones who left.
        return 'allow', max(agreeing), total_cost, trace_id
    # No quorum reached -- report the ORIGINAL low-confidence result so
    # _jev_safety_gate's escalation message stays accurate (this really was
    # sampled multiple times and stayed unsure, not a fabricated one-shot).
    return decision, confidence, total_cost, trace_id


def _jev_quorum_choice_sync(instructions, criteria):
    """gap caught: quorum sampling above only ever covered
    the SAFETY gates (a confirmed real problem -- the same DreyX URL got a
    confident 0.87 allow one run, a low-confidence 0.56 escalate-and-deny
    another). The exact same single-noisy-sample problem applies equally to
    serve's own routine-but-consequential ROUTING deciders --
    _classify_request_lane_default (story vs spike -- peer-gated or not),
    _classify_team_default, _classify_room_default, _classify_product_
    default, the escalation delegated-approval decision, and the peer-report
    worker picker -- none of which had ever been protected. All six are
    synchronous (called from background threads, not the async endpoints
    the safety gates live in), hence a plain sync counterpart rather than
    reusing _jev_quorum_decision directly.

    Unlike the safety-specific version, there is no 'a firm block is always
    trusted' asymmetry to lean on -- these are multi-way choices (a lane, a
    team, a room, a worker index) where every option is equally worth
    getting right, not an allow/block binary with a safe default. So this
    resamples on ANY low-confidence result regardless of which option was
    picked, and resolves by PLURALITY VOTE among the resamples (the option
    most independent samples agreed on wins; ties broken by the higher
    confidence), using the winning option's own highest confidence as the
    reported one -- same 'don't dilute a real quorum with the votes that
    disagreed' reasoning as the safety version. Returns (decision,
    confidence, total_cost), the same 3-tuple shape _jev_choice returns, so
    every caller's existing post-processing (matching against its own
    candidate list, falling back on None/an unrecognized choice) is
    unchanged."""
    import serve as _serve

    def sample():
        data = _serve._call_openrouter_decision_sync(
            _serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice', 'instructions': instructions, 'criteria': criteria}})
        return _serve._jev_choice(data)

    try:
        decision, confidence, cost = sample()
    except Exception:
        return None, 1.0, 0.0

    if confidence >= _serve._effective_safety_confidence():
        return decision, confidence, cost  # already confident -- one sample is enough

    total_cost = cost
    votes = [(decision, confidence)]
    for _ in range(QUORUM_SAMPLE_SIZE - 1):
        try:
            d, c, extra_cost = sample()
        except Exception:
            continue  # a failed re-sample just isn't a vote either way
        total_cost += extra_cost
        votes.append((d, c))

    tally = {}
    for d, c in votes:
        if d is None:
            continue
        tally.setdefault(d, []).append(c)
    if not tally:
        return decision, confidence, total_cost  # nothing usable at all -- report the original
    winner_id, winner_confidences = max(tally.items(), key=lambda kv: (len(kv[1]), max(kv[1])))
    return winner_id, max(winner_confidences), total_cost


# Composite multi-signal trust gate for the DIRECTOR auto-approval path.
# The old check was single-signal: a lone Jev `confidence` above the floor let the
# delegated director auto-approve/deny ANY pending escalation. That gave a
# high-confidence Jev answer the same authority regardless of (a) how risky the
# escalation's *kind* was, or (b) how flaky the Jev decision model had been.
#
# The composite score is: effective = confidence - error_penalty, gated
# per-escalation-KIND by a risk floor. The category override is the sharp
# part: a safety-critical kind's floor can be RAISED above the global default
# (or set to 1.0 = never auto-approve), so a confident-but-wrong Jev answer
# still lands in the human's inbox for that class of escalation. The gate
# itself (and the judge cross-check) stays in protected serve.py; this DATA
# table is what self-edits may tune.
ESCALATION_KIND_RISK = {
    # kind -> {'floor': min composite confidence to auto-approve, 1.0 = never}
    # "unsure safety decision" is a genuinely uncertain one by construction (the
    # classifier already said "not clearly blocked"); raise its floor above a
    # routine approval.
    'unsure safety decision':  {'floor': 0.85},
    # A human's explicit "blocked" verdict on a command or pipeline step is the
    # strongest signal the think tank produces that the action is dangerous. A
    # director-LLM re-framing it as benign should NOT override that -- 1.0 =
    # always human.
    'blocked command':        {'floor': 1.0},
    'blocked pipeline step':  {'floor': 1.0},
    # A blocked browser action is the same class of decision as a blocked
    # command -- Jev said no, and the only acceptable override is the human
    # who owns the keyboard. Same floor as blocked command.
    'blocked browser action': {'floor': 1.0},
    # Agent-driven "could not resolve a review requirement": ordinary routine
    # approval/denial is fine at the default floor.
    'unresolved review requirement': {'floor': JEV_SAFETY_CONFIDENCE},
    # Agent-requested internet-allowlist addition. Operator decision
    # (2026-10-13): agents' #1 complaint was that these waited on an email
    # that could sit for hours -- the operator wants requests escalated to
    # the DIRECTORS first, exactly like 'allowlist candidate'. So the floor
    # drops from 1.0 to the default delegable bar: the director loop decides
    # within a pass (30s cadence) through the same composite+judge
    # cross-check every delegable kind gets, the grant applies on approve
    # (see the loop's side-effect below), and the human is only emailed when
    # the director gives up (ask cap / judge-drift circuit) -- same fallback
    # as every other delegable kind.
    'allowlist request': {'floor': JEV_SAFETY_CONFIDENCE},
}
ESCALATION_DEFAULT_FLOOR = JEV_SAFETY_CONFIDENCE  # unknown kinds keep the old bar
ESCALATION_ERROR_PENALTY_STEP = 0.10               # composite hit per consecutive Jev error
ESCALATION_ERROR_PENALTY_CAP = 0.40                # ... capped so one bad streak can't zero a confident call