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
// work (runWorkroomTask/runResearchTask/runMediaDigestTask/
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
  { title: 'Research an open question', room: 'weatherstation', instructions: 'Pick whoever is best suited to research an open question at the Weather Station.' },
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
  // (internet-access room -> Eli, banking -> Ben, etc.), so Dev's four assistants --
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
    .map(d => ({ id: d.id, description: `${d.name}, ${d.role}. ${agentMission(d)}` }));
  if (candidates.length === 0) return null;

  let chosenId = (await requestJevChoice(instructions, candidates))?.choice;
  if (!chosenId) chosenId = candidates[0].id;

  const chosen = AGENTS[chosenId];
  if (chosen && chosen.offDuty) appearFromOutskirts(chosen);

  return assignTask(chosenId, title, room, instructions, projectLabel, extra);
}

