# AI Think Tank

A persistent, server-owned simulation where LLM agents walk between rooms,
work tasks, refine backlogs, peer-review each other's deliverables, get hired
and fired, and file reports; all of it is driven by real model calls through
OpenRouter, gated by a JEV policy classifier where decisions have
consequences. Close the browser and the think tank keeps running; reopen it and
you see what happened while you were away.

See `DESIGN.md` for the full architecture documentation.

## Running it

```bash
cd world
python3 serve.py 8936
# open http://127.0.0.1:8936  (default port)
```

First run (a genuinely fresh checkout with no database yet):

1. Start the server as above.
2. The startup log prints the auto-created admin account **once**:
   ```
   [auth] First run -- admin account created.
   [auth] username: admin
   [auth] password: <generated>  (shown once -- save it)
   ```
3. Log in at `http://127.0.0.1:8936`. The first authenticated state load
   seeds agent identities into the database if they're not already there.

Secrets live in `.env` at the repo root (git-ignored). Copy `.env.template`
to `.env` and set at least `OPENROUTER_API_KEY`. Admin credentials and
`SERVER_SECRET` are auto-generated on first boot if absent.

Tests:

```bash
cd world && python3 -m pytest tests/
```

## What it is

- A persistent think tank database (`think_tank.db`, SQLite). Every action is
  logged; agent history is replayable. No state is lost on restart.
- A FastAPI server (`world/serve.py`) that owns the simulation, the model
  calls (via OpenRouter), the shared Library, spend accounting, and session
  auth.
- A server-side simulation engine (`world/sim.py`) that drives movement,
  task assignment, ceremonies (refinement, social, governance, escalation),
  peer-review gates, and sprint lifecycles, all in a single
  read-modify-write pass every 6 seconds.
- Content executors (`world/content.py`) that run when an agent arrives at a
  task: research crawls, code writing and review, weather, distillation,
  media digests, bank reviews, and time-boxed investigations (spikes).
- A single-page browser client (`world/index.html`) that renders the think tank
  as a pixel-art canvas and lets the player interact, but is now a pure
  viewport. The server owns every position, every task, every decision.
- A JEV-gated model tier system: everything defaults to the cheap `low`
  tier (deepseek-v4-flash at ~$0.26/M). JEV decides which calls deserve
  `mid` (lightly gated) or `high` (heavily gated + monthly budget-capped).
  Coding tasks automatically use the coding tier (qwen3-coder). The daily
  refresh re-picks each band's best-value model from the live OpenRouter
  catalog.
- Budget controls: 1,000 page requests/month, a $5 hard spend cap, and a
  $5/month high-tier budget that the JEV gate respects.
- A plain-writing directive at the model-call boundary: prose replies carry an
  anti-AI-slop "write plainly, no filler" instruction with a word-ban list, so
  the village spends fewer tokens on the same information. On by default,
  opt-out per request, JSON prompts exempt.

## Key architecture

| Component | What it owns |
|-----------|-------------|
| `world/serve.py` | FastAPI app: state DB, model calls, tools, auth, spend ledger, simulation driver, team/director hierarchy, model-tier refresh |
| `world/sim.py` | Server simulation engine: movement, task lifecycle, ceremonies, peer gates, sprint lifecycle, governance |
| `world/content.py` | Per-room content executors: research, code writing/review, weather, media, distill, spikes, bank teller |
| `world/web_helpers.py` | Pure HTML stripping, link extraction, HTTP-date parsing, SSRF host check |
| `world/sim_helpers.py` | Pure priority normalization, room/team derivation, sprint/product id generation |
| `world/index.html` + `world/*.js` | Browser renderer; the client is a viewport, never the state machine |
| `world/tests/` | Python test suite (869 tests) covering every path |

## Repo layout

- `world/`: the full implementation (server, sim, renderer, tests).
- `agents/*`: per-agent identity files (`agent.json`, `AGENTS.md`,
  `MEMORY.md`). **Regenerated mirrors of `think_tank.db`**, never the source of
  truth. Gitignored.
- `library/`: shared project Library (skills, archive, wiki). Skills are
  tracked; runtime content (archive, wiki, social) is gitignored and
  regenerated.
- `sandboxes/`: per-agent isolated development directories.
- `.env` (git-ignored): per-install secrets and configuration. Copy
  `.env.template` for the documented format.

## Notes

- Agent identities live ONLY in `think_tank.db`. The codebase has zero hardcoded
  agent names; the roster is seeded from `.env` on a cold DB.
- Governance is idle-quiet: an idle think tank spends no model budget.
- The high tier (the expensive one) is bounded twice: a per-model price
  ceiling ($5/M) and a monthly spend cap ($5/mo).
- Room descriptions are director-editable; room geometry is not (it is a
  code-graph invariant).