"""Coverage-gap tests for apply/remediation, pre-apply, and reload paths."""
import importlib
import json
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import selfedit  # noqa: E402
import serve  # noqa: E402

from _selfedit_support import fake_repo  # noqa: E402


def _arm_row(edit_id='ed-1', path='world/gear.py', status='armed'):
    selfedit._record(edit_id, time.time(), 'worker', path, 'summary',
                     'before', 'after', status)


def test_cov_write_preapply_env_redacts_and_filters(fake_repo, monkeypatch):
    root, world = fake_repo
    (root / '.env').write_text(
        '# comment\nSECRET_KEY=abc\nPLAIN=1\nEMPTYLINE\n'
        'SELF_BASE_URL=http://live\nTELEGRAM_NOTIFICATIONS_ENABLED=true\n',
        encoding='utf-8')
    dest_dir = root / 'copy'
    dest_dir.mkdir()
    selfedit._write_preapply_env(str(root), str(dest_dir))
    text = (dest_dir / '.env').read_text(encoding='utf-8')
    assert 'SECRET_KEY=preapply' in text
    assert 'PLAIN=1' in text
    assert 'SELF_BASE_URL=http://127.0.0.1:1' in text
    assert 'TELEGRAM_NOTIFICATIONS_ENABLED=false' in text
    assert 'TELEGRAM_SUPPRESS_DURING_TESTS=true' in text
    assert text.count('SELF_BASE_URL=') == 1
    # A missing live env file and a chmod failure both stay non-fatal.
    selfedit._write_preapply_env(str(root / 'missing'), str(dest_dir))
    monkeypatch.setattr(selfedit.os, 'chmod',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    selfedit._write_preapply_env(str(root), str(dest_dir))
    # An unreadable live env file is skipped rather than failing the gate.
    import builtins
    real_open = builtins.open

    def unreadable(path, *a, **k):
        if str(path).endswith('.env'):
            raise OSError('unreadable env')
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, 'open', unreadable)
    selfedit._write_preapply_env(str(root), str(dest_dir))


def test_cov_init_preapply_db_success_and_failures(fake_repo, monkeypatch):
    root, world = fake_repo
    tmp = root / 'copy'
    (tmp / 'world').mkdir(parents=True)

    class Result:
        returncode = 0
        stdout = b''

    monkeypatch.setattr(selfedit.subprocess, 'run', lambda *a, **k: Result())
    path, err = selfedit._init_preapply_db(str(tmp))
    assert err is None and path.endswith('think_tank.db')

    Result.returncode = 1
    Result.stdout = b'init boom'
    path, err = selfedit._init_preapply_db(str(tmp))
    assert path is None and 'init boom' in err

    monkeypatch.setattr(selfedit.subprocess, 'run',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    path, err = selfedit._init_preapply_db(str(tmp))
    assert path is None and 'database init failed' in err


def _preapply_rows(world, edit_id='ed-pre'):
    cand = world / '.selfedit_staging' / f'{edit_id}.cand'
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text('X = 2\n', encoding='utf-8')
    return [{'id': edit_id, 'path': 'world/gear.py'}]


def test_cov_run_pre_apply_tests_success_missing_escape(fake_repo, monkeypatch):
    root, world = fake_repo
    monkeypatch.setattr(selfedit.shutil, 'copytree', lambda *a, **k: None)
    monkeypatch.setattr(selfedit, '_write_preapply_env', lambda *a, **k: None)
    monkeypatch.setattr(selfedit, '_init_preapply_db',
                        lambda tmp: (str(root / 'think_tank.db'), None))
    monkeypatch.setattr(selfedit, '_run_test_gate',
                        lambda cwd, label, env=None: (True, 'PRE GREEN'))
    ok, tail = selfedit._run_pre_apply_tests(_preapply_rows(world))
    assert ok is True and tail == 'PRE GREEN'

    ok, tail = selfedit._run_pre_apply_tests(
        [{'id': 'ed-missing', 'path': 'world/gear.py'}])
    assert ok is False and 'is missing' in tail

    rows = _preapply_rows(world)
    rows[0]['path'] = '../../escape.py'
    ok, tail = selfedit._run_pre_apply_tests(rows)
    assert ok is False and 'escapes the pre-apply copy' in tail

    monkeypatch.setattr(selfedit, '_init_preapply_db',
                        lambda tmp: (None, 'db boom'))
    ok, tail = selfedit._run_pre_apply_tests(_preapply_rows(world))
    assert ok is False and 'db boom' in tail

    monkeypatch.setattr(selfedit.shutil, 'copytree',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('copy')))
    ok, tail = selfedit._run_pre_apply_tests(_preapply_rows(world))
    assert ok is False and 'could not run' in tail


def test_cov_test_gate_log_dir_failure(fake_repo, monkeypatch):
    root, world = fake_repo
    class Result:
        returncode = 1
        stdout = b'GATE FAILED'
    monkeypatch.setattr(selfedit.subprocess, 'run', lambda *a, **k: Result())
    monkeypatch.setattr(selfedit.os, 'makedirs',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    ok, tail = selfedit._run_test_gate(str(root), 'post_apply')
    assert ok is False and 'GATE FAILED' in tail


def test_cov_apply_pass_kernel_branch(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'serve.py').write_text('X = 1\n', encoding='utf-8')
    _arm_row('ed-kernel', path='world/serve.py')
    monkeypatch.setattr(serve, '_dormant', lambda: True)
    seen = []
    monkeypatch.setattr(selfedit, '_apply_kernel',
                        lambda row_id, display, sha: seen.append(
                            (row_id, display)) or 1)
    assert selfedit.apply_pass() == 1
    assert seen and seen[0][0] == 'ed-kernel'


def test_cov_apply_kernel_restart_paths(fake_repo, monkeypatch):
    _, world = fake_repo
    target = world / 'serve.py'
    cand = world / '.selfedit_staging' / 'ed-k.cand'
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text('X = 2\n', encoding='utf-8')
    monkeypatch.setattr(selfedit, '_staging_candidate', lambda r: str(cand))
    monkeypatch.setattr(selfedit, '_update', lambda *a, **k: None)
    monkeypatch.setattr(selfedit, '_row_agent', lambda r: 'worker')
    monkeypatch.setattr(selfedit, '_notify_agent', lambda *a, **k: None)
    monkeypatch.setattr(
        selfedit, '_s',
        lambda: types.SimpleNamespace(ROOT=str(world),
                                      log_action=lambda *a, **k: None))
    monkeypatch.setattr(selfedit, '_run_kernel_preflight',
                        lambda: (True, 'ok'))
    monkeypatch.setattr(selfedit, '_kernel_argv', lambda: ['python', 'serve.py'])
    monkeypatch.setattr(selfedit, 'KERNEL_RESTART_ENABLED', True)

    # Missing target is a conflict, never a write.
    assert selfedit._apply_kernel('ed-k', 'serve.py', '') == 0

    target.write_text('X = 1\n', encoding='utf-8')
    sha = selfedit.hashlib.sha256(b'X = 1\n').hexdigest()
    monkeypatch.setattr(selfedit, '_spawn_kernel_watchdog',
                        lambda *a, **k: False)
    assert selfedit._apply_kernel('ed-k', 'serve.py', sha) == 0
    assert target.read_text(encoding='utf-8') == 'X = 1\n'

    monkeypatch.setattr(selfedit, '_spawn_kernel_watchdog',
                        lambda *a, **k: True)
    monkeypatch.setattr(selfedit.os, 'chdir', lambda p: None)
    monkeypatch.setattr(selfedit.os, 'execv',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('no')))
    assert selfedit._apply_kernel('ed-k', 'serve.py', sha) == 0
    assert target.read_text(encoding='utf-8') == 'X = 1\n'

    monkeypatch.setattr(selfedit.os, 'execv', lambda *a, **k: None)
    assert selfedit._apply_kernel('ed-k', 'serve.py', sha) == 1


def test_cov_kernel_health_url_equals_form():
    url = selfedit._kernel_health_url(['python', '--host=10.0.0.2',
                                       '--port=7777'])
    assert url == 'http://10.0.0.2:7777/api/rooms'


def test_cov_row_agent_and_restart_helper(fake_repo, monkeypatch):
    _, world = fake_repo
    _arm_row('ed-row', path='world/gear.py')
    assert selfedit._row_agent('ed-row') == 'worker'

    def boom():
        raise RuntimeError('no db')

    monkeypatch.setattr(selfedit, '_s', boom)
    assert selfedit._row_agent('ed-row') == 'unknown'

    class InlineThread:
        def __init__(self, target=None, daemon=None):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(selfedit.threading, 'Thread', InlineThread)
    monkeypatch.setattr(selfedit, '_kernel_argv', lambda: None)
    selfedit._schedule_kernel_restart(0)
    monkeypatch.setattr(selfedit, '_kernel_argv', lambda: ['python', 'serve.py'])
    monkeypatch.setattr(selfedit.os, 'chdir', lambda p: None)
    monkeypatch.setattr(selfedit.os, 'execv',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    selfedit._schedule_kernel_restart(0)
    monkeypatch.setattr(selfedit.os, 'execv', lambda *a, **k: None)
    selfedit._schedule_kernel_restart(0)


def test_cov_submit_fix_path_refusal(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')
    monkeypatch.setattr(selfedit, 'remediation_window', lambda: 'win-open')
    monkeypatch.setattr(selfedit, '_path_refusal',
                        lambda *a: 'blocked by gate')
    ok, payload = selfedit.submit_remediation_fix(
        'worker', 'gear.py', 'SPEED = 2', 'SPEED = 3', 'why')
    assert ok is False and payload['error'] == 'blocked by gate'


def test_cov_revert_one_kernel_schedules_restart(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'serve.py').write_text('X = 1\n', encoding='utf-8')
    _arm_row('ed-rk', path='world/serve.py', status='applied')
    backups = world / '.selfedit_backups'
    backups.mkdir(exist_ok=True)
    (backups / 'ed-rk').write_text('X = 0\n', encoding='utf-8')
    restarted = []
    monkeypatch.setattr(selfedit, '_schedule_kernel_restart',
                        lambda: restarted.append(True))
    assert selfedit._revert_one_from_backup('ed-rk') is True
    assert restarted == [True]


def test_cov_reload_env_exports_and_loader(fake_repo, monkeypatch):
    _, world = fake_repo
    monkeypatch.setattr(importlib, 'reload', lambda mod: mod)
    # env_reload has a dedicated refresh branch.
    selfedit._reload(str(world / 'env_reload.py'))
    assert hasattr(serve, '_env_flag')
    # A registered post-reload loader is called, and a failing loader is
    # swallowed rather than breaking the apply.
    stub = types.ModuleType('cov_load_mod')
    monkeypatch.setitem(sys.modules, 'cov_load_mod', stub)
    monkeypatch.setitem(selfedit.POST_RELOAD_LOADERS, 'cov_load_mod',
                        ('_cov_loader',))
    calls = []
    monkeypatch.setattr(serve, '_cov_loader', lambda: calls.append('ok'),
                        raising=False)
    selfedit._reload(str(world / 'cov_load_mod.py'))
    assert calls == ['ok']
    monkeypatch.setattr(serve, '_cov_loader',
                        lambda: (_ for _ in ()).throw(RuntimeError('x')),
                        raising=False)
    selfedit._reload(str(world / 'cov_load_mod.py'))


def test_cov_refresh_serve_imports_ast_paths(fake_repo, monkeypatch, tmp_path):
    source = tmp_path / 'serve_src.py'
    source.write_text(
        'from covmod_unique import alpha as cov_beta_xyz, gamma, absent\n',
        encoding='utf-8')
    stub = types.ModuleType('covmod_unique')
    stub.alpha = 'A'
    stub.gamma = 'G'
    monkeypatch.setitem(sys.modules, 'covmod_unique', stub)
    monkeypatch.setattr(serve, '__file__', str(source))
    try:
        rebound = selfedit._refresh_serve_imports('covmod_unique')
        assert 'cov_beta_xyz' in rebound and 'gamma' in rebound
        assert getattr(serve, 'cov_beta_xyz') == 'A'
        assert getattr(serve, 'gamma') == 'G'
        assert not hasattr(serve, 'absent')
        # A second call takes the cached name list.
        assert selfedit._refresh_serve_imports('covmod_unique') == [
            'cov_beta_xyz', 'gamma']
    finally:
        for name in ('cov_beta_xyz', 'gamma'):
            if hasattr(serve, name):
                delattr(serve, name)

    bad_mod = 'covmod_badparse'
    monkeypatch.setitem(sys.modules, bad_mod,
                        type('StubModule', (), {'__name__': bad_mod})())
    monkeypatch.setattr(selfedit, '_read_source',
                        lambda path: (_ for _ in ()).throw(OSError('gone')))
    assert selfedit._refresh_serve_imports(bad_mod) == []
    assert selfedit._refresh_serve_imports('not_loaded_anywhere') == []


def test_cov_refresh_env_reload_exports(fake_repo):
    selfedit._refresh_serve_env_reload_exports()
    import env_reload
    assert serve._ENV_RELOAD_FIELDS == env_reload.ENV_RELOAD_FIELDS
    assert serve._env_flag == env_reload._env_flag
