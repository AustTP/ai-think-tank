#!/usr/bin/env python3
"""Train the band router from the think tank's own operating history.

Reads three tables and one spend map, all read-only:

- task_archive     -- the task features (taskType, room, sizeEstimate,
                      priority, budgetUsd, instruction/title length,
                      villageId), with completed_deliverable rows as a
                      secondary features source (the archive prunes old
                      tasks, the outcome ledger does not);
- grade_outcomes   -- the behavioral label per task: 'held' (the judge's
                      verdict survived the follow-up window) vs 'rework'
                      (adverse actions followed). 'pending' rows are
                      excluded from the quality model (unmatured);
- kv_spend         -- the per-task model spend (__task__/{id}: used, calls),
                      the cost label.

It trains two pure-Python artifacts and writes them plus an evaluation
report to library/analysis/band_router.json:

1. A risk model: logistic regression over one-hot features predicting
   P(outcome = rework). Pure gradient descent, no numpy, no model download.
2. A spend table: median/call-count per (taskType, room) cell.

And it proposes band deltas: for every (taskType, room) cell with enough
samples, the rework rate and median spend, and whether the cell looks
UNDER- or OVER-provisioned relative to the fleet.

HONEST LIMITS (also recorded inside the artifact):
- This is a correlational model. The tank assigns bands deterministically
  (_band_for_work_item), so the data has no counterfactuals: a cell that
  fails more may need a higher tier, or may be harder work. The delta
  proposals are hypotheses for an operator, not a policy.
- Numeric grades recorded before the 2026-11-01 grade-prompt fix are
  degenerate (every deliverable graded 0) and are excluded; the behavioral
  rework/held labels survive that bug and are the model's label.

Usage:
    python tools/train_band_router.py [--db PATH] [--out PATH]
"""
import argparse
import json
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

ROWORK = 'rework'
HELD = 'held'
MIN_CELL_SAMPLES = 8          # a (taskType, room) cell needs this many matured rows to speak
MIN_RISK_SAMPLES = 30         # below this, the risk model is not trained at all
TEST_FRACTION = 0.2
SEED = 20261101
SPEND_HIGH_USD = 0.05         # per-task model spend that starts to matter


def _load_rows(db_path=None):
    """(features dict, spend_usd, calls, outcome or None) per archived task."""
    import db_backend
    if db_path:
        conn = db_backend.connect(db_path)
    else:
        conn = db_backend.connect()
    cur = conn.cursor()

    cur.execute('SELECT id, blob FROM task_archive')
    tasks = {}
    for task_id, blob in cur.fetchall():
        try:
            tasks[task_id] = json.loads(blob)
        except Exception:
            continue
    # The archive prunes; the outcome ledger does not. completed_deliverable
    # rows fill the feature gap for recent tasks (archive wins on conflict:
    # it carries the fuller work-order features).
    try:
        cur.execute('SELECT id, blob FROM completed_deliverable')
        for task_id, blob in cur.fetchall():
            if task_id in tasks:
                continue
            try:
                d = json.loads(blob)
            except Exception:
                continue
            tasks[task_id] = {
                'taskType': d.get('taskType'),
                'room': d.get('room'),
                'sizeEstimate': d.get('sizeEstimate'),
                'title': d.get('title'),
            }
    except Exception:
        pass

    spend = {}
    try:
        cur.execute('SELECT blob FROM kv_spend')
        row = cur.fetchone()
        if row:
            data = json.loads(row[0])
            for key, val in data.items():
                if key.startswith('__task__/') and isinstance(val, dict):
                    task_id = key.split('/', 1)[1]
                    spend[task_id] = (float(val.get('used') or 0.0),
                                      int(val.get('calls') or 0))
    except Exception:
        spend = {}

    outcomes = {}
    try:
        cur.execute('SELECT task_ref, outcome FROM grade_outcomes '
                    "WHERE outcome IN ('rework', 'held')")
        for ref, outcome in cur.fetchall():
            outcomes[ref] = outcome
    except Exception:
        outcomes = {}

    rows = []
    for task_id, task in tasks.items():
        if task_id not in spend:
            continue
        used, calls = spend[task_id]
        rows.append({
            'id': task_id,
            'taskType': (task.get('taskType') or 'other')[:24],
            'room': (task.get('room') or 'none')[:24],
            'sizeEstimate': (task.get('sizeEstimate') or 'none')[:8],
            'priority': (task.get('priority') or 'normal')[:12],
            'budgeted': 1 if task.get('budgetUsd') else 0,
            'titleLen': min(len(task.get('title') or ''), 200) // 20,
            'orderLen': min(len(task.get('instructions') or ''), 4000) // 400,
            'spend': used,
            'calls': calls,
            'outcome': outcomes.get(task_id),
        })
    return rows


def _onehot(rows, fields):
    """Feature index: sorted category values per field, plus the numeric
    fields. Returns (names, encode(row) -> dict[name, value])."""
    names = []
    values = {}
    for f in fields:
        seen = sorted({r[f] for r in rows})
        for v in seen:
            names.append(f'{f}={v}')
            values[f'{f}={v}'] = (f, v)
    for f in ('budgeted', 'titleLen', 'orderLen'):
        names.append(f)
        values[f] = (f, None)
    return names, values


def _encode(row, names, values):
    x = {n: 0.0 for n in names}
    for n in names:
        f, v = values[n]
        if v is None:
            x[n] = float(row.get(f) or 0)
        elif row.get(f) == v:
            x[n] = 1.0
    return x


def _fit_logistic(rows, names, values, label_of, epochs=300, lr=0.5, l2=1e-3):
    """Pure-Python L2 logistic regression by full-batch gradient descent.
    Returns ({name: weight}, bias)."""
    xs = [_encode(r, names, values) for r in rows]
    ys = [label_of(r) for r in rows]
    w = {n: 0.0 for n in names}
    b = 0.0
    n = len(rows)
    for _ in range(epochs):
        gw = {k: 0.0 for k in w}
        gb = 0.0
        for x, y in zip(xs, ys):
            z = b + sum(w[k] * v for k, v in x.items() if v)
            p = 1.0 / (1.0 + pow(2.718281828459045, -z)) if -z < 50 else 0.0
            d = p - y
            for k, v in x.items():
                if v:
                    gw[k] += d * v
            gb += d
        for k in w:
            w[k] -= lr * (gw[k] / n + l2 * w[k])
        b -= lr * gb / n
    return w, b


def _predict(w, b, x):
    z = b + sum(w[k] * v for k, v in x.items() if v)
    return 1.0 / (1.0 + pow(2.718281828459045, -z)) if -z < 50 else 0.0


def _auc(pairs):
    """Rank-AUC for (score, is_positive) pairs; None when one class only."""
    pos = sum(1 for _, y in pairs if y == 1)
    neg = len(pairs) - pos
    if not pos or not neg:
        return None
    order = sorted(range(len(pairs)), key=lambda i: pairs[i][0])
    rank_sum = 0.0
    i = 0
    while i < len(order):
        j = i
        while j < len(order) and pairs[order[j]][0] == pairs[order[i]][0]:
            j += 1
        avg_rank = (i + j - 1) / 2.0 + 1.0
        for k in range(i, j):
            if pairs[order[k]][1] == 1:
                rank_sum += avg_rank
        i = j
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def _median(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def train(rows):
    """Returns the artifact dict: risk model, spend table, cell report,
    band-delta proposals, evaluation metrics, and the honest-limits note."""
    matured = [r for r in rows if r['outcome'] in (ROWORK, HELD)]
    random.Random(SEED).shuffle(matured)
    n_test = int(len(matured) * TEST_FRACTION)
    test, train_rows = matured[:n_test], matured[n_test:]

    names, values = _onehot(train_rows, ('taskType', 'room', 'sizeEstimate',
                                         'priority'))
    artifact = {
        'schema': 'band-router-artifact',
        'trainedAt': __import__('datetime').datetime.utcnow().isoformat() + 'Z',
        'samples': {'total': len(rows), 'matured': len(matured),
                    'train': len(train_rows), 'test': len(test)},
        'limits': [
            'correlational: bands are assigned deterministically, so the '
            'data has no counterfactuals; deltas are hypotheses, not policy',
            'labels are behavioral outcomes (rework/held), not judge scores; '
            'numeric grades before the 2026-11-01 grade-prompt fix are '
            'excluded (every deliverable graded 0)',
            'spend is the task bucket from kv_spend, which excludes shared '
            'ceremony spend (breakdowns, reviews)',
        ],
    }

    if len(train_rows) >= MIN_RISK_SAMPLES:
        w, b = _fit_logistic(
            train_rows, names, values,
            label_of=lambda r: 1 if r['outcome'] == ROWORK else 0)
        pairs = [(_predict(w, b, _encode(r, names, values)),
                  1 if r['outcome'] == ROWORK else 0) for r in test]
        auc = _auc(pairs)
        base_rate = (sum(1 for r in matured if r['outcome'] == ROWORK)
                     / max(1, len(matured)))
        ranked = sorted(((abs(v), k) for k, v in w.items()), reverse=True)[:8]
        artifact['riskModel'] = {
            'kind': 'logistic-l2',
            'bias': round(b, 4),
            'weights': {k: round(v, 4) for k, v in sorted(w.items())},
            'topDrivers': [{'feature': k, 'weight': round(w[k], 4)}
                           for _, k in ranked if abs(w[k]) > 1e-6],
            'eval': {'testAuc': round(auc, 4) if auc is not None else None,
                     'baseReworkRate': round(base_rate, 4),
                     'testRows': len(test)},
        }

    cells = {}
    for r in matured:
        key = f"{r['taskType']}/{r['room']}"
        c = cells.setdefault(key, {'n': 0, 'spends': [], 'rework': 0,
                                   'calls': []})
        c['n'] += 1
        c['spends'].append(r['spend'])
        c['calls'].append(r['calls'])
        if r['outcome'] == ROWORK:
            c['rework'] += 1
    table = {}
    for key, c in sorted(cells.items()):
        table[key] = {
            'samples': c['n'],
            'medianSpendUsd': round(_median(c['spends']), 5),
            'medianCalls': _median(c['calls']),
            'reworkRate': round(c['rework'] / c['n'], 3),
        }
    artifact['spendTable'] = table

    fleet_rework = (sum(1 for r in matured if r['outcome'] == ROWORK)
                    / max(1, len(matured)))
    proposals = []
    for key, c in sorted(cells.items()):
        if c['n'] < MIN_CELL_SAMPLES:
            continue
        rate = c['rework'] / c['n']
        med = _median(c['spends'])
        direction = None
        if rate > max(0.5, fleet_rework * 1.5) and med > SPEND_HIGH_USD:
            direction = 'raise-tier'
        elif rate <= fleet_rework * 0.5 and med < SPEND_HIGH_USD:
            direction = 'candidate-cheaper-tier'
        if direction:
            proposals.append({'cell': key, 'direction': direction,
                              'samples': c['n'],
                              'reworkRate': round(rate, 3),
                              'medianSpendUsd': round(med, 5)})
    artifact['bandDeltaProposals'] = proposals
    artifact['fleetReworkRate'] = round(fleet_rework, 4)
    return artifact


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--db', default=None, help='SQLite db path (default: the configured backend)')
    ap.add_argument('--out', default=None, help='output artifact path')
    args = ap.parse_args(argv)

    rows = _load_rows(args.db)
    if not rows:
        print('no usable rows (tasks with spend buckets); nothing to train')
        return 1
    artifact = train(rows)

    out = args.out
    if not out:
        try:
            import serve as _serve
            out = os.path.join(_serve.LIBRARY_DIR, 'analysis', 'band_router.json')
        except Exception:
            out = os.path.join(_HERE, '..', 'library', 'analysis', 'band_router.json')
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w') as f:
        json.dump(artifact, f, indent=2, sort_keys=True)
        f.write('\n')

    s = artifact['samples']
    print(f"band router artifact: {out}")
    print(f"  samples: {s['total']} with spend, {s['matured']} matured "
          f"({s['train']} train / {s['test']} test)")
    rm = artifact.get('riskModel')
    if rm:
        ev = rm['eval']
        print(f"  risk model: test AUC {ev['testAuc']} "
              f"(base rework rate {ev['baseReworkRate']})")
        for d in rm['topDrivers'][:5]:
            print(f"    {d['weight']:+.3f}  {d['feature']}")
    else:
        print(f"  risk model: not trained (needs >= {MIN_RISK_SAMPLES} matured rows)")
    print(f"  spend cells: {len(artifact['spendTable'])} "
          f"({sum(1 for c in artifact['spendTable'].values() if c['samples'] >= MIN_CELL_SAMPLES)} "
          f"with >= {MIN_CELL_SAMPLES} samples)")
    for p in artifact['bandDeltaProposals']:
        print(f"  proposal: {p['direction']}: {p['cell']} "
              f"(n={p['samples']}, rework {p['reworkRate']}, "
              f"median ${p['medianSpendUsd']})")
    return 0


if __name__ == '__main__':
    sys.exit(main())
