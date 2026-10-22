"""CRON script lane for standing tasks."""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cron_scripts  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _permissive_allowlist(monkeypatch):
    # These tests exercise cron behavior with temp scripts; the allowlist,
    # hash, URL, and host-approval rules are covered in test_cron_allowlist.py.
    def _entries():
        root = cron_scripts._repo_root()
        out = {}
        for rel in ('scripts/ok.py', 'scripts/watch.py',
                    'scripts/bad.py', 'scripts/slow.py'):
            path = os.path.join(root, rel)
            try:
                with open(path, 'rb') as f:
                    data = f.read()
            except OSError:
                continue
            out[rel] = {'path': rel,
                        'sha256': hashlib.sha256(data).hexdigest(),
                        'runner': 'sandbox', 'allowedHosts': []}
        return out

    def _local_sandbox(relpath, timeout_s, env):
        path = os.path.join(cron_scripts._repo_root(), relpath)
        try:
            proc = cron_scripts.subprocess.run(
                [cron_scripts.sys.executable, path],
                cwd=cron_scripts._repo_root(), env=env,
                capture_output=True, text=True, timeout=timeout_s)
        except cron_scripts.subprocess.TimeoutExpired:
            return {'exitCode': None, 'stdout': '', 'stderr': '',
                    'timedOut': True}
        return {'exitCode': proc.returncode, 'stdout': proc.stdout,
                'stderr': proc.stderr, 'timedOut': False}

    monkeypatch.setattr(cron_scripts, '_allowed_scripts', _entries)
    monkeypatch.setattr(cron_scripts, '_run_sandboxed', _local_sandbox)


def test_resolve_rejects_escapes_and_non_scripts(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_scripts, '_repo_root', lambda: str(tmp_path))
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts' / 'ok.py').write_text('print(1)\n', encoding='utf-8')
    assert cron_scripts.resolve_script('scripts/ok.py')[1] is None
    for bad in ('../ok.py', '/tmp/ok.py', 'scripts/ok.sh', 'scripts/nope.py'):
        assert cron_scripts.resolve_script(bad)[1]


def test_run_parses_notify_line(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_scripts, '_repo_root', lambda: str(tmp_path))
    (tmp_path / 'scripts').mkdir()
    script = tmp_path / 'scripts' / 'watch.py'
    script.write_text(
        'import json\nprint(json.dumps({"notify": True, "summary": "new rule"}))\n',
        encoding='utf-8')
    out = cron_scripts.run({'script': 'scripts/watch.py'})
    assert out['ok'] is True
    assert out['notify'] is True
    assert out['summary'] == 'new rule'


def test_run_standing_queues_only_on_notify():
    logs, queued = [], []

    def log(state, actor, action, details):
        logs.append((actor, action, details))

    def queue(state, items):
        queued.extend(items)

    original = cron_scripts.run
    try:
        cron_scripts.run = lambda decl: {'ok': True, 'notify': False,
                                         'script': 'scripts/x.py'}
        cron_scripts.run_standing({}, {'taskId': 't1', 'name': 'quiet'},
                                  log, queue)
        assert queued == []
        assert logs[0][1] == 'standing_task_script'
        cron_scripts.run = lambda decl: {'ok': True, 'notify': True,
                                         'summary': 'check this',
                                         'script': 'scripts/x.py'}
        cron_scripts.run_standing({}, {'taskId': 't2', 'name': 'loud'},
                                  log, queue)
        assert len(queued) == 1
        assert 'check this' in queued[0]['instructions']
    finally:
        cron_scripts.run = original


def test_run_missing_script_is_an_error():
    out = cron_scripts.run({'script': '../nope.py'})
    assert out['ok'] is False
    assert out['notify'] is False
    assert 'scripts/' in out['error']


def test_run_nonzero_exit_reports_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_scripts, '_repo_root', lambda: str(tmp_path))
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts' / 'bad.py').write_text(
        'import sys\nprint("boom", file=sys.stderr)\nsys.exit(3)\n',
        encoding='utf-8')
    out = cron_scripts.run({'script': 'scripts/bad.py'})
    assert out['ok'] is False
    assert out['returncode'] == 3
    assert 'boom' in out['error']


def test_run_timeout_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_scripts, '_repo_root', lambda: str(tmp_path))
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts' / 'slow.py').write_text('print(1)\n', encoding='utf-8')

    def boom(*args, **kwargs):
        raise cron_scripts.subprocess.TimeoutExpired('slow.py', 1)

    monkeypatch.setattr(cron_scripts.subprocess, 'run', boom)
    out = cron_scripts.run({'script': 'scripts/slow.py'})
    assert out['ok'] is False
    assert 'timed out' in out['error']


def test_run_standing_pins_notify_agent():
    queued = []
    original = cron_scripts.run
    try:
        cron_scripts.run = lambda decl: {'ok': True, 'notify': True,
                                         'summary': 's', 'script': 'scripts/x.py'}
        cron_scripts.run_standing(
            {}, {'taskId': 't3', 'name': 'loud', 'notifyAgent': 'theo'},
            lambda *a: None, lambda state, items: queued.extend(items))
        assert queued[0]['assignedTo'] == 'theo'
    finally:
        cron_scripts.run = original


def test_declaration_fields_validates_and_keeps_followup(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_scripts, '_repo_root', lambda: str(tmp_path))
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts' / 'ok.py').write_text('print(1)\n', encoding='utf-8')
    fields, error = cron_scripts.declaration_fields({
        'script': 'scripts/ok.py', 'scriptTimeoutSeconds': '60',
        'notifyAgent': 'theo'})
    assert error is None
    assert fields['script'] == 'scripts/ok.py'
    assert fields['scriptRunner'] == 'sandbox'
    assert fields['scriptTimeoutSeconds'] == 60
    assert fields['notifyAgent'] == 'theo'
    assert fields['scriptSha256']
    fields, error = cron_scripts.declaration_fields({'script': 'scripts/ok.sh'})
    assert fields is None and 'scripts/' in error
    fields, error = cron_scripts.declaration_fields({
        'script': 'scripts/ok.py', 'scriptTimeoutSeconds': 'soon'})
    assert fields is None and 'integer' in error
