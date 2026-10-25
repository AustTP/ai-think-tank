// Activity feed, player inbox, charter panel, big-task delegation, plan
// approvals, and publish controls. Split out of main.js.

function openActivity() {
  state.ui = 'activity';
  document.getElementById('activityModal').classList.remove('hidden');
  renderActivityFeed();
  // Cheap (a single local SQLite read, no model/API cost) so a short
  // real-time-feeling refresh interval while the modal's actually open
  // costs nothing -- stopped again on close, not left running in the
  // background.
  activityRefreshTimer = setInterval(renderActivityFeed, 3000);
}

function closeActivity() {
  state.ui = null;
  document.getElementById('activityModal').classList.add('hidden');
  clearInterval(activityRefreshTimer);
  activityRefreshTimer = null;
}

// Player Inbox: the one place agents can genuinely block the
// think tank waiting on YOU -- a director-gated question the agent couldn't
// resolve internally (sim.py _deliver_player_ask). Until this panel existed
// the questions were filed server-side and the player was emailed, but the
// UI to actually answer them was missing, so an awaiting question left the
// agent frozen forever. Open → fetch, poll while open, answer posts back.
let playerInboxRefreshTimer = null;

function openPlayerInbox() {
  state.ui = 'player-inbox';
  document.getElementById('playerInboxModal').classList.remove('hidden');
  renderCharterPanel();
  renderPlayerInbox();
  playerInboxRefreshTimer = setInterval(renderPlayerInbox, 3000);
}

function closePlayerInbox() {
  state.ui = null;
  document.getElementById('playerInboxModal').classList.add('hidden');
  clearInterval(playerInboxRefreshTimer);
  playerInboxRefreshTimer = null;
}

let playerInboxCache = [];

async function refreshPlayerInbox() {
  try {
    const res = await apiFetch('/api/player-inbox');
    if (!res.ok) return;
    const data = await res.json();
    if (data.ok) playerInboxCache = data.messages || [];
  } catch (e) { /* poll errors are transient -- keep the last good cache */ }
}

async function renderCharterPanel() {
  const panel = document.getElementById('charterPanel');
  if (!panel) return;
  let charter = {};
  try {
    const res = await apiFetch('/api/intent/charter');
    if (res.ok) {
      const data = await res.json();
      if (data.ok && data.charter) charter = data.charter;
    }
  } catch (e) { /* poll errors are transient */ }
  const goal = charter.goal || '';
  const interests = (charter.interests || []).join(', ');
  const notes = charter.notes || '';
  panel.innerHTML = `
    <div style="font-weight:bold;color:#7fd0ff;">Your charter — the spine every lane points at</div>
    <div style="opacity:0.7;font-size:11px;margin-top:2px;">Set one direction; the tank plans against it (roadmap, refinement, and every task's instructions).</div>
    <input id="charterGoal" type="text" value="${goal.replace(/"/g, '&quot;')}" placeholder="Goal — the north-star (e.g. ship a research-backed product)" style="width:100%;margin-top:6px;" />
    <input id="charterInterests" type="text" value="${interests.replace(/"/g, '&quot;')}" placeholder="Interests — comma-separated (e.g. research, writing, media)" style="width:100%;" />
    <input id="charterNotes" type="text" value="${notes.replace(/"/g, '&quot;')}" placeholder="Notes for the directors (optional)" style="width:100%;" />
    <div style="display:flex;gap:6px;margin-top:4px;">
      <button id="charterSaveBtn" class="hud-btn">Set charter</button>
      <span id="charterStatus" style="opacity:0.7;font-size:11px;"></span>
    </div>`;
  const btn = document.getElementById('charterSaveBtn');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    const status = document.getElementById('charterStatus');
    status.textContent = 'saving...';
    const body = {
      goal: document.getElementById('charterGoal').value.trim(),
      interests: document.getElementById('charterInterests').value.split(',').map(s => s.trim()).filter(Boolean),
      notes: document.getElementById('charterNotes').value.trim(),
    };
    try {
      const res = await apiFetch('/api/intent/charter', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        status.textContent = 'saved — the tank will plan against this.';
        btn.disabled = false;
      } else {
        const data = await res.json();
        status.textContent = (data && data.error) || 'failed — try again';
        btn.disabled = false;
      }
    } catch (e) {
      status.textContent = 'failed — try again';
      btn.disabled = false;
    }
  });
}

async function renderPlayerInbox() {
  await refreshPlayerInbox();
  const log = document.getElementById('playerInboxLog');
  log.innerHTML = '';
  const awaiting = playerInboxCache.filter(m => m.status === 'awaiting_input' || m.status === 'needs_player');
  const answered = playerInboxCache.filter(m => m.status !== 'awaiting_input' && m.status !== 'needs_player');
  if (playerInboxCache.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'Nothing waiting. Agents only block on you when a director-approved question can\u2019t be answered internally, or when a task is handed to you.';
    log.appendChild(empty);
    return;
  }
  const header = document.createElement('div');
  header.className = 'thread-header';
  if (awaiting.length === 0) {
    header.textContent = 'Nothing waiting on you right now';
  } else {
    const asks = awaiting.filter(m => m.status === 'awaiting_input').length;
    const tasks = awaiting.filter(m => m.status === 'needs_player').length;
    header.textContent = `${asks} question${asks === 1 ? '' : 's'} and ${tasks} task${tasks === 1 ? '' : 's'} waiting on you`;
  }
  log.appendChild(header);
  for (const m of awaiting) appendInboxMessage(log, m, true);
  for (const m of answered) appendInboxMessage(log, m, false);
}

function appendInboxMessage(log, m, waiting) {
  const box = document.createElement('div');
  box.className = 'msg' + (waiting ? ' unread' : '');
  box.style.borderLeft = waiting ? '3px solid #c34f1f' : '3px solid #7fd0ff';
  box.style.paddingLeft = '6px';
  const who = document.createElement('div');
  who.className = 'from';
  if (m.kind === 'player_task') {
    who.textContent = `Assigned to you — ${m.title || 'a task'}`;
    box.appendChild(who);
    if (m.lane) {
      const badge = document.createElement('div');
      badge.style.fontSize = '11px';
      badge.style.opacity = '0.7';
      badge.textContent = `lane: ${m.lane}`;
      box.appendChild(badge);
    }
    const q = document.createElement('div');
    q.style.whiteSpace = 'pre-wrap';
    q.textContent = m.instructions || '';
    box.appendChild(q);
    if (waiting) {
      const row = document.createElement('div');
      row.className = 'thread-input';
      row.style.display = 'flex';
      row.style.gap = '6px';
      const btn = document.createElement('button');
      btn.textContent = 'Mark done \u2014 resume the work behind it';
      const send = async () => {
        btn.disabled = true;
        try {
          const res = await apiFetch(`/api/intent/player-task/${encodeURIComponent(m.taskId)}/complete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
          });
          if (res.ok) await renderPlayerInbox();
          else {
            btn.disabled = false;
            btn.textContent = 'Failed \u2014 try again';
          }
        } catch (e) { btn.disabled = false; }
      };
      btn.addEventListener('click', send);
      row.appendChild(btn);
      box.appendChild(row);
    } else {
      const a = document.createElement('div');
      a.style.opacity = '0.85';
      a.textContent = '\u2713 Done \u2014 anything queued behind this task has resumed.';
      box.appendChild(a);
    }
    log.appendChild(box);
    return;
  }
  if (m.kind === 'card_report') {
    // Completed work the think tank shared with the player: the full card
    // report plus a feedback control. Feedback is ONLY allowed here -- the
    // player rates the work an agent completed and shared, not chat replies.
    who.textContent = `Completed — ${m.title || 'a card'}`;
    box.appendChild(who);
    const meta = document.createElement('div');
    meta.style.fontSize = '11px';
    meta.style.opacity = '0.6';
    meta.textContent = (m.closeReason ? m.closeReason + ' \u00b7 ' : '') + new Date(m.createdAt || Date.now()).toLocaleString();
    box.appendChild(meta);
    const body = document.createElement('div');
    body.style.whiteSpace = 'pre-wrap';
    body.style.fontSize = '12px';
    body.textContent = m.body || '';
    box.appendChild(body);
    if (m.feedback && !m.feedback._editing) {
      const fb = document.createElement('div');
      fb.style.marginTop = '6px';
      fb.style.fontSize = '12px';
      fb.style.opacity = '0.9';
      const ratingTxt = m.feedback.rating ? m.feedback.rating + '/5' : 'no rating';
      fb.textContent = `Your feedback: ${ratingTxt}${m.feedback.comment ? ' \u2014 ' + m.feedback.comment : ''}`;
      box.appendChild(fb);
      const editBtn = document.createElement('button');
      editBtn.textContent = 'Edit feedback';
      editBtn.style.marginTop = '4px';
      editBtn.addEventListener('click', () => {
        m.feedback._editing = true;
        renderPlayerInbox();
      });
      box.appendChild(editBtn);
    } else {
      const wrap = document.createElement('div');
      wrap.style.marginTop = '6px';
      const stars = document.createElement('div');
      stars.style.display = 'flex';
      stars.style.gap = '4px';
      stars.style.marginBottom = '4px';
      let picked = (m.feedback && m.feedback.rating) || 0;
      for (let i = 1; i <= 5; i++) {
        const star = document.createElement('button');
        star.textContent = i <= picked ? '\u2605' : '\u2606';
        star.style.fontSize = '16px';
        star.style.border = 'none';
        star.style.background = 'transparent';
        star.style.cursor = 'pointer';
        star.addEventListener('click', () => { picked = i; renderStars(); });
        stars.appendChild(star);
      }
      const renderStars = () => {
        Array.from(stars.children).forEach((s, idx) => {
          s.textContent = (idx + 1) <= picked ? '\u2605' : '\u2606';
        });
      };
      wrap.appendChild(stars);
      const input = document.createElement('input');
      input.type = 'text';
      input.placeholder = 'Feedback on this work (optional)...';
      input.style.flex = '1';
      input.style.boxSizing = 'border-box';
      if (m.feedback && m.feedback.comment) input.value = m.feedback.comment;
      wrap.appendChild(input);
      const btn = document.createElement('button');
      btn.textContent = m.feedback ? 'Update feedback' : 'Send feedback';
      const send = async () => {
        btn.disabled = true;
        try {
          const res = await apiFetch(`/api/player-inbox/${encodeURIComponent(m.id)}/feedback`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rating: picked || null, comment: input.value.trim() }),
          });
          if (res.ok) await renderPlayerInbox();
          else {
            btn.disabled = false;
            btn.textContent = 'Failed \u2014 try again';
          }
        } catch (e) { btn.disabled = false; }
      };
      btn.addEventListener('click', send);
      wrap.appendChild(btn);
      box.appendChild(wrap);
    }
    log.appendChild(box);
    return;
  }
  const agentName = (AGENT_ROSTER.find(d => d.id === m.agentId) || {}).name || m.agentId;
  who.textContent = `${agentName} — ${m.issueKey || 'ask'}`;
  box.appendChild(who);
  const q = document.createElement('div');
  q.textContent = m.question || '';
  box.appendChild(q);
  if (m.context) {
    const ctx = document.createElement('div');
    ctx.style.opacity = '0.6';
    ctx.style.fontSize = '11px';
    ctx.textContent = m.context;
    box.appendChild(ctx);
  }
  if (waiting) {
    const row = document.createElement('div');
    row.className = 'thread-input';
    row.style.display = 'flex';
    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = 'Your answer unblocks them...';
    input.style.flex = '1';
    const btn = document.createElement('button');
    btn.textContent = 'Answer';
    const send = async () => {
      const answer = input.value.trim();
      if (!answer) return;
      btn.disabled = true;
      try {
        const res = await apiFetch(`/api/player-inbox/${encodeURIComponent(m.id)}/respond`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ answer }),
        });
        if (res.ok) await renderPlayerInbox();
        else {
          btn.disabled = false;
          input.value = '';
          input.placeholder = 'Failed -- try again';
        }
      } catch (e) { btn.disabled = false; }
    };
    btn.addEventListener('click', send);
    input.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
    row.appendChild(input);
    row.appendChild(btn);
    box.appendChild(row);
  } else {
    const a = document.createElement('div');
    a.style.opacity = '0.85';
    a.textContent = `\u2192 ${m.answer || '(no answer recorded)'}`;
    box.appendChild(a);
  }
  log.appendChild(box);
}

async function refreshInboxCount() {
  await refreshPlayerInbox();
  const el = document.getElementById('hudInboxCount');
  if (!el) return;
  const n = playerInboxCache.filter(m => m.status === 'awaiting_input' || m.status === 'needs_player').length;
  el.textContent = n > 0 ? String(n) : '';
  el.style.background = n > 0 ? '#c34f1f' : 'transparent';
}

// Give an admin a large task and let THEM break it down
// and delegate it, rather than you hand-assigning individual TASK_POOL
// entries yourself. See assignBigTask() (tasks.js) for the actual
// breakdown-then-Jev-assign flow -- this is just the UI wiring.
function clearBigTaskPins() {
  ['pinBudgetUsd', 'pinRoles', 'pinConstraints', 'pinMustInclude', 'pinOrder']
    .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
  const pins = document.getElementById('bigTaskPins');
  if (pins) pins.open = false;
}
async function handleBigTaskDelegate() {
  const input = document.getElementById('bigTaskInput');
  const goal = input.value.trim();
  if (!goal) return;
  // Optional pins: only non-empty fields are sent; an all-empty form is the
  // same as not opening the section at all (the tank plans freely).
  const constraints = {};
  const budgetRaw = (document.getElementById('pinBudgetUsd')?.value || '').trim();
  if (budgetRaw) {
    const n = parseFloat(budgetRaw);
    if (Number.isFinite(n) && n > 0) constraints.budgetUsd = n;
  }
  const listify = (id) => (document.getElementById(id)?.value || '')
    .split(',').map(s => s.trim()).filter(Boolean);
  const roles = listify('pinRoles');
  if (roles.length) constraints.roles = roles;
  const must = listify('pinMustInclude');
  if (must.length) constraints.mustInclude = must;
  const order = listify('pinOrder');
  if (order.length) constraints.order = order;
  const neverLines = (document.getElementById('pinConstraints')?.value || '')
    .split('\n').map(s => s.trim()).filter(Boolean);
  if (neverLines.length) constraints.constraints = neverLines;
  const statusEl = document.getElementById('bigTaskStatus');
  const btn = document.getElementById('bigTaskBtn');
  btn.disabled = true;
  statusEl.textContent = 'Breaking this down...';
  try {
    let result;
    if (typeof SIM_STATUS !== 'undefined' && SIM_STATUS && SIM_STATUS.owner === 'server') {
      // Server owns the sim: the browser has no authoritative WORK_QUEUE, so
      // delegation goes through the server intent endpoint, which plans and
      // queues the subtasks server-side and returns the same {admin, subtasks}
      // shape the UI below already expects.
      const payload = { goal };
      if (Object.keys(constraints).length) payload.constraints = constraints;
      const res = await apiFetch('/api/intent/plan-big-task', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      result = await res.json();
      if (!res.ok && !result.error) result.error = 'Could not delegate that right now.';
    } else {
      result = await assignBigTask(goal);
    }
    if (result.error) {
      statusEl.textContent = result.error;
    } else if (result.plan) {
      statusEl.textContent = result.note || 'Plan proposed. Approve it before work is filed.';
      input.value = '';
      clearBigTaskPins();
      loadPlanApprovals();
    } else if (result.staffed) {
      // Server-owned sim, sprint staffing: the ask is filed with a free team,
      // whose scrum master + workers break it into concrete stories at their
      // next planning session (the breakdown ceremony). Nothing is predicted
      // or queued up front -- the team plans its own work, like a real org.
      statusEl.textContent = result.note ||
        `${AGENTS[result.admin] ? AGENTS[result.admin].name : result.admin} filed this with the ${result.teamName || result.team} team to break into stories at its next planning session.`;
      input.value = '';
      clearBigTaskPins();
    } else {
      // Reports what was QUEUED, not what was assigned: assignBigTask no
      // longer assigns inline. The admin plans, the work lands in
      // WORK_QUEUE, and runTaskCycle hands it out to whoever is actually
      // free -- so "assigned" isn't known yet at this point, and reading
      // the old result.assigned here threw a TypeError that the catch
      // below silently turned into "something went wrong."
      const adminName = AGENTS[result.admin] ? AGENTS[result.admin].name : result.admin;
      const lines = result.subtasks.map(s => {
        const schedule = s.notBefore ? `, not before ${new Date(s.notBefore).toLocaleString()}` : '';
        const priority = s.priority && s.priority !== 'normal' ? `, ${s.priority} priority` : '';
        return `${s.title} (${s.room})${s.pair ? ' [pair]' : ''}${schedule}${priority}`;
      });
      statusEl.textContent = `${adminName} split this into ${result.subtasks.length} subtask(s), now queued: ${lines.join('; ')}`;
      input.value = '';
      clearBigTaskPins();
    }
  } catch (e) {
    statusEl.textContent = 'Something went wrong delegating that.';
  }
  btn.disabled = false;
}

let _planRefreshBusy = false;
async function loadPlanApprovals() {
  if (_planRefreshBusy) return;
  _planRefreshBusy = true;
  try {
    const res = await apiFetch('/api/plans?status=pending');
    const data = await res.json();
    renderPlanApprovals(data.plans || []);
  } catch (e) {
    // board is best-effort; a failed read leaves the previous list in place
  } finally {
    _planRefreshBusy = false;
  }
}

function renderPlanApprovals(plans) {
  const box = document.getElementById('planApprovals');
  if (!box) return;
  box.innerHTML = '';
  if (!plans.length) {
    const none = document.createElement('div');
    none.style.opacity = '0.55';
    none.textContent = 'No plans waiting for approval.';
    box.appendChild(none);
    return;
  }
  const title = document.createElement('div');
  title.style.cssText = 'opacity:0.75; margin:2px 0;';
  title.textContent = `Plans waiting for approval (${plans.length})`;
  box.appendChild(title);
  for (const plan of plans) {
    const row = document.createElement('div');
    row.style.cssText = 'border:1px solid rgba(255,255,255,0.18); padding:5px; margin:4px 0; border-radius:3px;';
    const head = document.createElement('div');
    const budget = plan.payload && plan.payload.estimatedBudgetUsd;
    head.textContent = `v${plan.version} [${plan.route}] ${plan.goal}`
      + (budget ? ` -- budget $${Number(budget).toFixed(2)}` : '');
    head.style.cssText = 'font-weight:bold; margin-bottom:3px;';
    row.appendChild(head);
    const meta = document.createElement('div');
    meta.style.cssText = 'opacity:0.65; font-size:11px; margin-bottom:4px;';
    meta.textContent = plan.payload && plan.payload.teamName
      ? `Team: ${plan.payload.teamName}` : 'Shared backlog route';
    row.appendChild(meta);
    const actions = document.createElement('div');
    const approve = document.createElement('button');
    approve.textContent = 'Approve';
    approve.onclick = () => actOnPlan(plan.planId, plan.version, 'approve');
    const reject = document.createElement('button');
    reject.textContent = 'Reject';
    reject.style.marginLeft = '4px';
    reject.onclick = () => actOnPlan(plan.planId, plan.version, 'reject');
    const revise = document.createElement('button');
    revise.textContent = 'Revise';
    revise.style.marginLeft = '4px';
    revise.onclick = () => actOnPlan(plan.planId, plan.version, 'revise');
    actions.append(approve, reject, revise);
    row.appendChild(actions);
    box.appendChild(row);
  }
}

async function actOnPlan(planId, version, action) {
  const statusEl = document.getElementById('bigTaskStatus');
  let body = { version };
  if (action === 'reject') {
    const reason = prompt('Why reject this plan? The planner needs a concrete reason.');
    if (!reason || !reason.trim()) return;
    body.reason = reason.trim();
  }
  if (action === 'revise') {
    const goal = prompt('Revised goal for this plan:', '');
    if (!goal || !goal.trim()) return;
    body.goal = goal.trim();
    const current = (await apiFetch(`/api/plans/${encodeURIComponent(planId)}`).then(r => r.json())).revisions || [];
    const latest = current[current.length - 1];
    body.constraints = (latest && latest.constraints) || {};
    action = 'revise';
  }
  const path = action === 'approve'
    ? `/api/plans/${encodeURIComponent(planId)}/approve`
    : action === 'reject'
      ? `/api/plans/${encodeURIComponent(planId)}/reject`
      : `/api/plans/${encodeURIComponent(planId)}/revise`;
  try {
    const res = await apiFetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      statusEl.textContent = data.error || 'Plan action failed.';
    } else {
      statusEl.textContent = action === 'approve'
        ? (data.note || 'Plan applied.')
        : action === 'reject' ? 'Plan rejected.' : 'Plan revised; review the new version.';
    }
  } catch (e) {
    statusEl.textContent = 'Plan action failed.';
  }
  loadPlanApprovals();
}

// Player-triggered publish. Freezes the think tank's released work (projects,
// wiki pages, skills) and pushes it to the configured private GitHub repo.
// This is a real network push -- nothing leaves the machine unless the player
// explicitly clicks publish, so agents stay scoped to their own work.
async function handlePublish() {
  const statusEl = document.getElementById('publishStatus');
  const btn = document.getElementById('publishBtn');
  if (!confirm('Push the think tank\'s released work (released projects, wiki pages, skills) to your configured private GitHub repo?\n\nThis is a real push to GitHub.')) return;
  btn.disabled = true;
  statusEl.textContent = 'Staging and pushing...';
  try {
    const res = await fetch('/api/intent/publish', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({}),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      statusEl.textContent = 'Publish failed: ' + (data.error || 'unknown error');
      return;
    }
    statusEl.textContent = `Published ${data.entries} group(s) to ${data.repo} (${data.staged.length} path(s)).`;
  } catch (e) {
    statusEl.textContent = 'Something went wrong publishing.';
  } finally {
    btn.disabled = false;
  }
}

