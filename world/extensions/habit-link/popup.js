const status = document.getElementById('status');
const pauseBtn = document.getElementById('pause');
const resumeBtn = document.getElementById('resume');

function render({ paused, pausedUntil, serverEnabled, token }) {
  const live = !paused && serverEnabled !== false && !!token;
  if (!token) {
    status.textContent = 'Not paired — open Options, paste the tank URL and token.';
    status.className = 'off';
    return;
  }
  if (serverEnabled === false) {
    status.textContent = 'Server disabled collection. Nothing is being sent.';
    status.className = 'off';
    return;
  }
  if (paused && pausedUntil && Date.now() < pausedUntil) {
    const mins = Math.max(1, Math.round((pausedUntil - Date.now()) / 60000));
    status.textContent = `Paused, ${mins} min left. Nothing is being sent.`;
    status.className = 'off';
    return;
  }
  if (paused) {
    status.textContent = 'Paused. Nothing is being sent.';
    status.className = 'off';
    return;
  }
  status.textContent = 'Collecting (metadata only).';
  status.className = 'on';
}

function refresh() {
  chrome.storage.local.get(
    { paused: false, pausedUntil: 0, serverEnabled: true, token: '' },
    render,
  );
}

pauseBtn.addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'habit-link-pause', minutes: 60 }, () => {
    void chrome.runtime.lastError;
    refresh();
  });
});

resumeBtn.addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'habit-link-resume' }, () => {
    void chrome.runtime.lastError;
    refresh();
  });
});

document.getElementById('options').addEventListener('click', () => {
  chrome.runtime.openOptionsPage();
});

refresh();
