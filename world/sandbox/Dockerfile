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
    black==24.10.0 \
    mypy==1.8.0 \
    bandit==1.9.4 \
    pytest==7.4.0 \
    pytest-cov==7.1.0
WORKDIR /workspace
RUN python -m pip install --no-cache-dir \
    numpy==2.2.6 pandas==2.2.3 scikit-learn==1.6.1 yfinance==0.2.65
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
