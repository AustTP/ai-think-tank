"""Host-run cron scripts are operator-owned and allowlisted. A standing task
must not be able to name an arbitrary script under scripts/."""

import hashlib
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import cron_scripts  # noqa: E402


def _make_repo(tmp_path, source, hosts=None, runner='sandbox', sha=None):
    (tmp_path / 'scripts').mkdir(parents=True, exist_ok=True)
    script = tmp_path / 'scripts' / 'host.py'
    script.write_text(source, encoding='utf-8')
    if sha is None:
        sha = hashlib.sha256(script.read_bytes()).hexdigest()
    entry = {
        'path': 'scripts/host.py',
        'sha256': sha,
        'runner': runner,
        'allowedHosts': hosts or [],
        'env': [],
    }
    if runner == 'host':
        entry['hostReason'] = 'test host-bound script'
    payload = {'comment': 'test allowlist', 'scripts': [entry]}
    (tmp_path / 'scripts' / 'cron_allowlist.json').write_text(
        json.dumps(payload), encoding='utf-8')
    return script


def test_allowlisted_script_resolves():
    path, error = cron_scripts.resolve_script('scripts/check_models.py')
    assert error is None
    assert path and path.endswith('scripts/check_models.py')


def test_non_allowlisted_script_is_refused():
    with mock.patch.object(cron_scripts, '_allowed_scripts',
                           return_value={'scripts/check_models.py': {}}):
        path, error = cron_scripts.resolve_script('scripts/browser_record.py')
    assert path is None
    assert 'allowlist' in error


def test_run_refuses_non_allowlisted_without_executing():
    with mock.patch.object(cron_scripts, '_allowed_scripts',
                           return_value={'scripts/check_models.py': {}}):
        result = cron_scripts.run({'script': 'scripts/browser_record.py'})
    assert result['ok'] is False
    assert 'allowlist' in result['error']


def test_missing_allowlist_fails_closed():
    with mock.patch.object(cron_scripts, '_allowed_scripts', return_value=None):
        path, error = cron_scripts.resolve_script('scripts/check_models.py')
    assert path is None
    assert 'allowlist' in error


def test_hash_mismatch_refused(tmp_path, monkeypatch):
    _make_repo(tmp_path, 'print(1)\n', sha='deadbeef')
    monkeypatch.setattr(cron_scripts, '_repo_root', lambda: str(tmp_path))
    path, error = cron_scripts.resolve_script('scripts/host.py')
    assert path is None
    assert 'hash' in error


def test_url_host_not_allowed_refused(tmp_path, monkeypatch):
    _make_repo(tmp_path, "print('https://evil.example/x')\n")
    monkeypatch.setattr(cron_scripts, '_repo_root', lambda: str(tmp_path))
    path, error = cron_scripts.resolve_script('scripts/host.py')
    assert path is None
    assert 'allowlist' in error


def test_url_host_vetted_allowed(tmp_path, monkeypatch):
    _make_repo(tmp_path, "print('https://allowed.example/x')\n",
               hosts=['allowed.example'])
    monkeypatch.setattr(cron_scripts, '_repo_root', lambda: str(tmp_path))
    path, error = cron_scripts.resolve_script('scripts/host.py')
    assert error is None and path


def test_host_script_requires_explicit_approval(tmp_path, monkeypatch):
    _make_repo(tmp_path, 'print(1)\n', runner='host')
    monkeypatch.setattr(cron_scripts, '_repo_root', lambda: str(tmp_path))
    fields, error = cron_scripts.declaration_fields({'script': 'scripts/host.py'})
    assert fields is None and 'allowHostExecution' in error
    fields, error = cron_scripts.declaration_fields({
        'script': 'scripts/host.py', 'allowHostExecution': True})
    assert error is None
    assert fields['scriptRunner'] == 'host'
    assert fields['allowHostExecution'] is True
