#!/usr/bin/env python3
"""browser_record -- record a real browser session the way a Photoshop Action
records edits: start it, do the task by hand, stop it, and get back an
artifact set an agent can learn from or replay.

Runs a headful Chromium under DevTools (CDP) and passively captures what the
operator does -- clicks, typed values, Enter/submit, SPA route changes,
every page load with its interactive fields and links, and the network rows
each step produced -- then writes three artifacts into the Library:

  session.txt    the human/agent-readable transcript: numbered steps with the
                 element locator, the action, the (possibly redacted) value,
                 page context and a screenshot. This is the "teach agents"
                 document.
  session.json   the machine-readable event log: steps, page snapshots,
                 screenshots (SHA-256), and the network rows behind each step.
  browser_act_actions.json  the same session restated in the EXACT wire
                 format world/sandbox/browser_driver.py speaks, so the flow
                 can be replayed through /api/browser-act (one Jev-gated op
                 at a time) instead of being rebuilt from prose.

Redaction is not optional. Values typed into fields whose type, name, id,
placeholder, autocomplete, aria-label, visible label or surrounding text
looks like a password/OTP/2FA/token/secret are replaced with a placeholder
BEFORE anything reaches disk; credential-bearing network headers and query
values are stripped the same way. The artifacts are designed to sit in the
shared Library, so they must never carry live credentials.

The capture is read-only toward the page: no free-form JS is ever evaluated.
The only injected code is the fixed listener bundle below (same fixed-op
philosophy as browser_driver.py), and it reports through a CDP binding rather
than writing anywhere. Screenshots come from the CDP protocol itself, so no
OS screen-recording permission is involved.

Usage (host-side operator tool, scripts/ -- not an agent primitive):
  python3 scripts/browser_record.py --label "huggingface login" [--url URL]
  # ...drive the task in the window that opens, then Ctrl-C in the terminal.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
from urllib.parse import urlencode, urlparse, parse_qsl

CDP_CONNECT_TIMEOUT_S = 30
BINDING_NAME = '__browserRecordSink'
MAX_FIELDS = 40
MAX_LINKS = 40
MAX_VALUE_LEN = 120
MAX_STEPS = 2000
MAX_NET_ROWS = 4000
REDACTED = '[redacted]'
STEADY_SHOT_MS = 1500          # min gap between step screenshots on a page
SHUTDOWN_GRACE_S = 3
# Queue-end marker. A distinct object (not None) on purpose: a nested
# screenshot-wait reader consumes the same queue and must never steal the
# pump's end-of-stream signal.
_EOF = object()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT_DIR = os.path.join(REPO, 'library', 'recordings')

BROWSER_CANDIDATES = (
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/usr/bin/google-chrome',
    '/snap/bin/chromium',
)

# Fields whose typed content must never reach a Library document. The check
# runs over every hint the element offers: type, id, name, placeholder,
# autocomplete, aria-label, visible label text and enclosing form text.
SENSITIVE_HINT_RE = re.compile(
    r'(?i)(?<!\w)(?:'
    r'password|passwd|pwd|passcode|pass[ \-_]?w?phrase'
    r'|otp|one[ \-_]?time|totp|hotp|2[ \-_]?fa|two[ \-_]?factor'
    r'|security[ \-_]?code|verification[ \-_]?code|verify[ \-_]?code|authenticator'
    r'|auth[ \-_]?code|confirm[ \-_]?code|pin[ \-_]?code'
    r'|secret|token|api[ \-_]?key|apikey|access[ \-_]?key|signing[ \-_]?key'
    r'|credit[ \-_]?card|card[ \-_]?number|cvv|cvc|ssn|social[ \-_]?security'
    # trailing guard allows underscores but not letters/digits, so
    # snake_case names like otp_code match while 'author' does not.
    r')(?![A-Za-z0-9])')
# autocomplete tokens that name secrets even when no other hint does
SENSITIVE_AUTOCOMPLETE = ('current-password', 'new-password', 'one-time-code',
                          'cc-csc', 'cc-number', 'cc-security-code')

NETWORK_HEADER_NAMES_TO_REDACT = ('authorization', 'proxy-authorization', 'cookie',
                                  'set-cookie', 'x-api-key', 'x-auth-token',
                                  'x-csrf-token', 'x-xsrf-token', 'private-token',
                                  'x-goog-api-key', 'x-amz-security-token')
NETWORK_HEADER_PATTERN_RE = re.compile(r'(?i)(token|secret|api-?key|cookie|auth)')


def _sensitive_field_hints(field):
    """Every attribute surface an element offers, joined for the hint regex.

    Reconciliation note: the in-page bundle ALSO runs a cheap version of this
    decision so the live page itself can withhold a sensitive value; Python
    recomputes the verdict from the attributes the bundle reports, and a value
    is only ever kept when BOTH sides say it is safe."""
    parts = (
        field.get('type') or '', field.get('id') or '', field.get('name') or '',
        field.get('placeholder') or '', field.get('ariaLabel') or '',
        field.get('autocomplete') or '', field.get('label') or '',
        field.get('parentText') or '', field.get('formText') or '',
    )
    return ' \n '.join(p for p in parts if p)


def is_sensitive_field(field):
    autocomplete = (field.get('autocomplete') or '').lower()
    if autocomplete in SENSITIVE_AUTOCOMPLETE:
        return True
    return bool(SENSITIVE_HINT_RE.search(_sensitive_field_hints(field)))


def mask_value(field, value):
    """The value a Library document may hold. Sensitive fields become a
    length-preserving placeholder (a transcript that says 'typed 12 chars'
    teaches flow better than '[redacted]' alone and leaks only a length),
    empty stays empty, ordinary values are length-capped."""
    if not value:
        return ''
    if is_sensitive_field(field):
        return '%s len %d' % (REDACTED, len(value))
    return str(value)[:MAX_VALUE_LEN]


def _sensitive_query_key(key):
    return bool(re.search(
        r'(?i)(token|secret|key|auth|state|nonce|signature|sid|session'
        r'|code|jwt|credential|passwd)', key))


def redact_url(url):
    """Strip credential-looking query parameters from a URL kept in an
    artifact. Names stay (they describe the site), values go."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.query:
        return url
    try:
        pairs = [(k, (REDACTED if _sensitive_query_key(k) else v))
                 for k, v in parse_qsl(parsed.query, keep_blank_values=True)]
        return parsed._replace(query=urlencode(pairs)).geturl()
    except ValueError:
        return url


def redact_headers(headers):
    """One header filter over a CDP headers dict (shared by request and
    response captures, so tokens cannot slip through an odd capitalization)."""
    out = {}
    for name, value in (headers or {}).items():
        low = name.lower()
        if low in NETWORK_HEADER_NAMES_TO_REDACT or NETWORK_HEADER_PATTERN_RE.search(low):
            out[name] = REDACTED
        else:
            out[name] = value
    return out


def redact_network_row(row):
    """One merged network row (method, url, status, headers), redacted.
    Request/Response bodies are never captured at all, so nothing here can
    hold one; the caller keeps it that way."""
    out = dict(row or {})
    if out.get('url'):
        out['url'] = redact_url(out['url'])
    out.pop('postData', None)
    if 'headers' in out:
        out['headers'] = redact_headers(out['headers'])
    return out


def best_selector(desc):
    """The most stable locator we can state for the element, readable in a
    transcript AND replayable by playwright: id > name > aria-label >
    visible-text anchor > nth-of-type path. Never empty."""
    tag = desc.get('tag') or ''
    if desc.get('id'):
        return '#%s' % desc['id']
    if desc.get('name'):
        return '%s[name="%s"]' % (tag, desc['name'])
    if desc.get('ariaLabel'):
        return '%s[aria-label="%s"]' % (tag, desc['ariaLabel'])
    text = (desc.get('text') or '').strip()
    if text and tag in ('button', 'a', 'label'):
        return '%s:has-text("%s")' % (tag, text[:40])
    if desc.get('csspath'):
        return desc['csspath']
    return tag or '*'


def aggregate_steps(raw_events):
    """Turn ordered raw injected/CDP events into final steps.

    Event kinds (from the fixed in-page bundle):
      load   {url, title, fields, links}   page finished loading
      route  {url}                          SPA history push/replace
      click  {selector, element{...}, isSubmit, href?}
      input  {selector, element{...}, value|nchars, masked}
      enter  {selector, element{...}}       Enter key pressed in an input
      submit {selector, element{...}}       form submitted

    Consecutive input events on one selector collapse into a single step
    holding the longest observed safe value plus a total typed-char count.
    Every step carries the pageId of the navigate step it happened on.
    """
    steps = []
    page_counter = 0
    last_page = None
    for ev in raw_events:
        kind = ev.get('kind')
        url = redact_url(ev.get('url') or '')
        if kind in ('load', 'route'):
            # A load for a URL we already opened (reload, or an SPA route
            # back) enriches the most recent navigate step for that URL
            # instead of minting a duplicate page.
            target = None
            for existing in reversed(steps):
                if existing.get('kind') == 'navigate' and existing.get('url') == url:
                    target = existing
                    break
            if target is not None:
                if kind == 'load':
                    target['fields'] = ev.get('fields') or target.get('fields')
                    target['links'] = ev.get('links') or target.get('links')
                    target['title'] = ev.get('title') or target.get('title')
                continue
            page_counter += 1
            last_page = 'page-%d' % page_counter
            steps.append({'kind': 'navigate', 'url': url,
                          'title': ev.get('title') or '', 'pageId': last_page,
                          'fields': ev.get('fields') or [], 'links': ev.get('links') or []})
            continue
        element = dict(ev.get('element') or {})
        element['selector'] = ev.get('selector') or best_selector(element)
        if kind == 'input':
            _merge_input(steps, element, ev, last_page)
            continue
        step = {'kind': kind,
                'selector': element['selector'],
                # The page's own `masked` verdict is NEVER trusted alone;
                # python recomputes sensitivity from the reported attributes.
                'masked': bool(ev.get('masked') or is_sensitive_field(element)),
                'element': {k: element.get(k) for k in
                            ('tag', 'id', 'name', 'type', 'ariaLabel', 'text', 'outer')},
                'pageId': last_page}
        if kind == 'click':
            step['value'] = mask_value(element, ev.get('value') or '')
            step['isSubmit'] = bool(ev.get('isSubmit'))
            if ev.get('href'):
                step['href'] = redact_url(ev['href'])
        steps.append(step)
        if len(steps) > MAX_STEPS:
            break
    return steps


def _merge_input(steps, element, ev, last_page):
    masked = bool(ev.get('masked') or is_sensitive_field(element))
    value = ev.get('value') or ''
    nchars = int(ev.get('nchars') or 0)
    prev = steps[-1] if steps else None
    same = (prev and prev.get('kind') == 'input'
            and prev.get('selector') == element['selector']
            and prev.get('pageId') == last_page)
    if same:
        if masked:
            prev['masked'] = True
            prev['value'] = ''
            prev['chars'] = int(prev.get('chars') or 0) + (nchars or 0)
        elif value:
            prev['value'] = mask_value(element, value)
            prev['chars'] = int(prev.get('chars') or 0) + max(nchars, 0)
        return
    step = {'kind': 'input', 'selector': element['selector'],
            'element': {k: element.get(k) for k in
                        ('tag', 'id', 'name', 'type', 'ariaLabel', 'text', 'outer')},
            'masked': masked, 'pageId': last_page, 'chars': nchars}
    step['value'] = '' if masked else mask_value(element, value)
    steps.append(step)


INJECTED_BUNDLE = r'''
(function () {
  if (window.__brInstalled) { return; }
  window.__brInstalled = true;
  var pending = [];
  function sink(payload) {
    try { window.__browserRecordSink(JSON.stringify(payload)); }
    catch (e) { pending.push(JSON.stringify(payload));
      if (pending.length > 50) { pending.length = 50; } }
  }
  function flush() {
    if (!window.__browserRecordSink || !pending.length) { return; }
    var left = [];
    pending.forEach(function (p) {
      try { window.__browserRecordSink(p); } catch (e) { left.push(p); }
    });
    pending = left;
  }
  function hintBits(el) {
    var hint = null;
    var lab = el.closest && el.closest('label');
    if (lab) { hint = (lab.innerText || '').trim().slice(0, 80); }
    var form = el.closest && el.closest('form');
    return { label: hint, form: '' , ctx: hint,
             formText: form ? (form.innerText || '').trim().slice(0, 240) : '' };
  }
  function sensitive(el, ctx) {
    var parts = [(el.type || ''), (el.id || ''), (el.name || ''),
      (el.placeholder || ''), (el.getAttribute('aria-label') || ''),
      (el.autocomplete || ''), ctx];
    var s = parts.join(' ').toLowerCase();
    return /pass(word|wd|code)|otp|one.?time|totp|hotp|2.?fa|two.?factor/.test(s)
        || /secret|token|api.?key|apikey|verif(y|ication).?code|security.?code/.test(s)
        || /auth(er|entication)?.?code|pin.?code|credit.?card|card.?number|cvv|cvc|ssn/.test(s);
  }
  function descFor(el) {
    var par = el.parentElement;
    var form = el.form || (el.closest && el.closest('form'));
    return {
      tag: (el.tagName || '').toLowerCase(),
      type: (el.getAttribute('type') || ''),
      id: (el.id || ''), name: el.name || '',
      placeholder: (el.getAttribute('placeholder') || ''),
      ariaLabel: (el.getAttribute('aria-label') || ''),
      autocomplete: (el.autocomplete || ''),
      text: ((el.innerText || '').trim() || '').slice(0, 60),
      label: (el.labels && el.labels[0] ? (el.labels[0].innerText || '').trim().slice(0, 80) : ''),
      parentText: (par ? ((par.innerText || '').trim() || '').slice(0, 240) : ''),
      formText: (form ? ((form.innerText || '').trim() || '').slice(0, 240) : ''),
      outer: ((el.outerHTML || '').trim() || '').slice(0, 220),
      formAction: (form && form.action ? String(form.action) : ''),
      href: (el.tagName === 'A' && el.href ? String(el.href) : '')
    };
  }
  function cssPath(el) {
    var parts = [], depth = 0;
    while (el && el.nodeType === 1 && depth < 6) {
      var seg = (el.tagName || '').toLowerCase();
      if (el.id) { parts.unshift('#' + el.id); break; }
      var sib = el, nth = 1;
      while ((sib = sib.previousElementSibling)) { nth++; }
      parts.unshift(seg + ':nth-of-type(' + nth + ')');
      el = el.parentElement; depth++;
    }
    return parts.join(' > ');
  }
  function send(kind, el, extra) {
    var d = descFor(el);
    var ctx = (extra && extra.ctx) ? extra.ctx
      : [d.parentText, d.formText, d.label].join(' ');
    var masked = sensitive(el, ctx);
    var payload = { kind: kind, url: String(location.href),
      selector: cssPath(el), element: d, masked: masked, t: Date.now() };
    if (extra) {
      if (extra.value !== undefined) {
        payload.value = masked ? '' : String(extra.value || '').slice(0, 120);
      }
      if (extra.nchars !== undefined) { payload.nchars = extra.nchars; }
      if (extra.isSubmit !== undefined) { payload.isSubmit = extra.isSubmit; }
    }
    sink(payload);
  }
  document.addEventListener('click', function (e) {
    var el = e.target;
    if (!el || !el.tagName) { return; }
    var h = hintBits(el);
    var isSubmit = (el.getAttribute('type') === 'submit'
      || el.tagName === 'BUTTON'
      || /sign.?in|log.?in|submit|verify|save/i.test((el.innerText || '').trim()));
    send('click', el, { isSubmit: isSubmit, ctx: h.ctx });
  }, true);
  document.addEventListener('input', function (e) {
    var el = e.target;
    if (!el || !el.tagName) { return; }
    if (!/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) { return; }
    var h = hintBits(el);
    var v = el.value || '';
    var masked = sensitive(el, [h.formText, h.label, h.ctx].join(' '));
    send('input', el, { value: masked ? '' : v, nchars: String(v).length, ctx: h.ctx });
  }, true);
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter') { return; }
    send('enter', e.target, {});
  }, true);
  document.addEventListener('submit', function (e) {
    send('submit', e.target, {});
  }, true);
  function route() { sink({ kind: 'route', url: String(location.href), t: Date.now() }); }
  var push = history.pushState, replace = history.replaceState;
  history.pushState = function () { push.apply(this, arguments); route(); };
  history.replaceState = function () { replace.apply(this, arguments); route(); };
  window.addEventListener('popstate', route);
  function pageState() {
    var fields = [], links = [];
    try {
      fields = Array.prototype.slice.call(
        document.querySelectorAll('input,select,textarea,button'), 0, 40)
        .map(function (el) {
          return { tag: (el.tagName || '').toLowerCase(),
            type: el.getAttribute('type') || '', name: el.name || '', id: el.id || '',
            placeholder: el.getAttribute('placeholder') || '',
            ariaLabel: el.getAttribute('aria-label') || '',
            text: ((el.innerText || '').trim() || '').slice(0, 80) };
        });
      links = Array.prototype.slice.call(document.querySelectorAll('a[href]'), 0, 40)
        .map(function (a) {
          return { text: ((a.innerText || '').trim() || '').slice(0, 100),
            href: String(a.href) };
        });
    } catch (err) { /* the snapshot must never be the thing that breaks a page */ }
    return { fields: fields, links: links };
  }
  function sendLoad() {
    var st = pageState();
    sink({ kind: 'load', url: String(location.href),
      title: (document.title || '').slice(0, 200), t: Date.now(),
      fields: st.fields, links: st.links });
  }
  window.addEventListener('load', sendLoad);
  if (document.readyState === 'complete') { sendLoad(); }
  setInterval(flush, 120);
})();
'''


def actions_from_steps(steps):
    """Restate the session as browser_act wire actions (the vocabulary of
    world/sandbox/browser_driver.py: goto/fill/click/submit). Each op carries
    the URL the driver must land on first, because a browser_act op opens a
    fresh page in the persistent profile -- it cannot assume the page the
    recorder saw. Redacted values come through EMPTY with a redacted marker:
    the replay must supply them from the credential vault, never from the
    transcript."""
    actions = []
    current_url = ''
    for step in steps:
        kind = step.get('kind')
        url = step.get('url') or ''
        if kind == 'navigate':
            current_url = url
            continue
        if kind == 'input' and not step.get('value') and step.get('chars'):
            actions.append({'op': 'fill', 'url': current_url,
                            'selector': step.get('selector') or '',
                            'value': '', 'redacted': True,
                            'note': 'supply secret from the vault at replay time'})
            continue
        if kind == 'click' and step.get('href'):
            actions.append({'op': 'goto', 'url': step['href'],
                            'note': 'recorded link click'})
            current_url = step['href']
            continue
        if kind == 'input':
            actions.append({'op': 'fill', 'url': current_url,
                            'selector': step.get('selector') or '',
                            'value': step.get('value') or ''})
            continue
        if kind in ('enter', 'submit') or _submit_like_click(kind, step):
            actions.append({'op': 'submit', 'url': current_url,
                            'selector': step.get('selector') or ''})
            continue
        actions.append({'op': 'click', 'url': current_url,
                        'selector': step.get('selector') or ''})
    return actions


def _submit_like_click(kind, step):
    if kind != 'click':
        return False
    el = step.get('element') or {}
    if el.get('type') == 'submit':
        return True
    text = (el.get('text') or '').lower()
    return bool(re.search(r'(?i)(sign.?in|log.?in|submit|verify)', text))


def render_txt(session):
    """The transcript an agent reads. Plain text, numbered steps, locator +
    action + value + context; then the completion summary whose success
    marker is where the flow ENDED."""
    meta = session['meta']
    out = []
    out.append('# Browser session recording: %s' % meta['label'])
    out.append('Session: %s' % meta['session_id'])
    out.append('Recorded: %s .. %s   Browser: %s' % (
        time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(meta['started_ts'])),
        time.strftime('%H:%M:%S', time.localtime(meta.get('ended_ts') or 0)),
        os.path.basename(meta.get('browser') or 'chromium')))
    out.append('')
    out.append('## Steps')
    for i, step in enumerate(session['steps'], 1):
        out.append(_render_step(i, step))
    out.append('')
    out.append('## Replay notes')
    out.extend(_replay_notes(session))
    return '\n'.join(out) + '\n'


def _render_step(n, step):
    if step.get('kind') == 'navigate':
        title = (step.get('title') or '')[:40]
        nf = len(step.get('fields') or [])
        nl = len(step.get('links') or [])
        shot = (' [shot %s]' % step['shot']) if step.get('shot') else ''
        return ' %2d | LOAD   | %-52s | %s | %d fields, %d links%s' % (
            n, (step.get('url') or '')[:52], title, nf, nl, shot)
    kind = (step.get('kind') or '').upper()
    el = step.get('element') or {}
    short = step.get('selector') or best_selector(el)
    label = el.get('tag') or ''
    if el.get('id'):
        label = '%s#%s' % (label, el['id'])
    if el.get('text'):
        label = '%s "%s"' % (label, el['text'][:26])
    bits = []
    if step.get('kind') == 'input':
        if step.get('masked'):
            bits.append('typed %d chars (redacted)' % int(step.get('chars') or 0))
        else:
            bits.append('value="%s"' % (step.get('value') or ''))
    elif step.get('kind') == 'click' and not step.get('masked') and step.get('value'):
        bits.append('label="%s"' % step['value'])
    if step.get('href'):
        bits.append('-> %s' % step['href'])
    line = ' %2d | %-6s | %-52s | %s' % (n, kind, short[:52], label[:44])
    if bits:
        line = line + ' | ' + ' '.join(bits)
    if step.get('shot'):
        line += ' [shot %s]' % step['shot']
    if step.get('network'):
        line += ' (%d net rows)' % len(step['network'])
    return line


def _replay_notes(session):
    notes = []
    pages = [s for s in session['steps'] if s.get('kind') == 'navigate']
    urls = ' -> '.join((p.get('url') or '') for p in pages[-6:]) or 'none'
    notes.append('Pages visited at the end of the flow: %s' % urls)
    last_url = (pages[-1].get('url') if pages else '') or 'unknown'
    notes.append('Success marker: the flow ended on %s' % last_url)
    notes.append('Every redacted value must be supplied at replay time from the credential vault.')
    notes.append('Replay format: browser_act_actions.json in this directory speaks the')
    notes.append('browser_driver.py wire format (ops: goto/fill/click/submit).')
    return notes


def session_id():
    return 'rec-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(2)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def write_session(session, out_root):
    """Create ONE new recording directory atomically: build in a temp dir,
    rename into place, so a crash leaves no half-written Library entry."""
    meta = session['meta']
    label = re.sub(r'[^A-Za-z0-9._-]+', '-', meta['label'] or '') or 'session'
    final_dir = os.path.join(out_root, '%s-%s' % (meta['session_id'], label))
    os.makedirs(out_root, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix='browser_record_', dir=out_root)
    try:
        # The redaction invariant: nothing raw leaves this process. The
        # per-step drains already wrote redacted copies; the buffered rows
        # left over (tail rows with no following action step) are dropped,
        # never serialized.
        session['network_pending'] = []
        os.makedirs(os.path.join(tmp_dir, 'shots'), exist_ok=True)
        meta['redactions_applied'] = sum(1 for s in session['steps'] if s.get('masked'))
        meta['artifacts'] = {}
        for i, step in enumerate(session['steps'], 1):
            png = step.get('_pending_shot_png')
            if not png:
                continue
            name = 'shots/step-%03d.png' % i
            with open(os.path.join(tmp_dir, name), 'wb') as f:
                f.write(base64.b64decode(png))
            step['shot'] = name
            meta['artifacts'][name] = sha256_file(os.path.join(tmp_dir, name))
            del step['_pending_shot_png']
        actions_path = os.path.join(tmp_dir, 'browser_act_actions.json')
        actions = {'session': meta['session_id'], 'label': meta['label'],
                   'actions': actions_from_steps(session['steps'])}
        with open(actions_path, 'w') as f:
            json.dump(actions, f, indent=2)
        meta['artifacts']['browser_act_actions.json'] = sha256_file(actions_path)
        txt = render_txt(session)
        meta['artifacts']['session.txt'] = hashlib.sha256(txt.encode()).hexdigest()
        with open(os.path.join(tmp_dir, 'session.txt'), 'w') as f:
            f.write(txt)
        meta['artifacts']['session.json'] = hashlib.sha256(
            json.dumps(session, sort_keys=True).encode()).hexdigest()
        with open(os.path.join(tmp_dir, 'session.json'), 'w') as f:
            json.dump(session, f, indent=2, sort_keys=True)
        os.rename(tmp_dir, final_dir)
    finally:
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return final_dir


def find_browser(explicit=None):
    if explicit:
        if not os.path.isfile(explicit):
            raise FileNotFoundError('--browser path not found: %s' % explicit)
        return explicit
    env = os.environ.get('BROWSER_RECORD_BIN')
    if env and os.path.isfile(env):
        return env
    for cand in BROWSER_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    raise FileNotFoundError('no Chromium-family browser found; pass --browser /path')


class _CdpWire:
    """Thin CDP over one WebSocket. Sends are sequential; replies are
    matched by id wherever the code that issued the call continues reading
    from the shared queue, so an inline screenshot call never deadlocks the
    pump."""

    def __init__(self, ws):
        self._ws = ws
        self._next_id = 0
        self.waiters = {}          # call id -> asyncio.Future for pending replies

    async def call(self, method, params=None, session_id=None):
        self._next_id += 1
        msg = {'id': self._next_id, 'method': method, 'params': params or {}}
        if session_id:
            msg['sessionId'] = session_id
        await self._ws.send(json.dumps(msg))
        return self._next_id

    async def call_future(self, method, params=None, session_id=None):
        """Fire-and-forget call: send and return the future the pump loop
        will resolve when the reply arrives. Fire-and-forget on purpose --
        the pump must KEEP CONSUMING events while a screenshot is in flight
        (a nested queue-drain would eat end-of-stream markers and can
        deadlock the whole loop)."""
        self._next_id += 1
        fut = asyncio.get_event_loop().create_future()
        self.waiters[self._next_id] = fut
        msg = {'id': self._next_id, 'method': method, 'params': params or {}}
        if session_id:
            msg['sessionId'] = session_id
        await self._ws.send(json.dumps(msg))
        return fut


class _SessionState:
    """Per attached page target: screenshot cooldown state."""

    def __init__(self, target_id):
        self.target_id = target_id
        self.last_shot_ts = 0.0

    def drain_net_rows(self, session):
        """Attach this page's buffered network rows (redacted copies) to the
        newest step, then clear the buffer so the next step only carries its
        own rows. The raw buffer never lives longer than one step."""
        rows = [redact_network_row(r) for r in session['network_pending'] if r]
        if rows and session['steps']:
            session['steps'][-1]['network'] = rows[-20:]
        session['network_pending'] = []


async def _wait_for_devtools(stderr, timeout_s):
    """The Chrome binary prints its DevTools endpoint on stderr; parse it out
    -- port 0 in the argv means the real port exists only in this line."""
    deadline = time.time() + timeout_s
    pattern = re.compile(r'DevTools listening on (ws://\S+)')
    while time.time() < deadline:
        line = await stderr.readline()
        if not line:
            break
        m = pattern.search(line.decode('utf-8', 'replace'))
        if m:
            return m.group(1)
    raise TimeoutError('Chromium did not report a DevTools endpoint in %.0fs' % timeout_s)


async def _run_capture_async(session, browser_bin, start_url, port_arg, max_minutes):
    """Open the recorded browser, attach to every page target, pump CDP
    events into the reducer until SIGINT / --max-minutes / hard cap."""
    import websockets
    profile_dir = tempfile.mkdtemp(prefix='browser_record_profile_')
    argv = [browser_bin,
            '--remote-debugging-port=%d' % (port_arg or 0),
            '--user-data-dir=%s' % profile_dir,
            '--no-first-run', '--no-default-browser-check',
            '--disable-features=Translate',
            '--window-size=1280,900']
    if start_url:
        argv.append(start_url)
    argv.append('--new-window')
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        ws_url = await _wait_for_devtools(proc.stderr, CDP_CONNECT_TIMEOUT_S)
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            await _pump(session, ws)
    finally:
        try:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), SHUTDOWN_GRACE_S)
                except asyncio.TimeoutError:
                    proc.kill()
        except ProcessLookupError:
            pass
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)


async def _pump(session, ws):
    """Read the WebSocket into a queue and process one message at a time.
    Nested consumption (a screenshot request inside a binding event) is safe
    because handler and pump take turns on the same queue."""
    import websockets
    import asyncio as _asyncio
    wire = _CdpWire(ws)
    queue = _asyncio.Queue()

    async def reader():
        try:
            async for raw in ws:
                try:
                    queue.put_nowait(json.loads(raw))
                except ValueError:
                    pass
        except websockets.ConnectionClosed:
            pass
        finally:
            queue.put_nowait(_EOF)

    reader_task = asyncio.create_task(reader())
    # Auto-attach BEFORE the first navigation so nothing is missed.
    await wire.call('Target.setAutoAttach',
                    {'autoAttach': True, 'waitForDebuggerOnStart': False, 'flatten': True})
    max_minutes = session['meta'].get('_max_minutes') or 0
    deadline = (time.time() + max_minutes * 60) if max_minutes else None
    page_states = {}
    try:
        while True:
            if deadline and time.time() > deadline:
                return
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if msg is _EOF:
                return
            await _handle_one(session, wire, queue, page_states, msg)
    finally:
        reader_task.cancel()


async def _handle_one(session, wire, queue, page_states, msg):
    """One queue message: a reply resolves whoever is waiting on that id
    (events may still have been consumed on the way); anything else is an
    event to dispatch."""
    if 'method' not in msg:
        future = wire.waiters.get(msg.get('id'))
        if future is not None:
            del wire.waiters[msg['id']]
            if not future.done():
                future.set_result(msg)
        return
    method = msg.get('method') or ''
    sid = msg.get('sessionId')
    is_page = method == 'Target.attachedToTarget' \
        and msg['params'].get('targetInfo', {}).get('type') == 'page'
    if is_page:
        state = _SessionState(msg['params']['targetInfo']['targetId'])
        page_states[sid] = state
        await _attach_session(wire, sid)
    elif not sid:
        return
    elif method == 'Runtime.bindingCalled' and sid in page_states:
        await _on_sink(session, page_states.get(sid), wire, sid,
                       msg['params'].get('payload') or '{}')
    elif method in ('Network.requestWillBeSent', 'Network.responseReceived'):
        _net_row(session, msg['params'], method)


async def _fetch_shot(session, wire, step, fut, timeout_s=10.0):
    """Resolve one screenshot reply out-of-band. The reply lands whenever the
    pump loop processes it; the step is patched in place then. No reply
    (timeout, killed browser) simply leaves the step without a shot."""
    try:
        reply = await asyncio.wait_for(fut, timeout_s)
        png = ((reply or {}).get('result') or {}).get('data')
        if png:
            step['_pending_shot_png'] = png
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def _attach_session(wire, cdp_session):
    """Give one page target the fixed bundle, binding and network domains.
    No page JS beyond this bundle is ever evaluated."""
    for method, params in (('Runtime.enable', None),
                           ('Page.enable', None),
                           ('Network.enable', None),
                           ('Runtime.addBinding', {'name': BINDING_NAME}),
                           ('Page.addScriptToEvaluateOnNewDocument',
                            {'source': INJECTED_BUNDLE})):
        await wire.call(method, params, cdp_session)


def _net_row(session, params, method):
    """Buffer one network observation keyed by requestId (the response event
    later fills in status). Rows stay raw in the short-lived buffer; they are
    redacted when a step actually references them and wiped before the
    session is serialized."""
    req = params.get('request') or {}
    info = req if method == 'Network.requestWillBeSent' else (params.get('response') or {})
    updates = {
        'requestId': params.get('requestId'),
        'method': req.get('method') or '',
        'url': info.get('url') or req.get('url') or '',
        'status': (params.get('response') or {}).get('status'),
        'resourceType': params.get('type') or '',
    }
    # request headers first, response headers under a res: prefix (so the
    # shared redaction pass covers Authorization/Cookie AND Set-Cookie).
    headers = {}
    for prefix, source in (('', req), ('res:', (params.get('response') or {}))):
        for name, value in (source.get('headers') or {}).items():
            headers['%s%s' % (prefix, name)] = value
    if headers:
        updates['headers'] = headers
    idx = None
    for i, buffered in enumerate(session['network_pending']):
        if buffered.get('requestId') == updates['requestId']:
            idx = i
            break
    if idx is None:
        idx = len(session['network_pending'])
        session['network_pending'].append({})
    buffered = session['network_pending'][idx]
    for key, val in updates.items():
        if val not in (None, ''):
            buffered[key] = val
    if len(session['network_pending']) > MAX_NET_ROWS:
        del session['network_pending'][:len(session['network_pending']) - MAX_NET_ROWS]


async def _on_sink(session, state, wire, cdp_session, payload):
    """One binding message from the page. Action steps get this page's
    buffered network rows attached and, when the cooldown allows, a CDP
    screenshot request whose reply is applied later by _fetch_shot (the
    pump keeps consuming events meanwhile -- see _CdpWire.call_future)."""
    try:
        ev = json.loads(payload)
    except ValueError:
        return
    session['steps'].extend(aggregate_steps([ev]))
    step = session['steps'][-1]
    step['ts'] = (ev.get('t') / 1000) if ev.get('t') else time.time()
    if state:
        state.drain_net_rows(session)
    if step.get('kind') in ('load', 'route', 'navigate'):
        return
    if step.get('masked'):
        return  # never screenshot a step that involves a masked field
    now = time.time()
    if state and (now - state.last_shot_ts) < STEADY_SHOT_MS / 1000.0:
        return
    if state:
        state.last_shot_ts = now
    try:
        fut = await wire.call_future('Page.captureScreenshot', {'format': 'png'}, cdp_session)
        asyncio.ensure_future(_fetch_shot(session, wire, step, fut))
    except Exception:
        pass  # a missing screenshot must never lose the step


def run_capture(session, browser_bin, start_url, port_arg, max_minutes):
    """Blocking wrapper for main(): SIGINT (Ctrl-C) is the normal finish."""
    session['meta']['_max_minutes'] = max_minutes
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_capture_async(
            session, browser_bin, start_url, port_arg, max_minutes))
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def main(argv=None):
    cli = argparse.ArgumentParser(
        description='Record a browser session into teachable Library artifacts.')
    cli.add_argument('--label', required=True, help='short name for the artifacts')
    cli.add_argument('--url', help='initial page to open (optional)')
    cli.add_argument('--browser', help='path to Chromium/Chrome (else auto)')
    cli.add_argument('--out', default=DEFAULT_OUT_DIR, help='Library root for recordings')
    cli.add_argument('--max-minutes', type=float, default=0,
                     help='hard stop; 0 = run until Ctrl-C')
    cli.add_argument('--port', type=int, default=0, help='CDP port (0 = automatic)')
    args = cli.parse_args(argv)
    try:
        import websockets  # noqa: F401 -- CDP transport only
    except ImportError:
        print('browser_record needs the websockets package (serve already uses it).')
        return 2
    try:
        browser_bin = find_browser(args.browser)
    except FileNotFoundError as e:
        print(str(e))
        return 2
    session = {'meta': {'label': args.label, 'session_id': session_id(),
                        'started_ts': time.time(), 'ended_ts': None,
                        'browser': browser_bin, 'redactions_applied': 0,
                        'artifacts': {}},
               'steps': [], 'network_pending': []}
    print('Recording session %s -- Ctrl-C finishes and writes the artifacts' % (
        session['meta']['session_id']))
    aborted = None
    try:
        run_capture(session, browser_bin, args.url, args.port, args.max_minutes)
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001 -- fail-open: partial data still beats none
        aborted = e
    session['meta']['ended_ts'] = time.time()
    if aborted:
        session['meta']['aborted'] = '%s: %s' % (type(aborted).__name__, aborted)
    final_dir = write_session(session, args.out)
    if aborted:
        print('capture aborted (%s) -- partial session still written' % aborted)
    print('Wrote %s' % final_dir)
    print('  %s' % os.path.join(final_dir, 'session.txt'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
