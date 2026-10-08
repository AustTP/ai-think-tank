// Morale meter, modeled on the reference video: "it rates every single
// agent on how they're doing and why" -- Ryan (5 approved, not spoken to
// in a week) reads as fine, Greg (26 approved, 9 dropped, not spoken to in
// over a week) reads as burnt out at zero. The video never discloses an
// exact formula, and the two examples don't reverse-engineer cleanly to
// one (their exact "over a week" durations aren't given either) -- this is
// a reasonable formula capturing the same shape, not a reconstruction of
// their real one.
//
// Deliberately NOT "more approved work = more morale": Greg does more work
// than anyone and has the worst morale in the reference, which is the
// whole point being made (burnout, not a leaderboard). Approved work gets
// a small, capped nod; drops cost real points; being
// neglected costs the most and is uncapped-feeling (never contacted is
// worse than any measured number of days). Reports/notes deliberately do
// NOT move the meter: the reference video's meter reacts to approved work,
// dropped work, and whether an agent is spoken to -- nothing else -- so a
// note about an agent (good or bad) informs firing review, not morale.
const MORALE_APPROVED_WEIGHT = 0.5, MORALE_APPROVED_CAP = 15;
const MORALE_DROPPED_WEIGHT = 6;
const MORALE_NEGLECT_WEIGHT = 3, MORALE_NEGLECT_CAP = 30;
// Real bug caught empirically, not by reading the code: droppedCount is
// only ever set once, at hire time (seed flavor data for the original
// six, or 0 for a real hire) -- nothing in the running think tank ever
// increments OR decays it. For an agent whose real activity doesn't
// otherwise clear the gap (Dev's seeded 6 drops cost a flat 36 points,
// permanently), that made morale a ceiling, not a meter: 236 real firing
// reviews, stuck at 45-49 the entire time, because the one
// input actually holding the score down could never move. A burnout
// meter that can't recover isn't a meter, it's a label. Real fix: the
// dropped-work penalty now fades out over MORALE_DROPPED_DECAY_DAYS of no
// NEW drop, the same "recency is what matters" logic neglect already
// uses in the other direction -- an old mistake shouldn't be a life
// sentence, but it isn't erased instantly either.
const MORALE_DROPPED_DECAY_DAYS = 14;

function daysSince(ts) {
  return (Date.now() - ts) / 86400000;
}

function moraleFor(agentId) {
  if (DELIVERY_STATE) return recentDeliveryScore(agentId).score;
  const a = AGENTS[agentId];
  if (!a) return null;
  const approvedBonus = Math.min(a.approvedCount * MORALE_APPROVED_WEIGHT, MORALE_APPROVED_CAP);
  // Decays toward 0 over MORALE_DROPPED_DECAY_DAYS since hire (or since
  // the fix landed, for anyone already in the roster -- see agents.js's
  // restore-path default) rather than sitting at full weight forever.
  const daysSinceHire = a.hiredAt ? daysSince(a.hiredAt) : 0;
  const dropDecay = Math.max(0, 1 - daysSinceHire / MORALE_DROPPED_DECAY_DAYS);
  const droppedPenalty = a.droppedCount * MORALE_DROPPED_WEIGHT * dropDecay;
  const neglectPenalty = a.lastContactedAt
    ? Math.min(daysSince(a.lastContactedAt) * MORALE_NEGLECT_WEIGHT, MORALE_NEGLECT_CAP)
    : MORALE_NEGLECT_CAP;
  const raw = 100 + approvedBonus - droppedPenalty - neglectPenalty;
  return Math.max(0, Math.min(100, Math.round(raw)));
}

// Live delivery data is refreshed by the server-state bridge.
let DELIVERY_STATE = null;
function recentDeliveryScore(agentId = null, now = Date.now()) {
  const state = DELIVERY_STATE || {};
  const tasks = Object.values(state.tasks || (typeof TASKS !== 'undefined' ? TASKS : {}));
  const cutoff = now - 7 * 86400000;
  const recent = ts => Number(ts) >= cutoff && Number(ts) <= now;
  const completions = new Map((state.completedDeliverables || []).map(d => [d.id, d.gradedAt]));
  const work = tasks.filter(t => !t.reviewOf && !['review','qa','code_preflight'].includes(t.taskType)
    && (!agentId || t.assignedTo === agentId) && !t.supersededByAutomaticRepair);
  const completed = work.filter(t => t.status === 'done' && recent(t.completedAt || completions.get(t.id) || t.createdAt)).length;
  const failed = work.filter(t => ['failed','abandoned'].includes(t.status) && recent(t.completedAt || t.createdAt)).length;
  const blocked = work.filter(t => !t.boardClearedAt && !['done','cancelled','abandoned'].includes(t.status)
    && (t.manualAttention || t.status === 'blocked' || t._peerGate?.escalated)).length;
  const working = work.filter(t => ['walking','working'].includes(t.status)).length;
  const review = work.filter(t => t.status === 'needs_review' && !t.manualAttention && !t._peerGate?.escalated).length;
  const teams = (state.teams || []).filter(t => t.directorId === agentId || t.scrumMasterId === agentId
    || (typeof AGENTS !== 'undefined' && AGENTS[agentId]?.director === t.directorId)).map(t => t.id);
  const sprints = Object.values(state.sprints || {}).filter(s => s.status === 'closed' && recent(s.closedAt)
    && (!agentId || (s.teamIds || []).some(id => teams.includes(id))));
  let committed = 0, shipped = 0;
  for (const sprint of sprints) {
    const total = (sprint.items || []).length;
    committed += total; shipped += Math.max(0, total - (sprint.rolledOver || []).length);
  }
  const released = Object.values(state.products || {}).filter(p => (p.revisions || []).some(r => recent(r.at)
    && (!agentId || r.releasedBy === agentId))).length;
  const evidence = completed + failed + sprints.length + released;
  // Smoothing prevents a single success from implying perfect delivery.
  let score = 100 * (completed + 2) / (completed + failed + 4);
  if (committed) score = .7 * score + .3 * (100 * shipped / committed);
  score += Math.min(15, released * 5);
  score -= Math.min(30, blocked * 5);
  return {score: evidence || blocked ? Math.round(Math.max(0, Math.min(100, score))) : null,
    completed, failed, blocked, working, review, sprints: sprints.length, shipped, committed, released};
}
function deliveryScoreDescription(metrics) {
  return `Last 7 days: ${metrics.completed} completed delivery tasks, ${metrics.failed} failed; `
    + `${metrics.sprints} closed sprints (${metrics.shipped}/${metrics.committed} items completed), `
    + `${metrics.released} projects released. Current: ${metrics.blocked} blocked, ${metrics.review} awaiting review. `
    + 'Score uses smoothed completion rate, sprint completion and release bonuses, minus blocker penalties. No recent outcomes = no score.';
}
function thinkTankMorale() {
  return recentDeliveryScore().score;
}
function workActivityCounts() {
  const agents = typeof AGENTS !== 'undefined' ? Object.values(AGENTS) : [];
  const tasks = DELIVERY_STATE?.tasks || (typeof TASKS !== 'undefined' ? TASKS : {});
  let working = 0;
  for (const agent of agents) {
    const task = tasks[agent.task];
    if (agent.busy || (task && ['walking','working'].includes(task.status))) working++;
  }
  return {total: agents.length, working, idle: agents.length - working};
}
