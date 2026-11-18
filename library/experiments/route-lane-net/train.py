"""Train the route-lane shadow net (foundry pilot 1).

Behavioral clone of the live triage cascade (_classify_request_lane_default:
Jev quorum over _ROUTING_LANES). Training data: every `route_classified`
action in the action log (lane + text[:200], one row per Telegram-routed
message). Pure Python (the tank venv has no numpy), hashed bag-of-words
features, one softmax layer. The artifact this writes -- nets/route-lane-v1.json
-- is loaded at request time by serve part 476_route_shadow_net.py, which
NEVER decides routing: it only logs route_shadow_classified beside the live
lane so agreement can be measured passively.

Net spec (both sides must implement exactly):
  text     = the message truncated to the same 200 chars the action log
             stores (inference must never see a longer prefix than training
             did -- unseen tokens would inflate the L2 norm)
  tokens   = re.findall('[a-z0-9]+', text.lower()), kept when len >= 2
  features = unigrams + bigrams (adjacent token pairs)
  bucket   = int(md5(feature.encode()).hexdigest()[:8], 16) % buckets
  vector   = counts, then L2-normalized (zero vector stays zero)
  score    = W[bucket][lane] sums + bias[lane]; argmax wins

Run: python3 train.py [dataset.json] [out.json]
"""
import hashlib
import json
import math
import random
import re
import sys
from collections import Counter

LANES = ['ask', 'schedule', 'schedule_once', 'spike', 'story', 'incident', 'unclear']
BUCKETS = 256
EPOCHS = 400
LR = 0.5
L2 = 1e-4
SEED = 20261118
TOKEN_RE = re.compile('[a-z0-9]+')


def features(text, buckets=BUCKETS):
    toks = [t for t in TOKEN_RE.findall((text or '').lower()[:200])
            if len(t) >= 2]
    grams = toks + [f'{a} {b}' for a, b in zip(toks, toks[1:])]
    counts = Counter()
    for g in grams:
        bucket = int(hashlib.md5(g.encode()).hexdigest()[:8], 16) % buckets
        counts[bucket] += 1.0
    norm = math.sqrt(sum(v * v for v in counts.values()))
    if norm > 0:
        counts = Counter({b: v / norm for b, v in counts.items()})
    return counts


def scores(vec, W, bias):
    out = list(bias)
    for b, v in vec.items():
        row = W[b]
        for k in range(len(out)):
            out[k] += v * row[k]
    return out


def predict(vec, W, bias):
    sc = scores(vec, W, bias)
    return max(range(len(sc)), key=lambda k: sc[k])


def train(examples, seed=SEED):
    n_classes = len(LANES)
    W = [[0.0] * n_classes for _ in range(BUCKETS)]
    bias = [0.0] * n_classes
    data = [(features(t), LANES.index(lane)) for lane, t in examples]
    rng = random.Random(seed)
    for _ in range(EPOCHS):
        rng.shuffle(data)
        for vec, y in data:
            sc = scores(vec, W, bias)
            mx = max(sc)
            exp = [math.exp(s - mx) for s in sc]
            z = sum(exp)
            grad = [exp[k] / z - (1.0 if k == y else 0.0) for k in range(n_classes)]
            for b, v in vec.items():
                row = W[b]
                for k in range(n_classes):
                    row[k] -= LR * (grad[k] * v + L2 * row[k])
            for k in range(n_classes):
                bias[k] -= LR * grad[k]
    return W, bias


def kfold(examples, k=5, seed=SEED):
    """Row-level folds, kept only to report the leakage-inflated number next
    to the honest one in the artifact's meta."""
    rng = random.Random(seed)
    order = list(range(len(examples)))
    rng.shuffle(order)
    folds = [[] for _ in range(k)]
    for i, idx in enumerate(order):
        folds[i % k].append(idx)
    return folds


def group_kfold(examples, k=5, seed=SEED):
    """Group-aware folds: rows sharing a normalized text (repeated asks, the
    log's own near-duplicates) never split across folds -- the same text in
    train and test at once is leakage, and it inflated the naive CV by about
    10 points (0.905 vs the honest 0.80)."""
    groups = {}
    for i, (lane, text) in enumerate(examples):
        groups.setdefault(' '.join(text.lower().split()), []).append(i)
    keys = sorted(groups, key=lambda g: (min(groups[g]), g))
    rng = random.Random(seed)
    rng.shuffle(keys)
    by_class = {}
    for g in keys:
        by_class.setdefault(examples[groups[g][0]][0], []).append(g)
    folds = [[] for _ in range(k)]
    i = 0
    for lane in sorted(by_class):
        for g in by_class[lane]:
            folds[i % k].append(g)
            i += 1
    return folds, groups


def main():
    ds_path = sys.argv[1] if len(sys.argv) > 1 else 'dataset.json'
    out_path = sys.argv[2] if len(sys.argv) > 2 else '../../nets/route-lane-v1.json'
    rows = json.load(open(ds_path))
    examples = [(r['lane'], r['text']) for r in rows]
    counts = Counter(lane for lane, _ in examples)
    W, bias = train(examples)
    fit_right = sum(1 for lane, t in examples
                    if LANES[predict(features(t), W, bias)] == lane)
    # Honest headline: group-aware CV (duplicate texts never split across
    # folds). The naive row-fold number is reported alongside it so the
    # artifact documents its own leakage history.
    g_folds, groups = group_kfold(examples)
    g_accs = []
    for f in range(len(g_folds)):
        test_idx = [i for g in g_folds[f] for i in groups[g]]
        train_idx = [i for other in range(len(g_folds)) if other != f
                     for g in g_folds[other] for i in groups[g]]
        tr = [examples[i] for i in train_idx]
        W_f, b_f = train(tr)
        right = sum(1 for i in test_idx
                    if LANES[predict(features(examples[i][1]), W_f, b_f)]
                    == examples[i][0])
        g_accs.append(right / len(test_idx) if test_idx else 1.0)
    n_folds = kfold(examples)
    n_accs = []
    for f in range(len(n_folds)):
        tr = [examples[i] for i in n_folds[f]]
        W_f, b_f = train(tr)
        test_idx = [i for other in range(len(n_folds)) if other != f
                    for i in n_folds[other]]
        right = sum(1 for i in test_idx
                    if LANES[predict(features(examples[i][1]), W_f, b_f)]
                    == examples[i][0])
        n_accs.append(right / len(test_idx) if test_idx else 1.0)
    out = {
        'version': 2,
        'lanes': LANES,
        'buckets': BUCKETS,
        'spec': ('text = message truncated to the same 200 chars the action '
                 'log stores; tokens = [a-z0-9]+ runs of text.lower() kept '
                 'when len>=2; features = unigrams + adjacent bigrams; '
                 'bucket = int(md5(feature)[:8],16) %% buckets; L2-normalized '
                 'counts; score = W[bucket][lane] sums + bias[lane]; '
                 'argmax wins'),
        'weights': [[round(v, 4) for v in row] for row in W],
        'bias': [round(v, 4) for v in bias],
        'meta': {
            'trainedAt': '2026-11-18',
            'examples': len(examples),
            'uniqueTexts': len(groups),
            'classCounts': dict(counts),
            'cvFolds': 5,
            'cvMode': 'group-aware (duplicate texts never split across folds)',
            'cvAccuracies': [round(a, 3) for a in g_accs],
            'cvMeanAccuracy': round(sum(g_accs) / len(g_accs), 3),
            'naiveCvMeanAccuracy': round(sum(n_accs) / len(n_accs), 3),
            'naiveCvCaveat': ('row-level folds leak duplicate texts between '
                              'train and test; inflated by ~10 points'),
            'fullFitAccuracy': round(fit_right / len(examples), 3),
            'unseenLanes': ['schedule', 'schedule_once', 'incident'],
            'unseenLanesNote': ('zero training rows; the net cannot predict '
                                'these lanes until live traffic provides '
                                'labeled examples'),
            'source': 'action_log route_classified (21d retention window)',
            'role': 'shadow only -- never decides routing (476_route_shadow_net.py)',
        },
    }
    with open(out_path, 'w') as fh:
        json.dump(out, fh, indent=1)
    print(f'examples={len(examples)} unique={len(groups)} classCounts={dict(counts)}')
    print(f'group-aware cv accs={[round(a, 3) for a in g_accs]} '
          f'mean={out["meta"]["cvMeanAccuracy"]}')
    print(f'naive cv mean={out["meta"]["naiveCvMeanAccuracy"]} (leaky, reported for history)')
    print(f'full-fit acc={out["meta"]["fullFitAccuracy"]}')
    print(f'wrote {out_path}')


if __name__ == '__main__':
    main()
