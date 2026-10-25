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
