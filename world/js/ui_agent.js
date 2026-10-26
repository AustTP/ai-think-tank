// Conversations, roster/profile, reports, meetings, and chat UI.
// Split out of main.js.

let activeConversationAgent = null;

// Temporary (2026-10-26): the south outlet is dialog-only while the south and
// winter villages are revisited. Flip this to true to restore the south travel
// button, the winter destination button, and the south "Call a Meeting" button.
// The dialog and "Walk away" stay available either way.
const SOUTH_OUTLET_EXTRAS_ENABLED = false;
const SOUTH_OUTLET_EXTRA_IDS = [
  'southOutletDestinations', 'southOutletTravelBtn', 'southOutletMeetingBtn',
];

function applySouthOutletAvailability() {
  for (const id of SOUTH_OUTLET_EXTRA_IDS) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.hidden = !SOUTH_OUTLET_EXTRAS_ENABLED;
    // The destinations wrapper carries an inline display:flex, which beats the
    // [hidden] UA rule, so hide it explicitly.
    el.style.display = SOUTH_OUTLET_EXTRAS_ENABLED
      ? (id === 'southOutletDestinations' ? 'flex' : '')
      : 'none';
  }
}

// Who the player is currently standing next to (null if no one) -- the
// prompt shows/hides based on this, and the 'b' keydown handler reads it
// to know who to actually open a conversation with.
let nearbyAgentId = null;

// Click-to-follow camera -- click any agent's sprite (outside view only)
// to have the camera smoothly track them instead of the player; click
// again (or click empty ground) to release. Purely a viewing mode: the
// player's own position and movement are completely unaffected, this
// only changes what cameraFor() centers on. cameraFocus starts null and
// is lazily seeded to the player's own position on the first update()
// tick (SPAWN isn't guaranteed settled yet at module-eval time).
let followAgentId = null;
let cameraFocus = null;

// See frame()'s try/catch below -- throttling state for the "don't flood
// the real action log with a per-frame error" guard.
let lastLoopErrorMsg = null;
let lastLoopErrorLoggedAt = 0;

function camXY(zoom, worldW, worldH, focusX, focusY) {
  const visW = VIEW_W / zoom, visH = VIEW_H / zoom;
  const camX = visW >= worldW ? -(visW - worldW) / 2
    : Math.max(0, Math.min(worldW - visW, focusX - visW / 2));
  const camY = visH >= worldH ? -(visH - worldH) / 2
    : Math.max(0, Math.min(worldH - visH, focusY - visH / 2));
  return [camX, camY];
}

function updateCameraFocus(dt) {
  if (!cameraFocus) cameraFocus = { x: state.player.x, y: state.player.y };
  // Follow only makes sense outside -- indoor rooms are small enough that
  // the camera already always centers on the player, and a followed agent
  // working a room task is drawn at its own roomX/roomY, a different
  // coordinate space entirely.
  if (state.location.kind !== 'outside') followAgentId = null;
  const target = (followAgentId && AGENTS[followAgentId] && AGENTS[followAgentId].visible)
    ? AGENTS[followAgentId] : state.player;
  const lerp = Math.min(1, dt * 4);
  cameraFocus.x += (target.x - cameraFocus.x) * lerp;
  cameraFocus.y += (target.y - cameraFocus.y) * lerp;
}

function updateInteractPrompt() {
  const el = document.getElementById('interactPrompt');
  if (nearbyAgentId) {
    el.innerHTML = `<kbd>B</kbd>Talk to ${AGENTS[nearbyAgentId].name}`;
    el.classList.remove('hidden');
  } else {
    el.classList.add('hidden');
  }
}

// viaPhone: true when reached through the Phone Booth rather than by
// walking up -- same conversation UI/log either way (per the "shared
// dialog styling, not duplicated" precedent), just skips the physical
// walk-away-on-close nudge below, which only makes sense if the player
// was actually standing next to them to begin with.
let convoViaPhone = false;

function openConversation(agentId, viaPhone = false) {
  busyToastAgent = null;
  nearbyAgentId = null;
  updateInteractPrompt();
  activeConversationAgent = agentId;
  convoViaPhone = viaPhone;
  state.ui = 'conversation';
  const a = AGENTS[agentId];
  document.getElementById('convoName').textContent = a.name;
  document.getElementById('convoRole').textContent = a.role;
  renderConversationLog();
  document.getElementById('conversationModal').classList.remove('hidden');
  const input = document.getElementById('convoInput');
  input.value = '';
  input.focus();
}

function closeConversation() {
  state.ui = null;
  document.getElementById('conversationModal').classList.add('hidden');
  // Push the player back away from whoever they were talking to -- they're
  // still standing right at contact distance (now solid, see
  // agentBlockedAt()), so without this the very next frame would just
  // reopen the same conversation. Skipped for a phone call -- the player
  // was never physically there, so there's nothing to walk back from.
  const a = AGENTS[activeConversationAgent];
  if (a && !convoViaPhone) {
    const dx = state.player.x - a.x, dy = state.player.y - a.y;
    const dist = Math.hypot(dx, dy) || 1;
    state.player.x += (dx / dist) * 10;
    state.player.y += (dy / dist) * 10;
    clampTo(state.player, GROUND_W, GROUND_H);
  }
  activeConversationAgent = null;
}

// Compaction for the per-agent player chat log: the model only ever sees
// the last few exchanges, and the full log is persisted into agent state on
// every autosave, so bound it like the other decay-scored memory records.
const CONVERSATION_LOG_CAP = 400;

function renderConversationLog() {
  const log = document.getElementById('convoLog');
  log.innerHTML = '';
  const a = AGENTS[activeConversationAgent];
  if (a.conversationLog.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = `Nothing said to ${a.name} yet.`;
    log.appendChild(empty);
  } else {
    for (const entry of a.conversationLog) {
      const line = document.createElement('div');
      line.className = 'msg';
      const from = entry.fromId === 'player' ? 'You' : a.name;
      line.innerHTML = `<span class="from">${from}:</span>${entry.text}`;
      log.appendChild(line);
    }
  }
  log.scrollTop = log.scrollHeight;
}

// The actual AI wiring -- everyone else's job (persistence, hiring, the
// rest of the game) stays exactly as it was; this is the one function
// that now makes a real, billed call through the backend's /api/chat
// proxy (serve.py), using the agent's own assigned model tier
// (hiring.js's MODEL_TIERS) rather than one model for everyone.
async function requestAgentReply(agent, log) {
  const tier = MODEL_TIERS[agent.model] || MODEL_TIERS.small;
  const systemPrompt = `You are ${agent.name}, working as ${agent.role} in a small think tank. `
    + `Your mission: ${agentMission(agent)} `
    + `Stay fully in character: keep replies short (1-3 sentences), casual, and specific to your role. `
    + `Never mention that you are an AI or a language model.`;
  const messages = [
    { role: 'system', content: systemPrompt },
    // Last few turns only -- plenty of context for a short in-character
    // reply, and keeps every call cheap regardless of how long a
    // conversation has run.
    ...log.slice(-8).map(e => ({ role: e.fromId === 'player' ? 'user' : 'assistant', content: e.text })),
  ];
  try {
    const res = await agentFetch('/api/chat', agent.id, {
      method: 'POST',
      body: JSON.stringify({ model: tier.slug, messages, max_tokens: 150, agentId: agent.id }),
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      console.error('Agent reply failed:', data.error);
      return `(${agent.name} seems distracted and doesn't answer.)`;
    }
    return data.reply.trim();
  } catch (e) {
    console.error('Agent reply failed:', e);
    return `(${agent.name} seems distracted and doesn't answer.)`;
  }
}

async function handleConversationSend() {
  const input = document.getElementById('convoInput');
  const text = input.value.trim();
  if (!text) return;
  const agentId = activeConversationAgent;
  const a = AGENTS[agentId];
  const ts = Date.now();
  a.conversationLog.push({ fromId: 'player', text, ts });
  markContacted(agentId, ts);
  input.value = '';
  renderConversationLog();

  // A lightweight "typing" line while the real call is in flight --
  // renderConversationLog() below (called once the reply lands) wipes it
  // along with everything else, so no separate cleanup is needed.
  const log = document.getElementById('convoLog');
  const typing = document.createElement('div');
  typing.className = 'msg';
  typing.innerHTML = `<span class="from">${a.name}:</span>...`;
  log.appendChild(typing);
  log.scrollTop = log.scrollHeight;
  input.disabled = true;
  document.getElementById('convoSendBtn').disabled = true;

  const replyText = await requestAgentReply(a, a.conversationLog);
  a.conversationLog.push({ fromId: agentId, text: replyText, ts: Date.now() });
  a.conversationLog = a.conversationLog.slice(-CONVERSATION_LOG_CAP);

  input.disabled = false;
  document.getElementById('convoSendBtn').disabled = false;
  // Guard: the player may have closed this conversation while the reply
  // was in flight -- only touch the DOM if it's still the open one.
  if (activeConversationAgent === agentId) {
    renderConversationLog();
    input.focus();
  }

  // Agents should be able to update their own notes as
  // they see fit, and a conversation is exactly the kind of moment that
  // might warrant it. Fire-and-forget -- doesn't block the UI, and
  // conversations already work from anywhere on the map (proximity-
  // triggered, not room-gated), so this inherits that same reach without
  // needing its own location check.
  maybeUpdateNotesFromConversation(agentId);
}

async function maybeUpdateNotesFromConversation(agentId) {
  const a = AGENTS[agentId];
  if (!a) return;
  const systemPrompt = `You are ${a.name}, working as ${a.role}. You just had a conversation. `
    + `If anything from it is worth adding to your own working notes, reply with ONE short sentence to add. `
    + `If nothing is worth remembering, reply with exactly: NONE.`;
  try {
    const res = await agentFetch('/api/chat', agentId, {
      method: 'POST',
      body: JSON.stringify({
        model: (MODEL_TIERS[a.model] || MODEL_TIERS.small).slug,
        // Bug: this always ends on the agent's own last
        // reply (an 'assistant' turn), and Nova Micro (and likely others)
        // silently returns null content when a call ends that way instead
        // of on a 'user' turn -- confirmed by isolating the exact same
        // messages with and without a trailing user turn via curl. A
        // final explicit instruction turn fixes it generally, not just
        // for this one call.
        messages: [
          { role: 'system', content: systemPrompt },
          ...a.conversationLog.slice(-4).map(e => ({ role: e.fromId === 'player' ? 'user' : 'assistant', content: e.text })),
          { role: 'user', content: 'Reply now with either one short note-worthy sentence, or exactly NONE.' },
        ],
        max_tokens: 60,
        agentId,
      }),
    });
    const data = await res.json();
    if (!res.ok || data.error) return;
    const note = data.reply.trim();
    if (!note || note.toUpperCase() === 'NONE') return;
    a.profile.notes.push(note);
    if (a.profile.notes.length > 5) a.profile.notes.shift();
    logThinkTankAction(agentId, 'notes_updated', { source: 'conversation', note });
  } catch (e) {
    // best-effort -- a missed note shouldn't affect the conversation itself
  }
}

function renderMailboxRoster() {
  const roster = document.getElementById('mailboxRoster');
  roster.innerHTML = '';
  for (const def of AGENT_ROSTER) {
    const a = AGENTS[def.id];
    const btn = document.createElement('button');
    btn.className = 'roster-item' + (activeMailboxThread === def.id ? ' active' : '');
    const count = a.mailbox.length;
    const unread = a.mailbox.filter(m => !m.read).length;
    const countLabel = unread > 0
      ? `${count} message${count === 1 ? '' : 's'} (${unread} new)`
      : `${count} message${count === 1 ? '' : 's'}`;
    btn.innerHTML = `${def.name}<span class="role">${countLabel}</span>`;
    if (unread > 0) btn.classList.add('has-unread');
    btn.addEventListener('click', () => { activeMailboxThread = def.id; renderMailboxRoster(); renderMailboxThread(); });
    roster.appendChild(btn);
  }
}

function renderMailboxThread() {
  const header = document.getElementById('mailboxThreadHeader');
  const log = document.getElementById('mailboxThreadLog');
  log.innerHTML = '';
  if (!activeMailboxThread) {
    header.textContent = 'Select a mailbox';
    return;
  }
  const a = AGENTS[activeMailboxThread];
  header.textContent = `${a.name}'s inbox`;
  if (a.mailbox.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'Empty.';
    log.appendChild(empty);
  } else {
    for (const msg of a.mailbox) {
      const line = document.createElement('div');
      line.className = 'msg' + (msg.read ? '' : ' unread');
      line.textContent = msg.text;
      log.appendChild(line);
    }
  }
  // Opening a thread IS reading it -- mark everything currently shown as
  // read so the roster badge and HUD count reflect what was actually just
  // seen, then refresh the roster so its badge updates immediately rather
  // than staying stale until the next unrelated re-render.
  if (a.mailbox.some(m => !m.read)) {
    markMailRead(activeMailboxThread);
    renderMailboxRoster();
  }
}

function handleTerminalSend() {
  const input = document.getElementById('terminalInput');
  const text = input.value;
  if (!text.trim()) return;
  appendTerminalLine('> ' + text, true);
  appendTerminalLine('Command not recognized -- nothing is connected to this terminal yet.');
  input.value = '';
}

// Roster/profile access is only offered while just walking around outside
// -- kept simple deliberately, rather than trying to stack this modal on
// top of a room, a meeting, or another modal that's already open.
function openRosterList() {
  if (state.ui || state.location.kind !== 'outside') return;
  state.ui = 'roster';
  renderRosterList();
  document.getElementById('rosterListModal').classList.remove('hidden');
}

function closeRosterList() {
  state.ui = null;
  document.getElementById('rosterListModal').classList.add('hidden');
}

function renderRosterList() {
  document.getElementById('rosterListTitle').textContent = `Think Tank Roster — Morale ${thinkTankMorale()}`;
  const body = document.getElementById('rosterListBody');
  body.innerHTML = '';
  for (const def of AGENT_ROSTER) {
    const btn = document.createElement('button');
    btn.className = 'roster-item';
    btn.innerHTML = `${def.name}<span class="role">${def.role} · Morale ${moraleFor(def.id)}</span>`;
    btn.addEventListener('click', () => openProfile(def.id));
    body.appendChild(btn);
  }
}

function openProfile(agentId) {
  document.getElementById('rosterListModal').classList.add('hidden');
  state.ui = 'profile';
  renderProfile(agentId);
  document.getElementById('profileModal').classList.remove('hidden');
}

function closeProfile() {
  state.ui = null;
  document.getElementById('profileModal').classList.add('hidden');
}

function backToRosterFromProfile() {
  document.getElementById('profileModal').classList.add('hidden');
  state.ui = null;
  openRosterList();
}

function renderProfile(agentId) {
  const a = AGENTS[agentId];
  document.getElementById('profileFilename').textContent = `AGENTS.md — ${a.id}`;

  const body = document.getElementById('profileBody');
  body.innerHTML = '';

  const roleSection = document.createElement('div');
  roleSection.className = 'profile-section';
  const tier = MODEL_TIERS[a.model];
  const modelLine = tier ? ` <span style="opacity:0.6;">(${tier.label})</span>` : '';
  roleSection.innerHTML = `<h4>ROLE</h4><div>${a.role} — ${agentMission(a)}${modelLine}</div>`;
  body.appendChild(roleSection);

  if (a.elevatedAccess) {
    const accessSection = document.createElement('div');
    accessSection.className = 'profile-section';
    accessSection.innerHTML = `<h4>ACCESS</h4><div>${a.accessGrant}</div>`;
    body.appendChild(accessSection);
  }

  const statusSection = document.createElement('div');
  statusSection.className = 'profile-section';
  statusSection.id = 'profileStatusSection';
  body.appendChild(statusSection);
  renderStatusSection(agentId);

  const instrSection = document.createElement('div');
  instrSection.className = 'profile-section';
  instrSection.innerHTML = '<h4>OPERATING INSTRUCTIONS</h4>';
  a.profile.instructions.forEach((line, i) => {
    const row = document.createElement('div');
    row.className = 'profile-instr';
    row.innerHTML = `<span>${i + 1}. ${line}</span> <button class="report-link" data-line="${i}">file report</button>`;
    instrSection.appendChild(row);
  });
  body.appendChild(instrSection);
  instrSection.querySelectorAll('.report-link').forEach(btn => {
    btn.addEventListener('click', () => showReportForm(agentId, a.profile.instructions[+btn.dataset.line]));
  });

  const notesSection = document.createElement('div');
  notesSection.className = 'profile-section';
  notesSection.innerHTML = `<h4>NOTES</h4><ul class="profile-notes">${a.profile.notes.map(n => `<li>${n}</li>`).join('')}</ul>`;
  body.appendChild(notesSection);

  const reportsSection = document.createElement('div');
  reportsSection.className = 'profile-section';
  reportsSection.id = 'profileReportsSection';
  body.appendChild(reportsSection);
  renderReportsSection(agentId);
}

function renderStatusSection(agentId) {
  const a = AGENTS[agentId];
  const section = document.getElementById('profileStatusSection');
  const contact = a.lastContactedAt
    ? `${daysSince(a.lastContactedAt).toFixed(1)} days ago`
    : 'never';
  section.innerHTML = `
    <h4>STATUS</h4>
    <div>Morale: ${moraleFor(agentId)}</div>
    <div>Approved: ${a.approvedCount} &nbsp; Dropped: ${a.droppedCount}</div>
    <div>Last contacted: ${contact}</div>
    <div class="modal-actions" style="justify-content:flex-start; margin-top:6px;">
      <button id="logApprovedBtn">+ Log approved work</button>
      <button id="logDroppedBtn">+ Log dropped work</button>
    </div>
  `;
  document.getElementById('logApprovedBtn').addEventListener('click', () => {
    a.approvedCount++;
    renderStatusSection(agentId);
  });
  document.getElementById('logDroppedBtn').addEventListener('click', () => {
    a.droppedCount++;
    renderStatusSection(agentId);
  });
}

function renderReportsSection(agentId) {
  const section = document.getElementById('profileReportsSection');
  const reports = reportsAbout(agentId);
  section.innerHTML = `<h4>REPORTS (${reports.length})</h4>`;
  if (reports.length === 0) {
    const empty = document.createElement('div');
    empty.style.cssText = 'opacity:0.6;font-size:12px;';
    empty.textContent = 'Nothing filed on this agent.';
    section.appendChild(empty);
  }
  for (const r of reports) {
    const block = document.createElement('div');
    block.className = 'report-block';
    const fromLabel = r.fromId === 'player' ? 'You' : AGENTS[r.fromId].name;
    block.innerHTML = `
      <div>From: ${fromLabel}</div>
      <div class="quote">&gt; ${r.quote}</div>
      <div class="attribution">— ${agentId}/AGENTS.md</div>
      <div class="note">${r.note}</div>
      <div class="caught-stamp">CAUGHT</div>
    `;
    section.appendChild(block);
  }
}

function showReportForm(agentId, quote) {
  const section = document.getElementById('profileReportsSection');
  const existingForm = document.getElementById('reportForm');
  if (existingForm) existingForm.remove();

  const form = document.createElement('div');
  form.className = 'report-form';
  form.id = 'reportForm';

  const others = AGENT_ROSTER.filter(d => d.id !== agentId);
  const options = ['<option value="player">You</option>', ...others.map(d => `<option value="${d.id}">${d.name}</option>`)].join('');

  form.innerHTML = `
    <label>Quoted from ${agentId}/AGENTS.md:<br><em>"${quote}"</em></label>
    <label>Filed by <select id="reportFromSelect">${options}</select></label>
    <label>Note <textarea id="reportNoteInput" rows="2" placeholder="What happened..."></textarea></label>
    <div class="modal-actions">
      <button id="reportCancelBtn">Cancel</button>
      <button id="reportSubmitBtn">File Report</button>
    </div>
  `;
  section.insertBefore(form, section.children[1] || null);

  document.getElementById('reportCancelBtn').addEventListener('click', () => form.remove());
  document.getElementById('reportSubmitBtn').addEventListener('click', () => {
    const fromId = document.getElementById('reportFromSelect').value;
    const note = document.getElementById('reportNoteInput').value;
    fileReport(agentId, fromId, quote, note);
    logThinkTankAction(fromId, 'report_filed', { about: agentId, quote });
    form.remove();
    renderReportsSection(agentId);
    renderStatusSection(agentId); // a new report changes this agent's morale
  });
}

// Reachable two ways now: walking into Town Hall's door trigger (always
// 'outside' by construction when that fires), or the HUD bar's Call
// Meeting button (reachable from anywhere) -- the guard matters for the
// second path, so it's here rather than only at each call site.
function openMeetingUI(fromDoor) {
  if (state.ui || state.location.kind !== 'outside') return;
  state.ui = 'townhall';
  // Which door opened the meeting, so cancelling nudges off THAT door. The
  // south outlet can start a meeting too, and cancel must not teleport the
  // player back to the Town Hall.
  state.meetingDoor = fromDoor || null;
  document.getElementById('meetingMsg').textContent = '';
  renderAgentChecklist();
  document.getElementById('townhallModal').classList.remove('hidden');
}

function closeMeetingUI() {
  state.ui = null;
  document.getElementById('townhallModal').classList.add('hidden');
}

function openDoorMenu() {
  state.ui = 'doorMenu';
  document.getElementById('doorMenuModal').classList.remove('hidden');
}

function closeDoorMenu() {
  state.ui = null;
  document.getElementById('doorMenuModal').classList.add('hidden');
  // Nudge clear of the door tile so a "walk away" (or exit back from the
  // hangout) doesn't instantly re-open the menu next frame -- same idea as
  // handleCancelCall nudging off the townhall door.
  const door = ROOM_DOOR_TRIGGERS && ROOM_DOOR_TRIGGERS.townhall;
  if (door) state.player.y = door.y + door.h + 6;
}

function doorMenuEnterHangout() {
  closeDoorMenu();
  enterRoom('hangout');
}

function doorMenuCallMeeting() {
  closeDoorMenu();
  openMeetingUI('townhall');
}

function renderSouthDestinations() {
  const box = document.getElementById('southOutletDestinations');
  if (!box) return;
  box.innerHTML = '';
  if (!SOUTH_OUTLET_EXTRAS_ENABLED) return;
  for (const id in SCENES) {
    if (id === 'main' || id === 'south') continue;
    const btn = document.createElement('button');
    btn.textContent = 'Walk to the ' + id.replace(/-/g, ' ') + ' village';
    btn.addEventListener('click', () => travelToVillage(id));
    box.appendChild(btn);
  }
}

function travelToVillage(id) {
  const scene = SCENES[id];
  if (!scene || !scene.bg || !scene.grid) {
    showToast('That village is not ready yet.');
    return;
  }
  document.getElementById('southOutletModal').classList.add('hidden');
  state.ui = null;
  switchScene(id);
  showToast('Arrived in the ' + id.replace(/-/g, ' ') + ' village.');
}

function openSouthMenu() {
  if (state.ui) return;
  state.ui = 'southOutlet';
  applySouthOutletAvailability();
  renderSouthDestinations();
  document.getElementById('southOutletModal').classList.remove('hidden');
}

function closeSouthMenu() {
  state.ui = null;
  document.getElementById('southOutletModal').classList.add('hidden');
  // Nudge north off the outlet tile so closing the menu does not immediately
  // re-open it, the same way closeDoorMenu clears the Town Hall door.
  const door = ROOM_DOOR_TRIGGERS && ROOM_DOOR_TRIGGERS.south_outlet;
  if (door) state.player.y = door.y - state.player.h - 6;
}

function southMenuTravel() {
  document.getElementById('southOutletModal').classList.add('hidden');
  state.ui = null;
  switchScene('south');
  showToast('Arrived in the south village.');
}

function southMenuCallMeeting() {
  closeSouthMenu();
  openMeetingUI('south_outlet');
}

function wireSouthOutletMenu() {
  const travel = document.getElementById('southOutletTravelBtn');
  const meet = document.getElementById('southOutletMeetingBtn');
  const cancel = document.getElementById('southOutletCancelBtn');
  if (!travel) return;
  travel.addEventListener('click', southMenuTravel);
  meet.addEventListener('click', southMenuCallMeeting);
  cancel.addEventListener('click', closeSouthMenu);
}

function wireDoorMenu() {
  const enter = document.getElementById('doorMenuHangoutBtn');
  const meet = document.getElementById('doorMenuMeetingBtn');
  const cancel = document.getElementById('doorMenuCancelBtn');
  if (!enter) return;
  enter.addEventListener('click', doorMenuEnterHangout);
  meet.addEventListener('click', doorMenuCallMeeting);
  cancel.addEventListener('click', closeDoorMenu);
}

function renderAgentChecklist() {
  const list = document.getElementById('agentList');
  list.innerHTML = '';
  for (const def of AGENT_ROSTER) {
    const a = AGENTS[def.id];
    const label = document.createElement('label');
    label.className = a.busy ? 'busy' : '';
    label.innerHTML = `<input type="checkbox" value="${def.id}" ${a.busy ? 'disabled' : ''}> ${def.name} — ${def.role}${a.busy ? ' (busy)' : ''}`;
    list.appendChild(label);
  }
}

function renderPresetButtons() {
  const row = document.getElementById('presetRow');
  row.innerHTML = '';
  for (const name in MEETING_PRESETS) {
    const btn = document.createElement('button');
    btn.textContent = name;
    btn.addEventListener('click', () => {
      const ids = new Set(MEETING_PRESETS[name]);
      for (const cb of document.querySelectorAll('#agentList input[type=checkbox]')) {
        cb.checked = ids.has(cb.value) && !cb.disabled;
      }
    });
    row.appendChild(btn);
  }
}

// Suggests, doesn't decide -- same player-in-control principle as
// hiring/firing: this only checks
// boxes; the player still presses Start Call themselves. Iterative "pick one more,
// or stop" Jev calls (same shape as browseTowardGoal's link-following),
// since Jev is a one-of-N classifier, not a pick-a-subset tool.
async function suggestMeetingAttendees(topic) {
  const picked = [];
  const pickedIds = new Set();
  for (let i = 0; i < 4; i++) {
    const candidates = AGENT_ROSTER
      .filter(d => !isAgentBusy(d.id) && !pickedIds.has(d.id))
      .map(d => ({ id: d.id, description: `${d.name}, ${d.role}. ${agentMission(d)}` }));
    if (candidates.length === 0) break;
    candidates.push({ id: 'stop', description: 'No one else needs to be in this meeting.' });

    const already = picked.map(id => AGENTS[id].name).join(', ') || 'no one yet';
    const decision = await requestJevChoice(
      `Suggesting attendees for a Town Hall meeting about: "${topic}". Already suggested: ${already}. Pick the next most relevant person to add, or 'stop' if the list is already complete.`,
      candidates
    );
    const choice = decision && decision.choice;
    if (!choice || choice === 'stop') break;
    pickedIds.add(choice);
    picked.push(choice);
  }
  return picked;
}

async function handleSuggestAttendees() {
  const topic = document.getElementById('meetingTopicInput').value.trim();
  if (!topic) {
    document.getElementById('meetingMsg').textContent = 'Say what the meeting is about first.';
    return;
  }
  const btn = document.getElementById('suggestAttendeesBtn');
  btn.disabled = true;
  document.getElementById('meetingMsg').textContent = 'Thinking...';
  try {
    const suggested = await suggestMeetingAttendees(topic);
    const ids = new Set(suggested);
    for (const cb of document.querySelectorAll('#agentList input[type=checkbox]')) {
      cb.checked = ids.has(cb.value) && !cb.disabled;
    }
    document.getElementById('meetingMsg').textContent = suggested.length
      ? `Suggested: ${suggested.map(id => AGENTS[id].name).join(', ')}. Review and adjust before starting.`
      : 'No one obviously relevant -- pick manually.';
  } finally {
    btn.disabled = false;
  }
}

function handleStartCall() {
  const selected = [...document.querySelectorAll('#agentList input[type=checkbox]:checked')].map(cb => cb.value);
  if (selected.length === 0) {
    document.getElementById('meetingMsg').textContent = 'Pick at least one person to call.';
    return;
  }
  const meeting = startMeeting('player', selected);
  if (!meeting) {
    document.getElementById('meetingMsg').textContent = 'Everyone you picked is already in a call.';
    return;
  }
  closeMeetingUI();
  state.location = { kind: 'meeting', meetingId: meeting.id };
  openChat();
}

// Which thread the chat window is currently showing: null for the whole-
// call group thread, or an agent id for a private DM with that person.
// Resets to the group thread every time a new call starts.
let chatThread = null;

function openChat() {
  chatThread = null;
  renderChatRoster();
  renderChatThread();
  document.getElementById('chatModal').classList.remove('hidden');
  document.getElementById('chatInput').focus();
}

function closeChat() {
  document.getElementById('chatModal').classList.add('hidden');
}

function currentMeeting() {
  return MEETINGS[state.location.meetingId];
}

function renderChatRoster() {
  const roster = document.getElementById('chatRoster');
  roster.innerHTML = '';
  const meeting = currentMeeting();

  const groupBtn = document.createElement('button');
  groupBtn.className = 'roster-item' + (chatThread === null ? ' active' : '');
  groupBtn.innerHTML = `Group<span class="role">${meeting.participants.length} people</span>`;
  groupBtn.addEventListener('click', () => { chatThread = null; renderChatRoster(); renderChatThread(); });
  roster.appendChild(groupBtn);

  for (const pid of meeting.participants) {
    if (pid === 'player') continue;
    const a = AGENTS[pid];
    const btn = document.createElement('button');
    btn.className = 'roster-item' + (chatThread === pid ? ' active' : '');
    btn.innerHTML = `${a.name}<span class="role">${a.role}</span>`;
    btn.addEventListener('click', () => { chatThread = pid; renderChatRoster(); renderChatThread(); });
    roster.appendChild(btn);
  }
}

function labelFor(id) {
  if (id === 'player') return 'You';
  const a = AGENTS[id];
  return a ? a.name : id;
}

function renderChatThread() {
  const meeting = currentMeeting();
  const header = document.getElementById('threadHeader');
  const log = document.getElementById('threadLog');
  log.innerHTML = '';

  const entries = chatThread === null
    ? meeting.groupLog
    : (meeting.dmLogs[dmKey('player', chatThread)] || []);

  header.textContent = chatThread === null ? 'Group' : `Direct message — ${labelFor(chatThread)}`;

  if (entries.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = chatThread === null
      ? 'Nothing said yet.'
      : `Nothing said to ${labelFor(chatThread)} yet.`;
    log.appendChild(empty);
  } else {
    for (const entry of entries) {
      const line = document.createElement('div');
      line.className = 'msg';
      line.innerHTML = `<span class="from">${labelFor(entry.fromId)}:</span>${entry.text}`;
      log.appendChild(line);
    }
  }
  log.scrollTop = log.scrollHeight;
}

// Who replies to a GROUP message -- a real Jev call, the same classifier
// "pick one of N" shape as task assignment/firing, not every participant
// replying at once (which would be both a wall of text in a multi-person
// call and N times the cost per message) and not a fixed "the initiator
// always answers" rule either.
async function requestGroupReply(meeting, text) {
  const candidates = meeting.participants
    .filter(pid => pid !== 'player')
    .map(pid => { const a = AGENTS[pid]; return a ? { id: pid, description: `${a.name}, ${a.role}. ${agentMission(a)}` } : null; })
    .filter(Boolean);
  if (candidates.length === 0) return;

  const decision = await requestJevChoice(
    `In a Town Hall group call, the player just said: "${text}". Pick whichever participant is best suited to respond.`,
    candidates
  );
  let chosenId = decision && decision.choice;
  if (!chosenId) chosenId = candidates[Math.floor(Math.random() * candidates.length)].id;

  const agent = AGENTS[chosenId];
  if (!agent) return;
  const replyText = await requestAgentReply(agent, meeting.groupLog);
  postMessage(meeting.id, chosenId, replyText, null);
}

async function handleChatSend() {
  const input = document.getElementById('chatInput');
  const text = input.value;
  if (!text.trim()) return;
  const meeting = currentMeeting();
  const meetingId = state.location.meetingId;
  const dmTarget = chatThread; // capture before any async gap -- the player could switch threads while a reply is in flight
  postMessage(meetingId, 'player', text, dmTarget);
  input.value = '';
  renderChatThread();
  if (!meeting) return;

  const sendBtn = document.getElementById('chatSendBtn');
  input.disabled = true;
  sendBtn.disabled = true;
  try {
    if (dmTarget === null) {
      await requestGroupReply(meeting, text);
    } else {
      const agent = AGENTS[dmTarget];
      if (agent) {
        const replyText = await requestAgentReply(agent, meeting.dmLogs[dmKey('player', dmTarget)] || []);
        postMessage(meetingId, dmTarget, replyText, 'player');
      }
    }
  } finally {
    input.disabled = false;
    sendBtn.disabled = false;
    // Guard: the call may have ended while the reply was in flight --
    // MEETINGS[meetingId] is gone by then, and there's nothing sane left
    // to render into.
    if (currentMeeting() && currentMeeting().id === meetingId) {
      renderChatThread();
      input.focus();
    }
  }
}

function handleCancelCall() {
  closeMeetingUI();
  // Nudge off the door tile that opened the meeting so closing the modal
  // doesn't immediately re-trigger it next frame -- same idea as exitRoom()
  // placing the player clear of the door they just used.
  const key = state.meetingDoor;
  state.meetingDoor = null;
  const door = key && ROOM_DOOR_TRIGGERS[key];
  if (door) state.player.y = door.y + door.h + 6;
}

function handleEndCall() {
  const occupied = endMeeting(state.location.meetingId);
  const spot = pickFreeSpot(occupied);
  state.player.x = spot.x;
  state.player.y = spot.y;
  state.player.dir = 'south';
  state.location = { kind: 'outside' };
  closeChat();
}

