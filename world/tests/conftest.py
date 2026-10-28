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
os.environ['THINK_TANK_DB'] = _TEST_DB
if os.path.exists(_LIVE_DB):
    _src = sqlite3.connect(_LIVE_DB)
    _dst = sqlite3.connect(_TEST_DB)
    try:
        _src.backup(_dst)
    finally:
        _dst.close()
        _src.close()
else:
    # No live DB (fresh checkout / CI): build the full schema so unpatched
    # tests hit a valid empty-state database instead of "no such table".
    # (The old behavior left a zero-byte file and relied on every test
    # module initializing its own DB -- only true where a live DB existed
    # to silently back them.)
    open(_TEST_DB, 'ab').close()
    import serve  # THINK_TANK_DB is set above; serve reads it at import
    serve.init_db()


@pytest.fixture(autouse=True)
def _ensure_test_db_schema():
    """Tests that point serve.DB_PATH at a not-yet-existing file get the full
    schema built for them. Without this, a suite run on a machine (or CI
    runner) with no live think_tank.db fails every such test with
    "no such table" -- locally the live-DB copy masked the gap."""
    import serve
    try:
        db_path = str(serve.DB_PATH)
    except Exception:
        db_path = None
    if db_path and not os.path.exists(db_path):
        parent = os.path.dirname(db_path)
        if parent and not os.path.isdir(parent):
            return  # a missing parent dir is the test's own scenario
        try:
            serve.init_db()
        except Exception:
            pass
    yield


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


# The DB copy above only covers sqlite. The service also writes the Library,
# escalations.json, agent profile trees, sandboxes, and several ledgers. Tests
# that exercise those paths without patching them must not touch the operator's
# real files, so point every writable root at a session-local copy.
_LIVE_ROOT = _REPO
_TEST_ROOT = os.path.join(_tmpdir, 'root')


def _seed_test_root(root):
    os.makedirs(root, exist_ok=True)
    # Keep read fixtures (reports, skills, project files) but never their live
    # absolute paths: the copy is writable and disposable.
    live_library = os.path.join(_LIVE_ROOT, 'library')
    if os.path.isdir(live_library):
        shutil.copytree(live_library, os.path.join(root, 'library'),
                        dirs_exist_ok=True)
    # Agent profiles are mostly small JSON/Markdown; skip the large avatar and
    # prototype media trees. Tests that need those patch their own temp root.
    live_agents = os.path.join(_LIVE_ROOT, 'agents')
    if os.path.isdir(live_agents):
        shutil.copytree(
            live_agents, os.path.join(root, 'agents'), dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                'avatars', 'prototypes', '*.png', '*.jpg', '*.jpeg', '*.webp',
                '*.gif', '*.mp4', '*.mov', '*.mp3', '*.wav'))
    # The live-read supervisor test intentionally reads source from the
    # project root. Copy the source tree (not tests or git metadata) so that
    # read still works without exposing the live tree to writes.
    live_world = os.path.join(_LIVE_ROOT, 'world')
    if os.path.isdir(live_world):
        shutil.copytree(
            live_world, os.path.join(root, 'world'), dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                '.git', 'tests', '__pycache__', '*.pyc',
                '.selfedit_staging', '.selfedit_failed'))
    for name in ('escalations.json', 'webhooks.jsonl', 'failures.json',
                 'rule_proposals.json', 'successes.json',
                 'success_proposals.json', 'browse_trail.json',
                 'library_usage.json', 'object_stores.json'):
        src = os.path.join(_LIVE_ROOT, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(root, name))
    for name in ('sandboxes', 'think-tank-db-backups', '.http_cache',
                 '.secret_keys'):
        os.makedirs(os.path.join(root, name), exist_ok=True)


@pytest.fixture(scope='session', autouse=True)
def _isolate_live_tree():
    """Point every writable service path at the session temp root."""
    _seed_test_root(_TEST_ROOT)
    import serve
    import core.object_store as object_store

    paths = {
        'THINK_TANK_DIR': _TEST_ROOT,
        'SANDBOXES_DIR': os.path.join(_TEST_ROOT, 'sandboxes'),
        'ESCALATIONS_PATH': os.path.join(_TEST_ROOT, 'escalations.json'),
        'WEBHOOKS_PATH': os.path.join(_TEST_ROOT, 'webhooks.jsonl'),
        'FAILURES_PATH': os.path.join(_TEST_ROOT, 'failures.json'),
        'RULE_PROPOSALS_PATH': os.path.join(_TEST_ROOT, 'rule_proposals.json'),
        'SUCCESSES_PATH': os.path.join(_TEST_ROOT, 'successes.json'),
        'SUCCESS_PROPOSALS_PATH': os.path.join(_TEST_ROOT, 'success_proposals.json'),
        'LIBRARY_DIR': os.path.join(_TEST_ROOT, 'library'),
        'LIBRARY_ARCHIVE_DIR': os.path.join(_TEST_ROOT, 'library', 'archive'),
        'PASSPORT_PATH': os.path.join(_TEST_ROOT, 'library', '.passport.json'),
        'AGENTS_DIR': os.path.join(_TEST_ROOT, 'agents'),
        'DB_BACKUP_DIR': os.path.join(_TEST_ROOT, 'think-tank-db-backups'),
        'BROWSE_TRAIL_PATH': os.path.join(_TEST_ROOT, 'browse_trail.json'),
        'LIBRARY_USAGE_PATH': os.path.join(_TEST_ROOT, 'library_usage.json'),
        'HTTP_CACHE_DIR': os.path.join(_TEST_ROOT, '.http_cache'),
        '_FERNET_EDEK_DIR': os.path.join(_TEST_ROOT, '.secret_keys'),
    }
    saved = {name: getattr(serve, name, None) for name in paths}
    for name, value in paths.items():
        setattr(serve, name, value)
    old_agents = object_store.AGENTS_DIR
    old_registry = object_store.REGISTRY_PATH
    object_store.AGENTS_DIR = os.path.join(_TEST_ROOT, 'agents')
    object_store.REGISTRY_PATH = os.path.join(_TEST_ROOT, 'object_stores.json')
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(serve, name, value)
        object_store.AGENTS_DIR = old_agents
        object_store.REGISTRY_PATH = old_registry
