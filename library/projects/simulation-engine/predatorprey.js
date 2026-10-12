// Predator-prey ecosystem: grass / prey / predators on a wrapping grid.
// Deterministic for a fixed seed; pure ES module, no DOM.

export const EMPTY = 0;
export const GRASS = 1;
export const PREY = 2;
export const PREDATOR = 3;

function mulberry32(seed) {
  let a = seed >>> 0;
  return function next() {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export class PredatorPreyEcosystem {
  constructor(options = {}) {
    this.w = options.w ?? options.width ?? 20;
    this.h = options.h ?? options.height ?? 20;
    this.grassRegrow = options.grassRegrow ?? 0.04;
    this.predatorStarve = options.predatorStarve ?? options.predatorMaxHunger ?? 8;
    this.predatorReproduce = options.predatorReproduce ?? 0.15;
    this.rng = mulberry32(options.seed ?? 1);
    this.stepCount = 0;
    this.grid = new Array(this.w * this.h).fill(EMPTY);
    this.energy = new Array(this.w * this.h).fill(0);

    const initialPrey = options.initialPrey ?? options.prey ?? 40;
    const initialPredators = options.initialPredators ?? options.predators ?? 8;
    const initialGrass = options.initialGrass ?? Math.floor(this.w * this.h * 0.35);

    this._scatter(GRASS, initialGrass);
    this._scatter(PREY, initialPrey);
    this._scatter(PREDATOR, initialPredators);
    for (let i = 0; i < this.grid.length; i++) {
      if (this.grid[i] === PREDATOR) this.energy[i] = this.predatorStarve;
    }
  }

  _idx(x, y) {
    return ((y + this.h) % this.h) * this.w + ((x + this.w) % this.w);
  }

  _coords(i) {
    return { x: i % this.w, y: Math.floor(i / this.w) };
  }

  _neighbors(i) {
    const { x, y } = this._coords(i);
    return [
      this._idx(x - 1, y), this._idx(x + 1, y),
      this._idx(x, y - 1), this._idx(x, y + 1),
    ];
  }

  _scatter(kind, count) {
    const n = Math.min(count, this.grid.length);
    let placed = 0;
    let guard = 0;
    while (placed < n && guard < this.grid.length * 4) {
      guard++;
      const i = Math.floor(this.rng() * this.grid.length);
      if (this.grid[i] === EMPTY) {
        this.grid[i] = kind;
        placed++;
      }
    }
  }

  counts() {
    let grass = 0, prey = 0, predator = 0;
    for (let i = 0; i < this.grid.length; i++) {
      if (this.grid[i] === GRASS) grass++;
      else if (this.grid[i] === PREY) prey++;
      else if (this.grid[i] === PREDATOR) predator++;
    }
    return { step: this.stepCount, grass, prey, predator };
  }

  stats() {
    return this.counts();
  }

  inBounds(x, y) {
    return x >= 0 && x < this.w && y >= 0 && y < this.h;
  }

  step() {
    const next = this.grid.slice();
    const nextEnergy = this.energy.slice();
    const moved = new Array(this.grid.length).fill(0);

    for (let idx = 0; idx < this.grid.length; idx++) {
      const kind = this.grid[idx];
      if (kind === EMPTY || kind === GRASS || moved[idx]) continue;
      const { x, y } = this._coords(idx);
      const dirs = [[0, 1], [0, -1], [1, 0], [-1, 0]];
      const di = Math.floor(this.rng() * 4);
      const target = this._idx(x + dirs[di][0], y + dirs[di][1]);
      if (moved[target]) continue;

      if (kind === PREY) {
        if (next[target] === EMPTY) {
          if (this.rng() < 0.2) next[idx] = PREY;
          next[target] = PREY;
        } else if (next[target] === GRASS && this.rng() < 0.5) {
          next[target] = PREY;
        }
      } else if (kind === PREDATOR) {
        let hunted = false;
        const nbrs = this._neighbors(idx);
        let preySpot = -1;
        for (const n of nbrs) {
          if (next[n] === PREY) { preySpot = n; break; }
        }
        if (preySpot >= 0) {
          next[preySpot] = PREDATOR;
          next[idx] = EMPTY;
          nextEnergy[preySpot] = this.predatorStarve;
          hunted = true;
        } else if (next[target] === EMPTY) {
          next[target] = PREDATOR;
          next[idx] = EMPTY;
          nextEnergy[target] = nextEnergy[idx] - 1;
        } else {
          nextEnergy[idx] -= 1;
        }
        if (next[idx] === PREDATOR && nextEnergy[idx] <= 0) next[idx] = EMPTY;
        if (hunted && this.rng() < this.predatorReproduce) {
          for (const n of nbrs) {
            if (next[n] === EMPTY) {
              next[n] = PREDATOR;
              nextEnergy[n] = this.predatorStarve;
              break;
            }
          }
        }
      }
      moved[target] = 1;
    }

    for (let i = 0; i < next.length; i++) {
      if (next[i] === EMPTY && this.rng() < this.grassRegrow) next[i] = GRASS;
    }

    this.grid = next;
    this.energy = nextEnergy;
    this.stepCount++;
    return this.stats();
  }
}

export function runSimulation(options = {}, steps = 25) {
  const sim = new PredatorPreyEcosystem(options);
  const history = [sim.stats()];
  for (let i = 0; i < steps; i++) history.push(sim.step());
  return history;
}

export function createEcosystem(options = {}) {
  const sim = new PredatorPreyEcosystem(options);
  sim.history = [sim.stats()];
  const origStep = sim.step.bind(sim);
  sim.step = function step() {
    const s = origStep();
    sim.history.push(s);
    return s;
  };
  return sim;
}
