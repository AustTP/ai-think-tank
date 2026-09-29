// Agent-to-agent handoffs -- your call: agents should talk to each other
// for real reasons, not just chat with the player. First real trigger:
// task dependency. When a task whose room other work DEPENDS ON finishes
// (TASK_POOL's new `dependsOn` field, tasks.js), the agent who just
// finished walks over to whoever should hear about it and hands it off
// with one real, in-character line -- the collaboration IS the
// conversation, not a separate mechanic bolted alongside it.
//
// Deliberately reuses tasks.js's own movement machinery (findPath(),
// tickAgentMovement()'s stuck-detection/replan/respawn-fallback) rather
// than building a second walking system -- a handoff target (another
// agent's current position) is just as valid a findPath() destination as
// a door front, and none of that machinery cares which kind of "arrival"
// follows. Only the ARRIVAL behavior differs (a brief exchange, not
// entering a room), so `a.handoff` (separate from `a.task`) is what
// tickAgentMovement checks to decide which arrival/cancel path applies.
//
// Explicitly NOT covering every reason you named for agents to talk --
// "discussing a third agent" maps onto the reports/performance-review
// space and is a bigger, separate mechanic, deferred rather than crammed
// in here.
const HANDOFF_TALK_DURATION_MS = 1500;

let HANDOFFS = {};
let nextHandoffId = 1;

// A single real model call, not the full 1:1 conversationLog machinery --
// requestAgentReply() (index.html) is shaped around a player/agent
// exchange (its role-mapping assumes one side is 'player'), which doesn't
// fit an agent-to-agent line. This is deliberately its own small,
// self-contained call instead of contorting that one to fit.
async function requestHandoffLine(fromAgent, toAgent, taskTitle) {
  const tier = MODEL_TIERS[fromAgent.model] || MODEL_TIERS.small;
  const systemPrompt = `You are ${fromAgent.name}, working as ${fromAgent.role} in a small think tank. `
    + `You just finished "${taskTitle}", and ${toAgent.name} (${toAgent.role}) needs to know because their own work depends on it. `
    + `Say one short, casual, in-character sentence handing that off to them. Never mention you are an AI or a language model.`;
  try {
    const res = await agentFetch('/api/chat', fromAgent.id, {
      method: 'POST',
      body: JSON.stringify({ model: tier.slug, messages: [{ role: 'system', content: systemPrompt }, { role: 'user', content: 'Go ahead and tell them.' }], max_tokens: 60, agentId: fromAgent.id }),
    });
    const data = await res.json();
    if (!res.ok || data.error) return `(${fromAgent.name} mentions finishing ${taskTitle}.)`;
    return data.reply.trim();
  } catch (e) {
    return `(${fromAgent.name} mentions finishing ${taskTitle}.)`;
  }
}

// The recipient's side of the exchange -- per your ask for a better
// agent-to-agent conversation, a handoff was previously one-directional
// (fromAgent narrates, toAgent silently receives). Same shape as
// requestHandoffLine(), just the other agent's own model/voice replying
// to the specific line just said, not a generic acknowledgment.
async function requestHandoffReply(toAgent, fromAgent, line, taskTitle) {
  const tier = MODEL_TIERS[toAgent.model] || MODEL_TIERS.small;
  const systemPrompt = `You are ${toAgent.name}, working as ${toAgent.role} in a small think tank. `
    + `${fromAgent.name} just told you: "${line}" -- about finishing "${taskTitle}", which your own work depends on. `
    + `Reply with one short, casual, in-character sentence acknowledging it. Never mention you are an AI or a language model.`;
  try {
    const res = await agentFetch('/api/chat', toAgent.id, {
      method: 'POST',
      body: JSON.stringify({ model: tier.slug, messages: [{ role: 'system', content: systemPrompt }, { role: 'user', content: 'Go ahead and reply.' }], max_tokens: 60, agentId: toAgent.id }),
    });
    const data = await res.json();
    if (!res.ok || data.error) return `(${toAgent.name} acknowledges.)`;
    return data.reply.trim();
  } catch (e) {
    return `(${toAgent.name} acknowledges.)`;
  }
}

// Called from finishTask() (tasks.js) right after a task completes.
// Returns true if a handoff actually started (in which case the caller
// should NOT immediately clock the agent off duty -- that happens once
// the handoff itself resolves, see arriveAtHandoff/cancelHandoff below).
async function attemptHandoff(fromId, finishedRoom, finishedTitle) {
  const dependents = TASK_POOL.filter(t => t.dependsOn === finishedRoom);
  if (dependents.length === 0) return false;

  const idleCandidates = AGENT_ROSTER
    // DEDICATED_PROJECT_ROLES (tasks.js) -- same reasoning as
    // assignTaskViaJev/assignPairTask: a hired project specialist
    // shouldn't be pulled into unrelated ambient think tank handoffs either.
    .filter(d => !d.isAdmin && d.id !== fromId && !DEDICATED_PROJECT_ROLES.has(d.role))
    // !a.pairWith -- same race as assignTaskViaJev (tasks.js): an agent
    // mid-walk toward a pair session still shows visible/not-busy/no-task
    // until she actually arrives, so without this a handoff could target
    // someone who's about to vanish into a pairing session out from under it.
    .filter(d => { const a = AGENTS[d.id]; return a && a.visible && !a.busy && !a.task && !a.pairWith && !a.offDuty; })
    .map(d => ({ id: d.id, description: `${d.name}, ${d.role}. ${d.profile.mission}` }));

  // Real gap caught live: if everyone eligible happens to be busy right
  // now, this used to just silently give up. Per your call, mail
  // (agents.js's sendMail()) is exactly the channel for this -- it
  // reaches someone wherever they are, without needing them free.
  const anyCandidates = AGENT_ROSTER
    .filter(d => !d.isAdmin && d.id !== fromId && AGENTS[d.id])
    .map(d => ({ id: d.id, description: `${d.name}, ${d.role}. ${d.profile.mission}` }));

  const candidates = idleCandidates.length > 0 ? idleCandidates : anyCandidates;
  if (candidates.length === 0) return false;

  const chosenId = (await requestJevChoice(
    `${AGENTS[fromId].name} just finished "${finishedTitle}", which other work in the think tank depends on. Pick whoever should be told about it next.`,
    candidates
  ))?.choice;
  if (!chosenId) return false;

  if (idleCandidates.length === 0) {
    // Nobody free for a real walk-over handoff -- mail the same line
    // instead of waiting for them or dropping it. No physical handoff
    // starts, so the caller (finishTask) clocks her off duty normally.
    const line = await requestHandoffLine(AGENTS[fromId], AGENTS[chosenId], finishedTitle);
    sendMail(fromId, chosenId, line);
    return false;
  }

  const from = AGENTS[fromId], to = AGENTS[chosenId];
  // Re-check everyone's still in the state we expect -- a real async gap
  // (the Jev call) sits between the checks above and here, same class of
  // race attemptAutoHire() had to guard against.
  if (!from || !to || from.busy || to.busy || from.task || to.task || from.handoff || to.visible === false) return false;

  // The recipient's own exact position always fails findPath's target
  // check -- real bug caught live: agentBlockedAt() correctly reports
  // "someone's standing there," and that someone IS the recipient, so a
  // target of exactly (to.x, to.y) can never validate. Try a few points
  // just next to them instead (whichever direction has room), same idea
  // as assignTask() targeting a point in front of a door rather than the
  // door's own center.
  const offsets = [[AGENT_W + 4, 0], [-(AGENT_W + 4), 0], [0, AGENT_H + 4], [0, -(AGENT_H + 4)]];
  let path = null, targetX, targetY;
  for (const [ox, oy] of offsets) {
    targetX = to.x + ox; targetY = to.y + oy;
    path = findPath(from.x, from.y, targetX, targetY, fromId);
    if (path) break;
  }
  // See tasks.js's assignTask() for why this checks .length too, not just
  // truthiness -- findPath() used to (and, defensively, still might one
  // day) hand back a truthy-but-empty array for an already-there start
  // cell, which crashes the whole game's update() loop, not just this
  // one handoff.
  if (!path || path.length === 0) return false;

  const id = 'handoff-' + (nextHandoffId++);
  HANDOFFS[id] = { id, fromId, toId: chosenId, title: finishedTitle, status: 'walking' };
  from.handoff = id;
  from.path = path;
  from.pathIndex = 0;
  from.pathTarget = { x: targetX, y: targetY };
  from.stuckTimer = 0;
  from.replanCount = 0;
  from.respawnedForTask = false;
  return true;
}

function arriveAtHandoff(id) {
  const a = AGENTS[id];
  const h = HANDOFFS[a.handoff];
  a.path = null;
  a.pathIndex = 0;
  a.pathTarget = null;
  a.stuckTimer = 0;
  a.replanCount = 0;
  a.respawnedForTask = false;
  a.handoff = null;
  a.dir = 'south';

  // Clocking off is what finishTask() deferred to let this handoff play
  // out first -- happens regardless of whether the recipient is still
  // there by the time she arrives.
  a.offDuty = true;
  a.visible = false;

  if (!h) return;
  const to = AGENTS[h.toId];
  if (!to) return;

  h.status = 'talking';
  (async () => {
    const line = await requestHandoffLine(a, to, h.title);
    // Real two-way exchange, not a monologue -- to's own model/voice
    // replies to the specific line just said.
    const reply = await requestHandoffReply(to, a, line, h.title);
    markContacted(h.toId, Date.now());
    // Real speech bubble over the speaker, truncated -- replaces the old
    // screen-wide toast for the common case. `a` is a real exception: she
    // was already set offDuty/invisible at the TOP of this function (before
    // this async dialogue even started -- "vanish in place" fires the moment
    // she arrives, not when she finishes talking), so her own bubble would
    // never actually render. Fall back to a toast just for her line; `to`
    // stays visible through this whole exchange so her reply gets a real
    // bubble.
    if (a.visible) saySpeech(a.id, line); else showToast(`${a.name}: "${line}"`, 4500);
    setTimeout(() => saySpeech(h.toId, reply), 2200);
    h.status = 'done';
    // Previously logged only the metadata (who/what) -- the actual lines
    // said were shown in a 5s toast and then lost forever. Persisting
    // them here means the Activity Log (and MEMORY.md's decay-scored
    // history) can actually show what was said, not just that something
    // was.
    logThinkTankAction(a.id, 'handoff', { to: h.toId, title: h.title, line, reply });
    if (AGENTS[a.id]) {
      AGENTS[a.id].profile.notes.push(`Told ${to.name}: "${line}"`);
      if (AGENTS[a.id].profile.notes.length > 5) AGENTS[a.id].profile.notes.shift();
    }
    if (AGENTS[h.toId]) {
      AGENTS[h.toId].profile.notes.push(`${a.name} told me: "${line}" -- I said: "${reply}"`);
      if (AGENTS[h.toId].profile.notes.length > 5) AGENTS[h.toId].profile.notes.shift();
    }
  })();
}

// Mirrors cancelTask()'s "give up cleanly" shape for the handoff case --
// reached if the walk itself never manages to arrive (stuck with no
// route, same as a real task).
function cancelHandoff(id) {
  const a = AGENTS[id];
  const h = HANDOFFS[a.handoff];
  if (h) h.status = 'cancelled';
  a.path = null;
  a.pathIndex = 0;
  a.pathTarget = null;
  a.stuckTimer = 0;
  a.replanCount = 0;
  a.respawnedForTask = false;
  a.handoff = null;
  a.offDuty = true;
  a.visible = false;
}
