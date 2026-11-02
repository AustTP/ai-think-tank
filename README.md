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

Secrets live in `.env` at the repo root. Git ignores that file. Copy `.env.template` to `.env` and set at least `OPENROUTER_API_KEY`. Values may be single- or double-quoted so `source .env` stays safe (an unquoted `SEED_ROSTER` pipes through the shell's grammar); the server's loader strips one matching quote pair. If the admin credentials and `SERVER_SECRET` are absent, the server generates them on first boot.

Only `OPENROUTER_API_KEY` is required. Every other key in `.env.template` is optional. Leave a key blank or omit it and its feature stays off. The server reads each key on its own and treats blank the same as unset. A partial `.env` works. Fill in only the integrations you use.

Run the tests:

```bash
python3 -m pytest world/tests -q
node world/tests/js_coverage.mjs
```

`bash world/tests/run_all.sh` runs both plus the package-boundary check. The suite has 8,832 collected Python tests and a 90% JavaScript coverage floor.

For restart and recovery evidence, run the failure-injection soak harness:

```bash
python3 scripts/soak_tank.py --duration 21d --kill-every 6h
```

It seeds live and archived tasks, SIGKILLs the server, restarts it, performs one DB-restore test, and writes a JSON report plus JSONL events. It fails on any lost task or seeded-task mutation.

Prompt-contract evals live in `evals/`:

```bash
python3 evals/run_evals.py --dry-run
TANK_AGENT_KEY=... python3 evals/run_evals.py
```

## Theo's control room

Theo keeps the building honest. He cares about state, gates, and where the money goes.

- **The database.** `think_tank.db` is SQLite. Every action gets logged. Agent history can be replayed. Nothing is lost on restart.
- **The server.** `world/serve.py` loads the parts in `world/server/serve_parts/`. The FastAPI app owns the simulation, model calls through OpenRouter, the shared Library, spend accounting, and session auth.
- **The simulation.** `world/sim.py` loads the parts in `world/village/sim_parts/`. It moves agents, assigns tasks, runs ceremonies, enforces peer-review gates, and drives sprint lifecycles in one read-modify-write pass every 6 seconds. Wake-on-mail uses a durable queue with coalescing, claim recovery, and marker restore after a restart. Task dependencies are a small DAG: `dependsOn` takes one task id or a list, a gated card releases only when every predecessor has landed (join semantics), and self-edges plus closing edges are pruned where the durable task record is born, so a cycle is dropped rather than deadlocked.
- **The executors.** `world/content.py` loads the parts in `world/agents/content_parts/`. It handles research crawls, code writing and review, weather, distillation, media digests, bank reviews, and time-boxed investigations called spikes.
- **Plan approvals.** A large request is proposed as a durable plan revision before any work is filed. The player can approve, reject with a reason, or revise it. Approval binds the exact plan hash and route; a stale team or authority forces a revision instead of applying an old decision.
- **The client.** `world/index.html` draws the tank as a pixel-art canvas and lets you interact. It is a viewport. The server owns every position, task, and decision.
- **The model tiers.** Work starts on the cheap `low` tier. JEV decides which calls earn `mid` or `high`. Coding tasks use the coding tier automatically. 3D work aliases to coding until a dedicated model is configured. The refresh re-picks each band's best-value model from the live OpenRouter catalog. `world/model_providers.json` can point any band, or every band, at a custom endpoint or local model. A custom endpoint is a pin: the tier refresh and the 30-minute price watch both skip that band, and the tier API reports the pin instead of the unused auto-pick. Remove the entry and `POST /api/policy/reload` to let the tank re-pick it.
- **Village art.** A director can outsource a new map as one card: `POST /api/villages/<id>/art` quotes the PixelLab call, then queues a supervise card. The `generate_village_map` tool writes the background, `collision_grid_<id>.json`, `door_triggers_<id>.json`, and a `scenes.json` entry. Collision and doors are exact (mirrored or copied from the main map), never guessed from the image. The quote is checked against a per-call cap, the PixelLab balance, and the shared monthly cap before any spend; the API's real `usage.usd` is charged to the actor. A new scene becomes walkable from the south outlet's travel list on the next page load.
- **API prices.** `world/api_services.json` carries a `pricing` block per service (free, fixed, per page request, per size bucket, per item, or explicitly variable). The `service_prices` tool lists or quotes any call, and every `api_call` result includes a model-visible price line and a `priceQuote`. Free services report $0; variable ones name the budget that enforces them. `GET/POST /api/service-pricing` exposes the same catalog.
- **Price drift.** Every call that returns a billed amount records the quote and the actual charge. A 6-hour audit compares the medians; a 20% gap over at least 3 calls logs `service_price_drift` and sends a Telegram notice. `/prices [service]` shows the catalog, budgets, and drift. The catalog is never auto-edited: a drift is a finding for the operator, and `pricing.source_url` is where the published price is checked. Services that return no billed amount cannot be detected from calls; they rely on that periodic page review.
- **A2A.** The tank is one A2A agent at `/.well-known/agent-card.json` and `POST /a2a`. A player session, or a director/admin agent key sent as `Authorization: Bearer <key>`, can submit a goal (`message/send`), read it (`tasks/get`), or cancel it while its cards are still queued (`tasks/cancel`). A goal files a large request; the receiving team's breakdown turns it into cards; completed cards return their Library paths as artifacts. Streaming and push notifications are off.
- **URL reputation.** Before the JEV gate classifies a browser or `api_call` request to an external URL, the tank consults outside reputation: RDAP domain age, URLhaus, VirusTotal, and Google Safe Browsing. Verdicts are `trusted` / `checkable` / `untrusted`. A trusted URL's plain GET passes without JEV; an untrusted one is refused; writes never ride reputation -- they always see the JEV gate. Verdicts cache 24 hours per host; the keyed lookups are Bank-accounted, day-keyed quotas (`VIRUSTOTAL_DAILY_BUDGET` 400, `SAFEBROWSING_DAILY_BUDGET` 10,000, at least 15s between VirusTotal calls). Set `VIRUSTOTAL_API_KEY` and `SAFEBROWSING_API_KEY` in `.env` to arm the keyed sources; without them the score still runs on RDAP age and URLhaus.
- **Outbound webhooks.** The tank received webhooks but could not notify anyone. `POST /api/webhooks` registers a subscribed URL with optional shared secret and event patterns; when an action-log kind matches, the tank POSTs the record with `X-Tank-Event` and, when a secret is set, `X-Tank-Signature: sha256=<hex hmac>` over the exact body. Delivery is one bounded POST, no retries; targets must resolve to public hosts (SSRF-checked at registration and at every delivery). Secrets are stored server-side and never listed back.
- **Code grounding.** A read-only GitHub repo tool gives agents ephemeral workspaces over real repositories. MCP servers load from a two-layer registry: the shipped `mcp_servers.json` (REA, plus GitHub's official remote MCP server at `api.githubcopilot.com/mcp/` authenticated by `GITHUB_TOKEN` as a bearer credential), and an operator-local `mcp_servers.local.json` that overrides by id.- **The budgets.** Page requests are capped at 1,000 per month. The hard spend cap defaults to $50 per month and is set with `SPEND_CAP_USD`. The high tier has its own monthly budget, which defaults to $5. JEV respects both. Every priced model call also writes a durable usage receipt, replayed after an interrupted projection; unpriced usage holds that task's budget by default (`UNPRICED_USAGE_POLICY=block`). The URL-reputation lookups have their own day-keyed Bank-accounted quotas (see above), not the monthly dollar cap.
- **Plain writing.** Prose replies at the model-call boundary carry a "write plainly, no filler" instruction and a word-ban list. The tank spends fewer tokens on the same information. It is on by default. You can opt out per request. JSON prompts are exempt.

## Nora's personnel floor

Nora keeps the roster, the ceremonies, and the memory. She knows that a think tank is not a stack of code. It is a room full of agents with tasks, reputations, and something to prove.

- **Villages.** A village is the top-level identity and memory boundary. Every agent belongs to one. The default is `main`. Wiki pages and design taste docs are scoped to the writer's or reader's village, so one village's memory never leaks into another. Every village has a regional VP (role `regional_vp`), installed at creation, backfilled on the tick, or appointed by the player through `/api/villages/chief`. The player creates villages and assigns agents through `/api/villages` and `/api/villages/assign`.
- **Memory hygiene.** Every wiki page records an owner, a clock (slow or fast), a source, and a `reviewAfterMs` expiry. Expired pages stop counting as trusted context. Fast-clock pages get flagged so the village re-fetches the live source instead of trusting the copy. Design taste docs carry an owner and a review-after date. Expired ones are skipped. Every shared-memory write lands in a provenance ledger at `GET /api/memory-ledger`, with claim, source, owner, scope, and review date.
- **Knowledge sources.** Teams ground on shared sources of truth: append-only truth documents (agents add attributed entries, nobody rewrites) and template repositories (read-only pointers to a GitHub repo or a granted local folder -- agents study the template and build around it; the template stays pristine). Both scope global or per-team. Agents read them through the `knowledge` tool; the player curates through `/api/knowledge`. The seeded global template is the operator's Intel Forge repo.
- **Bookmarks.** A shared link library, global plus per-team. The player seeds globals through `/api/bookmarks`; agents read global plus their team's links through the `bookmarks` tool, and admin agents curate globals as the player's delegate. A link a player sends in the ask lane is auto-allowlisted for the reply task, so a conversation never stalls on the gate.- **The roster.** Agent identities live only in `think_tank.db`. The codebase hardcodes no agent names. The roster is seeded from `.env` on a cold database.
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
| `world/tests/` | Python and JavaScript test suites (8,832 collected Python tests plus the JS coverage harness) |

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
