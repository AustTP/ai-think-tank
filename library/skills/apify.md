# Apify

## Purpose
Reach for this before running any structured web-scraping actor through the
Apify platform -- e.g. pulling a site's data at scale, scraping X/Twitter or
LinkedIn, or harvesting a list of pages systematically. The think tank holds an
`APIFY_API_KEY` and this is the scraping arm that complements (and overlaps
with) Treg.

## Key facts
- Base URL: `https://api.apify.com/v2`. Auth: `Authorization: Bearer <token>`
  header (recommended -- never put the token in a query param where it lands in
  logs).
- **Run an actor**: `POST /v2/actors/{actorId}/runs` with the actor's input as
  a JSON body. Returns 201 with the run object, including `defaultDatasetId`.
  Set `waitForFinish` (0-60s) to have the server block until a terminal state.
- **Run a task**: `POST /v2/actor-tasks/{taskId}/runs` (input overrides the
  task's saved defaults per-field).
- **Check a run**: `GET /v2/actor-runs/{runId}`. Statuses: `READY`, `RUNNING`,
  `SUCCEEDED`, `FAILED`, `TIMING-OUT`, `TIMED-OUT`, `ABORTING`, `ABORTED`. The
  run object carries `defaultDatasetId` plus `usage`/`usageTotalUsd`/`stats.
  computeUnits` for cost logging (usage fields settle ~10s after completion).
- **Fetch results**: `GET /v2/datasets/{datasetId}/items?format=json&clean=1&
  limit=&offset=`. `clean=1` strips `#`-hidden fields. The dataset ID alone is
  enough to read items (no auth needed) -- treat it as capability, not secret.
- **Delete a dataset**: `DELETE /v2/datasets/{datasetId}`. Always clean up
  datasets you created once results are harvested.
- **Account / usage**: `GET /v2/users/me` (plan, `maxMonthlyUsageUsd`),
  `GET /v2/users/me/usage/monthly` (this cycle's spend, per service). The think
  tank's Bank already reconciles against these live.
- **Store discovery**: `GET /v2/store?search=...` (no auth), `GET /v2/acts/
  {actorId}` (input schema in `inputSchema`). Inspect an actor's schema BEFORE
  running it -- every actor defines its own input.

## Budget reality (verified against the Free plan)
- The account is **Free tier**: prepaid ~$5/month of compute credits, no
  pay-as-you-go. When credits run out the account is **blocked until the next
  cycle**; unused credits do not roll over.
- **Treg's X/Twitter and LinkedIn endpoints are sourced from Apify actors**
  (per library/skills/treg.md) -- they draw from the SAME account and the SAME
  monthly cap. Agent-driven Apify runs and Treg tool calls silently deplete one
  shared budget. Always check `GET /v2/users/me/usage/monthly` before a big
  run and log every run's `usageTotalUsd` into the Bank.

## Policy for this think tank
1. Check the monthly usage cap before any non-trivial run; fail closed and
   escalate rather than spending the last of the month's credits on a
   speculative scrape.
2. Cap each run's downside with `maxTotalChargeUsd` and `timeout` when the
   actor supports them.
3. Log every run's real cost (`usageTotalUsd`) through the Bank ledger like
   OpenRouter/Treg calls, so Apify shows up in spend accounting.
4. Delete datasets after harvesting; never leave results parked indefinitely.
5. Prefer well-known actors with clean input schemas; verify an unknown
   actor's schema (`inputSchema`) before the first run.

## Sources
- https://docs.apify.com/api/v2 (API overview, auth)
- https://docs.apify.com/api/v2/actors-runs-post.md (run an actor)
- https://docs.apify.com/api/v2/actor-run-get.md (run status + usage fields)
- https://docs.apify.com/api/v2/dataset-items-get.md (dataset item fetch)
- https://docs.apify.com/account/subscriptions and /account/limits (Free plan
  credits, no rollover, blocked when exhausted)

## Lessons learned
- The one mistake to avoid is treating Apify as unlimited just because it's
  "prepaid" -- the Free plan hard-stops mid-month when credits are gone, and
  because Treg shares the same account, Treg's X/LinkedIn tools can be the
  thing that silently eats the budget an agent wanted for an Apify scrape.
