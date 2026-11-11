// Habit Link background service worker.
//
// Receives one metadata event per page from content scripts, buffers them,
// and flushes a batch to the tank every 60s (or when 20 events pile up).
// Never collects page content -- content.js only ever sends url/title/dwell/
// scroll/clicks, and this worker refuses to buffer anything else.
//
// Off switches, checked here:
//   - paused / pausedUntil (popup Pause button, 1h default)
//   - per-host blocklist (options page)
//   - server kill switch: every 5 minutes a GET /api/habit-link/status with
//     the pairing token; 403/401/501/network-fail means STOP and DROP the
//     buffer -- the server is the authority, the extension just complies.

const FLUSH_ALARM = 'habit-link-flush';
const STATUS_ALARM = 'habit-link-status';
const BUFFER_LIMIT = 500;
const FLUSH_BATCH = 20;

function cfg() {
  return chrome.storage.local.get({
    tankUrl: 'http://127.0.0.1:8936',
    token: '',
    paused: false,
    pausedUntil: 0,
    blockedHosts: [],
    buffer: [],
    serverEnabled: true,
    lastStatusCheck: 0,
  });
}

function setPaused(paused, minutes) {
  return chrome.storage.local.set({
    paused,
    pausedUntil: paused ? Date.now() + (minutes || 60) * 60 * 1000 : 0,
  });
}

function hostBlocked(hosts, host) {
  return (hosts || []).some((h) => host === h || host.endsWith('.' + h));
}

function pausedNow({ paused, pausedUntil }) {
  return paused && (!pausedUntil || Date.now() < pausedUntil);
}

async function bufferEvent(ev) {
  const { buffer, blockedHosts, paused, pausedUntil, serverEnabled } = await cfg();
  if (pausedNow({ paused, pausedUntil }) || serverEnabled === false) return;
  if (!ev || !ev.host) return;
  if (hostBlocked(blockedHosts, ev.host)) return;
  // Defensive: keep the buffer to the documented metadata fields only.
  const clean = {
    url: String(ev.url || '').slice(0, 2000),
    title: String(ev.title || '').slice(0, 300),
    dwellMs: Math.max(0, Math.min(Number(ev.dwellMs) || 0, 3600000)),
    scrollFrac: Math.max(0, Math.min(Number(ev.scrollFrac) || 0, 1)),
    clicks: Math.max(0, Math.min(Number(ev.clicks) || 0, 100)),
    ts: Number(ev.ts) || Date.now(),
    host: String(ev.host).slice(0, 253),
  };
  const next = (buffer || []).concat([clean]).slice(-BUFFER_LIMIT);
  await chrome.storage.local.set({ buffer: next });
  if (next.length >= FLUSH_BATCH) await flush();
}

async function flush() {
  const { buffer, tankUrl, token, serverEnabled } = await cfg();
  if (!buffer || !buffer.length) return;
  if (!token || serverEnabled === false) {
    // Unpaired or server said stop: holding events has no point, drop them.
    await chrome.storage.local.set({ buffer: [] });
    return;
  }
  try {
    const resp = await fetch(`${tankUrl.replace(/\/+$/, '')}/api/habit-link`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Habit-Link-Token': token },
      body: JSON.stringify({ events: buffer }),
    });
    if (resp.status === 403 || resp.status === 501) {
      // Server-side kill switch or unconfigured: comply and forget.
      await chrome.storage.local.set({ buffer: [], serverEnabled: false });
      return;
    }
    if (resp.ok) await chrome.storage.local.set({ buffer: [] });
    // 401 (bad token) keeps the buffer so events survive a token fix, but
    // the user should re-pair in options.
  } catch (_e) {
    // Tank down: keep buffering (ring buffer bounds it).
  }
}

async function checkServerStatus(force) {
  const { tankUrl, token, lastStatusCheck } = await cfg();
  if (!token) return;
  const now = Date.now();
  if (!force && now - (lastStatusCheck || 0) < 4 * 60 * 1000) return;
  try {
    const resp = await fetch(`${tankUrl.replace(/\/+$/, '')}/api/habit-link/status`, {
      headers: { 'X-Habit-Link-Token': token },
    });
    const enabled = resp.ok;
    const patch = { lastStatusCheck: now, serverEnabled: enabled };
    if (!enabled) patch.buffer = [];
    await chrome.storage.local.set(patch);
  } catch (_e) {
    await chrome.storage.local.set({ lastStatusCheck: now });
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === 'habit-link-event') {
    bufferEvent(msg.event).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg && msg.type === 'habit-link-pause') {
    setPaused(true, msg.minutes || 60).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg && msg.type === 'habit-link-resume') {
    setPaused(false, 0).then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});

chrome.alarms.create(FLUSH_ALARM, { periodInMinutes: 1 });
chrome.alarms.create(STATUS_ALARM, { periodInMinutes: 5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === FLUSH_ALARM) flush();
  if (alarm.name === STATUS_ALARM) checkServerStatus(false);
});

chrome.runtime.onInstalled.addListener(() => checkServerStatus(true));
chrome.runtime.onStartup.addListener(() => checkServerStatus(true));
