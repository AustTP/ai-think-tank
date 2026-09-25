// Firing, gated on a joint review -- per your call, "firing is something
// that should only be done after two admin agents have discussed the
// performance of an agent." Mirrors hiring.js's shape deliberately (same
// two-phase busy/invisible pattern, same Control Room location, same
// interval-driven autonomy) so the two admin mechanics read as one
// system, not two unrelated ones -- but this one needs BOTH reviewers
// free at once, not just one, and resolves via a real Jev decision
// instead of always firing whoever's worst off.
//
// Architecture (2026-09-21): the new approval model has a SINGLE admin
// (Theo) who passes personnel/approval matters down to the directors,
// and the senior-most director (Nora) approves/denies on the admin's
// behalf. Admin/director identity lives in the DATABASE and reaches this
// JS only because the server hydrates AGENT_ROSTER from it on load --
// agents.js carries none of these fields on purpose. Firing is exactly
// the consequential personnel call that needs independent oversight, so
// the joint review is done by the admin + the senior-most director
// together, not "two admins" (there is only one admin now). That is the
// smallest thing that preserves the original two-independent-approvers
// intent under the new single-admin structure.
const FIRING_COOLDOWN_MS = 60000;
const FIRING_REVIEW_DURATION_MS = 10000;

// 2026-09-23: no FIRING_MORALE_THRESHOLD constant anymore -- morale is the
// load-spreading/help signal, not a firing gate. Firing keys off
// hasFiringSignal() (a negative report or a real drop-off) so it stays "only
// after discussion of real evidence," never "fires as often as hiring."

let lastFiringReviewAt = 0;

// The two independent approvers who jointly review a firing: the admin
// (isAdmin) and the senior-most director (isDirector && !isAdmin && no
// `director` of their own -- the top of the director chain that stands in
// for the admin on approvals). Theo + Nora in the current roster.
// Returns [] if both can't be identified (shouldn't happen with the
// current roster, but a future edit that removes one shouldn't crash this
// instead of just silently not firing anyone).
function firingReviewers() {
  const admin = AGENT_ROSTER.find(d => d.isAdmin);
  let seniorDirector = null;
  for (const d of AGENT_ROSTER) {
    if (d.isDirector && !d.isAdmin && !d.director) { seniorDirector = d; break; }
  }
  if (!admin || !seniorDirector) return [];
  // Both in roster order.
  return [admin, seniorDirector];
}
// real reviews, all about the same agent, all landing on "keep," morale
// stuck at 45-49 the entire time (a seeded burnout example whose
// dropped-work penalty permanently keeps it just under the threshold).
// The morale gate is real, but nothing stopped it from re-litigating the
// exact same evidence forever once it had already been looked at and
// found not to warrant firing. Per your explicit call: unless something
// is actually found to be wrong, or something has genuinely changed since
// the last look, this shouldn't run again -- a "keep" verdict now skips
// this candidate until their morale score or their report count actually
// moves, not just after the base cooldown elapses.
function reviewIsStale(def) {
  const a = AGENTS[def.id];
  const last = a && a.lastFiringReview;
  if (!last || last.verdict !== 'keep') return false; // never reviewed, or last verdict was 'fire' (moot -- they'd be gone) -- nothing to skip
  const sameMorale = moraleFor(def.id) === last.morale;
  const sameReportCount = reportsAbout(def.id).length === last.reportCount;
  return sameMorale && sameReportCount;
}

// Morale is the load-spreading / who-needs-help signal (hiring keys its
// "who needs an assistant" Jev call on it) -- NOT a firing criterion. A
// neglected-but-otherwise-fine agent reads low morale and is a help/hire
// target; they aren't a firing target. Firing keys off a real corroborated
// personnel problem: a negative report filed against the candidate, or a
// true drop-off (handed work and dropped more than 1/3 of what they
// approved). Mirrors sim.py _has_firing_signal / _fire_decision.
function hasFiringSignal(def) {
  const a = AGENTS[def.id];
  if (!a) return false;
  const negatives = reportsAbout(def.id).filter(r => isNegativeSeverity(r.severity));
  if (negatives.length) return true;
  return a.droppedCount > a.approvedCount * 0.3;
}

function whoNeedsReview() {
  let best = null;
  for (const def of AGENT_ROSTER) {
    // Only workers are firing candidates -- the admin and the directors are
    // the ones who review, never the ones under review (a firing decision is
    // made by independent approvers over someone below them on the chain).
    if (def.isAdmin || def.isDirector) continue;
    if (reviewIsStale(def)) continue; // already looked at this exact situation and kept them -- nothing new to decide
    // Real gap: a fire decision deletes AGENTS[id] outright (see
    // finishFiringReview below), which is exactly right for "removed from
    // the map, uncallable" -- but doing that to someone mid-task/mid-pair/
    // mid-handoff would leave whoever they're tied to (a pair navigator's
    // .pairWith, a handoff partner) pointing at an id that no longer
    // resolves to anything. Same idle check used everywhere else in
    // tasks.js (assignTaskViaJev's pool filter, _awakeIdleCount) --
    // deferring until they're free avoids the dangling-reference case
    // entirely rather than needing to clean it up after the fact. Note
    // .busy alone isn't enough: a pair navigator never gets .busy set
    // (see arrivePair/runPairProgrammingSession), only .pairWith.
    const candidate = AGENTS[def.id];
    if (!candidate || candidate.busy || candidate.task || candidate.pairWith || candidate.handoff) continue;
    if (!hasFiringSignal(def)) continue;
    if (best === null) best = def; // among signaled candidates, first valid (roster order) is fine
  }
  return best;
}

// Called periodically (index.html's main()), same pattern as
// attemptAutoHire. No-ops unless the cooldown's elapsed, BOTH reviewers
// (the admin and the senior-most director) exist and are free, and
// someone's morale is actually low enough to warrant a look.
function attemptAutoFiringReview() {
  // Same idle rule as attemptAutoHire(): reviewing performance in a
  // village where nobody has been asked to do anything is exactly the
  // "runs more often than the problem it's for" waste you identified --
  // 247 real reviews last session, every one of them a 'keep'.
  if (!villageHasWork()) return false;
  const now = Date.now();
  if (now - lastFiringReviewAt < FIRING_COOLDOWN_MS) return false;

  const reviewers = firingReviewers();
  if (reviewers.length < 2) return false;
  const [reviewer1Def, reviewer2Def] = reviewers;
  const reviewer1 = AGENTS[reviewer1Def.id], reviewer2 = AGENTS[reviewer2Def.id];
  // Real bug caught live: this never checked offDuty, only busy -- an
  // off-duty approver (not busy, just resting) was treated as available,
  // and finishFiringReview() unconditionally sets visible=true on
  // completion without ever restoring offDuty, leaving her stuck in an
  // impossible offDuty=true/visible=true state. Same boundary every
  // other assignment path already draws: approval duties wait for them
  // to actually be on duty.
  if (!reviewer1 || !reviewer2 || reviewer1.busy || reviewer2.busy || reviewer1.offDuty || reviewer2.offDuty) return false;

  const candidateDef = whoNeedsReview();
  if (!candidateDef) return false;

  lastFiringReviewAt = now;
  for (const reviewer of [reviewer1, reviewer2]) {
    reviewer.busy = true;
    reviewer.visible = false;
    reviewer.inRoom = 'commandcenter';
    reviewer.dir = 'south';
  }
  // Two different desk blocks in the shared workstations layout (see
  // ROOM_COLLISIONS.workstations, rooms.js) -- reviewer1 reuses hiring.js's
  // exact desk, reviewer2 takes a different one entirely, so walking into
  // Control Room during a review shows two agents actually meeting, not
  // two copies stacked on the same spot.
  reviewer1.roomX = 315; reviewer1.roomY = 155;
  reviewer2.roomX = 485; reviewer2.roomY = 155;

  setTimeout(() => finishFiringReview(reviewer1Def, reviewer2Def, candidateDef), FIRING_REVIEW_DURATION_MS);
  return true;
}

// Who has firsthand, day-to-day knowledge of the candidate's work, beyond
// the two reviewers? Per your two calls: a report should notify the
// candidate's OWN supervisor first (reports.js notifySupervisorOfReport),
// and a firing should only happen after the reviewers have consulted
// anyone who's actually worked with them AND the person who reported them.
// This gathers that consultation pool from real evidence: every distinct
// agent who filed a report against them (the reporter/s), plus anyone
// currently paired or handing off work with them (actual collaborators).
// Returns { reporters: [{id,name,quote,note,severity}], coworkers: [{id,name}] }.
function firingConsultation(candidateDef) {
  const reports = reportsAbout(candidateDef.id);
  const reportedBy = new Set(reports.map(r => r.fromId).filter(id => id && id !== 'player'));
  const reporters = [...reportedBy].map(id => {
    const d = AGENT_ROSTER.find(x => x.id === id);
    const own = reports.filter(r => r.fromId === id);
    return { id, name: d?.name || id, quote: own.map(r => r.quote).join(' | '), note: own.map(r => r.note).join(' | '), severity: own.map(r => r.severity || 'unclassified').join(',') };
  });
  const coworkers = [];
  for (const other of AGENT_ROSTER) {
    if (other.id === candidateDef.id) continue;
    const o = AGENTS[other.id];
    if (!o) continue;
    // A real collaboration bond: currently paired with them, or pushing a
    // handoff to them. (Full historical "worked with" tracking is future
    // work; these are the people who've actually worked alongside them.)
    if (o.pairWith === candidateDef.id || o.handoff === candidateDef.id) {
      coworkers.push({ id: other.id, name: other.name || other.id });
    }
  }
  return { reporters, coworkers };
}

// Is a report actually a NEGATIVE signal of the kind that should weigh
// toward firing? Reports come from two different systems with two
// different severity vocabularies (see the reports.js vs serve.py note
// below), and NOT all reports are negative -- the server peer-review files
// "Standout performer" (positive) and "Nominal output" (neutral) reports
// too, which must never count as evidence for firing.
function isNegativeSeverity(severity) {
  const s = (severity || '').toLowerCase();
  return s.includes('severe') || s.includes('serious') || s.includes('major');
}

// Two independent guardrails, both learned from your two calls (supervisor
// notified first; don't fire prematurely without consulting people who've
// worked with them). If EITHER holds, the reviewers refrain from firing:
//   (1) the candidate has an ACTIVE collaborator (pair/handoff right now) --
//       firing them would strand that collaboration and suggests the
//       reviewers don't have the full picture of current work;
//   (2) there is no corroborated NEGATIVE report -- i.e. no severe/serious/
//       major evidence, or only a single uncorroborated voice, which is the
//       "premature" case you flagged. Positive/neutral peer-reviews do NOT
//       count as evidence for firing. Firing stays possible when there is
//       real, corroborated negative evidence.
function consultationBlocksFiring(candidateDef, morale) {
  const { reporters, coworkers } = firingConsultation(candidateDef);
  // Guardrail A -- never fire while someone is actively working with the
  // candidate, even on strong evidence (firing strands a live collaboration).
  if (coworkers.length > 0) return true;
  const negatives = reporters.filter(r => r.severity.split(',').some(isNegativeSeverity));
  // Guardrail B -- don't fire on weak signal. Only genuine negative evidence
  // (severe/serious/major) ever counts; minor reports are trivia regardless
  // of how many. A SEVERE report is real enough on its own (it's the bar the
  // classifier reserves for "should weigh toward a firing review"); a
  // serious/major concern needs a second independent voice before firing
  // isn't premature.
  if (negatives.length === 0) return true;                 // no genuine concern at all
  if (negatives.some(r => r.severity.split(',').some(s => s.toLowerCase() === 'severe'))) return false; // severe alone justifies
  return negatives.length < 2;                              // single serious/major -> defer for consultation
}

async function finishFiringReview(reviewer1Def, reviewer2Def, candidateDef) {
  const reviewer1 = AGENTS[reviewer1Def.id], reviewer2 = AGENTS[reviewer2Def.id];
  const candidate = AGENTS[candidateDef.id];

  for (const reviewer of [reviewer1, reviewer2]) {
    reviewer.busy = false;
    reviewer.visible = true;
    reviewer.inRoom = null;
  }

  // Candidate may have been fired, quit, or otherwise removed by the time
  // this review concludes (10s is enough for other systems to have acted)
  // -- nothing to resolve if there's no one left to discuss.
  if (!candidate) return;

  // Real evidence, the same signals morale.js itself weighs, laid out for
  // Jev rather than re-deriving its own judgment call -- this is exactly
  // the classifier "pick one of N" decision your Jev note describes, not
  // a reasoning task.
  const morale = moraleFor(candidateDef.id);
  const reportQuotes = reportsAbout(candidateDef.id).map(r => `"${r.quote}" -- ${r.note} (severity: ${r.severity || 'unclassified'})`).join(' | ') || 'none filed';
  const { reporters, coworkers } = firingConsultation(candidateDef);
  const consultantLine = [
    ...reporters.map(r => `${r.name} (reported them: "${r.quote}"${r.note ? ' -- ' + r.note : ''})`),
    ...coworkers.map(c => `${c.name} (currently working directly with ${candidate.name})`),
  ].join(' | ') || 'no one reported them and no one is currently working directly with them';
  const instructions = `${reviewer1Def.name} and ${reviewer2Def.name} are jointly reviewing ${candidate.name}'s (${candidate.role}) performance. ${reviewer1Def.name} is the admin; ${reviewer2Def.name} is the senior-most director standing in for the admin. Morale score: ${morale}/100. Approved work: ${candidate.approvedCount}. Dropped work: ${candidate.droppedCount}. Reports filed against them: ${reportQuotes}. ${candidate.name}'s own manager has already been notified of these reports. People consulted who have worked with or reported ${candidate.name}: ${consultantLine}. Decide whether to fire them or keep them on.`;
  const candidates = [
    { id: 'fire', description: `End ${candidate.name}'s role in the village -- performance does not justify keeping them on, and the people who work with them don't outweigh the evidence.` },
    { id: 'keep', description: `Keep ${candidate.name} on -- performance is acceptable, improving, or the evidence or the people who work with them don't support firing.` },
  ];

  let jevDecision = await requestJevChoice(instructions, candidates, reviewer1Def.id);
  let decision = jevDecision && jevDecision.choice;
  if (!decision) {
    // Jev outage fallback -- same idea as assignTaskViaJev's fallback,
    // simple enough not to need a model, re-keyed off the firing signal
    // (see hasFiringSignal) not the raw morale score: a corroborated
    // negative report AND a real drop record together justify letting
    // them go; either alone stays 'keep' (that agent needs help, not
    // firing). 2026-09-23 morale decouple.
    decision = (hasFiringSignal(candidateDef) && candidate.droppedCount > candidate.approvedCount * 0.3) ? 'fire' : 'keep';
  }

  // The anti-premature-firing guard, applied AFTER Jev (and after the
  // fallback) so it operates on the actual decision: if the reviewers
  // concluded "fire" but the people who'd know best say otherwise (an
  // active collaborator, or only thin/unconfirmed evidence), they hold off
  // rather than act on weak signal.
  if (decision === 'fire' && consultationBlocksFiring(candidateDef, morale)) {
    logVillageAction(reviewer1Def.id, 'firing_review', { about: candidateDef.id, decision: 'deferred_for_consultation', morale, reviewers: [reviewer1Def.id, reviewer2Def.id], consulted: { reporters: reporters.map(r => r.id), coworkers: coworkers.map(c => c.id) } });
    candidate.lastFiringReview = undefined; // don't mark it reviewed -- a real change (collaboration ends, evidence grows) should re-open it
    return;
  }

  // Same idle check as whoNeedsReview(), re-run here because the Jev call
  // just awaited above is real network time -- long enough for the
  // ambient task cycle to have picked the candidate up since the review
  // started. Deleting AGENTS[id] out from under a task/pair/handoff in
  // progress is exactly the dangling-reference case that check exists to
  // avoid; bail without recording a verdict so this candidate is looked
  // at fresh next cycle rather than wrongly treated as a real "keep".
  if (candidate.busy || candidate.task || candidate.pairWith || candidate.handoff) {
    logVillageAction(reviewer1Def.id, 'firing_review', { about: candidateDef.id, decision: 'deferred', reason: 'candidate became busy mid-review' });
    return;
  }

  if (decision === 'fire') {
    // Real edge case: the player could have a conversation window open
    // with exactly this agent when the review lands. Close it before
    // deleting -- renderConversationLog()/closeConversation() (index.html)
    // both dereference AGENTS[activeConversationAgent] unguarded, which
    // would throw the moment the player sent another line or closed the
    // window against someone no longer there.
    if (typeof activeConversationAgent !== 'undefined' && activeConversationAgent === candidateDef.id) closeConversation();
    delete AGENTS[candidateDef.id];
    const idx = AGENT_ROSTER.indexOf(candidateDef);
    if (idx !== -1) AGENT_ROSTER.splice(idx, 1);
    showToast(`${reviewer1.name} and ${reviewer2.name} let ${candidate.name} go after reviewing their performance.`, 5000);
  } else {
    // Recorded on the agent itself (persists the same way the rest of
    // AGENTS already does) so reviewIsStale() can skip re-litigating this
    // exact same evidence next cycle -- only a real change (morale moves,
    // or a new report gets filed) makes them eligible for review again.
    candidate.lastFiringReview = { morale, reportCount: reportsAbout(candidateDef.id).length, verdict: decision, at: Date.now() };
    showToast(`${reviewer1.name} and ${reviewer2.name} reviewed ${candidate.name}'s performance and decided to keep them on.`, 5000);
  }
  logVillageAction(reviewer1Def.id, 'firing_review', { about: candidateDef.id, decision, morale, reviewers: [reviewer1Def.id, reviewer2Def.id] });
}
