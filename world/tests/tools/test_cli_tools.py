"""CLI transport: allowlisted argv, tiers, timeout, and output caps."""
from __future__ import annotations

import json
import sys

import pytest

import tools.cli_tools as cli_tools


@pytest.fixture
def cli(tmp_path, monkeypatch):
    config = {
        'tools': {
            'echo': {
                'name': 'Echo',
                'enabled': True,
                'command': [sys.executable, '-c', "print('hello')"],
                'read_only': True,
            },
            'echo_args': {
                'name': 'Echo args',
                'enabled': True,
                'command': [sys.executable, '-c',
                            'import sys; print(sys.argv[1])'],
                'read_only': True,
                'allow_args': True,
            },
            'mutate': {
                'name': 'Mutate',
                'enabled': True,
                'command': [sys.executable, '-c', "print('mutated')"],
                'read_only': False,
            },
            'sleep': {
                'name': 'Sleep',
                'enabled': True,
                'command': [sys.executable, '-c',
                            'import time; time.sleep(2)'],
                'read_only': True,
                'timeout_s': 1,
            },
            'big': {
                'name': 'Big output',
                'enabled': True,
                'command': [sys.executable, '-c', "print('x' * 5000)"],
                'read_only': True,
                'max_output_bytes': 1024,
            },
            'env': {
                'name': 'Env',
                'enabled': True,
                'command': [sys.executable, '-c',
                            'import os; print(os.environ.get("CHILD", ""))'],
                'read_only': True,
                'env_from': {'CHILD': 'PARENT'},
            },
            'off': {
                'name': 'Off',
                'enabled': False,
                'command': [sys.executable, '-c', "print('no')"],
                'read_only': True,
            },
        },
    }
    path = tmp_path / 'cli_tools.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(cli_tools, 'REGISTRY_PATH', str(path))
    cli_tools.reload_tools()
    yield


def test_tools_text_lists_tiers(cli):
    text = cli_tools.tools_text()
    assert 'Echo' in text
    assert 'read-only' in text
    assert 'commit' in text


def test_read_only_runs(cli):
    out = cli_tools.run_tool('echo', read_only=True)
    assert out['ok'] is True
    assert 'hello' in out['stdout']


def test_read_cli_refuses_commit(cli):
    out = cli_tools.run_tool('mutate', read_only=True)
    assert out['ok'] is False
    assert 'commit-tier' in out['error']


def test_run_cli_runs_commit(cli):
    out = cli_tools.run_tool('mutate')
    assert out['ok'] is True
    assert 'mutated' in out['stdout']


def test_extra_args_refused_by_default(cli):
    out = cli_tools.run_tool('echo', args=['extra'], read_only=True)
    assert out['ok'] is False
    assert 'does not allow' in out['error']


def test_extra_args_allowed_when_configured(cli):
    out = cli_tools.run_tool('echo_args', args=['world'], read_only=True)
    assert out['ok'] is True
    assert 'world' in out['stdout']


def test_timeout(cli):
    out = cli_tools.run_tool('sleep', read_only=True)
    assert out['ok'] is False
    assert 'timed out' in out['error']


def test_output_cap(cli):
    out = cli_tools.run_tool('big', read_only=True)
    assert out['ok'] is True
    assert out['truncated'] is True
    assert '[truncated]' in out['stdout']


def test_env_from(cli, monkeypatch):
    monkeypatch.setenv('PARENT', 'from-parent')
    out = cli_tools.run_tool('env', read_only=True)
    assert out['ok'] is True
    assert 'from-parent' in out['stdout']


def test_disabled(cli):
    out = cli_tools.run_tool('off', read_only=True)
    assert out['ok'] is False
    assert 'disabled' in out['error']


def test_unknown(cli):
    with pytest.raises(cli_tools.CliToolError):
        cli_tools.get_tool('missing')
