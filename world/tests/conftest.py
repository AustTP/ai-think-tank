"""Pytest-wide isolation of the live think_tank.db.

Several tests exercise sim/content/serve paths that call serve.log_action()
(or other _db() writers) without patching serve.DB_PATH themselves. Before
this conftest existed, those writes went straight into the LIVE
think_tank.db (action_log pollution: fixture titles like "Fix the weather
report", "news.com/a" digests, week-0 spikes).

This file runs before any test module is imported. It copies the real DB
into a per-session temp file via the sqlite backup API (safe under WAL,
works while the live server is running) and points THINK_TANK_DB at the
copy. serve.py reads THINK_TANK_DB at import time for DB_PATH, so every
unpatched test writes to the copy; tests that already patch serve.DB_PATH
keep their own isolation and are unaffected.
"""
import atexit
import os
import shutil
import sqlite3
import sys
import tempfile
import time

import pytest

_WORLD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORLD not in sys.path:
    sys.path.insert(0, _WORLD)

_REPO = os.path.dirname(_WORLD)
_LIVE_DB = os.path.join(_REPO, 'think_tank.db')

_TMP_PREFIX = 'tt-test-db-'
# Legacy copies carry no PID and a crashed run can leave one behind. Only
# reclaim those once they are old enough that no live session can own them.
_LEGACY_STALE_AFTER_S = 3600


def _pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sweep_stale_test_dbs(base):
    """Delete DB copies left by crashed or killed pytest runs.

    Every session copies the live DB (100+ MB) into its own temp directory.
    Without this sweep a SIGKILL, a crashed worker, or a timeout leaves that
    copy behind forever. Current sessions name their directory with their PID
    so a copy is only reclaimed once its owner is gone.
    """
    try:
        entries = os.listdir(base)
    except OSError:
        return
    for name in entries:
        if not name.startswith(_TMP_PREFIX):
            continue
        path = os.path.join(base, name)
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        head = name[len(_TMP_PREFIX):].split('-', 1)[0]
        pid = int(head) if head.isdigit() else 0
        if pid:
            if _pid_alive(pid):
                continue
        else:
            try:
                limit = _LEGACY_STALE_AFTER_S
                if time.time() - os.path.getmtime(path) < limit:
                    continue
            except OSError:
                continue
        shutil.rmtree(path, ignore_errors=True)


def _remove_tmpdir(path):
    shutil.rmtree(path, ignore_errors=True)


_sweep_stale_test_dbs(tempfile.gettempdir())
_tmpdir = tempfile.mkdtemp(prefix=f'{_TMP_PREFIX}{os.getpid()}-')
atexit.register(_remove_tmpdir, _tmpdir)
_TEST_DB = os.path.join(_tmpdir, 'think_tank.db')
if os.path.exists(_LIVE_DB):
    _src = sqlite3.connect(_LIVE_DB)
    _dst = sqlite3.connect(_TEST_DB)
    try:
        _src.backup(_dst)
    finally:
        _dst.close()
        _src.close()
else:
    # No live DB (fresh checkout / CI): an empty file; tests that need
    # tables call init_db() or patch DB_PATH to their own temp file.
    open(_TEST_DB, 'ab').close()

os.environ['THINK_TANK_DB'] = _TEST_DB


@pytest.fixture(autouse=True)
def _reset_credit_pause():
    """Keep one test's simulated OpenRouter credit exhaustion from leaking.

    The pause is process-global, while the live account balance can dip below
    the watchdog floor during a test run. Without this reset, every later chat
    test sees a 503 caused by a background probe, not by the code under test.
    """
    def reset():
        try:
            import serve
        except Exception:
            return
        serve._CREDITS_PAUSE.update(active=False, since=0.0)
        serve._CREDITS_WATCHDOG_STATE.update(low=False)

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def _clear_unpriced_receipts():
    """Keep a mocked no-cost task call from blocking an unrelated later test."""
    yield
    try:
        import serve
        with serve._db() as conn:
            conn.execute("DELETE FROM usage_receipts WHERE status = 'unpriced'")
    except Exception:
        pass


def pytest_sessionfinish(session, exitstatus):
    """Remove this run's DB copy the moment pytest finishes."""
    _remove_tmpdir(_tmpdir)
