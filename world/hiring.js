// Autonomous hiring in Control Room. Per your call: this is agent-
// initiated, not something you fill out a form for -- an authority figure
// (see agents.js's availableAuthority: the admin, or the senior-most
// director) periodically hires someone to help whoever's struggling most,
// with no player action required. Under the single-admin model the admin
// passes day-to-day personnel work down to the directors, so the senior
// director is a legitimate hirer too, not just Theo. House (a separate,
// physical building) is "just the board" for you; Control Room (reachable
// only via the HUD's Command Center button, see rooms.js) is where this
// actually happens.
//
// You need to be able to monitor this, not just see a toast after the
// fact -- so hiring is two real phases, not one instant background event.
// While it's running, the hirer is busy/invisible on the outdoor map (same
// as being in a Town Hall call) but genuinely present and visible if you
// walk into Control Room yourself during that window
// (renderAgentsInRoom(), agents.js). A room nobody can observe is also a
// room where elevated access could be misused without anyone noticing --
// noted as a real design tension, not resolved here (no actual misuse
// mechanic is built; this just makes the room a real, checkable place
// instead of an opaque instant event).
const HIRE_NAME_POOL = ['Marcus', 'Priya', 'Sam', 'Nadia', 'Leo', 'Ines', 'Theo', 'Yuki', 'Omar', 'Greta'];
const HIRE_COLOR_POOL = ['#f6b26b', '#76a5af', '#a4c2f4', '#d5a6bd', '#b6d7a8', '#ffe599'];
const HIRE_COOLDOWN_MS = 45000;
const HIRE_DURATION_MS = 8000;

// Real budget protection, per your call -- autonomous hiring with no cap
// would grow unbounded (and once real model calls exist, so would spend).
// Split into two separate numbers per your explicit call on 2026-09-20:
// a large TOTAL inventory (agents hired for a skill set, most of them
// fully offline/dormant most of the time -- MAX_TOTAL_AGENTS, a budget
// backstop against a runaway hiring bug, not a real constraint on
// breadth of skills) versus a much smaller ACTIVE ceiling
// (MAX_ACTIVE_AGENTS -- online, or freshly called in for a scheduled
// item that just came due). A dormant hire with nothing current or
// imminent counts toward neither number and stays invisible/gone from
// the map, same as any other off-duty agent. See
// activeAgentCount()/canActivateAnother() (tasks.js) for where the
// active half of this is actually enforced.
const MAX_TOTAL_AGENTS = 500;
const MAX_ACTIVE_AGENTS = 25;

// How often to actually log/toast "we're at the cap" while blocked --
// attemptAutoHire() is polled every 5s (index.html), so without a
// separate, longer-lived cooldown here, being at the cap would log the
// exact same fact hundreds of times an hour. This is a "so you actually
// notice" cadence, not a real hire attempt, so it doesn't need to be
// anywhere near as frequent as HIRE_COOLDOWN_MS.
const HIRE_CAP_NOTICE_COOLDOWN_MS = 30 * 60 * 1000;
let lastHireCapNoticeAt = 0;

function noteHireBlockedAtCap(adminId) {
  const now = Date.now();
  if (now - lastHireCapNoticeAt < HIRE_CAP_NOTICE_COOLDOWN_MS) return;
  lastHireCapNoticeAt = now;
  logThinkTankAction(adminId || 'admin', 'hire_blocked_at_cap', { rosterSize: AGENT_ROSTER.length, max: MAX_TOTAL_AGENTS });
  showToast(`Think Tank is at its ${MAX_TOTAL_AGENTS}-agent total inventory limit (${AGENT_ROSTER.length} hired) -- no further hires until someone leaves for good.`, 6000);
}

// Model tier metadata -- wired for real via /api/chat (serve.py) and
// requestAgentReply() (index.html) for 1:1 conversations. All three slugs
// below confirmed against OpenRouter's live /api/v1/models catalog. Per
// your call: most agents don't need a powerful model, so 'small' is the
// default for both the original roster and every new hire; only the
// admin (and her senior-most director, who inherits the judgment tier for
// these decisions) get a step up, since hiring decisions involve more
// judgment than routine work.
// Per your call: these shouldn't be three slugs frozen in code -- Jev
// picks them from OpenRouter's real, current catalog by price band (see
// serve.py's refresh_model_tiers()). Static fallback values here only
// matter for the brief window before loadModelTiers() (world.js)
// resolves at startup, or if that fetch ever fails outright.
const MODEL_TIERS = {
  small: { label: 'loading...', slug: 'amazon/nova-micro-v1' },
  // Per your call (2026-09-25): mid and high/planning replaced with the low
  // band's model -- most agent work doesn't need more than that. Old values
  // commented out (and preserved in think_tank.db's model_tiers.previous_* columns,
  // same rows) so this is a one-line revert if you change your mind:
  // mid: { label: 'loading...', slug: 'deepseek/deepseek-v4-flash-0731' },
  mid: { label: 'loading...', slug: 'deepseek/deepseek-v4-flash' },
  // Split out of a single overloaded 'premium' tier, per your call:
  // writing code and planning work are different jobs. `coding` is what
  // every real code-generation path uses; `planning` is the expensive
  // one, used once per request to decompose it (assignBigTask).
  coding: { label: 'loading...', slug: 'qwen/qwen3-coder' },
  // planning: { label: 'loading...', slug: 'deepseek/deepseek-v4-pro-0813' },
  planning: { label: 'loading...', slug: 'deepseek/deepseek-v4-flash' },
  // Real bug caught live: reviewScreenshot()/researchVisually() (world.js)
  // used to hardcode `premium` for vision calls, on the assumption that
  // whatever wins the coding-benchmark-driven 'premium' pick will also
  // happen to support image input. It doesn't -- once 'premium' became
  // Qwen3-Coder-480B (a real, text-only coding model, correctly chosen for
  // being the best value at CODE), every visual review silently started
  // failing ("vision call failed or returned nothing"). Vision support is
  // a real, catalog-verifiable capability (architecture.input_modalities
  // includes 'image'), completely independent of coding/chat/judgment
  // quality, so it gets its own band instead of piggybacking on whichever
  // model a different benchmark happened to pick.
  vision: { label: 'loading...', slug: 'openai/gpt-4o-mini' },
};

let lastHireAt = 0;

// Real bug caught live (2026-09-21): this fixed 10-name list was the
// ONLY name source, and it was already fully exhausted at 17 real
// hires (7 original + all 10 pool names) -- every hire attempt from
// then on silently returned null and failed, regardless of
// MAX_TOTAL_AGENTS (raised to 500 the same day, which changes nothing
// if hiring can't actually produce a new name). Per your call, names
// are now generated by the same LLM call that already writes the
// onboarding profile (generateHireProfile, one call, not two) -- this
// pool is now only the FALLBACK for when that call fails outright
// (network/model issue), not the everyday path, so it should rarely be
// reached at all. The numbered-suffix loop is a second-level fallback
// for the genuinely rare case where even the pool itself is exhausted
// -- hiring should never hard-stop just because every plain name
// happens to already be taken.
function pickFallbackHireName() {
  const used = new Set(AGENT_ROSTER.map(d => d.name));
  const available = HIRE_NAME_POOL.filter(n => !used.has(n));
  if (available.length > 0) return available[Math.floor(Math.random() * available.length)];
  const base = HIRE_NAME_POOL[Math.floor(Math.random() * HIRE_NAME_POOL.length)];
  for (let i = 2; i < 1000; i++) {
    const candidate = `${base}${i}`;
    if (!used.has(candidate)) return candidate;
  }
  return null; // every plain name AND every numbered variant already taken -- should never happen in practice
}

// Who most needs help right now -- a real Jev call now, not just lowest
// morale: this was flagged from the moment morale-based selection was
// written as "the natural replacement target" once Jev existed, and never
// actually wired until now. Given real evidence (morale, approved/dropped
// counts, report count) rather than one number alone, so it can weigh a
// mixed case the way a human reviewer would, same shape as firing's
// evidence-driven decision.
// Who most needs help right now -- a real Jev call now, not just lowest
// morale: this was flagged from the moment morale-based selection was
// written as "the natural replacement target" once Jev existed, and never
// actually wired until now. Given real evidence (morale, approved/dropped
// counts, report count) rather than one number alone, so it can weigh a
// mixed case the way a human reviewer would, same shape as firing's
// evidence-driven decision.
//
// Per your call: directors/admins need help too, so they're legitimate
// hire targets -- the only person excluded is the one currently doing the
// hiring (you don't hire someone to help the hirer mid-hire). `hirerId`
// is the admin/director running attemptAutoHire; it's excluded from the
// candidate pool but everyone else -- workers, directors, and the other
// administrator -- is eligible to be hired-for.
async function whoNeedsHelp(hirerId) {
  const candidatesList = AGENT_ROSTER.filter(d => d.id !== hirerId);
  const candidates = candidatesList.map(d => {
    const a = AGENTS[d.id];
    const morale = moraleFor(d.id);
    const reportCount = reportsAbout(d.id).length;
    return { id: d.id, description: `${d.name}, ${d.role}. Morale: ${morale}/100. Approved work: ${a.approvedCount}. Dropped work: ${a.droppedCount}. Reports filed against them: ${reportCount}.` };
  });
  if (candidates.length === 0) return null;

  const decision = await requestJevChoice(
    'Pick whoever most needs additional help right now -- weigh morale, workload signals (approved vs. dropped work), and any reports filed against them. Anyone on the roster except the person currently handling the hire may be chosen.',
    candidates
  );
  const picked = candidatesList.find(d => d.id === (decision && decision.choice));
  if (picked) return picked;

  // Fallback -- the original plain "lowest morale" rule, so a Jev outage
  // doesn't mean hiring simply stops functioning.
  let worst = null, worstScore = Infinity;
  for (const def of candidatesList) {
    const score = moraleFor(def.id);
    if (score !== null && score < worstScore) { worstScore = score; worst = def; }
  }
  return worst;
}

// Called periodically (see index.html's main()). Silently no-ops most
// calls -- only actually starts a hire once the cooldown has elapsed, an
// admin exists and is free, and someone needs help. Returns true if a
// hire just started (useful for tests/verification), not the hire itself
// -- the new agent doesn't exist until finishHire() runs.
async function attemptAutoHire() {
  // Per your call that an idle think tank should make no API calls at all:
  // hiring help for a think tank with nothing to do is spend with no
  // possible payoff. whoNeedsHelp() below is a real Jev call, so this
  // check has to come before it, not after.
  if (!thinkTankHasWork()) return false;
  const now = Date.now();
  if (now - lastHireAt < HIRE_COOLDOWN_MS) return false;

  const adminDef = availableAuthority();
  if (AGENT_ROSTER.length >= MAX_TOTAL_AGENTS) {
    noteHireBlockedAtCap(adminDef && adminDef.id);
    return false;
  }

  const admin = adminDef && AGENTS[adminDef.id];
  // Real bug caught live: this never checked offDuty, only busy -- an
  // off-duty admin (not busy, just resting) was treated as available,
  // and finishHire() unconditionally sets visible=true on completion
  // without ever restoring offDuty, leaving her stuck in an impossible
  // offDuty=true/visible=true state, standing wherever she happened to
  // be when the hire started. Per the "larger inventory, only active
  // ones present" model, admin duties should wait for her to actually
  // be on duty, same boundary every other assignment path already draws.
  if (!admin || admin.busy || admin.offDuty) return false;

  // Claimed BEFORE the await below, not after -- whoNeedsHelp() now makes
  // a real async Jev call, so without claiming the cooldown slot up front
  // a second interval tick 5s later could pass every check above and
  // start a SECOND concurrent hire attempt while the first is still
  // waiting on Jev. Released again if nothing actually starts.
  lastHireAt = now;
  const helpFor = await whoNeedsHelp(adminDef.id);
  if (!helpFor) { lastHireAt = 0; return false; }
  if (admin.busy) return false; // the admin took on something else while we were waiting on Jev

  admin.busy = true;
  admin.visible = false;
  admin.inRoom = 'commandcenter';
  // Standing at the middle desk of the shared workstations layout --
  // native room coordinates, not outdoor ones (see ROOM_COLLISIONS.workstations).
  admin.roomX = 315; admin.roomY = 155;
  admin.dir = 'south';

  setTimeout(() => finishHire(adminDef, helpFor), HIRE_DURATION_MS);
  return true;
}

// Real, confirmed bug in the onboarding files this produces: with no
// grounding in what actually exists here, the model defaults to generic,
// plausible-sounding software-team boilerplate -- "push to the develop
// branch," "communicate via the project's Slack channel" -- for tools
// that don't exist anywhere in this think tank. Same failure shape as an
// agent guessing at a nonexistent global instead of checking (the whole
// reason page-probe exists), just at hiring time instead of coding time.
// This is the fix: tell the admin plainly what's real, and forbid
// inventing what isn't, so the instructions it writes are actually
// usable by the person who has to follow them.
const THINK_TANK_REAL_MECHANISMS = `Real facts about how work actually happens in this think tank -- ground the onboarding file in ONLY these, and do not invent a tool or process that isn't listed here (no GitHub, no git branches, no Slack, no ticketing system -- none of that exists): `
  + `code is written and run via real shell commands in a shared sandbox (not committed to any repository); `
  + `a real page-probe can check what a page's DOM/console actually shows right now, which should be trusted over guessing what a variable or function is named; `
  + `research and reviews get filed as real files in a shared Library, not a wiki or ticket; `
  + `agents send each other real in-think tank mail, not chat messages on an external tool.`;

// Per your call: the admin who hires someone should be the one who
// writes their AGENTS.md-equivalent file, not a fixed template -- and if
// there's a specific reason for the hire (helping a specific agent with
// a specific kind of work), that should shape what gets written, not be
// discarded. Falls back to the old templated profile if the model call
// fails or comes back malformed, so a bad response never blocks a hire
// that's already otherwise committed to happening.
// Real fix (2026-09-21): generates the new hire's NAME in this same
// call now, not from a separate fixed pool -- see pickFallbackHireName's
// own comment for why. The model is told exactly which names are
// already taken, but "asked nicely" isn't "verified": a hallucinated or
// ignored constraint here would otherwise silently collide with an
// existing agent's own id the moment finishHire()/hireSpecialist()
// writes AGENTS[id]. Real name is only trusted if it's a plain
// alphabetic string AND genuinely absent from the current roster;
// anything else returns null for that field so the caller falls back to
// pickFallbackHireName() instead of risking a collision.
async function generateHireProfile(admin, role, helpFor) {
  const usedNames = AGENT_ROSTER.map(d => d.name).join(', ');
  const systemPrompt = `You are ${admin.name}, an admin who just hired someone as "${role}" specifically to help ${helpFor.name} (${helpFor.role}) with overflow work. `
    + `Pick a real, ordinary first name for them -- it must NOT be any of these already-used names: ${usedNames}. `
    + `Write their onboarding file, reflecting that specific reason for the hire. Keep every field to one short sentence. ${THINK_TANK_REAL_MECHANISMS} `
    + `Respond with ONLY valid JSON, no other text, no markdown fences, in exactly this shape: `
    + `{"name":"a single real first name, not already used","mission":"one sentence mission statement","instructions":["one or two short operating instructions"],"notes":["one short onboarding note"]}`;
  try {
    const res = await agentFetch('/api/chat', admin.id, {
      method: 'POST',
      body: JSON.stringify({
        // Real call (2026-09-21): picking a name and writing a couple of
        // one-sentence template fields is routine work, not the actual
        // hiring judgment call (that already happened via whoNeedsHelp()'s
        // own Jev call) -- it doesn't need the admin's own 'mid' tier.
        model: MODEL_TIERS.small.slug,
        messages: [{ role: 'system', content: systemPrompt }, { role: 'user', content: 'Write the new hire\'s onboarding file, including their name.' }],
        max_tokens: 300,
        agentId: admin.id,
      }),
    });
    const data = await res.json();
    if (!res.ok || data.error) return null;
    const cleaned = data.reply.trim().replace(/^```json\s*|^```\s*|```\s*$/g, '');
    const parsed = JSON.parse(cleaned);
    if (!parsed.mission || !Array.isArray(parsed.instructions) || !Array.isArray(parsed.notes)) return null;
    const usedSet = new Set(AGENT_ROSTER.map(d => d.name));
    const rawName = (parsed.name || '').trim();
    const validName = /^[A-Za-z]+$/.test(rawName) && !usedSet.has(rawName) ? rawName : null;
    return { mission: parsed.mission, instructions: parsed.instructions, notes: parsed.notes, name: validName };
  } catch (e) {
    return null;
  }
}

async function finishHire(adminDef, helpFor) {
  const admin = AGENTS[adminDef.id];
  admin.busy = false;
  admin.visible = true;
  admin.inRoom = null;

  const role = `Assistant to ${helpFor.name}`;
  const generated = await generateHireProfile(admin, role, helpFor);
  const name = (generated && generated.name) || pickFallbackHireName();
  if (!name) { showToast(`${admin.name} came back empty-handed -- no names left to hire.`, 4000); return; }

  const id = name.toLowerCase();
  const color = HIRE_COLOR_POOL[Math.floor(Math.random() * HIRE_COLOR_POOL.length)];
  const accessGrant = `Read/write access to ${helpFor.name}'s ${helpFor.role} files and tooling.`;

  // generated also carries the `name` field generateHireProfile()
  // returned it with -- stripped out here since profile.name would just
  // be redundant, confusing data sitting next to the real, canonical
  // AGENTS[id].name a few lines below.
  const profile = generated
    ? { mission: generated.mission, instructions: generated.instructions, notes: generated.notes }
    : {
      mission: `Support ${helpFor.name} (${helpFor.role}) with overflow work.`,
      instructions: [
        `Report to ${helpFor.name} -- pick up whatever they flag as overloaded.`,
        'Hired via Command Center -- elevated access is scoped to what you\'re helping with, not think tank-wide.',
      ],
      notes: [`Hired by ${admin.name} via Command Center.`],
    };

  const def = {
    id, name, color, role,
    // Every new hire defaults to the small tier -- overflow/assistant work
    // doesn't need a bigger model, per your call.
    model: 'small',
    approvedCount: 0, droppedCount: 0,
    mailbox: [`Welcome aboard -- you're here to help ${helpFor.name} with ${helpFor.role.toLowerCase()} work.`],
    // Per your call: Command-Center hires get access the original six
    // don't. This is flavor/data for now, not a wired permissions system
    // (that's Phase 3 territory) -- but it's real data other UI can read,
    // not a hardcoded string only shown here.
    elevatedAccess: true,
    accessGrant,
    profile,
  };
  AGENT_ROSTER.push(def);

  const occupied = Object.values(AGENTS).filter(a => a.visible).map(a => ({ x: a.x, y: a.y }));
  const spot = pickFreeSpot(occupied);
  AGENTS[id] = {
    id, name: def.name, color: def.color, role: def.role, profile: def.profile, model: def.model,
    approvedCount: 0, droppedCount: 0, mailbox: def.mailbox.map(text => ({ text, read: false, ts: Date.now() })), conversationLog: [],
    lastContactedAt: null, hiredAt: Date.now(), elevatedAccess: true, accessGrant,
    inRoom: null, roomX: null, roomY: null,
    x: spot.x, y: spot.y, dir: 'south',
    visible: true, busy: false, meetingId: null,
  };

  // Real, reported problem this fixes: a brand-new hire used to just pop
  // into existence at a random valid outdoor spot -- x/y above is only
  // the fallback now. Standing up for the first time should look real,
  // same as everyone else who's ever gone off duty and come back -- but
  // only if there's actually room for her to be active. Per your call on
  // 2026-09-20: a large total inventory (MAX_TOTAL_AGENTS) is fine, but
  // only MAX_ACTIVE_AGENTS may be online/awaiting scheduled work at once.
  // Hiring someone new for overflow help while the think tank is already at
  // its active ceiling means she joins the inventory dormant, not walking
  // out uselessly into an already-full active roster.
  if (canActivateAnother()) {
    spawnAgentAtFreeSpot(id, occupied);
  } else {
    AGENTS[id].offDuty = true;
    AGENTS[id].visible = false;
  }

  showToast(`${admin.name} hired ${name} to help ${helpFor.name}.`, 4000);
  logThinkTankAction(adminDef.id, 'hire', { hired: id, helping: helpFor.id });
}

// A more general hire, built for the finger-drumming project -- the
// existing finishHire() above is specifically shaped around
// attemptAutoHire()'s "someone's overloaded, hire them an assistant"
// flow (fixed role string, always the small tier). Real project roles
// (coder, UI researcher, QA) need a specific role AND a specific default
// model tier picked for what the job actually needs -- coding gets
// 'premium' by default, per your call that agents who code need a
// better model, still subject to pickModelTierForAction() overriding it
// per-action same as anyone else. Still goes through an admin authoring
// the onboarding file for real, same "admins write the AGENTS.md when
// hiring" principle as the original flow.
async function hireSpecialist(adminId, role, model, missionHint) {
  if (AGENT_ROSTER.length >= MAX_TOTAL_AGENTS) {
    noteHireBlockedAtCap(adminId);
    return null;
  }
  const admin = AGENTS[adminId];
  if (!admin) return null;

  // Real fix (2026-09-21): name now comes from this same call (a small-
  // tier model, since picking a name + writing one-sentence template
  // fields is routine work, not a judgment call), checked against the
  // real roster for collisions -- see generateHireProfile's identical
  // reasoning. pickFallbackHireName() only runs if this call fails
  // outright or the model's own name choice doesn't survive validation.
  const usedNames = AGENT_ROSTER.map(d => d.name).join(', ');
  const systemPrompt = `You are ${admin.name}, an admin who just hired someone as "${role}" for a specific project: ${missionHint}. `
    + `Pick a real, ordinary first name for them -- it must NOT be any of these already-used names: ${usedNames}. `
    + `Write their onboarding file. Keep every field to one short sentence. ${THINK_TANK_REAL_MECHANISMS} `
    + `Respond with ONLY valid JSON, no other text, no markdown fences, in exactly this shape: `
    + `{"name":"a single real first name, not already used","mission":"one sentence mission statement","instructions":["one or two short operating instructions"],"notes":["one short onboarding note"]}`;
  let profile = null;
  let generatedName = null;
  try {
    const res = await agentFetch('/api/chat', adminId, {
      method: 'POST',
      body: JSON.stringify({
        model: MODEL_TIERS.small.slug,
        messages: [{ role: 'system', content: systemPrompt }, { role: 'user', content: 'Write the new hire\'s onboarding file, including their name.' }],
        max_tokens: 300,
        agentId: adminId,
      }),
    });
    const data = await res.json();
    if (res.ok && !data.error) {
      const cleaned = data.reply.trim().replace(/^```json\s*|^```\s*|```\s*$/g, '');
      const parsed = JSON.parse(cleaned);
      if (parsed.mission && Array.isArray(parsed.instructions) && Array.isArray(parsed.notes)) {
        profile = { mission: parsed.mission, instructions: parsed.instructions, notes: parsed.notes };
        const usedSet = new Set(AGENT_ROSTER.map(d => d.name));
        const rawName = (parsed.name || '').trim();
        if (/^[A-Za-z]+$/.test(rawName) && !usedSet.has(rawName)) generatedName = rawName;
      }
    }
  } catch (e) { /* fall through to the template/fallback name below */ }

  const name = generatedName || pickFallbackHireName();
  if (!name) return null; // every plain name AND every numbered variant already taken -- should never happen in practice
  const id = name.toLowerCase();
  const color = HIRE_COLOR_POOL[Math.floor(Math.random() * HIRE_COLOR_POOL.length)];

  if (!profile) {
    profile = {
      mission: missionHint,
      instructions: [`Hired specifically for: ${missionHint}`],
      notes: [`Hired by ${admin.name} for the finger-drumming project.`],
    };
  }

  const def = { id, name, color, role, model, approvedCount: 0, droppedCount: 0, mailbox: [`Welcome aboard -- ${missionHint}`], elevatedAccess: false, profile };
  AGENT_ROSTER.push(def);

  const occupied = Object.values(AGENTS).filter(a => a.visible).map(a => ({ x: a.x, y: a.y }));
  const spot = pickFreeSpot(occupied);
  AGENTS[id] = {
    id, name: def.name, color: def.color, role: def.role, profile: def.profile, model: def.model,
    approvedCount: 0, droppedCount: 0, mailbox: def.mailbox.map(text => ({ text, read: false, ts: Date.now() })), conversationLog: [],
    lastContactedAt: null, hiredAt: Date.now(), elevatedAccess: false,
    inRoom: null, roomX: null, roomY: null,
    x: spot.x, y: spot.y, dir: 'south',
    visible: true, busy: false, meetingId: null,
  };

  // See finishHire()'s identical logic -- x/y above is only the fallback,
  // and she only actually appears on the map if there's room for her to be
  // active (MAX_ACTIVE_AGENTS); otherwise she joins the inventory
  // dormant until something calls her in.
  if (canActivateAnother()) {
    spawnAgentAtFreeSpot(id, occupied);
  } else {
    AGENTS[id].offDuty = true;
    AGENTS[id].visible = false;
  }

  showToast(`${admin.name} hired ${name} as ${role}.`, 4000);
  logThinkTankAction(adminId, 'hire', { hired: id, role, model });
  return id;
}
