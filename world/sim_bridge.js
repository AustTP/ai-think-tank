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
let serverStateRefreshing = false;

// Positions alone cannot refresh hires, task results, or the Board. Adopt the
// canonical state in place so open views observe the same live objects.
async function refreshServerState() {
  if (serverStateRefreshing || !SIM_STATUS || SIM_STATUS.owner !== 'server') return;
  serverStateRefreshing = true;
  try {
    const res = await apiFetch('/api/state');
    if (!res.ok) return;
    const saved = await res.json();
    if (!saved || !saved.sim || saved.sim.owner !== 'server') return;
    if (typeof AGENT_ROSTER !== 'undefined' && Array.isArray(saved.agentRoster)) {
      AGENT_ROSTER.splice(0, AGENT_ROSTER.length, ...saved.agentRoster);
    }
    if (typeof AGENTS !== 'undefined' && saved.agents) {
      for (const id of Object.keys(AGENTS)) {
        if (!saved.agents[id]) delete AGENTS[id];
      }
      for (const [id, agent] of Object.entries(saved.agents)) {
        const current = AGENTS[id];
        if (current) {
          const { x, y } = current;
          Object.assign(current, agent);
          current.x = x;
          current.y = y;
        } else AGENTS[id] = agent;
      }
    }
    if (typeof TASKS !== 'undefined') {
      for (const id of Object.keys(TASKS)) delete TASKS[id];
      Object.assign(TASKS, saved.tasks || {});
    }
    if (typeof WORK_QUEUE !== 'undefined') WORK_QUEUE.splice(0, WORK_QUEUE.length, ...(saved.workQueue || []));
    if (typeof RESEARCH_TOPICS !== 'undefined') RESEARCH_TOPICS.splice(0, RESEARCH_TOPICS.length, ...(saved.researchTopics || []));
    if (typeof REPORTS !== 'undefined') REPORTS.splice(0, REPORTS.length, ...(saved.reports || []));
    if (typeof nextReportId !== 'undefined' && saved.nextReportId) nextReportId = saved.nextReportId;
    if (typeof AGENT_KEYS !== 'undefined' && saved.agentKeys) AGENT_KEYS = saved.agentKeys;
    if (typeof renderSpecialistRoles === 'function') renderSpecialistRoles();
    if (!document.getElementById('rosterListModal')?.classList.contains('hidden') && typeof renderRosterList === 'function') renderRosterList();
  } finally {
    serverStateRefreshing = false;
  }
}

async function refreshSimStatus() {
  try {
    const res = await apiFetch('/api/sim/status');
    if (!res.ok) return;
    SIM_STATUS = await res.json();
    window.dispatchEvent && window.dispatchEvent(new CustomEvent('sim-status', { detail: SIM_STATUS }));
    await refreshServerState();
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
    if (typeof sp.task !== 'undefined') a.task = sp.task;
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
