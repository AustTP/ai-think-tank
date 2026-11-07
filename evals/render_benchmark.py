#!/usr/bin/env python3
"""Render the recorded eval runs into a static benchmark page.

Reads every JSON record written by run_evals.py (evals/results/*.json),
takes the LATEST run per model, and writes a self-contained HTML page a
stranger can verify: which cases ran, what passed, what it cost, when,
and on which code revision. Nothing here invents a number: a missing
baseline run renders as BASELINE MISSING, an unpriced call renders as
'unpriced', and every figure names the record file it came from.

    python3 evals/render_benchmark.py                 # writes benchmark.html
    python3 evals/render_benchmark.py --out /tmp/x.html

Publishing the page is a separate, explicit operator step; this tool
only renders what was recorded.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The baseline is the cheapest band model run zero-shot on the same cases
# (see world/server/serve_parts model bands: sublow). Override when the
# band changes.
BASELINE_MODEL = 'inclusionai/ling-3.0-flash'
TANK_MODEL = 'openrouter/auto'


def load_runs(results_dir: Path) -> list[dict]:
    runs = []
    if not results_dir.is_dir():
        return runs
    for path in sorted(results_dir.glob('*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if not all(k in data for k in ('runAt', 'model', 'cases', 'passed', 'total')):
            continue
        data['_source'] = path.name
        runs.append(data)
    return runs


def latest_per_model(runs: list[dict]) -> dict[str, dict]:
    # Records sort by ISO runAt, then by filename for same-second runs.
    latest: dict[str, dict] = {}
    for run in sorted(runs, key=lambda r: (str(r.get('runAt')), str(r.get('_source')))):
        latest[str(run['model'])] = run
    return latest


def fmt_cache(run: dict) -> str:
    """Provider-reported prefix-cache hit rate for a run, or '' when the
    run recorded no token breakdown (older records, or the server predates
    includeUsage). Nothing is inferred from a missing number."""
    if not isinstance(run, dict):
        return ''
    tokens = run.get('tokens')
    if not isinstance(tokens, dict):
        return ''
    cached = tokens.get('cached_tokens')
    prompt = tokens.get('prompt_tokens')
    if not isinstance(prompt, (int, float)) or prompt <= 0:
        return ''
    if not isinstance(cached, (int, float)):
        return 'cache n/a'
    return f'cache hit {cached / prompt:.0%}'


def fmt_cost(run: dict) -> str:
    cost = run.get('costUsd')
    if not isinstance(cost, (int, float)):
        return 'unpriced'
    return f'${cost:.4f}'


def case_rows(run: dict) -> list[tuple[str, str]]:
    rows = []
    for case in run.get('cases', []):
        if case.get('ok'):
            rows.append((str(case.get('name')), 'PASS'))
        elif case.get('error'):
            rows.append((str(case.get('name')), 'ERROR: ' + str(case['error'])[:80]))
        else:
            rows.append((str(case.get('name')),
                         'FAIL: missing ' + ', '.join(case.get('missing') or [])))
    return rows


def run_table(label: str, run: dict) -> list[str]:
    out = [f'<h2>{escape(label)}</h2>',
           f'<p class="meta">model <code>{escape(run["model"])}</code> · '
           f'{escape(str(run.get("runAt")))} · git '
           f'<code>{escape(str(run.get("gitSha") or "unknown"))}</code> · '
           f'{run.get("passed")}/{run.get("total")} passed · '
           f'cost {escape(fmt_cost(run))} · '
           f'{run.get("durationMs", "?")} ms · '
           + (f'{escape(fmt_cache(run))} · ' if fmt_cache(run) else '')
           + f'source <code>{escape(run["_source"])}</code></p>']
    out.append('<table><tr><th>case</th><th>result</th></tr>')
    for name, result in case_rows(run):
        cls = 'pass' if result == 'PASS' else 'fail'
        out.append(f'<tr><td>{escape(name)}</td>'
                   f'<td class="{cls}">{escape(result)}</td></tr>')
    out.append('</table>')
    return out


def render(runs: list[dict], generated_at: str) -> str:
    latest = latest_per_model(runs)
    tank = latest.get(TANK_MODEL)
    baseline = latest.get(BASELINE_MODEL)
    parts = [
        '<!DOCTYPE html><html><head><meta charset="utf-8">',
        '<title>Think tank eval benchmark</title>',
        '<style>body{font-family:system-ui,sans-serif;max-width:900px;',
        'margin:2rem auto;padding:0 1rem;color:#222}',
        'table{border-collapse:collapse;width:100%}',
        'td,th{border:1px solid #ccc;padding:4px 8px;text-align:left}',
        '.pass{color:#060}.fail{color:#900}.meta{color:#666;font-size:.9em}',
        '.missing{color:#900;font-weight:600}</style></head><body>',
        '<h1>Think tank eval benchmark</h1>',
        f'<p class="meta">rendered {escape(generated_at)} · prompt-contract '
        'smoke evals, not an outcome benchmark · every number is a recorded '
        'run, reproduced with <code>evals/run_evals.py</code></p>',
    ]
    if tank:
        parts += run_table(f'Tank run ({TANK_MODEL})', tank)
    else:
        parts.append('<p class="missing">No tank run recorded yet — run '
                     '<code>TANK_AGENT_KEY=... python3 evals/run_evals.py</code>.</p>')
    if baseline:
        parts += run_table(f'Naive baseline ({BASELINE_MODEL}, zero-shot)', baseline)
    else:
        parts.append('<p class="missing">Baseline missing — no recorded run for '
                     f'<code>{escape(BASELINE_MODEL)}</code>. The comparison '
                     'section stays empty until one exists; nothing is '
                     'inferred for it.</p>')
    if tank and baseline:
        parts += [
            '<h2>Comparison</h2>',
            '<table><tr><th></th><th>tank</th><th>baseline</th></tr>',
            '<tr><td>passed</td>'
            f'<td>{tank.get("passed")}/{tank.get("total")}</td>'
            f'<td>{baseline.get("passed")}/{baseline.get("total")}</td></tr>',
            '<tr><td>cost per run</td>'
            f'<td>{escape(fmt_cost(tank))}</td>'
            f'<td>{escape(fmt_cost(baseline))}</td></tr>',
            '<tr><td>duration</td>'
            f'<td>{tank.get("durationMs", "?")} ms</td>'
            f'<td>{baseline.get("durationMs", "?")} ms</td></tr>',
        ]
    tank_cache, baseline_cache = fmt_cache(tank), fmt_cache(baseline)
    if tank_cache or baseline_cache:
        parts += [
            '<tr><td>provider cache</td>'
            f'<td>{escape(tank_cache or "n/a")}</td>'
            f'<td>{escape(baseline_cache or "n/a")}</td></tr>',
            '</table>',
        ]
    else:
        parts.append('</table>')
    parts += [
        '<p class="meta">Both rows are the latest recorded run per '
        'model; runs on different code revisions are labeled with their '
        'git sha above. A fair comparison pairs runs from the same '
        'revision. The cache line is the provider\'s own reported '
        'cached-token share of prompt tokens; runs without a token '
        'breakdown show n/a, never a guess.</p>',
    ]
    parts.append('</body></html>')
    return '\n'.join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--results-dir', type=Path, default=HERE / 'results')
    parser.add_argument('--out', type=Path, default=HERE / 'benchmark.html')
    args = parser.parse_args(argv)
    runs = load_runs(args.results_dir)
    generated_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    args.out.write_text(render(runs, generated_at), encoding='utf-8')
    print(f'{len(runs)} recorded run(s); wrote {args.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
