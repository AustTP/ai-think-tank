# AI Think Tank

Two notes are taped inside the front door.

> **Theo, Control Room:** The building runs whether you watch it or not. Agents move, work, argue, review, and file reports. Every position and every decision lives on the server. The browser is a window, not the machinery.

> **Nora, Personnel:** I keep the files. I know who was hired, who was let go, and who is still pretending the backlog is under control. Read this before you start the server.

This is a persistent simulation. LLM agents walk between rooms, pick up tasks, refine backlogs, peer-review each other's work, get hired and fired, and write reports. Real model calls through OpenRouter drive all of it. A JEV policy classifier gates the calls that cost money. Close the browser and the tank keeps working. Come back and you see what happened while you were gone.

For the full architecture, read `DESIGN.md`. This file is the operator's tour.

## Start the building

```bash
cd world
python3 serve.py 8936
# open http://127.0.0.1:8936  (default port)
```

On a genuinely fresh checkout with no database yet:

1. Start the server as above.
2. The startup log prints the auto-created admin account **once**:
   ```
   [auth] First run -- admin account created.
   [auth] username: admin
   [auth] password: <generated>  (shown once -- save it)
   ```
3. Log in at `http://127.0.0.1:8936`. The first authenticated state load seeds agent identities into the database if they are not already there.

Secrets live in `.env` at the repo root. Git ignores that file. Copy `.env.template` to `.env` and set at least `OPENROUTER_API_KEY`. If the admin credentials and `SERVER_SECRET` are absent, the server generates them on first boot.

Only `OPENROUTER_API_KEY` is required. Every other key in `.env.template` is optional. Leave a key blank or omit it and its feature stays off. The server reads each key on its own and treats blank the same as unset. A partial `.env` works. Fill in only the integrations you use.

Run the tests:

```bash
python3 -m pytest world/tests -q
node world/tests/js_coverage.mjs
```

`bash world/tests/run_all.sh` runs both. The suite has 7,814 passing Python tests and a 90% JavaScript coverage floor.

## Theo's control room

Theo keeps the building honest. He cares about state, gates, and where the money goes.

- **The database.** `think_tank.db` is SQLite. Every action gets logged. Agent history can be replayed. Nothing is lost on restart.
- **The server.** `world/serve.py` loads the parts in `world/server/serve_parts/`. The FastAPI app owns the simulation, model calls through OpenRouter, the shared Library, spend accounting, and session auth.
- **The simulation.** `world/sim.py` loads the parts in `world/village/sim_parts/`. It moves agents, assigns tasks, runs ceremonies, enforces peer-review gates, and drives sprint lifecycles in one read-modify-write pass every 6 seconds.
- **The executors.** `world/content.py` loads the parts in `world/agents/content_parts/`. It handles research crawls, code writing and review, weather, distillation, media digests, bank reviews, and time-boxed investigations called spikes.
- **The client.** `world/index.html` draws the tank as a pixel-art canvas and lets you interact. It is a viewport. The server owns every position, task, and decision.
- **The model tiers.** Work starts on the cheap `low` tier. JEV decides which calls earn `mid` or `high`. Coding tasks use the coding tier automatically. The refresh re-picks each band's best-value model from the live OpenRouter catalog.
- **The budgets.** Page requests are capped at 1,000 per month. The hard spend cap defaults to $50 per month and is set with `SPEND_CAP_USD`. The high tier has its own monthly budget, which defaults to $5. JEV respects both.
- **Plain writing.** Prose replies at the model-call boundary carry a "write plainly, no filler" instruction and a word-ban list. The tank spends fewer tokens on the same information. It is on by default. You can opt out per request. JSON prompts are exempt.

## Nora's personnel floor

Nora keeps the roster, the ceremonies, and the memory. She knows that a think tank is not a stack of code. It is a room full of agents with tasks, reputations, and something to prove.

- **Villages.** A village is the top-level identity and memory boundary. Every agent belongs to one. The default is `main`. Wiki pages and design taste docs are scoped to the writer's or reader's village, so one village's memory never leaks into another. The player creates villages and assigns agents through `/api/villages` and `/api/villages/assign`.
- **Memory hygiene.** Every wiki page records an owner, a clock (slow or fast), a source, and a `reviewAfterMs` expiry. Expired pages stop counting as trusted context. Fast-clock pages get flagged so the village re-fetches the live source instead of trusting the copy. Design taste docs carry an owner and a review-after date. Expired ones are skipped. Every shared-memory write lands in a provenance ledger at `GET /api/memory-ledger`, with claim, source, owner, scope, and review date.
- **The roster.** Agent identities live only in `think_tank.db`. The codebase hardcodes no agent names. The roster is seeded from `.env` on a cold database.
- **The work floor.** Agents move through rooms, pick up cards, hand work to reviewers, and wait at gates. An idle tank spends no model budget. Governance goes quiet when nothing is happening.
- **The exits.** Hiring and firing are real processes with approvals, reviews, and records. Nora would like you to remember that.

## Where things live

| Component | What it owns |
|-----------|-------------|
| `world/serve.py` + `world/server/serve_parts/` | FastAPI app: state DB, model calls, tools, auth, spend ledger, simulation driver, team and director hierarchy, model-tier refresh |
| `world/integrations/bank.py` | The Bank: spend ledger, per-service budget gates, spend cap, forecasts |
| `world/core/auth.py` | Player auth: credential creation, password hashing, login sessions, rate limiting |
| `world/integrations/notify.py` | Escalation and player-notification email and Telegram senders |
| `world/sim.py` + `world/village/sim_parts/` | Simulation engine: movement, task lifecycle, ceremonies, peer gates, sprint lifecycle, governance |
| `world/core/confirmations.py` | Durable two-phase confirmation proposals: target and argument binding, exact phrase, expiry, completion |
| `world/server/confirmations_api.py` | Player and agent HTTP surface for proposing, confirming, listing, and cancelling actions |
| `world/server/migrations.py` + `world/migrations/` | Versioned SQLite migrations with checksums |
| `world/core/model_config.py` | Model-band validation and operator table |
| `world/content.py` + `world/agents/content_parts/` | Per-room content executors: research, code writing and review, weather, media, distill, spikes, bank teller |
| `world/integrations/web_helpers.py` | Pure HTML stripping, link extraction, HTTP-date parsing, SSRF host check |
| `world/village/sim_helpers.py` | Pure priority normalization, room and team derivation, sprint and product ID generation, village and memory-clock helpers |
| `world/index.html` + `world/js/*.js` | Browser renderer. The client is a viewport and never the state machine. |
| `world/tests/` | Python and JavaScript test suites (7,814 Python tests plus the JS coverage harness) |

## Repo layout

- `world/`: the full implementation. Server, sim, renderer, tests.
- `agents/*`: per-agent identity files (`agent.json`, `AGENTS.md`, `MEMORY.md`). These are regenerated mirrors of `think_tank.db`, never the source of truth. Git ignores them.
- `library/`: the shared project Library. Skills are tracked. Runtime content such as archive, wiki, and social is gitignored and regenerated.
- `sandboxes/`: isolated development directories, one per agent.
- `.env`: per-install secrets and configuration. Git ignores it. Copy `.env.template` for the documented format.

## House rules

- Agent identities live only in `think_tank.db`. No agent names are hardcoded.
- An idle think tank spends no model budget.
- The high tier is bounded twice: a per-model price ceiling and a monthly spend cap. The monthly cap defaults to $5. The per-model ceiling is unset, which disables it.
- Room descriptions are editable by directors. Room geometry is not. It is a code-graph invariant.

Theo will tell you when the server is unhealthy. Nora will tell you when the people are. Listen to both.

## Security

Read `SECURITY.md` before exposing this server to a network. Never commit
`.env`, cookies, API keys, tokens, or real user data. Report a vulnerability
through GitHub's private vulnerability reporting, not a public issue.
