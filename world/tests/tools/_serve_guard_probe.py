"""Subprocess probe for the production-DB guard (00_header._refuse_...).

NOT collected as a test module (name does not match test_*/ *_test.py):
its only job is to be the argv[0] of a spawned interpreter whose argv
path sits under a /tests/ directory, so the guard's entrypoint heuristic
fires exactly as it would for someone running a test module directly.
Prints GUARD-TRIGGERED when importing serve raised the guard's error,
IMPORT-OK otherwise; the real assertions live in the spawning test.
"""
import os
import sys

try:
    import serve  # noqa: F401
    print('IMPORT-OK')
except RuntimeError as exc:
    if 'refuses to open the production think_tank.db' in str(exc):
        print('GUARD-TRIGGERED')
    else:
        print(f'OTHER-RUNTIME-ERROR: {exc}')
except Exception as exc:  # noqa: BLE001 -- report anything else distinctly
    print(f'OTHER-ERROR: {type(exc).__name__}: {exc}')
finally:
    sys.stdout.flush()
    os._exit(0)
