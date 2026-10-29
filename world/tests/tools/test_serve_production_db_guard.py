"""The production-DB guard: a test process must never open the live DB.

Background: world/tests/conftest.py isolates every pytest run (per-session
copy of the live DB + redirected writable roots), but conftest only runs
under pytest. A test module executed directly -- `python tests/core/test_x.py`,
`python -m unittest ...` -- imports serve with the operator's real
environment, and every _db() writer then lands in the production
think_tank.db. That exact action_log pollution happened before conftest
existed. The guard in 00_header raises at import time for that shape, so
the bypass fails loudly instead of silently writing.

Hermetic: each case spawns a fresh interpreter against either a scrubbed
environment that the guard REFUSES (nothing is ever opened) or an explicit
scratch THINK_TANK_DB; this process itself only ever sees the conftest's
session copy.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WORLD = os.path.dirname(os.path.dirname(_HERE))
_PROBE = os.path.join(_HERE, '_serve_guard_probe.py')


def _spawn(argv0, env_overrides=None):
    """Run the probe with the given argv[0]. serve is made importable via
    PYTHONPATH (the probe's own directory would otherwise shadow it)."""
    env = {k: v for k, v in os.environ.items() if k != 'THINK_TANK_DB'}
    env['PYTHONPATH'] = _WORLD
    env.update(env_overrides or {})
    return subprocess.run(
        [sys.executable, argv0],
        capture_output=True, text=True, env=env, timeout=180,
    )


class GuardTrigger(unittest.TestCase):
    def test_direct_test_module_run_is_refused(self):
        """The shape that used to leak: a tests/ path as argv[0] with the
        production environment. serve must refuse to import rather than
        open the live DB."""
        result = _spawn(_PROBE)
        self.assertIn('GUARD-TRIGGERED', result.stdout)
        self.assertNotIn('IMPORT-OK', result.stdout)

    def test_pytest_basename_entrypoint_is_refused(self):
        """`python -m pytest` reports a pytest basename as argv[0]; a copy
        or shim named that way is still a test entrypoint. Emulated by
        giving the probe a pytest-prefixed basename in a scratch dir."""
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'pytestshim.py')
            shutil.copy2(_PROBE, scratch)
            result = _spawn(scratch)
        self.assertIn('GUARD-TRIGGERED', result.stdout)


class GuardAllowsOperatorShapes(unittest.TestCase):
    def test_non_tests_entrypoint_imports_cleanly(self):
        """The live server and operator scripts keep working: a non-tests
        argv[0] passes the guard. THINK_TANK_DB points at a scratch file so
        the spawned import is fully hermetic."""
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'operator_import_probe.py')
            shutil.copy2(_PROBE, scratch)
            result = _spawn(scratch, {'THINK_TANK_DB': os.path.join(td, 'scratch.db')})
        self.assertIn('IMPORT-OK', result.stdout)
        self.assertNotIn('GUARD-TRIGGERED', result.stdout)

    def test_redirected_think_tank_db_passes_from_a_tests_path(self):
        """What conftest does -- THINK_TANK_DB pointed away before serve is
        imported -- passes the guard even from a tests/ path."""
        with tempfile.TemporaryDirectory() as td:
            result = _spawn(_PROBE, {'THINK_TANK_DB': os.path.join(td, 'scratch.db')})
        self.assertIn('IMPORT-OK', result.stdout)


if __name__ == '__main__':
    unittest.main()
