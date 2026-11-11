const tankUrl = document.getElementById('tankUrl');
const token = document.getElementById('token');
const blockedInput = document.getElementById('blockedInput');
const blockedList = document.getElementById('blocked');
const saved = document.getElementById('saved');

function normalizeHost(value) {
  try {
    const u = new URL(value.includes('://') ? value : `https://${value}`);
    return (u.hostname || '').toLowerCase();
  } catch (_e) {
    return value.trim().toLowerCase();
  }
}

function renderBlocked(hosts) {
  blockedList.textContent = '';
  (hosts || []).forEach((h) => {
    const li = document.createElement('li');
    li.textContent = h + ' ';
    const rm = document.createElement('button');
    rm.textContent = 'remove';
    rm.addEventListener('click', async () => {
      const { blockedHosts } = await chrome.storage.local.get({ blockedHosts: [] });
      await chrome.storage.local.set({
        blockedHosts: blockedHosts.filter((x) => x !== h),
      });
      load();
    });
    li.appendChild(rm);
    blockedList.appendChild(li);
  });
}

async function load() {
  const cfg = await chrome.storage.local.get({
    tankUrl: 'http://127.0.0.1:8936',
    token: '',
    blockedHosts: [],
  });
  tankUrl.value = cfg.tankUrl;
  token.value = cfg.token;
  renderBlocked(cfg.blockedHosts);
}

document.getElementById('addBlocked').addEventListener('click', async () => {
  const host = normalizeHost(blockedInput.value);
  if (!host) return;
  const { blockedHosts } = await chrome.storage.local.get({ blockedHosts: [] });
  if (!blockedHosts.includes(host)) blockedHosts.push(host);
  await chrome.storage.local.set({ blockedHosts });
  blockedInput.value = '';
  load();
});

document.getElementById('save').addEventListener('click', async () => {
  await chrome.storage.local.set({
    tankUrl: tankUrl.value.trim().replace(/\/+$/, '') || 'http://127.0.0.1:8936',
    token: token.value.trim(),
  });
  saved.textContent = 'Saved ✓';
  setTimeout(() => { saved.textContent = ''; }, 2000);
});

load();
