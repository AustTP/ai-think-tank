"""Cloudflare / Turnstile challenge helpers for browser_driver.

Split out so every script stays under 1,000 lines. Imported by
browser_driver.py inside the sandbox image (both files are copied to
/opt, so a plain sibling import resolves).
"""
import os
import random
import re
import time


CF_FRAME_URL = re.compile(
    r'^https?://challenges\.cloudflare\.com/cdn-cgi/challenge-platform/.*')
CF_MAX_SOLVE_ATTEMPTS = 3
CF_POLL_DEADLINE_S = 5.0
CF_CLEAR_TIMEOUT_S = 10.0


def _cf_mode(action):
    """Which automatic CAPTCHA work this run may do. Reads the action first so
    the server can decide per request; falls back to the container env."""
    mode = str((action or {}).get('autoSolve')
               or os.environ.get('CAPTCHA_AUTO_SOLVE') or 'off').strip().lower()
    return mode if mode in ('cloudflare', 'all') else 'off'


def _cf_challenge_type(page):
    """Return the Cloudflare challenge type the page currently carries.

    Mirrors Scrapling's marker read: the challenge script declares its cType
    in page HTML. 'embedded' is a Turnstile widget on an otherwise normal
    page. Returns None when no marker is present."""
    try:
        html = page.content() or ''
    except Exception:
        return None
    for ctype in ('non-interactive', 'managed', 'interactive'):
        if "cType: '%s'" % ctype in html or 'cType: "%s"' % ctype in html:
            return ctype
    if re.search(r'challenges\.cloudflare\.com/turnstile/v', html):
        return 'embedded'
    return None


def _cf_frame(page):
    try:
        return page.frame(url=CF_FRAME_URL)
    except Exception:
        return None


def _cf_cleared(page, challenge_type):
    if challenge_type == 'embedded':
        return True
    return _cf_challenge_type(page) is None


def _wait_cf_cleared(page, challenge_type, timeout_s=CF_CLEAR_TIMEOUT_S):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _cf_cleared(page, challenge_type):
            return True
        page.wait_for_timeout(200)
    return _cf_cleared(page, challenge_type)


def _cf_wait_idle(page, timeout_ms=5000):
    try:
        page.wait_for_load_state('networkidle', timeout=timeout_ms)
    except Exception:
        pass


def _element_box(page, selector):
    """Bounding box of the last matching element, or None. Playwright returns
    None for a hidden or absent element, which is what the caller wants."""
    try:
        return page.locator(selector).last.bounding_box()
    except Exception:
        return None


def _click_point(page, x, y):
    try:
        page.mouse.move(x - random.randint(4, 12), y - random.randint(4, 12))
        page.wait_for_timeout(random.randint(90, 220))
        page.mouse.click(x, y, delay=random.randint(90, 200))
        return True
    except Exception:
        return False


def _click_cf_box(page, box):
    if not box:
        return False
    return _click_point(page, box['x'] + random.randint(26, 28),
                        box['y'] + random.randint(25, 27))


def _solve_cloudflare(page, action):
    """Scrapling-style Turnstile/Interstitial solver.

    It never reads or submits a token. It waits for the widget, clicks the
    checkbox area like a person, waits for the challenge marker to clear, and
    retries a bounded number of times. Anything unusual is reported back as
    unsolved so the existing owner relay still gets a chance."""
    if _cf_mode(action) == 'off':
        return None
    challenge_type = None
    deadline = time.monotonic() + CF_POLL_DEADLINE_S
    for poll in range(51):
        if poll % 5 == 0 or time.monotonic() >= deadline:
            challenge_type = _cf_challenge_type(page)
        if challenge_type or time.monotonic() >= deadline:
            break
        page.wait_for_timeout(100)
    if not challenge_type:
        return {'attempted': False, 'solved': False, 'kind': 'turnstile'}
    attempts = 0
    solved = False
    error = ''
    while attempts < CF_MAX_SOLVE_ATTEMPTS and not solved:
        attempts += 1
        try:
            if challenge_type == 'non-interactive':
                _cf_wait_idle(page, 10000)
                solved = _wait_cf_cleared(page, challenge_type)
            elif challenge_type == 'embedded':
                frame = _cf_frame(page)
                clicked = False
                if frame is not None:
                    try:
                        frame.locator('input[type="checkbox"]').first.click(
                            timeout=10000, delay=random.randint(90, 200))
                        clicked = True
                    except Exception:
                        clicked = False
                if not clicked:
                    clicked = _click_cf_box(
                        page, _element_box(
                            page, '#cf-turnstile, .cf-turnstile, [data-sitekey]'))
                _cf_wait_idle(page, 5000)
                solved = bool(clicked)
            else:
                frame = _cf_frame(page)
                box = None
                if frame is not None:
                    try:
                        frame.wait_for_load_state('load', timeout=5000)
                    except Exception:
                        pass
                    try:
                        element = frame.frame_element()
                        if element.is_visible():
                            box = element.bounding_box()
                    except Exception:
                        box = None
                if not box:
                    box = _element_box(
                        page,
                        '#cf_turnstile div, #cf-turnstile div, .turnstile>div>div, '
                        '.main-content p+div>div>div')
                if box:
                    _click_cf_box(page, box)
                    _cf_wait_idle(page, 5000)
                    solved = _wait_cf_cleared(page, challenge_type)
            if not solved:
                challenge_type = _cf_challenge_type(page) or challenge_type
        except Exception as e:
            error = '%s: %s' % (type(e).__name__, e)
            break
    return {'attempted': True, 'solved': bool(solved), 'kind': 'turnstile',
            'challengeType': challenge_type, 'attempts': attempts,
            'error': error[:200]}


def _autosolve(page, action):
    if _cf_mode(action) == 'off':
        return None
    try:
        return _solve_cloudflare(page, action)
    except Exception as e:
        return {'attempted': True, 'solved': False, 'kind': 'turnstile',
                'error': '%s: %s' % (type(e).__name__, e)}
