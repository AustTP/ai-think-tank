# Think Tank evals

A small prompt-contract smoke suite for a running tank. It sends each case in
`cases.json` to `/api/chat` through an agent key and checks the reply against
explicit patterns. It is a starting harness, not an outcome benchmark.

```bash
python3 evals/run_evals.py --dry-run
TANK_AGENT_KEY=... python3 evals/run_evals.py --base-url http://127.0.0.1:8936
```

Cases cover assignment pickup, blocked reporting, approval refusal, village
boundaries, denied tools, idle exit, and restart recovery. Add a case by giving
it a name, a single user message, and one or more regex expectations.

Run this after changing a model, a system prompt, or a tool gate. It does not
replace the 7,922-test code suite or the JavaScript coverage gate.

## Recording runs and the benchmark page

Every real run records a provenance file under `evals/results/` (model,
UTC timestamp, git sha, per-case pass/fail with the missed patterns,
duration, and the spend OpenRouter reported — no reply text, no endpoint
host). The latest run per model renders into `evals/benchmark.html`:

```bash
TANK_AGENT_KEY=... python3 evals/run_evals.py                 # tank run
TANK_AGENT_KEY=... TANK_EVAL_MODEL=inclusionai/ling-3.0-flash \
    python3 evals/run_evals.py                                # naive baseline
python3 evals/render_benchmark.py                             # renders the page
```

The baseline is the cheapest band model (`inclusionai/ling-3.0-flash`)
run zero-shot on the same cases. The page renders `unpriced` when the
server did not price a call, `Baseline missing` when no baseline run is
recorded, and never infers a number. Publishing the rendered page is an
explicit operator step; this toolchain only records and renders.
