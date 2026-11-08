/* Tank Mission Control client.
 *
 * One WebSocket, sealed live queries, zero polling: each panel names a
 * query from the server's allowlist; the server seals it (AES-GCM
 * capability, 60 s TTL), the panel subscribes with it, and the server
 * pushes fresh rows whenever the underlying tables change. Capabilities
 * renew themselves before expiry. Mirrors the @neon/realtime model
 * (Apache-2.0), rebuilt self-hosted. */
'use strict';

const SCREENS = {
  overview: [
    { query: 'counts', render: renderCounts },
    { query: 'programs', render: renderPrograms },
    { query: 'agents', render: null, limit: 24 },
  ],
  work: [{ query: 'tasks-live', render: renderTasks }],
  queue: [{ query: 'queue', render: null }],
  issues: [{ query: 'issues-open', render: null }],
  activity: [{ query: 'actions', render: null }],
  attention: [
    { query: 'inbox', render: renderAttention },
    { query: 'confirmations', render: renderConfirmations },
  ],
  decisions: [
    { query: 'decisions', render: null },
  ],
  health: [
    { query: 'counts', render: renderCounts },
  ],
};

let ws = null;
let wsWanted = false;
let reconnectMs = 1000;
let currentScreen = 'overview';
const panels = new Map();   // fingerprint -> panel state
const renewTimers = new Map();

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function setLive(on, detail) {
  const dot = document.getElementById('livedot');
  dot.className = 'dot ' + (on ? 'on' : 'off');
  dot.title = detail || (on ? 'realtime connected' : 'disconnected');
}

function setStatus(text) {
  document.getElementById('statusbar').textContent = text;
}

async function seal(query, params) {
  const r = await fetch('/api/mc/seal', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ query, params: params || [] }),
  });
  if (!r.ok) throw new Error(`seal ${query}: HTTP ${r.status}`);
  return r.json();
}

function scheduleRenew(panel) {
  const prev = renewTimers.get(panel.fp);
  if (prev) clearTimeout(prev);
  const wait = Math.max(5_000, (panel.expiresAt - Date.now()) - 15_000);
  renewTimers.set(panel.fp, setTimeout(async () => {
    if (!panels.has(panel.fp)) return;   // screen switched away
    try {
      const sealed = await seal(panel.query, panel.params);
      panel.capability = sealed.capability;
      panel.expiresAt = sealed.expiresAt;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ capability: sealed.capability }));
      }
      scheduleRenew(panel);
    } catch (err) {
      setStatus(`${panel.query} renew failed: ${err.message}`);
      scheduleRenew(panel);
    }
  }, wait));
}

function applyRows(panel, rows) {
  panel.rows = rows || [];
  const body = panel.body;
  body.textContent = '';
  body.dataset.fresh = String(Date.now());
  if (panel.render) {
    panel.render(panel);
    return;
  }
  const view = panel.limit ? panel.rows.slice(0, panel.limit) : panel.rows;
  if (!view.length) {
    body.appendChild(el('p', 'muted', 'no rows'));
    return;
  }
  const cols = Object.keys(view[0]);
  const table = el('table', 'grid');
  const thead = el('thead', null, null);
  const hr = el('tr');
  cols.forEach((c) => hr.appendChild(el('th', null, c)));
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = el('tbody');
  view.forEach((row) => {
    const tr = el('tr');
    cols.forEach((c) => {
      let v = row[c];
      if (v === null || v === undefined) v = '';
      else if (typeof v === 'object') v = JSON.stringify(v);
      else v = String(v);
      if (c === 'details' && v.length > 110) v = v.slice(0, 110) + '…';
      if (c === 'title' && v.length > 90) v = v.slice(0, 90) + '…';
      const td = el('td', null, v);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  body.appendChild(table);
}

function dropPanel(fp) {
  const t = renewTimers.get(fp);
  if (t) clearTimeout(t);
  renewTimers.delete(fp);
  panels.delete(fp);
}

function buildScreen(name) {
  currentScreen = name;
  for (const fp of [...panels.keys()]) dropPanel(fp);
  const mount = document.getElementById('panels');
  mount.textContent = '';
  (SCREENS[name] || []).forEach((spec) => {
    const card = el('section', 'panel');
    card.appendChild(el('h2', null, spec.query));
    const body = el('div', 'panel-body');
    body.appendChild(el('p', 'muted', 'sealing…'));
    card.appendChild(body);
    mount.appendChild(card);
    seal(spec.query, spec.params).then((sealed) => {
      if (currentScreen !== name) return;
      const panel = {
        query: spec.query,
        params: spec.params || [],
        render: spec.render,
        limit: spec.limit,
        capability: sealed.capability,
        fp: sealed.queryFingerprint,
        expiresAt: sealed.expiresAt,
        rows: [],
        body,
      };
      panels.set(panel.fp, panel);
      scheduleRenew(panel);
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ capability: panel.capability }));
      }
    }).catch((err) => {
      if (currentScreen !== name) return;
      body.textContent = '';
      body.appendChild(el('p', 'muted', 'seal failed: ' + err.message));
    });
  });
}

function connect() {
  wsWanted = true;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/api/mc/ws`);
  ws.onopen = () => {
    setLive(true);
    reconnectMs = 1000;
    setStatus('');
    for (const panel of panels.values()) {
      ws.send(JSON.stringify({ capability: panel.capability }));
    }
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'error') {
      setStatus('query error: ' + msg.detail);
      return;
    }
    const panel = panels.get(msg.fp);
    if (!panel) return;
    if (msg.type === 'snapshot' || msg.type === 'update') {
      applyRows(panel, msg.rows);
    }
  };
  ws.onclose = () => {
    setLive(false, 'reconnecting…');
    if (wsWanted) {
      setTimeout(connect, reconnectMs);
      reconnectMs = Math.min(reconnectMs * 2, 15_000);
    }
  };
  ws.onerror = () => { try { ws.close(); } catch { /* noop */ } };
}

/* --- panel renderers --- */
function renderCounts(panel) {
  const row = panel.rows[0] || {};
  if (currentScreen === 'health') { renderHealth(panel); }
  const tiles = el('div', 'tiles');
  const labels = {
    liveTasks: 'live tasks', stuckTasks: 'stuck',
    doneTasks: 'done (live)', queuedItems: 'queued',
    openIssues: 'open issues',
  };
  for (const [key, label] of Object.entries(labels)) {
    const tile = el('div', 'tile');
    tile.appendChild(el('div', 'num', String(row[key] ?? '—')));
    tile.appendChild(el('div', 'label', label));
    tiles.appendChild(tile);
  }
  panel.body.appendChild(tiles);
}

function renderPrograms(panel) {
  panel.rows.forEach((p) => {
    const card = el('div', 'program');
    card.appendChild(el('h3', null, `${p.id} — ${p.title || ''}`));
    let pieceStatus = p.piecestatus ?? p.pieceStatus;
    if (typeof pieceStatus === 'string') {
      try { pieceStatus = JSON.parse(pieceStatus); } catch { pieceStatus = {}; }
    }
    const line = el('div', 'pieces');
    for (const [title, st] of Object.entries(pieceStatus || {})) {
      line.appendChild(el('span', 'chip st-' + String(st).replace(/[^a-z]/g, ''),
        `${st}: ${String(title).slice(0, 40)}`));
    }
    card.appendChild(line);
    const flags = [
      p.status,
      p.planningcardminted ? 'plan' : null,
      p.meetingcardminted ? 'meeting' : null,
      p.piecesminted ? 'pieces' : null,
      p.crrequested ? 'CR' : null,
    ].filter(Boolean);
    card.appendChild(el('div', 'muted', flags.join(' · ')));
    panel.body.appendChild(card);
  });
  if (!panel.rows.length) {
    panel.body.appendChild(el('p', 'muted', 'no programs'));
  }
}

function renderTasks(panel) {
  const byStatus = {};
  panel.rows.forEach((r) => {
    const st = r.status || 'unknown';
    (byStatus[st] ||= []).push(r);
  });
  for (const status of ['working', 'walking', 'repairing', 'needs_review',
    'blocked', 'failed', 'queued', 'investigating']) {
    const rows = byStatus[status];
    if (!rows || !rows.length) continue;
    const group = el('details', 'group');
    group.open = ['working', 'blocked', 'failed', 'repairing'].includes(status);
    group.appendChild(el('summary', null, `${status} (${rows.length})`));
    const grid = el('div', 'tasklist');
    rows.slice(0, 40).forEach((r) => {
      const item = el('div', 'task st-' + status.replace(/[^a-z]/g, ''));
      item.appendChild(el('span', 'who', r.assignedto || '—'));
      item.appendChild(el('span', 'what', r.title || r.id));
      grid.appendChild(item);
    });
    group.appendChild(grid);
    panel.body.appendChild(group);
  }
}

document.querySelectorAll('#screens button').forEach((b) => {
  b.addEventListener('click', () => {
    document.querySelectorAll('#screens button').forEach((x) =>
      x.classList.remove('on'));
    b.classList.add('on');
    buildScreen(b.dataset.screen);
  });
});

setInterval(() => {
  const clock = document.getElementById('clock');
  if (clock) clock.textContent = new Date().toISOString().slice(11, 19) + 'Z';
}, 1000);

buildVillageSwitcher();
buildScreen('overview');
connect();

/* --- attention: waiting on you --- */
function renderAttention(panel) {
  panel.body.textContent = '';
  if (!panel.rows.length) {
    panel.body.appendChild(el('p', 'muted',
      'nothing is waiting on you right now'));
    return;
  }
  panel.rows.forEach((m) => {
    const card = el('div', 'ask');
    card.appendChild(el('div', null, m.title || m.question || m.id));
    if (m.body) {
      const b = el('div', 'muted', String(m.body).slice(0, 220));
      card.appendChild(b);
    }
    const meta = el('div', 'muted', `${m.agentid || ''} · ${m.status || ''}`);
    card.appendChild(meta);
    const actions = el('div', 'ask-actions');
    const input = el('input');
    input.placeholder = 'your answer…';
    actions.appendChild(input);
    const btn = el('button', null, 'respond');
    btn.addEventListener('click', async () => {
      if (!input.value) return;
      const r = await fetch(`/api/player-inbox/${encodeURIComponent(m.id)}/respond`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answer: input.value }),
      });
      setStatus(r.ok ? 'answer sent' : `respond failed: HTTP ${r.status}`);
    });
    actions.appendChild(btn);
    card.appendChild(actions);
    panel.body.appendChild(card);
  });
}

function renderConfirmations(panel) {
  panel.body.textContent = '';
  if (!panel.rows.length) {
    panel.body.appendChild(el('p', 'muted', 'no pending confirmations'));
    return;
  }
  panel.rows.forEach((c) => {
    const card = el('div', 'ask');
    card.appendChild(el('div', null, `${c.action} — ${c.actor}`));
    card.appendChild(el('div', 'muted', `expires ${c.expiresat || c.expiresAt}`));
    const actions = el('div', 'ask-actions');
    const phrase = el('input');
    phrase.placeholder = 'confirmation phrase…';
    actions.appendChild(phrase);
    const btn = el('button', null, 'confirm');
    btn.addEventListener('click', async () => {
      if (!phrase.value) return;
      const r = await fetch(`/api/confirmations/${encodeURIComponent(c.id)}/confirm`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phrase: phrase.value }),
      });
      setStatus(r.ok ? 'confirmed' : `confirm failed: HTTP ${r.status}`);
    });
    actions.appendChild(btn);
    card.appendChild(actions);
    panel.body.appendChild(card);
  });
}

/* --- task drawer: the trail of one card --- */
let drawerPanel = null;

async function openTaskDrawer(task) {
  let host = document.getElementById('taskdrawer');
  if (!host) {
    host = el('div', null);
    host.id = 'taskdrawer';
    document.body.appendChild(host);
  }
  host.textContent = '';
  host.appendChild(el('h2', null, task.title || task.id));
  host.appendChild(el('div', 'muted',
    `${task.id} · ${task.status} · ${task.assignedto || task.assignedTo || ''}`));
  const body = el('div', 'drawer-body');
  host.appendChild(body);
  host.appendChild(el('button', 'close', 'close'));
  host.querySelector('button.close').addEventListener('click', () => {
    host.remove();
    if (drawerPanel) { dropPanel(drawerPanel.fp); drawerPanel = null; }
  });
  host.classList.add('open');
  try {
    const detailSealed = await seal('task-detail', [task.id]);
    const dws = ws && ws.readyState === WebSocket.OPEN ? ws : null;
    if (dws) {
      const detailRows = await new Promise((resolve) => {
        const handler = (ev) => {
          let m; try { m = JSON.parse(ev.data); } catch { return; }
          if (m.fp === detailSealed.queryFingerprint &&
              (m.type === 'snapshot' || m.type === 'update')) {
            ws.removeEventListener('message', handler);
            resolve(m.rows || []);
          }
        };
        ws.addEventListener('message', handler);
        dws.send(JSON.stringify({ capability: detailSealed.capability }));
        setTimeout(() => resolve([]), 4000);
      });
      const d = (detailRows && detailRows[0]) || {};
      const facts = el('div', 'drawer-facts');
      const spend = d.ownSpendUsd != null ? `$${(+d.ownSpendUsd).toFixed(4)}`
        : (d.budgetUsd != null ? `budget $${d.budgetUsd}` : '—');
      facts.appendChild(el('div', null,
        `spend: ${spend}${d.budgetUsd != null && d.ownSpendUsd != null ? ` / budget $${d.budgetUsd}` : ''}`));
      if (d.statusnote || d.note) {
        facts.appendChild(el('div', 'muted',
          String(d.statusnote || d.note).slice(0, 300)));
      }
      host.appendChild(facts);
    }
  } catch { /* detail is best-effort; the log trail still shows */ }
  const sealed = await seal('task-log', [task.id]);
  const panel = {
    query: 'task-log', params: [task.id], render: null,
    capability: sealed.capability, fp: sealed.queryFingerprint,
    expiresAt: sealed.expiresAt, rows: [], body,
  };
  if (drawerPanel) dropPanel(drawerPanel.fp);
  drawerPanel = panel;
  panels.set(panel.fp, panel);
  scheduleRenew(panel);
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ capability: panel.capability }));
  }
}

/* --- the map --- */
let mapBuilt = false;

async function buildMap() {
  const mount = document.getElementById('panels');
  mount.textContent = '';
  if (mapBuilt) return;
  const m = await (await fetch('/mc/map.json')).json();
  const stage = el('div', 'map');
  stage.style.position = 'relative';
  stage.style.height = '420px';
  const nodes = new Map(m.nodes.map((n) => [n.id, n]));
  m.nodes.forEach((n) => {
    const card = el('div', 'map-node kind-' + n.kind);
    card.style.left = n.x + 'px';
    card.style.top = n.y + 'px';
    card.style.width = (n.w || 130) + 'px';
    card.appendChild(el('div', 'map-title', n.title));
    if (n.detail) {
      const d = el('div', 'map-detail muted', n.detail);
      d.style.display = 'none';
      card.appendChild(d);
      card.addEventListener('click', () => {
        d.style.display = d.style.display === 'none' ? 'block' : 'none';
      });
    }
    stage.appendChild(card);
  });
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.style.position = 'absolute';
  svg.style.inset = '0';
  svg.style.width = '100%';
  svg.style.height = '100%';
  svg.setAttribute('pointer-events', 'none');
  m.edges.forEach((e) => {
    const a = nodes.get(e.from), b = nodes.get(e.to);
    if (!a || !b) return;
    const x1 = a.x + (a.w || 130) + 8, y1 = a.y + (a.h || 64) / 2;
    const x2 = b.x - 8, y2 = b.y + (b.h || 64) / 2;
    const path = document.createElementNS(
      'http://www.w3.org/2000/svg', 'path');
    const mid = (x1 + x2) / 2;
    path.setAttribute('d', `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`);
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', e.dashed ? 'var(--muted)' : 'var(--accent)');
    path.setAttribute('stroke-dasharray', e.dashed ? '4 4' : 'none');
    svg.appendChild(path);
    if (e.label) {
      const t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      t.setAttribute('x', mid);
      t.setAttribute('y', (y1 + y2) / 2 - 6);
      t.setAttribute('fill', 'var(--muted)');
      t.setAttribute('font-size', '10');
      t.textContent = e.label;
      svg.appendChild(t);
    }
  });
  stage.appendChild(svg);
  mount.appendChild(stage);
  mapBuilt = true;
}

// hook the map screen into the screen switcher
const origBuildScreen = buildScreen;
buildScreen = function (name) {
  if (name === 'map') {
    for (const fp of [...panels.keys()]) dropPanel(fp);
    currentScreen = name;
    document.getElementById('panels').textContent = '';
    buildMap();
    return;
  }
  mapBuilt = false;
  origBuildScreen(name);
};
