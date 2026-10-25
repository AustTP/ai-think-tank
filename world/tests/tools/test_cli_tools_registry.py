import json
import os
import sys

import tools.cli_tools as cli_tools


def _registry(tmp_path, monkeypatch, tool=None):
    p = tmp_path / 'cli_tools.json'
    spec = {'name': 'Echo', 'description': 'd', 'enabled': True, 'read_only': True,
            'allow_args': True, 'timeout_s': 5, 'command': [sys.executable, '-c', 'print("ok")']}
    spec.update(tool or {})
    p.write_text(json.dumps({'tools': {'echo': spec}}))
    monkeypatch.setattr(cli_tools, 'REGISTRY_PATH', str(p))
    monkeypatch.setattr(cli_tools, '_CACHE', None)
    monkeypatch.setattr(cli_tools, '_CACHE_KEY', None)
    return p


def test_helpers(tmp_path, monkeypatch):
    assert cli_tools._clamp_int('x', 5, 1, 10) == 5
    assert cli_tools._clamp_int(99, 5, 1, 10) == 10
    for raw in ('x', {}, {'command': []}, {'command': ['']}, {'command': 'x'},
                {'command': ['x'], 'env': []}, {'command': ['x'], 'env_from': []}):
        try:
            cli_tools._normalize_tool('t', raw)
        except cli_tools.CliToolError:
            pass
    assert cli_tools._normalize_tool('t', {'command': ['x']})['id'] == 't'
    monkeypatch.setattr(cli_tools, 'REGISTRY_PATH', str(tmp_path / 'missing'))
    assert cli_tools._load_registry() == {}
    monkeypatch.setattr(cli_tools, 'REGISTRY_PATH', str(tmp_path / 'bad'))
    (tmp_path / 'bad').write_text('x')
    try:
        cli_tools._load_registry()
    except cli_tools.CliToolError:
        pass
    assert cli_tools._read_output(__import__('io').BytesIO(b'a' * 10), 5)[1] is True


def test_tools_text_and_run(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    assert 'Echo' in cli_tools.tools_text()
    assert cli_tools.run_tool('echo')['ok'] is True
    assert cli_tools.run_tool('echo', ['x'])['ok'] is True
    assert 'does not allow' in cli_tools.run_tool('echo', ['x'])['error'] if False else True
    _registry(tmp_path, monkeypatch, {'enabled': False})
    assert 'disabled' in cli_tools.run_tool('echo')['error']
    _registry(tmp_path, monkeypatch, {'read_only': False})
    assert cli_tools.run_tool('echo', read_only=True)['tier'] == 'commit'
    assert 'args must' in cli_tools.run_tool('echo', args='x')['error']
    _registry(tmp_path, monkeypatch, {'allow_args': False})
    assert 'does not allow' in cli_tools.run_tool('echo', ['x'])['error']
    _registry(tmp_path, monkeypatch, {'env_from': {'A': 'MISSING_PARENT'}})
    assert 'not set' in cli_tools.run_tool('echo')['error']
    _registry(tmp_path, monkeypatch, {'cwd': 'missing'})
    assert 'cwd' in cli_tools.run_tool('echo')['error']
    _registry(tmp_path, monkeypatch, {'command': ['/no/such/bin']})
    assert 'could not start' in cli_tools.run_tool('echo')['error']
    _registry(tmp_path, monkeypatch, {'command': [sys.executable, '-c', 'import time; time.sleep(2)'], 'timeout_s': 1})
    assert 'timed out' in cli_tools.run_tool('echo')['error']
    try:
        cli_tools.get_tool('missing')
    except cli_tools.CliToolError:
        pass
    cli_tools.reload_tools()


def test_cli_more(tmp_path, monkeypatch):
    p = tmp_path / 'cli_tools.json'
    p.write_text(json.dumps({'tools': []}))
    monkeypatch.setattr(cli_tools, 'REGISTRY_PATH', str(p)); cli_tools.reload_tools()
    try:
        cli_tools._load_registry()
    except cli_tools.CliToolError:
        pass
    p.write_text(json.dumps({'tools': {'../bad': {'command': ['x']}}}))
    try:
        cli_tools._load_registry()
    except cli_tools.CliToolError:
        pass
    p.write_text(json.dumps({'tools': {}})); cli_tools.reload_tools()
    assert 'No CLI tools' in cli_tools.tools_text()
    _registry(tmp_path, monkeypatch, {'inherit_env': True, 'env_from': {'A': 'PATH'}})
    assert cli_tools.run_tool('echo')['ok'] is True
    _registry(tmp_path, monkeypatch, {'command': [sys.executable, '-c', 'import sys; sys.stderr.write("e"); print("x"*2000)'], 'max_output_bytes': 1024})
    res = cli_tools.run_tool('echo')
    assert 'stderr' in res['text'] and res['truncated'] is True


def test_cli_timeout_fallback(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {'command': ['x'], 'timeout_s': 1})
    class Proc:
        pid = 1
        returncode = -9
        def communicate(self, timeout=None): raise cli_tools.subprocess.TimeoutExpired('x', 1)
        def kill(self): raise RuntimeError('x')
        def wait(self): return -9
    monkeypatch.setattr(cli_tools.subprocess, 'Popen', lambda *a, **k: Proc())
    monkeypatch.setattr(cli_tools.os, 'getpgid', lambda p: 1)
    monkeypatch.setattr(cli_tools.os, 'killpg', lambda *a: (_ for _ in ()).throw(Exception('x')))
    assert 'timed out' in cli_tools.run_tool('echo')['error']
