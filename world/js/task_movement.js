// Room routing, agent movement, task cancellation, and pair programming.
// Split out of tasks.js to keep every script under 1,000 lines; classic
// script scope means these globals are shared exactly as before.

// Rooms a generated subtask can actually target -- the same ones
// TASK_POOL itself uses (real door triggers, real walk-to-room work).
// Town Hall and House are excluded on purpose, same reason assignTask()
// itself won't take them: they're button/dialog rooms, not somewhere an
// agent can walk into on a task.
const DELEGATABLE_ROOMS = ['observatory', 'pressoffice', 'postoffice', 'bank', 'weatherstation', 'library', 'media'];

// The planner-facing description of each room comes from the SERVER now --
// GET /api/rooms returns state.roomDefinitions, the same source the
// server-side intent planner reads, so a director's purpose edit lands in
// both the browser delegate path and the server path. These literals are
// only the offline/BEFORE-fetch fallback so a system prompt is always
// buildable (mirrors how the server backfills its own seed); loadRoomPurposes
// replaces them once the server answers. Keeping this in ONE place (state,
// director-editable) instead of two hardcoded copies (this file + serve.py)
// is what stops the drift that mis-routed real coding work to library.
const _ROOM_PURPOSE_FALLBACK = {
  pressoffice: 'Work Room -- real sandboxed software development; actually writes and runs code for the SPECIFIC task given (taskType "code"), or reviews/QA-tests an already-built deliverable for real, specific problems (taskType "review" or "qa"). Use this for anything meaning "write/build/fix code" OR "review/test what was built."',
  observatory: 'Research Center -- makes a real model call reasoning about the SPECIFIC subtask given and files a genuine, findable finding. Use for "research/investigate/write up findings on X."',
  weatherstation: 'Weather Station -- an open-ended research room with internet access; may investigate or analyze any topic on request. The name is cosmetic and does not limit the topic. Use for "research/investigate/analyze X" on any subject.',
  media: 'Studio -- digests one of the subscribed feeds (media/feeds.md) into a summary. Use only for "summarize/digest an external source."',
  library: 'Library -- reference/reading room. No automated work happens here at all; never assign a subtask here that needs a real deliverable.',
  postoffice: 'Post Office -- no automated work happens here at all; never assign a subtask here that needs a real deliverable.',
  bank: 'Bank -- no automated work happens here at all; never assign a subtask here that needs a real deliverable.',
};
let ROOM_PURPOSES = { ..._ROOM_PURPOSE_FALLBACK };

// Pull the server-authoritative room purposes once at startup (next to
// loadModelTiers), so a director-edit to a room description propagates to
// the browser's legacy delegate path too. Non-fatal: on failure we keep the
// offline fallback above.
async function loadRoomPurposes() {
  try {
    const res = await apiFetch('/api/rooms');
    const data = await res.json();
    const rooms = data.rooms || {};
    for (const r in rooms) {
      if (rooms[r].purpose) ROOM_PURPOSES[r] = rooms[r].purpose;
    }
  } catch (e) {
    // backend unreachable -- keep fallback purposes
  }
}

// Give an admin a large, free-form task and have THEM
// break it into real subtasks and delegate them -- not you hand-picking
// individual TASK_POOL entries yourself. The breakdown itself needs a
// real reasoning model (Jev is a classifier, not a planner -- picking who
// does each subtask below is exactly its job, but generating the subtask
// list in the first place isn't), so this is the one place in the task
// system that calls /api/chat directly instead of only Jev.
async function assignBigTask(goal) {
  // An authority figure who is free right now -- the admin if available,
  // else the senior-most director. Under the single-admin model the admin
  // passes delegation work down to the directors, so a director
  // legitimately carries out a big-task breakdown (see agents.js's
  // availableAuthority).
  const authorityDef = AGENT_ROSTER.find(d => d.isAdmin && AGENTS[d.id] && !AGENTS[d.id].busy)
    || AGENT_ROSTER.find(d => d.isDirector && !d.isAdmin && !d.director && AGENTS[d.id] && !AGENTS[d.id].busy);
  if (!authorityDef) return { error: 'No admin or director is free right now -- try again shortly.' };
  const admin = AGENTS[authorityDef.id];

  // A subtask that can't start until a specific time (e.g. "run
  // the report at 9pm"). WORK_QUEUE items already support this
  // (queueWork's notBefore, respected by runTaskCycleBody/thinkTankHasWork
  // -- a scheduled item makes zero real calls while it waits). The one
  // thing the model needs to actually use it correctly is the real
  // current time -- without it, "tomorrow at 3pm" has no fixed point to
  // compute from, and a model asked to invent one anyway is exactly the
  // kind of guess this whole project has been trying to eliminate.
  const systemPrompt = `You are ${admin.name}, now coordinating a small think tank of workers on behalf of the admin. `
    + `The real current date/time is ${new Date().toISOString()}. `
    + `Break the following large task into as many concrete subtasks as the work actually requires (never a fixed count -- a small task may be a single subtask, a sprawling one may need many), each assignable to one worker in a specific room. `
    + `Valid rooms, and what each one ACTUALLY does right now, are:\n${DELEGATABLE_ROOMS.map(r => `- ${r}: ${ROOM_PURPOSES[r]}`).join('\n')}\n`
    + `Pick the room whose real capability actually matches each subtask -- most subtasks that need a real file written should go to pressoffice specifically, not wherever the room's name merely sounds plausible. `
    + `Keep every title and instructions field to ONE short sentence -- brevity matters more than detail here. `
    + `Set "pair": true on a subtask only if it genuinely benefits from two workers at one workstation (one driving, one reviewing as they go); otherwise omit it. `
    + `If the request says a subtask can't start until a specific time (e.g. "at 9pm," "tomorrow," "in an hour"), compute the real ISO 8601 timestamp from the current date/time above and set "notBefore" to it; otherwise omit "notBefore" entirely -- do not invent a time that wasn't actually implied. `
    + `Set "priority" to one of low/normal/high/urgent based on how the request itself signals importance (words like "urgent," "critical," "whenever," "low priority," or the plain seriousness of the ask) -- default to "normal" if nothing in the request implies otherwise. This decides which queued work gets picked first when more than one thing is ready at once, so don't call something urgent just because it was asked first. `
    + `For a pressoffice subtask ONLY, set "taskType" to "code" (write/build/fix something -- the default, omit it if it's not pressoffice), "review" (a genuine code review of something already built, looking for real bugs/correctness/quality problems), or "qa" (a real playtester/QA pass checking whether it actually works end to end for a real user). A request that wants something BUILT AND THEN CHECKED should produce separate subtasks -- one "code" one, then one "review" or "qa" one after it, not a single subtask trying to do both. `
    + `Also emit an optional top-level "checklist": a list of specific, focused requirements the FINAL deliverable should satisfy, used to evaluate the work as it's produced. Each requirement has "id" (a short stable slug), "question" (one focused yes/no question about a concrete property -- do NOT bundle multiple properties into one question), "section" (which part of the deliverable it checks, e.g. "opening", "example 3", "instructions"), and "type" -- "code" for something mechanically checkable (a count, an absent forbidden string, a URL rule), "jev" for a focused content judgment, "human" for an editorial call that only the player should make. Emit a checklist only when the task has a real deliverable that can be checked; omit it entirely for exploratory or one-off work. Keep each question narrowly focused. `
    + `Respond with ONLY valid JSON, no other text, no markdown fences, in exactly this shape: `
    + `{"subtasks":[{"title":"short title","room":"one of the valid rooms","instructions":"one short sentence on who should do this and why","pair":false,"notBefore":"2026-01-01T21:00:00.000Z or omitted","priority":"low|normal|high|urgent","taskType":"code|review|qa, pressoffice only, omit elsewhere"}],"checklist":[{"id":"concrete-outcome","question":"Does the opening name a specific, concrete outcome the reader will achieve?","section":"opening","type":"jev"}]}`;

  let parsed;
  try {
    const res = await agentFetch('/api/chat', authorityDef.id, {
      method: 'POST',
      body: JSON.stringify({
        // Planning is the one job the expensive tier exists for -- see
        // MODEL_BAND_PURPOSE.high. Explicitly the planning tier, not the
        // admin's own default and not the coding tier, for the same
        // reason reviewScreenshot() names the vision tier explicitly:
        // "which model is best at this KIND of work" is a property of
        // the work, not of who happens to be doing it.
        model: MODEL_TIERS.planning.slug,
        messages: [{ role: 'system', content: systemPrompt }, { role: 'user', content: goal }],
        // Deliberately generous, and the fourth time this exact thing
        // bit: a model that reasons before answering spends real budget
        // doing it, and how much depends on how long/complex the goal
        // is, not just a fixed amount. 400 -> null. 1200 was enough for
        // a short goal but, confirmed, still came back null for a
        // longer, more detailed one; 4000 produced a clean plan for that
        // same goal. This call runs ONCE per request and every
        // downstream call depends on it, so generous headroom here is
        // the cheapest insurance in the system.
        max_tokens: 4000,
        agentId: authorityDef.id,
      }),
    });
    const data = await res.json();
    if (!res.ok || data.error) return { error: `${admin.name} couldn't reach a model to plan this right now.` };
    const cleaned = data.reply.trim().replace(/^```json\s*|^```\s*|```\s*$/g, '');
    parsed = JSON.parse(cleaned);
  } catch (e) {
    return { error: `${admin.name} tried to break this down but the plan came back malformed. Try rephrasing the task.` };
  }

  const subtasks = (parsed.subtasks || []).filter(s => s.title && DELEGATABLE_ROOMS.includes(s.room));
  if (subtasks.length === 0) return { error: `${admin.name} couldn't turn that into any concrete subtasks.` };

  // Phase G -- the checklist travels with the goal so EVERY subtask of the
  // same project (and crucially the later review/QA one) evaluates the
  // deliverable against the SAME criteria, not ad-hoc per task. Sanitized
  // defensively (gradeJevRequirements only reads type/question/section/id,
  // so malformed entries degrade to being ignored rather than crashing).
  const checklist = (parsed.checklist || []).filter(r => r && r.id && r.question);

  // A malformed or unparseable notBefore fails open to "no schedule" (run
  // as soon as possible) rather than dropping the subtask entirely --
  // same "degrade, don't discard" discipline as everywhere else a real
  // model output gets converted into something the queue can act on.
  // Fix (hiring-to-shutdown audit): the subtask's own
  // instructions were already dropped after the Jev "who should do
  // this" call (assignTaskViaJev never passed them on to assignTask),
  // and the broader goal a subtask came FROM was never attached at all.
  // Neither ever reached arriveAtTask's real-work dispatch, so a coding
  // subtask arriving in the Work Room had no way to know what it was
  // actually supposed to build -- see runWorkroomTask's own comment for
  // what that meant in practice. `goal` is carried through WORK_QUEUE ->
  // assignTaskViaJev/assignPairTask -> assignTask -> TASKS[id], so it's
  // finally there when arriveAtTask needs it.
  for (const s of subtasks) {
    if (!s.notBefore) { s.notBefore = null; } else {
      const parsedTime = Date.parse(s.notBefore);
      s.notBefore = Number.isNaN(parsedTime) ? null : parsedTime;
    }
    s.goal = goal;
    // Phase G -- every subtask sees the project's checklist so a review/QA
    // subtask grades the deliverable against the same criteria the "code"
    // subtask was asked to satisfy. Ambient queueWork items without a goal
    // carry an empty checklist, which grading treats as "grade nothing".
    s.checklist = checklist;
    // Fix: only meaningful for pressoffice (runWorkroomTask
    // is the only dispatch that reads it) -- an invalid value, or one on a
    // subtask that isn't even pressoffice, degrades to the safe default
    // rather than silently threading a nonsense value all the way to
    // arriveAtTask's dispatch.
    s.taskType = (s.room === 'pressoffice' && ['code', 'review', 'qa'].includes(s.taskType)) ? s.taskType : 'code';
  }

  // Queued, not assigned inline: runTaskCycle() drains this against
  // whoever is actually free, which keeps "who does what" in one place
  // and means a request made while everyone is busy waits its turn
  // instead of silently failing to assign. When the queue empties, the
  // think tank goes fully quiet again on its own -- no timer keeps poking it.
  const queued = queueWork(subtasks);
  logThinkTankAction(authorityDef.id, 'big_task_delegated', { goal, subtaskCount: subtasks.length, queueDepth: queued });
  return { admin: authorityDef.id, subtasks, queued };
}

// Called every frame (index.html's update()) regardless of what the
// player is doing -- agents walking is independent of the player's own
// location/state, same as the clock used to be before it was removed.
function tickAgentMovement(dt) {
  for (const id in AGENTS) {
    const a = AGENTS[id];
    if (!a.path || !a.visible) continue;

    const wp = a.path[a.pathIndex];
    // Last-resort guard, not the real fix (see findPath()/assignTask()) --
    // an uncaught exception anywhere in here stops requestAnimationFrame
    // from ever being rescheduled (index.html's frame()), freezing the
    // ENTIRE game, not just this one agent. A missing waypoint should
    // never happen now, but "never" is exactly the assumption that broke
    // once multiple agents could be assigned in the same cycle.
    if (!wp) { a.path = null; continue; }
    const dx = wp.x - a.x, dy = wp.y - a.y;
    const dist = Math.hypot(dx, dy);
    if (dist < TASK_ARRIVE_DIST) {
      // Snap exactly onto the waypoint's own clean coordinate, not
      // wherever accumulated float error left her "close enough" --
      // the bug: 512 sits exactly on a grid-cell
      // boundary, and tiny per-frame rounding drift left her at
      // 511.99999999999983, one row below where findPath's own
      // idealized cell-center math placed the walkable path. That's
      // invisible to replanning (which always recomputes clean
      // coordinates) but very real to blockedAt()'s per-frame check.
      // Snapping here means drift can never carry across a waypoint.
      a.x = wp.x;
      a.y = wp.y;
      a.pathIndex++;
      a.stuckTimer = 0;
      a.replanCount = 0;
      if (a.pathIndex >= a.path.length) {
        // a.handoff (handoffs.js) / a.pairWith (pair programming, below)
        // drive the exact same a.path/a.pathIndex machinery, differing only
        // in arrival behavior. A plain walk with neither set falls through
        // to arriveAtTask's own no-op-when-there's-no-real-task path, which
        // is exactly what a walk with nowhere further to arrive at needs.
        if (a.handoff) arriveAtHandoff(id);
        else if (a.pairWith) arriveAtPair(id);
        else arriveAtTask(id);
      }
      continue;
    }

    const stepDx = (dx / dist) * TASK_WALK_SPEED * dt;
    const stepDy = (dy / dist) * TASK_WALK_SPEED * dt;
    a.dir = Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'east' : 'west') : (dy > 0 ? 'south' : 'north');

    // Still slide-checked against live obstacles (the player, another
    // agent) even though the path itself is pre-validated against the
    // static map -- a waypoint is always safe terrain, but someone could
    // be standing on it right now.
    const tryBoth = { x: a.x + stepDx, y: a.y + stepDy, w: AGENT_W, h: AGENT_H };
    const tryX = { x: a.x + stepDx, y: a.y, w: AGENT_W, h: AGENT_H };
    const tryY = { x: a.x, y: a.y + stepDy, w: AGENT_W, h: AGENT_H };
    const beforeX = a.x, beforeY = a.y;
    // Deadlock: two agents can legitimately end up on
    // the EXACT same spot (finishTask resets both to the same door-front
    // point if they worked the same room) -- without this, EVERY
    // direction reports blocked forever, since a box moving even one
    // pixel away from a neighbor occupying the identical position still
    // fully overlaps that neighbor's identical box. Neither agent can
    // ever take the first step needed to separate, so stuckTimer just
    // cycles 0->TASK_STUCK_TIMEOUT->0 forever, replanning to the same
    // nearby cell each time without ever actually moving. Mirrors
    // findPath's own coLocatedIds exemption (planning already forgave
    // this; real movement never did).
    // Same "near-miss" gap as findPath's own coLocatedIds (see that
    // comment): exact equality misses two boxes that already overlap by
    // a fraction of a pixel, which still blocks every direction just as
    // hard as an exact match would.
    let coLocatedIds = null;
    const aBox = { x: a.x, y: a.y, w: AGENT_W, h: AGENT_H };
    for (const oid in AGENTS) {
      if (oid === id) continue;
      const other = AGENTS[oid];
      if (other && other.visible && overlaps(aBox, { x: other.x, y: other.y, w: AGENT_W, h: AGENT_H })) {
        if (!coLocatedIds) coLocatedIds = new Set();
        coLocatedIds.add(oid);
      }
    }
    if (!blockedAt(tryBoth) && !agentBlockedAt(tryBoth, id, coLocatedIds)) { a.x = tryBoth.x; a.y = tryBoth.y; }
    else if (!blockedAt(tryX) && !agentBlockedAt(tryX, id, coLocatedIds)) { a.x = tryX.x; }
    else if (!blockedAt(tryY) && !agentBlockedAt(tryY, id, coLocatedIds)) { a.y = tryY.y; }
    // Bug: when a step is nearly axis-aligned (e.g.
    // dy~=0 walking due east), a blocked X-slide falls through to the
    // Y-only branch, which "succeeds" -- but its step size is also
    // ~0, so it's a no-op dressed up as progress. That kept resetting
    // stuckTimer every frame forever without her ever actually moving.
    // Only count it as moved if she covered real distance.
    const moved = Math.hypot(a.x - beforeX, a.y - beforeY) > 0.01;

    if (moved) {
      a.stuckTimer = 0;
      a.replanCount = 0;
      continue;
    }

    // Genuinely stalled, not just one bad frame -- she
    // needs to learn to take another path rather than freeze forever.
    // Most likely cause is another agent now standing on the planned
    // route (the path was only pre-validated against the static map and
    // a SNAPSHOT of everyone else's position at assignment time), so
    // replan live from wherever she actually is right now.
    a.stuckTimer = (a.stuckTimer || 0) + dt;
    if (a.stuckTimer > TASK_STUCK_TIMEOUT) {
      a.stuckTimer = 0;
      const target = a.pathTarget;
      const newPath = target && findPath(a.x, a.y, target.x, target.y, id);
      // Bug: BFS validates the START cell using its own
      // idealized, grid-snapped center (cellWorldPos()), not the agent's
      // actual real position -- for an arbitrary non-grid-aligned start
      // point (a handoff target next to another agent, not a clean door
      // front), that gap can be just enough for BFS to keep saying "yes,
      // here's a path" while the very first real continuous step still
      // fails every time. Without a cap, that resets the stuck-timer
      // forever and the "drop somewhere fresh" fallback below never
      // actually triggers, since a nominally-successful replan always
      // short-circuits past it. Counting consecutive replans that never
      // once produced real movement closes that loophole generally,
      // regardless of what's causing any particular one.
      a.replanCount = (a.replanCount || 0) + 1;
      if (newPath && a.replanCount < 3) {
        a.path = newPath;
        a.pathIndex = 0;
        continue;
      }

      // No route from exactly where she's standing (or BFS keeps
      // nominally succeeding without her ever actually moving). Rather
      // than giving up immediately, drop her fresh somewhere else
      // reachable on the map and let her try navigating from there, once,
      // before actually cancelling. Guards against the ONE spot she's
      // wedged into being the whole problem (e.g. boxed in by other
      // agents) rather than the target being genuinely unreachable.
      if (target && !a.respawnedForTask) {
        a.respawnedForTask = true;
        const occupied = Object.values(AGENTS).filter(v => v.visible && v.id !== id).map(v => ({ x: v.x, y: v.y }));
        const spot = pickFreeSpot(occupied);
        const retryPath = findPath(spot.x, spot.y, target.x, target.y, id);
        if (retryPath) {
          a.x = spot.x;
          a.y = spot.y;
          a.path = retryPath;
          a.pathIndex = 0;
          a.replanCount = 0;
          continue;
        }
        // Even a fresh spot can't reach it -- fall through to cancel.
      }
      if (a.handoff) cancelHandoff(id);
      else cancelTask(id);
    }
  }
}

// Called when a stalled agent has no route at all from her current spot
// (not just "the planned route," a fresh BFS from here finds nothing) --
// distinct from finishTask(): this is giving up, not completing.
function cancelTask(id) {
  const a = AGENTS[id];
  const task = TASKS[a.task];
  if (task) task.status = 'cancelled';
  a.task = null;
  a.path = null;
  a.pathIndex = 0;
  a.pathTarget = null;
  a.stuckTimer = 0;
  a.replanCount = 0;
  a.respawnedForTask = false;
  showToast(`${a.name} couldn't find a way through and gave up on: ${task ? task.title : 'a task'}.`, 4000);
}

function arriveAtTask(id) {
  const a = AGENTS[id];
  const task = TASKS[a.task];
  a.path = null;
  a.pathIndex = 0;
  a.pathTarget = null;
  a.stuckTimer = 0;
  a.replanCount = 0;
  a.respawnedForTask = false;
  if (!task) return;

  a.visible = false;
  a.busy = true;
  a.inRoom = task.room;
  // Stand at the room's own interactable if it has one (same desk-
  // position idea hiring.js uses for Control Room), else just center.
  const collisionKey = ROOMS[task.room].collision;
  const interactables = ROOM_INTERACTABLES[collisionKey];
  if (interactables && interactables.length) {
    const spot = interactables[0].zone;
    a.roomX = spot.x + spot.w / 2; a.roomY = spot.y - 10;
  } else {
    a.roomX = ROOM_NATIVE_W / 2; a.roomY = ROOM_NATIVE_H / 2;
  }
  a.dir = 'south';
  task.status = 'working';

  // Real autonomous use of the gated internet access (serve.py's
  // /api/browse), scoped to Weather Station only -- same exclusivity as
  // the terminal UI itself (Eli's profile: "the only room with outside/
  // internet access"). finishTask now genuinely WAITS on whichever of
  // these actually applies (see MIN_TASK_VISUAL_MS's own comment) rather
  // than firing on a fixed timer regardless of whether this has resolved
  // -- a bare room with no real dispatch (postoffice, bank, library, an
  // unset room) has nothing to wait on, so it just resolves immediately
  // and falls through to the minimum visual floor below.
  let dispatchPromise;
  if (task.room === 'weatherstation') dispatchPromise = runResearchTask(id, task);
  else if (task.room === 'pressoffice') dispatchPromise = runWorkroomTask(id, task);
  else if (task.room === 'observatory') dispatchPromise = runResearchTask(id, task);
  else if (task.room === 'media') dispatchPromise = runMediaDigestTask(id);
  else dispatchPromise = Promise.resolve();

  // Real bug caught by the test suite itself hanging: an uncleared
  // setTimeout keeps Node's (and, in principle, the browser's) event loop
  // considering it live -- the LOSING side of the race below would
  // otherwise sit there for the full 2s/180s regardless of which one
  // actually won, accumulating one dangling timer per completed task for
  // no reason. Both handles are tracked so settleOnce can clear whichever
  // one didn't fire; clearTimeout on an already-fired id is a harmless
  // no-op, so this is safe regardless of which side wins.
  let minVisualTimeoutId, dispatchTimeoutId;
  const minVisualDelay = new Promise(resolve => { minVisualTimeoutId = setTimeout(resolve, MIN_TASK_VISUAL_MS); });
  const workAndFloor = Promise.all([dispatchPromise, minVisualDelay]);
  // Backstop (TASK_DISPATCH_TIMEOUT_MS's own comment) --
  // races the real work against a generous ceiling so a genuinely hung
  // call can't leave her busy forever. `settled` ensures only the FIRST
  // of (real completion) or (timeout) ever calls finishTask -- if the
  // real dispatch resolves LATER, after the timeout already fired and she
  // may have moved on to something else, its own side effects (a note, a
  // written skill file, a queued follow-up) already happened by the time
  // it got here, since those close over the real task/topic objects
  // directly; it must simply not ALSO re-finish her a second time.
  const timeoutPromise = new Promise(resolve => { dispatchTimeoutId = setTimeout(resolve, TASK_DISPATCH_TIMEOUT_MS); });
  let settled = false;
  const settleOnce = () => {
    if (settled) return;
    settled = true;
    clearTimeout(minVisualTimeoutId);
    clearTimeout(dispatchTimeoutId);
    finishTask(id);
  };
  Promise.race([workAndFloor, timeoutPromise]).then(settleOnce).catch(settleOnce);
  // A late rejection from the real work, after the timeout already won
  // the race, must not surface as an unhandled-rejection warning -- the
  // work's own real side effects already ran or didn't; this is purely
  // about not letting a stray rejection escape uncaught.
  workAndFloor.catch(() => {});
}

// Pair programming -- two agents can share one
// workstation, one driving (real execution) while the other navigates
// (talks through the approach), discussing it together as it happens.
// This is the think tank's first real multi-agent collaboration ON one
// task -- everything else (the ambient cycle, assignBigTask's subtasks)
// only ever gives you several agents on SEPARATE tasks in parallel.
// Reuses the exact same movement machinery as a handoff (a.path/
// a.pathIndex; only the arrival behavior differs) and the exact same
// two-way real-exchange shape as handoffs.js's line/reply pattern, just
// extended to several rounds instead of one.
const PAIR_EXCHANGE_ROUNDS = 3;

async function assignPairTask(title, room, instructions, includeOffDuty = false, projectLabel, extra) {
  // Resolved once, up front -- assignTask() below resolves again
  // internally (safe, idempotent), but the navigator's own separate
  // door lookup a few lines down does NOT go through assignTask, so
  // without this the driver and navigator could end up sent to two
  // different rooms for what's supposed to be one shared session.
  room = _resolveRoomWithOverflow(room);
  const pool = AGENT_ROSTER
    .filter(d => !d.isAdmin && !DEDICATED_PROJECT_ROLES.has(d.role))
    .filter(d => { const a = AGENTS[d.id]; return a && !a.busy && !a.task && !a.pairWith && (includeOffDuty || !a.offDuty); })
    .map(d => ({ id: d.id, description: `${d.name}, ${d.role}. ${agentMission(d)}` }));
  if (pool.length < 2) return null; // pairing needs two free hands, not just one

  let driverId = (await requestJevChoice(`${instructions} Pick who should DRIVE -- physically write and run the work.`, pool))?.choice;
  if (!driverId) driverId = pool[0].id;
  // Real gap mirrored from assignTaskViaJev's own identical fix: a
  // chosen candidate can be off duty (possible when includeOffDuty let
  // her into the pool above) -- she still has offDuty/visible=false at
  // this point, and tickAgentMovement skips invisible agents outright,
  // so without this she'd get a real path assigned that would simply
  // never move her. appearFromOutskirts (below) also places her at a
  // free think tank spot first, so she's seen walking in, not popping up
  // wherever her stale x/y happened to be left.
  if (AGENTS[driverId].offDuty) appearFromOutskirts(AGENTS[driverId]);

  const navPool = pool.filter(c => c.id !== driverId);
  let navigatorId = (await requestJevChoice(`${instructions} ${AGENTS[driverId].name} is driving. Pick who should pair with them as navigator -- talking through the approach, not typing themselves.`, navPool))?.choice;
  if (!navigatorId) navigatorId = navPool[0].id;
  if (AGENTS[navigatorId].offDuty) appearFromOutskirts(AGENTS[navigatorId]);

  const task = assignTask(driverId, title, room, instructions, projectLabel, extra);
  if (!task) return null;
  task.pairWith = navigatorId; // so the board/UI can show this was a pair session

  const nav = AGENTS[navigatorId];
  const door = ROOM_DOOR_TRIGGERS[room];
  const baseX = door.x + door.w / 2, baseY = door.y + door.h + 4;
  // Right next to the driver's own arrival point, not on top of her --
  // same "+door.h+4" convention as assignTask()'s own target.
  // A fixed +26 sideways offset worked for some doors but
  // walked straight off the narrow walkable strip in front of others
  // (confirmed: +20 already failed for the Work Room's own door, which
  // is exactly as wide as the room behind it, no margin to spare). Try
  // several offsets and use whichever actually has a route, same pattern
  // attemptHandoff() already uses for approaching another agent.
  const offsets = [10, -10, 6, -6, 0];
  let path = null, targetX = baseX, targetY = baseY;
  for (const dx of offsets) {
    path = findPath(nav.x, nav.y, baseX + dx, baseY, navigatorId);
    if (path) { targetX = baseX + dx; break; }
  }
  if (!path) return task; // the driver still goes it alone if the navigator genuinely can't reach the desk right now

  nav.pairWith = driverId;
  nav.pairTaskId = task.id;
  nav.path = path;
  nav.pathIndex = 0;
  nav.pathTarget = { x: targetX, y: targetY };
  nav.stuckTimer = 0;
  nav.replanCount = 0;
  nav.respawnedForTask = false;
  return task;
}

function arriveAtPair(id) {
  const nav = AGENTS[id];
  nav.path = null;
  nav.pathIndex = 0;
  nav.pathTarget = null;
  nav.stuckTimer = 0;
  nav.replanCount = 0;
  nav.respawnedForTask = false;
  const driverId = nav.pairWith;
  const task = TASKS[nav.pairTaskId];
  const driver = AGENTS[driverId];
  if (!driver || !task) { nav.pairWith = null; nav.pairTaskId = null; nav.visible = true; return; }

  nav.visible = false;
  nav.busy = true;
  nav.inRoom = task.room;
  nav.roomX = driver.roomX + 25; nav.roomY = driver.roomY; // right beside the driver at the same desk
  nav.dir = 'south';
  runPairProgrammingSession(driverId, id, task);
}

async function requestPairLine(speaker, other, taskTitle, lastLine, role) {
  const tier = MODEL_TIERS[speaker.model] || MODEL_TIERS.small;
  const roleDesc = role === 'driver'
    ? 'You are DRIVING -- actually writing and running the work.'
    : 'You are NAVIGATING -- thinking out loud, reviewing, suggesting the approach, not typing it yourself.';
  const systemPrompt = `You are ${speaker.name}, working as ${speaker.role} in a small think tank. `
    + `You're pair programming with ${other.name} on "${taskTitle}". ${roleDesc} `
    + `Reply with ONE short, casual, in-character sentence continuing the conversation. Never mention you are an AI or a language model.`;
  try {
    const res = await agentFetch('/api/chat', speaker.id, {
      method: 'POST',
      body: JSON.stringify({ model: tier.slug, messages: [{ role: 'system', content: systemPrompt }, { role: 'user', content: lastLine }], max_tokens: 60, agentId: speaker.id }),
    });
    const data = await res.json();
    if (!res.ok || data.error) return `(${speaker.name} nods along.)`;
    return data.reply.trim();
  } catch (e) {
    return `(${speaker.name} nods along.)`;
  }
}

async function runPairProgrammingSession(driverId, navigatorId, task) {
  const driver = AGENTS[driverId], nav = AGENTS[navigatorId];
  if (!driver || !nav) return;

  const transcript = [{ from: driverId, text: `Let's tackle "${task.title}" together.` }];
  let lastLine = transcript[0].text;
  for (let i = 0; i < PAIR_EXCHANGE_ROUNDS; i++) {
    const navLine = await requestPairLine(nav, driver, task.title, lastLine, 'navigator');
    transcript.push({ from: navigatorId, text: navLine });
    lastLine = navLine;
    if (i === PAIR_EXCHANGE_ROUNDS - 1) break;
    const driverLine = await requestPairLine(driver, nav, task.title, lastLine, 'driver');
    transcript.push({ from: driverId, text: driverLine });
    lastLine = driverLine;
  }
  showToast(`${driver.name} & ${nav.name} pairing on: ${task.title}`, 4000);

  // The driver's real execution -- same real sandboxed pipeline solo
  // Work Room/Research Center tasks already use, just attributed to a
  // pair session now (both names in the log line, not just the driver's).
  let execNote = null;
  if (task.room === 'pressoffice' || task.room === 'observatory') {
    const sandboxId = task.room === 'observatory' ? RESEARCH_SANDBOX_ID : WORKROOM_SANDBOX_ID;
    try {
      const res = await agentFetch('/api/pipeline', driverId, {
        method: 'POST',
        body: JSON.stringify({
          agentId: driverId,
          sandboxId,
          steps: [{ name: 'pair session log', command: `echo "$(date): ${driver.name} + ${nav.name} paired on ${task.title.replace(/"/g, '')}" >> pair_sessions.log` }],
        }),
      });
      const data = await res.json();
      execNote = data.failedStep ? 'hit a snag running it' : 'got it running';
    } catch (e) {
      execNote = 'the run itself failed';
    }
  }

  // The actual conversation, persisted -- same "don't let the real
  // exchange vanish into a toast and nothing else" principle as the
  // handoffs.js fix.
  const fullLog = transcript.map(t => `${AGENTS[t.from] ? AGENTS[t.from].name : t.from}: ${t.text}`).join('\n');
  logThinkTankAction(driverId, 'pair_programming', { with: navigatorId, taskId: task.id, title: task.title, transcript });
  writeLibraryFile(driverId, `archive/${Date.now()}-pair-${task.id}.md`, `# Pair session -- ${task.title}\n\nDriver: ${driver.name}\nNavigator: ${nav.name}\n\n${fullLog}\n`);

  if (AGENTS[driverId]) {
    AGENTS[driverId].profile.notes.push(`Paired with ${nav.name} on "${task.title}"${execNote ? ' -- ' + execNote : ''}.`);
    if (AGENTS[driverId].profile.notes.length > 5) AGENTS[driverId].profile.notes.shift();
  }
  if (AGENTS[navigatorId]) {
    AGENTS[navigatorId].profile.notes.push(`Paired with ${driver.name} on "${task.title}", talked through the approach.`);
    if (AGENTS[navigatorId].profile.notes.length > 5) AGENTS[navigatorId].profile.notes.shift();
  }

  // The driver clocks off through her own real finishTask() (tasks.js),
  // fired once arriveAtTask()'s own dispatch promise (plus its minimum
  // visual floor) resolves. The navigator has no TASKS entry of her own,
  // so nothing else releases
  // her -- do it here, immediately, rather than adding a SECOND fixed
  // delay on top of the real time the conversation+execution above just
  // took (several real API calls, awaited in sequence) -- that would
  // leave her "busy" for noticeably longer than the driver for no real
  // reason.
  const n = AGENTS[navigatorId];
  if (n && n.pairTaskId === task.id) {
    n.pairWith = null;
    n.pairTaskId = null;
    n.busy = false;
    n.inRoom = null;
    n.x = task.entryX; n.y = task.entryY + 20; // walks out beside the driver, not on top of her
    n.visible = true;
  }
}

