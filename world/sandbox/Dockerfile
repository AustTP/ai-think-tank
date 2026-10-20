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
# unconstrained reinstall of flake8==6.0.0 can pull newer plugin packages that
# crash inside flake8's _import_plugins loader before any lint runs, which
# surfaced as unfixable pipeline failures on tasks the agent never controlled.
RUN python -m pip install --no-cache-dir \
    flake8==6.0.0 \
    pycodestyle==2.10.0 \
    pyflakes==3.0.1 \
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

# Node.js for the JS half of the quality gate. wrq-8 asked for a vanilla-JS
# simulation engine; the sandbox had no node, so `node --check`/`node --test`
# died with exit 127 (command not found) and every JS task failed its quality
# gate forever. Node 20 LTS via NodeSource (pinned major, same offline-bake
# principle as the Python tools above: installed HERE, not at agent runtime).
# node --check parses .mjs/.js (ESM) without executing; node --test runs the
# *.test.mjs suites the agents write with the built-in node:test runner.
RUN apt-get update && apt-get install -y ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs xauth \
    && rm -rf /var/lib/apt/lists/*

# autopep8: agents reach for `python -m autopep8 --in-place ...` to auto-fix
# the flake8 E/W errors the gate rejects on. It was not in the image, so every
# attempt burned a review cycle on "No module named autopep8" before the agent
# could hand-fix the file. 2.0.4 is the only release whose dependency range
# still accepts pycodestyle==2.10.0 (flake8 6.0.0's hard pin): 2.1.2+ require
# pycodestyle>=2.11, which would either ResolutionImpossible against the pin
# or silently upgrade pycodestyle and crash flake8's plugin loader (the
# _import_plugins failure this file's header already documents). Placed AFTER
# the node layer on purpose: this RUN reuses the cached flake8/mypy/bandit/
# pytest/chromium layers above and only executes this one pip install, instead
# of invalidating the whole build.
RUN python -m pip install --no-cache-dir autopep8==2.0.4 pycodestyle==2.10.0

WORKDIR /workspace