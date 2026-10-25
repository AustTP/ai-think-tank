// Terminal, mailbox, teller, toast, board, products/wiki/sprints panels.
// Split out of main.js to keep every script under 1,000 lines.

function appendTerminalLine(text, isCmd) {
  const output = document.getElementById('terminalOutput');
  const line = document.createElement('div');
  line.className = 'line' + (isCmd ? ' cmd' : '');
  line.textContent = text;
  output.appendChild(line);
  output.scrollTop = output.scrollHeight;
}

function closeTerminal() {
  state.ui = null;
  document.getElementById('terminalModal').classList.add('hidden');
  clearInteractZone();
}

let activeMailboxThread = null;

function openMailbox(item) {
  activeInteractZone = item.zone;
  state.ui = 'mailbox';
  activeMailboxThread = null;
  renderMailboxRoster();
  renderMailboxThread();
  document.getElementById('mailboxModal').classList.remove('hidden');
}

function closeMailbox() {
  state.ui = null;
  document.getElementById('mailboxModal').classList.add('hidden');
  clearInteractZone();
}

// Bank teller -- no real currency/economy system exists anywhere in
// World (confirmed: nothing tracks a balance), so for now,
// "multiple options," Check Balance and Make a Deposit are honest flavor
// text rather than a fake ledger. Report Irregular Activity is the one
// genuinely mechanical option -- it logs a real, timestamped entry to the
// same action_log every other real action goes through, distinct from
// fileReport()'s AGENTS.md quote-catching mechanic (that one's UI/text is
// specific to catching an agent's own profile claim, which doesn't fit a
// bank report).
let activeTellerIndex = null;

function openTeller(item) {
  activeInteractZone = item.zone;
  activeTellerIndex = item.tellerIndex;
  state.ui = 'teller';
  document.getElementById('tellerTitle').textContent = `Bank -- Teller ${item.tellerIndex + 1}`;
  const output = document.getElementById('tellerOutput');
  output.innerHTML = '';
  appendTellerLine(`-- Teller ${item.tellerIndex + 1} --`);
  appendTellerLine('"Welcome. What can I help you with today?"');
  renderTellerMenu();
  document.getElementById('tellerModal').classList.remove('hidden');
}

function closeTeller() {
  state.ui = null;
  activeTellerIndex = null;
  document.getElementById('tellerModal').classList.add('hidden');
  clearInteractZone();
}

function appendTellerLine(text) {
  const output = document.getElementById('tellerOutput');
  const line = document.createElement('div');
  line.className = 'line';
  line.textContent = text;
  output.appendChild(line);
  output.scrollTop = output.scrollHeight;
}

function renderTellerMenu() {
  const menu = document.getElementById('tellerMenu');
  menu.innerHTML = `
    <button id="tellerBalanceBtn">Check Balance</button>
    <button id="tellerDepositBtn">Make a Deposit</button>
    <button id="tellerReportBtn">Report Irregular Activity</button>
  `;
  document.getElementById('tellerBalanceBtn').addEventListener('click', () => {
    appendTellerLine('> Check Balance');
    appendTellerLine('"Everything looks in order -- no outstanding holds on file."');
  });
  document.getElementById('tellerDepositBtn').addEventListener('click', () => {
    appendTellerLine('> Make a Deposit');
    appendTellerLine('"Deposit recorded. Thanks for banking with us."');
  });
  document.getElementById('tellerReportBtn').addEventListener('click', showTellerReportForm);
}

function showTellerReportForm() {
  const menu = document.getElementById('tellerMenu');
  const others = AGENT_ROSTER.filter(d => d.id);
  const options = ['<option value="">-- general / unspecified --</option>', ...others.map(d => `<option value="${d.id}">${d.name}</option>`)].join('');
  menu.innerHTML = `
    <label style="display:block;width:100%;">Concerning <select id="tellerReportAbout">${options}</select></label>
    <textarea id="tellerReportNote" rows="2" style="width:100%;" placeholder="Describe what looked irregular..."></textarea>
    <div class="terminal-inputline">
      <button id="tellerReportCancelBtn">Cancel</button>
      <button id="tellerReportSubmitBtn">File Report</button>
    </div>
  `;
  document.getElementById('tellerReportCancelBtn').addEventListener('click', renderTellerMenu);
  document.getElementById('tellerReportSubmitBtn').addEventListener('click', () => {
    const about = document.getElementById('tellerReportAbout').value;
    const note = document.getElementById('tellerReportNote').value.trim();
    if (!note) return;
    logThinkTankAction('player', 'bank_report_filed', { tellerIndex: activeTellerIndex, about: about || null, note });
    appendTellerLine('> Report Irregular Activity');
    appendTellerLine(`"Thank you, this has been logged${about ? ' concerning ' + AGENTS[about].name : ''}."`);
    renderTellerMenu();
  });
}

// Defensive cap: the toast is a single fixed-position banner
// with no built-in length limit -- most real dialogue now goes through a
// real, truncated speech bubble instead (agents.js saySpeech), but this stays
// as a backstop for the paths that still fall back to a toast (e.g. a
// handoff line said by an agent who already went off-duty/invisible before
// her line was ready).
const TOAST_MAX_CHARS = 160;
let toastTimer = null;
function showToast(text, ms = 2000) {
  const el = document.getElementById('toast');
  el.textContent = text.length > TOAST_MAX_CHARS ? text.slice(0, TOAST_MAX_CHARS) + '…' : text;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), ms);
}

let busyToastAgent = null; // last agent toasted, so standing next to one doesn't spam every frame
function showBusyToast(agent) {
  if (busyToastAgent === agent.id) return;
  busyToastAgent = agent.id;
  showToast(`${agent.name} is busy right now.`);
}

// House, for the player, is "literally just the board" --
// distinct from Command Center's admin-only hiring use (hiring.js), which
// is a different room entirely, reached only via the HUD button below.
let boardRefreshTimer = null;

function openBoard() {
  if (state.ui || state.location.kind !== 'outside') return;
  state.ui = 'board';
  renderBoard();
  document.getElementById('boardModal').classList.remove('hidden');
  // Cheap (reads the same in-memory TASKS/AGENTS objects everything else
  // already has, no fetch) so keeping this live while open costs nothing.
  boardRefreshTimer = setInterval(renderBoard, 2000);
}

// Real think tank status, not the static placeholder text this used to be --
// straight off TASKS/AGENTS, the same live objects everything else reads,
// not a separate summary someone has to remember to update.
function renderBoard() {
  loadPlanApprovals();
  const body = document.getElementById('boardBody');
  const open = Object.values(TASKS).filter(t => t.status !== 'done');
  const done = Object.values(TASKS).filter(t => t.status === 'done').sort((a, b) => b.createdAt - a.createdAt).slice(0, 5);
  // Peer-approval gate: a deliverable story awaiting two reviewer approvals is
  // neither "in progress" (its author finished) nor done. Show it separately.
  const needsReview = open.filter(t => t.status === 'needs_review');
  const inProgress = open.filter(t => t.status !== 'needs_review');

  const openLines = inProgress.length
    ? inProgress.map(t => {
        const who = AGENTS[t.assignedTo] ? AGENTS[t.assignedTo].name : t.assignedTo;
        const room = ROOMS[t.room] ? ROOMS[t.room].label : t.room;
        const lane = t.lane ? ` <span style="opacity:0.6;font-size:11px;">[${t.lane}]</span>` : '';
        return `<div>&bull; ${t.title} -- ${who} (${room}, ${t.status})${lane}</div>`;
      }).join('')
    : '<div style="opacity:0.6;">Nothing in progress right now.</div>';

  const reviewLines = needsReview.length
    ? needsReview.map(t => {
        const g = t._peerGate || {};
        const done = (g.approvals || 0);
        const total = (g.reviewerIds || []).length;
        // Cross-training affordance (Phase E2a): mark a reviewer who has
        // actually completed work in this story's room -- the author's work is
        // catching a known-craft eye, not just a same-team one.
        const reviewers = (g.reviewerIds || []).map(id => {
          const a = AGENTS[id];
          const known = a && Array.isArray(a.completedRooms) && a.completedRooms.includes(t.room);
          const name = a ? a.name : id;
          return `${name}${known ? ' (✓'+t.room+')' : ''}`;
        }).join(', ');
        const mark = g.closed ? '&check;' : (done >= total ? '&check;' : `(${done}/${total})`);
        return `<div>&bull; <strong>${t.title}</strong> -- awaiting ${total} reviews (${done}/${total}) by ${reviewers} ${mark}</div>`;
      }).join('')
    : '';

  const doneLines = done.length
    ? done.map(t => {
        const who = AGENTS[t.assignedTo] ? AGENTS[t.assignedTo].name : t.assignedTo;
        // Phase E3.4: the player's quality-veto. This board is the player-only
        // House, so the reject affordance is implicitly player-only. Sends a
        // closed story back to its author for rework (re-enters peer review).
        // encodeURIComponent so quotes/odd chars in ids or titles can't break
        // the onclick attribute; vetoStory decodes before use.
        const btn = ` <button class="hud-btn" style="font-size:11px;padding:1px 6px;" onclick="vetoStory('${encodeURIComponent(t.id)}','${encodeURIComponent(t.title)}')">reject</button>`;
        return `<div>&bull; ${t.title} -- ${who}${btn}</div>`;
      }).join('')
    : '<div style="opacity:0.6;">Nothing completed yet.</div>';

  // Phase E3.4 player quality-veto handler (defined after renderBoard; the
  // button onclick above references it via the global scope at click time).
  window.vetoStory = async function (encId, encTitle) {
    const taskId = decodeURIComponent(encId);
    const title = decodeURIComponent(encTitle);
    const reason = prompt(`Send "${title}" back to its author for rework?\n\nOptionally say why (the reason is passed to the author):`, '');
    if (reason === null) return; // cancelled
    const res = await fetch(`/api/intent/story/${encodeURIComponent(taskId)}/reject`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({reason: reason}),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      alert(data.error || 'Could not reject that story.');
      return;
    }
    alert(`Sent back to ${data.author ? (AGENTS[data.author] ? AGENTS[data.author].name : data.author) : 'its author'} for rework. It will need another review before it can close again.`);
    renderBoard();
  };

  // Phase E3.5 spike->triage: promote a completed spike's findings into a real
  // queued deliverable. Player decides; the spike stays an immutable done record.
  window.promoteSpike = async function (encId, encTitle) {
    const taskId = decodeURIComponent(encId);
    const title = decodeURIComponent(encTitle);
    const confirm = confirm(`Promote "${title}" into a real work item?\n\nThe spike's findings will become a queued deliverable, which then needs peer review like any committed story.`);
    if (!confirm) return;
    const res = await fetch(`/api/intent/spike/${encodeURIComponent(taskId)}/promote`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({}),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      alert(data.error || 'Could not promote that spike.');
      return;
    }
    alert(`Queued "${data.queued.title}" (${data.queued.room}) from the spike's findings. The spike remains on record.`);
    renderBoard();
  };

  // Phase E3.5 spike->triage: done spikes (finished investigations) are shown
  // for the player to promote into real work. Computing from live TASKS, like
  // the rest of the board -- no extra endpoint. Only *done* spikes are triage-
  // able; a working one isn't finished (the endpoint 409s on it).
  const spikeTasks = Object.values(TASKS).filter(t => t.taskType === 'spike');
  const doneSpikes = spikeTasks.filter(t => t.status === 'done');
  const spikeLines = doneSpikes.length
    ? doneSpikes.map(t => {
        const finding = (t.note || '').split('. ')[0]; // first sentence, enough to triage by
        const who = AGENTS[t.assignedTo] ? AGENTS[t.assignedTo].name : t.assignedTo;
        const btn = ` <button class="hud-btn" style="font-size:11px;padding:1px 6px;" onclick="promoteSpike('${encodeURIComponent(t.id)}','${encodeURIComponent(t.title)}')">promote</button>`;
        return `<div>&bull; ${t.title} -- ${who} <span style="opacity:0.7;font-size:11px;">("${finding}")</span>${btn}</div>`;
      }).join('')
    : '<div style="opacity:0.6;">No finished spikes to triage.</div>';

  body.innerHTML = `
    <div style="margin-bottom:4px;"><strong>In progress (${inProgress.length})</strong></div>
    ${openLines}
    ${reviewLines ? `<div style="margin:8px 0 4px;"><strong>Awaiting peer review (${needsReview.length})</strong></div>${reviewLines}` : ''}
    <div style="margin:8px 0 4px;"><strong>Recently completed</strong></div>
    ${doneLines}
    <div style="margin:8px 0 4px;"><strong>Spikes to triage (${doneSpikes.length})</strong></div>
    ${spikeLines}
  `;
  // The House is the directors'/admin's workspace -- the role-template library
  // lives here, gated to the acting authority (which availableAuthority()
  // resolves to: admin, else senior-most director). Refresh with the board so
  // an edit made by another session shows up.
  if (typeof renderTemplatesEditor === 'function') renderTemplatesEditor();
  renderSprints();
  renderProducts();
  renderWiki();
}

// Phase C: a read-only sprint dashboard for the House board. Sprints are
// director-authored server-side state; a sprint may only involve teams that
// each have a designated scrum master, and progress is derived from live task
// + queue state (POST /api/intent/sprint, GET /api/intent/sprints). No create/
// edit UI here -- that stays API/chat-driven -- this just lets a director SEE
// what their sprints look like at a glance.
let _productsCache = null;
async function renderProducts() {
  const body = document.getElementById('boardProductsBody');
  if (!body) return;
  try {
    const res = await fetch('/api/intent/products');
    if (!res.ok) { body.innerHTML = '<div style="opacity:0.6;">Products unavailable.</div>'; return; }
    const data = await res.json();
    _productsCache = data.products || {};
  } catch (e) {
    _productsCache = _productsCache || null;
  }
  const products = _productsCache || {};
  const ids = Object.keys(products);
  if (!ids.length) {
    body.innerHTML = '<div style="opacity:0.6;">No products yet. A director can create one via /api/intent/product.</div>';
    return;
  }
  body.innerHTML = ids.map(pid => {
    const p = products[pid] || {};
    const revisions = (p.revisions || []).length;
    const badge = p.status === 'released'
      ? ' <span style="color:#6f6;">(released)</span>'
      : ` <span style="color:#ffb84d;">(${p.status || 'draft'})</span>`;
    const revTxt = revisions ? ` &middot; ${revisions} release${revisions === 1 ? '' : 's'}` : ' &middot; no releases yet';
    const sand = p.sandboxId ? ` &middot; <span style="opacity:0.8;">${p.sandboxId}</span>` : '';
    return `<div style="margin:2px 0;"><strong>${p.name || pid}</strong>${badge}${sand}${revTxt}<br><span style="opacity:0.6; font-size:11px;">${p.summary || ''}</span></div>`;
  }).join('<hr style="opacity:0.2; margin:4px 0;">');
}

let _wikiCache = null;
async function renderWiki() {
  const body = document.getElementById('boardWikiBody');
  if (!body) return;
  try {
    const res = await fetch('/api/intent/wiki');
    if (!res.ok) { body.innerHTML = '<div style="opacity:0.6;">Wiki unavailable.</div>'; return; }
    const data = await res.json();
    _wikiCache = data;
  } catch (e) {
    _wikiCache = _wikiCache || null;
  }
  const data = _wikiCache || { pages: {}, categories: {} };
  const pages = data.pages || {};
  const cats = data.categories || {};
  const ids = Object.keys(pages);
  if (!ids.length) {
    body.innerHTML = '<div style="opacity:0.6;">No wiki pages yet. Directors can write one via /api/intent/wiki/page.</div>';
    return;
  }
  // Group the page list by category, ordered by each category's `order`.
  const byCat = {};
  for (const pid of ids) {
    const rec = pages[pid] || {};
    byCat[rec.category] = byCat[rec.category] || [];
    byCat[rec.category].push({ pid, rec });
  }
  const ordered = Object.entries(byCat).sort((a, b) => {
    const oa = (cats[a[0]] || {}).order || 0;
    const ob = (cats[b[0]] || {}).order || 0;
    return oa - ob;
  });
  body.innerHTML = ordered.map(([cat, list]) => {
    const label = (cats[cat] || {}).label || cat;
    const entries = list.map(({ pid, rec }) =>
      `<div style="margin:1px 0;">&bull; ${rec.title || pid} <span style="opacity:0.6; font-size:11px;">(v${rec.version}, by ${rec.editedBy || '?'})</span></div>`).join('');
    return `<div style="margin:2px 0;"><strong>${label}</strong></div>${entries}`;
  }).join('<hr style="opacity:0.2; margin:4px 0;">');
}

let _sprintsCache = null;
async function renderSprints() {
  const body = document.getElementById('boardSprintsBody');
  if (!body) return;
  try {
    const res = await fetch('/api/intent/sprints');
    if (!res.ok) { body.innerHTML = '<div style="opacity:0.6;">Sprints unavailable.</div>'; return; }
    const data = await res.json();
    _sprintsCache = data.sprints || {};
  } catch (e) {
    _sprintsCache = _sprintsCache || null;
  }
  const sprints = _sprintsCache || {};
  const ids = Object.keys(sprints);
  if (!ids.length) {
    body.innerHTML = '<div style="opacity:0.6;">No sprints yet. A director can seed one via /api/intent/sprint.</div>';
    return;
  }
  body.innerHTML = ids.map(sid => {
    const s = sprints[sid];
    const rec = (s && s.record) || {};
    const p = (s && s.progress) || { total: 0, done: 0, queued: 0, inProgress: 0, pct: 0 };
    const badge = rec.status === 'closed' ? ' <span style="opacity:0.5;">(closed)</span>'
      : ` <span style="color:${p.pct === 100 ? '#6f6' : '#ffb84d'};">${p.done}/${p.total}</span>`;
    const prog = p.total ? `${p.done} done, ${p.inProgress} in progress, ${p.queued} queued (${p.pct}%)` : 'no items';
    const scrum = rec.teamIds && rec.teamIds.length
      ? ` &middot; teams: ${rec.teamIds.join(', ')}` : '';
    // Phase E3.5: the "what landed" digest -- done items, in sprint order.
    // Minimal inline escape so an agent-authored title can't inject markup
    // (titles flow into body.innerHTML below with the whole modal).
    const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const landed = Array.isArray(p.landed) && p.landed.length
      ? `<div style="margin:1px 0 0;opacity:0.75;"><span style="font-size:11px;">Landed:</span> ${p.landed.map(t => `<span style="font-size:11px;">&bull; ${esc(t)}</span>`).join(' ')}</div>` : '';
    return `<div style="margin:2px 0;"><strong>${rec.name || sid}</strong>${badge} &mdash; <span style="opacity:0.8;">${prog}${scrum}</span><br><span style="opacity:0.6; font-size:11px;">${rec.goal || ''}</span>${landed}</div>`;
  }).join('<hr style="opacity:0.2; margin:4px 0;">');
}

function closeBoard() {
  state.ui = null;
  document.getElementById('boardModal').classList.add('hidden');
  clearInterval(boardRefreshTimer);
  boardRefreshTimer = null;
  const door = ROOM_DOOR_TRIGGERS.house;
  state.player.y = door.y + door.h + 6;
}

// Phone Booth -- the red booth on the outdoor map is a dialog-only
// trigger (no room behind it), and the HUD button is the second way in,
// same access pattern as Command Center's convenience button. The booth
// reaches an agent regardless of where they physically are, including
// off duty (asleep) or mid-task and invisible inside a room -- neither of
// which a physical walk-up conversation can ever reach. Reuses the exact
// same conversation UI/log as an in-person chat (openConversation()) per
// the "shared dialog styling, not duplicated" precedent (Phase 19)
// rather than building a second chat interface. Works from any location:
// the modal promises "wherever you are", and a room interior is still a
// place the player can stand.
function openPhoneBooth() {
  if (state.ui) return;
  state.ui = 'phone';
  renderPhoneRoster();
  document.getElementById('phoneModal').classList.remove('hidden');
}

function closePhoneBooth() {
  state.ui = null;
  document.getElementById('phoneModal').classList.add('hidden');
  // Step clear of the booth's trigger so closing does not reopen it next frame.
  const door = ROOM_DOOR_TRIGGERS && ROOM_DOOR_TRIGGERS.phonebooth;
  if (door && state.location.kind === 'outside' && overlaps(state.player, door)) {
    state.player.y = door.y + door.h + 6;
  }
}

function renderPhoneRoster() {
  const body = document.getElementById('phoneRosterBody');
  body.innerHTML = '';
  for (const def of AGENT_ROSTER) {
    const a = AGENTS[def.id];
    if (!a) continue;
    let status = 'available';
    if (a.busy) status = 'unreachable right now';
    else if (a.offDuty) status = 'off duty -- calling wakes them';
    else if (a.task) status = 'mid-task';
    const btn = document.createElement('button');
    btn.className = 'roster-item';
    btn.disabled = a.busy;
    btn.innerHTML = `${def.name}<span class="role">${def.role} · ${status}</span>`;
    btn.addEventListener('click', () => callAgent(def.id));
    body.appendChild(btn);
  }
}

function callAgent(agentId) {
  const a = AGENTS[agentId];
  if (!a || a.busy) return;
  if (a.offDuty) {
    a.offDuty = false;
    a.visible = true;
    logThinkTankAction('player', 'phone_wake', { agentId });
  }
  closePhoneBooth();
  openConversation(agentId, true);
}

// A live view straight off action_log (serve.py's /api/activity) -- the
// same table every real action already writes into, not a second parallel
// record. Dedup collapses consecutive rows sharing the same agent+action+
// details into one line with a count, since the ambient task cycle alone
// produces a steady trickle of near-identical entries (repeated 'decide'
// picks, routine task_assigned/task_completed pairs) that would otherwise
// bury the events actually worth noticing.
let activityRefreshTimer = null;

function dedupActivity(entries) {
  const out = [];
  for (const e of entries) {
    const key = e.agentId + '|' + e.action + '|' + JSON.stringify(e.details);
    const last = out[out.length - 1];
    if (last && last.key === key) {
      last.count++;
      last.oldestTs = e.ts; // still DESC order, so this is the earliest of the run
    } else {
      out.push({ key, agentId: e.agentId, action: e.action, details: e.details, ts: e.ts, oldestTs: e.ts, count: 1 });
    }
  }
  return out;
}

function formatActivityLine(row) {
  const time = new Date(row.ts * 1000).toLocaleTimeString();
  const who = row.agentId || 'system';
  const countSuffix = row.count > 1 ? ` (x${row.count})` : '';
  let detail = '';
  if (row.details && typeof row.details === 'object') {
    detail = ' -- ' + Object.entries(row.details).map(([k, v]) => `${k}=${v}`).join(', ').slice(0, 140);
  }
  return `[${time}] ${who} :: ${row.action}${countSuffix}${detail}`;
}

async function renderActivityFeed() {
  const output = document.getElementById('activityOutput');
  try {
    const res = await apiFetch('/api/activity?limit=100');
    const data = await res.json();
    const rows = dedupActivity(data.entries || []);
    output.innerHTML = '';
    if (rows.length === 0) {
      output.innerHTML = '<div class="line">Nothing logged yet.</div>';
      return;
    }
    for (const row of rows) {
      const line = document.createElement('div');
      line.className = 'line';
      line.textContent = formatActivityLine(row);
      output.appendChild(line);
    }
  } catch (e) {
    output.innerHTML = '<div class="line">Could not load the activity log.</div>';
  }
}

