# Client-Side Simulation Engine

A reusable, dependency-free agent-based simulation lab in vanilla JavaScript.
Each simulation is a plain ES module exposing a small, consistent API, so the
think tank can keep adding models without inventing new conventions.

## Run

Open `index.html` (loads `predatorprey-core.js`, `predatorprey-app.js`,
`weatherstation.js` and the panel wiring). Run the test suite with:

    node --test

## Public API

### Core utilities

- `rng.js` — `mulberry32(seed)`: deterministic PRNG. Returns a `function`
  producing floats in `[0, 1)`. Same seed, same stream.
- `sim-engine.js` — generic step/schedule engine used by the models.
- `world.js` — grid world helpers shared by the ecosystem models.
- `boids.js` — flocking agents (separation / alignment / cohesion).

### Predator–Prey ecosystem (`predatorprey-core.js`)

Exports `PredatorPreyEcosystem` (also attached to `window` by `index.html`).

- `new PredatorPreyEcosystem(opts)` — opts: grid size, initial prey/predator
  counts, seed.
- `ecosystem.step()` — advance one tick (prey reproduce, predators eat/starve).
- `ecosystem.stats()` — `{ grass, prey, predator, step }` counts, the same
  object shape published as `window.ppStats` in the demo app.
- Constants: `EMPTY = 0`, `GRASS = 1`, `PREY = 2`, `PREDATOR = 3`.

### Weather Station (`weatherstation.js`)

The weather station is the observability layer over the simulation lab: it
records per-model metrics and exposes them to panels and to headless runs.
`index.html` attaches a single ready-made instance as `window.WeatherStation`
(the module itself exports a factory/class for creating more instances).

Public methods on a weather-station instance:

- `record(name, value)` — append a sample under metric `name` and notify
  subscribers. `value` may be a number or an object; it is stored verbatim.
- `history(name)` — array of all samples recorded so far for `name`
  (oldest first). Returns an empty array for an unknown name.
- `latest(name)` — the most recent sample for `name`, or `undefined` if
  nothing has been recorded under that name yet.
- `names()` — array of all metric names recorded so far, in insertion order.
- `onChange(fn)` — subscribe a listener; it is called whenever new data is
  recorded. Returns an unsubscribe function: calling it stops further
  notifications for that listener. Listeners do not survive `reset()`.
- `reset()` — clear all recorded history (and listener state). After
  `reset()`, `names()` returns `[]` and `latest(name)` returns `undefined`.

Verified live in the page: `record` → `history`/`latest`/`names` reflect the
sample immediately, `onChange` fires on each `record`, and after
unsubscribing (or `reset`) no further callbacks fire.

### Weather Station adapters (`weatherstation-*.js`)

Thin glue modules that feed lab models into the weather station, one per
model so each stays independently testable:

- `weatherstation-predatorprey.js` — wire the predator–prey ecosystem's
  `stats()` into `record` on every step.
- `weatherstation-boids.js` — record flocking metrics per step.
- `weatherstation-sir.js` — record SIR compartment counts per step.
- `weatherstation-global.js` — creates and exposes the shared
  `window.WeatherStation` instance used by the page and the panel.
- `weatherstation-panel.js` — renders recorded metrics into the on-page
  panel; subscribes via `onChange` so it updates live.
- `weatherstation-headless.js` — run a model N steps with no DOM, recording
  into a weather-station instance; used for CI-friendly test runs.

### Demo app (`predatorprey-app.js`, `index.html`)

Canvas rendering plus controls for the ecosystem demo; publishes live
`ecosystem.stats()` output through the weather station.

## Conventions

- ES modules only, no build step, no dependencies.
- Every module ships with a co-located `*.test.mjs` runnable via `node --test`.
- Determinism: any randomness comes from `mulberry32` with an explicit seed,
  so headless runs are reproducible.

<!-- API:GENERATED START -->

## Public API

The list below is generated from the actual `export` statements in each module — it is verified against the sources by `api-docs.test.mjs`.

### `boids.js`

- `createFlock`

### `ca-engine.js`

- `createCA`
- `RULES`

### `predatorprey.js`

- `EMPTY`
- `GRASS`
- `PREY`
- `PREDATOR`
- `PredatorPreyEcosystem`
- `runSimulation`
- `createEcosystem`

### `rng.js`

- `mulberry32`
- `hashSeed`
- `Rng`
- `RNG`

### `sim-engine.js`

- `Agent`
- `Engine`
- `mulberry32`
- `World`

### `sir.js`

- `createSIR`

### `weatherstation-boids.js`

- `createBoidsStation`

### `weatherstation-headless.js`

- `toJSON`
- `runHeadless`
- `runHeadlessFinal`

### `weatherstation-predatorprey.js`

- `predatorPreyStation`

### `weatherstation-sir.js`

- `runEnsemble`
- `bulletin`

### `weatherstation.js`

- `WeatherStation`
- `createWeatherSystem`

### `world.js`

- `World`

<!-- API:GENERATED END -->
