import hashlib
import types

import selfedit


def test_kernel_helpers(monkeypatch, tmp_path):
    class P:
        stdout = b'/usr/bin/python3 serve.py --host 0.0.0.0 --port=9000\n'
    monkeypatch.setattr(selfedit.subprocess, 'run', lambda *a, **k: P())
    argv = selfedit._kernel_argv()
    assert argv
    assert selfedit._kernel_health_url(argv) == 'http://127.0.0.1:9000/api/rooms'
    assert selfedit._kernel_health_url(['x', '--host', '::', '--port', '1']) == 'http://127.0.0.1:1/api/rooms'
    P.stdout = b''
    assert selfedit._kernel_argv() is None
    monkeypatch.setattr(selfedit.subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    assert selfedit._kernel_argv() is None
    # spawn
    monkeypatch.setattr(selfedit, '_world', lambda: str(tmp_path))
    monkeypatch.setattr(selfedit.subprocess, 'Popen', lambda *a, **k: None)
    assert selfedit._spawn_kernel_watchdog(['x'], 'b', 't', 'h') is True
    monkeypatch.setattr(selfedit.subprocess, 'Popen', lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    assert selfedit._spawn_kernel_watchdog(['x'], 'b', 't', 'h') is False
    # preflight
    class R:
        returncode = 0; stdout = b'ok'
    monkeypatch.setattr(selfedit.subprocess, 'run', lambda *a, **k: R())
    assert selfedit._run_kernel_preflight()[0] is True
    R.returncode = 1
    assert selfedit._run_kernel_preflight()[0] is False
    monkeypatch.setattr(selfedit.subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(selfedit.subprocess.TimeoutExpired('x', 1)))
    assert 'timed out' in selfedit._run_kernel_preflight()[1]
    monkeypatch.setattr(selfedit.subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    assert 'could not start' in selfedit._run_kernel_preflight()[1]


def test_apply_kernel(monkeypatch, tmp_path):
    world = tmp_path / 'world'; world.mkdir()
    target = world / 'serve.py'; target.write_text('old')
    (world / 'cand').write_text('new')
    updates = []
    monkeypatch.setattr(selfedit, '_world', lambda: str(world))
    monkeypatch.setattr(selfedit, '_target_from_display', lambda d: str(target))
    monkeypatch.setattr(selfedit, '_staging_candidate', lambda r: str(world / 'cand'))
    monkeypatch.setattr(selfedit, '_update', lambda *a, **k: updates.append((a, k)))
    monkeypatch.setattr(selfedit, '_s', lambda: types.SimpleNamespace(log_action=lambda *a, **k: None))
    monkeypatch.setattr(selfedit, '_notify_agent', lambda *a, **k: None)
    monkeypatch.setattr(selfedit, '_row_agent', lambda r: 'a')
    monkeypatch.setattr(selfedit, 'KERNEL_RESTART_ENABLED', False)
    # missing target
    assert selfedit._apply_kernel('r', 'x', '') == 0
    target.write_text('old')
    sha = hashlib.sha256(b'old').hexdigest()
    monkeypatch.setattr(selfedit, '_run_kernel_preflight', lambda: (False, 'bad'))
    assert selfedit._apply_kernel('r', 'x', sha) == 0
    assert target.read_text() == 'old'
    monkeypatch.setattr(selfedit, '_run_kernel_preflight', lambda: (True, 'ok'))
    monkeypatch.setattr(selfedit, '_kernel_argv', lambda: None)
    assert selfedit._apply_kernel('r', 'x', sha) == 0
    monkeypatch.setattr(selfedit, '_kernel_argv', lambda: ['x'])
    assert selfedit._apply_kernel('r', 'x', sha) == 1
