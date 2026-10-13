"""Curate a small evidence-backed seed set; withhold ungrounded log reports."""
import csv
import hashlib
import io
import json
from pathlib import Path

import prepare_unsloth_dataset as export

OUT = export.ROOT / 'datasets/local/research-labelled'


def evidence(path, start=1, stop=None, basis='direct_local_source'):
    text = (export.ROOT / path).read_text(encoding='utf-8-sig')
    lines = text.splitlines()
    selected = '\n'.join(lines[start - 1:stop])
    label = {'source': path, 'line_start': start, 'line_end': min(stop or len(lines), len(lines)),
             'basis': basis, 'sha256': hashlib.sha256(text.encode()).hexdigest(),
             'review_date': '2026-10-12'}
    return label, json.dumps(label) + '\n' + selected


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows, skips = export.candidates(export.ROOT / 'think_tank.db', 40000)
    by_id = {r['id']: r for r in rows}
    for row in rows:
        row.update(status='rejected', evidence_label='insufficient_evidence',
                   review_notes='Withheld: original retrieved evidence is not preserved; task completion and report assertions do not establish factual support.')
    approved = []

    def add(identifier, group, instruction, response, sources, origin='curated_source_based_example'):
        row = by_id.get(identifier, {'id': identifier, 'flags': [], 'source_path': '', 'task_type': 'research'})
        labels, excerpts = zip(*(evidence(*source) for source in sources))
        row.update(group_id=group, status='approved', instruction=instruction, response=response,
                   evidence='\n\n'.join(excerpts), evidence_sources=list(labels),
                   evidence_label='supported_by_local_sources', origin=origin,
                   review_notes='Assistant-curated against included source excerpts. Historical results are saved observations, not newly executed tests or independent provider authentication.')
        approved.append(row)

    add('task-2210', 'wrq-197', by_id['task-2210']['instruction'],
        '# Offline strategy implementation plan\n\n'
        'The implementation uses a trailing SMA diagnostic and RSI-based long-only signals: RSI below 35 opens candidate exposure; RSI above 65 closes it. Risk management caps exposure at 10% and shifts it once to the next bar. Backtest consumes that shifted exposure directly and charges an assumed 0.001 times turnover fee. No exchange-order submission appears in this module. [world/trading_strategy.py]\n\n'
        'Use generate_signals, apply_risk_management, and backtest in that order on time-sorted close-price data. Validate warm-up behavior, exposure bounds, entry timing and fee accounting before extending the strategy. These are proposed implementation checks, not a claim that I ran them.\n\n'
        'The stored simulation records total_return 0.028875622791604938, sharpe_ratio 0.3149275595533023, max_drawdown -0.045977498589920285 and 14 exposure-change events. This is a saved result, not proof of future returns or daily income. [library/reports/review-recovery/strategy-daily-backtest.json]\n\n'
        'Future work should explicitly define slippage, live execution controls, capital assumptions and out-of-sample evaluation. The source implements a bounded hypothesis, not an optimized or validated live trading system.',
        [('world/trading_strategy.py',), ('library/reports/review-recovery/strategy-daily-backtest.json',)], 'corrected_logged_report')

    add('task-1126', 'wrq-199', by_id['task-1126']['instruction'],
        '# Storage comparison from the supplied inventory\n\n'
        '| Option | Listed advantages | Listed drawbacks | Evidence status |\n|---|---|---|---|\n'
        '| CSV | Simple, human-readable, widely supported | Limited functionality and manual-editing errors | Unverified source comparison |\n'
        '| MySQL | Reliability, large-dataset support, ACID transactions | Setup/management complexity and SQL knowledge | Unverified source comparison |\n'
        '| MongoDB | Flexible model and claimed scalability | No standard SQL support and consistency challenges | Unverified source comparison |\n'
        '| AWS S3 | Claimed scalability, durability and availability | Structured-data limitations, usage charges and connectivity dependency | Unverified source comparison |\n\n'
        'Basis: the supplied inventory records these pros and cons, but labels every row unverified. This report transcribes those assertions; it does not independently endorse workload-specific performance or scalability claims. [library/reports/review-recovery/storage-options.csv]\n\n'
        'No measured latency, throughput, capacity threshold, current price comparison or benchmark is supplied. Choosing an option requires workload and access-pattern requirements plus measured comparisons. A completed comparison document is not an implemented storage system.',
        [('library/reports/review-recovery/storage-options.csv', 1, None, 'unverified_source_assertions')], 'corrected_logged_report')

    add('seed-strategy-validation', 'wrq-197', 'Write a validation findings report from the saved quality log. State the test count and distinguish recorded results from a new run.',
        '# Recorded validation findings\n\n'
        'The saved quality record has ok=true. Format, flake8, mypy, Bandit and pytest-cov each have exitCode 0. The pytest output records 19 passed in 1.43s and 100% total coverage against a required 90% threshold. [library/reports/review-recovery/project-wrq-197-734e4b58a4-quality.json]\n\n'
        'These are historical results preserved in a local log. I did not rerun the checks, establish which exact source revision produced them, or verify live trading. The evidence supports 19 tests, not 24. Coverage and passing checks do not establish strategy profitability.',
        [('library/reports/review-recovery/project-wrq-197-734e4b58a4-quality.json', 1, None, 'recorded_validation_result')])

    add('seed-strategy-timing', 'wrq-197', 'Explain the strategy execution timing and event-count metric using the supplied implementation.',
        '# Timing and accounting findings\n\n'
        'apply_risk_management forward-fills nonzero signals, clips negative exposure to zero, shifts exposure by one bar and multiplies it by 0.1. backtest reads position_size directly; it does not apply a second shift. This places the signal delay in one function. [world/trading_strategy.py]\n\n'
        'Fees are turnover times 0.001. num_trades counts bars with positive absolute exposure change, so it measures exposure-change events rather than completed round trips. The Sharpe calculation annualizes daily net returns using sqrt(365). These are code-level observations; no execution, statistical robustness or real fill quality has been verified.',
        [('world/trading_strategy.py', 37, 69)])

    add('seed-collector-review', 'wrq-197', 'Review the historical candle collector for input handling, data validation and publication limitations.',
        '# Historical collector source review\n\n'
        'collect accepts uppercase USD spot pairs, rejects empty or future ranges and ranges over 1,825 days, and treats the end date as exclusive. It requests daily candles in 299-day batches. Returned candles must have six fields; OHLCV values must be finite, volume nonnegative and price ordering valid. Conflicting duplicate timestamps are rejected; output is timestamp-sorted. [world/historical_data.py]\n\n'
        'The collector records missing-day counts but does not reject every incomplete dataset. Consumers must check completeness against their own requirements. fetch uses a 25-second timeout with no retry loop in the supplied source. CSV and JSON metadata are published with separate atomic replacements, so the pair is not a single atomic transaction. These are source findings; collection was not run for this review.',
        [('world/historical_data.py',)])

    metadata_path = export.ROOT / 'library/datasets/btc-usd-daily-2023-10-10-to-2026-10-10.json'
    metadata = json.loads(metadata_path.read_text())
    csv_path = export.ROOT / 'library' / metadata['path']
    payload = csv_path.read_bytes()
    data = list(csv.DictReader(io.StringIO(payload.decode('utf-8-sig'))))
    timestamps = [int(r['unix_time']) for r in data]
    assert hashlib.sha256(payload).hexdigest() == metadata['sha256']
    assert len(data) == 1096 and timestamps == sorted(set(timestamps))
    assert all(b - a == 86400 for a, b in zip(timestamps, timestamps[1:]))
    assert all(all(v.strip() for v in r.values()) for r in data)
    audit = {'rows': len(data), 'sha256_matches_metadata': True, 'unique_sorted_daily_timestamps': True,
             'empty_cells': 0, 'first_timestamp': data[0]['timestamp'], 'last_timestamp': data[-1]['timestamp'],
             'basis': 'computed_local_file_check', 'provider_authenticity_independently_verified': False}
    audit_path = OUT / 'historical-data-audit.json'
    audit_path.write_text(json.dumps(audit, indent=2), encoding='utf-8')
    add('seed-historical-data-audit', 'wrq-197', 'Report the saved BTC-USD dataset coverage and integrity checks. Distinguish local verification from provider authentication.',
        '# Saved dataset coverage\n\n'
        'The local file has 1,096 rows with unique, sorted timestamps spaced one day apart, no empty cells, first timestamp 2023-10-10T00:00:00+00:00 and last timestamp 2026-10-09T00:00:00+00:00. Its SHA256 matches the saved metadata. [datasets/local/research-labelled/historical-data-audit.json]\n\n'
        'The metadata identifies BTC-USD daily candles, an exclusive requested end of 2026-10-10, zero missing days and retrieval time 2026-10-10T17:31:54.775369+00:00. It records Coinbase Exchange source URLs. [library/datasets/btc-usd-daily-2023-10-10-to-2026-10-10.json]\n\n'
        'The file checks establish local integrity and coverage. They do not independently authenticate exchange responses, verify every price against the provider, or establish coverage for other products.',
        [('library/datasets/btc-usd-daily-2023-10-10-to-2026-10-10.json', 1, None, 'saved_provenance'),
         ('datasets/local/research-labelled/historical-data-audit.json', 1, None, 'computed_local_file_check')])

    add('seed-architecture', 'seed-architecture-docs', 'Summarize the documented application architecture and identify the authoritative state store.',
        '# Documented architecture\n\n'
        'The design describes a server-owned simulation. world/serve.py is the FastAPI server; world/sim.py manages the state machine, tasks and review lifecycle; world/content.py executes research and coding work. bank.py, auth.py and notify.py provide budgets, player authentication and notifications. [DESIGN.md]\n\n'
        'SQLite think_tank.db is authoritative. The kv_state blob stores live agents, teams, tasks, sprints and room definitions. action_log records agent activity and decision_tape records decision inputs and responses. The browser presents server state. These findings describe the documentation, not a live inspection proving every subsystem currently behaves as specified.',
        [('DESIGN.md', 1, 77, 'repository_documentation')])

    add('seed-identity-persistence', 'seed-architecture-docs', 'Explain agent identity persistence from the supplied design excerpt, including the role of generated files.',
        '# Agent identity persistence\n\n'
        'The documented state has an agentRoster array for identity metadata and an agents dictionary for live positions, assignments, mailboxes and counters. Both reside in kv_state. Generated agent.json, AGENTS.md, MEMORY.md and state.json files are mirrors regenerated on database saves, not the authoritative store. [DESIGN.md]\n\n'
        'A troubleshooting plan should therefore inspect the database-backed state and regeneration path before assuming a mirror-file edit will persist. This recommendation follows from the documented ownership model; regeneration has not been tested in this report.',
        [('DESIGN.md', 69, 77, 'repository_documentation')])

    add('seed-review-test-plan', 'seed-manual-test-checklist', 'Prepare a manual validation plan for the documented work lifecycle. Do not claim the checks were executed.',
        '# Work lifecycle validation plan\n\n'
        'Start the server and verify the browser renders moving agents. File a story through POST /api/intent/issues and check its key and title. Confirm it enters backlogRequests as pending, is groomed into a sprint, and is assigned to an agent. [world/TESTING-CHECKLIST.md]\n\n'
        'For deliverable work, check the needs_review peer gate and assignment of two reviewers. Exercise both outcomes: two clean approvals lead to done; rejection returns a fix to the original author. Confirm the completing agent goes off duty and the story becomes gradable. Finally check that grading updates the roadmap and surfaces follow-up work.\n\n'
        'Record PASS, FAIL or BLOCKED with reasons for each step. This is a test plan derived from the checklist; no steps have been executed and no results are asserted.',
        [('world/TESTING-CHECKLIST.md', 1, 30, 'documented_test_plan')])

    add('seed-recovery-budget-report', 'seed-runtime-readme', 'Summarize the documented recovery controls and how a request-level spending limit is applied.',
        '# Documented recovery and spending controls\n\n'
        'The README describes recovery of unfinished stories with exponential backoff and explicit blockers after retries. Pauses, stopped stories, frozen review gates and existing budgets remain respected. Default completionControl values are five concurrent root stories, three recovery attempts, a 30-second initial delay and at most two recoveries per tick. [README.md]\n\n'
        'A player request can carry a USD model-spend budget. Planning and linked story, repair, investigation and review calls share it; blank leaves global and task limits active. Limits are checked before calls, while already-running calls can exceed the recorded cap. Historical calls without task attribution cannot be reconstructed.\n\n'
        'These are documented controls rather than measurements from a live run. The evidence does not establish that historical spending stayed within a particular request budget.',
        [('README.md', 64, 85, 'repository_documentation')])

    values = export.secret_values()
    # Labels and evidence references are kept outside the model's target schema.
    for row in rows + [r for r in approved if r['id'] not in by_id]:
        for field in ('instruction', 'response', 'evidence', 'review_notes'):
            row[field] = export.redact(row.get(field, ''), values)
    labelled = rows + [r for r in approved if r['id'] not in by_id]
    export.write_jsonl(OUT / 'reviewed.jsonl', labelled)
    summary = export.build(OUT / 'reviewed.jsonl', OUT, 0.2, 40000)
    summary.update(candidate_reports=len(rows), approved=len(approved),
                   corrected_logged_reports=2, source_based_seed_examples=len(approved) - 2,
                   purpose='Small report-writing smoke-test dataset; insufficient for a production fine-tune.',
                   evidence_policy='Local source inspection or explicitly attributed unverified assertions; no fabricated retrieved evidence.')
    (OUT / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
