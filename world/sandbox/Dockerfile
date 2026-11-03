FROM python:3.12-slim
# Coding-standard toolchain baked into the Work Room sandbox image (Cut 4).
# Agents write real code in a network-disabled Docker sandbox (/api/execute,
# /api/pipeline). flake8/mypy/bandit/pytest-cov are installed HERE so the
# quality gate runs offline, deterministically, at every agent build -- pinned
# to the exact versions the think tank's own repo gate uses (world/tests/run_all.sh).
#
# Build once on the host:
#   docker build -t ai-think-tank-work-sandbox world/sandbox
# serve.py SANDBOX_IMAGE points at it.
# flake8's own deps (pycodestyle/pyflakes/mccabe) are pinned too: an
# unconstrained reinstall can pull plugin packages that crash inside flake8's
# _import_plugins loader before any lint runs, which surfaced as unfixable
# pipeline failures on tasks the agent never controlled. 7.1.1/2.12.1/3.2.0
# mirror requirements.txt: flake8 6.0.0 + Python 3.12 misparses PEP-701
# f-strings and flagged E201/E202/E713 false positives inside plain string
# text (DEV-135 reviews, 2026-11-03), so the image must lint like the host.
RUN python -m pip install --no-cache-dir \
    flake8==7.1.1 \
    pycodestyle==2.12.1 \
    pyflakes==3.2.0 \
    mccabe==0.7.0 \
    mypy==1.8.0 \
    bandit==1.9.4 \
    pytest==7.4.0 \
    pytest-cov==7.1.0

# browser_act (Cut 2026-10-08): Playwright + Chromium baked into the sandbox
# image, plus the fixed-op browser driver at /opt/browser_driver.py, so the
# /api/browser-act endpoint can run real headed-browser form interactions
# inside the same network-isolated container every other agent command runs
# in. The driver is a fixed script the endpoint invokes with ONE op per run --
# it is the only browser entry point an agent has, deliberately (no free-form
# JS, no arbitrary playwright scripts passed through).
RUN python -m pip install --no-cache-dir playwright==1.60.0
RUN python -m playwright install --with-deps chromium
COPY browser_driver.py /opt/browser_driver.py
COPY browser_cf.py /opt/browser_cf.py

# Node.js for the JS half of the quality gate. wrq-8 asked for a vanilla-JS
# simulation engine; the sandbox had no node, so `node --check`/`node --test`
# died with exit 127 (command not found) and every JS task failed its quality
# gate forever. Node 20 LTS via NodeSource (pinned major, same offline-bake
# principle as the Python tools above: installed HERE, not at agent runtime).
# node --check parses .mjs/.js (ESM) without executing; node --test runs the
# *.test.mjs suites the agents write with the built-in node:test runner.
# x11vnc + noVNC/websockify back the player takeover window (see
# world/browser_session.py): the session container runs Xvfb, Chromium,
# x11vnc on localhost, and websockify serving /usr/share/novnc on the one
# loopback-published port. Without these the image can still run one-shot
# browser_act ops; only the live takeover view fails.
RUN apt-get update && apt-get install -y ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs xauth x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*

# autopep8: agents reach for `python -m autopep8 --in-place ...` to auto-fix
# the flake8 E/W errors the gate rejects on. It was not in the image, so every
# attempt burned a review cycle on "No module named autopep8" before the agent
# could hand-fix the file. 2.3.1 is the line whose dependency range accepts
# pycodestyle==2.12.1 (flake8 7.1.1's pin, matching requirements.txt); older
# 2.0.x pins require pycodestyle<2.11 and would fight the upgraded stack.
# Placed AFTER the node layer on purpose: this RUN reuses the cached
# flake8/mypy/bandit/pytest/chromium layers above and only executes this one
# pip install, instead of invalidating the whole build.
RUN python -m pip install --no-cache-dir autopep8==2.3.1 pycodestyle==2.12.1 black==24.10.0

WORKDIR /workspace