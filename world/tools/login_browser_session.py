#!/usr/bin/env python3
"""Runs INSIDE the login container (world/tools/agent_browser_login.py).

Opens the SAME Playwright Chromium build the browser lane uses, headful on
the container's Xvfb, with the agent's persistent profile mounted at
/browser-profile -- the exact shape the lane's persistent context uses, so
cookies written here are read later by browser_act. Navigates to LOGIN_URL
and stays alive until the context closes or the wrapper's timeout ends.
No proxy: this container rides the default bridge (operator session, not an
agent session).
"""
import os
import signal
import time

from playwright.sync_api import sync_playwright


def main():
    profile = '/browser-profile'
    url = os.environ.get('LOGIN_URL') or 'https://accounts.google.com'
    args = ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
            '--disable-blink-features=AutomationControlled', '--no-first-run',
            '--no-default-browser-check']
    running = True

    def _stop(_sig, _frm):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            profile, headless=False, args=args,
            viewport={'width': 1280, 'height': 900}, accept_downloads=False)
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', "
            "{get: () => undefined});")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(url, timeout=60000)
        except Exception as exc:
            print('goto failed (the page may still be usable):', exc,
                  flush=True)
        while running and ctx.pages:
            time.sleep(2)
        try:
            ctx.close()
        except Exception:
            pass
    print('login session ended', flush=True)


if __name__ == '__main__':
    main()
