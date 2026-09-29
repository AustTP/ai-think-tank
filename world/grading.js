// Phase G -- the grading half of the self-improving revision loop.
//
// The article loop ("How to Build a Self-Improving AI Employee") is
// create -> evaluate -> revise -> evaluate again. The think tank already
// creates and revises (tasks.js); this module supplies the EVALUATE step
// that's missing: turn a deliverable (here, a review/QA write-up) into a
// set of per-requirement Jev grades, each meets/fails/insufficient, so a
// failed check can target which section to revise and an uncertain one can
// surface to the player.
//
// Jev is a classifier over a candidate list (jev.js) -- perfect for
// "this specific requirement: met or not?"; NOT for "is this whole thing
// good?" (that bundles idea + evidence + writing together and is the
// player's call, exactly like the article's "i still decide whether the
// idea is interesting enough to publish").

// A checklist requirement the planning model can emit for a big task:
//   { id, question, section, type: 'code'|'jev'|'human' }
// - 'code': mechanically checkable (a count, an absent string, a URL rule);
//   graded locally, no Jev call.
// - 'jev': a focused content judgment; graded via gradeAgainstRequirements.
// - 'human': the player's editorial call -- never auto-decided, surfaced.
const GRADE_MEETS = 'meets_requirement';
const GRADE_FAILS = 'fails_requirement';
const GRADE_UNSURE = 'insufficient_evidence';

// Confidence bar reused from jev.js (JEV_GRADE_CONFIDENCE, which mirrors
// serve.py's JEV_SAFETY_CONFIDENCE) -- below it, an automatic Jev grade is
// unreliable and must surface to the player as "unsure" rather than be
// acted on (revising or passing on weak signal). Declared in jev.js; we
// read the same global so the two definitions can't drift.

// How many times a single task may be revised in response to failed
// grades before the loop stops and whatever is still failing or uncertain
// escalates to the player -- the article's "allow at most two revision
// rounds, then return the latest draft and unresolved issues."
const MAX_REVISION_ROUNDS = 2;

// Grade one focused requirement against a piece of the deliverable.
// `requirement` is { id, question, section }; `evidence` is the relevant
// chunk of the review/draft text. Returns { requirementId, section, verdict,
// confidence }. verdict is meets/fails, or insufficient_evidence when the
// Jev call fails OR its confidence is below the acting bar.
async function gradeAgainstRequirements(requirement, evidence, agentId) {
  const decision = await requestJevChoice(
    `You are grading one specific requirement of a deliverable. `
    + `Requirement: "${requirement.question}". `
    + `The section being checked is "${requirement.section}". `
    + `Relevant part of the deliverable: "${String(evidence).slice(0, 1500)}" `
    + `Answer whether THIS requirement is met by THIS deliverable.`,
    [
      { id: GRADE_MEETS, description: 'The deliverable satisfies this specific requirement.' },
      { id: GRADE_FAILS, description: 'The deliverable does not satisfy this specific requirement.' },
      { id: GRADE_UNSURE, description: 'Not enough evidence in the deliverable to judge, or the question cannot be answered from it.' },
    ],
    agentId
  );
  const choice = decision && decision.choice;
  const confidence = decision ? decision.confidence : 0.0;
  // The Jev contract: act when confident, escalate when unsure. A grade at
  // low confidence -- or a failed call -- must not drive an automatic
  // revision, so route it to the player as insufficient instead.
  if (choice === GRADE_MEETS || choice === GRADE_FAILS) {
    if (confidence < JEV_GRADE_CONFIDENCE) {
      return { requirementId: requirement.id, section: requirement.section, verdict: GRADE_UNSURE, confidence };
    }
    return { requirementId: requirement.id, section: requirement.section, verdict: choice, confidence };
  }
  return { requirementId: requirement.id, section: requirement.section, verdict: GRADE_UNSURE, confidence };
}

// Grade the 'jev'-type requirements of a checklist against a deliverable
// chunk. 'code'- and 'human'-type requirements are skipped here -- code is
// graded locally (gradeCodeRequirement), human is surfaced to the player.
// Returns the list of { requirementId, section, verdict, confidence }.
async function gradeJevRequirements(checklist, review, agentId) {
  if (!checklist || checklist.length === 0) return [];
  const grades = [];
  for (const req of checklist) {
    if (req.type !== 'jev') continue;
    grades.push(await gradeAgainstRequirements(req, review, agentId));
  }
  return grades;
}

// A code-type requirement: `code` is a predicate on the deliverable text.
// Kept pure so it's unit-testable (test_grading.mjs). Returns a grade
// object shaped like gradeAgainstRequirements's, so the caller can treat
// all requirements uniformly.
function gradeCodeRequirement(req, review) {
  let verdict = GRADE_UNSURE;
  try {
    if (req.code && req.code(review)) verdict = GRADE_MEETS;
    else if (req.code) verdict = GRADE_FAILS; // a real predicate that returned false
  } catch (e) {
    verdict = GRADE_UNSURE; // malformed predicate -> don't guess
  }
  return { requirementId: req.id, section: req.section, verdict, confidence: 1.0 };
}