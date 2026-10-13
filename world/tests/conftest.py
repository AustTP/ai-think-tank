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
import os
import sqlite3
import sys
import tempfile

_WORLD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORLD not in sys.path:
    sys.path.insert(0, _WORLD)

_REPO = os.path.dirname(_WORLD)
_LIVE_DB = os.path.join(_REPO, 'think_tank.db')

_tmpdir = tempfile.mkdtemp(prefix='tt-test-db-')
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
