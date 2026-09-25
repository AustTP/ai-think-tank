// Real Jev wiring -- POST /api/alpha/decisions via OpenRouter (serve.py's
// /api/decide proxy), NOT /chat/completions. This took two wrong turns to
// nail down (see DESIGN.md and the project-jev-routing memory): a search
// summary claimed a generic "passthrough" without checking the actual
// endpoint shape, and OpenRouter's own /v1/models catalog doesn't list
// "decisions"-type models at all, which looked like a confident "Jev
// isn't on OpenRouter" until an actual functional call against the right
// endpoint proved otherwise. Lesson applied here: this file exists
// because a live call settled it, not a document.
//
// Deliberately generic -- picks one of N labeled candidates given some
// context, nothing task- or hiring-specific baked in. tasks.js and
// hiring.js both use this for exactly the kind of classifier "pick one of
// N" decision Jev is for; neither generates a reply from it.
const JEV_MODEL = 'typesafe/jev-1.13';

// Confidence at or above this is treated as "act on it"; below it a
// decision is routed to a human instead of acted on (the Jev contract:
// "act when confident, escalate when unsure"). Mirrors serve.py's
// JEV_SAFETY_CONFIDENCE for the safety gates; grading call sites (g2+)
// use the same bar so an unsure grade surfaces to the player rather than
// silently revising or silently passing.
const JEV_GRADE_CONFIDENCE = 0.6;

// candidates: [{id, description}]. Returns {choice, confidence, cost},
// where `choice` is the chosen candidate's id or null if the call fails
// or Jev's answer doesn't match a real candidate -- callers decide their
// own fallback, Jev doesn't have one of its own. confidence defaults to
// 1.0 when absent so existing callers behave exactly as before; grading
// call sites opt into thresholding via JEV_GRADE_CONFIDENCE.
async function requestJevChoice(instructions, candidates, agentId) {
  if (!candidates || candidates.length === 0) return null;
  const criteria = {};
  for (const c of candidates) criteria[c.id] = c.description;
  try {
    const res = await apiFetch('/api/decide', {
      method: 'POST',
      body: JSON.stringify({
        model: JEV_MODEL,
        agentId: agentId,
        state: { messages: [], signals: {} },
        questions: { choice: { type: 'choice', instructions, criteria } },
      }),
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      console.error('Jev decision failed:', data.error);
      return { choice: null, confidence: 1.0, cost: 0.0 };
    }
    const ans = (data.answers || {}).choice || {};
    const choice = candidates.some(c => c.id === ans.choice) ? ans.choice : null;
    const confidence = (typeof ans.confidence === 'number') ? ans.confidence : 1.0;
    const cost = (typeof data?.usage?.cost === 'number') ? data.usage.cost : 0.0;
    console.info(`Jev chose "${choice}" (confidence ${confidence.toFixed(3)}, cost ${cost.toFixed(6)})`);
    return { choice, confidence, cost };
  } catch (e) {
    console.error('Jev decision failed:', e);
    return { choice: null, confidence: 1.0, cost: 0.0 };
  }
}
