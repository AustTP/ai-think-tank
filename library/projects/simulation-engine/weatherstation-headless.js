// Headless runner: execute a model for N steps and export JSON snapshots.
// No DOM, no canvas -- usable from node --test or a browser console.

const VERSION = '1.0.0';

/**
 * Serialize a simulation state into a JSON-safe snapshot.
 * Arrays are copied; plain objects are shallow-copied; primitives pass through.
 */
export function toJSON(state) {
  if (Array.isArray(state)) return state.map(toJSON);
  if (state && typeof state === 'object') {
    const out = {};
    for (const key of Object.keys(state)) {
      const value = state[key];
      if (typeof value !== 'function') out[key] = toJSON(value);
    }
    return out;
  }
  return state;
}

/**
 * Run a model headlessly.
 *
 * model: { init(config) -> state, step(state) -> state, [serialize(state) -> snapshot] }
 * options: { steps, config }
 *
 * Returns { version, steps, frames: [snapshot, ...] } where frames[0] is the
 * initial state before any step. Snapshots are plain JSON-safe objects.
 */
export function runHeadless(model, options = {}) {
  if (!model || typeof model.step !== 'function') {
    throw new TypeError('runHeadless: model.step(state) is required');
  }
  const steps = options.steps === undefined ? 0 : Math.floor(options.steps);
  if (!Number.isFinite(steps) || steps < 0) {
    throw new RangeError('runHeadless: options.steps must be a non-negative integer');
  }
  const serialize = model.serialize || toJSON;
  let state = model.init
    ? model.init(options.config ?? {})
    : (options.config ?? {});
  const frames = [serialize(state)];
  for (let i = 0; i < steps; i++) {
    state = model.step(state);
    if (state === undefined) {
      throw new Error('runHeadless: model.step returned undefined');
    }
    frames.push(serialize(state));
  }
  return { version: VERSION, steps, frames };
}

/** Convenience: run headlessly and return the final frame only. */
export function runHeadlessFinal(model, options = {}) {
  const result = runHeadless(model, options);
  return result.frames[result.frames.length - 1];
}

export default { runHeadless, runHeadlessFinal, toJSON, VERSION };
