// Fixed-timestep World with a systems registry.
// The World owns the clock: systems are called with a constant dt,
// and variable frame times are accumulated so simulation stays
// deterministic regardless of render rate.

export class World {
  constructor({ dt = 1 / 60, maxSubSteps = 5 } = {}) {
    if (!Number.isFinite(dt) || dt <= 0) {
      throw new TypeError('dt must be a positive finite number');
    }
    if (!Number.isInteger(maxSubSteps) || maxSubSteps < 1) {
      throw new TypeError('maxSubSteps must be a positive integer');
    }
    this.dt = dt;
    this.maxSubSteps = maxSubSteps;
    this.systems = [];
    this.time = 0;       // simulated seconds elapsed
    this.ticks = 0;      // number of fixed steps executed
    this.running = false;
    this._accumulator = 0;
    this._rafId = null;
    this._lastFrameTime = null;
    this._boundFrame = this._frame.bind(this);
  }

  // --- systems registry -------------------------------------------------

  addSystem(system) {
    if (!system || typeof system.update !== 'function') {
      throw new TypeError('system must have an update(dt, world) method');
    }
    if (this.systems.includes(system)) {
      return this;
    }
    this.systems.push(system);
    if (typeof system.init === 'function') {
      system.init(this);
    }
    return this;
  }

  removeSystem(system) {
    const i = this.systems.indexOf(system);
    if (i !== -1) {
      this.systems.splice(i, 1);
      if (typeof system.deinit === 'function') {
        system.deinit(this);
      }
    }
    return this;
  }

  hasSystem(system) {
    return this.systems.includes(system);
  }

  // --- simulation ---------------------------------------------------------

  // Advance simulation by exactly one fixed step. Public so tests and
  // headless runners can drive the world deterministically.
  step() {
    for (const system of this.systems) {
      system.update(this.dt, this);
    }
    this.time += this.dt;
    this.ticks += 1;
  }

  // Feed a real elapsed-time delta (seconds); runs 0..maxSubSteps fixed
  // steps so the accumulator never spirals on slow frames.
  advance(delta) {
    if (!Number.isFinite(delta) || delta < 0) {
      throw new TypeError('delta must be a non-negative finite number');
    }
    this._accumulator += delta;
    let steps = 0;
    while (this._accumulator >= this.dt && steps < this.maxSubSteps) {
      this.step();
      this._accumulator -= this.dt;
      steps += 1;
    }
    if (this._accumulator >= this.dt) {
      // Drop the backlog rather than banking it; keeps the sim live.
      this._accumulator = 0;
    }
    return steps;
  }

  // --- real-time loop -----------------------------------------------------

  start() {
    if (this.running) return this;
    this.running = true;
    this._lastFrameTime = null;
    if (typeof requestAnimationFrame === 'function') {
      this._rafId = requestAnimationFrame(this._boundFrame);
    }
    return this;
  }

  stop() {
    this.running = false;
    if (this._rafId !== null && typeof cancelAnimationFrame === 'function') {
      cancelAnimationFrame(this._rafId);
    }
    this._rafId = null;
    return this;
  }

  _frame(nowMs) {
    if (!this.running) return;
    if (this._lastFrameTime === null) {
      this._lastFrameTime = nowMs;
    }
    const delta = Math.max(0, (nowMs - this._lastFrameTime) / 1000);
    this._lastFrameTime = nowMs;
    this.advance(delta);
    if (typeof requestAnimationFrame === 'function') {
      this._rafId = requestAnimationFrame(this._boundFrame);
    } else {
      this.running = false;
    }
  }
}

export default World;
