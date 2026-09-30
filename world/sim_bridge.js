// Client-side bridge to the server-side simulation engine (sim.py).
//
// Phase 1-rest: the server now owns a `sim` section of the authoritative
// state and advances it on a loop that runs whether or not a browser is open.
// The browser is still the engine for movement (Phase 2), so this module does
// NOT yet write agent positions -- it only:
//   1. Polls /api/sim/status so a player (or a "runs closed" probe) can see
//      the server loop is alive: tick increasing with no browser attached.
//   2. Exposes the latest status globally + via an event, so a future HUD can
//      render it and tests can assert convergence without UI intrusions.
// 1000 (was 2000) to match the finer SIM_TICK_S=1.0s server
// heartbeat -- status/positions displayed ~2x sooner on the map.
const SIM_POLL_MS = 1000;

// Latest status from the server: {running, tick, lastTickEpochS, owner, ageS}.
let SIM_STATUS = null;

// Server-authoritative agent positions when the server owns movement
// (sim.owner == 'server'): { owner, tick, agents: { id: {x,y,dir,busy,
// inRoom,offDuty,pathActive,task} } }. Under client ownership this stays
// empty -- the browser is the mover and renders its own live truth.
let SERVER_POSITIONS = null;

async function refreshSimStatus() {
  try {
    const res = await apiFetch('/api/sim/status');
    if (!res.ok) return;
    SIM_STATUS = await res.json();
    window.dispatchEvent && window.dispatchEvent(new CustomEvent('sim-status', { detail: SIM_STATUS }));
  } catch (e) {
    // backend unreachable -- leave last known status in place
  }
}

async function refreshServerPositions() {
  try {
    const res = await apiFetch('/api/sim/agents');
    if (!res.ok) return;
    SERVER_POSITIONS = await res.json();
  } catch (e) {
    // backend unreachable -- keep last known positions
  }
}

// Exponential lerp factor for rendering server positions. The server ticks
// every SIM_TICK_S (1.0s, sim.py) and the poll refreshes faster; a small lerp
// makes agents glide toward the latest authoritative position instead of
// snapping, converging without a fixed interpolation buffer.
// (player call: "I feel there is a slight delay"): 0.25 meant it
// took ~2s of real time (4 polls at the old 500ms rate) to close 68% of any
// gap -- noticeably laggy. Raised to 0.45 alongside the faster 250ms poll
// below; the tradeoff is a slightly less silky glide on a big jump, which is
// the right side of that tradeoff for responsiveness.
const SERVER_LERP = 0.45;

// Adopt the server's authoritative position snapshot into the live AGENTS
// the renderer draws. Only meaningful under server ownership; callers (the
// frame loop) skip this when SIM_STATUS.owner !== 'server'. When the server
// clears an agent's path it flips pathActive off -- the client has nothing to
// smooth toward, so it leaves the agent parked at the last lerped position.
function applyServerPositions(state) {
  if (!SERVER_POSITIONS || !SERVER_POSITIONS.agents || !state) return;
  for (const id in SERVER_POSITIONS.agents) {
    const sp = SERVER_POSITIONS.agents[id];
    const a = state[id];
    if (!a) continue;
    if (typeof sp.x === 'number' && typeof sp.y === 'number') {
      a.x += (sp.x - a.x) * SERVER_LERP;
      a.y += (sp.y - a.y) * SERVER_LERP;
    }
    // Logical state snaps immediately (it isn't interpolated).
    if (sp.dir) a.dir = sp.dir;
    if (typeof sp.busy === 'boolean') a.busy = sp.busy;
    if (typeof sp.inRoom !== 'undefined') a.inRoom = sp.inRoom;
    if (typeof sp.offDuty === 'boolean') a.offDuty = sp.offDuty;
    // Bug: this snapshot never carried
    // `visible` before, so a client's copy stayed frozen at whatever it was
    // on page load -- an agent who went invisible afterward (entering a
    // room, being parked off duty) kept rendering as a "ghost" at her last
    // known outdoor position for the rest of that browser session, on
    // every already-open tab. agentIsDrawn() (agents.js) reads exactly
    // this field, so syncing it here is what actually makes her disappear.
    if (typeof sp.visible === 'boolean') a.visible = sp.visible;
  }
}

async function startSimPoll() {
  await refreshSimStatus();
  await refreshServerPositions();
  setInterval(refreshSimStatus, SIM_POLL_MS);
  // Positions refresh slightly faster than the status poll so a fresh server
  // tick lands in the renderer without waiting the full poll interval.
  // (was 500ms): halved again, paired with the higher SERVER_LERP
  // above, so a real 1.0s server tick shows up on the map much sooner instead
  // of visibly lagging behind.
  setInterval(refreshServerPositions, 250);
}