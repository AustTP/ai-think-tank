// Habit Link content script: metadata only.
//
// Per page it tracks: URL, title, dwell time (visible time), max scroll
// depth, click count. It NEVER reads page text, forms, cookies, or
// localStorage. The event leaves on pagehide / tab-hidden, lands in the
// service worker's buffer, and nothing is sent anywhere the options page
// did not configure. Incognito tabs are ignored entirely.

(() => {
  try {
    if (document.getElementById('habit-link-marker')) return;
    const marker = document.createElement('div');
    marker.id = 'habit-link-marker';
    marker.style.display = 'none';
    document.documentElement.appendChild(marker);
  } catch (_e) { /* keep going without the marker */ }

  if (typeof chrome === 'undefined' || !chrome.runtime) return;
  // Incognito: never collected.
  if (chrome.extension && chrome.extension.inIncognitoContext) return;

  const start = Date.now();
  let maxScroll = 0;
  let clicks = 0;
  let sent = false;

  function host() {
    try { return location.hostname || ''; } catch (_e) { return ''; }
  }

  function scrollFrac() {
    try {
      const doc = document.documentElement;
      const span = (doc.scrollHeight - window.innerHeight) || 0;
      return span > 0 ? Math.min(1, Math.max(0, window.scrollY / span)) : 0;
    } catch (_e) { return 0; }
  }

  window.addEventListener('scroll', () => {
    const s = scrollFrac();
    if (s > maxScroll) maxScroll = s;
  }, { passive: true, capture: true });

  window.addEventListener('click', () => { clicks += 1; }, { passive: true, capture: true });

  function send() {
    if (sent) return;
    sent = true;
    const dwell = Date.now() - start;
    if (dwell < 1500) return; // bounces under 1.5s are not habits
    try {
      chrome.runtime.sendMessage({
        type: 'habit-link-event',
        event: {
          url: location.href,
          title: document.title || '',
          host: host(),
          dwellMs: dwell,
          scrollFrac: maxScroll || scrollFrac(),
          clicks,
          ts: Date.now(),
        },
      }, () => void chrome.runtime.lastError);
    } catch (_e) { /* context gone; nothing to do */ }
  }

  window.addEventListener('pagehide', send);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') send();
  });
})();
