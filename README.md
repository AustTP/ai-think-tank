# AI Village

A single-screen, top-down pixel-art village (SNES / Stardew / Game Boy cadence) that
large-language-model agents actually **live** in. Agents walk between rooms, work when
there's work, idle when there isn't, get hired and (rarely) fired by a governance
loop, and file reports against each other. You can drop in as a playable character —
hand out tasks, call a meeting, or just watch.

The backend is **authoritative and persistent**: closing the browser never resets
anything. The simulation engine runs server-side and ticks whether or not a client is
attached (`village.db` is SQLite; every action is logged; agent history is replayable).

See `DESIGN.md` for the full design + execution plan and the project's history
(including why the art-driven approach was chosen over two earlier failed attempts).
`world/` contains the full implementation (server, sim, renderer, tests).

## What it is

- A persistent, simulated village of LLM agents, each with a typed identity, a role, a
  `state.json` + `MEMORY.md`, an agent key, and room-scoped tool access.
- A real-time renderer built as a single HTML canvas (`world/index.html`) that draws a
  tiled outdoor map, enterable buildings, and walking agents.
- A server (`world/serve.py`, FastAPI) that owns the state database, the model calls
  (via OpenRouter), the project library, per-agent file access, the activity/passport
  logs, and the server-side simulation loop.
- A movement + pathfinding core (`world/sim.py`) ported **byte-for-byte** from the
  original client (`world/tasks.js`), with differential parity tests so the two can't
  drift.

## The key architectural move: the sim runs server-side

Originally the browser ran the whole loop via `setInterval` timers — close the tab and
the village froze. The migration made the server the authoritative engine:

| Phase | What moved server-side | Commit |
|-------|------------------------|--------|
| 1 | Decide-throttle + `state['sim']` tick loop (`/api/sim/status`) | `fb20782` |
| 2 | Pathfinding + movement ported to `sim.py` (byte-parity tests); `sim.owner -> 'server'` | `a27e0c9` `c7636a9` `ec63b82` `c036b5a` |
| 3 | Task lifecycle (queue→assign→walk→arrive→work→complete→off-duty) + per-room content executors (research, weather, media, skill-review, pressoffice coding/review) | `9e4b477` … `802b3bc` |
| 4 | Governance (auto-hire + auto-firing review) as one cadence pass | `a09d025` |
| 5 | Player-intent API + client-as-pure-renderer + "runs closed" integration test | `a09d025` |

The browser is now a **renderer/player** under `sim.owner == 'server'`: it polls
`/api/sim/agents`, lerps positions toward server truth, and the only state it writes
back through the merge is player-facing (chat, mailbox, reports, intent). Everything
the engine owns — positions, work, tasks, the roster, governance, room definitions —
is carried forward by `_merge_server_owned` so a renderer's autosave can't clobber it.

The "runs closed" proof is `world/tests/test_runs_closed.py`: it boots the real
headless loop with an injected clock and a fake content executor, and asserts a task
reaches completion with the browser closed.

## Running it

```bash
cd world
python3 serve.py 8936
# open http://127.0.0.1:8936  (default port)
```

First run (a genuinely fresh checkout with no database yet):

1. Start the server as above.
2. The startup log prints the auto-created admin account **once**, including a
   randomly generated password — capture it (it is never shown again):

   ```
   [auth] First run -- admin account created.
   [auth] username: admin
   [auth] password: <generated>  (shown once -- save it)
   ```

3. Log in at `http://127.0.0.1:8936` with that username/password. The first
   authenticated state load materializes the default roster of agents and their
   roles into the database (they are seeded on demand, not pre-committed).

Secrets live in `.env` at the repo root (git-ignored). Copy `.env.template` to
`.env` and set at least `OPENROUTER_API_KEY` — required for any agent model call.
The admin credentials and `SERVER_SECRET` are **auto-generated** on first boot if
absent; you only need to create `.env` yourself if you want to set them
deterministically or set optional toggles (see `.env.template`).

Tests (Python + JS, including the parity and runs-closed suites):

```bash
cd world && bash tests/run_all.sh
```

## Repo layout (selected)

- `world/serve.py` — FastAPI app: state DB, model calls, tools, auth/session, merge
  guard, and the server-owned simulation tick loop. The largest file.
- `world/sim.py` — the pure, network-free server engine: pathfinding, movement,
  task lifecycle, content dispatch, governance. Unit-testable with zero model calls.
- `world/tasks.js` — originally the client engine; now mostly the legacy/dual-mode
  path and the shared `findPath` reference that `sim.py` is diff-tested against.
- `world/index.html` + `world/*.js` — the renderer and client logic.
- `world/tests/` — Python unit tests + Node parity tests + the runs-closed test.
- `agents/*` — each agent's live identity: `AGENTS.md`, `MEMORY.md`, `state.json`,
  `conversations/`, `reports/`. These are the village's own records, tracked.
- `library/` — the shared project library agents read/write (a hash-chain
  `.passport.json` guard log is created here on first run).
- `sandboxes/` — per-agent isolated development directories.

## Notes / known limits

- Room descriptions (`/api/rooms`) are director-editable; room keys + geometry are
  not (they are code-graph invariants).
- Governance is idle-quiet: an idle village spends no model budget.
- The legacy client-driven paths (`tasks.js assignBigTask`, client governance) remain
  behind the `!serverDrivesTasks` gate so ownership can flip back if the server isn't
  present.