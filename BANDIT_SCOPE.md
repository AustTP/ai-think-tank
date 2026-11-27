# Bandit scope decision: B101 (assert_used) in tests

Decided by the operator, 2026-11-27. This is the durable scope decision the
"[STORY] Decide bandit B101 scope on tests and clear the test-blocked chain"
card was asking for (task-2443 chain, stuck since 2026-10-31 because the
execution service refuses sandbox writes of dotfiles like `.bandit`).

## The decision

- **Test files may use bare `assert` (B101 accepted).** pytest verification
  requires bare assert; the tank's quality gate already scopes bandit to the
  mypy file list, which excludes test files
  (`world/agents/content_parts/20_body_03.py` around line 239:
  "bandit mirrors mypy's exclusion: test files legitimately `assert`").
- **Production files must not use `assert` for validation, control flow, or
  enforcement.** Asserts vanish under `python -O`, so anything an assert
  guards is unguarded in an optimized run. Production code raises real
  errors instead. All former production B101 sites were converted to real
  raises in the same commit as this document.
- **Do not implement this decision by writing a `.bandit` config that skips
  B101.** A repo-wide skip would also silence genuine production findings.
  The scope is enforced by the gate's file selection (tests excluded), not by
  bandit config. This is why the agents' repeated `cat > .bandit` attempts
  were the wrong lever and why the decision is recorded here instead.

## How to verify

Production scope must stay clean (no B101 outside test paths):

```sh
./.venv/bin/python -m bandit -r world -q -f json | \
  ./.venv/bin/python -c "
import json, sys
d = json.load(sys.stdin)
bad = [r for r in d['results'] if r['test_id'] == 'B101'
       and '/tests/' not in r['filename']]
print(len(bad))  # expect 0
"
```

If a future change needs an `assert` in production code (e.g. narrowing a
type for mypy), it must be a real `if ... raise` instead, or carry a
`# nosec B101` with a written justification in the same line.
