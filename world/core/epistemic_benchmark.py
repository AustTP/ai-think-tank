"""The Epistemic Benchmark: a synthetic security investigation the tank can
be graded on.

The scenario: "Determine whether an AI application's prompt-injection
defenses can be monitored operationally -- from attempted attack through
downstream tool/action and incident response."

The seed is deliberately incomplete and conflicting: a current official doc,
a stale doc past its review window, telemetry that contradicts the docs,
meeting notes promising future work, an unsupported assertion repeated by
two agents from the SAME source (which must never count as independent
corroboration), a required validation, a blocked validation (missing
authority), and a decision with a resulting action.

The grader does NOT read prose. It grades the epistemic SNAPSHOT against an
answer key, category by category -- Known, Unknown, Hypothesis, Unsupported
Assumption, Contradiction, Stale Evidence, Evidence Source, Evidence
Lineage, Authoritative Source/Owner, Required Validation, Blocked
Validation, Decision, Recommended Next Action -- because the acceptance
test is calibrated knowledge, not a persuasive final report.
"""

from core.epistemics import (
    epistemic_snapshot,
    record_assertion,
    record_contradiction,
    record_decision,
    record_hypothesis,
    record_unknown,
    attach_evidence_from_source,
    propose_validation,
    review_validation,
    complete_validation,
    start_validation,
)

SCENARIO_GOAL = ("Determine whether an AI application's prompt-injection "
                 'defenses can be monitored operationally from attempted '
                 'attack through downstream tool/action and incident response')

_ACTOR = 'benchmark'

# Seeded source identities. The meeting-notes URL is cited by BOTH agents'
# assertions -- same identity, so the second attach is a repost and the
# lineage grade must see one source, not two.
_SRC_DOC_GATEWAY = 'https://docs.example.com/ai-gateway/audit-pipeline'
_SRC_DOC_RATELIMIT = 'https://docs.example.com/tool-gateway/limits'
_SRC_DOC_FILTER = 'https://docs.example.com/ai-gateway/injection-filter'
_SRC_TELEMETRY = 'https://telemetry.example.com/prod/config-snapshot'
_SRC_SIEM = 'https://siem.example.com/rules/injection-events'
_SRC_MEETING = 'https://notes.example.com/standup-2026-11-10'
_SRC_REDTICKETS = 'https://redteam.example.com/tickets'


def build_scenario():
    """Seed the epistemic ledger with the benchmark's evidence and return
    ({expected answer key}, {ids}) . Writes through the module's public
    functions; tests patch the ledger seams for isolation."""
    now_ms_fresh = 4102444800000  # 2100-01-01: 'current' for the grading run
    stale_ms = 1000               # epoch-ms 1: long past every review window

    # Known: the gateway audit pipeline, validated and reviewed, with a
    # named authoritative owner. TWO agents attach claims from the same
    # doc identity (mira's doc read and june's mirror of it): the second
    # attach must come back corroborated, never independent -- that is
    # the lineage the grader checks for.
    item_gateway = record_unknown(
        'Whether every inbound prompt is logged to the audit pipeline',
        source=_SRC_DOC_GATEWAY, actor=_ACTOR, review_after_ms=now_ms_fresh)['id']
    attach_evidence_from_source(
        item_gateway, _SRC_DOC_GATEWAY,
        'The AI gateway logs every inbound prompt to the audit pipeline',
        actor='mira')
    repost = attach_evidence_from_source(
        item_gateway, _SRC_DOC_GATEWAY,
        'Mirror note: gateway audit pipeline ingests every inbound prompt',
        actor='june')
    val_g = propose_validation(
        item_gateway, 'Cross-check audit-pipeline dashboards against a '
        'seeded test prompt', needed_evidence='audit pipeline ingest count '
        'for a seeded prompt', obtainment='SIEM query against the audit '
        'index', estimated_cost_usd=0.2, worth_it=True,
        worth_it_note='one SIEM query', actor=_ACTOR)['id']
    start_validation(val_g, cost_ok=True, actor=_ACTOR)
    complete_validation(
        val_g, 'Seeded prompt appears in the audit index once',
        source_url=_SRC_SIEM, claim_text='Audit index ingests seeded '
        'gateway prompts', actor=_ACTOR)
    review_validation(val_g, True, reviewer='verifier',
                      authoritative_owner='platform-oncall')

    # Stale: the rate-limit doc was true once; its review window passed and
    # nothing re-verified it, so it must not count as trusted current
    # knowledge (the record itself stays readable).
    item_rate = record_unknown(
        'The tool gateway rate limit is 100 requests per minute',
        source=_SRC_DOC_RATELIMIT, actor=_ACTOR, review_after_ms=stale_ms)['id']
    attach_evidence_from_source(
        item_rate, _SRC_DOC_RATELIMIT,
        'Rate limiting on the tool gateway is 100 requests per minute',
        actor=_ACTOR, review_after_ms=stale_ms)

    # Contradiction: docs say filter v2 is deployed; the prod config
    # snapshot says it is not. Open until resolved; NEITHER side is
    # established while it stands.
    item_filter = record_unknown(
        'Whether prompt-injection filter v2 is deployed in production',
        source=_SRC_DOC_FILTER, actor=_ACTOR, review_after_ms=now_ms_fresh)['id']
    attach_evidence_from_source(
        item_filter, _SRC_DOC_FILTER,
        'Prompt-injection filter v2 is deployed in production',
        actor=_ACTOR)
    attach_evidence_from_source(
        item_filter, _SRC_TELEMETRY,
        'Filter v2 signature absent from the production config snapshot',
        actor=_ACTOR)
    contradiction_id = record_contradiction(
        item_filter, _claim_for(_SRC_DOC_FILTER),
        _claim_for(_SRC_TELEMETRY), actor=_ACTOR,
        note='docs vs prod config snapshot')['id']

    # Unknown with a REQUIRED validation: blocked-injection events and the
    # incident-response rota.
    item_rota = record_unknown(
        'Whether blocked prompt-injection attempts reach the '
        'incident-response rota', source=_SRC_MEETING, actor=_ACTOR,
        review_after_ms=now_ms_fresh)['id']
    propose_validation(
        item_rota, 'Trace one blocked attempt end to end through SIEM to '
        'the IR rota', needed_evidence='a blocked-attempt event id visible '
        'in the IR rota channel', obtainment='replay a lab attempt against '
        'the staging gateway', estimated_cost_usd=0.5, worth_it=True,
        worth_it_note='one lab replay', actor=_ACTOR)

    # Unknown with a BLOCKED validation: red-team ticket export needs an
    # authority nobody in the tank holds.
    item_red = record_unknown(
        'How many attempted prompt injections the red team logged this '
        'quarter', source=_SRC_REDTICKETS, actor=_ACTOR,
        review_after_ms=now_ms_fresh)['id']
    propose_validation(
        item_red, 'Pull the red-team ticket export for the quarter',
        needed_evidence='ticket export with injection attempt counts',
        obtainment='red-team lead grants export access', blocked=True,
        blocked_reason='no granted authority to read red-team tickets',
        estimated_cost_usd=0.0, worth_it=None,
        worth_it_note='only worth it if the rota question is live',
        actor=_ACTOR)

    # Unsupported assumption, repeated by two agents from the SAME source:
    # one item, two askers, still unknown -- repetition never establishes.
    item_pager = record_assertion(
        'The SOC pager auto-escalates prompt-injection alerts',
        source=_SRC_MEETING, owner='mira', actor='mira')['id']
    record_assertion(
        'The SOC pager auto-escalates prompt-injection alerts',
        source=_SRC_MEETING, owner='june', actor='june')

    # Hypothesis from meeting notes: coverage gaps limited to one hop.
    record_hypothesis(
        'Monitoring coverage gaps are limited to the tool-execution hop',
        owner='sam', actor='sam')

    # Decision with a resulting action and a reconsideration trigger.
    record_decision(
        'Treat the SIEM injection-event alert as the interim monitoring '
        'signal until the rota question resolves',
        rationale='the audit pipeline is established and the alert already '
                  'fires on it; waiting for the full trace blocks '
                  'incident response for no gain',
        item_ids=[item_gateway],
        reconsider_when='the rota validation completes or the filter '
                        'contradiction resolves',
        actions=['Draft a detection-rule review with platform-oncall'],
        actor='theo')

    expected = {
        'Known': ['Whether every inbound prompt is logged to the audit '
                  'pipeline'],
        'Unknown': ['Whether blocked prompt-injection attempts reach the '
                    'incident-response rota',
                    'How many attempted prompt injections the red team '
                    'logged this quarter'],
        'Hypothesis': ['Monitoring coverage gaps are limited to the '
                       'tool-execution hop'],
        'Unsupported Assumption': ['The SOC pager auto-escalates '
                                   'prompt-injection alerts'],
        'Contradiction': ['Whether prompt-injection filter v2 is deployed '
                          'in production'],
        'Stale Evidence': ['The tool gateway rate limit is 100 requests '
                           'per minute'],
        'Evidence Source': [_SRC_DOC_GATEWAY],
        'Evidence Lineage': [f'corroborated:{_SRC_DOC_GATEWAY}'],
        'Authoritative Source/Owner': ['platform-oncall'],
        'Required Validation': ['Trace one blocked attempt end to end '
                                'through SIEM to the IR rota'],
        'Blocked Validation': ['Pull the red-team ticket export for the '
                               'quarter'],
        'Decision': ['Treat the SIEM injection-event alert as the interim '
                     'monitoring signal until the rota question resolves'],
        'Recommended Next Action': ['Draft a detection-rule review with '
                                    'platform-oncall'],
    }
    ids = {'gateway': item_gateway, 'rateLimit': item_rate,
           'filter': item_filter, 'contradiction': contradiction_id,
           'rota': item_rota, 'redTeam': item_red, 'pager': item_pager,
           'gatewayRepost': repost}
    return expected, ids


def _claim_for(identity):
    """The first claim id in the evidence ledger with this resolved
    identity -- the benchmark's way of naming a side of a contradiction
    without reaching past the public API."""
    import core.evidence as evidence
    for claim in evidence.list_claims():
        if claim.get('identity') == identity:
            return claim['id']
    raise RuntimeError(f'benchmark seed lost its claim for {identity!r}')


_GRADE_CATEGORIES = (
    'Known', 'Unknown', 'Hypothesis', 'Unsupported Assumption',
    'Contradiction', 'Stale Evidence', 'Evidence Source', 'Evidence Lineage',
    'Authoritative Source/Owner', 'Required Validation', 'Blocked Validation',
    'Decision', 'Recommended Next Action')


def _norm(text):
    return ' '.join(str(text or '').lower().split())


def _candidates(snapshot, now_ms):
    """Everything gradeable, grouped by benchmark category. Lineage
    candidates are 'corroborated:<identity>' strings: the grade asks
    whether the snapshot EXPOSES that two same-source attachments were
    treated as one source, not whether prose mentioned it."""
    known_ids = {v['id'] for v in snapshot['established']}
    out = {cat: [] for cat in _GRADE_CATEGORIES}
    for view in snapshot['established']:
        out['Known'].append(view['text'])
        out['Authoritative Source/Owner'].extend(
            [view.get('authoritativeOwner') or '', view.get('owner') or ''])
        for src in view.get('sources') or []:
            out['Evidence Source'].append(src.get('identity') or '')
    for view in snapshot['unknowns']:
        out['Unknown'].append(view['text'])
    for view in snapshot['asserted']:
        if view.get('kind') == 'hypothesis':
            out['Hypothesis'].append(view['text'])
        else:
            out['Unsupported Assumption'].append(view['text'])
    for view in snapshot['disputed']:
        out['Contradiction'].append(view['text'])
    for cx in snapshot['openContradictions']:
        out['Contradiction'].append(cx.get('text') or '')
    for view in snapshot['stale']:
        out['Stale Evidence'].append(view['text'])
    for view in snapshot['asserted'] + snapshot['established'] + \
            snapshot['disputed']:
        for src in view.get('sources') or []:
            if src.get('identity') and not src.get('independent'):
                out['Evidence Lineage'].append(
                    f"corroborated:{src['identity']}")
    for v in snapshot['validationsRequired']:
        out['Required Validation'].append(v['procedure'])
    for v in snapshot['validationsBlocked']:
        out['Blocked Validation'].append(v['procedure'])
    for d in snapshot['decisions']:
        out['Decision'].append(d['text'])
    for a in snapshot['openActions']:
        out['Recommended Next Action'].append(a['text'])
    return {k: [c for c in v if c] for k, v in out.items()}, known_ids


def grade_snapshot(snapshot, expected, now_ms=None):
    """Grade a snapshot against the answer key. Matching is normalized
    substring containment -- the key names the fact, the snapshot names it
    its own way. Returns per-category matched/missed plus an overall score
    (matched / expected, averaged over categories that expect anything)."""
    candidates, _known_ids = _candidates(snapshot, now_ms)
    report = {'categories': {}, 'score': None}
    matched_total = expected_total = 0
    for cat in _GRADE_CATEGORIES:
        want = [_norm(w) for w in (expected.get(cat) or [])]
        have = [_norm(c) for c in candidates.get(cat) or []]
        matched = [w for w in want
                   if any(w in c or c == w for c in have)]
        missed = [w for w in want if w not in matched]
        report['categories'][cat] = {
            'expectedCount': len(want), 'matchedCount': len(matched),
            'matched': matched, 'missed': missed}
        matched_total += len(matched)
        expected_total += len(want)
    report['score'] = round(matched_total / expected_total, 4) \
        if expected_total else None
    return report


def run_benchmark():
    """Seed the scenario and grade the tank's snapshot against the key.
    The number is calibrated-knowledge coverage, not prose quality."""
    expected, _ids = build_scenario()
    snapshot = epistemic_snapshot()
    return grade_snapshot(snapshot, expected)
