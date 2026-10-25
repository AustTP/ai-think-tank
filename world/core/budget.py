"""Budget overrun intelligence (the workroom budget ask).

Every card carries a per-task model-spend ceiling (sim._TASK_BUDGET_USD,
stamped onto the durable task as budgetUsd). When a card's ledger bucket
crosses that ceiling, the /api/chat gate refuses further calls, the sim's
budgetExhausted branch fails the card closed, and the owning director is
notified. This module is the analysis layer on top of that event:

- evaluate_completed_work: what the card had already produced when the
  money ran out (did any completion evidence land before the stop?);
- analyze_overrun: WHY the ceiling was hit -- too many attempts
  (retry/loop churn), an overly expensive tier (few but costly calls),
  or underestimated scope (the work simply needed more than the
  ceiling granted);
- record_overrun: the durable per-overrun history (state['budgetOverruns']);
- overrun_factor: a multiplicative scaling for FUTURE type-default
  budgets of similar work, derived from that history, so the same
  class of card stops failing on its default ceiling.

Pure functions plus plain state mutation. No network, no model calls, no
serve import -- the sim loop calls in at its budgetExhausted branch.
"""

import datetime
import statistics

# At/above this many calls, an overrun reads as retry/loop churn rather
# than a few heavy steps.
_ATTEMPTS_HEAVY_CALLS = 6
# At/below this many calls, a per-call cost this large a share of the
# card's own ceiling reads as an expensive tier eating the budget.
_TIER_HEAVY_CALLS = 3
# A call is "cheap" when its average cost is under a tenth of the
# card's own ceiling, "expensive" at a quarter of it or more.
_CHEAP_CALL_FRACTION = 0.10
_EXPENSIVE_CALL_FRACTION = 0.25
# Attempts-heavy overruns understate the real shortfall (the agent was
# still retrying when the money ran out), so they weigh 25% more when
# deriving the future-budget factor; tier-heavy weigh 10% more.
_REASON_WEIGHT = {'attempts': 1.25, 'tier': 1.10, 'scope': 1.0}
# The factor only ever raises a future ceiling, and by at most 2x.
OVERRUN_FACTOR_MAX = 2.0
# Cap the durable history so state['budgetOverruns'] stays bounded.
OVERRUN_HISTORY_MAX = 500


def analyze_overrun(task, spent=None, attempts=None):
    """Classify why `task` exhausted its model-spend budget.

    Returns a dict with:
      reason       'attempts' | 'tier' | 'scope'
      detail       one-line human explanation
      perCallUsd   average cost per model call (0.0 without telemetry)
      overrunRatio spent / budget (None without a positive ceiling)

    The heuristic is deliberately simple and deterministic: the average
    per-call cost is compared against the card's OWN ceiling.
    - 'attempts': 6+ calls averaging under a tenth of the ceiling --
      the money went to repetition (retries/loops), not one heavy step.
    - 'tier': up to 3 calls averaging a quarter of the ceiling or more
      -- a single step's price ate the ceiling (expensive model tier).
    - 'scope': anything else -- moderate calls at moderate cost, i.e.
      the work was simply bigger than the ceiling granted.
    """
    task = task or {}
    budget = task.get('budgetUsd')
    budget = budget if isinstance(budget, (int, float)) and budget > 0 else None
    spent = spent if isinstance(spent, (int, float)) else None
    attempts = attempts if isinstance(attempts, (int, float)) and attempts > 0 else None
    per_call = (float(spent) / float(attempts)) if (spent is not None and attempts) else 0.0
    ratio = (float(spent) / float(budget)) if (spent is not None and budget) else None
    if budget is None:
        reason, detail = 'scope', 'no budgetUsd recorded on the card'
    elif spent is None or attempts is None:
        reason, detail = 'scope', 'no spend/attempt telemetry for the overrun'
    elif attempts >= _ATTEMPTS_HEAVY_CALLS and per_call < budget * _CHEAP_CALL_FRACTION:
        reason = 'attempts'
        detail = (f'{int(attempts)} model calls averaging ${per_call:.4f} each -- '
                  'retry/loop churn spent the budget, not one heavy step')
    elif attempts <= _TIER_HEAVY_CALLS and per_call >= budget * _EXPENSIVE_CALL_FRACTION:
        reason = 'tier'
        detail = (f'{int(attempts)} model calls averaging ${per_call:.4f} each -- '
                  'an expensive tier ate the ceiling, not retry churn')
    else:
        reason = 'scope'
        detail = (f'{int(attempts)} calls at ${per_call:.4f} each -- the work '
                  'needed more than the ceiling granted')
    return {'reason': reason, 'detail': detail,
            'perCallUsd': round(per_call, 6),
            'overrunRatio': (round(ratio, 3) if ratio is not None else None)}


def evaluate_completed_work(task):
    """What a budget-exhausted card had already produced. The durable
    completion evidence is the task's `note` (the executor's completion
    note); a review subtask carries its verdict instead. Returns
    {'landed': bool, 'verdict': 'partial' | 'empty', 'evidence': str}.

    'partial' means some evidence landed before the money ran out -- the
    director can see the card is worth re-opening; 'empty' means nothing
    did, and the card likely needs a rethink, not just more money."""
    task = task or {}
    evidence = (task.get('note') or '').strip()
    verdict = (task.get('peerVerdict') or '').strip()
    landed = bool(evidence or verdict)
    shown = (evidence or verdict)[:500]
    return {'landed': landed,
            'verdict': ('partial' if landed else 'empty'),
            'evidence': shown}


def record_overrun(state, task, spent=None, attempts=None,
                        analysis=None, completed=None):
    """Append the overrun analysis to the durable history
    (state['budgetOverruns'], newest last, capped) and stamp the task
    with the cause for the audit trail. Returns the record. `analysis`
    and `completed` may be passed in (the sim branch computes them once
    and shares them with the director notification) or are derived here."""
    task = task or {}
    analysis = analysis or analyze_overrun(task, spent, attempts)
    completed = completed or evaluate_completed_work(task)
    record = {
        'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'taskId': task.get('id'),
        'title': (task.get('title') or '')[:120],
        'taskType': task.get('taskType') or 'code',
        'budgetBand': task.get('budgetBand') or 'standard',
        'budgetUsd': task.get('budgetUsd'),
        'spentUsd': spent,
        'attempts': attempts,
        'perCallUsd': analysis.get('perCallUsd'),
        'overrunRatio': analysis.get('overrunRatio'),
        'reason': analysis.get('reason'),
        'detail': analysis.get('detail'),
        'room': task.get('room'),
        'teamId': task.get('teamId'),
        'productId': task.get('productId'),
        'assignedTo': task.get('assignedTo'),
        'workLanded': completed.get('landed'),
    }
    history = (state or {}).setdefault('budgetOverruns', [])
    history.append(record)
    del history[:-OVERRUN_HISTORY_MAX]
    task['overrunReason'] = analysis.get('reason')
    task['overrunDetail'] = analysis.get('detail')
    task['overrunRatio'] = analysis.get('overrunRatio')
    task['workLanded'] = completed.get('landed')
    return record


def overrun_factor(state, room=None, team_id=None, product_id=None,
                        task_type=None):
    """Multiplicative scaling for FUTURE type-default budgets of similar
    work, derived from the overrun history. 1.0 with no history.

    Matching is scoped room -> team -> product -> global (the first
    scope that has records wins; `task_type` filters within a scope when
    given, so a code overrun never scales a research card). Each
    record's overrun ratio (spent/budget) is weighted by its cause --
    attempts-heavy overruns understate the shortfall, so they count 25%
    more, tier-heavy 10% more -- and the MEDIAN of the weighted ratios
    is the factor, clamped to [1.0, OVERRUN_FACTOR_MAX]. History only
    ever raises a ceiling, never lowers one."""
    history = (state or {}).get('budgetOverruns') or []
    if not history:
        return 1.0
    scopes = []
    if room:
        scopes.append(lambda r: r.get('room') == room)
    if team_id:
        scopes.append(lambda r: r.get('teamId') == team_id)
    if product_id:
        scopes.append(lambda r: r.get('productId') == product_id)
    scopes.append(lambda r: True)
    for matches_scope in scopes:
        records = [r for r in history
                   if matches_scope(r)
                   and (not task_type
                        or (r.get('taskType') or 'code') == task_type)]
        if records:
            ratios = []
            for r in records:
                ratio = r.get('overrunRatio')
                if not isinstance(ratio, (int, float)) or ratio <= 0:
                    ratio = 1.0
                weight = _REASON_WEIGHT.get(r.get('reason'), 1.0)
                ratios.append(float(ratio) * weight)
            return max(1.0, min(OVERRUN_FACTOR_MAX, statistics.median(ratios)))
    return 1.0
