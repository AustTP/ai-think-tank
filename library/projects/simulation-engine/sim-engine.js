// sim-engine.js — reusable client-side agent-based simulation engine.
// ES module, no dependencies. Provides Engine (step loop) and Agent.
export class Agent {
  constructor({ x = 0, y = 0, state = "S", meta = {} } = {}) {
    this.x = x;
    this.y = y;
    this.state = state;
    this.meta = meta;
    this.age = 0; // steps spent in current state
  }
}

export class Engine {
  constructor({ agents = [], rules = [], seed = 1 } = {}) {
    this.agents = agents.slice();
    this.rules = rules; // each rule: { apply(agent, engine, rng) }
    this.stepCount = 0;
    this.rng = mulberry32(seed);
    this.listeners = [];
  }

  onStep(fn) {
    this.listeners.push(fn);
  }

  step() {
    this.stepCount += 1;
    for (const rule of this.rules) {
      for (const agent of this.agents) {
        rule.apply(agent, this, this.rng);
      }
    }
    for (const fn of this.listeners) {
      fn(this);
    }
  }

  countStates() {
    const counts = {};
    for (const a of this.agents) {
      counts[a.state] = (counts[a.state] || 0) + 1;
    }
    return counts;
  }

  run(steps) {
    for (let i = 0; i < steps; i += 1) {
      this.step();
    }
  }
}

// Deterministic PRNG so runs are reproducible.
export function mulberry32(seed) {
  let a = seed >>> 0;
  return function next() {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// World — fixed-timestep simulation host with a systems registry.
// Systems are named units with an update(world, dt, rng) method, executed in
// registration order. Rendering frames feed variable deltaMs into tick(),
// which accumulates time and runs whole fixed-dt steps (max maxSubSteps per
// frame) so simulation determinism never depends on frame rate.
export class World {
  constructor({ dt = 1 / 60, maxSubSteps = 5, seed = 1 } = {}) {
    this.dt = dt;
    this.maxSubSteps = maxSubSteps;
    this.systems = new Map();
    this.order = [];
    this.rng = mulberry32(seed);
    this.time = 0;
    this.accumulator = 0;
    this.running = false;
    this._raf = null;
    this._last = 0;
  }

  registerSystem(name, system) {
    if (!system || typeof system.update !== 'function') {
      throw new TypeError('system must expose update(world, dt, rng)');
    }
    if (!this.systems.has(name)) {
      this.order.push(name);
    }
    this.systems.set(name, system);
    return this;
  }

  unregisterSystem(name) {
    this.systems.delete(name);
    this.order = this.order.filter((n) => n !== name);
  }

  getSystem(name) {
    return this.systems.get(name) || null;
  }

  // One deterministic fixed-dt step across all systems.
  step() {
    for (const name of this.order) {
      this.systems.get(name).update(this, this.dt, this.rng);
    }
    this.time += this.dt;
  }

  // Feed a real (variable) frame delta in milliseconds; returns steps run.
  tick(deltaMs) {
    const maxSeconds = this.maxSubSteps * this.dt;
    this.accumulator += Math.min(deltaMs / 1000, maxSeconds);
    let steps = 0;
    while (this.accumulator >= this.dt && steps < this.maxSubSteps) {
      this.step();
      this.accumulator -= this.dt;
      steps += 1;
    }
    return steps;
  }

  // Browser rAF loop (guarded so Node tests can import without a DOM).
  start() {
    if (this.running) return;
    this.running = true;
    this._last = performance.now();
    const loop = (now) => {
      if (!this.running) return;
      this.tick(now - this._last);
      this._last = now;
      this._raf = requestAnimationFrame(loop);
    };
    this._raf = requestAnimationFrame(loop);
  }

  stop() {
    this.running = false;
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = null;
  }
}
