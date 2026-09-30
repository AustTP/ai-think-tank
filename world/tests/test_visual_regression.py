"""Automated visual regression check against a REAL running server.

Not hermetic, and deliberately NOT wired into run_all.sh's default suite --
every other test in this directory is a pure-function test with no live
server/DB/browser (see test_bank.py's own docstring on why). This one can't
be: the whole point is to look at what the renderer actually draws in a real
browser, which no unit test can see.

Borrowed from a real comparison against Hermes Town, which runs
`verify:characters`/`verify:world` as real Playwright checks in CI, catching
exactly the class of bug ("undefined" nameplates, overlapping sprites) that
A human once spent an entire live-debugging session finding manually. This
is the automated version of the checks a human was doing by eye.

Read-only: never mutates state, so it's safe to run against the real
think_tank.db a developer cares about. Requires a server already running
(default http://127.0.0.1:8936) and `playwright` with a Chromium install
(`python3 -m playwright install chromium` once, if not already present).

The app sits behind a login page -- this needs real credentials, supplied via
env vars (never hardcoded into a committed test file):
    AI_THINK_TANK_TEST_USERNAME (defaults to 'admin')
    AI_THINK_TANK_TEST_PASSWORD (required -- the real admin password)

Usage:
    AI_THINK_TANK_TEST_PASSWORD=... python3 tests/test_visual_regression.py [base_url]

Exits 0 on a clean pass, 1 if any check fails, 2 if the server/browser/login
couldn't be reached at all (distinguished so a CI job can tell "the app is
broken" from "the environment isn't set up").
"""
import os
import sys


def _bounding_boxes_overlap(a, b, w=20, h=16):
    return (a['x'] < b['x'] + w and a['x'] + w > b['x']
            and a['y'] < b['y'] + h and a['y'] + h > b['y'])


def run(base_url):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed (pip install playwright && "
              "python3 -m playwright install chromium)")
        return 2

    username = os.environ.get('AI_THINK_TANK_TEST_USERNAME', 'admin')
    password = os.environ.get('AI_THINK_TANK_TEST_PASSWORD')
    if not password:
        print("SKIP: AI_THINK_TANK_TEST_PASSWORD not set -- this app requires login, "
              "and a real password is never hardcoded into a committed test file.")
        return 2

    failures = []
    console_errors = []

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.on("console", lambda msg: console_errors.append(msg.text)
                     if msg.type == "error" and "favicon" not in msg.text else None)
            page.goto(base_url, timeout=15000)
            if 'Sign in' in page.title():
                page.fill('#u', username)
                page.fill('#p', password)
                page.click('button[type="submit"]')
                page.wait_for_load_state('load', timeout=10000)
            page.wait_for_timeout(1500)  # let the first render + one poll cycle land
        except Exception as e:
            print(f"COULD NOT REACH/LOG IN TO {base_url}: {e}")
            return 2

        # Pull the live AGENTS dict straight from the page's own JS state --
        # the same ground truth the renderer draws from, not a screenshot
        # pixel-diff (which is fragile to font/theme/zoom and tells you THAT
        # something looks wrong, not WHAT). This is the "what does the app
        # itself believe is true" check.
        try:
            agents = page.evaluate("""() => {
                const out = [];
                for (const id in AGENTS) {
                    const a = AGENTS[id];
                    const drawn = typeof agentIsDrawn === 'function' ? agentIsDrawn(a) : (a.visible && !a.offDuty);
                    if (!drawn) continue;
                    out.push({id, name: a.name, x: a.x, y: a.y});
                }
                return out;
            }""")
        except Exception as e:
            print(f"COULD NOT READ AGENTS FROM PAGE: {e}")
            browser.close()
            return 2

        # Check 1: no agent that's actually being drawn has an undefined/empty name.
        # This is the exact bug ("undefined" rendered over an agent's head) found
        # and fixed in live debugging.
        for a in agents:
            if not a.get('name') or a['name'] == 'undefined':
                failures.append(f"agent {a['id']} is drawn with no real name: {a.get('name')!r}")

        # Check 2: no two DRAWN agents occupy overlapping ground -- the doorway
        # pileup bug found and fixed in live debugging.
        for i in range(len(agents)):
            for j in range(i + 1, len(agents)):
                if _bounding_boxes_overlap(agents[i], agents[j]):
                    failures.append(
                        f"agents {agents[i]['id']} and {agents[j]['id']} overlap at "
                        f"~({agents[i]['x']:.0f},{agents[i]['y']:.0f})")

        # Check 3: no real console errors during load (favicon 404 is filtered
        # above as the one known, harmless exception).
        for err in console_errors:
            failures.append(f"console error: {err}")

        browser.close()

    print(f"checked {len(agents)} drawn agent(s), {len(console_errors)} console error(s)")
    if failures:
        print(f"\nFAIL ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS")
    return 0


if __name__ == '__main__':
    url = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8936'
    sys.exit(run(url))
