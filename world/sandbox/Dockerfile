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
RUN python -m pip install --no-cache-dir \
    flake8==6.0.0 \
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
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace