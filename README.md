# AI Think Tank

AI Think Tank is a running AI organization you delegate to, not a toy and not a chat window. LLM agents hold roles in a durable structure: they pick up work from real cards, refine backlogs, peer-review each other, escalate when unsure, get hired and fired, and leave behind reports, code, and written receipts you can audit. The pixel-art building is one monitoring view. The system is the server.

Two notes taped near that view:

> **Theo, Control Room:** The building runs whether you watch it or not. Every position and every decision lives on the server. The browser is a window, not the machinery.

> **Nora, Personnel:** I keep the files. I know who was hired, who was let go, and who is still pretending the backlog is under control. Read this before you start the server.

What that means in practice:

- **Durable work.** Tasks, issues, plans, mail, and decisions are rows in a database, not messages in a chat. Restart the process mid-run and the tank resumes where it stopped.
- **Spending you can see.** Every model call is priced, logged, and gated. A deterministic-first policy engine (JEV) decides what may spend, and the free model pool is used before paid tiers.
- **Autonomy with a leash.** Agents browse, run read-only commands, write code in sandboxes, and file work, but money-losing or irreversible paths still route through a classifier or to you.
- **Self-observation.** The tank runs scheduled digests on itself, investigates its own failures, and keeps an epistemic ledger that separates what it asserted from what it established.

For the full architecture, read `DESIGN.md`. For the operating procedures, read `BUSINESS-PROCESSES.md`. This file is the operator's tour.

## Run it

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

`bash world/tests/run_all.sh` runs both plus the package-boundary check. The suite has 9,996 collected Python tests and a 90% JavaScript coverage floor.

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

## The server: work, gates, and money

This half of the tour is state, gates, and where the money goes.

Harness doctrine (after Zhang, Chakraborty & Khattab, "Language Model Harnesses Can Be Compositional Generalizers", 2026): a harness should keep every model call locally in-distribution instead of accumulating one long trajectory. The tank already does this structurally -- work is decomposed into cards, each resolved by fresh-context sub-calls; the groom cascade and the route-shadow net are cheap supervision standing in for expensive judgment; escalation, not context length, is the only reason a decision leaves the deterministic or calibrated lanes. The house routing rule lives in `world/core/jev.py`: deterministic code, then calibrated ML, then JEV, then the operator.

- **The database.** SQLite (`think_tank.db`) by default; set `THINK_TANK_DB_URL` (see `.env.template`) and the store routes through `world/db_backend.py` to Postgres instead. Every action gets logged. Agent history can be replayed. Nothing is lost on restart.
- **The server.** `world/serve.py` loads the parts in `world/server/serve_parts/`. The FastAPI app owns the simulation, model calls through OpenRouter, the shared Library, spend accounting, and session auth.
- **The simulation.** `world/sim.py` loads the parts in `world/village/sim_parts/`. It moves agents, assigns tasks, runs ceremonies, enforces peer-review gates, and drives sprint lifecycles in one read-modify-write pass every 6 seconds. Wake-on-mail uses a durable queue with coalescing, claim recovery, and marker restore after a restart. Task dependencies are a small DAG: `dependsOn` takes one task id or a list, a gated card releases only when every predecessor has landed (join semantics), and self-edges plus closing edges are pruned where the durable task record is born, so a cycle is dropped rather than deadlocked.
- **The executors.** `world/content.py` loads the parts in `world/agents/content_parts/`. It handles research crawls, code writing and review, weather, distillation, media digests, bank reviews, and time-boxed investigations called spikes.
- **Scheduled items.** The tank keeps its own runbook in `state['scheduledItems']`: recurring body work (a cadence, an owner role, a deliverable path) the sim mints and runs on the tick. Two items are seeded on first boot and are the regional VP's standing duty: a 30-minute Telegram alert digest that writes a Markdown window over alerts, internal signals, and integrity anomalies into `library/analysis/` (a quiet window -- no alerts, no internal signals, no anomalies -- writes nothing and logs `telegram_digest_quiet` instead), and an hourly village scan that reads the digest's surfaces and files cards when a surface is non-empty. The engine stamps `lastRunAtMs` per item, so a quiet period still counts as scanned.
- **Plan approvals.** A large request is proposed as a durable plan revision before any work is filed. The player can approve, reject with a reason, or revise it. Approval binds the exact plan hash and route; a stale team or authority forces a revision instead of applying an old decision.
- **The client.** `world/index.html` draws the tank as a pixel-art canvas; the Ops board and Telegram digests are the other views. All of them render server state. The server owns every position, task, and decision.
- **The model tiers.** Work starts on the cheap `low` tier. JEV decides which calls earn `mid` or `high`. Coding tasks use the coding tier automatically. 3D work aliases to coding until a dedicated model is configured. The refresh re-picks each band's best-value model from the live OpenRouter catalog. Free-tier endpoints are picked by measured utilization (least-utilized first, daily-quota pacing, exact Retry-After windows), so a throttled key does not take the whole pool down. `world/model_providers.json` can point any band, or every band, at a custom endpoint or local model. A custom endpoint is a pin: the tier refresh and the 30-minute price watch both skip that band, and the tier API reports the pin instead of the unused auto-pick. Remove the entry and `POST /api/policy/reload` to let the tank re-pick it.
- **Village art.** A director can outsource a new map as one card: `POST /api/villages/<id>/art` quotes the PixelLab call, then queues a supervise card. The `generate_village_map` tool writes the background, `collision_grid_<id>.json`, `door_triggers_<id>.json`, and a `scenes.json` entry. Collision and doors are exact (mirrored or copied from the main map), never guessed from the image. The quote is checked against a per-call cap, the PixelLab balance, and the shared monthly cap before any spend; the API's real `usage.usd` is charged to the actor. A new scene becomes walkable from the south outlet's travel list on the next page load.
- **API prices.** `world/api_services.json` carries a `pricing` block per service (free, fixed, per page request, per size bucket, per item, or explicitly variable). The `service_prices` tool lists or quotes any call, and every `api_call` result includes a model-visible price line and a `priceQuote`. Free services report $0; variable ones name the budget that enforces them. `GET/POST /api/service-pricing` exposes the same catalog.
- **Price drift.** Every call that returns a billed amount records the quote and the actual charge. A 6-hour audit compares the medians; a 20% gap over at least 3 calls logs `service_price_drift` and sends a Telegram notice. `/prices [service]` shows the catalog, budgets, and drift. The catalog is never auto-edited: a drift is a finding for the operator, and `pricing.source_url` is where the published price is checked. Services that return no billed amount cannot be detected from calls; they rely on that periodic page review.
- **A2A.** The tank is one A2A agent at `/.well-known/agent-card.json` and `POST /a2a`. A player session, or a director/admin agent key sent as `Authorization: Bearer <key>`, can submit a goal (`message/send`), read it (`tasks/get`), or cancel it while its cards are still queued (`tasks/cancel`). A goal files a large request; the receiving team's breakdown turns it into cards; completed cards return their Library paths as artifacts. Streaming and push notifications are off.
- **URL reputation.** Before the JEV gate classifies a browser or `api_call` request to an external URL, the tank consults outside reputation: RDAP domain age, URLhaus, VirusTotal, and Google Safe Browsing. Verdicts are `trusted` / `checkable` / `untrusted`. A trusted URL's plain GET passes without JEV; an untrusted one is refused; writes never ride reputation -- they always see the JEV gate. Verdicts cache 24 hours per host; the keyed lookups are Bank-accounted, day-keyed quotas (`VIRUSTOTAL_DAILY_BUDGET` 400, `SAFEBROWSING_DAILY_BUDGET` 10,000, at least 15s between VirusTotal calls). Set `VIRUSTOTAL_API_KEY` and `SAFEBROWSING_API_KEY` in `.env` to arm the keyed sources; without them the score still runs on RDAP age and URLhaus.
- **Read-only commands skip the classifier.** A command that parses as provably read-only (`world/server/serve_parts/182_readonly_shell.py`: every segment a whitelisted read-only binary, output redirection confined to /dev/null, no interpreters or exec-flags) is pre-approved before the JEV classifier is asked, inside `_classify_command`. Plain `ls`, `cat`, pipes of reads, and read-only git never pay for a model round-trip. Refusals run before allows: the host-path check outranks the rule table. A decoded base64 wrapper always goes to JEV -- the original wrapper is what would run. This is lane 1 of the JEV routing rule (see `world/core/jev.py`): deterministic code first, calibrated ML second, JEV for open-ended judgment, the operator for the irreversible.
- **Outbound webhooks.** The tank received webhooks but could not notify anyone. `POST /api/webhooks` registers a subscribed URL with optional shared secret and event patterns; when an action-log kind matches, the tank POSTs the record with `X-Tank-Event` and, when a secret is set, `X-Tank-Signature: sha256=<hex hmac>` over the exact body. Delivery is one bounded POST, no retries; targets must resolve to public hosts (SSRF-checked at registration and at every delivery). Secrets are stored server-side and never listed back.
- **Code grounding.** A read-only GitHub repo tool gives agents ephemeral workspaces over real repositories. MCP servers load from a two-layer registry: the shipped `mcp_servers.json` (REA, plus GitHub's official remote MCP server at `api.githubcopilot.com/mcp/` authenticated by `GITHUB_TOKEN` as a bearer credential), and an operator-local `mcp_servers.local.json` that overrides by id.
- **The budgets.** Page requests are capped at 1,000 per month. The hard spend cap defaults to $50 per month and is set with `SPEND_CAP_USD`. The high tier has its own monthly budget, which defaults to $5. JEV respects both. Every priced model call also writes a durable usage receipt, replayed after an interrupted projection; unpriced usage holds that task's budget by default (`UNPRICED_USAGE_POLICY=block`). The URL-reputation lookups have their own day-keyed Bank-accounted quotas (see above), not the monthly dollar cap.
- **Live feeds.** External data streams are operator-managed registries, not always-on costs: websocket feeds (`kraken`, `aisstream`) and RSS/Atom poll feeds live in `world/ws_feeds.json` and `world/rss_feeds.json`, seeded into the database at first boot. For short-lived needs an agent can grant itself a temporary window (1-24 h) through the `manage_feed_access` tool or `POST /api/feeds/access` (rate-limited, logged, owner visible); the collectors start and stop on the window without touching the feed's permanent status. Removing a feed is a registry status change; the ws API's remove endpoint overwrites the live spec with a stub, so operator removals are done at the registry, not the API. The `/n` Ops board reads team-first: cards group by team, with a `Group by: Team | Project` toggle keeping the old project view.
- **Plain writing.** Prose replies at the model-call boundary carry a "write plainly, no filler" instruction and a word-ban list. The tank spends fewer tokens on the same information. It is on by default. You can opt out per request. JSON prompts are exempt.
- **Change windows.** Agent-initiated code changes never land live: an armed self-edit applies only inside an operator-approved dormancy window. A normal change request needs at least 6 hours' notice and its whole window must sit outside Eastern-time business hours (Mon-Fri 09:00-17:00 America/New_York) -- the tank ships while the office is closed. Only `tankDown=True` skips the hours gate. A missed window fails closed (`dormancy_window_missed`); nothing auto-applies.
- **Session vault.** Agents do not hold standing cookies for operator web accounts. An agent that hits an auth wall files an `operator_login_request`; the operator signs in once through a vault-profile browser (`world/tools/agent_browser_login.py`), and the run's success writes back to the canonical service session in the vault. A flow op can carry a bounded federated-OAuth click sequence under a session grant -- the dance cannot survive between acts, so every act re-presents the grant.
- **Agent-improvable rule tables.** `world/agent_rules.json` is an additive overlay over the code's built-in rule lists, so agents can extend the read-only command gate, the spike triage vocabulary, and the digest's signal list through a reviewed edit instead of a code change. The loader validates entries against per-namespace patterns and caps, reloads within a second of saving, and fails closed on a corrupt file. Additive only: an overlay entry can never remove a built-in rail.
- **Cross-village mail.** Villages exchange work through a durable mail queue instead of reaching into each other's state. `POST /api/villages/mail` sends; a relay pass on the tick delivers. This is the boundary that will let villages become separate processes later.
- **Sandbox runtime.** Set `SANDBOX_RUNTIME=runsc` in `.env` and every agent lane runs under gVisor with proxy hostname resolution pinned to IP literals. Per-run memory accounting (`run-mem` log lines) and an RSS watermark dispatch guard (`TANK_RSS_GUARD_FRACTION`, `TANK_RSS_GUARD_BYTES`) keep a runaway tool loop from starving the server.
- **Daily briefing.** `GET /api/briefing?days=N` joins investigations, evidence relations (strength grades E0-E4), outcome metrics, and route-shadow findings into one operator read. The route-shadow net is a tiny trained classifier that shadows the live triage lane and reports disagreement only -- it never decides.

## People, governance, and memory

The other half of the tour is the roster, the ceremonies, and the memory. A think tank is not a stack of code; it is a staff with roles, tasks, reputations, and records.

- **Villages.** A village is the top-level identity and memory boundary. Every agent belongs to one. The default is `main`. Wiki pages and design taste docs are scoped to the writer's or reader's village, so one village's memory never leaks into another. Every village has a regional VP (role `regional_vp`), installed at creation, backfilled on the tick, or appointed by the player through `/api/villages/chief`. The player creates villages and assigns agents through `/api/villages` and `/api/villages/assign`. Villages talk through the cross-village mail queue (see the server list), never through each other's agent state.
- **Memory hygiene.** Every wiki page records an owner, a clock (slow or fast), a source, and a `reviewAfterMs` expiry. Expired pages stop counting as trusted context. Fast-clock pages get flagged so the village re-fetches the live source instead of trusting the copy. Design taste docs carry an owner and a review-after date. Expired ones are skipped. Every shared-memory write lands in a provenance ledger at `GET /api/memory-ledger`, with claim, source, owner, scope, and review date.
- **Knowledge sources.** Teams ground on shared sources of truth: append-only truth documents (agents add attributed entries, nobody rewrites) and template repositories (read-only pointers to a GitHub repo or a granted local folder -- agents study the template and build around it; the template stays pristine). Both scope global or per-team. Agents read them through the `knowledge` tool; the player curates through `/api/knowledge`. The seeded global template is the operator's Intel Forge repo.
- **Bookmarks.** A shared link library, global plus per-team. The player seeds globals through `/api/bookmarks`; agents read global plus their team's links through the `bookmarks` tool, and admin agents curate globals as the player's delegate. A link a player sends in the ask lane is auto-allowlisted for the reply task, so a conversation never stalls on the gate.
- **The roster.** Agent identities live only in `think_tank.db`. The codebase hardcodes no agent names. The roster is seeded from `.env` on a cold database.
- **The work floor.** Agents move through rooms, pick up cards, hand work to reviewers, and wait at gates. An idle tank spends no model budget. Governance goes quiet when nothing is happening.
- **The exits.** Hiring and firing are real processes with approvals, reviews, and records. Nothing is fired quietly.

## Where things live

| Component | What it owns |
|-----------|-------------|
| `world/serve.py` + `world/server/serve_parts/` | FastAPI app: state DB, model calls, tools, auth, spend ledger, simulation driver, team and director hierarchy, model-tier refresh |
| `world/integrations/bank.py` | The Bank: spend ledger, per-service budget gates, spend cap, forecasts |
| `world/core/auth.py` | Player auth: credential creation, password hashing, login sessions, rate limiting |
| `world/integrations/notify.py` | Escalation and player-notification email and Telegram senders |
| `world/sim.py` + `world/village/sim_parts/` | Simulation engine: movement, task lifecycle, ceremonies, peer gates, sprint lifecycle, governance |
| `world/core/confirmations.py` | Durable two-phase confirmation proposals: target and argument binding, exact phrase, expiry, completion |
| `world/core/epistemics.py` | The epistemic ledger: asserted vs established, evidence lineage, validations, contradictions, staleness, replayable decisions |
| `world/core/epistemic_benchmark.py` | The prompt-injection monitoring benchmark: seeded conflicting evidence, snapshot graded on a 13-category answer key |
| `world/core/redact.py` | Edge secret-scrubbing applied before any ledger write (keys, bearers, JWTs, credential URLs) |
| `world/core/agent_replay.py` | Agent-run replay: typed action_log frames, run stats, scrub-through text form |
| `world/core/seer.py` | Failure investigations: symptom classification from the action_log tail, question-registry findings, remediation cards past threshold |
| `world/core/issue_engine.py` | Failure clusters -> root cause -> deduped fix cards + epistemic validations |
| `world/core/harness_optimizer.py` | Measure-first revision of the tank's own memory doctrine: failure clusters -> registry page hypothesis -> decision -> measured keep-or-supersede |
| `world/server/confirmations_api.py` | Player and agent HTTP surface for proposing, confirming, listing, and cancelling actions |
| `world/server/migrations.py` + `world/migrations/` | Versioned SQLite migrations with checksums |
| `world/core/model_config.py` | Model-band validation and operator table |
| `world/content.py` + `world/agents/content_parts/` | Per-room content executors: research, code writing and review, weather, media, distill, spikes, bank teller |
| `world/integrations/web_helpers.py` | Pure HTML stripping, link extraction, HTTP-date parsing, SSRF host check |
| `world/village/sim_helpers.py` | Pure priority normalization, room and team derivation, sprint and product ID generation, village and memory-clock helpers |
| `world/core/jev.py` | The JEV decision engine: quorum sampling policy, choice parser, per-kind floors, and the routing rule (deterministic code -> calibrated ML -> JEV -> operator) |
| `world/index.html` + `world/js/*.js` | Browser renderer. The client is a viewport and never the state machine. |
| `world/tests/` | Python and JavaScript test suites (9,963 collected Python tests plus the JS coverage harness) |

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

Health surfaces at `/api/health`; the personnel record is the roster itself. If either disagrees with this tour, the code wins and the doc gets fixed.

## Operator knobs added 2026-11-22

Three capabilities landed with their own `.env` controls; all are documented
in `.env.template` and default to safe behavior.

- **YOLO object detection on Colab** (`391_vision_detect.py`): video frames
  captured by `/api/youtube-frames` get a detection pass as one ephemeral
  Colab job. `YOLO_DETECT_ENABLED=0` disables the pass,
  `YOLO_DETECT_MODEL` picks any ultralytics slug (default `yolov8n.pt`),
  `YOLO_DETECT_CONF` is the confidence floor, `YOLO_DETECT_MAX_FRAMES`
  caps the batch, `YOLO_DETECT_RUNTIME` picks gpu/cpu.
- **Rare-day Colab IP lane** (also `391_vision_detect.py`): when the VM's
  datacenter IP draws a 403/429 from the plain browse path, one best-effort
  fetch of the same already-cleared URL runs through a Colab runtime's
  egress. Same Colab budget and account-affinity gates as every other Colab
  job. `COLAB_FETCH_ENABLED=0` shuts the lane off.
- **Jev institutional memory** (`392_jev_lessons.py`): confident
  director-chain verdicts on Jev's unsure cases are stored as lessons the
  safety gate recalls before escalating the same case again. Denies
  generalize by host/command-family; allows stay URL-exact. `JEV_LESSONS_DIR`
  overrides the store location (test hook).

## Security

Read `SECURITY.md` before exposing this server to a network. Never commit
`.env`, cookies, API keys, tokens, or real user data. Report a vulnerability
through GitHub's private vulnerability reporting, not a public issue.
