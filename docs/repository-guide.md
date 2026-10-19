# Repository guide

Keep the existing layout and place related changes beside their current owners.

| Path | Purpose |
| --- | --- |
| `world/` | Application backend, browser UI, and agent execution modules. |
| `world/tests/` | Python tests and JavaScript regression harnesses. |
| `world/assets/` | Versioned images and room assets used by the application. |
| `world/sandbox/` | Docker image for isolated agent execution and quality tools. |
| `scripts/` | Developer utilities, dataset preparation, and their tests. |
| `docs/` | Focused operational and development documentation. |
| `datasets/` | Dataset documentation and tooling inputs; local recordings stay ignored. |
| `agents/` | Agent working directories and materialized runtime identities. |
| `library/` | Local reports, artifacts, and knowledge documents; ignored by Git. |
| `sandboxes/` | Local story workspaces and recovery checkpoints; ignored by Git. |

`DESIGN.md` describes the architecture. `requirements.txt` holds the runtime and
quality-tool dependencies; `.github/workflows/ci.yml` defines the CI checks.

## Local configuration and state

Copy `.env.template` to `.env` and supply your own credentials. The template
contains the basic remote/local model configuration; optional integrations are
documented alongside their features in the README. Do not copy credentials into
source, examples, or tests.

The SQLite database, its backups, generated agent reports, Library contents,
and sandbox checkpoints belong to the local installation. Ignore rules keep
them out of future Git changes; they do not delete local data or erase Git history.
Preserve active recovery checkpoints when cleaning a workspace.

## Development checks

From the repository root:

```sh
python -m pytest world/tests/test_review_model.py scripts/test_prepare_unsloth_dataset.py
node world/tests/test_work_graph.cjs
```

`pytest.ini` supplies module paths and limits default discovery to application
and script tests, keeping generated Library and sandbox code out of collection.
`python -m pytest` selects all Python tests; `bash world/tests/run_all.sh` runs the
existing regression harness. CI also runs lint, type, and security checks.
Some application tests need Docker, browser binaries, or explicitly mocked
model calls; use focused tests while developing and inspect the test setup
before running suites against a live installation.

For a read-only database summary, run `python health.py`. Its default database
path is relative to the repository, regardless of your shell's current directory.
Use `python health.py --database /path/to/think_tank.db` for another installation.
