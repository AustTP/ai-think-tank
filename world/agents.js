// Stub agent roster for World -- placeholder characters that exist so the
// Town Hall meeting mechanic (see meetings.js) is real, testable code today,
// rather than a design doc waiting on Phase 2/3's actual agent behavior.
// They stand still (no wander AI -- that's real agent behavior, out of
// scope here) and reuse the player's own sprite set, tinted per agent via a
// nameplate so they're distinguishable on the map and in the call UI.

// The single live roster, hydrated entirely from the DATABASE (think_tank.db's
// kv_state blob) via /api/state. There are no
// static agent names in any JS file any more: the default roster is seeded
// server-side in serve.py's _seed_default_roster(), and this global starts
// empty and is filled from /api/state on every load. Nothing here carries a
// name, id, role, isDirector, or director field on purpose -- agents.js must
// never become a second source of truth that can drift from the DB.
let AGENT_ROSTER = [];

// An authority figure who can carry out an action that used to be admin-only
// -- hiring, big-task delegation. Under the single-admin model the admin
// (isAdmin, Theo) handles these and passes day-to-day approval/denial work
// down to the directors; the senior-most director (isDirector, not admin, no
// own `director` -- Nora) stands in for the admin on approval work. So "who
// does this admin-type action" is: the admin if available, else the
// senior-most director. Returns the roster def, or null if neither exists.
function availableAuthority(def) {
  if (def && def.isAdmin) return def;
  for (const d of AGENT_ROSTER) {
    if (d.isDirector && !d.isAdmin && !d.director) return d;
  }
  return null;
}

const AGENT_W = 20, AGENT_H = 16;
// Minimum world-space gap between two characters landing near each other --
// keeps respawned/placed characters from visually stacking.
const PLACEMENT_MIN_DIST = 60;

let AGENTS = {}; // id -> { id, name, color, x, y, dir, visible, busy, meetingId }

// Per-agent generated sprite sets (the "Per-agent sprites" product): id ->
// { south, north, east, west }, each an Image loaded from
// /api/avatar/<id>/<orientation>.png. Populated best-effort by index.html at
// boot; any orientation missing (PixelLab unavailable / not yet generated)
// falls back to the shared default player sprite via drawAgentAt.
let agentSprites = {};

// "Unblocked" alone isn't enough -- a cell can be walkable and still sit in
// a pocket with no path out (a grass patch fully boxed in by a fence or
// building edge, say), which would strand whoever spawns there. Flood-fill
// once from the player's own spawn point (guaranteed walkable and, by
// construction, on the reachable network -- the player starts there every
// load) to find every cell actually connected to it, cached after the
// first call since the grid doesn't change at runtime.
//
// Real bug in the first version of this fix, caught from an actual
// screenshot: it flood-filled the raw per-cell
// boolean, which only asks "is this one 8px cell blocked," not "does an
// agent's real 20x16 footprint fit here." A single unblocked cell right
// next to a building can still be too narrow a sliver for the actual box
// to occupy without clipping the wall -- exactly what stranded Ada next
// to Town Hall. Fixed by testing full-box placement (via `blockedAt()`,
// the same check real movement uses) at each cell instead of the raw
// boolean, both for the flood-fill's own traversal and its start check.
let REACHABLE_MASK = null;

function cellFitsAgent(gx, gy, cell) {
  const worldX = (gx * cell + cell / 2) * SCALE - AGENT_W / 2;
  const worldY = (gy * cell + cell / 2) * SCALE - AGENT_H / 2;
  return !blockedAt({ x: worldX, y: worldY, w: AGENT_W, h: AGENT_H });
}

function computeReachableMask() {
  const { cols, rows, cell } = COLLISION_GRID;
  const mask = Array.from({ length: rows }, () => new Array(cols).fill(false));
  const startGx = Math.floor((SPAWN.x / SCALE) / cell);
  const startGy = Math.floor((SPAWN.y / SCALE) / cell);
  if (!cellFitsAgent(startGx, startGy, cell)) return mask; // spawn itself doesn't fit the agent box -- shouldn't happen; bail safely to an all-false mask
  const stack = [[startGx, startGy]];
  mask[startGy][startGx] = true;
  while (stack.length) {
    const [gx, gy] = stack.pop();
    for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const nx = gx + dx, ny = gy + dy;
      if (nx < 0 || ny < 0 || nx >= cols || ny >= rows) continue;
      if (mask[ny][nx] || !cellFitsAgent(nx, ny, cell)) continue;
      mask[ny][nx] = true;
      stack.push([nx, ny]);
    }
  }
  return mask;
}

function isReachable(worldX, worldY) {
  if (!REACHABLE_MASK) REACHABLE_MASK = computeReachableMask();
  const { cell } = COLLISION_GRID;
  const gx = Math.floor((worldX / SCALE) / cell);
  const gy = Math.floor((worldY / SCALE) / cell);
  return !!(REACHABLE_MASK[gy] && REACHABLE_MASK[gy][gx]);
}

// Finds an unblocked, reachable point on the outdoor map that isn't too
// close to any point in `avoidPoints`. Used both for initial roster
// placement and for picking where someone reappears after a meeting ends
// -- same rule either way: never stack on top of another character, never
// strand them in a disconnected pocket, and (callers pass their own old
// spot in `avoidPoints` too) never land back exactly where you started.
function pickFreeSpot(avoidPoints = []) {
  for (let i = 0; i < 300; i++) {
    const x = Math.random() * (GROUND_W - AGENT_W);
    const y = Math.random() * (GROUND_H - AGENT_H);
    if (blockedAt({ x, y, w: AGENT_W, h: AGENT_H })) continue;
    // Bug (hiring-to-shutdown audit): this
    // originally checked reachability at the agent's CENTER
    // (x+AGENT_W/2, y+AGENT_H/2), which disagreed with findPath's own
    // start-validity check at the time (which used the box's raw
    // top-left with no center adjustment) -- stranding whoever landed
    // somewhere the two disagreed about. First fix (same day) made this
    // match findPath's raw convention instead -- WRONG DIRECTION: it
    // turned out findPath's own raw-top-left convention was the actual
    // bug (cellWorldPos centers every real waypoint on a cell, and
    // AGENT_W (20) doesn't evenly divide cell*SCALE (16), so reversing a
    // waypoint's OWN position with a plain floor division doesn't
    // recover the same cell -- confirmed: an agent resting at her
    // own real task.entryX/entryY, itself a real waypoint, could not
    // pathfind anywhere from her own position). findPath's start-cell
    // computation now correctly centers too, so THIS is the right
    // convention after all -- reverted back to it.
    if (!isReachable(x + AGENT_W / 2, y + AGENT_H / 2)) continue;
    if (avoidPoints.some(p => Math.hypot(p.x - x, p.y - y) < PLACEMENT_MIN_DIST)) continue;
    return { x, y };
  }
  return { x: SPAWN.x, y: SPAWN.y }; // pathological fallback, shouldn't hit on a real map
}

// Bug: an agent that goes idle (task finished/cancelled,
// off-duty, a handoff that never got picked back up) simply stays wherever
// she physically was, forever -- nothing ever re-examines a resting
// agent's position. When that resting spot happens to be a door tile (a
// single-file chokepoint, per door_triggers.json/ROOM_DOOR_TRIGGERS),
// every OTHER agent whose only route runs through that same door gets
// permanently blocked by her, which then loops through tickAgentMovement's
// stuck-timer/replan/respawn/cancel dance forever every time anyone else
// is assigned that door -- visible to you as a standing agent apparently
// stuck in an infinite loop, even though the immediate cause is a
// completely different, stationary agent. A door can only ever be single-
// file, so unlike open floor space, waiting for a resting agent to
// "eventually move out of the way" isn't viable -- she won't, until
// reassigned. Doors are exempted from agent-vs-agent blocking entirely,
// the same way each mover already ignores her own start cell.
function isOnADoorTile(x, y) {
  // `typeof` guard, not a bare reference -- ROOM_DOOR_TRIGGERS is declared
  // with `let` in rooms.js (loaded before this file in index.html), and a
  // second `let ROOM_DOOR_TRIGGERS` here would be a real SyntaxError
  // (duplicate lexical declaration) the moment both files share a global
  // scope, whether in the browser or in a test harness that doesn't happen
  // to load rooms.js at all.
  if (typeof ROOM_DOOR_TRIGGERS === 'undefined' || !ROOM_DOOR_TRIGGERS) return false;
  const point = { x, y, w: 1, h: 1 };
  for (const building in ROOM_DOOR_TRIGGERS) {
    if (overlaps(point, ROOM_DOOR_TRIGGERS[building])) return true;
  }
  return false;
}

// Visible agents are solid, not decoration -- the player, and now another
// walking agent (tasks.js), can't pass through one standing on the map.
// `excludeId` skips self-collision while an agent is moving toward its own
// task. Only checked against outdoor movement resolution, not against
// pickFreeSpot()'s own reachability math -- that's about whether the
// static map connects, not who else happens to be standing somewhere
// right now.
//
// `ignoreIds` (optional Set): bug -- two agents can
// legitimately end up standing on the EXACT same spot in real gameplay
// (finishTask() resets both to the same task.entryX/entryY door-front
// point if they worked the same room), and findPath's own planning
// already forgives this exact case (see findPath's coLocatedIds) -- but
// the REAL per-frame movement check here never did. A box moving even
// one pixel away from a neighbor occupying the identical start position
// still fully overlaps that neighbor's identical box, so EVERY direction
// (tryBoth/tryX/tryY) reported blocked forever, with neither agent ever
// able to take the first step needed to separate. Confirmed:
// stuckTimer cycled 0->1.2->0 indefinitely, replanning to the same
// nearby cell every time, position never changing by even 0.01px.
function agentBlockedAt(box, excludeId = null, ignoreIds = null) {
  for (const id in AGENTS) {
    if (id === excludeId) continue;
    if (ignoreIds && ignoreIds.has(id)) continue;
    const a = AGENTS[id];
    if (!a.visible) continue;
    if (isOnADoorTile(a.x, a.y)) continue;
    if (overlaps(box, { x: a.x, y: a.y, w: AGENT_W, h: AGENT_H })) return true;
  }
  return false;
}

// Whole-state persistence (Phase 2 -- see serve.py). Deliberately whole-
// snapshot, not incremental: the state here (a handful of agents, a
// handful of reports) is tiny, so POSTing the full thing on every save is
// simpler than diffing and costs nothing real.
//
// Meetings are NOT persisted -- they're treated as session-scoped, not
// think tank-scoped. A meeting mid-call when the page closes has no way to
// resume its chat UI meaningfully, so on load any agent left `busy`/
// invisible from an abandoned call is force-reset instead (see below)
// rather than trying to reconstruct a MEETINGS entry that no longer means
// anything without the client that was in it.
async function loadPersistedState() {
  try {
    const res = await apiFetch('/api/state');
    const data = await res.json(); // null if nothing saved yet
    if (data && data.agentKeys) AGENT_KEYS = data.agentKeys; // per-agent attribution keys (world.js), riding along with state
    return data;
  } catch (e) {
    return null; // backend not reachable (e.g. old plain-file serve) -- fall back to a fresh start
  }
}

async function saveState() {
  try {
    // Under server ownership (the Phase-2 flip), the browser is a renderer:
    // server-owned spatial fields must not clobber the server's authoritative
    // positions. Strip them from the autosave payload so the server's merge
    // carries them forward; the agent still syncs its non-spatial state
    // (task/busy/inRoom/offDuty) so client-side edits land. Before the flip the
    // client is the mover and sends AGENTS wholesale, unchanged.
    const serverOwns = typeof SIM_STATUS !== 'undefined' && SIM_STATUS && SIM_STATUS.owner === 'server';
    let agentsPayload = AGENTS;
    if (serverOwns) {
      const SERVER_OWNED = ['x', 'y', 'dir', 'path', 'pathIndex', 'pathTarget',
                            'stuckTimer', 'replanCount', 'respawnedForTask'];
      agentsPayload = {};
      for (const id in AGENTS) {
        const a = AGENTS[id];
        const copy = Object.assign({}, a);
        for (const f of SERVER_OWNED) delete copy[f];
        agentsPayload[id] = copy;
      }
    }
    await apiFetch('/api/state', {
      method: 'POST',
      body: JSON.stringify({
        agentRoster: AGENT_ROSTER,
        agents: agentsPayload,
        reports: REPORTS,
        nextReportId,
        // Gap: everything else here survives a reload;
        // WORK_QUEUE (tasks.js) didn't, so a real request queued via
        // assignBigTask and not yet fully drained would just silently
        // vanish if the page reloaded mid-flight -- the exact opposite of
        // "resolve the open items," since it was flagged as one.
        workQueue: WORK_QUEUE,
        // Same reasoning as workQueue -- a standing research topic's
        // cadence/lastRunAt/seenUrls (tasks.js) is exactly the kind of
        // state that must survive a reload, or a scheduled topic would
        // silently re-run from scratch (re-collecting pages it already
        // has) the moment the page reloaded.
        researchTopics: RESEARCH_TOPICS,
        // lastSkillReviewAt is intentionally NOT saved here. It is a
        // server-owned cadence stamp now (serve.py/sim.py write it on
        // every qualifying skill-review sweep). A client-kept copy would
        // shadow the server's corrected value -- and a stale TEST sentinel
        // (1e18) persisted by an old client would resurrect itself on every
        // autosave, re-disabling the standing skill-review sweep. The server
        // re-owns the cadence on the next cycle, so a reload firing skill-
        // review early is the correct, self-healing behavior.
      }),
    });
  } catch (e) {
    // best-effort -- a save failure shouldn't break gameplay
  }
}

async function initAgents() {
  const saved = await loadPersistedState();
  if (saved && saved.agents && Object.keys(saved.agents).length > 0) {
    // Restore hired-agent roster defs first -- every UI that lists agents
    // (checklists, rosters, mailboxes) iterates AGENT_ROSTER directly, so
    // this is what makes a Command-Center hire from a previous session
    // show up everywhere again, not just in AGENTS.
    if (saved.agentRoster) {
      AGENT_ROSTER.length = 0;
      for (const def of saved.agentRoster) AGENT_ROSTER.push(def);
    }
    AGENTS = saved.agents;
    // Recovery: a call, task, or hire in progress when the page last
    // closed has no corresponding MEETINGS/TASKS entry now (neither is
    // persisted -- there's no meaningful way to resume a chat UI or a
    // walk-in-progress after a refresh) -- anyone left marked busy/
    // invisible/mid-walk from that would be stuck forever with no way to
    // finish something that no longer exists. Treat a refresh as
    // "anything in progress is abandoned."
    for (const id in AGENTS) {
      AGENTS[id].busy = false;
      // A reload should restore whatever was
      // actually saved, not force everyone back on duty -- offDuty is a
      // deliberate rest state (sendAgentOffDuty), and someone genuinely
      // resting stays invisible and off duty across a reload exactly like
      // every other persisted field, with nothing abandoned to recover
      // from. Previously this line ran unconditionally and a separate
      // pass below force-reset offDuty to false and walked everyone who'd
      // been resting out of the outskirts door so they wouldn't be stuck
      // stacked there visible -- both removed along with this guard,
      // since neither is needed once resting is left alone on reload.
      if (!AGENTS[id].offDuty) AGENTS[id].visible = true;
      AGENTS[id].meetingId = null;
      AGENTS[id].inRoom = null;
      AGENTS[id].task = null;
      AGENTS[id].handoff = null; // an in-progress handoff (handoffs.js) is just as abandoned as an in-progress task
      // Bug: an agent mid pair-programming session
      // (pairWith/pairTaskId, tasks.js) when the page reloaded stayed
      // stuck that way FOREVER -- the async runPairProgrammingSession()
      // that would eventually have released her was just abandoned
      // in-memory along with the old page, and nothing else ever clears
      // these two fields. Same "abandoned is not the same as resolved"
      // reasoning as .handoff right above, just missed when pairing was
      // added.
      AGENTS[id].pairWith = null;
      AGENTS[id].pairTaskId = null;
      AGENTS[id].path = null;
      AGENTS[id].pathIndex = 0;
      // Stale-field guard: headingOffDuty is a retired transient from the old
      // walk-to-the-trailhead off-duty mechanic (removed). Clearing it keeps an
      // agent saved under the old regime from carrying a stale value forward.
      AGENTS[id].headingOffDuty = null;
      // Restored mail may be pre-migration plain strings (see
      // normalizeMailEntry) -- normalize on load so every consumer can
      // assume {text, read, ts} without special-casing old saves.
      AGENTS[id].mailbox = (AGENTS[id].mailbox || []).map(normalizeMailEntry);
      // Migration default for anyone hired before hiredAt existed
      // (morale.js's dropped-work decay) -- there's no real record of
      // when they actually joined, so this starts their decay clock now
      // rather than leaving it undefined (which moraleFor() would
      // otherwise read as "just hired," never decaying a seed-era
      // droppedCount at all).
      if (!AGENTS[id].hiredAt) AGENTS[id].hiredAt = Date.now();
    }
    if (saved.reports) { REPORTS.length = 0; for (const r of saved.reports) REPORTS.push(r); }
    if (saved.nextReportId) nextReportId = saved.nextReportId;
    // Restored in place (not reassigned) so every file that already
    // holds a reference to the real WORK_QUEUE array (tasks.js's own
    // runTaskCycleBody, queueWork, thinkTankHasWork) sees the restored
    // contents rather than a new array only agents.js knows about.
    if (saved.workQueue) { WORK_QUEUE.length = 0; for (const item of saved.workQueue) WORK_QUEUE.push(item); }
    if (saved.researchTopics) { RESEARCH_TOPICS.length = 0; for (const t of saved.researchTopics) RESEARCH_TOPICS.push(t); }
    // The server owns the seed now (serve.py's _seed_default_roster), and a
    // genuinely new think tank arrives with every agent at the same origin
    // (x=0, y=0). That stacked pile gets scattered the same way a hire-from-
    // scratch used to: pick a free walkable spot per agent in the main
    // think tank (outskirts rooms are gone). A warm restore (agents already
    // placed in the DB from real priors) has distinct positions and is
    // left alone.
    const distinctCoords = new Set(Object.values(AGENTS).map(a => `${a.x},${a.y}`));
    if (distinctCoords.size === 1 && AGENT_ROSTER.length > 1) {
      const occupied = [{ x: state.player.x, y: state.player.y }];
      for (const def of AGENT_ROSTER) {
        const spot = pickFreeSpot(occupied);
        spawnAgentAtFreeSpot(def.id, occupied);
        occupied.push(spot);
      }
    }
    return;
  }

  // Nothing saved AND /api/state unreachable -- the server seeds a fresh
  // database itself (serve.py's _seed_default_roster), so the client should
  // never invent identities. If the backend genuinely returned nothing, start
  // with an empty think tank rather than fabricate names; the next /api/state
  // fetch will deliver the server-seeded roster. This branch is a resilience
  // guard (e.g. old plain-file serve, backend briefly down), not a code path
  // a normal first boot normally takes.
}

// Called whenever the player actually messages this agent (group or DM,
// see meetings.js's postMessage()) -- the one live, non-seeded signal
// morale.js uses, versus the static approved/dropped counts above.
function markContacted(agentId, ts) {
  const a = AGENTS[agentId];
  if (a) a.lastContactedAt = ts;
}

// An agent (or the player) should be able to send mail to
// another agent from ANY location, not just walk up to them -- exactly
// the channel that's missing when the recipient is busy and a live
// conversation or handoff (handoffs.js) isn't possible right now.
// Server-validated now: /api/mail/send checks the sender/recipient against
// the roster and bounds the text, then appends the authoritative entry to
// the recipient's mailbox in state (mailbox content feeds model context, so
// the write path must not be an unvalidated client append). On a server
// failure we fall back to the local append so mail still works degraded, but
// the normal path is server-authoritative.
async function sendMail(fromId, toId, text) {
  const to = AGENTS[toId];
  if (!to || !text) return false;
  try {
    const res = await apiFetch('/api/mail/send', {
      method: 'POST',
      body: JSON.stringify({ fromId, toId, text }),
    });
    if (!res.ok) return false;
    const data = await res.json();
    if (data && data.mail) to.mailbox.push(data.mail);
    return true;
  } catch (e) {
    const fromName = fromId === 'player' ? 'You' : (AGENTS[fromId] ? AGENTS[fromId].name : fromId);
    to.mailbox.push({ text: `${fromName}: ${text}`, read: false, ts: Date.now() });
    return true;
  }
}

// Mailbox entries used to be plain strings with no read/unread concept at
// all -- every consumer (the HUD unread count, the Post Office thread view,
// gatherUnifiedContext's "your mailbox" block) just treated the whole array
// as permanently new, so mail sent to an agent (e.g. a closed-loop review
// escalation) never actually registered as "seen" by anyone, human or
// agent. This normalizes any entry -- including
// pre-existing string entries from state saved before this change -- into
// {text, read, ts}. Legacy string entries are treated as already-read
// (they were already visible under the old system) so migrating existing
// saved think_tank.db state doesn't suddenly flood the HUD with old "unread"
// mail nobody actually missed.
function normalizeMailEntry(msg) {
  if (typeof msg === 'string') return { text: msg, read: true, ts: 0 };
  return { text: msg.text, read: !!msg.read, ts: msg.ts || 0 };
}

// Marks every message currently in an agent's inbox as read. Called both
// when the player opens that agent's thread in the Post Office (index.html)
// and when the agent itself pulls its own mailbox into task context
// (gatherUnifiedContext, world.js) -- that context pull IS the agent
// "checking its mail," so it should have the same effect a human opening
// the thread does.
function markMailRead(agentId) {
  const a = AGENTS[agentId];
  if (!a) return;
  a.mailbox = a.mailbox.map(normalizeMailEntry);
  for (const m of a.mailbox) m.read = true;
}

// Speech bubbles: agent-to-agent dialogue (handoff exchanges, pair-programming
// announcements) used to go through the single global showToast() banner --
// not positioned over the speaker, and with no length limit at all, so a long
// model-generated line just overflowed the banner.
// a real bubble drawn over the speaking agent, and the text must be truncated.
const SPEECH_MAX_CHARS = 80;
const SPEECH_BUBBLE_DURATION_MS = 4500;
const SPEECH_BUBBLE_FONT = '11px monospace';
const SPEECH_BUBBLE_LINE_HEIGHT = 14;
const SPEECH_BUBBLE_MAX_WIDTH = 160;

// Truncates to SPEECH_MAX_CHARS (word-boundary where possible, not mid-word)
// and stamps an expiry so the renderer stops drawing it on its own -- callers
// never need to clear it themselves.
function saySpeech(agentId, text) {
  const a = AGENTS[agentId];
  if (!a || !text) return;
  let t = String(text).trim();
  if (t.length > SPEECH_MAX_CHARS) {
    const cut = t.slice(0, SPEECH_MAX_CHARS);
    const lastSpace = cut.lastIndexOf(' ');
    t = (lastSpace > SPEECH_MAX_CHARS * 0.6 ? cut.slice(0, lastSpace) : cut) + '…';
  }
  a.speechText = t;
  a.speechUntil = Date.now() + SPEECH_BUBBLE_DURATION_MS;
}

// Greedy word-wrap of already-truncated text into lines that fit maxWidth at
// the current ctx.font. Small inputs (<=80 chars) only ever wrap to a couple
// of lines, so this cheap per-frame reflow is fine -- no caching needed.
function _wrapSpeechLines(ctx, text, maxWidth) {
  const words = text.split(' ');
  const lines = [];
  let line = '';
  for (const w of words) {
    const test = line ? line + ' ' + w : w;
    if (ctx.measureText(test).width > maxWidth && line) {
      lines.push(line);
      line = w;
    } else {
      line = test;
    }
  }
  if (line) lines.push(line);
  return lines;
}

// Drawn above the nameplate, only while a.speechUntil hasn't expired -- a
// rounded bubble with a small tail pointing down at the speaker, matching the
// reference shape from the JevTown speech-bubble research (rounded rect +
// tail polygon + wrapped text), adapted to this canvas renderer.
function drawSpeechBubble(ctx, toScreen, zoom, a, x, y) {
  if (!a.speechText || !a.speechUntil || a.speechUntil < Date.now()) return;
  ctx.save();
  ctx.font = SPEECH_BUBBLE_FONT;
  const lines = _wrapSpeechLines(ctx, a.speechText, SPEECH_BUBBLE_MAX_WIDTH);
  const textWidth = Math.min(SPEECH_BUBBLE_MAX_WIDTH, Math.max(...lines.map(l => ctx.measureText(l).width)));
  const boxW = (textWidth + 16) * zoom;
  const boxH = (lines.length * SPEECH_BUBBLE_LINE_HEIGHT + 10) * zoom;
  const [cx, topY] = toScreen(x + AGENT_W / 2, y - 22);
  const left = cx - boxW / 2;
  const top = topY - boxH;

  ctx.fillStyle = 'rgba(255, 250, 235, 0.97)';
  ctx.strokeStyle = 'rgba(60, 45, 30, 0.9)';
  ctx.lineWidth = 1;
  const r = 6 * zoom;
  ctx.beginPath();
  ctx.moveTo(left + r, top);
  ctx.arcTo(left + boxW, top, left + boxW, top + boxH, r);
  ctx.arcTo(left + boxW, top + boxH, left, top + boxH, r);
  ctx.arcTo(left, top + boxH, left, top, r);
  ctx.arcTo(left, top, left + boxW, top, r);
  ctx.closePath();
  ctx.fill();
  ctx.stroke();
  // Tail pointing down at the speaker.
  ctx.beginPath();
  ctx.moveTo(cx - 5 * zoom, top + boxH);
  ctx.lineTo(cx, top + boxH + 7 * zoom);
  ctx.lineTo(cx + 5 * zoom, top + boxH);
  ctx.closePath();
  ctx.fillStyle = 'rgba(255, 250, 235, 0.97)';
  ctx.fill();

  ctx.fillStyle = 'rgba(40, 30, 20, 1)';
  ctx.textAlign = 'center';
  lines.forEach((line, i) => {
    ctx.fillText(line, cx, top + 14 * zoom + i * SPEECH_BUBBLE_LINE_HEIGHT * zoom);
  });
  ctx.restore();
}

function drawAgentAt(ctx, toScreen, zoom, playerSprites, a, x, y) {
  const per = (agentSprites && agentSprites[a.id]) ? agentSprites[a.id][a.dir] : null;
  const spr = per || playerSprites[a.dir];
  const [sx, sy] = toScreen(x - (spr.width - AGENT_W) / 2, y - (spr.height - AGENT_H));
  ctx.drawImage(spr, sx, sy, spr.width * zoom, spr.height * zoom);

  const [nx, ny] = toScreen(x + AGENT_W / 2, y - 4);
  ctx.save();
  ctx.font = 'bold 11px monospace';
  ctx.textAlign = 'center';
  ctx.lineWidth = 3;
  ctx.strokeStyle = 'rgba(0,0,0,0.8)';
  ctx.strokeText(a.name, nx, ny);
  ctx.fillStyle = a.color;
  ctx.fillText(a.name, nx, ny);
  ctx.restore();

  drawSpeechBubble(ctx, toScreen, zoom, a, x, y);
}

// Should this agent's sprite be drawn at all? Both visible AND on-duty.
// offDuty is server-authoritative (synced every poll, sim_bridge.js) while
// `visible` is client-owned and only as fresh as the last client flow that
// touched it -- so an agent the server sent off-duty can otherwise linger
// on the map as a ghost (theo was found busy+offDuty at the map origin).
// "When they go offline, they disappear."
function agentIsDrawn(a) {
  return a.visible && !a.offDuty;
}

function renderAgents(ctx, toScreen, zoom, playerSprites) {
  // Each outdoor scene shows only its own village's agents: main-village
  // agents live in the main map's coordinate space, winter-village agents in
  // the winter (flipped) map's space. Default to 'main' for any agent that
  // predates the villageId field.
  const sceneVillage = (typeof state !== 'undefined' && state.scene === 'winter') ? 'winter' : 'main';
  for (const id in AGENTS) {
    const a = AGENTS[id];
    if (!agentIsDrawn(a)) continue;
    if ((a.villageId || 'main') !== sceneVillage) continue;
    drawAgentAt(ctx, toScreen, zoom, playerSprites, a, a.x, a.y);
  }
}

// Agents "in" a specific room (hiring.js, while Control Room is in use) --
// only ever drawn if the player is currently inside that same room, so
// this doubles as the monitoring: walk in and see who's
// actually there.
function renderAgentsInRoom(ctx, toScreen, zoom, playerSprites, building) {
  for (const id in AGENTS) {
    const a = AGENTS[id];
    if (a.inRoom !== building) continue;
    if (!agentIsDrawn(a)) continue;
    drawAgentAt(ctx, toScreen, zoom, playerSprites, a, a.roomX, a.roomY);
  }
}
