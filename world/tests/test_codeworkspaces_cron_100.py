import json
import os
from pathlib import Path

import code_workspaces
import cron_scripts
import work_reuse


def test_code_workspaces_helpers(tmp_path):
    assert code_workspaces.has_web_assets(tmp_path / 'missing') is None
    (tmp_path / 'a.txt').write_text('x')
    assert code_workspaces.has_web_assets(tmp_path) is False
    (tmp_path / 'a.js').write_text('x')
    assert code_workspaces.has_web_assets(tmp_path) is True
    assert code_workspaces.fork_checkpoint(tmp_path, 'a', 'b', 'bad') is None
    try:
        code_workspaces.fork_checkpoint(tmp_path, '../a', 'b', 'stage-' + 'a' * 32)
    except ValueError:
        pass
    try:
        code_workspaces.ensure_test_directory(tmp_path, '../x')
    except ValueError:
        pass
    code_workspaces.ensure_test_directory(tmp_path, 'sb')
    assert (tmp_path / 'sb' / 'tests').is_dir()


def test_sandbox_for_variants():
    assert code_workspaces.sandbox_for({'tasks': {}}, {'id': 't', 'codeWorkspaceVersion': 2}).startswith('story-')
    assert code_workspaces.sandbox_for({'tasks': {}}, {'id': 't', 'sandboxId': 'ex'}) == 'ex'
    assert code_workspaces.sandbox_for({'tasks': {'t': {}}}, {'id': 't'}) == 'workroom-shared'
    out = code_workspaces.sandbox_for({'tasks': {}}, {'id': 't', 'workOrderId': 'My Project'})
    assert out.startswith('project-my-project-')


def test_prepare_and_fork(tmp_path):
    root = tmp_path
    (root / 'seed').mkdir(); (root / 'seed' / 'a.py').write_text('x')
    state = {'tasks': {'t': {'id': 't', 'codeWorkspaceVersion': 2,
                             'sandboxId': 'seed'}}, 'backlogRequests': []}
    sb = code_workspaces.prepare(state, state['tasks']['t'], root)
    assert (root / sb / 'tests').is_dir()
    assert (root / sb / 'mypy.ini').exists()
    # valid checkpoint
    ws = root / 'old'; ws.mkdir(); (ws / 'a').write_text('x')
    digest = work_reuse.tree_digest(str(ws))
    cid = 'stage-' + 'b' * 32
    (root / cid).mkdir(); (root / cid / 'a').write_text('x')
    (root / (cid + '.repair.json')).write_text(json.dumps({'workspace': 'old', 'baseDigest': digest}))
    assert code_workspaces.fork_checkpoint(root, 'old', 'old', cid)
    # symlink in checkpoint
    bad = 'stage-' + 'c' * 32
    (root / bad).mkdir(); (root / bad / 'l').symlink_to(ws / 'a')
    (root / (bad + '.repair.json')).write_text(json.dumps({'workspace': 'old', 'baseDigest': digest}))
    try:
        code_workspaces.fork_checkpoint(root, 'old', 'old', bad)
    except ValueError:
        pass


def _repo(tmp_path, monkeypatch, source='print(1)\n', entry=None):
    root = tmp_path / 'repo'; (root / 'scripts').mkdir(parents=True)
    path = root / 'scripts' / 's.py'; path.write_text(source)
    import hashlib
    item = {'path': 'scripts/s.py', 'runner': 'host', 'sha256': hashlib.sha256(source.encode()).hexdigest(),
            'allowedHosts': ['example.com'], 'env': ['FOO']}
    item.update(entry or {})
    (root / 'scripts' / 'cron_allowlist.json').write_text(json.dumps({'scripts': [item]}))
    monkeypatch.setattr(cron_scripts, '_repo_root', lambda: str(root))
    return root, path


def test_cron_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_scripts, '_repo_root', lambda: str(tmp_path / 'missing'))
    assert cron_scripts._allowed_scripts() is None
    root, path = _repo(tmp_path, monkeypatch)
    assert cron_scripts.resolve_script('scripts/s.py')[1] is None
    for rel in ('', '/x', '../x', 'x.txt', 'scripts/missing.py'):
        assert cron_scripts.resolve_script(rel)[1]
    monkeypatch.setattr(cron_scripts, '_allowed_scripts', lambda: None)
    assert cron_scripts.resolve_script('scripts/s.py')[1]
    monkeypatch.setattr(cron_scripts, '_allowed_scripts', lambda: {'scripts/s.py': {'runner': 'bad'}})
    assert cron_scripts.resolve_script('scripts/s.py')[1]
    monkeypatch.setattr(cron_scripts, '_allowed_scripts', lambda: {'scripts/s.py': {'runner': 'host'}})
    assert cron_scripts.resolve_script('scripts/s.py')[1]
    monkeypatch.setattr(cron_scripts, '_allowed_scripts', lambda: {'scripts/s.py': {'runner': 'host', 'sha256': 'bad'}})
    assert cron_scripts.resolve_script('scripts/s.py')[1]
    # url host tripwire
    monkeypatch.undo()
    root, path = _repo(tmp_path / 'two', monkeypatch, source='x="https://evil.com"\n')
    assert 'not on its allowlist' in (cron_scripts.resolve_script('scripts/s.py')[1] or '')
    assert cron_scripts._timeout({'scriptTimeoutSeconds': 'x'}) == 120
    assert cron_scripts._timeout({'scriptTimeoutSeconds': 99999}) == 600
    assert cron_scripts._parse_output('') == (False, '')
    assert cron_scripts._parse_output('x\n{"notify": true, "summary": "s"}') == (True, 's')
    env = cron_scripts._safe_env({'env': ['FOO']}, sandbox=True)
    assert env['HOME'] == '/tmp'
    assert 'PATH' not in env


def test_cron_run(tmp_path, monkeypatch):
    root, path = _repo(tmp_path, monkeypatch)
    monkeypatch.setenv('FOO', 'bar')
    # host success
    assert cron_scripts.run({'script': 'scripts/s.py', 'allowHostExecution': True})['ok'] is True
    # host without approval
    assert cron_scripts.run({'script': 'scripts/s.py'})['ok'] is False
    # sandbox runner
    import serve
    monkeypatch.setattr(cron_scripts, '_allowed_scripts', lambda: {'scripts/s.py': {'runner': 'sandbox', 'sha256': __import__('hashlib').sha256(path.read_bytes()).hexdigest()}})
    monkeypatch.setattr(serve, 'run_cron_script_sandboxed', lambda *a: {'exitCode': 0, 'stdout': '{"notify": true}', 'stderr': ''})
    assert cron_scripts.run({'script': 'scripts/s.py'})['notify'] is True
    monkeypatch.setattr(serve, 'run_cron_script_sandboxed', lambda *a: {'timedOut': True})
    assert cron_scripts.run({'script': 'scripts/s.py'})['error'] == 'script timed out'
    # host timeout and OSError
    monkeypatch.setattr(cron_scripts.subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(cron_scripts.subprocess.TimeoutExpired('x', 1)))
    assert cron_scripts.run({'script': 'scripts/s.py', 'allowHostExecution': True})['error'] == 'script timed out'
    monkeypatch.setattr(cron_scripts.subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    assert cron_scripts.run({'script': 'scripts/s.py', 'allowHostExecution': True})['ok'] is False


def test_code_workspaces_remaining(tmp_path, monkeypatch):
    # fork checkpoint missing manifest
    assert code_workspaces.fork_checkpoint(tmp_path, 'a', 'b', 'stage-' + 'd' * 32) is None
    ws = tmp_path / 'w'; ws.mkdir(); (ws / 'a').write_text('x')
    digest = work_reuse.tree_digest(str(ws))
    cid = 'stage-' + 'e' * 32; (tmp_path / cid).mkdir()
    (tmp_path / (cid + '.repair.json')).write_text(json.dumps({'workspace': 'other', 'baseDigest': digest}))
    assert code_workspaces.fork_checkpoint(tmp_path, 'w', 'w', cid) is None
    # prepare prerequisite source
    root = tmp_path / 'p'; root.mkdir()
    state = {'tasks': {'pre': {'id': 'pre', 'status': 'done', 'workOrderId': 'wo',
                              'workOrderStoryId': 's1', 'codeWorkspaceVersion': 2}},
             'backlogRequests': [{'id': 'wo', 'plan': {'stories': [{'id': 's2', 'prerequisiteStoryIds': ['s1']}]}}]}
    task = {'id': 's2', 'codeWorkspaceVersion': 2, 'workOrderId': 'wo', 'workOrderStoryId': 's2'}
    code_workspaces.prepare(state, task, root)
    # existing config files
    code_workspaces.prepare(state, task, root)


def test_cron_remaining(tmp_path, monkeypatch):
    root, path = _repo(tmp_path, monkeypatch)
    logged, queued = [], []
    monkeypatch.setattr(cron_scripts, 'run', lambda d: {'ok': True, 'notify': True, 'summary': 's', 'script': 'scripts/s.py', 'runner': 'host'})
    cron_scripts.run_standing({}, {'taskId': 't'}, lambda s, a, e, d: logged.append(e), lambda s, q: queued.extend(q))
    assert logged and queued
    monkeypatch.setattr(cron_scripts, 'run', lambda d: {'ok': False, 'notify': False})
    cron_scripts.run_standing({}, {}, lambda *a: None, lambda *a: None)
    assert cron_scripts.declaration_fields({}) == ({}, None)
    assert cron_scripts.declaration_fields({'script': 'bad'})[0] is None
    assert cron_scripts.declaration_fields({'script': 'scripts/s.py'})[0] is None
    fields, err = cron_scripts.declaration_fields({'script': 'scripts/s.py', 'allowHostExecution': True,
                                                   'scriptTimeoutSeconds': 3, 'notifyAgent': 'a'})
    assert fields['scriptRunner'] == 'host' and fields['notifyAgent'] == 'a'
    assert cron_scripts.declaration_fields({'script': 'scripts/s.py', 'allowHostExecution': True,
                                            'scriptTimeoutSeconds': 'x'})[0] is None
    # malformed allowlists
    import json as _json
    for data in ('x', {'scripts': 'x'}, {'scripts': [1]}, {'scripts': [{}]}):
        (root / 'scripts' / 'cron_allowlist.json').write_text(data if isinstance(data, str) else _json.dumps(data))
        assert cron_scripts._allowed_scripts() is None
    # path outside scripts
    monkeypatch.setattr(cron_scripts, '_allowed_scripts', lambda: {'scripts/x.py': {'runner': 'host', 'sha256': 'x'}})
    (root / 'scripts' / 'x.py').symlink_to(tmp_path / 'outside.py')
    (tmp_path / 'outside.py').write_text('x')
    assert 'outside' in (cron_scripts.resolve_script('scripts/x.py')[1] or '')
    # missing, too large, unicode
    monkeypatch.undo()
    root2, path2 = _repo(tmp_path / 'big', monkeypatch)
    (path2).write_text('x' * (cron_scripts.MAX_SCRIPT_BYTES + 1))
    assert 'too large' in (cron_scripts.resolve_script('scripts/s.py')[1] or '')
    root3, path3 = _repo(tmp_path / 'bin', monkeypatch)
    path3.write_bytes(b'\xff\xfe')
    import hashlib as _h
    (root3 / 'scripts' / 'cron_allowlist.json').write_text(__import__('json').dumps({'scripts': [{
        'path': 'scripts/s.py', 'runner': 'host',
        'sha256': _h.sha256(b'\xff\xfe').hexdigest(), 'allowedHosts': []}]}))
    assert cron_scripts.resolve_script('scripts/s.py')[1] is None


def test_code_workspaces_final(tmp_path):
    assert code_workspaces.sandbox_for({'tasks': {}}, {'id': 'x'}) == 'workroom-shared'
    root = tmp_path / 'r'; root.mkdir(); (root / 'seed').mkdir(); (root / 'seed' / 'l').symlink_to(root / 'seed' / 'x')
    state = {'tasks': {'t': {'id': 't', 'codeWorkspaceVersion': 2, 'sandboxId': 'seed'}}}
    try:
        code_workspaces.prepare(state, state['tasks']['t'], root)
    except ValueError:
        pass


def test_cron_final(tmp_path, monkeypatch):
    assert cron_scripts._repo_root()
    monkeypatch.setattr(cron_scripts, '_repo_root', lambda: str(tmp_path))
    assert cron_scripts._repo_root() == str(tmp_path)
    root, path = _repo(tmp_path / 'r', monkeypatch)
    # missing file with valid allowlist entry
    monkeypatch.setattr(cron_scripts, '_allowed_scripts', lambda: {'scripts/missing.py': {'runner': 'host', 'sha256': 'x'}})
    assert 'not found' in (cron_scripts.resolve_script('scripts/missing.py')[1] or '')
    # read error
    monkeypatch.setattr(cron_scripts, '_allowed_scripts', lambda: {'scripts/s.py': {'runner': 'host', 'sha256': 'x'}})
    monkeypatch.setattr(cron_scripts, 'open', lambda *a, **k: (_ for _ in ()).throw(OSError('x')), raising=False)
    assert 'could not read' in (cron_scripts.resolve_script('scripts/s.py')[1] or '')
    monkeypatch.undo()
    # invalid URL
    root2, path2 = _repo(tmp_path / 'u', monkeypatch, source='x="http://[bad"\n')
    assert cron_scripts.resolve_script('scripts/s.py')[1] is None
    assert cron_scripts._parse_output('123') == (False, '123')
    assert cron_scripts._parse_output('not json') == (False, 'not json')
    assert cron_scripts.run({})['ok'] is False
    # host timeout and os error
    root3, path3 = _repo(tmp_path / 'h', monkeypatch)
    monkeypatch.setattr(cron_scripts.subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(cron_scripts.subprocess.TimeoutExpired('x', 1)))
    assert cron_scripts.run({'script': 'scripts/s.py', 'allowHostExecution': True})['error'] == 'script timed out'
    monkeypatch.setattr(cron_scripts.subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    assert cron_scripts.run({'script': 'scripts/s.py', 'allowHostExecution': True})['ok'] is False
