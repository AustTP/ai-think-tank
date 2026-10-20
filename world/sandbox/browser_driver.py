#!/usr/bin/env python3
"""browser_act driver -- the ONLY browser capability an agent can reach from
inside the Work Room sandbox (/api/browser-act). A fixed, deterministic set of
playwright ops; no free-form JS is ever passed through.

This script runs INSIDE the sandbox container (baked at /opt/browser_driver.py),
so all of its network traffic rides the same internal SANDBOX_NETWORK + egress
allowlist proxy that any agent script already rides -- a page it cannot reach
through that proxy is a page it cannot load, full stop. The endpoint gates the
action (Jev) before this even runs; this script only carries out one already-
approved op.

Wire format (one JSON document):
  action:  {"op", "url", "selector", "value", "viewport", "timeoutMs",
            "workDir", "profileDir", "screenshotPath", "maxText"}
  result:  {"ok", "op", "url", "title", "text", "fields", "links",
            "screenshot", "error"}

Sessions persist across calls via `profileDir` (persistent context user data):
cookies/localStorage survive across browser_act calls inside the same sandbox,
so a logged-in session persists exactly like a human's browser profile would.

Exit code is always 0 -- the result JSON carries ok/error. A nonzero exit would
mean the harness itself died, which the endpoint reports as a sandbox failure.
"""
import json
import os
import re
import sys
import traceback


OPS = ('goto', 'read', 'fill', 'fill_many', 'login', 'click', 'submit', 'screenshot')
MAX_TEXT = int(os.environ.get('BROWSER_ACT_MAX_TEXT', '16000'))
MAX_FIELDS = 40
MAX_LINKS = 40

# Hard read-only backstop for the player-vetted social hosts. The server
# already refuses these ops, but the driver is the last place that sees the
# REAL current page URL (an omitted url must not turn into a bypass).
READ_ONLY_SOCIAL_HOSTS = (
    'facebook.com', 'www.facebook.com', 'm.facebook.com', 'mbasic.facebook.com',
    'instagram.com', 'www.instagram.com',
)


def _is_social_host(host):
    host = (host or '').strip().lower().rstrip('.')
    return any(host == h or host.endswith('.' + h) for h in READ_ONLY_SOCIAL_HOSTS)


def _social_host(url):
    try:
        from urllib.parse import urlparse
        return urlparse(url or '').hostname or ''
    except Exception:
        return ''


def _is_login_path(url):
    try:
        from urllib.parse import urlparse
        path = (urlparse(url or '').path or '/').lower()
    except Exception:
        return False
    return any(marker in path for marker in
               ('/login', '/accounts/login', '/checkpoint', '/challenge'))


def _social_write_guard(op, url):
    if not _is_social_host(_social_host(url)):
        return None
    if op in ('fill', 'fill_many', 'click', 'submit'):
        return ('read-only social policy: fill, click, and submit are disabled on '
                'this host; use op=login once on a login page, then only '
                'goto/read/screenshot')
    if op == 'login' and not _is_login_path(url):
        return 'read-only social policy: op=login is allowed only on a login path'
    return None


def _try_click(page, selector, timeout=2500):
    """Click the first matching element only if it is actually visible and
    enabled. Facebook renders a hidden/disabled input[type=submit] alongside
    its real button, so a blind click on the submit selector burns 30s."""
    try:
        loc = page.locator(selector).first
        loc.wait_for(state='visible', timeout=timeout)
        if not loc.is_enabled():
            return False
        loc.click(timeout=timeout)
        return True
    except Exception:
        return False


def _settle(page, ms):
    page.wait_for_timeout(ms)


def _captcha_state(page):
    """Detect visible CAPTCHA widgets and the text field they guard.

    This never solves anything. It marks candidates with a stable data
    attribute so the endpoint can hand a selector to the broker without
    guessing at site-specific markup.
    """
    try:
        return page.evaluate("""() => {
          const visible = (el) => {
            try {
              const r = el.getBoundingClientRect();
              const s = getComputedStyle(el);
              return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
            } catch (e) { return false; }
          };
          const nodes = Array.from(document.querySelectorAll('iframe,div,img,input,textarea'));
          const out = [];
          let idx = 0;
          for (const el of nodes) {
            const tag = el.tagName.toLowerCase();
            const src = (el.getAttribute('src') || '').toLowerCase();
            const id = (el.id || '').toLowerCase();
            const name = (el.getAttribute('name') || '').toLowerCase();
            const cls = (el.className || '').toString().toLowerCase();
            const ph = (el.getAttribute('placeholder') || '').toLowerCase();
            const aria = (el.getAttribute('aria-label') || '').toLowerCase();
            const text = [src, id, name, cls, ph, aria].join(' ');
            let kind = '';
            if (src.includes('recaptcha') || text.includes('g-recaptcha')) kind = 'recaptcha';
            else if (src.includes('hcaptcha') || text.includes('h-captcha')) kind = 'hcaptcha';
            else if (src.includes('turnstile') || text.includes('cf-turnstile')) kind = 'turnstile';
            else if (src.includes('arkose') || text.includes('funcaptcha')) kind = 'arkose';
            else if (text.includes('captcha') || text.includes('verify you are human') || text.includes('security check')) {
              if (tag === 'input' || tag === 'textarea') kind = 'input';
              else if (tag === 'img') kind = 'image';
              else kind = 'container';
            }
            if (!kind || !visible(el)) continue;
            const rect = el.getBoundingClientRect();
            const interactive = ['recaptcha', 'hcaptcha', 'turnstile', 'arkose'].includes(kind);
            if (interactive && (rect.width < 180 || rect.height < 70)) continue;
            if (kind === 'recaptcha' && src.includes('/anchor')) continue;
            const key = 'tt-captcha-' + (idx++);
            el.setAttribute('data-tt-captcha', key);
            out.push({
              kind: kind,
              tag: tag,
              selector: '[data-tt-captcha="' + key + '"]',
              id: id,
              name: name,
              src: src.slice(0, 200),
              text: (el.innerText || '').trim().slice(0, 120),
            });
          }
          for (const rec of out) {
            if (rec.tag === 'input' || rec.tag === 'textarea') continue;
            const base = document.querySelector(rec.selector);
            if (!base) continue;
            const scope = base.parentElement || document;
            const inp = scope.querySelector('input:not([type=hidden]):not([type=submit]), textarea');
            if (inp && visible(inp)) {
              const key = 'tt-captcha-input-' + (idx++);
              inp.setAttribute('data-tt-captcha-input', key);
              rec.inputSelector = '[data-tt-captcha-input="' + key + '"]';
            }
          }
          return out.slice(0, 5);
        }""") or []
    except Exception:
        return []


def _page_state(page, max_text):
    """Snapshot of the live page: url/title, visible text, interactive form
    fields, links. Truncated so a huge page can't blow the sandbox stdout cap."""
    try:
        url = page.url
    except Exception:
        url = ''
    try:
        title = page.title() or ''
    except Exception:
        title = ''
    text = ''
    try:
        text = page.evaluate('document.body ? document.body.innerText : ""') or ''
    except Exception:
        text = ''
    text = text[:max_text]
    fields = []
    try:
        fields = page.evaluate(
            "Array.from(document.querySelectorAll('input,select,textarea,button,[role=button]')).slice(0, %d).map(el => {"
            "const r = el.getBoundingClientRect(); const s = getComputedStyle(el);"
            "const visible = r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';"
            "return {"
            "tag: el.tagName.toLowerCase(),"
            "type: el.getAttribute('type') || '',"
            "role: el.getAttribute('role') || '',"
            "name: el.getAttribute('name') || '',"
            "id: el.id || '',"
            "placeholder: el.getAttribute('placeholder') || '',"
            "ariaLabel: el.getAttribute('aria-label') || '',"
            "visible: visible,"
            "disabled: !!el.disabled,"
            "text: (el.textContent || '').trim().slice(0, 80),"
            "valueLength: (el.value || '').length,"
            "outer: (el.outerHTML || '').slice(0, 500)"
            "};})" % MAX_FIELDS
        ) or []
    except Exception:
        fields = []
    links = []
    try:
        links = page.evaluate(
            "Array.from(document.querySelectorAll('a[href]')).slice(0, %d)"
            ".map(a => ({text: (a.textContent || '').trim().slice(0, 100), href: a.href}))" % MAX_LINKS
        ) or []
    except Exception:
        links = []
    return {'url': url, 'title': title[:200], 'text': text, 'fields': fields, 'links': links,
            'captchas': _captcha_state(page)}


def _launch(playwright, action):
    args = ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
            '--disable-blink-features=AutomationControlled', '--no-first-run',
            '--no-default-browser-check']
    # The sandbox network is INTERNAL (no route out at all); the ONLY way to
    # the internet is the egress allowlist proxy (see serve.ensure_sandbox_networking
    # + sandbox_proxy.py). Chromium ignores http_proxy/http_proxy env vars by
    # default, so the proxy must be passed explicitly as --proxy-server or the
    # browser has no way out and every goto fails with a network error.
    proxy = os.environ.get('BROWSER_PROXY_URL') or os.environ.get('https_proxy') or os.environ.get('http_proxy') or ''
    if proxy:
        proxy = proxy.replace('https://', 'http://').replace('HTTP://', 'http://')
        if proxy.startswith('http://'):
            args.append(f'--proxy-server={proxy}')
    profile_dir = action.get('profileDir') or '/workspace/.browser-profile'
    viewport = action.get('viewport') or '1280x900'
    try:
        w, h = str(viewport.split('x')[0]), str(viewport.split('x')[1])
        vp = {'width': int(w), 'height': int(h)}
    except Exception:
        vp = {'width': 1280, 'height': 900}
    # A real Chrome UA, not Playwright's default HeadlessChrome string. Sites
    # behind CDN WAFs (CloudFront, Fastly, Akamai) serve a 403 to any client
    # whose UA says HeadlessChrome regardless of how real the rest of the
    # fingerprint looks -- the sandbox is egressed through the same IP as the
    # host (verified: a host curl with a normal UA gets 200 from HF, the
    # sandbox browser got 403 purely on this string). Masking the UA is the
    # least that unblocks legitimate logins to player-vetted, allowlisted
    # sites the player explicitly wants agents to reach.
    ctx = playwright.chromium.launch_persistent_context(
        profile_dir, headless=(os.environ.get('BROWSER_HEADLESS', '0') == '1'), args=args, viewport=vp, accept_downloads=False,
        user_agent=('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/131.0.0.0 Safari/537.36'),
        locale='en-US', timezone_id='America/New_York',
    )
    try:
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    except Exception:
        pass
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def run(action):
    op = action.get('op')
    if op not in OPS:
        return {'ok': False, 'op': op, 'error': f'unknown op {op!r}; expected one of {", ".join(OPS)}'}
    # Validate required params BEFORE launching a browser -- a missing arg
    # should fail fast with a clear message, not after a wasted chromium boot.
    if op == 'goto' and not action.get('url'):
        return {'ok': False, 'op': op, 'error': 'goto requires url'}
    if op == 'fill' and not action.get('selector'):
        return {'ok': False, 'op': op, 'error': 'fill requires selector'}
    if op == 'fill_many' and not action.get('fields'):
        return {'ok': False, 'op': op, 'error': 'fill_many requires fields (list of {selector, value})'}
    if op == 'login' and not action.get('fields'):
        return {'ok': False, 'op': op, 'error': 'login requires fields (list of {selector, value})'}
    if op == 'click' and not action.get('selector'):
        return {'ok': False, 'op': op, 'error': 'click requires selector'}
    timeout_ms = int(action.get('timeoutMs') or 30000)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return {'ok': False, 'op': op, 'error': f'playwright not available in sandbox image: {e}'}
    try:
        with sync_playwright() as p:
            ctx, page = _launch(p, action)
            try:
                # Every op that carries a url navigates there first. Without
                # this, fill/click/submit/read each open a blank page (the
                # persistent profile restores cookies but not the page itself),
                # so a fill would time out waiting for a selector that was
                # never on screen. goto is the only op that skips this -- it IS
                # the navigation.
                if op != 'goto' and action.get('url'):
                    page.goto(action.get('url'), timeout=timeout_ms, wait_until='domcontentloaded')
                    _settle(page, 2500)
                social_error = _social_write_guard(op, page.url)
                if social_error:
                    return {'ok': False, 'op': op, 'url': page.url,
                            'error': social_error,
                            'socialReadOnly': True}
                if op == 'goto':
                    page.goto(action.get('url'), timeout=timeout_ms, wait_until='domcontentloaded')
                    _settle(page, 2500)
                    state = _page_state(page, int(action.get('maxText') or MAX_TEXT))
                    return {'ok': True, 'op': op, 'screenshot': _maybe_shot(ctx, page, action), **state}
                if op == 'read':
                    state = _page_state(page, int(action.get('maxText') or MAX_TEXT))
                    return {'ok': True, 'op': op, 'screenshot': _maybe_shot(ctx, page, action), **state}
                if op == 'fill':
                    page.fill(action.get('selector'), action.get('value') or '', timeout=timeout_ms)
                    _settle(page, 500)
                    state = _page_state(page, int(action.get('maxText') or MAX_TEXT))
                    return {'ok': True, 'op': op, 'screenshot': _maybe_shot(ctx, page, action), **state}
                if op == 'fill_many':
                    for f in action.get('fields', []):
                        page.locator(f['selector']).first.wait_for(state='visible', timeout=timeout_ms)
                        page.fill(f['selector'], f.get('value') or '', timeout=timeout_ms)
                    _settle(page, 500)
                    state = _page_state(page, int(action.get('maxText') or MAX_TEXT))
                    return {'ok': True, 'op': op, 'screenshot': _maybe_shot(ctx, page, action), **state}
                if op == 'login':
                    fields = action.get('fields', [])
                    filled_lengths = []
                    for f in fields:
                        loc = page.locator(f['selector']).first
                        loc.wait_for(state='visible', timeout=timeout_ms)
                        # Type real key events, not just set .value: Facebook's
                        # login form is React-controlled and its submit control
                        # stays inert when the synthetic input event is skipped.
                        try:
                            loc.click(timeout=timeout_ms)
                            loc.fill('', timeout=timeout_ms)
                            loc.press_sequentially(f.get('value') or '', delay=20, timeout=timeout_ms)
                        except Exception:
                            loc.fill(f.get('value') or '', timeout=timeout_ms)
                        try:
                            filled_lengths.append(len(loc.input_value()))
                        except Exception:
                            filled_lengths.append(-1)
                    _settle(page, 1500)
                    before = page.url
                    submit_used = ''
                    # Press Enter in the last filled field (usually password).
                    # This targets the form even when the visible submit button
                    # is a div/role=button and a hidden input[type=submit] exists.
                    if fields:
                        try:
                            page.locator(fields[-1]['selector']).first.press('Enter', timeout=5000)
                            submit_used = 'enter'
                        except Exception:
                            page.keyboard.press('Enter')
                            submit_used = 'enter-global'
                    _settle(page, 3000)
                    if page.url == before:
                        # Prefer the accessible submit control. Facebook's real
                        # button is often a div[role=button] named "Log in",
                        # while input[type=submit] is hidden/disabled.
                        try:
                            page.get_by_role('button', name=re.compile('log in', re.I)).first.click(timeout=8000)
                            submit_used = 'role:button:log-in'
                            _settle(page, 3000)
                        except Exception:
                            candidates = [action.get('submitSelector'),
                                          'form button[type=submit]', 'form [role=button]',
                                          'form input[type=submit]',
                                          'button[type=submit]', 'div[role=button]:has-text("Log in")',
                                          'input[type=submit]']
                            for sel in candidates:
                                if sel and _try_click(page, sel):
                                    submit_used = sel
                                    _settle(page, 3000)
                                    break
                    if page.url == before:
                        # Last resort: submit the real form, not a visual
                        # wrapper. Facebook's visible "Log in" is a
                        # div[role=button] that may not trigger onSubmit.
                        try:
                            submitted = page.evaluate(
                                """() => {
                                    const f = document.querySelector('form');
                                    if (!f) return false;
                                    if (f.requestSubmit) { f.requestSubmit(); }
                                    else { f.submit(); }
                                    return true;
                                }""")
                            if submitted:
                                submit_used = (submit_used + '+form-submit').strip('+')
                                _settle(page, 4000)
                        except Exception:
                            pass
                    state = _page_state(page, int(action.get('maxText') or MAX_TEXT))
                    return {'ok': True, 'op': op, 'submitUsed': submit_used,
                            'filledFieldLengths': filled_lengths,
                            'urlAfterSubmit': page.url,
                            'screenshot': _maybe_shot(ctx, page, action), **state}
                if op == 'click':
                    page.click(action.get('selector'), timeout=timeout_ms)
                    _settle(page, 2500)
                    state = _page_state(page, int(action.get('maxText') or MAX_TEXT))
                    return {'ok': True, 'op': op, 'screenshot': _maybe_shot(ctx, page, action), **state}
                if op == 'submit':
                    selector = action.get('selector') or ''
                    try:
                        if selector:
                            page.click(selector, timeout=timeout_ms)
                        else:
                            page.locator('button[type=submit], input[type=submit]').first.click(timeout=timeout_ms)
                    except Exception:
                        page.keyboard.press('Enter')
                    _settle(page, 3000)
                    state = _page_state(page, int(action.get('maxText') or MAX_TEXT))
                    return {'ok': True, 'op': op, 'screenshot': _maybe_shot(ctx, page, action), **state}
                if op == 'screenshot':
                    return {'ok': True, 'op': op, 'screenshot': _maybe_shot(ctx, page, action),
                            **_page_state(page, 0)}
                return {'ok': False, 'op': op, 'error': f'unhandled op {op!r}'}  # pragma: no cover
            except Exception as e:
                # A selector timeout on a CAPTCHA page must still return the
                # page state, or the endpoint cannot see the challenge and
                # relay it to the owner.
                state = {}
                try:
                    state = _page_state(page, int(action.get('maxText') or MAX_TEXT))
                except Exception:
                    pass
                return {'ok': False, 'op': op, 'error': f'{type(e).__name__}: {e}',
                        'trace': traceback.format_exc(limit=4)[-1200:], **state}
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        tb = traceback.format_exc(limit=4)
        return {'ok': False, 'op': op, 'error': f'{type(e).__name__}: {e}', 'trace': tb[-1200:]}


def _maybe_shot(ctx, page, action):
    path = action.get('screenshotPath')
    if not path:
        return None
    try:
        page.screenshot(path=path)
        return os.path.basename(path)
    except Exception as e:
        return f'__screenshot_failed__: {e}'


def main(argv):
    action = {}
    try:
        if len(argv) > 1 and argv[1]:
            with open(argv[1], 'r') as f:
                action = json.load(f)
        else:
            action = json.load(sys.stdin.read())
    except Exception:
        action = {}
    result = run(action or {})
    sys.stdout.write(json.dumps(result))
    # Also persist to the result file (if one was given) so the endpoint can
    # read the full result back from the mounted /workspace instead of relying
    # on stdout alone -- stdout can be truncated by the sandbox capture cap.
    result_path = (action or {}).get('resultPath')
    if result_path:
        try:
            with open(result_path, 'w') as f:
                json.dump(result, f)
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))