// Main game loop and UI wiring, extracted verbatim from index.html's
// inline <script> so no single script file exceeds 1,000 lines.
// Loaded after every other world/js module, in the same position the
// inline block occupied.

const VIEW_W = 1024, VIEW_H = Math.round(1024 * 384 / 688);
const canvas = document.getElementById('game');
const ctx = canvas.getContext('2d');
canvas.width = VIEW_W;
canvas.height = VIEW_H;
ctx.imageSmoothingEnabled = false;

const viewport = document.getElementById('viewport');
const stage = document.getElementById('stage');
const HUD_H = 60, ACTION_H = 34;
function fitViewport() {
  const ratio = VIEW_W / VIEW_H;
  const availW = window.innerWidth - 16;
  const availH = window.innerHeight - HUD_H - ACTION_H - 16;
  let w = Math.min(VIEW_W, availW);
  let h = Math.min(VIEW_H, w / ratio);
  if (h > availH) { h = availH; w = h * ratio; }
  stage.style.width = w + 'px';
  viewport.style.height = h + 'px';
}
window.addEventListener('resize', fitViewport);
fitViewport();

const ASSET_V = Date.now();
function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = src + (src.includes('?') ? '&' : '?') + 'v=' + ASSET_V;
  });
}

function clampTo(p, w, h) {
  p.x = Math.max(0, Math.min(w - p.w, p.x));
  p.y = Math.max(0, Math.min(h - p.h, p.y));
}
// blockedAt() itself lives in world.js -- it reads the per-cell collision
// grid (COLLISION_GRID), loaded asynchronously in main() below.

function overlaps(a, b) {
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
}

function blockedInRoom(p, rects) {
  for (const r of rects) if (overlaps(p, r)) return true;
  return false;
}

const ZOOM_MIN = 0.4, ZOOM_MAX = 4, ZOOM_STEP = 0.3;
const ZOOM_SHOW_ALL = Math.min(VIEW_W / GROUND_W, VIEW_H / GROUND_H);
const ZOOM_ROOM = Math.min(VIEW_W / ROOM_NATIVE_W, VIEW_H / ROOM_NATIVE_H, 1.8);

// state.location is { kind: 'outside' }, { kind: 'room', building }, or
// { kind: 'meeting', meetingId } (from a Town Hall call -- see meetings.js;
// the player is frozen and hidden for the duration, same as any other
// participant). state.ui is null or 'townhall' while the call-setup modal
// is open (also freezes movement, so walking away isn't possible mid-pick).
const state = {
  player: { x: SPAWN.x, y: SPAWN.y, w: 20, h: 16, dir: 'south', speed: 90 },
  keys: new Set(),
  zoom: ZOOM_SHOW_ALL,
  showFootprints: false,
  location: { kind: 'outside' },
  ui: null,
  scene: 'main',
};

// Remembered so exitRoom() has somewhere sane to put the player back for a
// room with no physical door on the outdoor map (Control Room -- reachable
// only via the HUD's Command Center button, see below).
let lastOutdoorPos = null;

function enterRoom(building) {
  // Guards this needs now that it's reachable from the HUD button (see
  // hudCommandCenterBtn) too, not just the outdoor door-trigger loop, which
  // already guaranteed 'outside'/no-modal by construction.
  if (state.ui || state.location.kind !== 'outside') return;
  const room = ROOMS[building];
  if (!room) return;
  lastOutdoorPos = { x: state.player.x, y: state.player.y };
  state.location = { kind: 'room', building };
  // Spawn near the room's open south (bottom) edge -- the implied unseen
  // entrance per the art (see rooms.js) -- clear of every layout's
  // furniture (checked against all four collision sets) and clear of the
  // exit trigger itself.
  state.player.x = ROOM_NATIVE_W / 2 - state.player.w / 2;
  state.player.y = 370;
  state.player.dir = 'north';
  state.zoom = ZOOM_ROOM;
}

function exitRoom() {
  const building = state.location.building;
  const door = ROOM_DOOR_TRIGGERS[building];
  state.location = { kind: 'outside' };
  if (door) {
    state.player.x = door.x + door.w / 2 - state.player.w / 2;
    state.player.y = door.y + door.h + 4;
  } else if (lastOutdoorPos) {
    state.player.x = lastOutdoorPos.x;
    state.player.y = lastOutdoorPos.y;
  }
  state.player.dir = 'south';
  state.zoom = ZOOM_SHOW_ALL;
}

// Tracks which zone opened the current room interaction (terminal or
// mailbox) so closing it knows exactly where to nudge the player clear of
// -- a fixed offset would risk landing back inside a neighboring
// collision rect, since these zones sit in narrow gaps.
let activeInteractZone = null;

function clearInteractZone() {
  if (activeInteractZone) {
    state.player.y = activeInteractZone.y + activeInteractZone.h + 2;
  }
  activeInteractZone = null;
}

function openTerminal(item) {
  // Weather Station only, per Eli's own profile line ("the only room with
  // outside/internet access -- do not share that access outside this
  // room") -- every other workstations-layout room (Media, Research
  // Center) keeps the generic stub terminal below. Press Office is
  // labeled "Work Room" (ROOMS.pressoffice) -- real sandboxed execution.
  if (state.location.building === 'weatherstation') { openBrowser(item); return; }
  if (state.location.building === 'pressoffice') { openWorkroom(item); return; }
  if (state.location.building === 'observatory') { openResearch(item); return; }
  if (state.location.building === 'library') { openLibrary(item); return; }
  if (state.location.building === 'media') { openLibrary(item); return; }

  activeInteractZone = item.zone;
  state.ui = 'terminal';
  const room = ROOMS[state.location.building];
  document.getElementById('terminalTitle').textContent = `${room.label} Terminal`;
  const output = document.getElementById('terminalOutput');
  output.innerHTML = '';
  appendTerminalLine(`-- ${room.label} terminal --`);
  appendTerminalLine('Not connected to anything yet. Type a command and press Enter.');
  document.getElementById('terminalModal').classList.remove('hidden');
  const input = document.getElementById('terminalInput');
  input.value = '';
  input.focus();
}

// Real internet access, gated -- see serve.py's /api/browse: Jev
// classifies the URL + stated purpose BEFORE anything is fetched, every
// request is logged server-side, and the whole thing fails closed if the
// classifier is unreachable. This modal is the player-driven front end
// for it; an agent's own research task work also uses it autonomously
// now (tasks.js's runResearchTask()).
function openBrowser(item) {
  activeInteractZone = item.zone;
  state.ui = 'browser';
  document.getElementById('browserUrlInput').value = '';
  document.getElementById('browserPurposeInput').value = '';
  document.getElementById('browserStatus').textContent = '';
  const output = document.getElementById('browserOutput');
  output.innerHTML = '';
  appendBrowserLine('-- Weather Station outside-access terminal --');
  appendBrowserLine('Every request is reviewed before anything loads. Enter a URL and why you want it.');
  document.getElementById('browserModal').classList.remove('hidden');
  document.getElementById('browserUrlInput').focus();
}

function appendBrowserLine(text) {
  const output = document.getElementById('browserOutput');
  const line = document.createElement('div');
  line.className = 'line';
  line.textContent = text;
  output.appendChild(line);
  output.scrollTop = output.scrollHeight;
}

function closeBrowser() {
  state.ui = null;
  document.getElementById('browserModal').classList.add('hidden');
  clearInteractZone();
}

const BROWSE_MAX_HOPS = 3;

// Follows breadcrumbs toward a stated goal, not just one fixed URL --
// An agent may not know in advance which page actually has
// what it's looking for. At each stop, Jev picks whichever link on the
// CURRENT page gets closer to the goal (or says to stop here), the same
// classifier "pick one of N" shape as everything else Jev does in this
// project. Every hop still goes through the full /api/browse gate
// independently -- this doesn't bypass or batch the safety checks, it
// just chains individually-gated requests. Capped at BROWSE_MAX_HOPS so
// this can't wander (and spend) indefinitely.
async function browseTowardGoal(startUrl, purpose, agentId) {
  let url = startUrl;
  let data = null;
  let hops = 0;
  for (; hops <= BROWSE_MAX_HOPS; hops++) {
    const res = await agentFetch('/api/browse', agentId, { method: 'POST', body: JSON.stringify({ url, purpose, agentId }) });
    data = await res.json();
    if (!data.allowed || data.error || !data.links || data.links.length === 0 || hops === BROWSE_MAX_HOPS) break;

    // The full set (server already caps at 40, see _extract_links) --
    // truncating further client-side was the actual reason an early test
    // of this wandered into nav chrome instead of real article content:
    // most pages put boilerplate navigation links first and body-content
    // links later, so an arbitrary "first N" slice mostly shows Jev the
    // chrome, not the substance.
    const candidates = data.links.map((l, i) => ({ id: 'link' + i, description: `"${l.text}" (${l.url})` }));
    candidates.push({ id: 'stop', description: 'This page already has what the purpose asks for -- no further navigation needed.' });
    const decision = await requestJevChoice(
      `Looking for: ${purpose || 'general information'}. Currently on: ${url}. Pick the link that gets closer to that, or 'stop' if this page already answers it.`,
      candidates
    );
    const choice = decision && decision.choice;
    if (!choice || choice === 'stop') break;
    const link = data.links[parseInt(choice.slice(4), 10)];
    if (!link) break;
    url = link.url;
  }
  return { data, hops };
}

async function handleBrowserGo() {
  const url = document.getElementById('browserUrlInput').value.trim();
  const purpose = document.getElementById('browserPurposeInput').value.trim();
  if (!url) return;
  const statusEl = document.getElementById('browserStatus');
  const goBtn = document.getElementById('browserGoBtn');
  appendBrowserLine(`> ${url}`);
  statusEl.textContent = 'Checking...';
  goBtn.disabled = true;
  try {
    const { data, hops } = await browseTowardGoal(url, purpose, 'player');
    if (!data.allowed) {
      statusEl.textContent = 'Blocked';
      appendBrowserLine(`[blocked] ${data.reason || 'Not approved.'}`);
    } else if (data.error) {
      statusEl.textContent = 'Approved, load failed';
      appendBrowserLine(`[error] ${data.error}`);
    } else {
      statusEl.textContent = hops > 0 ? `Loaded (followed ${hops} link${hops === 1 ? '' : 's'})` : 'Loaded';
      if (data.url && data.url !== url) appendBrowserLine(`-> ${data.url}`);
      appendBrowserLine((data.text || '(empty page)').slice(0, 2000));
      if (data.truncated) appendBrowserLine('[...truncated]');
    }
  } catch (e) {
    statusEl.textContent = 'Error';
    appendBrowserLine(`[error] ${e.message}`);
  }
  goBtn.disabled = false;
}

// The raw-HTTP sibling to "Go" -- real status/headers/body, not text
// stripped for human reading. Same Jev gate as Go, plus serve.py's own
// real room check (curlRequest -> /api/curl): this only works from
// inside the Weather Station, enforced server-side against the actual
// saved position, not just because this button happens to live in this
// modal.
async function handleBrowserCurl() {
  const url = document.getElementById('browserUrlInput').value.trim();
  const purpose = document.getElementById('browserPurposeInput').value.trim();
  if (!url) return;
  const statusEl = document.getElementById('browserStatus');
  const curlBtn = document.getElementById('browserCurlBtn');
  appendBrowserLine(`> RAW GET ${url}`);
  statusEl.textContent = 'Checking...';
  curlBtn.disabled = true;
  try {
    const data = await curlRequest('player', 'GET', url, purpose);
    if (!data.allowed) {
      statusEl.textContent = 'Blocked';
      appendBrowserLine(`[blocked] ${data.reason || 'Not approved.'}`);
    } else if (data.error) {
      statusEl.textContent = 'Approved, request failed';
      appendBrowserLine(`[error] ${data.error}`);
    } else {
      statusEl.textContent = `${data.status}`;
      appendBrowserLine(`status: ${data.status}`);
      appendBrowserLine(`headers: ${JSON.stringify(data.headers)}`);
      appendBrowserLine((data.body || '(empty body)').slice(0, 2000));
      if (data.truncated) appendBrowserLine('[...truncated]');
    }
  } catch (e) {
    statusEl.textContent = 'Error';
    appendBrowserLine(`[error] ${e.message}`);
  }
  curlBtn.disabled = false;
}

// Real sandboxed execution -- see serve.py's /api/execute and
// /api/pipeline. Every command is classified by Jev before it runs and
// then actually executes inside an isolated, resource-capped Docker
// container with allowlisted egress. One shared sandbox for the whole
// Work Room, not a fresh one per visit -- agents (and you)
// should be able to step out to the Weather Station or Library and come
// back to find things exactly as they left them. serve.py's own
// `_sandbox_dir_for()` handles the other half of that: it only wipes this
// sandbox if it's gone genuinely untouched for 15+ minutes, not just
// because whoever was using it walked away for a bit.
const WORKROOM_SANDBOX_ID = 'workroom-shared';
let workroomSandboxId = WORKROOM_SANDBOX_ID;

function openWorkroom(item) {
  activeInteractZone = item.zone;
  state.ui = 'workroom';
  document.getElementById('workroomPurposeInput').value = '';
  document.getElementById('workroomCommandInput').value = '';
  document.getElementById('workroomStatus').textContent = '';
  const output = document.getElementById('workroomOutput');
  output.innerHTML = '';
  appendWorkroomLine('-- Work Room sandbox -- ');
  appendWorkroomLine('Every command is reviewed before it runs, then executes in an isolated container with no network and no access outside its own workspace.');
  appendWorkroomLine('One command, or several lines to run as a pipeline (stops at the first failure).');
  document.getElementById('workroomModal').classList.remove('hidden');
  document.getElementById('workroomCommandInput').focus();
}

function appendWorkroomLine(text) {
  const output = document.getElementById('workroomOutput');
  const line = document.createElement('div');
  line.className = 'line';
  line.textContent = text;
  output.appendChild(line);
  output.scrollTop = output.scrollHeight;
}

function closeWorkroom() {
  state.ui = null;
  document.getElementById('workroomModal').classList.add('hidden');
  clearInteractZone();
}

async function handleWorkroomRun() {
  const raw = document.getElementById('workroomCommandInput').value;
  const lines = raw.split('\n').map(l => l.trim()).filter(Boolean);
  if (lines.length === 0) return;
  const purpose = document.getElementById('workroomPurposeInput').value.trim();
  const statusEl = document.getElementById('workroomStatus');
  const runBtn = document.getElementById('workroomRunBtn');
  runBtn.disabled = true;
  statusEl.textContent = 'Reviewing...';

  try {
    if (lines.length === 1) {
      appendWorkroomLine(`$ ${lines[0]}`);
      const res = await agentFetch('/api/execute', 'player', {
        method: 'POST',
        body: JSON.stringify({ agentId: 'player', command: lines[0], purpose, sandboxId: workroomSandboxId }),
      });
      const data = await res.json();
      if (!data.allowed) {
        statusEl.textContent = 'Blocked';
        appendWorkroomLine(`[blocked] ${data.reason || 'Not approved.'}${data.escalationId ? ' -- escalated for review.' : ''}`);
      } else {
        workroomSandboxId = data.sandboxId;
        statusEl.textContent = data.timedOut ? 'Timed out' : `Exit code ${data.exitCode}`;
        if (data.stdout) appendWorkroomLine(data.stdout);
        if (data.stderr) appendWorkroomLine(data.stderr);
      }
    } else {
      appendWorkroomLine(`$ running ${lines.length} steps as a pipeline...`);
      const res = await agentFetch('/api/pipeline', 'player', {
        method: 'POST',
        body: JSON.stringify({ agentId: 'player', sandboxId: workroomSandboxId, steps: lines.map((command, i) => ({ name: `step ${i + 1}`, command })) }),
      });
      const data = await res.json();
      workroomSandboxId = data.sandboxId;
      for (const step of data.results) {
        appendWorkroomLine(`-- ${step.name} --`);
        if (!step.allowed) { appendWorkroomLine(`[blocked] ${step.reason}`); continue; }
        if (step.stdout) appendWorkroomLine(step.stdout);
        if (step.stderr) appendWorkroomLine(step.stderr);
      }
      statusEl.textContent = data.failedStep ? `Failed at: ${data.failedStep}` : 'All steps succeeded';
    }
  } catch (e) {
    statusEl.textContent = 'Error';
    appendWorkroomLine(`[error] ${e.message}`);
  }
  runBtn.disabled = false;
}

// Research Center -- Observatory's real capability ("active investigative
// work," deliberately given the same visual layout as the Work Room).
// Mechanically that means the same
// class of capability as the Press Office's Work Room (real sandboxed
// execution), NOT the internet-access tool -- that one stays exclusive to
// Weather Station per Eli's own profile line. A SEPARATE persistent
// sandbox (not WORKROOM_SANDBOX_ID) so investigative scratch work never
// collides with Press Office's coding work.
const RESEARCH_SANDBOX_ID = 'research-shared';
let researchSandboxId = RESEARCH_SANDBOX_ID;

function openResearch(item) {
  activeInteractZone = item.zone;
  state.ui = 'research';
  document.getElementById('researchPurposeInput').value = '';
  document.getElementById('researchCommandInput').value = '';
  document.getElementById('researchStatus').textContent = '';
  const output = document.getElementById('researchOutput');
  output.innerHTML = '';
  appendResearchLine('-- Research Center sandbox --');
  appendResearchLine('Every command is reviewed before it runs, then executes in its own isolated container -- separate from the Work Room\'s.');
  appendResearchLine('One command, or several lines to run as a pipeline (stops at the first failure).');
  document.getElementById('researchModal').classList.remove('hidden');
  document.getElementById('researchCommandInput').focus();
}

function appendResearchLine(text) {
  const output = document.getElementById('researchOutput');
  const line = document.createElement('div');
  line.className = 'line';
  line.textContent = text;
  output.appendChild(line);
  output.scrollTop = output.scrollHeight;
}

function closeResearch() {
  state.ui = null;
  document.getElementById('researchModal').classList.add('hidden');
  clearInteractZone();
}

async function handleResearchRun() {
  const raw = document.getElementById('researchCommandInput').value;
  const lines = raw.split('\n').map(l => l.trim()).filter(Boolean);
  if (lines.length === 0) return;
  const purpose = document.getElementById('researchPurposeInput').value.trim();
  const statusEl = document.getElementById('researchStatus');
  const runBtn = document.getElementById('researchRunBtn');
  runBtn.disabled = true;
  statusEl.textContent = 'Reviewing...';

  try {
    if (lines.length === 1) {
      appendResearchLine(`$ ${lines[0]}`);
      const res = await agentFetch('/api/execute', 'player', {
        method: 'POST',
        body: JSON.stringify({ agentId: 'player', command: lines[0], purpose, sandboxId: researchSandboxId }),
      });
      const data = await res.json();
      if (!data.allowed) {
        statusEl.textContent = 'Blocked';
        appendResearchLine(`[blocked] ${data.reason || 'Not approved.'}${data.escalationId ? ' -- escalated for review.' : ''}`);
      } else {
        researchSandboxId = data.sandboxId;
        statusEl.textContent = data.timedOut ? 'Timed out' : `Exit code ${data.exitCode}`;
        if (data.stdout) appendResearchLine(data.stdout);
        if (data.stderr) appendResearchLine(data.stderr);
      }
    } else {
      appendResearchLine(`$ running ${lines.length} steps as a pipeline...`);
      const res = await agentFetch('/api/pipeline', 'player', {
        method: 'POST',
        body: JSON.stringify({ agentId: 'player', sandboxId: researchSandboxId, steps: lines.map((command, i) => ({ name: `step ${i + 1}`, command })) }),
      });
      const data = await res.json();
      researchSandboxId = data.sandboxId;
      for (const step of data.results) {
        appendResearchLine(`-- ${step.name} --`);
        if (!step.allowed) { appendResearchLine(`[blocked] ${step.reason}`); continue; }
        if (step.stdout) appendResearchLine(step.stdout);
        if (step.stderr) appendResearchLine(step.stderr);
      }
      statusEl.textContent = data.failedStep ? `Failed at: ${data.failedStep}` : 'All steps succeeded';
    }
  } catch (e) {
    statusEl.textContent = 'Error';
    appendResearchLine(`[error] ${e.message}`);
  }
  runBtn.disabled = false;
}

// The Library's real capability -- a shared file directory any agent (or
// you) can read from or write to: code, notes, completed-
// task records. Deliberately NOT injected into any agent's prompt
// automatically -- reading a file here is an explicit action (a real
// fetch someone chose to make), the same "memory as something you
// retrieve on demand, not something prepended to every run regardless"
// principle that keeps this from becoming the exact shared-notes cost
// problem flagged earlier.
let libraryActiveFile = null;

function openLibrary(item) {
  activeInteractZone = item.zone;
  state.ui = 'library';
  libraryActiveFile = null;
  document.getElementById('libraryPathInput').value = '';
  document.getElementById('libraryContentInput').value = '';
  document.getElementById('libraryStatus').textContent = '';
  document.getElementById('libraryFileHeader').textContent = 'Select a file';
  document.getElementById('libraryFileContent').innerHTML = '';
  document.getElementById('libraryModal').classList.remove('hidden');
  renderLibraryFileList();
  populateAgentFilePicker();
}

// Issue #9: agents view one another's files (read-only here). Populate the
// picker with every roster agent so the player can inspect anyone's real
// on-disk files -- the read is always allowed; only DIRECTOR/ADMIN can write,
// and the player isn't either, so these are strictly read-only in this view.
function populateAgentFilePicker() {
  const picker = document.getElementById('agentFilePicker');
  const agentFileList = document.getElementById('agentFileList');
  agentFileList.innerHTML = '';
  if (!picker) return;
  const prev = picker.value;
  picker.innerHTML = '<option value="">— choose an agent —</option>';
  for (const def of AGENT_ROSTER) {
    const opt = document.createElement('option');
    opt.value = def.id;
    opt.textContent = `${def.name} (${def.role})`;
    picker.appendChild(opt);
  }
  picker.value = prev && AGENT_ROSTER.some(d => d.id === prev) ? prev : '';
}

async function renderAgentFileList(agentId) {
  const listEl = document.getElementById('agentFileList');
  listEl.innerHTML = '<div class="empty">Loading...</div>';
  try {
    const res = await apiFetch('/api/agent-files?agentId=' + encodeURIComponent(agentId));
    const data = await res.json();
    listEl.innerHTML = '';
    if (!data.files || data.files.length === 0) {
      listEl.innerHTML = '<div class="empty">No files.</div>';
      return;
    }
    for (const f of data.files) {
      const btn = document.createElement('button');
      btn.className = 'roster-item';
      btn.innerHTML = `${f.path}<span class="agent-file-badge">${f.size}b</span>`;
      btn.addEventListener('click', () => openAgentFile(agentId, f.path));
      listEl.appendChild(btn);
    }
  } catch (e) {
    listEl.innerHTML = '<div class="empty">Could not load.</div>';
  }
}

async function openAgentFile(agentId, path) {
  document.getElementById('libraryFileHeader').textContent = `${agentId}/${path} (read-only)`;
  document.getElementById('libraryPathInput').value = '';
  document.getElementById('libraryContentInput').value = '';
  const content = document.getElementById('libraryFileContent');
  content.textContent = 'Loading...';
  try {
    const res = await apiFetch('/api/agent-files/read?agentId=' + encodeURIComponent(agentId) + '&path=' + encodeURIComponent(path));
    const data = await res.json();
    if (data.error) { content.textContent = `Could not read: ${data.error}`; return; }
    content.textContent = data.content || '(empty)';
  } catch (e) {
    content.textContent = 'Could not load this file.';
  }
}

function closeLibrary() {
  state.ui = null;
  document.getElementById('libraryModal').classList.add('hidden');
  clearInteractZone();
}

async function renderLibraryFileList() {
  const list = document.getElementById('libraryFileList');
  list.innerHTML = '<div class="empty">Loading...</div>';
  try {
    const res = await apiFetch('/api/library');
    const data = await res.json();
    list.innerHTML = '';
    if (!data.files || data.files.length === 0) {
      list.innerHTML = '<div class="empty">Nothing shared yet.</div>';
      return;
    }
    for (const f of data.files) {
      const btn = document.createElement('button');
      btn.className = 'roster-item' + (libraryActiveFile === f.path ? ' active' : '');
      btn.innerHTML = `${f.path}<span class="role">${f.size}b</span>`;
      btn.addEventListener('click', () => openLibraryFile(f.path));
      list.appendChild(btn);
    }
  } catch (e) {
    list.innerHTML = '<div class="empty">Could not load the library.</div>';
  }
}

// Real search, not just a flat listing -- per the reviewer's own
// retrospective naming this directly as a real frustration. Empty query
// falls back to the normal listing rather than showing nothing.
async function handleLibrarySearch() {
  const query = document.getElementById('librarySearchInput').value.trim();
  if (!query) { renderLibraryFileList(); return; }
  const list = document.getElementById('libraryFileList');
  list.innerHTML = '<div class="empty">Searching...</div>';
  try {
    const res = await apiFetch('/api/library/search?q=' + encodeURIComponent(query));
    const data = await res.json();
    list.innerHTML = '';
    if (!data.matches || data.matches.length === 0) {
      list.innerHTML = '<div class="empty">No matches.</div>';
      return;
    }
    for (const m of data.matches) {
      const btn = document.createElement('button');
      btn.className = 'roster-item' + (libraryActiveFile === m.path ? ' active' : '');
      btn.innerHTML = `${m.path}<span class="role">${m.snippet || ''}</span>`;
      btn.addEventListener('click', () => openLibraryFile(m.path));
      list.appendChild(btn);
    }
  } catch (e) {
    list.innerHTML = '<div class="empty">Search failed.</div>';
  }
}

async function openLibraryFile(path) {
  libraryActiveFile = path;
  document.getElementById('libraryFileHeader').textContent = path;
  document.getElementById('libraryPathInput').value = path;
  const content = document.getElementById('libraryFileContent');
  content.textContent = 'Loading...';
  try {
    const res = await apiFetch('/api/library/file?path=' + encodeURIComponent(path));
    const data = await res.json();
    content.textContent = data.content || '(empty)';
    document.getElementById('libraryContentInput').value = data.content || '';
  } catch (e) {
    content.textContent = 'Could not load this file.';
  }
  renderLibraryFileList();
}

async function handleLibraryWrite() {
  const path = document.getElementById('libraryPathInput').value.trim();
  const content = document.getElementById('libraryContentInput').value;
  if (!path) return;
  const statusEl = document.getElementById('libraryStatus');
  const btn = document.getElementById('libraryWriteBtn');
  btn.disabled = true;
  statusEl.textContent = 'Saving...';
  try {
    await writeLibraryFile('player', path, content);
    statusEl.textContent = `Saved ${path}.`;
    logThinkTankAction('player', 'library_write', { path });
    renderLibraryFileList();
  } catch (e) {
    statusEl.textContent = 'Save failed.';
  }
  btn.disabled = false;
}

// Real document ingestion: feed PDFs/Excel/code/HTML/
// images/directories/zips straight off your own machine into the
// Library. Player-only server-side (serve.py enforces this too, not just
// the UI) -- a human pointing the server at their own file is a
// different, much narrower risk than letting an agent choose arbitrary
// local paths on its own.
async function handleLibraryIngest() {
  const sourcePath = document.getElementById('libraryIngestPathInput').value.trim();
  if (!sourcePath) return;
  const statusEl = document.getElementById('libraryIngestStatus');
  const btn = document.getElementById('libraryIngestBtn');
  btn.disabled = true;
  statusEl.textContent = 'Ingesting...';
  try {
    const res = await apiFetch('/api/library/ingest', {
      method: 'POST',
      body: JSON.stringify({ agentId: 'player', sourcePath }),
    });
    const data = await res.json();
    if (data.error) {
      statusEl.textContent = `Failed: ${data.error}`;
    } else {
      statusEl.textContent = `Ingested ${data.okCount}/${data.totalCount} file(s).`;
      logThinkTankAction('player', 'library_ingest', { sourcePath, okCount: data.okCount, totalCount: data.totalCount });
      renderLibraryFileList();
    }
  } catch (e) {
    statusEl.textContent = 'Ingest failed.';
  }
  btn.disabled = false;
}

// Designmind-style design references: player uploads an image (or gives a
// live URL), the server runs a vision pass and writes a design brief into
// library/design-references/ that any agent can read. Player-only on the
// server too, not just the UI.
async function handleDesignRefUpload() {
  const fileInput = document.getElementById('designRefImageInput');
  const nameInput = document.getElementById('designRefNameInput');
  const statusEl = document.getElementById('designRefUploadStatus');
  const btn = document.getElementById('designRefUploadBtn');
  const file = fileInput.files && fileInput.files[0];
  const name = nameInput.value.trim();
  if (!file) { statusEl.textContent = 'Choose an image first.'; return; }
  if (!name) { statusEl.textContent = 'Give it a name first.'; return; }
  btn.disabled = true;
  statusEl.textContent = 'Analyzing image...';
  try {
    const imageBase64 = await readFileAsBase64(file);
    const res = await apiFetch('/api/design-reference', {
      method: 'POST',
      body: JSON.stringify({
        agentId: 'player',
        name,
        imageBase64,
        project: document.getElementById('designRefProjectInput').value.trim(),
      }),
    });
    const data = await res.json();
    if (data.error) {
      statusEl.textContent = `Failed: ${data.error}`;
    } else {
      statusEl.textContent = `Brief written to ${data.briefPath}.`;
      document.getElementById('libraryFileHeader').textContent = data.briefPath;
      document.getElementById('libraryFileContent').textContent = data.text;
      logThinkTankAction('player', 'design_reference', { name, briefPath: data.briefPath });
      renderLibraryFileList();
    }
  } catch (e) {
    statusEl.textContent = 'Upload failed.';
  }
  btn.disabled = false;
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || '');
      const idx = result.indexOf(',');
      resolve(idx >= 0 ? result.slice(idx + 1) : result);
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

async function handleDesignRefUrl() {
  const urlInput = document.getElementById('designRefUrlInput');
  const statusEl = document.getElementById('designRefUrlStatus');
  const btn = document.getElementById('designRefUrlBtn');
  const url = urlInput.value.trim();
  if (!url) { statusEl.textContent = 'Give a URL first.'; return; }
  btn.disabled = true;
  statusEl.textContent = 'Screenshotting and analyzing...';
  try {
    const res = await apiFetch('/api/design-reference-url', {
      method: 'POST',
      body: JSON.stringify({
        agentId: 'player',
        url,
        project: document.getElementById('designRefUrlProjectInput').value.trim(),
      }),
    });
    const data = await res.json();
    if (data.error) {
      statusEl.textContent = `Failed: ${data.error}`;
    } else {
      statusEl.textContent = `Review written to ${data.briefPath}.`;
      document.getElementById('libraryFileHeader').textContent = data.briefPath;
      document.getElementById('libraryFileContent').textContent = data.text;
      logThinkTankAction('player', 'design_reference_url', { url, briefPath: data.briefPath });
      renderLibraryFileList();
    }
  } catch (e) {
    statusEl.textContent = 'Review failed.';
  }
  btn.disabled = false;
}

async function handleDesignRefTaste() {
  const projectInput = document.getElementById('designRefTasteProjectInput');
  const statusEl = document.getElementById('designRefTasteStatus');
  const btn = document.getElementById('designRefTasteBtn');
  const project = projectInput.value.trim();
  if (!project) { statusEl.textContent = 'Give a project first.'; return; }
  btn.disabled = true;
  statusEl.textContent = 'Folding taste doc...';
  try {
    const res = await apiFetch('/api/design-reference/taste', {
      method: 'POST',
      body: JSON.stringify({ agentId: 'player', project }),
    });
    const data = await res.json();
    if (data.error) {
      statusEl.textContent = `Failed: ${data.error}`;
    } else {
      statusEl.textContent = `Taste doc written to ${data.tastePath} from ${data.briefCount} brief(s).`;
      document.getElementById('libraryFileHeader').textContent = data.tastePath;
      document.getElementById('libraryFileContent').textContent = data.text;
      logThinkTankAction('player', 'design_reference_taste', { project, tastePath: data.tastePath });
      renderLibraryFileList();
    }
  } catch (e) {
    statusEl.textContent = 'Fold failed.';
  }
  btn.disabled = false;
}

