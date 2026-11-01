"""Tripwire: a test process must NEVER resolve the production DSN.

On 2026-11-01 the repo .env gained THINK_TANK_DB_URL (production Postgres).
db_backend._dsn() falls back to the repo .env when the env var is unset, so
every test process started opening PRODUCTION Postgres instead of its own
isolated SQLite copy -- fixture saves clobbered the live row tables and the
whole-blob state. The guard lives in db_backend._test_sandboxed() (pytest
module present, or THINK_TANK_FORCE_SQLITE set) and tests/conftest.py sets
the env flag. If either regresses, this file fails before any fixture world
can reach production."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import db_backend  # noqa: E402


def test_test_process_never_resolves_production_dsn():
    assert db_backend._test_sandboxed() is True
    assert db_backend._dsn() in (None, ''), (
        'a test process resolved a production DSN; fixture writes would '
        f'hit {_dsn_or_env()}')
    assert db_backend.is_postgres() is False


def _dsn_or_env():
    dsn = os.environ.get('THINK_TANK_DB_URL')
    return dsn or 'the repo .env THINK_TANK_DB_URL'


def test_force_sqlite_flag_is_respected_without_pytest_module():
    """The env flag alone must force SQLite even where 'pytest' is not in
    sys.modules (embedded runners)."""
    import importlib
    env_flag = 'THINK_TANK_FORCE_SQLITE'
    old = os.environ.get(env_flag)
    try:
        os.environ[env_flag] = '1'
        fake = sys.modules.pop('pytest', None)
        try:
            assert db_backend._test_sandboxed() is True
            assert db_backend.is_postgres() is False
        finally:
            if fake is not None:
                sys.modules['pytest'] = fake
    finally:
        if old is None:
            os.environ.pop(env_flag, None)
        else:
            os.environ[env_flag] = old
