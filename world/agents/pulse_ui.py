"""Player-facing Ops page: pulse desk and project board."""

PULSE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tank Ops</title>
<style>
:root{--bg:#0b0f14;--panel:#121922;--line:#223042;--text:#d7e2ef;--muted:#7f93a8;--accent:#57d1c9;--warn:#f2b84b;--bad:#ff7b72;--good:#76d275}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}
header{display:flex;align-items:center;gap:14px;padding:14px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:rgba(11,15,20,.94);backdrop-filter:blur(6px);z-index:5}
h1{font-size:16px;letter-spacing:.14em;margin:0;color:var(--accent);font-weight:600}
.sub{color:var(--muted);font-size:12px}
header .spacer{flex:1}
button,a.btn{background:#16222f;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:7px 11px;font:inherit;text-decoration:none;cursor:pointer}
button:hover,a.btn:hover{border-color:var(--accent)}
main{padding:18px 20px 40px;max-width:1400px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
section{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}
h2{font-size:12px;letter-spacing:.12em;color:var(--muted);margin:0 0 10px;text-transform:uppercase}
pre{white-space:pre-wrap;background:#0c131b;border:1px solid var(--line);border-radius:6px;padding:10px;color:var(--text);font-size:12px;margin:0 0 12px;max-height:220px;overflow:auto}
.card{border:1px solid var(--line);border-radius:6px;padding:10px;margin:0 0 8px;background:#0e1620}
.card .top{display:flex;gap:8px;align-items:baseline;justify-content:space-between}
.card .title{font-weight:600;overflow:hidden;text-overflow:ellipsis}
.badge{font-size:11px;border:1px solid var(--line);border-radius:99px;padding:2px 7px;color:var(--muted);white-space:nowrap}
.badge.open{color:var(--accent);border-color:#2b5a58}
.badge.done{color:var(--good);border-color:#2f5a35}
.badge.bad{color:var(--bad);border-color:#5a2f2f}
.meta{color:var(--muted);font-size:12px;margin-top:4px;display:flex;gap:12px;flex-wrap:wrap}
.event{border-left:2px solid var(--line);padding:4px 0 4px 8px;margin:0 0 6px;color:var(--muted);font-size:12px}
.event b{color:var(--text)}
.trace{border-left:2px solid var(--line);padding:6px 0 6px 10px;margin:0 0 8px}
.trace .top{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.trace .kind{font-size:11px;border:1px solid var(--line);border-radius:99px;padding:1px 6px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.trace .kind.action{color:var(--accent);border-color:#2b5a58}
.trace .kind.thought{color:var(--warn);border-color:#5a4a2f}
.trace .kind.decision{color:var(--good);border-color:#2f5a35}
.trace .summary{color:var(--muted);font-size:12px;white-space:pre-wrap;margin-top:3px}
.trace .detail{color:var(--muted);font-size:11px;white-space:pre-wrap;margin-top:3px;max-height:120px;overflow:auto}
.empty{color:var(--muted);font-size:12px}
.err{color:var(--bad)}
footer{color:var(--muted);font-size:11px;margin-top:18px}
</style></head><body>
<header>
  <h1>TANK OPS</h1><span class="sub">pulse desk / project board</span>
  <span class="spacer"></span>
  <button id="refresh">Refresh</button>
  <a class="btn" href="/">World</a>
</header>
<main>
  <div id="error" class="err"></div>
  <div class="grid">
    <section>
      <h2>Pulse brief</h2>
      <pre id="brief">Loading...</pre>
      <h2>Watches</h2>
      <div id="watches" class="empty">Loading...</div>
      <h2>Recent events</h2>
      <div id="events" class="empty">Loading...</div>
    </section>
    <section>
      <h2>Open cards</h2>
      <div id="board" class="empty">Loading...</div>
    </section>
    <section>
      <h2>Browser sessions</h2>
      <div class="meta" style="margin-bottom:10px">
        <input id="sessAgent" placeholder="agent id" autocomplete="off"
               style="flex:1;min-width:120px;background:#0c131b;color:inherit;border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit">
        <button id="sessStart">Start session</button>
      </div>
      <div id="sessions" class="empty">Loading...</div>
      <div class="meta" style="margin-top:10px">Takeover opens the live browser in a new tab. Hand back before the agent continues.</div>
    </section>
  </div>
  <section style="margin-top:16px">
    <h2>Agent traces</h2>
    <div class="meta" style="margin-bottom:10px">
      <select id="traceAgent" style="min-width:240px;background:#0c131b;color:inherit;border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit"></select>
      <button id="traceRefresh">Load trace</button>
      <label style="color:var(--muted)"><input id="traceAuto" type="checkbox" checked> auto</label>
    </div>
    <div id="traceMeta" class="meta"></div>
    <div id="traces" class="empty">Loading agents...</div>
  </section>
  <footer id="stamp"></footer>
</main>
<script>
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
function card(top, title, meta, badgeText, badgeClass) {
  const c = el('div','card');
  const t = el('div','top'); t.appendChild(el('div','title',title));
  if (badgeText) t.appendChild(el('span','badge '+(badgeClass||''),badgeText));
  c.appendChild(t);
  if (meta) c.appendChild(el('div','meta',meta));
  return c;
}
function renderPulse(data) {
  $('brief').textContent = data.brief || 'No brief.';
  const w = $('watches'); w.textContent = '';
  if (!data.watches || !data.watches.length) { w.className='empty'; w.textContent='No watches yet. Ask an agent to create one with pulse_watch.'; }
  else {
    w.className='';
    data.watches.forEach(x => {
      const age = x.lastSeenTs ? Math.max(0, Math.round((Date.now()-x.lastSeenTs)/60000))+'m ago' : 'no events yet';
      w.appendChild(card(null, x.label, 'source '+x.source+(x.match ? ' / match "'+x.match+'"' : '')+' / '+age, 'watch', 'open'));
    });
  }
  const e = $('events'); e.textContent = '';
  if (!data.events || !data.events.length) { e.className='empty'; e.textContent='No matched events yet.'; }
  else {
    e.className='';
    data.events.slice(0,40).forEach(x => {
      const row = el('div','event');
      row.appendChild(el('b',null,x.label+' '));
      row.appendChild(document.createTextNode(x.summary+'  '));
      row.appendChild(el('span',null,new Date(x.ts).toLocaleString()));
      e.appendChild(row);
    });
  }
}
function renderBoard(data) {
  const b = $('board'); b.textContent = '';
  if (!data.tasks || !data.tasks.length) { b.className='empty'; b.textContent='No open cards.'; return; }
  b.className='';
  data.tasks.forEach(t => {
    const meta = [t.teamId && ('team '+t.teamId), t.assignedTo && ('owner '+t.assignedTo), t.projectLabel && ('project '+t.projectLabel), 'budget $'+(t.budgetUsd||0), 'spent $'+(t.spentUsd||0)].filter(Boolean).join(' / ');
    const c = card(null, t.title, meta, t.status, t.status === 'done' ? 'done' : 'open');
    if (t.reportPath) c.appendChild(el('div','meta', 'report '+t.reportPath+(t.artifactExists ? ' (on disk)' : ' (missing)')));
    b.appendChild(c);
  });
}
async function getJSON(url) {
  const r = await fetch(url, {credentials:'same-origin'});
  if (r.status === 401) throw new Error('Session expired. Sign in at /.');
  if (!r.ok) throw new Error('Request failed: '+r.status);
  return r.json();
}
async function postJSON(url, body) {
  const r = await fetch(url, {method:'POST', credentials:'same-origin',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  let data = {};
  try { data = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(data.error || ('Request failed: '+r.status));
  return data;
}
function sessRow(s) {
  const c = el('div','card');
  const t = el('div','top');
  t.appendChild(el('div','title', s.agentId));
  const badge = s.takeover ? 'takeover' : s.status;
  t.appendChild(el('span','badge '+(s.takeover ? 'bad' : (s.status === 'running' ? 'done' : '')), badge));
  c.appendChild(t);
  const age = Math.max(0, Math.round((Date.now()-(s.lastUsed||s.startedAt))/60000));
  c.appendChild(el('div','meta', 'idle '+age+'m / port '+s.port));
  const row = el('div','meta');
  const mk = (label, fn, cls) => { const b = el('button', cls, label); b.onclick = fn; row.appendChild(b); };
  mk('Take over', async () => {
    try {
      await postJSON('/api/browser-session/takeover', {agentId:s.agentId});
      window.open('/api/browser-session/view?token='+encodeURIComponent(s.token), '_blank');
      loadSessions();
    } catch(e) { alert(e.message); }
  });
  if (s.takeover) mk('Hand back', async () => { try { await postJSON('/api/browser-session/handback', {agentId:s.agentId}); loadSessions(); } catch(e) { alert(e.message); } });
  mk('Stop', async () => { try { await postJSON('/api/browser-session/stop', {agentId:s.agentId}); loadSessions(); } catch(e) { alert(e.message); } });
  c.appendChild(row);
  return c;
}
async function loadSessions() {
  const box = $('sessions');
  try {
    const data = await getJSON('/api/browser-session/list');
    box.textContent = '';
    if (!data.sessions || !data.sessions.length) { box.className='empty'; box.textContent='No sessions.'; return; }
    box.className='';
    data.sessions.forEach(s => box.appendChild(sessRow(s)));
  } catch (e) { box.className='empty'; box.textContent = e.message; }
}
let traceAgentId = '';
let traceTimer = null;
function traceRow(x) {
  const row = el('div','trace');
  const top = el('div','top');
  top.appendChild(el('span','kind '+(x.kind||''), x.kind||'event'));
  top.appendChild(el('b',null,x.action||x.tool||'event'));
  if (x.tool) top.appendChild(el('span','badge',x.tool));
  if (x.confidence !== undefined && x.confidence !== null) top.appendChild(el('span','badge','conf '+x.confidence));
  top.appendChild(el('span','badge', new Date(x.ts*1000).toLocaleString()));
  row.appendChild(top);
  if (x.taskId) row.appendChild(el('div','meta','task '+x.taskId));
  if (x.summary) row.appendChild(el('div','summary',x.summary));
  if (x.text && x.text !== x.summary) row.appendChild(el('div','detail',x.text));
  else if (x.details) row.appendChild(el('div','detail', typeof x.details === 'string' ? x.details : JSON.stringify(x.details)));
  return row;
}
async function loadAgents() {
  const select = $('traceAgent');
  try {
    const data = await getJSON('/api/ops/agents');
    const agents = data.agents || [];
    const keep = traceAgentId || select.value || (agents[0] && agents[0].id) || '';
    select.textContent = '';
    agents.forEach(a => {
      const opt = el('option', null, a.id+' / '+a.role+(a.busy ? ' / busy' : ''));
      opt.value = a.id;
      select.appendChild(opt);
    });
    if (keep && agents.some(a => a.id === keep)) select.value = keep;
    traceAgentId = select.value;
    if (!agents.length) { $('traces').className='empty'; $('traces').textContent='No agents.'; }
  } catch (e) { $('traces').className='empty'; $('traces').textContent = e.message; }
}
async function loadTrace() {
  const id = ($('traceAgent').value || '').trim();
  if (!id) return;
  traceAgentId = id;
  const box = $('traces');
  try {
    const data = await getJSON('/api/ops/agents/'+encodeURIComponent(id)+'/trace?limit=150');
    box.textContent = '';
    const events = data.events || [];
    $('traceMeta').textContent = id+' / '+events.length+' events / newest first';
    if (!events.length) { box.className='empty'; box.textContent='No trace yet.'; return; }
    box.className='';
    events.forEach(x => box.appendChild(traceRow(x)));
  } catch (e) { box.className='empty'; box.textContent = e.message; }
}
async function refresh() {
  $('error').textContent='';
  try {
    const [p, b] = await Promise.all([
      getJSON('/api/pulse/overview'),
      getJSON('/api/project-board?status=open&limit=40')
    ]);
    renderPulse(p); renderBoard(b);
    $('stamp').textContent = 'Updated '+new Date().toLocaleString();
  } catch (e) { $('error').textContent = e.message; }
  await loadSessions();
  await loadAgents();
  await loadTrace();
}
$('sessStart').addEventListener('click', async () => {
  const agentId = ($('sessAgent').value || '').trim();
  if (!agentId) { $('sessAgent').focus(); return; }
  try {
    await postJSON('/api/browser-session/start', {agentId});
    $('sessAgent').value=''; loadSessions();
  } catch (e) { alert(e.message); }
});
$('refresh').addEventListener('click', refresh);
$('traceRefresh').addEventListener('click', loadTrace);
$('traceAgent').addEventListener('change', loadTrace);
$('traceAuto').addEventListener('change', () => {
  if (traceTimer) { clearInterval(traceTimer); traceTimer = null; }
  if ($('traceAuto').checked) traceTimer = setInterval(loadTrace, 10000);
});
refresh();
setInterval(refresh, 30000);
traceTimer = setInterval(loadTrace, 10000);
</script>
</body></html>"""
