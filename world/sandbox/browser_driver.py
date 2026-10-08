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
import sys
import traceback


OPS = ('goto', 'read', 'fill', 'click', 'submit', 'screenshot')
MAX_TEXT = int(os.environ.get('BROWSER_ACT_MAX_TEXT', '16000'))
MAX_FIELDS = 40
MAX_LINKS = 40


def _settle(page, ms):
    page.wait_for_timeout(ms)


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
            "Array.from(document.querySelectorAll('input,select,textarea,button')).slice(0, %d).map(el => ({"
            "tag: el.tagName.toLowerCase(),"
            "type: el.getAttribute('type') || '',"
            "name: el.getAttribute('name') || '',"
            "id: el.id || '',"
            "placeholder: el.getAttribute('placeholder') || '',"
            "ariaLabel: el.getAttribute('aria-label') || '',"
            "text: (el.textContent || '').trim().slice(0, 80)"
            "}))" % MAX_FIELDS
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
    return {'url': url, 'title': title[:200], 'text': text, 'fields': fields, 'links': links}


def _launch(playwright, action):
    args = ['--no-sandbox', '--disable-gpu']
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
    ctx = playwright.chromium.launch_persistent_context(
        profile_dir, headless=True, args=args, viewport=vp, accept_downloads=False,
    )
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
                return {'ok': False, 'op': op, 'error': f'unhandled op {op!r}'}
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