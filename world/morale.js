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
// a small, capped nod; drops and being reported on cost real points; being
// neglected costs the most and is uncapped-feeling (never contacted is
// worse than any measured number of days).
const MORALE_APPROVED_WEIGHT = 0.5, MORALE_APPROVED_CAP = 15;
const MORALE_DROPPED_WEIGHT = 6;
const MORALE_REPORT_WEIGHT = 10;
const MORALE_NEGLECT_WEIGHT = 3, MORALE_NEGLECT_CAP = 30;
// Real bug caught empirically, not by reading the code: droppedCount is
// only ever set once, at hire time (seed flavor data for the original
// six, or 0 for a real hire) -- nothing in the running think tank ever
// increments OR decays it. For an agent whose real activity doesn't
// otherwise clear the gap (Dev's seeded 6 drops cost a flat 36 points,
// permanently), that made morale a ceiling, not a meter: 236 real firing
// reviews this session, stuck at 45-49 the entire time, because the one
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
  const a = AGENTS[agentId];
  if (!a) return null;
  const approvedBonus = Math.min(a.approvedCount * MORALE_APPROVED_WEIGHT, MORALE_APPROVED_CAP);
  // Decays toward 0 over MORALE_DROPPED_DECAY_DAYS since hire (or since
  // the fix landed, for anyone already in the roster -- see agents.js's
  // restore-path default) rather than sitting at full weight forever.
  const daysSinceHire = a.hiredAt ? daysSince(a.hiredAt) : 0;
  const dropDecay = Math.max(0, 1 - daysSinceHire / MORALE_DROPPED_DECAY_DAYS);
  const droppedPenalty = a.droppedCount * MORALE_DROPPED_WEIGHT * dropDecay;
  const reportPenalty = reportsAbout(agentId).length * MORALE_REPORT_WEIGHT;
  const neglectPenalty = a.lastContactedAt
    ? Math.min(daysSince(a.lastContactedAt) * MORALE_NEGLECT_WEIGHT, MORALE_NEGLECT_CAP)
    : MORALE_NEGLECT_CAP;
  const raw = 100 + approvedBonus - droppedPenalty - reportPenalty - neglectPenalty;
  return Math.max(0, Math.min(100, Math.round(raw)));
}

function thinkTankMorale() {
  const scores = Object.keys(AGENTS).map(moraleFor).filter(v => v !== null);
  if (!scores.length) return null;
  return Math.round(scores.reduce((sum, v) => sum + v, 0) / scores.length);
}
