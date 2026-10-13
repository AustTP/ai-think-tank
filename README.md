# AI Think Tank

A persistent, server-owned simulation where LLM agents walk between rooms,
work tasks, refine backlogs, peer-review each other's deliverables, get hired
and fired, and file reports; all of it is driven by real model calls through
OpenRouter, gated by a JEV policy classifier where decisions have
consequences. Close the browser and the think tank keeps running; reopen it and
you see what happened while you were away.

See `DESIGN.md` for the full architecture documentation.

## Running it

### Local active-work model

Future training interactions can be recorded locally, with actual prompts,
responses, tool evidence and task outcomes. See
[recording and export instructions](datasets/INTERACTION_RECORDING.md).

Active worker completions can use a fine-tuned adapter hosted by Unsloth Studio's
OpenAI-compatible API. Set these server-side values in the git-ignored `.env`:

```ini
LOCAL_WORK_MODEL_ENABLED=true
LOCAL_WORK_MODEL_PATH=C:\Users\Khalil\.unsloth\studio\outputs\unsloth_Qwen3.5-4B_1791825047
LOCAL_WORK_MODEL_BASE_URL=http://127.0.0.1:8888/v1
LOCAL_WORK_MODEL_API_KEY=<Studio API key>
LOCAL_WORK_MODEL_TIMEOUT=120
LOCAL_WORK_MODEL_MANAGE_STUDIO=true
```

With `LOCAL_WORK_MODEL_MANAGE_STUDIO=true`, each local call checks the loaded
model and loads the adapter in 4-bit mode with an 8,192-token context if another
model is resident. Studio must be running, and the API key must permit loading.
This can switch Studio's active model while the think tank is working. The
adapter's base weights must be available locally. The path is a LoRA adapter, not standalone
base weights. Reviewers, the Code Director, review/QA/audit/validation tasks,
repairs, recovery investigations and coding correction rounds retain their
existing configured models. Policy decisions also retain their existing model.

Each eligible agent gets a stable, distinct seed derived from its ID. Sampling
uses temperature 0.6; a seed is best-effort reproducibility, not a separate
personality or guarantee of identical GPU outputs. Local calls are serialized
to share one GPU, have zero provider cost, and record model/provider/seed in the
action log. Existing auth and task budget controls still apply. Local inference
failures surface without silently switching that call to a paid model. Setting
`LOCAL_WORK_MODEL_ENABLED=false` restores normal model routing. Restart the
think-tank server after installing code changes; `.env` routing settings are
read on each call. Keep Studio running.

Build the agent execution sandbox once (with Docker running):

```powershell
docker build -t ai-think-tank-work-sandbox world/sandbox
```

This image contains the offline quality tools. Without it, execution and
review tasks fail before their commands run. The server starts the sandbox
egress proxy automatically.

Install the browser used by `browse_page(render=true)` and page probes:

```powershell
python -m playwright install chromium
```

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

Only `OPENROUTER_API_KEY` is required to run. Every other key in
`.env.template` is optional: leave a key blank (or omit it) and the matching
feature simply stays off — the server reads each key independently and treats
blank the same as unset. So a partial `.env` works fine; you only fill in the
integrations you actually use.

Tests:

```bash
cd world && python3 -m pytest tests/
```

Unfinished-story recovery runs on each simulation tick. Completed blocker
investigations queue a repair of the original story; repairs return to its
normal quality checks and independent review. Orphaned investigations and
repairs retry with exponential backoff, then become explicit blockers requiring
a decision. Pauses, stopped stories, frozen review gates, and exhausted budgets
are respected. The board shows delivery state, next action, and last progress.

The persisted `completionControl` settings default to five concurrent root
stories, three recovery attempts, a 30-second initial backoff, and at most two
recoveries per tick. Linked repairs, investigations, and reviews have scheduling
priority and can finish admitted stories even when the concurrency limit is
full. Supplying new recovery guidance resets the recovery allowance. Existing
model-spend limits still apply; retries never raise budgets automatically.

Player requests accept an optional model-spend budget in USD, editable from the
request list. Blank means no request-level cap; global and individual-task
limits still apply. Planning and all linked story, repair, investigation and
review calls share that request budget. Limits are checked before calls; calls
already in flight can bring recorded spend above the cap. Historical calls
without task attribution cannot be reconstructed and external service invoices
are not included in this model-spend rollup.

Every task retains its original story, request and scrum-master IDs. The board
and request list show individual task costs and story/request totals without
adding duplicate service or village buckets. Blocked stories have a player-only
**Mark complete with exceptions** action requiring a reason. This retains the
failed reviews, cancels remaining descendants, and records exceptional acceptance
separately from passing quality checks. It does not advance the trading workflow.

Repeated unchanged failures are tracked across replacement task IDs. Two failed
attempts with the same evidence and plan stop automatic cycling; new evidence
or explicit changed guidance is needed. The Librarian opens at most one recovery
for the same missing-artifact set per original story, with at most two artifact
recoveries before a changed plan or supplied contents are required. Mechanical failures skip
model reviews. Passing code checks can be reused for one hour when source,
commands and the Docker image digest match; failures are never cached. Public
documentation previously authorized for the same agent can be reused for six
hours, with a forced-refresh option. Market-data requests remain live. Repair
prompts keep exact original requirements and concise current feedback.

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
- Budget controls: 1,000 page requests/month, a configurable hard spend cap
  (default $50/month, set via `SPEND_CAP_USD`), and a high-tier budget that the
  JEV gate respects.
- A plain-writing directive at the model-call boundary: prose replies carry an
  anti-AI-slop "write plainly, no filler" instruction with a word-ban list, so
  the village spends fewer tokens on the same information. On by default,
  opt-out per request, JSON prompts exempt.
- Villages: the top-level identity + memory boundary. Every agent belongs to a
  village (default `main`); wiki pages and design taste docs are scoped to the
  writer/reader's village, so one village's memory never leaks into another.
  Villages are created and agents assigned via the player-only
  `/api/villages` / `/api/villages/assign` endpoints.
- Memory engineering (shared-memory hygiene): every wiki page carries an
  owner, a clock (slow/fast), a source, and a `reviewAfterMs` expiry. Expired
  pages are no longer injected as trusted context, and fast-clock pages are
  flagged to re-fetch the live source instead of trusting the copy. Design
  taste docs carry an owner + review-after date and are skipped when expired.
  Every shared-memory write is recorded in a provenance ledger
  (`GET /api/memory-ledger`) with claim/source/owner/scope/review date.

## Key architecture

| Component | What it owns |
|-----------|-------------|
| `world/serve.py` | FastAPI app: state DB, model calls, tools, auth, spend ledger, simulation driver, team/director hierarchy, model-tier refresh |
| `world/bank.py` | The Bank: spend ledger + per-service budget gates, spend cap, forecasts (re-exported from serve.py) |
| `world/auth.py` | Player auth: credential creation, password hashing, login sessions, rate limiting (re-exported from serve.py) |
| `world/notify.py` | Escalation + player-notification email/telegram senders (re-exported from serve.py) |
| `world/sim.py` | Server simulation engine: movement, task lifecycle, ceremonies, peer gates, sprint lifecycle, governance |
| `world/content.py` | Per-room content executors: research, code writing/review, weather, media, distill, spikes, bank teller |
| `world/web_helpers.py` | Pure HTML stripping, link extraction, HTTP-date parsing, SSRF host check |
| `world/sim_helpers.py` | Pure priority normalization, room/team derivation, sprint/product id generation, village + memory-clock helpers |
| `world/index.html` + `world/*.js` | Browser renderer; the client is a viewport, never the state machine |
| `world/tests/` | Python test suite (3,520 tests) covering every path |

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
  `.env.template` for the documented format. Only `OPENROUTER_API_KEY` is
  required; every other key is optional and simply disables its feature when
  blank or absent.

## Notes

- Agent identities live ONLY in `think_tank.db`. The codebase has zero hardcoded
  agent names; the roster is seeded from `.env` on a cold DB.
- Governance is idle-quiet: an idle think tank spends no model budget.
- The high tier (the expensive one) is bounded twice: a per-model price
  ceiling and a monthly spend cap, both configurable via `.env` and 0/unset
  by default (disabled).
- Room descriptions are director-editable; room geometry is not (it is a
  code-graph invariant).
### Coinbase spot research and paper trading

The player inbox includes a **Needs your decision** panel showing each current
blocker's evidence, responsible agent and recommended action. Supply inputs,
clarify scope with measurable acceptance criteria, assign a repair, retry after
Docker is healthy, or cancel. Resumed/completed/dismissed work no longer leaves
an unresolved inbox notice.

The Coinbase workflow queues Aria's research, Dorian's strategy, Mira's risk
review and Rowan's paper monitoring sequentially. Each stage requires a saved
report and structured acceptance manifest; missing or explicitly incomplete
requirements block progression. Research requires observed quotes and at least
100 actual candles per proposed pair. Strategy metrics must match an executed
backtest output and use non-overlapping training/test windows. Risk review must
read the saved evidence and address the $50 total-equity loss constraint.
These gates establish evidence and reproducibility requirements, not profitability.
Weather scheduling and consensus can be disabled by the Coinbase focus migration.

Once research/strategy/risk pass, player-authorized paper orders enforce the
proposed pair list, order limit and allocation. Portfolio bids are required for
held assets; new exposure is blocked at $50 of observed rolling 24-hour equity
drawdown. This check runs on order requests and is not a continuous stop-loss
or a live exchange order. Rowan's monitor observes the simulated ledger every
15 seconds after the approved paper stage starts. Live execution remains unavailable.

Coinbase public market data is available through authenticated
`GET /api/trading/coinbase/market/BTC-USD` and
`GET /api/trading/coinbase/candles/BTC-USD?granularity=ONE_HOUR&limit=100`.
Research agents have `coinbase_spot_market` and `coinbase_spot_candles` tools.
These public Coinbase endpoints require no exchange API credentials.

`GET /api/trading/coinbase/status` reports the execution mode.
`GET /api/trading/coinbase/paper/account` returns an isolated simulated ledger.
Logged-in players can POST `/api/trading/coinbase/paper/orders` with
`{"mode":"paper","productId":"BTC-USD","side":"BUY","baseSize":"0.0001","clientOrderId":"unique-id"}`.
Reusing an ID for the same order returns its original simulated fill.
Conflicting IDs, overselling, invalid increments, stale quotes and oversized
orders are rejected. The paper account starts with $10,000 simulated USD,
a $100 simulated order ceiling, estimated 60-basis-point fees and
10-basis-point slippage. These are testing assumptions, not researched strategy
parameters or actual Coinbase balances/fees. A $50 rolling 24-hour realized
loss limit blocks new buys while permitting exits; unrealized losses are not
yet part of that paper limit.

Live trading and withdrawals are not implemented. The agreed research brief
is Coinbase spot trading with a $50 loss limit; agents must propose allocation,
order sizing and eligible pairs with evidence, held-out backtesting and paper
results. The live executor still needs approved parameters, account credentials,
total-equity risk enforcement, strategy approval and order/fill reconciliation.

### Closed-position accounting

The Bank reports realized trading P&L per agent and quote currency alongside
its existing operating-spend review. Trading results use actual fill prices:
long gross P&L = (exitPrice - entryPrice) * quantity; short reverses the sign.
Net P&L subtracts fees. Model/API costs remain separate because historical
spend is attributed to services/tasks, not reliably to trading agents.

A future broker adapter should POST `/api/bank/closed-positions` only after
confirming a **fully closed** position. Authenticate with the player session
or the owning agent's `X-Agent-Key`. Example payload:

```json
{
  "broker": "paper-account-1",
  "positionId": "position-123",
  "agentId": "ben",
  "symbol": "ABC",
  "quoteCurrency": "USD",
  "status": "closed",
  "side": "long",
  "quantity": "2",
  "entryPrice": "100.10",
  "exitPrice": "110.20",
  "fees": "0.20",
  "openedAt": "2026-10-01T12:00:00Z",
  "closedAt": "2026-10-02T12:00:00Z"
}
```

Use weighted-average executed entry/exit prices, total closed quantity, and
all entry/exit commissions and financing costs in the quote currency. Use a
broker/account namespace and a unique position ID per completed lifecycle.
Records are immutable: identical retries return the stored record; conflicting
retries are rejected. SQLite stores closures independently of world autosaves.
Decimal strings preserve monetary precision. The example nets 20.00 USD.

GET `/api/bank/profit-loss` with a player session returns closure records and
per-agent/currency gross P&L, fees, net P&L, wins, losses, breakeven counts, and
win rate. The Bank teller also includes these results even before any API spend.
No currencies are combined or converted. This foundation supports linear
instruments; derivatives with contract multipliers, partial-close fill matching,
open-position valuations, and live broker execution need adapter work before use.


### Recovery and spending controls

Agent work deadlines are soft thresholds. A running worker keeps its assignment until it returns a real result; crossing the threshold does not fail or restart its task. Missing workers and actual execution failures still trigger recovery controls.

Quality pipelines submit background jobs and poll their progress through `/api/pipeline/jobs/{job_id}`. Agent sandbox commands have no wall-clock cutoff by default; execution policy and resource constraints still apply. HTTP transport allowances scale with requested response size, pipeline step count and observed runtimes. Runtime estimates and background job records are in memory; server restarts use the existing in-flight task recovery. Explicit story closure prevents staged changes from publishing afterward.

Coding tasks generate changes in a separate sandbox workspace. Only changes that pass the real quality pipeline replace the working workspace; failed stages preserve the working files and the previous successful workspace remains available for recovery.

Automatic retries are recorded on the original story across subtasks, gate resets and server restarts. Two failures without new validation evidence, or four failures in one recovery window, pause the story for a specific player decision. A player decision grants one additional attempt and keeps the failure history. Policy denials, missing inputs, unavailable execution services and budget failures pause immediately instead of launching generic code repair.

The optional request budget covers planning, stories, subtasks and reviews together. Blank leaves the request budget unset. Calls under a request budget run serially for that request and use configured model token prices to bound the next response against remaining funds; requests with unknown prices pause. Individual task ceilings remain additional controls. Provider billing changes can differ from configured prices, so recorded actual spending remains authoritative.
