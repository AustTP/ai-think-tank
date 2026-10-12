// Generates README.md: hand-written guide + API extracted from real sources.
import { readFileSync, writeFileSync } from 'node:fs';

const SOURCES = [
  'sim-engine.js',
  'world.js',
  'weatherstation.js',
  'weatherstation-sir.js',
  'weatherstation-predatorprey.js',
  'weatherstation-headless.js',
  'boids.js',
  'ca-engine.js',
  'sir.js',
  'predatorprey.js',
  'rng.js',
];

function extractExports(source) {
  const names = [];
  const re = /export\s+(?:default\s+)?(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)/g;
  let m;
  while ((m = re.exec(source)) !== null) names.push(m[1]);
  const brace = /export\s*\{([^}]*)\}/g;
  while ((m = brace.exec(source)) !== null) {
    for (const part of m[1].split(',')) {
      const n = part.split(/\s+as\s+/).pop().trim();
      if (n) names.push(n);
    }
  }
  return names;
}

let api = '';
for (const file of SOURCES) {
  let src;
  try {
    src = readFileSync(file, 'utf8');
  } catch {
    continue;
  }
  const names = extractExports(src);
  if (names.length === 0) continue;
  api += `\n### \`${file}\`\n\n\`\`\`js\nexport { ${names.join(', ')} }\n\`\`\`\n`;
}

const readme = `# Simulation Lab

A reusable, dependency-free client-side simulation engine (vanilla JavaScript,
ES modules) for the think tank — an interactive agent-based simulation lab that
runs entirely in the browser.

## Core concepts

The engine is built around four operations: **create a world**, **register
systems**, **step** the simulation, and take a **snapshot** of its state.

- **World** — holds the entities and the ordered list of systems.
- **System** — a plain function (or object with an \`update\` method) called
  once per step to mutate the world.
- **Step** — advances the simulation one tick by running every registered
  system in registration order.
- **Snapshot** — a plain, serializable copy of the world state, suitable for
  logging, diffing, or feeding to a renderer.

## Quick start

\`\`\`js
import { createWorld } from './world.js';
import { step } from './sim-engine.js';

// 1. Create a world.
const world = createWorld({ width: 64, height: 64 });

// 2. Register one or more systems. A system receives the world each step.
world.registerSystem(function gravity(world) {
  for (const entity of world.entities) {
    entity.vy += 0.1;
    entity.y += entity.vy;
  }
});

// 3. Step the simulation.
for (let i = 0; i < 100; i++) {
  step(world);
}

// 4. Snapshot the state (plain JSON-friendly object).
const snap = world.snapshot();
console.log(JSON.stringify(snap));
\`\`\`

### Registering a system object

\`\`\`js
const system = {
  name: 'sir-epidemic',
  update(world, dt) {
    // mutate world state here
  },
};
world.registerSystem(system);
\`\`\`

### Determinism with the seeded RNG

\`\`\`js
import { createRng } from './rng.js';
const rng = createRng(42); // fixed seed => reproducible runs
world.rng = rng;
\`\`\`

## Included simulations

- **boids.js** — flocking agents (separation / alignment / cohesion).
- **sir.js** — SIR epidemic model (susceptible / infected / recovered).
- **predatorprey.js** — Lotka–Volterra predator–prey agents.
- **ca-engine.js** — cellular-automata grid engine.

## Weather station

The \`weatherstation-*.js\` modules adapt each simulation into a common
"weather station" interface so the harness page (\`index.html\`) can display
any of them with the same controls: a live canvas, sliders for parameters,
and step/play controls. The headless variant
(\`weatherstation-headless.js\`) runs the same simulations with no DOM, for
tests and scripted runs.

## Browser harness

Open \`index.html\` over HTTP (ES modules do not load over \`file://\`):

\`\`\`sh
npx serve .        # or: python3 -m http.server
\`\`\`

## Tests

\`\`\`sh
node --test
\`\`\`

## Public API surface
${api}
`;

writeFileSync('README.md', readme);
console.log('README.md written');
