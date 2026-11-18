"""Harness optimizer: measure-first revision of the tank's own memory
doctrine (the Self-Improving Memory lesson, 2026-11-17).

Meta FAIR's harness-optimization result: an agent's memory prompts are a
tunable harness; a meta pass that reads trajectories, proposes revisions,
and KEEPS ONLY WHAT SCORES BETTER beats hand-written designs -- and 37 of
its 44 generated revisions lost, so the wins came from measurement and
selection, not generation. The tank's harness is its wiki doctrine (the
pages `_wiki_context_for_task` injects before every task) plus the brief.

This module implements the loop with the same discipline, using parts the
tank already ships:

1. PROPOSE: the issue Engine's failure clusters are the trajectory
   diagnosis. For the top recurring symptom, propose an indexed
   symptom->fix registry page (the paper's winning shape; append-only
   monolithic notes were their diagnosed losing shape). The revision is
   recorded as an epistemic hypothesis citing the sweep evidence -- it is
   a claim about what will reduce the failures, not a fact.
2. APPLY (serve layer, operator-gated): write the page into the wiki
   (versioned; the same symptom edits the same page instead of spawning
   monolithic append-only files), then `mark_applied` records the
   provisional adoption as an epistemic DECISION with the baseline
   cluster size and a measure validation.
3. MEASURE: re-run the sweep over a fresh window and complete the
   measure validation with the delta. Improved -> keep, awaiting the
   normal review gate; not improved -> supersede recommendation (and the
   page gets reverted by the reviewer, never silently by code).

The epistemic rules carry the discipline: nothing here can set 'validated'
(it stays behind the review gate), supersession is explicit, and every
revision is traceable to the failure rows that motivated it.
"""

import json
import time

_MIN_CLUSTER_SIZE = 2
_MEASURE_WINDOW_S = 7 * 86400
_SWEEP_SOURCE_URL = 'https://tank.internal/issue-engine-sweep'


def _symptom_key(cluster):
    """Stable key for a failure cluster: the engine's symptom + failure
    action. One key == one registry page == one hypothesis."""
    return f"{cluster['symptom']}:{cluster['failureAction']}"


def _page_id_for(key):
    slug = key.replace(':', '-').replace('_', '-').lower()
    return 'harness-' + ''.join(
        ch if ch.isalnum() or ch == '-' else '-' for ch in slug)[:60].strip('-')


def _registry_body(key, cluster, causes):
    """The indexed registry template: index table first, then per-cause
    fix rows. Deliberately NOT an append-only log -- each revise replaces
    the whole body through wiki_write_page's versioning."""
    symptom = cluster['symptom']
    lines = [
        f'# Harness registry: {symptom} ({cluster["failureAction"]})',
        '',
        'Purpose: the indexed fix registry for one recurring failure '
        'signature. Consult BEFORE handling the failure; edit the matching '
        'row, never append a new monologue. Keep it short: this page is '
        'injected into worker context.',
        '',
        '| Symptom | Known fix | Status |',
        '|---|---|---|',
    ]
    for cause in causes or []:
        lines.append(f'| {str(cause.get("cause"))[:90]} | '
                     '(pending: confirm the fix, then write it here) | '
                     'open |')
    if not causes:
        lines.append(f'| {symptom} (unlocalized) | '
                     '(pending: localize from replay frames) | open |')
    lines += [
        '',
        f'Cluster facts at adoption: {cluster["size"]} failures across '
        f'{len(cluster["agents"])} agent(s) '
        f'({", ".join(cluster["agents"][:5])}).',
        'Rule: when a row\'s fix is confirmed working, mark it fixed; '
        'when the whole page stops matching reality, let the next '
        'optimizer sweep supersede it.',
    ]
    return '\n'.join(lines)


def propose(conn=None, *, actor='harness-optimizer', window_s=86400):
    """Diagnose -> propose. Returns the proposal for the serve layer to
    apply (page write + mark_applied). Deduped by symptom key: an open
    hypothesis for the same signature is returned as-is, never duplicated.
    Read-only over action_log; writes only epistemic records."""
    try:
        return _propose(conn, actor=actor, window_s=window_s)
    except Exception as exc:  # noqa: BLE001 -- a proposal must not kill callers
        return {'proposed': False, 'reason': 'optimizer_error',
                'error': str(exc)[:200]}


def _propose(conn, *, actor, window_s):
    import core.epistemics as epistemics
    import core.issue_engine as issue_engine
    now = time.time()
    if conn is None:
        import serve as _serve
        with _serve._db() as conn:
            return _propose(conn, actor=actor, window_s=window_s)
    report = issue_engine.sweep(conn, window_s=window_s, file_cards=False,
                                actor=actor)
    clusters = [c for c in report.get('clusters') or []
                if c['size'] >= _MIN_CLUSTER_SIZE]
    if not clusters:
        return {'proposed': False,
                'reason': 'no_cluster', 'failureRows': report.get('failureRows')}
    top = clusters[0]
    key = _symptom_key(top)
    # Dedupe: one open hypothesis per signature.
    for view in epistemics.query_items(owner=actor, limit=100)['items']:
        if view.get('sourceKey') == key and view['status'] not in (
                'superseded', 'contradicted'):
            return {'proposed': False, 'reason': 'already_open',
                    'itemId': view['id'], 'symptomKey': key}
    text = (f'Harness revision: add indexed registry page for '
            f'"{top["symptom"]}" failures ({top["failureAction"]}); '
            f'expect the cluster to shrink')
    rec = epistemics.record_hypothesis(text, owner=actor, actor=actor)
    if not rec.get('recorded'):
        return {'proposed': False, 'reason': rec.get('reason', 'invalid'),
                'error': rec.get('error')}
    item_id = rec['id']
    epistemics.annotate_item(item_id, {'sourceKey': key}, actor=actor)
    claim = epistemics.attach_evidence_from_source(
        item_id, _SWEEP_SOURCE_URL,
        claim_text=f'{top["failureAction"]} cluster: {top["size"]} failures '
                   f'in the last {int(window_s)}s; top causes: '
                   + '; '.join(str(c.get('cause'))[:80]
                               for c in (top.get('causes') or [])[:3]),
        actor=actor)
    return {'proposed': True, 'itemId': item_id, 'symptomKey': key,
            'symptom': top['symptom'], 'failureAction': top['failureAction'],
            'pageId': _page_id_for(key),
            'title': f'Harness registry: {top["symptom"]}',
            'body': _registry_body(key, top, top.get('causes')),
            'baseline': {'clusterSize': top['size'],
                         'agents': top['agents'], 'windowS': int(window_s),
                         'measuredAt': now},
            'claim': claim, 'claimId': claim.get('id')}


def mark_applied(proposal, page_record, *, actor):
    """Called by the serve layer after the registry page exists: record the
    provisional adoption as an epistemic decision, advance the hypothesis
    to evidenced, and propose the measure validation with the baseline."""
    import core.epistemics as epistemics
    item_id = proposal['itemId']
    baseline = proposal['baseline']
    key = proposal['symptomKey']
    val = epistemics.propose_validation(
        item_id,
        f'Re-run the failure sweep over a fresh window and confirm the '
        f'"{proposal["symptom"]}" cluster drops below {baseline["clusterSize"]}',
        needed_evidence='a follow-up sweep report for the same symptom key',
        obtainment='POST /api/harness/measure after the window',
        estimated_cost_usd=0.0, worth_it=True,
        worth_it_note='measurement is the whole point; no model spend',
        actor=actor)
    decision = epistemics.record_decision(
        f'Adopt harness revision provisionally: registry page '
        f'{proposal["pageId"]} for {key} (baseline cluster size '
        f'{baseline["clusterSize"]}); keep only if the measured cluster '
        f'shrinks, else supersede',
        rationale='measure-first adoption: 37/44 harness revisions in the '
                  'reference study lost; selection is where the wins come '
                  'from',
        item_ids=[item_id],
        validation_ids=[val['id']] if val.get('recorded') else [],
        reconsider_when=f'sweep after {_MEASURE_WINDOW_S}s or the first '
                        'cluster regrowth',
        actions=[{'text': f'Measure the {key} cluster after the window'}],
        actor=actor)
    epistemics.set_status(item_id, 'evidenced', actor=actor,
                          note='registry page applied; provisional adoption')
    epistemics.annotate_item(item_id, {
        'measureBaseline': baseline,
        'pageId': proposal['pageId'],
    }, actor=actor)
    return {'itemId': item_id, 'validationId': val.get('id'),
            'decisionId': decision.get('id'),
            'pageVersion': (page_record or {}).get('version')}


def measure(conn=None, *, actor='harness-optimizer', window_s=None):
    """Re-run the sweep and complete each open revision's measure
    validation with the delta. Never promotes or supersedes on its own:
    results go through the normal review gate. Returns a report."""
    try:
        return _measure(conn, actor=actor, window_s=window_s)
    except Exception as exc:  # noqa: BLE001
        return {'measured': False, 'reason': 'optimizer_error',
                'error': str(exc)[:200]}


def _measure(conn, *, actor, window_s):
    import core.epistemics as epistemics
    import core.issue_engine as issue_engine
    now = time.time()
    if conn is None:
        import serve as _serve
        with _serve._db() as conn:
            return _measure(conn, actor=actor, window_s=window_s)
    window = int(window_s or _MEASURE_WINDOW_S)
    report = issue_engine.sweep(conn, window_s=window, file_cards=False,
                                actor=actor)
    fresh = {_symptom_key(c): c for c in report.get('clusters') or []}
    out = {'measured': True, 'revisions': []}
    for view in epistemics.query_items(owner=actor, limit=100)['items']:
        baseline = view.get('measureBaseline')
        if not baseline or view['status'] != 'evidenced':
            continue
        key = view.get('sourceKey')
        if not key:
            continue
        current = fresh.get(key)
        cur_size = current['size'] if current else 0
        base_size = int(baseline.get('clusterSize') or 0)
        improved = cur_size < base_size
        finding = (f'Cluster "{key}": {base_size} -> {cur_size} failures '
                   f'over {window}s')
        val = epistemics.propose_validation(
            view['id'],
            f'Measurement record: {finding}. Review decides keep vs '
            'supersede',
            needed_evidence='this sweep report', obtainment='issue engine',
            estimated_cost_usd=0.0, worth_it=True,
            worth_it_note='comparison against the recorded baseline',
            actor=actor)
        entry = {'itemId': view['id'], 'symptomKey': key,
                 'baselineSize': base_size, 'currentSize': cur_size,
                 'improved': improved, 'recommendation':
                     'keep (awaiting review)' if improved else
                     'supersede (awaiting review)',
                 'measurementValidationId': val.get('id')}
        out['revisions'].append(entry)
    return out
