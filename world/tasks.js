// A small, real task system -- just enough to give an agent somewhere to
// walk to and something to do when they arrive. Agents had
// no incentive to go anywhere until they had a task. Not a project-
// management system -- one task per agent at a time, a fixed target room,
// a fixed work duration. This is the first thing all session that makes
// an agent actually WALK across the map on their own; hiring and meetings
// both teleport.
//
// Who gets assigned a task is a real Jev call now (assignTaskViaJev(),
// below) -- exactly the classifier "pick one of N" decision Jev is for
// (see jev.js). assignTask() itself still takes an explicit agentId and
// stays usable directly if you want to hand-pick someone.
//
// Movement follows a real BFS-computed grid path (findPath(), below), not
// a straight line -- the first version was a straight-line walk with only
// basic axis-sliding, which you caught getting stuck at a water's edge
// almost immediately (a straight line to a door can easily cross a river
// that only has specific bridge crossings). Reuses the exact same
// box-fit cell test (cellFitsAgent(), agents.js) the reachability fix
// already established, so a computed path only ever crosses terrain an
// agent's real footprint can actually occupy.

const TASK_WALK_SPEED = 60; // world-space px/sec -- a bit slower than the player's 90
const TASK_ARRIVE_DIST = 12;
// Fix: this used to be the WHOLE duration -- a fixed
// setTimeout fired finishTask() regardless of whether the real dispatched
// work (checkWeatherReference/runWorkroomTask/runResearchTask/
// runMediaDigestTask, all real async calls -- a model call, a crawl, a
// multi-step coding pipeline) had actually finished yet. A fast check
// looked artificially slow; a genuinely long one (a scheduled research
// crawl, a multi-file coding task) got cut off mid-flight -- finishTask()
// ran, clocked her off, and reset her position while that same work was
// still awaiting a response in the background. Now just a MINIMUM visual
// floor (arriveAtTask awaits the real dispatch promise itself and takes
// whichever is longer), so a task still visibly takes a moment even when
// the real work finishes near-instantly, without ever capping how long a
// genuinely slower one gets to actually run.
// let, not const -- nothing in the real app ever reassigns this, but the
// test harness (vm.runInContext) needs to override it to a tiny value to
// test the completion-timing logic itself without waiting on a real
// multi-second delay every run.
let MIN_TASK_VISUAL_MS = 2000;

// Removing the old fixed timer above also removed
// an accidental backstop against a genuinely hung dispatch -- nothing now
// bounds how long a single real call can leave an agent "busy." Sized
// from the real worst cases already in this codebase: _urlopen_with_
// resilience (serve.py) allows up to 3 attempts x 30s each for a single
// /api/chat call, and runCodingTask's probe/continuation loop can chain
// up to ~5-6 such calls in one task. 180s comfortably clears realistic
// worst cases (a full crawl+synthesize, a multi-round coding pass)
// without being so tight it fires under normal load. This frees the
// AGENT's own visible state -- it does not, and cannot, cancel the
// underlying server-side call, which has no cancellation mechanism today;
// if that call eventually does resolve, its own real side effects (a
// note, a written skill file, a queued follow-up) still happen, they just
// can't also re-finish an agent who's since moved on (see arriveAtTask's
// `settled` guard).
let TASK_DISPATCH_TIMEOUT_MS = 180000;
const TASK_STUCK_TIMEOUT = 1.2; // seconds with zero progress before replanning around whatever's blocking

let TASKS = {};
let nextTaskId = 1;

// A small rotating pool of demo tasks -- the original ask was "give
// agents an incentive to go somewhere," which the very first version
// satisfied with exactly one hardcoded task, ever, for one agent. Real
// gap: everyone else (Omar included) then just stands
// still forever with nothing to do. Cycling through a few room-appropriate
// tasks keeps the whole roster actually moving, not just whoever Jev
// picked once at boot.
const TASK_POOL = [
  // `pair: true` -- these two can be worked as a real pair
  // (one drives, one navigates, sharing one workstation) instead of
  // always solo. See assignPairTask()/runPairProgrammingSession() below.
  { title: "Review this week's research findings", room: 'observatory', instructions: "Pick whoever is best suited to review this week's research findings in the Research Center.", pair: true },
  // pressoffice is labeled "Work Room" in ROOMS -- real sandboxed
  // execution, not press work (the room's own art/name predates that
  // decision). Runs a real command in the shared sandbox on arrival, see
  // runWorkroomTask() below.
  { title: 'Maintain the shared Work Room tooling', room: 'pressoffice', instructions: 'Pick whoever is best suited to maintain the shared coding tools in the Work Room.', pair: true },
  { title: 'Sort the incoming mail queue', room: 'postoffice', instructions: 'Pick whoever is best suited to sort the incoming mail at the Post Office.' },
  { title: "Log today's weather readings", room: 'weatherstation', instructions: 'Pick whoever is best suited to log readings at the Weather Station.' },
  { title: 'Catalog new library arrivals', room: 'library', instructions: 'Pick whoever is best suited to catalog new arrivals at the Library.' },
  // `dependsOn` (see handoffs.js): agents should talk to
  // each other for real reasons -- a real dependency between two rooms'
  // work is the first one. Broadcast prep needs the day's press briefing
  // filed first; the ledger reconciliation needs the day's mail (invoices,
  // correspondence) sorted first. Whoever finishes the DEPENDED-ON room's
  // task walks over and hands off to whoever needs it next, rather than
  // the dependency being invisible plumbing nobody ever sees play out.
  { title: "Prep tonight's broadcast segment", room: 'media', dependsOn: 'pressoffice', instructions: 'Pick whoever is best suited to prep tonight\'s broadcast segment at Media.' },
  { title: 'Reconcile the daily ledger', room: 'bank', dependsOn: 'postoffice', instructions: 'Pick whoever is best suited to reconcile the daily ledger at the Bank.' },
  // Gap: every task above maps to one named specialist
  // (weather -> Eli, banking -> Ben, etc.), so Dev's four assistants --
  // whose whole job is "overflow support," nobody's specialty -- could
  // never win a best-fit call against the actual specialist. This is the
  // one task in the pool explicitly for them.
  { title: 'Handle Studio overflow support', room: 'media', instructions: "Pick whichever of Dev's assistants (Sam, Theo, Yuki, or Omar) is best suited to handle overflow support work in the Studio, not Dev himself." },
];

// room -> timestamp of its last completed task. Kept as a real record
// (finishTask() writes it); no longer drives task SELECTION, since there
// is no longer an ambient "pick something to do" step at all.
let lastTaskCompletedAt = {};

// Nothing should be working when there is nothing
// to do, and no API calls should fire at all while the think tank is idle.
//
// The old design made that impossible by construction -- TASK_POOL is a
// fixed list that ALWAYS has entries, so pickNextTask() (now deleted)
// always found "work," every idle agent always got assigned something,
// and the 6-second cycle burned 2-3 real Jev calls per agent forever,
// long after the player stopped asking for anything. Confirmed: 247
// firing reviews and a continuous stream of `decide` calls with nobody
// actually waiting on any of it.
//
// Now: real requested work goes in this queue, and ONLY this queue
// drives assignment. Empty queue means runTaskCycle() returns before
// making a single call. A new request from you wakes the admin
// (assignBigTask), who breaks it into real subtasks that land here --
// the agents drain it, and when it's empty everything goes quiet again.
// TASK_POOL itself stays because handoffs.js still reads its `dependsOn`
// metadata to know which rooms' work depends on which.
let WORK_QUEUE = [];
const WORK_ITEM_MAX_ATTEMPTS = 3;

// Research on a topic should be able to run on a
// recurring cadence (daily/weekly/etc.), not just once. Each entry is a
// standing request, not a queued task -- it only ever turns INTO a real
// WORK_QUEUE item (via checkResearchSchedule/queueWork below) once its own
// cadence says it's due, exactly the same "not work until it's actually
// time" shape queueWork's own notBefore already has for one-off items.
// `seenUrls` is what makes a repeat run cover new ground instead of
// re-collecting the same pages -- see crawlAndCollect's skipUrls (world.js).
let RESEARCH_TOPICS = [];
let nextResearchTopicId = 1;

// No UI for this yet, same as sandbox-download/crawlAndCollect -- a real,
// callable capability, not a demo. `lastRunAt` defaults to 0 (due
// immediately) rather than Date.now(), since defining a topic and then
// waiting a full cadence before it ever runs once isn't what "start
// researching this" should mean.
function defineResearchTopic({ topic, startUrl, linkKeyword, pageKeyword, cadenceMs, createdBy, lastRunAt = 0 }) {
  const entry = {
    id: 'topic-' + (nextResearchTopicId++),
    topic, startUrl, linkKeyword: linkKeyword || '', pageKeyword: pageKeyword || '',
    cadenceMs, lastRunAt, seenUrls: [], createdBy: createdBy || 'player', createdAt: Date.now(),
  };
  RESEARCH_TOPICS.push(entry);
  return entry;
}

// Called once per tick, same interval as runTaskCycle (index.html) -- but
// deliberately NOT folded into runTaskCycleBody itself, since that
// function already short-circuits on an empty/not-due WORK_QUEUE (the
// idle-think tank-spends-nothing contract, test_idle_quiet.mjs) and this
// needs to run regardless of whether anything's already queued. Zero
// calls of its own either way: an empty RESEARCH_TOPICS, or one where
// nothing's due yet, costs exactly what an empty WORK_QUEUE already does
// -- nothing. lastRunAt is stamped BEFORE the resulting task is even
// assigned, same reasoning as every other "mark it taken up front" fix
// (WORK_QUEUE's own attemptedThisCycle) -- a topic due at
// the same instant runTaskCycleBody happens to take a while to actually
// assign it must not be picked up a second time on the very next tick.
function checkResearchSchedule() {
  const now = Date.now();
  for (const topic of RESEARCH_TOPICS) {
    if (now - (topic.lastRunAt || 0) < topic.cadenceMs) continue;
    // "Date aware... if it has run previously" --
    // captured BEFORE topic.lastRunAt gets overwritten below, since that's
    // the real point in time this topic was last actually checked.
    // previousRunAt === 0 for a genuinely first-ever run, which is exactly
    // right -- crawlAndCollect's own since-comparison only ever matters
    // for a URL already in seenUrls, and a first run has none.
    const previousRunAt = topic.lastRunAt || 0;
    topic.lastRunAt = now;
    queueWork([{
      title: `Scheduled research: ${topic.topic}`,
      room: 'observatory',
      instructions: `Crawl starting from ${topic.startUrl} and update the "${topic.topic}" skill file with anything genuinely new since last time.`,
      goal: topic.topic,
      research: { topicId: topic.id, since: previousRunAt },
    }]);
  }
}

// A separate standing job from research topics -- this reviews what
// research (or anything else) has already WRITTEN to pending_review/
// skills/, not a per-topic crawl. One shared cadence (not per-topic:
// there's nothing to key it on here) is enough. 30 minutes -- short
// enough that a real backlog doesn't sit for a full day before anyone
// looks at it, long enough that this doesn't hit the Library listing
// endpoint every few seconds.
const SKILL_REVIEW_CADENCE_MS = 30 * 60 * 1000;
let lastSkillReviewAt = 0;

// Same shape and same "costs nothing until it's actually due" reasoning
// as checkResearchSchedule -- called from the same interval (index.html).
function checkSkillReviewSchedule() {
  const now = Date.now();
  if (now - lastSkillReviewAt < SKILL_REVIEW_CADENCE_MS) return;
  lastSkillReviewAt = now;
  queueWork([{
    title: 'Review pending skill files',
    room: 'observatory',
    instructions: 'Review whatever is waiting in pending_review/skills/ and decide, file by file, whether each one is accurate and worth keeping as real reference material.',
    skillReview: true,
  }]);
}

// A scheduled-for-later item (see queueWork's notBefore) is real work
// that EXISTS, but isn't work yet -- it must count as "nothing to do
// right now" everywhere that gate matters, or a single item scheduled
// for tomorrow would keep the ambient cycle (and auto-hire/firing-review,
// which share this same gate) making real calls every 6s in the
// meantime, exactly the endless-spend bug the queue was built to kill.
function _isWorkItemDue(item) {
  return !item.notBefore || Date.now() >= item.notBefore;
}

// Among what's actually due, the highest WORK_PRIORITY value wins; a
// STRICT > (not >=) means the first-queued item at that priority keeps
// its place, so equal-priority work still resolves in arrival order
// rather than being reshuffled every tick.
function _pickNextDueIndex(excludeItems) {
  let bestIndex = -1, bestPriority = -Infinity;
  for (let i = 0; i < WORK_QUEUE.length; i++) {
    const item = WORK_QUEUE[i];
    if (!_isWorkItemDue(item)) continue;
    // excludeItems (runTaskCycleBody's own attemptedThisCycle set): real
    // fix from an audit -- without this, a failed item gets re-queued
    // at the BACK, but this SAME synchronous loop (bounded by rosterSize,
    // often 15-20) can still reach it again before returning, exhausting
    // all WORK_ITEM_MAX_ATTEMPTS retries within one call and giving real
    // transient congestion (agents genuinely walking toward a contested
    // door) zero actual wall-clock time to clear before abandoning.
    // Confirmed: 3 real subtasks all abandoned within about a
    // second of each other, not spread across separate scheduled ticks.
    if (excludeItems && excludeItems.has(item)) continue;
    const priority = item.priority ?? WORK_PRIORITY.normal;
    if (priority > bestPriority) { bestPriority = priority; bestIndex = i; }
  }
  return bestIndex;
}

// The single "is anything actually happening" test, shared by every
// interval-driven mechanism so none of them can independently decide to
// keep spending while the think tank is idle.
function thinkTankHasWork() {
  if (WORK_QUEUE.some(_isWorkItemDue)) return true;
  return Object.values(AGENTS).some(a => a.task || a.handoff || a.pairWith || a.busy);
}

// Let the player mark some queued work as more important than
// other work, so it gets picked before older but less important items
// instead of everything processing in strict arrival order regardless of
// how much it actually matters. Named levels, not a raw number a caller
// has to invent and keep consistent -- 'high'/'urgent' mean the same
// thing everywhere they're used.
const WORK_PRIORITY = { low: 0, normal: 1, high: 2, urgent: 3 };

function _normalizePriority(p) {
  if (typeof p === 'number' && Object.values(WORK_PRIORITY).includes(p)) return p;
  if (typeof p === 'string' && Object.prototype.hasOwnProperty.call(WORK_PRIORITY, p.toLowerCase())) {
    return WORK_PRIORITY[p.toLowerCase()];
  }
  return WORK_PRIORITY.normal;
}

// Real requested work entering the think tank -- the only way anything gets
// queued now. `pair` is preserved from the admin's own plan so pair
// programming survives the removal of the ambient pool (it used to come
// from a hardcoded TASK_POOL flag). `notBefore` (a real epoch-ms
// timestamp, e.g. Date.now() + 3600000 for "in an hour") is optional --
// for agents that can't start something until a specific
// time. An item with it sits inertly in the queue, untouched and not
// burning retry attempts, until that time actually arrives (see
// _isWorkItemDue / runTaskCycleBody). `priority` (a WORK_PRIORITY name or
// its number, defaulting to 'normal') decides which DUE item gets picked
// first when more than one is ready -- see _pickNextDueIndex.
function queueWork(items) {
  for (const item of items) {
    if (!item || !item.title || !item.room) continue;
    WORK_QUEUE.push({
      title: item.title,
      room: item.room,
      instructions: item.instructions || `Pick whoever is best suited for: ${item.title}`,
      pair: !!item.pair,
      notBefore: item.notBefore || null,
      priority: _normalizePriority(item.priority),
      // Fix (hiring-to-shutdown audit): queueWork()
      // whitelists its fields defensively, which is right in general,
      // but it was silently dropping the broader goal a subtask came
      // from (assignBigTask) -- the ONE thing that would have told
      // arriveAtTask's real-work dispatch what a coding subtask was
      // actually FOR. Optional and null for anything that isn't part of
      // a real project (ambient TASK_POOL items, direct queueWork calls
      // with no goal at all) -- runWorkroomTask's own fallback covers
      // that case.
      goal: item.goal || null,
      // Same whitelist-drop risk as goal above, for checkResearchSchedule's
      // scheduled items: { topicId } pointing back into RESEARCH_TOPICS,
      // so runResearchTask can find the full topic record (startUrl,
      // seenUrls, etc.) once this actually gets assigned.
      research: item.research || null,
      // Same whitelist-drop risk as goal/research above -- 'code' is
      // always a safe default (runWorkroomTask's existing behavior) for
      // anything that didn't set this explicitly.
      taskType: item.taskType || 'code',
      // Same reasoning, for checkSkillReviewSchedule's standing sweep --
      // whether this item is the skill-curation pass, not a research
      // topic or ordinary code/review work.
      skillReview: !!item.skillReview,
      // Phase G -- the project's evaluation checklist (or [] for ambient
      // work with no checklist), so a review/QA subtask can grade the
      // deliverable against what the "code" subtask was asked to satisfy.
      checklist: item.checklist || [],
    });
  }
  return WORK_QUEUE.length;
}

// Called on an interval (index.html). Gap: assigning
// exactly one task per call meant only ever one agent was walking at a
// time even with nine others standing idle -- they should
// all be able to work in parallel, not queue for a turn. Loops one
// assignment per currently-idle agent instead, each a fresh Jev call
// (since who's idle shrinks after every assignment in the loop).
// Real bug caught during this Jev-call audit: unlike attemptAutoHire()
// (hiring.js) and attemptAutoFiringReview() (firing.js), which both
// explicitly guard against a second interval tick starting while the
// first is still mid-flight, this had no such guard at all -- and unlike
// those two, this one does multiple SEQUENTIAL awaited Jev calls per idle
// agent (one to pick the task, one to pick who does it, two more if it's
// a pair task), all inside one `await`-in-a-loop cycle. With more than a
// couple of idle agents, one cycle can plausibly take longer than its own
// 6s setInterval period, so the next tick would start a second, fully
// overlapping cycle: duplicate Jev calls for the same idle roster, and a
// real race on who gets assigned what, since neither cycle sees the
// other's in-flight assignments until they land.
let taskCycleRunning = false;

async function runTaskCycle() {
  if (taskCycleRunning) return;
  taskCycleRunning = true;
  try {
    await runTaskCycleBody();
  } finally {
    taskCycleRunning = false;
  }
}

async function runTaskCycleBody() {
  // The whole point of the queue: an idle think tank makes ZERO API calls.
  // This returns before any Jev call, any chat call, anything -- not
  // "cheaply," but literally not at all. A queue that's non-empty but
  // has nothing DUE yet (every item scheduled for later) must be exactly
  // as quiet as an empty one, or a single far-future item would keep
  // this cycle spending on every 6s tick until its time arrives.
  if (!WORK_QUEUE.some(_isWorkItemDue)) return;

  function _awakeIdleCount() {
    return AGENT_ROSTER.filter(d => !d.isAdmin).filter(d => { const a = AGENTS[d.id]; return a && !a.busy && !a.task && !a.pairWith && !a.offDuty; }).length;
  }
  function _anyAvailableIncludingOffDuty() {
    return AGENT_ROSTER.filter(d => !d.isAdmin).some(d => {
      const a = AGENTS[d.id];
      if (!a || a.busy || a.task || a.pairWith) return false;
      // Waking her would increase the active count -- only "available"
      // if there's real room under MAX_ACTIVE_AGENTS. An already on-duty
      // idle agent doesn't change that count, so she's always available.
      return a.offDuty ? canActivateAnother() : true;
    });
  }

  // Bounded by real roster size, not a fixed idle count computed once --
  // recomputed live each iteration since who's awake/idle changes as
  // assignments land within the same tick.
  const rosterSize = AGENT_ROSTER.filter(d => !d.isAdmin).length;
  // Fix (hiring-to-shutdown audit): tracks which items
  // THIS call has already attempted, so a failed-and-requeued item can't
  // be pulled again within the same synchronous cycle -- see
  // _pickNextDueIndex's own comment for the real failure this closes.
  // A retried item still gets picked up on the NEXT scheduled tick (a
  // real few seconds later, index.html's setInterval), so genuine
  // transient congestion gets an actual chance to clear before the
  // WORK_ITEM_MAX_ATTEMPTS budget is spent.
  const attemptedThisCycle = new Set();
  for (let i = 0; i < rosterSize; i++) {
    // Highest-priority DUE item wins, not just the first one in arrival
    // order -- a scheduled-for-later item further back must still never
    // be picked, retried, or attempt-counted before its own time
    // arrives, and among what IS ready, more important work goes first.
    // Ties (equal priority) resolve to whichever was queued earliest,
    // since this only overwrites bestIndex on a STRICTLY higher value.
    const dueIndex = _pickNextDueIndex(attemptedThisCycle);
    if (dueIndex === -1) break; // nothing left this tick is actually ready (or due, but already tried once this cycle)
    const pick = WORK_QUEUE[dueIndex];
    attemptedThisCycle.add(pick);
    // A genuinely SCHEDULED item (a real notBefore) can always wake
    // someone, the way a real on-call rotation works. Ordinary ambient
    // work can ALSO wake someone, but only as a real fallback -- when
    // every on-duty agent is already busy, not preferentially over one
    // who's simply idle and available. Either way, waking
    // anyone is still capped by
    // MAX_ACTIVE_AGENTS (canActivateAnother()) -- 25 active/awaiting-
    // scheduled-work is a hard ceiling, not a preference, so a due item
    // that would need to exceed it just waits (re-queued, retried next
    // cycle) until someone already active clocks off and frees a slot.
    const canWakeOffDuty = (!!pick.notBefore || _awakeIdleCount() === 0) && canActivateAnother();
    if (_awakeIdleCount() === 0 && !(canWakeOffDuty && _anyAvailableIncludingOffDuty())) break; // nobody who could take this right now, of any kind
    // Taken off the queue up front: a failed assignment (e.g. a room
    // whose door-front cell is already occupied) must not leave the same
    // item at the head of the queue to be retried forever on every tick,
    // which would recreate exactly the endless-spend problem the queue
    // exists to eliminate. Re-queued at the BACK on failure instead, so
    // it's retried later without blocking everything behind it.
    WORK_QUEUE.splice(dueIndex, 1);
    const assigned = pick.pair
      ? await assignPairTask(pick.title, pick.room, pick.instructions, canWakeOffDuty, pick.goal, { research: pick.research, taskType: pick.taskType, skillReview: pick.skillReview, checklist: pick.checklist })
      : await assignTaskViaJev(pick.title, pick.room, pick.instructions, canWakeOffDuty, pick.goal, { research: pick.research, taskType: pick.taskType, skillReview: pick.skillReview, checklist: pick.checklist });
    if (!assigned) {
      // Bounded retries, or a permanently-unassignable item (an
      // unreachable room, a role nobody holds) would sit at the head of
      // the queue being retried on every single tick forever -- which is
      // the exact endless-spend failure this queue exists to remove,
      // just with extra steps.
      pick.attempts = (pick.attempts || 0) + 1;
      if (pick.attempts < WORK_ITEM_MAX_ATTEMPTS) {
        WORK_QUEUE.push(pick);
      } else {
        logThinkTankAction(null, 'work_item_abandoned', { title: pick.title, room: pick.room, attempts: pick.attempts });
      }
    }
  }
}

// BFS shortest path over the same box-fit grid computeReachableMask()
// already uses, from (startX, startY) to (targetX, targetY) in world-
// space. Returns a list of world-space waypoints (cell centers), or null
// if no route exists. Computed once per task assignment, not every frame
// -- the grid is small (86x48 cells) so this is cheap.
//
// `excludeAgentId` is who's asking for the path -- every OTHER currently-
// visible agent's position is treated as a temporary obstacle too, not
// just the static map. Real bug caught here: the first version only
// checked the static map, so a route could -- and did -- pass straight
// through wherever a stationary agent happened to be standing, producing
// a genuine deadlock at that exact spot (neither the diagonal nor either
// axis-only slide could get past a box-shaped obstacle sitting on the
// path itself). Since this stub roster doesn't wander on its own, a
// snapshot of "where everyone is right now" stays valid for the whole
// walk.
function findPath(startX, startY, targetX, targetY, excludeAgentId) {
  const { cols, rows, cell } = COLLISION_GRID;

  // Found via the new test suite: AGENT_W (20) being wider than one grid
  // cell (16 world px, see transitionIsFree's own comment below) means an
  // agent resting EXACTLY on the mover's own start position -- confirmed
  // to happen live (two finished tasks parking on the identical door-
  // front coordinate) -- also registers as blocking every cell adjacent
  // to the start, not just the start cell itself. startCellIsFree already
  // exempts the start cell's OWN occupancy on this same "already here,
  // regardless of who else is nearby" reasoning; extending that to
  // whoever is co-located with her (not everyone nearby, just whoever is
  // at the exact same spot) keeps her from being deadlocked in place by
  // someone standing on top of her.
  // Deadlock: two agents landing a FRACTION
  // of a pixel apart (float drift from independent walk calculations, not
  // an exact re-park) never match this exact-equality check, yet their
  // 20px-wide boxes still fully overlap -- each then blocks EVERY cell
  // adjacent to the other's start (AGENT_W > cell, so an overlapping
  // neighbor's box always spans into the surrounding cells too), and
  // findPath fails outright for both, forever, with no retry able to ever
  // change the outcome. "Co-located" has to mean "already overlapping,"
  // not "bit-identical coordinates" -- exact equality was only ever a
  // stand-in for that, and real walks don't reliably land on it.
  const coLocatedIds = new Set();
  const startBox = { x: startX, y: startY, w: AGENT_W, h: AGENT_H };
  for (const id in AGENTS) {
    if (id === excludeAgentId) continue;
    const other = AGENTS[id];
    if (other && other.visible && overlaps(startBox, { x: other.x, y: other.y, w: AGENT_W, h: AGENT_H })) coLocatedIds.add(id);
  }

  function cellWorldPos(gx, gy) {
    return { x: (gx * cell + cell / 2) * SCALE - AGENT_W / 2, y: (gy * cell + cell / 2) * SCALE - AGENT_H / 2 };
  }
  function agentBlockedIgnoringCoLocated(box) {
    for (const id in AGENTS) {
      if (id === excludeAgentId || coLocatedIds.has(id)) continue;
      const other = AGENTS[id];
      if (!other.visible) continue;
      if (overlaps(box, { x: other.x, y: other.y, w: AGENT_W, h: AGENT_H })) return true;
    }
    return false;
  }
  function boxFree(x, y) {
    return !blockedAt({ x, y, w: AGENT_W, h: AGENT_H }) && !agentBlockedIgnoringCoLocated({ x, y, w: AGENT_W, h: AGENT_H });
  }
  function cellIsFree(gx, gy) {
    const p = cellWorldPos(gx, gy);
    return boxFree(p.x, p.y);
  }
  // How far (in cells) a contested target may be relaxed to the nearest
  // actually-free spot -- 3 cells is 48 world px, close enough to still
  // read as "arrived at the door" visually, generous enough that 2-3
  // agents converging on the same doorway in quick succession each find
  // somewhere real to stand rather than rejecting the whole request.
  const TARGET_RELAX_RADIUS = 3;
  // Bug, one layer deeper than the relaxation above:
  // two agents planned in the SAME synchronous batch (a mass "send
  // everyone home") start from the same/near-identical position, so
  // neither sees the other as a live obstacle yet -- both independently
  // relax to the exact same nearest free cell, since a deterministic ring
  // search from identical inputs always picks the identical answer.
  // Confirmed directly: after separating by one step via the co-located
  // exemption (tickAgentMovement/agentBlockedAt), they immediately
  // re-gridlock a few px apart, because AGENT_W (20) is wider than a
  // single step -- the exact-position fix alone only delays the freeze
  // by a heartbeat, it doesn't prevent it. Treating another agent's
  // ALREADY-CHOSEN pathTarget (not just their current physical position)
  // as claimed means the SECOND agent planned in the same batch picks a
  // genuinely different cell instead of the identical one, the moment
  // its own turn comes -- callers set a.pathTarget AFTER findPath
  // returns, so this only ever sees a target that's real by the time it
  // matters, never a stale one from a walk that's already finished.
  function cellClaimedByAnothersTarget(gx, gy) {
    for (const oid in AGENTS) {
      if (oid === excludeAgentId) continue;
      const other = AGENTS[oid];
      if (!other || !other.visible || !other.pathTarget) continue;
      const ogx = Math.floor((other.pathTarget.x / SCALE) / cell);
      const ogy = Math.floor((other.pathTarget.y / SCALE) / cell);
      if (ogx === gx && ogy === gy) return true;
    }
    return false;
  }
  function cellIsFreeForDestination(gx, gy) {
    return cellIsFree(gx, gy) && !cellClaimedByAnothersTarget(gx, gy);
  }
  function nearestFreeCell(gx, gy, maxRadius) {
    if (cellIsFreeForDestination(gx, gy)) return [gx, gy];
    for (let r = 1; r <= maxRadius; r++) {
      for (let dy = -r; dy <= r; dy++) {
        for (let dx = -r; dx <= r; dx++) {
          if (Math.max(Math.abs(dx), Math.abs(dy)) !== r) continue; // ring only, closest radius first
          const ngx = gx + dx, ngy = gy + dy;
          if (ngx < 0 || ngy < 0 || ngx >= cols || ngy >= rows) continue;
          if (cellIsFreeForDestination(ngx, ngy)) return [ngx, ngy];
        }
      }
    }
    return null;
  }
  // A second real bug, same session: validating only cell CENTERS isn't
  // enough when the agent's box (20px) is wider than one cell (16px
  // world-space) -- two adjacent centers can each individually fit while
  // the straight line BETWEEN them still clips an obstacle somewhere in
  // between. A single midpoint sample wasn't enough either --
  // via Playwright: a transition passed its midpoint check at
  // computation time, then froze a live agent at a point 25% of the way
  // along that same segment, well off-center. Sampling several points
  // along the segment (not just the middle one) closes that gap.
  function transitionIsFree(gx1, gy1, gx2, gy2) {
    const p1 = cellWorldPos(gx1, gy1), p2 = cellWorldPos(gx2, gy2);
    const STEPS = 4;
    for (let i = 1; i < STEPS; i++) {
      const t = i / STEPS;
      if (!boxFree(p1.x + (p2.x - p1.x) * t, p1.y + (p2.y - p1.y) * t)) return false;
    }
    return true;
  }

  // Bug (hiring-to-shutdown audit, a layer
  // deeper than the pickFreeSpot fix earlier): cellWorldPos
  // returns a box's top-left CENTERED on a cell (offset by -AGENT_W/2,
  // -AGENT_H/2 from the cell's own corner) -- which is exactly what
  // a.x/a.y become after ANY real walk, since tickAgentMovement snaps
  // onto the exact waypoint findPath returned. Reversing that with the
  // naive Math.floor((x/SCALE)/cell) -- as this line used to, for the
  // AGENT'S OWN current position -- does NOT recover the same cell,
  // because AGENT_W (20) doesn't evenly divide cell*SCALE (16): the
  // reverse formula is off by exactly one cell in X for any position
  // that came from a real waypoint (Y happens to divide evenly, so it
  // was never visibly broken there). Confirmed: an agent resting at
  // her own real, valid task.entryX/entryY (itself a real waypoint,
  // after the entryX/entryY fix earlier) could not
  // pathfind ANYWHERE from her own position -- findPath was checking
  // one cell to the left of where she actually was. The START must be
  // read as the box's CENTER (matching cellFitsAgent/computeReachable-
  // Mask's own convention) for this to agree with itself. TARGETS stay
  // on the raw (non-centered) conversion -- a door-front/task
  // coordinate is a literal point, not a re-examined agent box.
  const startGx = Math.floor(((startX + AGENT_W / 2) / SCALE) / cell), startGy = Math.floor(((startY + AGENT_H / 2) / SCALE) / cell);
  let targetGx = Math.floor((targetX / SCALE) / cell), targetGy = Math.floor((targetY / SCALE) / cell);

  // Found via the new test suite, not live play -- no real caller passes
  // an out-of-map target today (doors/agents are always within GROUND_W/
  // H), so this was a latent gap, not an observed crash. Still worth
  // closing: without it, a target outside the grid entirely reaches
  // `visited[targetGy][targetGx]` after the BFS with targetGy/targetGx
  // beyond the `visited` array's own bounds -- `visited[targetGy]` is
  // undefined, and indexing into that throws, which (per the frame-freeze
  // bug fixed) is exactly the class of error that
  // must never surface uncaught from here.
  if (targetGx < 0 || targetGy < 0 || targetGx >= cols || targetGy >= rows) return null;
  // Crash building the outskirts exit-room doors: an
  // earlier version of a door's approach-point math (since fixed) landed
  // one row past the bottom of the grid, which corrupted that agent's
  // OWN x/y the moment it was written before the crash -- so on the
  // very next call, it was the START, not just the target, that was
  // out of bounds. The target check above didn't catch it because the
  // out-of-bounds coordinate was the FIRST argument that call. Every
  // caller's start position is assumed valid the same way targets used
  // to be; assume less.
  if (startGx < 0 || startGy < 0 || startGx >= cols || startGy >= rows) return null;

  // The START cell only needs the STATIC check, not agentBlockedAt --
  // Bug, directly from the new "reappear at the
  // door you entered" fix: two agents finishing tasks at the same room
  // now land on the exact same spot, and the second one's own occupied
  // cell was failing agentBlockedAt against the first, permanently
  // blocking her from ever being assigned anywhere else. She's already
  // validly standing here regardless of who else is nearby; only the
  // route AHEAD of her needs to account for other agents.
  function startCellIsFree(gx, gy) {
    if (!cellFitsAgent(gx, gy, cell)) return false;
    const p = cellWorldPos(gx, gy);
    return !blockedAt({ x: p.x, y: p.y, w: AGENT_W, h: AGENT_H });
  }

  if (!startCellIsFree(startGx, startGy)) return null;

  // Root cause, confirmed: a fixed approach point in front of a
  // door/desk (assignTask's door-front target, etc.) is exactly one cell --
  // the instant ANY agent is standing on or near it, cellIsFree() on the
  // exact target cell returns false and
  // findPath returned null OUTRIGHT for every OTHER agent trying to reach
  // that same door, not just a slower route. Confirmed directly: with an
  // agent parked on the south outskirts approach point, findPath to that
  // door failed from literally every start position tested, including the
  // target cell's own immediate neighbors -- not a multi-agent tie, a hard
  // rejection before BFS even ran. This is the actual mechanism behind the
  // "agent X is blocking the door" reports (Greta, then dev/yuki, then
  // priya/sam twice, then priya/theo/greta converging on an identical
  // waypoint) -- whoever gets there first (or gets stuck there) makes the
  // exact target UNPLANNABLE for everyone else, and since the existing
  // stuck-timer replan (tickAgentMovement) retries the SAME fixed
  // a.pathTarget, it hits the identical rejection every time and can spin
  // forever without ever reaching the give-up/respawn fallback that
  // assumes a plan attempt can at least fail differently.
  //
  // The fix: approaching a door doesn't need the exact pixel, just close
  // enough -- same reasoning assignPairTask's own hand-rolled "try several
  // offsets" loop already uses for this exact problem, generalized here so
  // every caller (sendAgentOffDuty, the stuck-timer replan, assignTask,
  // handoffs) gets it for free instead of needing its own copy. A small
  // ring search outward from the requested cell for the nearest ACTUALLY
  // free one -- capped at TARGET_RELAX_RADIUS cells (48 world px) so this
  // never silently substitutes a wildly different destination, just
  // "close enough to still be the same door."
  if (!cellIsFreeForDestination(targetGx, targetGy)) {
    const relaxed = nearestFreeCell(targetGx, targetGy, TARGET_RELAX_RADIUS);
    if (!relaxed) return null; // nothing free anywhere near the requested target -- genuinely blocked, not just contested
    [targetGx, targetGy] = relaxed;
  }

  // Bug: an agent re-assigned to the exact room she's
  // already resting at (parked right on that room's own door-front
  // arrival point from a previous task) has start cell === target cell,
  // so the BFS below never runs and the backtrack loop below THAT never
  // executes either -- producing a truthy-but-EMPTY path array, not
  // null. assignTask()'s `if (!path) return null` doesn't catch an empty
  // array (`![]` is false), so the assignment went through with a
  // zero-length path and crashed tickAgentMovement on the very next tick
  // (`a.path[a.pathIndex]` reading .x off undefined). Return a single
  // real waypoint instead so "already there" still behaves like a
  // (trivially instant) walk, not a broken one.
  if (startGx === targetGx && startGy === targetGy) return [cellWorldPos(targetGx, targetGy)];

  const visited = Array.from({ length: rows }, () => new Array(cols).fill(false));
  const prev = Array.from({ length: rows }, () => new Array(cols).fill(null));
  const queue = [[startGx, startGy]];
  visited[startGy][startGx] = true;
  let found = false;

  while (queue.length && !found) {
    const [gx, gy] = queue.shift();
    for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const nx = gx + dx, ny = gy + dy;
      if (nx < 0 || ny < 0 || nx >= cols || ny >= rows) continue;
      if (visited[ny][nx] || !cellIsFree(nx, ny) || !transitionIsFree(gx, gy, nx, ny)) continue;
      visited[ny][nx] = true;
      prev[ny][nx] = [gx, gy];
      if (nx === targetGx && ny === targetGy) { found = true; break; }
      queue.push([nx, ny]);
    }
  }
  if (!visited[targetGy][targetGx]) return null; // genuinely unreachable from here

  const cellsPath = [];
  let cx = targetGx, cy = targetGy;
  while (cx !== startGx || cy !== startGy) {
    cellsPath.push([cx, cy]);
    [cx, cy] = prev[cy][cx];
  }
  cellsPath.reverse();

  // Top-left-corner convention, matching every other position in this
  // codebase (a.x/a.y, the player, blockedAt()) -- NOT the cell center.
  // Real bug caught here: the first version returned raw cell centers,
  // a systematic half-agent-box offset from where a.x/a.y actually needed
  // to be, which compounded across waypoints and produced a real,
  // reproducible freeze at a narrow crossing (a bridge) even though every
  // individual cell had already been verified to fit.
  return cellsPath.map(([gx, gy]) => ({
    x: (gx * cell + cell / 2) * SCALE - AGENT_W / 2,
    y: (gy * cell + cell / 2) * SCALE - AGENT_H / 2,
  }));
}

// Don't let a room's fixed desk count become a hard limit on
// getting work done. Work Room and Research Center share the exact same
// six-desk layout (ROOM_COLLISIONS.workstations via ROOM_INTERACTABLES),
// so overflow from one into the other is physically sensible, not just a
// fallback hack -- scoped to just this one pair, because this
// is about keeping ongoing work unblocked, not a general room-load-
// balancer, and that the think tank has enough rooms for now.
const ROOM_OVERFLOW_TARGET = { pressoffice: 'observatory' };

function _roomDeskCapacity(room) {
  const def = ROOMS[room];
  const interactables = def && ROOM_INTERACTABLES[def.collision];
  return interactables ? interactables.length : Infinity; // no known desk layout -- don't invent a limit
}

function _roomOccupancy(room) {
  return Object.values(AGENTS).filter(a => a.busy && a.inRoom === room).length;
}

// Only ever redirects to a room that itself has room to spare -- if BOTH
// are full, stays with the original rather than pretending a second full
// room is the answer. Idempotent: resolving an already-resolved room (or
// one with no overflow target at all) just returns it unchanged, so both
// assignTask() and assignPairTask() can safely call this without
// double-redirecting a pair session's driver and navigator differently.
function _resolveRoomWithOverflow(room) {
  const overflowTo = ROOM_OVERFLOW_TARGET[room];
  if (!overflowTo) return room;
  if (_roomOccupancy(room) < _roomDeskCapacity(room)) return room;
  return _roomOccupancy(overflowTo) < _roomDeskCapacity(overflowTo) ? overflowTo : room;
}

// `room` must be one of the buildings with a real door trigger (not
// Town Hall, House, or Command Center -- those are special-cased
// elsewhere and an agent can't "walk to" a button-only room).
//
// Refactor: projectLabel, then research, then taskType
// each arrived as one more trailing positional parameter -- a skillReview
// flag would have made an 8th. Bundled the newer, sparser ones into one
// `extra` object instead (projectLabel stays its own named param since
// it's checked far more centrally, in more places, than these); this is
// the point where growing the positional list further stops being
// reasonable, not a stylistic preference.
function assignTask(agentId, title, room, instructions, projectLabel, extra = {}) {
  const { research = null, taskType = 'code', skillReview = false, checklist = [] } = extra;
  room = _resolveRoomWithOverflow(room);
  const a = AGENTS[agentId];
  const door = ROOM_DOOR_TRIGGERS[room];
  if (!a || a.busy || a.task || !door) return null;

  // The door trigger's own center sits right at the building's edge --
  // real bug caught here: centering a full agent box exactly there can
  // clip the building itself, failing the box-fit test outright (this is
  // what actually caused an agent to look "stuck" -- findPath rejected
  // the target before any water was even involved). Target a point just
  // in front of the door instead, the same "+door.h+4" convention
  // exitRoom()/handleCancelCall() already use to place the player safely
  // clear of a door.
  const targetX = door.x + door.w / 2, targetY = door.y + door.h + 4;
  const path = findPath(a.x, a.y, targetX, targetY, agentId);
  // An unhandled exception inside update() (tickAgentMovement lives there)
  // stops requestAnimationFrame from ever being re-scheduled -- the WHOLE
  // game freezes dead, not just this one agent. findPath() itself is now
  // fixed to never hand back an empty array, but this check stays as real
  // defense-in-depth against exactly that failure mode, given how total
  // the consequence is.
  if (!path || path.length === 0) return null; // no route from here -- don't assign a task they can't actually reach

  const id = 'task-' + (nextTaskId++);
  // entryX/entryY: exactly where she walked in from, so finishTask() can
  // put her back there rather than a random spot on the map -- she should
  // walk back out of the door she used, not teleport
  // somewhere arbitrary.
  //
  // Bug (hiring-to-shutdown audit): this
  // used to store the RAW, pre-relaxation targetX/targetY -- fine for
  // findPath's OWN routing (which relaxes a contested/trap target cell
  // to the nearest one that actually passes cellFitsAgent internally),
  // but finishTask() later teleports her EXACTLY to entryX/entryY with
  // no such check. If the door's raw "+door.h+4" target happened to
  // land in a cell that fails cellFitsAgent (confirmed: this
  // affects real doors, not a hypothetical), every agent who ever
  // finished a task there got teleported straight into the same
  // permanent trap findPath itself had already correctly routed AROUND
  // on the way in. The path's own real final waypoint is guaranteed to
  // pass cellFitsAgent (findPath never returns a cell that doesn't), so
  // using it here instead keeps "where she rests" and "where she can
  // safely be" the same guarantee, not two different ones.
  const restX = path[path.length - 1].x, restY = path[path.length - 1].y;
  TASKS[id] = { id, title, room, instructions, projectLabel, research, taskType, skillReview, checklist, assignedTo: agentId, status: 'walking', createdAt: Date.now(), entryX: restX, entryY: restY };
  a.task = id;
  a.path = path;
  a.pathIndex = 0;
  a.pathTarget = { x: targetX, y: targetY }; // kept so a mid-walk replan knows where "there" still is
  a.stuckTimer = 0;
  a.replanCount = 0;
  a.respawnedForTask = false;
  logThinkTankAction(agentId, 'task_assigned', { taskId: id, title, room });
  return TASKS[id];
}

// Uses Jev to pick who does the task, from every non-admin agent who
// isn't already busy or on a task -- the admin is excluded the same way
// whoNeedsHelp() excludes them (hiring is their job, not task work).
// Falls back to the first eligible candidate if the Jev call fails, so an
// API outage doesn't mean tasks simply never get assigned.
//
// `includeOffDuty`: gap testing assignBigTask() below --
// a player-initiated, meaningful task has no eligible candidates at all
// once the ambient TASK_POOL cycle has run everyone off-duty, even though
// off-duty was only ever meant to bound the AUTOMATIC busywork loop, not
// something the player deliberately asked for. When true, an off-duty
// pick is woken back up (visible, on-duty) before the task starts.
// Every agent is protected from general assignment the same way: they're
// busy/task/pairWith when they're already working, and assignment filters
// on that state, never a role-wide ban. (Review/QA/UI-research are real,
// generally-delegatable kinds of work -- see runReviewTask,
// runResearchTask's projectLabel branch, and assignBigTask's taskType --
// so no role is carved out of the pool.)
async function assignTaskViaJev(title, room, instructions, includeOffDuty = false, projectLabel, extra) {
  const candidates = AGENT_ROSTER
    .filter(d => !d.isAdmin)
    // !a.pairWith -- race: without it, the ambient cycle
    // (this function, on its own 6s timer) could scoop up an agent mid-
    // walk toward a pair session (assignPairTask has already committed
    // her via pairWith, but her .task/.busy don't flip until she actually
    // arrives) into a completely unrelated solo task, corrupting her
    // in-flight path/state.
    .filter(d => { const a = AGENTS[d.id]; return a && !a.busy && !a.task && !a.pairWith && (includeOffDuty || !a.offDuty); })
    .map(d => ({ id: d.id, description: `${d.name}, ${d.role}. ${d.profile.mission}` }));
  if (candidates.length === 0) return null;

  let chosenId = (await requestJevChoice(instructions, candidates))?.choice;
  if (!chosenId) chosenId = candidates[0].id;

  const chosen = AGENTS[chosenId];
  if (chosen && chosen.offDuty) appearFromOutskirts(chosen);

  return assignTask(chosenId, title, room, instructions, projectLabel, extra);
}

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
  weatherstation: 'Weather Station -- currently only checks a fixed weather reference, not yet aware of a specific subtask\'s content.',
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

  // A subtask that can't start until a specific time (e.g. "log
  // tonight's weather at 9pm"). WORK_QUEUE items already support this
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
  if (task.room === 'weatherstation') dispatchPromise = checkWeatherReference(id);
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
    .map(d => ({ id: d.id, description: `${d.name}, ${d.role}. ${d.profile.mission}` }));
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

// Real code-writing capability, built for the finger-drumming project --
// an agent generates ACTUAL file content via a real chat call (model tier
// picked by Jev per pickModelTierForAction(), world.js -- coding is
// exactly the "reserve for real work" case that function exists for),
// then writes it into the shared sandbox through the exact same heredoc-
// via-/api/execute path validated live before any of this orchestration
// was built: a plain shell command, classified by Jev like any other
// (nothing new needed server-side -- the sandbox already lets an agent
// write/read files inside its own workspace).
// General, reusable sandbox-context reader -- reading "what real files
// already exist in this sandbox" isn't specific to any one project. Real
// bug this already fixed, worth keeping general: a naive
// concatenate-everything command can hit serve.py's own SANDBOX_MAX_OUTPUT
// cap (20,000 bytes) mid-file -- checking each file's size with `wc -c`
// before including it means the shell itself never emits enough to hit
// that ceiling, and an oversized file is skipped WHOLE with a clear note,
// never truncated mid-content.
async function getSandboxContext(agentId, sandboxId) {
  try {
    const res = await agentFetch('/api/execute', agentId, {
      method: 'POST',
      body: JSON.stringify({
        agentId,
        command: "budget=15000; total=0; for f in *.html *.js *.css *.py *.md; do [ -f \"$f\" ] || continue; sz=$(wc -c < \"$f\"); if [ $((total + sz)) -gt $budget ]; then echo \"--- $f --- (skipped, over context budget)\"; continue; fi; echo \"--- $f ---\"; cat \"$f\"; total=$((total + sz)); done",
        purpose: 'Reading current sandbox files for context before the next step.',
        sandboxId,
      }),
    });
    const data = await res.json();
    if (!data.allowed || !data.stdout) return '(nothing written yet)';
    return data.stdout;
  } catch (e) {
    return '(could not read current sandbox state)';
  }
}

function _heredocBalance(command) {
  const opens = (command.match(/<<-?\s*'?EOF'?/g) || []).length;
  const closes = command.split('\n').filter(line => line.trim() === 'EOF').length;
  return { opens, closes, balanced: opens === 0 || opens === closes };
}

// Real fix for a real, corroborated complaint -- three of five agent
// retrospectives independently named being "forced into awkward chunks"
// as a frustration (they blamed the model tier; the actual cause is this
// max_tokens ceiling, confirmed: a real generation cut
// off mid-heredoc, executed anyway, produced a genuinely broken file).
// The old fix (reject and give up) was safe but still left the agent's
// work half-finished. This is the real fix: when a generation looks
// truncated, ask the SAME model to continue exactly where it left off,
// bounded to a couple of attempts so this can't loop forever on a
// model that just can't finish the job.
const CODE_CONTINUATION_ATTEMPTS = 2;

// Real fix for the single biggest thing found wrong with this whole
// system, not by reading code but by running it for eight straight
// rounds: the core loop wasn't agentic. Every fact a fix ever needed
// (which globals exist, what a button's real parent is, what a boolean
// guard flag's actual value is) had to be pre-guessed and stuffed into
// context by a human, one missing fact at a time, because the model
// itself had no way to just go check. This lets the model ask FOR ITSELF,
// mid-generation, instead of guessing -- bounded so a confused model
// can't loop forever racking up real page-probe + chat calls.
const MAX_CODE_PROBE_ROUNDS = 3;

// A probe request looks like {"probeRequest": {"path", "actions", "probes"}}
// and nothing else -- deliberately narrow so an ordinary shell command
// (which never parses as JSON, let alone JSON shaped exactly like this)
// is never mistaken for one. Returns null for anything that isn't
// unambiguously a probe request, which just falls through to the normal
// shell-command path unchanged.
function _parseProbeRequest(reply) {
  let parsed;
  try {
    parsed = JSON.parse(reply.trim().replace(/^```(?:json)?\s*|\s*```\s*$/g, ''));
  } catch (e) {
    return null;
  }
  const req = parsed && parsed.probeRequest;
  if (!req || typeof req !== 'object') return null;
  if (!Array.isArray(req.actions) && !Array.isArray(req.probes)) return null;
  return { path: req.path || 'index.html', actions: req.actions || [], probes: req.probes || [] };
}

// `projectLabel` makes the actual thing being built a real parameter
// rather than a baked-in assumption, so this coding pipeline (probe-before-
// writing, heredoc-continuation handling, orphaned/phantom-file detection)
// serves any project, not just one hardcoded default.
async function runCodingTask(agentId, sandboxId, backlogItem, contextSummary, projectLabel) {
  const a = AGENTS[agentId];
  if (!a) return { ok: false, note: 'agent missing' };
  const tier = await pickModelTierForAction(a, `write real code for: ${backlogItem}`);
  let systemPrompt = `You are ${a.name}, a developer on a small team building ${projectLabel || 'a real, working small web application for the team'}. `
    + `Current project state:\n${contextSummary || '(nothing written yet -- you may be starting the first file.)'}\n\n`
    + `Your task right now: ${backlogItem}\n\n`
    + `Before writing your final answer, you may check real facts about how the ACTUAL running page behaves right now -- what globals it defines and their real shape, what a button click or keypress actually does -- instead of guessing a plausible-sounding name. `
    + `To do this, respond with ONLY a JSON object, no shell command, no markdown fences, no explanation, in exactly this shape: `
    + `{"probeRequest": {"path": "index.html", "actions": [{"type":"click","selector":"text=Play Challenge"}, {"type":"keydown","key":"q"}], "probes": ["document.body.className", "typeof window.SomeGlobal"]}}\n`
    + `Action types are click ({selector}), keydown ({key}), wait ({ms}), eval ({code}). You can do this up to ${MAX_CODE_PROBE_ROUNDS} times if you genuinely need to. `
    + `When ready to write the actual fix, respond with ONLY a single shell command, no explanation, no markdown fences, that writes or updates the necessary file(s) using one or more heredocs, e.g.:\n`
    + `cat > index.html << 'EOF'\n<contents>\nEOF\n`
    + `Write real, complete, working code for this specific piece -- no placeholders, no "TODO," no stubs. Keep it focused on just this task, building on what already exists rather than starting over. `
    + `If an existing file has grown large, PREFER adding a new small file (e.g. a separate .js file, linked with its own <script src> tag) over rewriting the whole large file -- a full rewrite risks running out of room mid-file and being cut off incomplete, which a small new file avoids.`;
  systemPrompt = prependWorkingGuide(systemPrompt, await readWorkingGuide());

  const messages = [{ role: 'system', content: systemPrompt }, { role: 'user', content: 'Go ahead.' }];
  let command = '';
  let attempts = 0;
  let probeRounds = 0;
  while (attempts <= CODE_CONTINUATION_ATTEMPTS) {
    let reply;
    try {
      const res = await agentFetch('/api/chat', agentId, {
        method: 'POST',
        body: JSON.stringify({ model: tier.slug, messages, max_tokens: 3500, agentId }),
      });
      const data = await res.json();
      if (!res.ok || data.error || !data.reply) return { ok: false, note: 'model call failed or returned nothing', tier: tier.label };
      reply = data.reply;
    } catch (e) {
      return { ok: false, note: 'model call failed: ' + e.message, tier: tier.label };
    }

    // Real agentic step, not a pre-guessed fact stuffed into context up
    // front: the model decides for itself, this round, whether it needs
    // to check something real before answering.
    const probeReq = probeRounds < MAX_CODE_PROBE_ROUNDS ? _parseProbeRequest(reply) : null;
    if (probeReq) {
      probeRounds++;
      messages.push({ role: 'assistant', content: reply });
      const probeData = await requestPageProbe(agentId, sandboxId, probeReq.path, probeReq.actions, probeReq.probes);
      let feedback = formatPageProbeResult(probeData);
      if (probeRounds >= MAX_CODE_PROBE_ROUNDS) {
        feedback += `\n\nYou have used all ${MAX_CODE_PROBE_ROUNDS} probe rounds. Respond now with ONLY your final shell command.`;
      }
      messages.push({ role: 'user', content: feedback });
      continue; // does not count against CODE_CONTINUATION_ATTEMPTS -- that guards against truncation, a different failure mode entirely
    }

    // Only the FIRST real command reply can have a fenced-code wrapper
    // worth stripping -- a continuation is raw mid-file text by
    // definition, and stripping ``` from the middle of a continued
    // heredoc body would corrupt real file content that happens to
    // contain a code fence.
    command += attempts === 0 ? reply.trim().replace(/^```(?:bash|sh)?\s*|\s*```\s*$/g, '') : reply;
    if (!command) return { ok: false, note: 'model returned an empty command', tier: tier.label };

    const balance = _heredocBalance(command);
    if (balance.balanced) break;
    attempts++;
    if (attempts > CODE_CONTINUATION_ATTEMPTS) {
      return { ok: false, note: `generation still looked truncated after ${CODE_CONTINUATION_ATTEMPTS} continuation attempt(s) (${balance.opens} heredoc(s) opened, ${balance.closes} closed) -- not executed`, tier: tier.label, command };
    }
    messages.push({ role: 'assistant', content: command });
    messages.push({ role: 'user', content: 'You were cut off before finishing. Continue EXACTLY where you left off -- do not repeat anything you already wrote, do not restart the heredoc or add a new one, just output the rest of the raw file content and the closing EOF line(s).' });
  }

  let result;
  try {
    const res = await agentFetch('/api/execute', agentId, {
      method: 'POST',
      body: JSON.stringify({ agentId, command, purpose: `Coding task: ${backlogItem}`, sandboxId }),
    });
    const data = await res.json();
    if (!data.allowed) return { ok: false, note: `blocked: ${data.reason || 'no reason given'}`, command, tier: tier.label };
    result = { ok: data.exitCode === 0 && !data.timedOut, exitCode: data.exitCode, stdout: data.stdout, stderr: data.stderr, command, tier: tier.label };
  } catch (e) {
    return { ok: false, note: 'execute failed: ' + e.message, command, tier: tier.label };
  }

  // Mistake class: a fix wrote a genuinely correct new
  // .js file, using
  // real property names verified against the actual running page, and it
  // had zero effect anyway -- because it was never referenced by a
  // <script src> tag in index.html, so the browser never loaded it. Not a
  // wrong guess this time; an orphaned file. A page-probe checks what's
  // ACTUALLY loaded, so it can't catch "you forgot to link this" -- that
  // needs a direct, mechanical check of the file the model just wrote.
  if (result.ok) {
    const writtenJsFiles = _extractWrittenJsFiles(command);
    if (writtenJsFiles.length > 0) {
      const unlinked = await _findUnlinkedJsFiles(agentId, sandboxId, writtenJsFiles);
      if (unlinked.length > 0) {
        const linked = await _autoLinkJsFiles(agentId, sandboxId, unlinked, messages, tier);
        result.note = linked.ok
          ? `auto-linked previously-orphaned file(s) into index.html: ${unlinked.join(', ')}`
          : `WARNING: created ${unlinked.join(', ')} but ${unlinked.length > 1 ? 'they are' : 'it is'} not referenced by a <script src> tag in index.html, and the automatic follow-up to link ${unlinked.length > 1 ? 'them' : 'it'} failed -- ${linked.note}`;
      }
    }

    // The mirror-image mistake: a rebuild
    // task, WITH the complete real file inventory already in its own
    // context (verified directly -- every real filename was present,
    // either in full or as a budget-skip notice), still fabricated three
    // plausible-sounding sibling files (keymap.js, score.js, game.js)
    // that were never written, and referenced them in <script src> tags
    // anyway. A page-probe can't catch this either -- it only sees
    // what's actually loaded; a 404 for a nonexistent script is silent.
    // This is a direct, mechanical check of index.html itself, same
    // "don't guess, check" discipline as the orphaned-file check above.
    const phantomRefs = await _findPhantomScriptRefs(agentId, sandboxId);
    if (phantomRefs.length > 0) {
      const cleaned = await _removePhantomScriptRefs(agentId, sandboxId, phantomRefs);
      const refList = phantomRefs.map(p => `${p.file} (in ${p.html})`).join(', ');
      const phantomNote = cleaned.ok
        ? `removed <script src> reference(s) to file(s) that don't actually exist: ${refList}`
        : `WARNING: HTML file(s) reference file(s) that were never written and don't exist: ${refList} -- automatic cleanup failed (${cleaned.note})`;
      result.note = result.note ? `${result.note} | ${phantomNote}` : phantomNote;
    }

    // Advisory only -- see _findDanglingSelectorRefs for why there's no
    // safe auto-fix here, unlike the two checks above.
    const dangling = await _findDanglingSelectorRefs(agentId, sandboxId);
    if (dangling.length > 0) {
      const danglingNote = `WARNING: code queries selector(s) that don't exist anywhere in the sandbox's markup and are never created dynamically either: ${dangling.map(d => `${d.selector} (in ${d.files.join(', ')})`).join('; ')} -- likely a missing element, not real gameplay yet.`;
      result.note = result.note ? `${result.note} | ${danglingNote}` : danglingNote;
    }
  }
  return result;
}

// Pulls every .js filename a heredoc-based shell command actually wrote
// (`cat > name.js` or `cat >> name.js`) -- deliberately simple pattern
// matching over the command text itself, not a filesystem diff, since the
// command is already the single source of truth for what this step
// intended to create.
function _extractWrittenJsFiles(command) {
  const files = new Set();
  const re = /cat\s*>>?\s*([A-Za-z0-9_.\-]+\.js)\b/g;
  let m;
  while ((m = re.exec(command))) files.add(m[1]);
  return [...files];
}

// A direct, mechanical grep against every real *.html file in the
// sandbox -- not a guess, not an LLM judgment call, and (per the real
// miss this closes) not just index.html either: a project can have more
// than one real page (e.g. a settings/remap screen), and a file legitimately
// linked from one of those, not index.html, used to get wrongly flagged
// as unlinked.
async function _findUnlinkedJsFiles(agentId, sandboxId, jsFiles) {
  const checkCommand = jsFiles
    .map(f => `grep -q 'src="${f}"' *.html 2>/dev/null && echo "LINKED:${f}" || echo "UNLINKED:${f}"`)
    .join(' ; ');
  try {
    const res = await agentFetch('/api/execute', agentId, {
      method: 'POST',
      body: JSON.stringify({ agentId, command: checkCommand, purpose: 'Verifying newly written script files are actually linked into index.html.', sandboxId }),
    });
    const data = await res.json();
    const stdout = data.stdout || '';
    return jsFiles.filter(f => stdout.includes(`UNLINKED:${f}`));
  } catch (e) {
    // Can't confirm either way -- treat as unlinked so the follow-up at
    // least attempts a fix rather than silently trusting an unconfirmed file.
    return jsFiles;
  }
}

// Gap found: settings.js got auto-linked into
// index.html by blind default, even though the sandbox already had a
// real settings.html it obviously belonged to instead. Linking it into
// the wrong page doesn't make it work -- it just moves where the dead
// reference lives, and now settings.js's own DOM queries are dangling on
// a page that never has those elements either. Prefer a same-named HTML
// page (settings.js -> settings.html) when one actually exists in the
// sandbox; index.html stays the fallback for anything that isn't
// obviously page-specific.
async function _guessLinkTargetHtml(agentId, sandboxId, jsFile) {
  const base = jsFile.replace(/\.js$/, '');
  if (base === 'index') return 'index.html';
  const checkCommand = `[ -f "${base}.html" ] && echo yes || echo no`;
  try {
    const res = await agentFetch('/api/execute', agentId, {
      method: 'POST',
      body: JSON.stringify({ agentId, command: checkCommand, purpose: `Checking whether ${jsFile} has a same-named HTML page to link into.`, sandboxId }),
    });
    const data = await res.json();
    return (data.stdout || '').includes('yes') ? `${base}.html` : 'index.html';
  } catch (e) {
    return 'index.html';
  }
}

// One-shot, narrowly-scoped follow-up: ask the SAME model, in the SAME
// conversation, for just the missing <script src> line(s) -- not a whole
// new coding task, since the actual file content was already correct.
// Grouped by target page (see _guessLinkTargetHtml) since not every
// unlinked file necessarily belongs on the same page.
async function _autoLinkJsFiles(agentId, sandboxId, unlinkedFiles, messages, tier) {
  const byTarget = new Map(); // html -> [files]
  for (const f of unlinkedFiles) {
    const html = await _guessLinkTargetHtml(agentId, sandboxId, f);
    if (!byTarget.has(html)) byTarget.set(html, []);
    byTarget.get(html).push(f);
  }

  let allOk = true;
  const notes = [];
  for (const [html, files] of byTarget) {
    const prompt = `You just created ${files.join(', ')} but never added a <script src="..."> tag for ${files.length > 1 ? 'them' : 'it'} in ${html}, so ${files.length > 1 ? 'they are' : 'it is'} never actually loaded by the page. `
      + `Respond with ONLY a single shell command, no explanation, that appends the missing <script src> tag(s) to ${html} -- e.g.:\ncat >> ${html} << 'EOF'\n<script src="${files[0]}"></script>\nEOF`;
    try {
      const res = await agentFetch('/api/chat', agentId, {
        method: 'POST',
        body: JSON.stringify({ model: tier.slug, messages: [...messages, { role: 'user', content: prompt }], max_tokens: 400, agentId }),
      });
      const data = await res.json();
      if (!res.ok || data.error || !data.reply) { allOk = false; notes.push(`${html}: follow-up model call failed or returned nothing`); continue; }
      const linkCommand = data.reply.trim().replace(/^```(?:bash|sh)?\s*|\s*```\s*$/g, '');
      const execRes = await agentFetch('/api/execute', agentId, {
        method: 'POST',
        body: JSON.stringify({ agentId, command: linkCommand, purpose: `Linking previously-unlinked file(s) into ${html}: ${files.join(', ')}`, sandboxId }),
      });
      const execData = await execRes.json();
      if (!execData.allowed) { allOk = false; notes.push(`${html}: blocked (${execData.reason || 'no reason given'})`); continue; }
      if (!(execData.exitCode === 0 && !execData.timedOut)) { allOk = false; notes.push(`${html}: exit code ${execData.exitCode}`); }
    } catch (e) {
      allOk = false; notes.push(`${html}: follow-up execute failed: ${e.message}`);
    }
  }
  return { ok: allOk, note: allOk ? [...byTarget.keys()].join(', ') : notes.join('; ') };
}

// The mirror check to _findUnlinkedJsFiles: which real <script src="...">
// references, in ANY *.html file in the sandbox, point at a .js file that
// doesn't actually exist. A direct, mechanical grep+existence-check
// inside the real sandbox -- not a guess, not an LLM judgment call about
// whether a name "sounds real."
//
// Real miss this closes: originally checked index.html only. A rewrite
// task wrote settings.html with <script src="settings.js">, but
// settings.js itself was never actually created -- caught only by
// directly reading the code afterward, not by this check, because it
// never looked past index.html. Every *.html file in the sandbox gets
// the same check now, not just the entry page.
async function _findPhantomScriptRefs(agentId, sandboxId) {
  const checkCommand = `for html in *.html; do [ -f "$html" ] || continue; for f in $(grep -oE 'src="[^"]+\\.js"' "$html" | sed -E 's/src="//;s/"$//' | sort -u); do [ -f "$f" ] && echo "EXISTS:$html:$f" || echo "PHANTOM:$html:$f"; done; done`;
  try {
    const res = await agentFetch('/api/execute', agentId, {
      method: 'POST',
      body: JSON.stringify({ agentId, command: checkCommand, purpose: 'Verifying every <script src> reference in every HTML file actually corresponds to a real file.', sandboxId }),
    });
    const data = await res.json();
    const stdout = data.stdout || '';
    return [...stdout.matchAll(/PHANTOM:([^:\n]+):(\S+)/g)].map(m => ({ html: m[1], file: m[2] }));
  } catch (e) {
    return []; // can't confirm -- don't guess-remove something that might be real
  }
}

// Removes phantom <script src> lines outright rather than asking the
// model to reconcile them -- the safest mechanical action available.
// Writing the referenced file for real isn't the right call either: the
// filename was fabricated, not a real, deferred piece of work, so
// "finishing" it would just be inventing content to match an invented
// name. A dangling reference to a file that will never exist is strictly
// worse (a silent 404 in a real browser) than no reference at all.
// Grouped by which HTML file each phantom reference actually came from --
// a project can have more than one real page, and cleaning the wrong one
// (or only ever index.html) would silently leave the others broken.
async function _removePhantomScriptRefs(agentId, sandboxId, phantomRefs) {
  const byHtml = new Map();
  for (const { html, file } of phantomRefs) {
    if (!byHtml.has(html)) byHtml.set(html, []);
    byHtml.get(html).push(file);
  }
  let allOk = true;
  const notes = [];
  for (const [html, files] of byHtml) {
    const excludes = files.map(f => `-e 'src="${f.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"'`).join(' ');
    const cleanupCommand = `grep -v ${excludes} "${html}" > "${html}.__cleanup_tmp" && mv "${html}.__cleanup_tmp" "${html}"`;
    try {
      const res = await agentFetch('/api/execute', agentId, {
        method: 'POST',
        body: JSON.stringify({ agentId, command: cleanupCommand, purpose: `Removing phantom <script src> reference(s) in ${html} to nonexistent file(s): ${files.join(', ')}`, sandboxId }),
      });
      const data = await res.json();
      if (!data.allowed) { allOk = false; notes.push(`${html}: blocked (${data.reason || 'no reason given'})`); continue; }
      if (!(data.exitCode === 0 && !data.timedOut)) { allOk = false; notes.push(`${html}: exit code ${data.exitCode}`); }
    } catch (e) {
      allOk = false; notes.push(`${html}: cleanup execute failed: ${e.message}`);
    }
  }
  return { ok: allOk, note: notes.length ? notes.join('; ') : 'removed' };
}

// A different class of dangling reference than the phantom-script check
// above: not "this file doesn't exist" but "this JS queries a class/id
// that nothing in the sandbox's markup ever creates, and nothing in the
// JS creates dynamically either." Real miss this closes: the finger-
// drums rewrite's app.js built and positioned falling-note elements
// against `.highway`/`.hit-line`, and styles.css had real CSS rules for
// both -- but no HTML file ever actually placed those elements in the
// page. Three individually-plausible files, never cross-checked against
// each other. Deliberately advisory, not auto-fixed like the phantom-
// script check: there's no single safe mechanical fix here (the right
// answer might be "add the missing element" or "this JS is dead, remove
// it," which needs real judgment), so this only surfaces the finding.
// Static and heuristic -- only simple `.class`/`#id`/getElementById
// forms are checked, and only flagged when the selector appears NOWHERE
// at all, neither in markup nor created dynamically, which is
// unambiguous regardless of when or how the page actually runs.
async function _findDanglingSelectorRefs(agentId, sandboxId) {
  const readCommand = `for f in *.html *.js; do [ -f "$f" ] || continue; echo "--- $f ---"; cat "$f"; done`;
  let blob;
  try {
    const res = await agentFetch('/api/execute', agentId, {
      method: 'POST',
      body: JSON.stringify({ agentId, command: readCommand, purpose: 'Checking for JS selectors with no matching markup or dynamic creation anywhere in the sandbox.', sandboxId }),
    });
    const data = await res.json();
    if (!data.allowed || !data.stdout) return [];
    blob = data.stdout;
  } catch (e) {
    return []; // can't confirm either way -- don't flag on a read failure
  }

  const files = {};
  let current = null;
  for (const line of blob.split('\n')) {
    const m = line.match(/^--- (.+) ---$/);
    if (m) { current = m[1]; files[current] = []; continue; }
    if (current) files[current].push(line);
  }

  const htmlText = Object.entries(files).filter(([f]) => f.endsWith('.html')).map(([, lines]) => lines.join('\n')).join('\n');
  const jsEntries = Object.entries(files).filter(([f]) => f.endsWith('.js'));
  const jsText = jsEntries.map(([, lines]) => lines.join('\n')).join('\n');

  const queried = new Map(); // "kind:name" -> {name, kind, files: Set}
  const noteQuery = (name, kind, file) => {
    const key = `${kind}:${name}`;
    if (!queried.has(key)) queried.set(key, { name, kind, files: new Set() });
    queried.get(key).files.add(file);
  };
  for (const [file, lines] of jsEntries) {
    const text = lines.join('\n');
    for (const m of text.matchAll(/\.querySelector(?:All)?\(\s*['"]([.#][A-Za-z0-9_-]+)['"]/g)) {
      const raw = m[1];
      noteQuery(raw.slice(1), raw[0] === '.' ? 'class' : 'id', file);
    }
    for (const m of text.matchAll(/getElementById\(\s*['"]([A-Za-z0-9_-]+)['"]/g)) {
      noteQuery(m[1], 'id', file);
    }
  }

  const dangling = [];
  for (const { name, kind, files: fileSet } of queried.values()) {
    const attr = kind === 'class' ? 'class' : 'id';
    const inMarkup = new RegExp(`${attr}="[^"]*\\b${name}\\b[^"]*"`).test(htmlText);
    if (inMarkup) continue;
    const createdDynamically = kind === 'class'
      ? new RegExp(`classList\\.add\\([^)]*['"]${name}['"]|className\\s*=\\s*['"][^'"]*\\b${name}\\b`).test(jsText)
      : new RegExp(`\\.id\\s*=\\s*['"]${name}['"]|setAttribute\\(\\s*['"]id['"]\\s*,\\s*['"]${name}['"]`).test(jsText);
    if (createdDynamically) continue;
    dangling.push({ selector: (kind === 'class' ? '.' : '#') + name, files: [...fileSet] });
  }
  return dangling;
}

// Real, generally-delegatable review/QA pass: gatherUnifiedContext
// (sandbox-agnostic), a skeptical critique call, a real screenshot visual
// pass, and a Jev actionable/clean verdict that queues a real follow-up
// fix. Escalation queues a plain WORK_QUEUE item with the same
// goal/projectLabel -- there's no fixed "the developers" role list for an
// arbitrary delegated project, so whoever's next free under that goal
// picks it up, the same mechanism every other generalized subtask uses.
// runCodingTask (above) lets the model request a runtime probe mid-task via
// a probeRequest JSON reply, parsed by _parseProbeRequest and run through
// requestPageProbe/formatPageProbeResult -- project-agnostic. Reviews reuse
// that same mechanism so a reviewer checks what the page actually DOES, not
// just what the source says. Capped a bit tighter than
// coding's MAX_CODE_PROBE_ROUNDS (3): reviewing is diagnostic, not
// iterative building, so there's less reason for a long back-and-forth.
const MAX_REVIEW_PROBE_ROUNDS = 2;

async function runReviewTask(agentId, task, projectLabel) {
  const a = AGENTS[agentId];
  const isQA = task.taskType === 'qa';
  const backlogItem = task.instructions ? `${task.title} -- ${task.instructions}` : task.title;

  // Produce the first review once here; the graded revision loop reuses it
  // for round 1 and only re-produces (via _produceReviewText) after an
  // inline revision.
  const fullReview = await _produceReviewText(agentId, task, projectLabel, a, isQA, backlogItem, null);
  if (!fullReview) return { ok: false, note: `Tried to ${isQA ? 'QA-test' : 'review'} "${task.title}", but the model call didn't produce anything usable.` };

  await writeLibraryFile(agentId, `archive/${Date.now()}-${task.taskType}-${task.id || 'adhoc'}.md`, `# ${task.title}\n\nProject: ${projectLabel}\nBy: ${a.name} (${isQA ? 'QA' : 'review'})\n\n${fullReview}\n`, 'firsthand');

  // Phase G -- the self-improving revision loop. When this review's task
  // carries a checklist (a big-task deliverable produced under one),
  // grade the deliverable requirement-by-requirement and let a failed
  // check drive an inline revision of the SAME project, re-grading after
  // each round (the article's create -> evaluate -> revise -> evaluate
  // again), bounded by MAX_REVISION_ROUNDS. An uncertain grade, a
  // human-type requirement, or an exhausted revision budget escalates to
  // the player instead of guessing or looping forever. Reviews with NO
  // checklist (ambient, ad-hoc, or pre-checklist big tasks) keep the old
  // single actionable/clean verdict and queue a fix -- unchanged.
  const checklist = task.checklist || [];
  if (checklist.length === 0) {
    const verdict = (await requestJevChoice(
      `A ${isQA ? 'QA/playtester' : 'reviewer'} just wrote this about "${projectLabel}": "${fullReview.slice(0, 1500)}" Does this identify at least one concrete, real, fixable problem (a bug, missing feature, broken integration)? Or does it say things genuinely look solid with nothing actionable?`,
      [
        { id: 'actionable', description: 'Yes -- it names at least one real, specific problem that should be fixed.' },
        { id: 'clean', description: 'No -- it says things look solid, or only raises vague/subjective suggestions, nothing a developer needs to act on.' },
      ]
    ))?.choice;
    if (verdict === 'actionable') {
      queueWork([{
        title: `Fix issues found in ${isQA ? 'QA testing' : 'review'} of "${task.title}"`,
        room: 'pressoffice',
        instructions: `A ${isQA ? 'QA pass' : 'review'} of "${projectLabel}" found real problems -- see the Library entry just filed for "${task.title}" -- fix them. Build on the existing files.`,
        goal: projectLabel,
      }]);
    }
    return { ok: true, note: `Filed a ${isQA ? 'QA pass' : 'review'} on "${task.title}" (text + visual)${verdict === 'actionable' ? ', queued a fix' : ', nothing actionable found'}.` };
  }

  // Checklist present: run the graded revision loop over this task's own
  // revision budget. Each round grades a review, and if any requirement
  // still fails with budget left, the shared sandbox is revised and the
  // review re-run. The first review (above) seeds round 1; only later
  // rounds pay another model call. Returns {ok, note}.
  task.revisions = task.revisions || 0;
  return await runGradedReviewLoop(agentId, task, projectLabel, isQA, {
    produce: async (reviseBrief) => {
      // Fresh review AFTER an inline revision re-checks exactly the
      // requirements that previously failed. Context is re-gathered inside
      // _produceReviewText so the reviewer sees the revised state.
      const brief = reviseBrief
        ? `${backlogItem}\n\nRevision round: these requirements previously failed -- confirm they are actually fixed, and flag anything still wrong:\n${reviseBrief}`
        : backlogItem;
      return await _produceReviewText(agentId, task, projectLabel, a, isQA, brief, null);
    },
    firstReview: fullReview,
  });
}

// Produce the review/QA text for the current state of the shared sandbox
// -- the probe + screenshot + critique pass that used to live inline in
// runReviewTask. Refactored out so the graded revision loop (G3) can call
// it once per round against the freshly-revised files.
async function _produceReviewText(agentId, task, projectLabel, a, isQA, brief, context) {
  const tier = await pickModelTierForAction(a, `${isQA ? 'QA/playtest' : 'review'} real code for: ${task.title}`);
  // Context is gathered fresh on every round so the reviewer sees the
  // CURRENT (possibly just-revised) state, not a snapshot from round 1.
  if (context === null) context = await gatherUnifiedContext(agentId, WORKROOM_SANDBOX_ID, task.title);
  let systemPrompt = (isQA
    ? `You are ${a.name}, QA-testing a teammate's real work for a project: ${projectLabel}. Current code/state:\n${context}\n\nTask: ${brief}\n\n`
      + `Assess, as a real playtester/QA would, whether this is actually usable end to end for its intended purpose -- not just "a file that sounds related exists." Call out anything broken, missing, or incomplete, as specifically as you can. Be honest either way.`
    : `You are ${a.name}, reviewing a teammate's real code for a project: ${projectLabel}. Be a genuine, skeptical reviewer, not a rubber stamp. Current code:\n${context}\n\nTask: ${brief}\n\n`
      + `List real, specific problems you actually see (bugs, broken logic, missing pieces), or say plainly if it genuinely looks solid. Be honest either way.`)
    + ` Before answering, you may check real facts about how the ACTUAL running page behaves right now -- what a button click or keypress actually does -- instead of guessing from the source alone. `
    + `To do this, respond with ONLY a JSON object, no markdown fences, no explanation, in exactly this shape: `
    + `{"probeRequest": {"path": "index.html", "actions": [{"type":"click","selector":"text=Some Button"}, {"type":"keydown","key":"a"}], "probes": ["document.body.className", "typeof window.SomeGlobal"]}}\n`
    + `Action types are click ({selector}), keydown ({key}), wait ({ms}), eval ({code}). You can do this up to ${MAX_REVIEW_PROBE_ROUNDS} times if you genuinely need to. `
    + `When ready, respond with your final assessment as plain text, not JSON.`;

  const messages = [{ role: 'system', content: systemPrompt }, { role: 'user', content: 'Give your assessment.' }];
  let review = null;
  let probeRounds = 0;
  while (true) {
    let reply;
    try {
      const res = await agentFetch('/api/chat', agentId, {
        method: 'POST',
        body: JSON.stringify({ model: tier.slug, messages, max_tokens: 900, agentId }),
      });
      const data = await res.json();
      if (!res.ok || data.error || !data.reply) break;
      reply = data.reply.trim();
    } catch (e) { break; }

    const probeReq = probeRounds < MAX_REVIEW_PROBE_ROUNDS ? _parseProbeRequest(reply) : null;
    if (probeReq) {
      probeRounds++;
      messages.push({ role: 'assistant', content: reply });
      const probeData = await requestPageProbe(agentId, WORKROOM_SANDBOX_ID, probeReq.path, probeReq.actions, probeReq.probes);
      let feedback = formatPageProbeResult(probeData);
      if (probeRounds >= MAX_REVIEW_PROBE_ROUNDS) {
        feedback += `\n\nYou have used all ${MAX_REVIEW_PROBE_ROUNDS} probe rounds. Respond now with your final assessment as plain text.`;
      }
      messages.push({ role: 'user', content: feedback });
      continue;
    }
    review = reply;
    break;
  }
  if (!review) return null;

  const visual = await reviewScreenshot(
    agentId, WORKROOM_SANDBOX_ID, 'index.html',
    `You're reviewing a real screenshot of this project's main page. Task: ${brief}. Describe what you actually see, and call out anything that looks visually broken -- elements in the wrong place, overlapping, cut off, or missing.`
  );
  return visual.ok
    ? `${review}\n\n## Visual check (real screenshot)\n\n${visual.review}`
    : `${review}\n\n## Visual check\n\n(could not complete: ${visual.note})`;
}

// Post a review requirement the loop can't resolve back to the player.
// Reuses serve.py's escalation email + resolve-link channel (the same one
// every sandbox/execution gate uses to surface a risky decision), so an
// uncertain or budget-exhausted requirement reaches the human as an
// actionable "here's what needs you" rather than being silently dropped
// or auto-decided.
async function escalateReviewRequirement(agentId, kind, question) {
  try {
    await agentFetch('/api/review/escalate', agentId, {
      method: 'POST',
      body: JSON.stringify({ agentId, kind, question }),
    });
  } catch (e) { /* email channel is best-effort -- never let escalation failure crash the review */ }
}

// Phase G -- the "working guide": the think tank's durable lessons file that
// every task reads at start so each correction is useful to the NEXT piece
// (the article's accumulated-lessons memory). Read by all agents; written
// only by directors/admin -- serve.py enforces that ACL server-side for the
// working-guide.md path specifically (see write_library_file), so a lay
// agent can never accidentally rewrite shared work-guiding knowledge.
// A missing or unreadable guide is not an error -- it simply contributes
// nothing to the prompt.
async function readWorkingGuide() {
  try {
    const content = await readLibraryFile('working-guide.md');
    return content ? content.trim() : null;
  } catch (e) { return null; }
}

// Prepend the working guide (when one exists) into a task-start system
// prompt. Callers build their prompt first, then wrap: the guide is placed
// ABOVE the task so the agent reads it as durable standing guidance.
function prependWorkingGuide(prompt, guide) {
  if (!guide) return prompt;
  return `You carry the think tank's working guide -- hard-won lessons from past work. Follow them in ADDITION to your current task:\n\n${guide}\n\n----\n\n${prompt}`;
}

// Director/admin-led helper to append a single, GENERAL lesson to the
// working guide (e.g. one distilled from a player rejection or a revision
// loop that kept failing). "General" is a discipline, enforced by whoever
// calls this -- this is the article's "separate general rules from changes
// specific to this piece." The write is fire-and-forget like every library
// write; serve.py rejects it silently if the caller doesn't hold director
// tier. Appending (never clobbering) keeps prior lessons intact.
async function appendWorkingGuide(agentId, lesson) {
  const guide = await readWorkingGuide();
  const block = guide
    ? `${guide}\n\n## Lesson\n\n${lesson}`
    : `# Working guide\n\nEvery task reads this before it starts. Each lesson is a GENERAL rule distilled from real past work -- not a per-piece fix.\n\n## Lesson\n\n${lesson}`;
  await writeLibraryFile(agentId, 'working-guide.md', block);
}

// The bounded graded revision loop (Phase G). Grades a task's checklist
// against the produced review, and when a requirement fails with revision
// budget left, revises the shared sandbox and re-reviews. Surfaces
// human-type and uncertain requirements to the player; stops auto-revising
// once MAX_REVISION_ROUNDS is exhausted. `produce` is injected so tests
// can drive the loop without a live browser -- grading.js keeps the pure
// per-requirement logic, this keeps the orchestration.
async function runGradedReviewLoop(agentId, task, projectLabel, isQA, hooks) {
  const checklist = task.checklist || [];
  let revisionBrief = null;
  let round = 0;
  let escalated = [];

  while (true) {
    round++;
    // Round 1 reuses the review runReviewTask already produced (no extra
    // model call). Only later rounds -- after an inline revision -- pay a
    // fresh review of the revised state.
    const review = round === 1 ? hooks.firstReview : await hooks.produce(revisionBrief);
    if (!review) return { ok: false, note: `Tried to ${isQA ? 'QA-test' : 'review'} "${task.title}", but the model call didn't produce anything usable.` };

    // Grade every checklist requirement in one pass.
    const jevGrades = await gradeJevRequirements(checklist, review, agentId);
    const codeGrades = [];
    for (const req of checklist) {
      if (req.type === 'code') codeGrades.push(gradeCodeRequirement(req, review));
    }
    const grades = jevGrades.concat(codeGrades);

    // Partition: what must reach the player vs. what triggers a revision.
    const failing = grades.filter(g => g.verdict === GRADE_FAILS);
    const unsure = grades.filter(g => g.verdict === GRADE_UNSURE);
    const humanReqs = checklist.filter(r => r.type === 'human');

    if (failing.length === 0 && unsure.length === 0 && humanReqs.length === 0) {
      return { ok: true, note: `${isQA ? 'QA pass' : 'review'} on "${task.title}" -- all ${checklist.length} checklist requirements met in round ${round}.`, grades, escalated };
    }

    // If we can still revise AND there are concrete failures to fix, revise.
    if (failing.length > 0 && task.revisions < MAX_REVISION_ROUNDS) {
      const reqText = failing.map(f => `- ${f.section}: ${checklist.find(r => r.id === f.requirementId)?.question || f.requirementId}`).join('\n');
      await writeLibraryFile(agentId, `archive/${Date.now()}-revision-${task.id || 'adhoc'}.md`,
        `# Revision round ${task.revisions + 1} -- ${task.title}\n\nProject: ${projectLabel}\n\nFailed requirements:\n${reqText}\n`, 'firsthand');
      let ctxSummary = '';
      try {
        const lsRes = await agentFetch('/api/execute', agentId, {
          method: 'POST',
          body: JSON.stringify({ agentId, sandboxId: WORKROOM_SANDBOX_ID, command: 'ls -la' }),
        });
        const lsData = await lsRes.json();
        if (lsData.allowed && lsData.stdout) ctxSummary = `Current files in the shared Work Room sandbox:\n${lsData.stdout}`;
      } catch (e) { /* fallbacks below cover an empty summary */ }
      const reviseBrief = `A ${isQA ? 'QA pass' : 'review'} of "${projectLabel}" found these requirements failing. Fix EACH of them, building on the existing files (do not start over or invent capabilities to pass the check):\n${reqText}`;
      await runCodingTask(agentId, WORKROOM_SANDBOX_ID, reviseBrief, ctxSummary, projectLabel);
      task.revisions++;
      revisionBrief = `Requirements that still need attention (re-review the fixed work):\n${reqText}`;
      // Loop back to produce() a fresh review against the revised files and re-grade.
      continue;
    }

    // Not revisable (no concrete failure, or budget exhausted): surface the
    // still-open requirements to the player and stop looping.
    await writeLibraryFile(agentId, `archive/${Date.now()}-${task.taskType}-${task.id || 'adhoc'}.md`,
      `# ${isQA ? 'QA pass' : 'review'} on "${task.title}" (final)\n\nProject: ${projectLabel}\n\n${review}\n`, 'firsthand');
    for (const g of unsure) {
      escalated.push(g.requirementId);
      await escalateReviewRequirement(agentId, 'uncertain review requirement',
        `Could not confidently judge requirement "${checklist.find(r => r.id === g.requirementId)?.question || g.requirementId}" (confidence ${g.confidence !== null ? g.confidence.toFixed(2) : 'n/a'}). Resolve it: ${g.section}`);
    }
    for (const r of humanReqs) {
      escalated.push(r.id);
      await escalateReviewRequirement(agentId, 'review decision for you', `${r.question} (${r.section})`);
    }
    const budgetNote = task.revisions >= MAX_REVISION_ROUNDS && failing.length > 0
      ? ` -- reached the ${MAX_REVISION_ROUNDS}-round revision cap, ${failing.length} requirement(s) still failing`
      : '';
    return { ok: true, note: `${isQA ? 'QA pass' : 'review'} on "${task.title}" finished after ${task.revisions} revision(s)${budgetNote}${escalated.length ? '; escalated to the player: ' + escalated.join(', ') : ''}.`, grades, escalated };
  }
}

// Fix (hiring-to-shutdown audit): this used to run the
// exact SAME fixed helper.py health-check every single time, completely
// ignoring whatever the assigned task actually said -- confirmed,
// a real coding subtask generated from assignBigTask (e.g. "build a
// simple to-do list page") arrived here and produced nothing resembling
// it, because nothing about the task's own title/instructions was ever
// read. Uses the SAME shared sandbox every time (WORKROOM_SANDBOX_ID,
// index.html) -- agents should reuse it, not each get a
// throwaway one, so real work persists across visits. When the task
// actually came from a real project (projectLabel set -- see
// assignBigTask), this now calls the same real coding pipeline
// runCodingTask() uses (probe-before-writing, heredoc-continuation
// handling, orphaned-file detection), with a real listing of the sandbox's
// own current files as context instead of a guessed one. Falls back to the
// original fixed health-check only for a bare ambient task with no real
// project lineage at all. A
// taskType of "review" or "qa" instead runs runReviewTask (above) --
// this dispatch only ever writes/edits code.
async function runWorkroomTask(agentId, task) {
  const a = AGENTS[agentId];
  if (!a) return;
  const projectLabel = task && task.projectLabel;
  let note;
  if (projectLabel && (task.taskType === 'review' || task.taskType === 'qa')) {
    // A real review/QA pass, generally delegatable through assignBigTask --
    // see runReviewTask below.
    const result = await runReviewTask(agentId, task, projectLabel);
    note = result.note;
  } else if (projectLabel) {
    const backlogItem = task.instructions ? `${task.title} -- ${task.instructions}` : task.title;
    let contextSummary = '';
    try {
      const lsRes = await agentFetch('/api/execute', agentId, {
        method: 'POST',
        body: JSON.stringify({ agentId, sandboxId: WORKROOM_SANDBOX_ID, command: 'ls -la' }),
      });
      const lsData = await lsRes.json();
      if (lsData.allowed && lsData.stdout) contextSummary = `Current files in the shared Work Room sandbox:\n${lsData.stdout}`;
    } catch (e) { /* fine without it -- runCodingTask's own fallback text covers an empty summary */ }

    const result = await runCodingTask(agentId, WORKROOM_SANDBOX_ID, backlogItem, contextSummary, projectLabel);
    note = result.ok
      ? `Worked in the shared Work Room sandbox on: ${task.title}.`
      : `Tried to work on "${task.title}" in the Work Room, but ${result.note || 'the attempt did not succeed'}.`;
  } else {
    try {
      const res = await agentFetch('/api/pipeline', agentId, {
        method: 'POST',
        body: JSON.stringify({
          agentId,
          sandboxId: WORKROOM_SANDBOX_ID,
          steps: [
            { name: 'update helper script', command: "echo 'print(\"helper script checked and working\")' > helper.py" },
            { name: 'run it', command: 'python3 helper.py' },
          ],
        }),
      });
      const data = await res.json();
      note = data.failedStep
        ? `Worked in the shared sandbox -- hit a failure at "${data.failedStep}".`
        : 'Checked and ran the shared tooling in the Work Room -- all good.';
    } catch (e) {
      note = 'Tried to work in the Work Room sandbox, but the request failed.';
    }
  }
  if (AGENTS[agentId]) {
    AGENTS[agentId].profile.notes.push(note);
    if (AGENTS[agentId].profile.notes.length > 5) AGENTS[agentId].profile.notes.shift();
  }
}

// "A research agent will need to review the skill
// files and determine which are relevant and which should stick." Lists
// the real Library (listLibraryFile, world.js) rather than trusting any
// in-memory record of what's pending -- pending_review/skills/*.md is the
// one real source of truth for what's actually waiting, regardless of
// which task or topic wrote it. Capped at 5 per sweep, same "bound one
// pass's cost" reasoning as crawlAndCollect's own maxPages -- a real
// backlog drains over several ticks rather than one unbounded burst.
// Promote/reject reuse the exact real judgment-call shape runReviewTask
// already uses (a Jev pick between two named outcomes over real content),
// just a different question.
const SKILL_REVIEW_MAX_PER_SWEEP = 5;

async function runSkillReviewTask(agentId) {
  const files = await listLibraryFiles();
  const pending = files.filter(f => f.path.startsWith('pending_review/skills/')).slice(0, SKILL_REVIEW_MAX_PER_SWEEP);
  if (pending.length === 0) return 'Checked for pending skill files -- nothing waiting right now.';

  let kept = 0, rejected = 0;
  for (const f of pending) {
    const content = await readLibraryFile(f.path);
    if (!content) continue; // genuinely unreadable -- leave it for a later sweep rather than guessing
    const verdict = (await requestJevChoice(
      `A candidate skill-reference file is waiting for review. Its content: "${content.slice(0, 1500)}" Is this accurate and genuinely useful as real reference material for future work, or should it be discarded?`,
      [
        { id: 'keep', description: 'Yes -- accurate and specific enough to be worth keeping as real reference material.' },
        { id: 'reject', description: 'No -- inaccurate, too vague/generic to be useful, or not actually relevant to real work here.' },
      ]
    ))?.choice;
    if (verdict === 'reject') {
      await writeLibraryFile(agentId, f.path, `${content}\n\n## Rejected\n\nReviewed and rejected on ${new Date().toISOString()} -- judged not accurate/relevant enough to keep as trusted reference material.\n`, 'firsthand');
      await rejectLibraryFile(agentId, f.path);
      rejected++;
    } else {
      await promoteLibraryFile(agentId, f.path);
      kept++;
    }
  }
  return `Reviewed ${pending.length} pending skill file(s): ${kept} promoted, ${rejected} rejected.`;
}

// Fix (hiring-to-shutdown audit): this used to run the
// exact SAME fixed log-and-tally action every time, regardless of what
// the assigned task actually asked for -- same gap class as
// runWorkroomTask's own fix. checkWeatherReference (weatherstation) is
// deliberately left as-is: that room's whole identity is one specific
// external reference, not a general-purpose room the way Research
// Center is framed to be. Uses the SAME persistent sandbox every time
// (RESEARCH_SANDBOX_ID) -- agents should reuse it, not
// each get a throwaway one. When the task has real project lineage
// (projectLabel set), this now makes a real model call reasoning about
// the SPECIFIC subtask and writes a genuine finding -- to findings.log
// via a quoted heredoc (never a raw string interpolated into a shell
// command; a heredoc with a quoted delimiter is immune to whatever
// quotes/`$()`/backticks the model's own prose happens to contain,
// same safe-write convention runCodingTask's own system prompt already
// teaches agents to use) and to the Library, so it's a real, findable
// record, not just a private note. Falls back to the original fixed
// log-and-tally only for a bare ambient task with no real project
// lineage at all.
async function runResearchTask(agentId, task) {
  const a = AGENTS[agentId];
  if (!a) return;
  const projectLabel = task && task.projectLabel;
  let note;
  if (task && task.skillReview) {
    // The skill-curation sweep -- see runSkillReviewTask
    // below. Checked ahead of task.research: this is its own standing
    // job, unrelated to any specific research topic.
    note = await runSkillReviewTask(agentId);
  } else if (task && task.research) {
    // The scheduled-topic path: crawlAndCollect/writeSkillFile/
    // readLibraryFile/skillSlug/SKILL_FILE_FORMAT_GUIDE all live in
    // world.js, loaded before tasks.js (index.html) -- real globals by the
    // time this ever runs, not a forward reference.
    const topic = RESEARCH_TOPICS.find(t => t.id === task.research.topicId);
    if (!topic) {
      note = `Scheduled research arrived with no matching topic record (it may have been removed) -- nothing to do.`;
    } else {
      const crawl = await crawlAndCollect(agentId, RESEARCH_SANDBOX_ID, topic.startUrl, `scheduled research: ${topic.topic}`, {
        maxPages: 6, linkKeyword: topic.linkKeyword, pageKeyword: topic.pageKeyword, skipUrls: topic.seenUrls,
        since: task.research.since || 0,
      });
      if (crawl.pagesKept === 0) {
        note = `Ran scheduled research for "${topic.topic}" -- nothing new since last time (checked ${crawl.pagesVisited} page(s)).`;
      } else {
        for (const p of crawl.pages) {
          if (!topic.seenUrls.includes(p.url)) topic.seenUrls.push(p.url);
        }
        const existingContent = await readLibraryFile(`skills/${skillSlug(topic.topic)}.md`);
        const tier = await pickModelTierForAction(a, `update the "${topic.topic}" skill file from freshly collected research`);
        const sourcesText = crawl.pages.map(p => `### ${p.url}\n${(p.text || '').slice(0, 3000)}`).join('\n\n');
        let updated = null;
        try {
          const res = await agentFetch('/api/chat', agentId, {
            method: 'POST',
            body: JSON.stringify({
              model: tier.slug,
              messages: [
                {
                  role: 'system',
                  content: `You are ${a.name}, updating a real skill-reference file for "${topic.topic}". ${SKILL_FILE_FORMAT_GUIDE} `
                    + (existingContent
                      ? `Here is the EXISTING skill file -- preserve what still holds, update what changed, add what is genuinely new:\n\n${existingContent.slice(0, 6000)}`
                      : `No existing skill file yet -- write one from scratch.`),
                },
                { role: 'user', content: `Freshly collected sources:\n\n${sourcesText}` },
              ],
              max_tokens: 900,
              agentId,
            }),
          });
          const data = await res.json();
          if (res.ok && !data.error && data.reply) updated = data.reply.trim();
        } catch (e) { /* falls through to the failure note below */ }

        if (updated) {
          await writeSkillFile(agentId, topic.topic, updated, 'external');
          note = `Ran scheduled research for "${topic.topic}" -- collected ${crawl.pagesKept} new page(s) and wrote an updated skill file (pending review).`;
        } else {
          note = `Collected ${crawl.pagesKept} new page(s) for "${topic.topic}", but the skill-file synthesis call didn't produce anything usable.`;
        }
      }
    }
  } else if (projectLabel) {
    const backlogItem = task.instructions ? `${task.title} -- ${task.instructions}` : task.title;
    const tier = await pickModelTierForAction(a, `research: ${backlogItem}`);
    let finding = null;
    try {
      const res = await agentFetch('/api/chat', agentId, {
        method: 'POST',
        body: JSON.stringify({
          model: tier.slug,
          messages: [
            { role: 'system', content: `You are ${a.name}, researching for a real project: ${projectLabel}. Your specific task right now: ${backlogItem}. Write 2-4 honest, concrete sentences of real findings or analysis -- no filler like "I will research this," an actual answer or set of concrete points.` },
            { role: 'user', content: 'Go ahead.' },
          ],
          max_tokens: 400,
          agentId,
        }),
      });
      const data = await res.json();
      if (res.ok && !data.error && data.reply) finding = data.reply.trim();
    } catch (e) { /* falls through to the failure note below */ }

    if (finding) {
      try {
        // The real timestamp is computed here, in JS, and embedded as
        // plain literal text -- NOT left as a shell $(date) inside the
        // heredoc body, which a quoted delimiter (below) would write out
        // as the literal four characters "$(date)" instead of actually
        // running it. The quoted delimiter itself is what keeps the
        // MODEL's own finding text safe regardless of quotes/`$()`/
        // backticks it happens to contain.
        await agentFetch('/api/pipeline', agentId, {
          method: 'POST',
          body: JSON.stringify({
            agentId,
            sandboxId: RESEARCH_SANDBOX_ID,
            steps: [{ name: 'log this finding', command: `cat >> findings.log << 'FINDING_EOF'\n${new Date().toISOString()}: ${task.title}\n${finding}\nFINDING_EOF` }],
          }),
        });
      } catch (e) { /* the Library write below still records it even if this fails */ }
      writeLibraryFile(agentId, `archive/${Date.now()}-research-${task.id || 'adhoc'}.md`, `# ${task.title}\n\nProject: ${projectLabel}\nBy: ${a.name}\n\n${finding}\n`);
      note = `Researched "${task.title}" for real and logged the finding.`;
    } else {
      note = `Tried to research "${task.title}", but the model call didn't produce anything usable.`;
    }
  } else {
    try {
      const res = await agentFetch('/api/pipeline', agentId, {
        method: 'POST',
        body: JSON.stringify({
          agentId,
          sandboxId: RESEARCH_SANDBOX_ID,
          steps: [
            { name: 'log this pass', command: "echo \"$(date): reviewed findings\" >> findings.log" },
            { name: 'tally findings so far', command: 'wc -l findings.log' },
          ],
        }),
      });
      const data = await res.json();
      note = data.failedStep
        ? `Worked in the Research Center sandbox -- hit a failure at "${data.failedStep}".`
        : 'Reviewed and logged this week\'s findings in the Research Center.';
    } catch (e) {
      note = 'Tried to work in the Research Center sandbox, but the request failed.';
    }
  }
  if (AGENTS[agentId]) {
    AGENTS[agentId].profile.notes.push(note);
    if (AGENTS[agentId].profile.notes.length > 5) AGENTS[agentId].profile.notes.shift();
  }
}

// A fixed, stable reference page rather than letting an agent (or its
// Live weather for the think tank's configured location, served by the
// server's /api/weather/now (real Open-Meteo data for WEATHER_LOCATION) --
// no hard-coded forecast or fixed reference page.
async function checkWeatherReference(agentId) {
  const a = AGENTS[agentId];
  if (!a) return;
  let note;
  try {
    const res = await agentFetch('/api/weather/now', agentId, { method: 'GET' });
    const data = await res.json();
    if (data.reading) {
      note = `Logged live weather for ${data.location}: "${data.reading.slice(0, 140).trim()}..."`;
    } else if (data.error) {
      note = `Tried to log live weather, but the server said: ${data.error}`;
    } else {
      note = 'Tried to log live weather, but the reading came back empty.';
    }
  } catch (e) {
    note = 'Tried to log live weather, but the request failed.';
  }
  // a may no longer be busy/on this task by the time this resolves (fired
  // outside the setTimeout that gates finishTask) -- still worth logging
  // even so, same as any other note.
  if (AGENTS[agentId]) {
    AGENTS[agentId].profile.notes.push(note);
    if (AGENTS[agentId].profile.notes.length > 5) AGENTS[agentId].profile.notes.shift();
  }
}

// Studio's real capability: you tell it what outside
// sources to watch (media/feeds.md, edited via the same Library UI as
// anything else -- no new config surface needed), and an agent assigned
// here fetches one, boils it down to a few honest sentences, and files the
// result at media/digests/ where the Library already makes it browsable.
// One line per feed in media/feeds.md, "url -- why it matters" or just a
// bare URL; blank lines and anything starting with # are ignored, so the
// player can leave themselves comments in the same file.
// NOTE: this only ever sees whatever a plain text fetch of the page
// returns -- there is no video-transcription capability here. A YouTube
// URL will get back page metadata, not the spoken content of the video.
const MEDIA_FEEDS_PATH = 'media/feeds.md';

function parseFeedUrls(text) {
  return text.split('\n')
    .map(l => l.trim())
    .filter(l => l && !l.startsWith('#'))
    .map(l => (l.match(/https?:\/\/\S+/) || [])[0])
    .filter(Boolean);
}

async function runMediaDigestTask(agentId) {
  const a = AGENTS[agentId];
  if (!a) return;
  let note;
  try {
    const feedsRes = await agentFetch('/api/library/file?path=' + encodeURIComponent(MEDIA_FEEDS_PATH), agentId);
    const feedsData = await feedsRes.json();
    const urls = feedsRes.ok ? parseFeedUrls(feedsData.content || '') : [];
    if (urls.length === 0) {
      note = 'No feeds configured yet -- waiting on media/feeds.md in the Library.';
    } else {
      const url = urls[Math.floor(Math.random() * urls.length)];
      // fetchPageSmart (world.js), not a raw /api/browse call: a
      // subscribed feed is exactly where a JS-rendered source (Reddit,
      // Twitter/X) would show up once you add one, and this automatically
      // falls back to a real rendered fetch only when the plain one comes
      // back looking like an empty shell, rather than silently filing a
      // digest of nothing.
      const browseData = await fetchPageSmart(agentId, url, 'Fetching a subscribed feed source to digest for the player.');
      if (!browseData.allowed) {
        note = `Tried to check a subscribed feed, but it wasn't approved: ${browseData.reason || 'no reason given'}`;
      } else if (!browseData.text) {
        note = `Checked ${url}, but the page came back empty.`;
      } else {
        const chatRes = await agentFetch('/api/chat', agentId, {
          method: 'POST',
          body: JSON.stringify({
            model: (MODEL_TIERS[a.model] || MODEL_TIERS.small).slug,
            messages: [
              { role: 'system', content: 'Summarize the following page in 2-3 short, honest sentences for someone who has not read it. Only report what is actually in the text -- do not invent detail.' },
              { role: 'user', content: browseData.text.slice(0, 6000) },
            ],
            max_tokens: 200,
            agentId,
          }),
        });
        const chatData = await chatRes.json();
        const summary = (chatData.reply || '').trim();
        if (!summary) {
          note = `Fetched ${url}, but couldn't summarize it this time.`;
        } else {
          const slug = url.replace(/^https?:\/\//, '').replace(/[^a-z0-9]+/gi, '-').slice(0, 40).toLowerCase();
          const path = `media/digests/${Date.now()}-${slug}.md`;
          const content = `# Digest -- ${new Date().toISOString()}\n\nSource: ${url}\nBy: ${a.name}\n\n${summary}\n`;
          await writeLibraryFile(agentId, path, content, 'firsthand');
          logThinkTankAction(agentId, 'media_digest_filed', { url, path });
          note = `Filed a digest on ${url} for the player.`;
        }
      }
    }
  } catch (e) {
    note = 'Tried to check a subscribed feed, but the request failed.';
  }
  if (AGENTS[agentId]) {
    AGENTS[agentId].profile.notes.push(note);
    if (AGENTS[agentId].profile.notes.length > 5) AGENTS[agentId].profile.notes.shift();
  }
}

function finishTask(id) {
  const a = AGENTS[id];
  const task = TASKS[a.task];
  if (task) { task.status = 'done'; lastTaskCompletedAt[task.room] = Date.now(); }

  a.task = null;
  a.busy = false;
  a.inRoom = null;
  // Bug: arriveAtTask() sets a.visible = false
  // on arrival (she's "inside" working, so the map shouldn't show her
  // outdoors) and nothing ever set it back. Since tickAgentMovement skips
  // invisible agents outright, that left EVERY agent who ever finished a
  // real task permanently invisible from this point on -- her own
  // off-duty walk home (sendAgentOffDuty) or a handoff walk (attemptHandoff,
  // handoffs.js) would get a real path assigned and then never actually
  // move, frozen forever with a path that just sits there. The "staying
  // visible ... until [the handoff] resolves" comment a few lines below
  // describes what was SUPPOSED to happen, not what the code did.
  a.visible = true;
  a.approvedCount++; // real completed work feeds morale, same signal as everything else

  // Reappear right where she walked in, not a random reachable spot --
  // She should walk out of the door she used. Falls back
  // to pickFreeSpot() only if this task somehow has no recorded entry
  // point (shouldn't happen via assignTask, but arriveAtTask() can still
  // be reached with a stale/missing task in edge cases).
  if (task && task.entryX != null) {
    // Deadlock: two agents finishing the SAME room's
    // task land on the identical entryX/entryY pixel. tickAgentMovement's
    // co-located exemption (agentBlockedAt) only ever buys the first one
    // a single step of separation -- nowhere near enough to clear
    // AGENT_W (20px) at real per-frame movement speed, so a SUBSEQUENT
    // walk from that exact shared point (off duty, a handoff) can
    // genuinely deadlock for real: confirmed, stuckTimer cycling
    // 0->TASK_STUCK_TIMEOUT->0 forever, replanning to the same nearby
    // cell every time, real position never moving. Checking for a real
    // occupant here and stepping aside by a full agent-width closes this
    // at the source, instead of relying on real-time collision recovery
    // to escape a full box overlap it was never built to escape.
    let ex = task.entryX, ey = task.entryY;
    for (const oid in AGENTS) {
      if (oid === id) continue;
      const other = AGENTS[oid];
      if (other && other.visible && other.x === ex && other.y === ey) {
        ex += AGENT_W + 8;
        break;
      }
    }
    a.x = ex; a.y = ey;
  } else {
    const occupied = Object.values(AGENTS).filter(v => v.visible && v.id !== id).map(v => ({ x: v.x, y: v.y }));
    const spot = pickFreeSpot(occupied);
    a.x = spot.x; a.y = spot.y;
  }

  // A plain local log entry, not a model call --
  // this shouldn't spend anything.
  if (task) {
    a.profile.notes.push(`${task.title} -- completed and logged.`);
    if (a.profile.notes.length > 5) a.profile.notes.shift();
  }

  showToast(`${a.name} finished: ${task ? task.title : 'a task'}.`, 4000);
  if (task) {
    logThinkTankAction(id, 'task_completed', { taskId: task.id, title: task.title, room: task.room });
    // Completed tasks get archived in the Library's shared
    // directory, not just logged privately -- a real record other agents
    // (or you) could actually read later, not just a database row.
    const record = `# ${task.title}\n\nCompleted by: ${a.name} (${id})\nRoom: ${task.room}\nCompleted: ${new Date().toISOString()}\n`;
    writeLibraryFile(id, `archive/${Date.now()}-${task.id}.md`, record);
  }

  // After logging it, she clocks off for the rest of this
  // session rather than immediately queuing for another task -- UNLESS
  // this task's room is something else's dependency (handoffs.js), in
  // which case she walks over and hands it off FIRST, staying visible and
  // on duty until that resolves. offDuty is excluded from
  // assignTaskViaJev's candidate list, and persists across a page reload
  // (initAgents(), agents.js) rather than resetting -- resting is a real,
  // deliberate state now, not something a reload undoes.
  if (task) {
    attemptHandoff(id, task.room, task.title).then(started => {
      if (!started) sendAgentOffDuty(id);
    });
  } else {
    sendAgentOffDuty(id);
  }
}

// 25 (MAX_ACTIVE_AGENTS, hiring.js)
// is a hard ceiling on how many agents may be online or freshly called
// in for a scheduled item AT ONCE -- not on the total inventory
// (MAX_TOTAL_AGENTS, which can be much larger, e.g. 100+, since most of
// it sits fully dormant). A dormant agent with nothing current or
// imminent counts toward neither number. Counts everyone on-duty,
// admins included, since the active ceiling is about real-time
// footprint on the map, not worker headcount specifically.
function activeAgentCount() {
  return AGENT_ROSTER.filter(d => !AGENTS[d.id].offDuty).length;
}
function canActivateAnother() {
  return activeAgentCount() < MAX_ACTIVE_AGENTS;
}

// The whole point of a larger hireable
// roster with only some agents "active" at once is that calling someone
// in is a real, visible event -- she appears at a free spot in the main
// think tank (outskirts rooms are gone) and walks from there, not an instant
// pop-in wherever her stale x/y happened to be left. Used by
// assignTaskViaJev/assignPairTask whenever a wake actually happens; the
// real walk to wherever she's needed is just assignTask's own ordinary
// pathfinding from this point, same as any other agent starting a task.
function appearFromOutskirts(agent) {
  agent.offDuty = false;
  agent.visible = true;
  agent.inRoom = null;
  const occupied = Object.values(AGENTS).filter(v => v.visible && v.id !== agent.id).map(v => ({ x: v.x, y: v.y }));
  const spot = pickFreeSpot(occupied);
  agent.x = spot.x;
  agent.y = spot.y;
}

// An agent going off duty VANISHES WHERE SHE
// STANDS -- no trek to a trailhead door (the outskirts rooms are gone).
// Mirrors the server's send_agent_off_duty so the client and server agree:
// an idle agent rests in place, invisible, inRoom cleared so she can't
// render inside a room the player walks into. Refusing anyone busy/mid-
// task/mid-pair/mid-handoff is what makes it safe to call on the WHOLE
// roster at once (a real "send everyone home" action) regardless of who's
// mid-task right now -- finishTask() calls this itself the instant a real
// task actually finishes, so refusing here just defers the request until
// the agent is genuinely idle.
function sendAgentOffDuty(id) {
  const a = AGENTS[id];
  if (!a) return;
  if (a.busy || a.task || a.pairWith || a.handoff) return;
  a.path = null;
  a.pathIndex = 0;
  a.pathTarget = null;
  a.headingOffDuty = null;
  a.offDuty = true;
  a.visible = false;
  a.inRoom = null;
}

// The other half: standing up for the first time (a brand-new hire) or
// coming back on duty appears at a free spot in the main think tank, not at
// a trailhead door (the outskirts rooms are gone). Mirrors the server's
// appear_from_outskirts / pick_free_spot so the client and server wake an
// agent identically. `occupied` is the same avoid-list shape pickFreeSpot()
// already takes elsewhere, so callers can pass in every other visible
// agent's position to avoid spawning her right on top of someone. The
// walk-to-target is a bonus (she visibly arrives where she'll wait), not a
// door-required step; if no path exists she stays at the free spot.
function spawnAgentAtFreeSpot(id, occupied = []) {
  const a = AGENTS[id];
  if (!a) return false;
  a.visible = true;
  a.inRoom = null;
  const spot = pickFreeSpot(occupied);
  a.x = spot.x;
  a.y = spot.y;
  // A short walk to a nearby free target makes her visibly arrive, but
  // standing on the free spot is already valid -- don't force a walk that
  // could stall her. Skip findPath entirely; she can pathfind when she's
  // actually assigned a task (assignTask's own machinery).
  return true;
}
