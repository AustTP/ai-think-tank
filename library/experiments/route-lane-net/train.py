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
    toks = [t for t in TOKEN_RE.findall((text or '').lower()) if len(t) >= 2]
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
    by_class = {}
    for lane, t in examples:
        by_class.setdefault(lane, []).append(t)
    rng = random.Random(seed)
    for lst in by_class.values():
        rng.shuffle(lst)
    folds = [[] for _ in range(k)]
    i = 0
    for lane in sorted(by_class):
        for t in by_class[lane]:
            folds[i % k].append((lane, t))
            i += 1
    accs = []
    for f in range(k):
        test = folds[f]
        train_set = [x for g in range(k) if g != f for x in folds[g]]
        W, bias = train(train_set)
        right = sum(1 for lane, t in test
                    if LANES[predict(features(t), W, bias)] == lane)
        accs.append(right / len(test) if test else 1.0)
    return accs


def main():
    ds_path = sys.argv[1] if len(sys.argv) > 1 else 'dataset.json'
    out_path = sys.argv[2] if len(sys.argv) > 2 else '../../nets/route-lane-v1.json'
    rows = json.load(open(ds_path))
    examples = [(r['lane'], r['text']) for r in rows]
    counts = Counter(lane for lane, _ in examples)
    folds = kfold(examples)
    W, bias = train(examples)
    fit_right = sum(1 for lane, t in examples
                    if LANES[predict(features(t), W, bias)] == lane)
    out = {
        'version': 1,
        'lanes': LANES,
        'buckets': BUCKETS,
        'spec': ('tokens = [a-z0-9]+ runs of text.lower() kept when len>=2; '
                 'features = unigrams + adjacent bigrams; bucket = '
                 'int(md5(feature)[:8],16) %% buckets; L2-normalized counts; '
                 'score = W[bucket][lane] sums + bias[lane]; argmax wins'),
        'weights': [[round(v, 4) for v in row] for row in W],
        'bias': [round(v, 4) for v in bias],
        'meta': {
            'trainedAt': '2026-11-18',
            'examples': len(examples),
            'classCounts': dict(counts),
            'cvFolds': 5,
            'cvAccuracies': [round(a, 3) for a in folds],
            'cvMeanAccuracy': round(sum(folds) / len(folds), 3),
            'fullFitAccuracy': round(fit_right / len(examples), 3),
            'source': 'action_log route_classified (21d retention window)',
            'role': 'shadow only -- never decides routing (476_route_shadow_net.py)',
        },
    }
    with open(out_path, 'w') as fh:
        json.dump(out, fh, indent=1)
    print(f'examples={len(examples)} classCounts={dict(counts)}')
    print(f'cv accs={ [round(a, 3) for a in folds] } mean={out["meta"]["cvMeanAccuracy"]}')
    print(f'full-fit acc={out["meta"]["fullFitAccuracy"]}')
    print(f'wrote {out_path}')


if __name__ == '__main__':
    main()
