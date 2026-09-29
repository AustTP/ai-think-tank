"""Weekly model probe: run a fixed prompt set through a candidate model and log
a comparable scoreboard row. This is the "generalist on purpose" practice -- the
think tank's home-base model stays put, and once a week you check whether any new
model earns a real job by running it through the SAME prompts you always use.

Uses serve's own lowest-level chat path (`_post_openrouter_raw`) so the probe
hits OpenRouter directly (no server needed), inherits the circuit breaker + model
bookkeeping, and measures each reply's cost + latency. Output is append-only to
library/probe/scores.md so the decision tape / scoreboard accumulates a history.

Usage:
    python3 probe_model.py <model_slug> [--prompts probe_prompts.md]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serve as _serve  # noqa: E402

# Fixed set: the same handful of think tank-real tasks, verbatim every week, so
# cross-model rows are comparable (changing the prompts breaks the comparison).
_DEFAULT_PROMPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'probe_prompts.md')
SCORES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'library', 'probe')
SCORES_FILE = os.path.join(SCORES_DIR, 'scores.md')


def _load_prompts(path):
    """Parse the prompt file: each `## name` heading starts a new prompt whose
    body (stripped) is the text. Returns [(name, text), ...]."""
    prompts = []
    cur = None
    buf = []
    for line in open(path, 'r', errors='replace'):
        if line.startswith('## '):
            if cur is not None:
                prompts.append((cur, '\n'.join(buf).strip()))
            cur = line[3:].strip()
            buf = []
        elif cur is not None:
            buf.append(line.rstrip('\n'))
    if cur is not None:
        prompts.append((cur, '\n'.join(buf).strip()))
    return prompts


def _extract_reply(data):
    """OpenRouter chat-completions choice text (or None)."""
    choice = ((data or {}).get('choices') or [{}])[0]
    return (choice.get('message') or {}).get('content')


def _probe_one(model, name, text):
    if _serve.is_model_circuit_broken(model):
        return None, 'circuit-broken', 0.0, 0.0
    t0 = time.time()
    try:
        data = _serve._post_openrouter_raw(
            model, [{'role': 'user', 'content': text}], max_tokens=800)
    except Exception as e:  # noqa: BLE001 -- report, don't mask, the failure
        return None, f'error: {e}', 0.0, time.time() - t0
    latency = time.time() - t0
    usage = (data or {}).get('usage') or {}
    cost = usage.get('cost', 0.0)
    reply = _extract_reply(data)
    return (reply or None), 'ok', cost, latency


def main():
    ap = argparse.ArgumentParser(description='Run the weekly model probe.')
    ap.add_argument('model', help='OpenRouter model slug, e.g. deepseek/deepseek-chat')
    ap.add_argument('--prompts', default=_DEFAULT_PROMPTS)
    ap.add_argument('--limit', type=int, default=None,
                    help='only probe the first N prompts (default: all)')
    args = ap.parse_args()

    prompts = _load_prompts(args.prompts)
    if not prompts:
        print(f'no prompts found in {args.prompts}')
        return 1
    if args.limit:
        prompts = prompts[:args.limit]

    iso = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'== probe {args.model} @ {iso} ==')
    rows = []
    for name, text in prompts:
        reply, status, cost, latency = _probe_one(args.model, name, text)
        words = len((reply or '').split())
        rows.append((name, status, reply, cost, latency, words))
        print(f'  {name:>12}  {status:>12}  ${cost:>8.4f}  {latency:>5.1f}s  {words}w')
        if reply:
            print(f'      {reply.strip()[:300].replace(chr(10), " ")}')
        print()

    # Append one scoreboard row (or one row per prompt, when they differ).
    os.makedirs(SCORES_DIR, exist_ok=True)
    header = '| date | model | prompt | status | cost | latency_s | words |\n' \
             '|---|---|---|---|---|---|---|'
    if not os.path.exists(SCORES_FILE):
        with open(SCORES_FILE, 'w') as f:
            f.write('# Model probe scoreboard\n\n' + header + '\n')
    with open(SCORES_FILE, 'a') as f:
        for name, status, reply, cost, latency, words in rows:
            text = (reply or status).replace('|', '\\|')[:120]
            f.write(f'| {iso} | {args.model} | {name} | {status} | '
                    f'{cost:.4f} | {latency:.1f} | {words} |\n')
    print(f'logged {len(rows)} row(s) -> {SCORES_FILE}')
    return 0


if __name__ == '__main__':
    sys.exit(main())