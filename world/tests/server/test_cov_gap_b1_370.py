"""Coverage-gap tests for serve_parts/370_prune_broken_story_refs.py.

Git ref pruning/sync/worktree paths use mocked git helpers in temp dirs.
HTTP endpoint paths are called directly with mock Requests and patched auth,
DB, and service helpers. No network, no docker, no live database.
"""
import asyncio
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types
import unittest.mock as mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve  # noqa: E402
import sim  # noqa: E402
import core.config as config  # noqa: E402

_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='tt-cov-b1-370-')
    _MODULE_PATCHER = mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, 'library', '.passport.json'),
        SANDBOXES_DIR=os.path.join(_MODULE_TMP_DIR, 'sandboxes'),
        COLAB_STANDBY_ENABLED=False,
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


def _req(body, headers=None, json_side=None, query=None):
    req = mock.Mock()
    req.json = mock.AsyncMock(return_value=body, side_effect=json_side)
    req.headers = headers or {}
    req.query_params = query or {}
    return req


def _value(resp):
    return json.loads(resp.body)


def _run(returncode=0, stdout='', stderr=''):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@contextlib.contextmanager
def _fake_db(conn):
    yield conn


# ---------------------------------------------------------------------------
# _prune_broken_story_refs
# ---------------------------------------------------------------------------

def test_prune_packed_and_loose_refs(monkeypatch, tmp_path):
    git = tmp_path / '.git'
    (git / 'refs' / 'heads' / 'story').mkdir(parents=True)
    (git / 'logs' / 'refs' / 'heads' / 'story').mkdir(parents=True)
    packed = git / 'packed-refs'
    packed.write_text(
        '# pack-refs with: peeled\n'
        '^peeledline\n'
        'malformedline\n'
        'badsha refs/heads/story/bad\n'
        'goodsha refs/heads/story/good\n'
        'sha3 refs/heads/main\n')
    (git / 'logs' / 'refs' / 'heads' / 'story' / 'bad').write_text('log')
    (git / 'refs' / 'heads' / 'story' / 'bad').write_text('badsha')
    (git / 'refs' / 'heads' / 'story' / 'good').write_text('goodsha')
    (git / 'refs' / 'heads' / 'story' / 'empty').write_text('')
    (git / 'refs' / 'heads' / 'story' / 'sub').mkdir()
    (git / 'refs' / 'heads' / 'story' / 'unreadable').write_text('badsha')
    (git / 'logs' / 'refs' / 'heads' / 'story' / 'good').write_text('log')

    def fake_git(base, args):
        if args[:2] == ['cat-file', '-e']:
            return _run(0 if args[2] in ('goodsha',) else 1)
        return _run()

    monkeypatch.setattr(serve, '_run_git_sync', fake_git)
    removed = serve._prune_broken_story_refs(str(tmp_path))
    assert removed == 3
    text = packed.read_text()
    assert 'refs/heads/story/bad' not in text
    assert 'refs/heads/story/good' in text
    assert not (git / 'refs' / 'heads' / 'story' / 'bad').exists()
    assert not (git / 'logs' / 'refs' / 'heads' / 'story' / 'bad').exists()


def test_prune_oserror_paths(monkeypatch, tmp_path):
    git = tmp_path / '.git'
    (git / 'refs' / 'heads' / 'story').mkdir(parents=True)
    packed = git / 'packed-refs'
    packed.write_text('badsha refs/heads/story/bad\n')
    (git / 'refs' / 'heads' / 'story' / 'bad').write_text('badsha')
    (git / 'refs' / 'heads' / 'story' / 'locked').write_text('badsha')
    (git / 'refs' / 'heads' / 'story' / 'unreadable').write_text('badsha')
    real_open = open
    real_remove = os.remove

    def flaky_open(path, *a, **k):
        if str(path) == str(packed) and 'r' in (a[0] if a else k.get('mode', 'r')):
            raise OSError('no packed')
        if str(path).endswith('unreadable'):
            raise OSError('no ref')
        return real_open(path, *a, **k)

    def flaky_remove(path):
        if str(path).endswith('locked'):
            raise OSError('locked')
        return real_remove(path)

    monkeypatch.setattr('builtins.open', flaky_open)
    monkeypatch.setattr(serve.os, 'remove', flaky_remove)
    monkeypatch.setattr(serve, '_run_git_sync', lambda *a: _run(1))
    removed = serve._prune_broken_story_refs(str(tmp_path))
    assert removed == 1
    assert (git / 'refs' / 'heads' / 'story' / 'locked').exists()


def test_prune_reflog_remove_errors(monkeypatch, tmp_path):
    git = tmp_path / '.git'
    (git / 'refs' / 'heads' / 'story').mkdir(parents=True)
    (git / 'logs' / 'refs' / 'heads' / 'story').mkdir(parents=True)
    (git / 'packed-refs').write_text('badsha refs/heads/story/bad\n')
    (git / 'logs' / 'refs' / 'heads' / 'story' / 'bad').write_text('log')
    (git / 'refs' / 'heads' / 'story' / 'bad2').write_text('badsha')
    (git / 'logs' / 'refs' / 'heads' / 'story' / 'bad2').write_text('log')
    real_remove = os.remove

    def remove(path):
        if 'logs' in str(path):
            raise OSError('locked log')
        return real_remove(path)

    monkeypatch.setattr(serve, '_run_git_sync', lambda *a: _run(1))
    monkeypatch.setattr(serve.os, 'remove', remove)
    assert serve._prune_broken_story_refs(str(tmp_path)) == 2


def test_prune_missing_refs_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, '_run_git_sync', lambda *a: _run(1))
    assert serve._prune_broken_story_refs(str(tmp_path)) == 0


# ---------------------------------------------------------------------------
# _sync_base_workroom_to_live
# ---------------------------------------------------------------------------

def test_sync_disabled_and_no_live_head(monkeypatch):
    monkeypatch.setattr(config, 'ALLOW_WORKSPACE_SYNC', False)
    assert serve._sync_base_workroom_to_live('/tmp') is False
    monkeypatch.setattr(config, 'ALLOW_WORKSPACE_SYNC', True)
    monkeypatch.setattr(serve, '_prune_broken_story_refs', lambda base: 0)
    monkeypatch.setattr(serve, '_live_repo_head', lambda: None)
    assert serve._sync_base_workroom_to_live('/tmp') is False


def test_sync_already_current(monkeypatch):
    monkeypatch.setattr(config, 'ALLOW_WORKSPACE_SYNC', True)
    monkeypatch.setattr(serve, '_prune_broken_story_refs', lambda base: 0)
    monkeypatch.setattr(serve, '_live_repo_head', lambda: 'live')
    monkeypatch.setattr(serve, '_run_git_sync',
                        lambda base, args: _run(0, 'live\n'))
    assert serve._sync_base_workroom_to_live('/base') is True


def test_sync_full_path(monkeypatch):
    monkeypatch.setattr(config, 'ALLOW_WORKSPACE_SYNC', True)
    monkeypatch.setattr(serve, '_prune_broken_story_refs', lambda base: 0)
    monkeypatch.setattr(serve, '_live_repo_head', lambda: 'live')
    monkeypatch.setattr(serve, '_run_git_sync',
                        lambda base, args: _run(0, ''))
    assert serve._sync_base_workroom_to_live('/base') is True


def test_sync_fetch_failure(monkeypatch):
    monkeypatch.setattr(config, 'ALLOW_WORKSPACE_SYNC', True)
    monkeypatch.setattr(serve, '_prune_broken_story_refs', lambda base: 0)
    monkeypatch.setattr(serve, '_live_repo_head', lambda: 'live')

    def git(base, args):
        if args[:2] == ['rev-parse', 'HEAD']:
            return _run(0, 'old\n')
        if args[0] == 'fetch':
            return _run(1)
        return _run(0, '')
    monkeypatch.setattr(serve, '_run_git_sync', git)
    assert serve._sync_base_workroom_to_live('/base') is False


def test_sync_reset_failure(monkeypatch):
    monkeypatch.setattr(config, 'ALLOW_WORKSPACE_SYNC', True)
    monkeypatch.setattr(serve, '_prune_broken_story_refs', lambda base: 0)
    monkeypatch.setattr(serve, '_live_repo_head', lambda: 'live')
    calls = []

    def git(base, args):
        calls.append(args)
        if args[:2] == ['rev-parse', 'HEAD']:
            return _run(0, 'old\n')
        if args[:2] == ['reset', '--hard'] and args[2] == 'FETCH_HEAD':
            return _run(1)
        return _run(0, '')
    monkeypatch.setattr(serve, '_run_git_sync', git)
    assert serve._sync_base_workroom_to_live('/base') is False
    assert ['reset', '--hard', 'HEAD'] in calls


# ---------------------------------------------------------------------------
# ensure_story_worktree
# ---------------------------------------------------------------------------

def test_ensure_story_worktree_configures_and_creates(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, 'SANDBOXES_DIR', str(tmp_path / 'sandboxes'))
    monkeypatch.setattr(serve, '_sandbox_dir_for', lambda sid: str(tmp_path / 'base'))
    monkeypatch.setattr(serve, '_ensure_sandbox_git_repo', lambda d: None)
    monkeypatch.setattr(serve, '_base_sandbox_for_story', lambda sid: 'base')
    monkeypatch.setattr(serve, '_story_branch_name', lambda sid: 'story/t')
    monkeypatch.setattr(serve, '_worktree_is_linked', lambda w, b: False)
    monkeypatch.setattr(serve, '_sync_base_workroom_to_live', lambda b: True)
    identity = {'n': 0}

    def is_configured(path):
        identity['n'] += 1
        return False
    monkeypatch.setattr(serve, '_git_identity_configured', is_configured)
    calls = []

    def git(base, args):
        calls.append(args)
        if args[:2] == ['rev-parse', '--verify'] and args[2] == 'HEAD':
            return _run(1)
        if args[:2] == ['rev-parse', '--verify'] and args[2] == 'story/t':
            return _run(0)
        if args[0] == 'worktree':
            return _run(0)
        return _run(0)
    monkeypatch.setattr(serve, '_run_git_sync', git)
    out = serve.ensure_story_worktree('story-task-1')
    assert out == str(tmp_path / 'sandboxes' / 'story-task-1')
    assert any(a[:2] == ['worktree', 'add'] and 'story/t' in a for a in calls)
    assert any(a[:2] == ['config', 'user.email'] for a in calls)


def test_ensure_story_worktree_new_branch_and_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, 'SANDBOXES_DIR', str(tmp_path / 'sandboxes'))
    monkeypatch.setattr(serve, '_sandbox_dir_for', lambda sid: str(tmp_path / 'base'))
    monkeypatch.setattr(serve, '_ensure_sandbox_git_repo', lambda d: None)
    monkeypatch.setattr(serve, '_base_sandbox_for_story', lambda sid: 'base')
    monkeypatch.setattr(serve, '_story_branch_name', lambda sid: 'story/t')
    monkeypatch.setattr(serve, '_worktree_is_linked', lambda w, b: False)
    monkeypatch.setattr(serve, '_git_identity_configured', lambda p: True)
    monkeypatch.setattr(serve, '_sync_base_workroom_to_live', lambda b: True)

    def git(base, args):
        if args[:2] == ['rev-parse', '--verify'] and args[2] == 'HEAD':
            return _run(0)
        if args[:2] == ['rev-parse', '--verify'] and args[2] == 'story/t':
            return _run(1)
        if args[0] == 'worktree':
            return _run(1, '', 'worktree failed')
        return _run(0)
    monkeypatch.setattr(serve, '_run_git_sync', git)
    with pytest.raises(ValueError, match='failed to create'):
        serve.ensure_story_worktree('story-task-1')


# ---------------------------------------------------------------------------
# credential endpoints
# ---------------------------------------------------------------------------

def test_list_credentials_endpoint(monkeypatch):
    monkeypatch.setattr(serve, '_require_player_session', lambda request: False)
    assert asyncio.run(serve.list_credentials(_req({}))).status_code == 403
    monkeypatch.setattr(serve, '_require_player_session', lambda request: True)
    monkeypatch.setattr(serve, '_list_credentials', lambda: [{'name': 'a'}])
    resp = asyncio.run(serve.list_credentials(_req({})))
    assert _value(resp)['credentials'] == [{'name': 'a'}]


def test_add_credential_endpoint(monkeypatch):
    monkeypatch.setattr(serve, '_require_player_session', lambda request: False)
    assert asyncio.run(serve.add_credential(_req({}))).status_code == 403
    monkeypatch.setattr(serve, '_require_player_session', lambda request: True)
    stored = []
    monkeypatch.setattr(serve, '_store_credential',
                        lambda *a, **k: stored.append((a, k)))
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: None)
    monkeypatch.setattr(serve, '_append_passport_decision', lambda *a, **k: None)

    assert asyncio.run(serve.add_credential(_req({}))).status_code == 400
    assert asyncio.run(serve.add_credential(
        _req({'name': 'bad name'}))).status_code == 400
    assert asyncio.run(serve.add_credential(
        _req({'name': 'login', 'kind': 'login'}))).status_code == 400
    resp = asyncio.run(serve.add_credential(_req({
        'name': 'login', 'kind': 'login', 'username': 'u', 'password': 'p'})))
    assert resp.status_code == 200 and stored[-1][0][2]['kind'] == 'login'
    assert asyncio.run(serve.add_credential(
        _req({'name': 'tok'}))).status_code == 400
    resp = asyncio.run(serve.add_credential(_req({'name': 'tok', 'value': 'v'})))
    assert resp.status_code == 200 and stored[-1][0][2]['kind'] == 'token'


def test_delete_credential_endpoint(monkeypatch):
    monkeypatch.setattr(serve, '_require_player_session', lambda request: False)
    assert asyncio.run(serve.delete_credential('a', _req({}))).status_code == 403
    monkeypatch.setattr(serve, '_require_player_session', lambda request: True)
    deleted = []
    monkeypatch.setattr(serve, '_delete_credential', lambda name: deleted.append(name))
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: None)
    monkeypatch.setattr(serve, '_append_passport_decision', lambda *a, **k: None)
    resp = asyncio.run(serve.delete_credential('a', _req({})))
    assert _value(resp)['ok'] is True and deleted == ['a']


def test_credential_list_endpoint(monkeypatch):
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (False, None))
    assert asyncio.run(serve.credential_list(_req({}))).status_code == 401
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (True, 'player'))
    monkeypatch.setattr(serve, '_list_credentials',
                        lambda: [{'name': 'a', 'service': 's', 'kind': 'token',
                                  'status': 'active', 'proposedBy': 'player'}])
    monkeypatch.setattr(serve, '_credential_payload', lambda name: {'kind': 'token',
                                                                    'token': 'secret'})
    monkeypatch.setattr(serve, '_mask_credential_preview',
                        lambda payload: {'masked': True})
    resp = asyncio.run(serve.credential_list(_req({})))
    assert _value(resp)['credentials'][0]['preview'] == {'masked': True}


def test_credential_propose_gaps(monkeypatch):
    monkeypatch.setattr(serve, 'check_rate_limit', lambda aid: True)
    monkeypatch.setattr(serve, 'verify_agent_key', lambda aid, key: True)
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: None)
    monkeypatch.setattr(serve, '_validate_credential_body',
                        lambda body: (None, None, None, 'bad body'))
    resp = asyncio.run(serve.credential_propose(_req({'agentId': 'a'})))
    assert resp.status_code == 400 and 'bad body' in _value(resp)['error']

    monkeypatch.setattr(serve, '_validate_credential_body',
                        lambda body: ('n', 's', {'kind': 'token'}, None))
    monkeypatch.setattr(serve, '_feed_registry_status',
                        lambda table, name, pk_col=None: 'proposed')
    resp = asyncio.run(serve.credential_propose(_req({'agentId': 'a'})))
    assert resp.status_code == 409 and 'pending review' in _value(resp)['error']


def test_credential_approve_unauthorised(monkeypatch):
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (False, None))
    assert asyncio.run(serve.credential_approve(_req({}))).status_code == 403


def test_credential_remove_endpoint(monkeypatch):
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (False, None))
    assert asyncio.run(serve.credential_remove(_req({}))).status_code == 403
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (True, 'player'))
    assert asyncio.run(serve.credential_remove(_req({}))).status_code == 400
    monkeypatch.setattr(serve, '_feed_registry_status',
                        lambda *a, **k: None)
    assert asyncio.run(serve.credential_remove(
        _req({'name': 'missing'}))).status_code == 404

    monkeypatch.setattr(serve, '_feed_registry_status',
                        lambda *a, **k: 'active')
    conn = mock.Mock()
    monkeypatch.setattr(serve, '_db', lambda: _fake_db(conn))
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: None)
    resp = asyncio.run(serve.credential_remove(_req({'name': 'a'})))
    assert _value(resp)['status'] == 'removed'
    assert conn.execute.call_count == 2


# ---------------------------------------------------------------------------
# /api/download gaps
# ---------------------------------------------------------------------------

def _download_env(monkeypatch):
    monkeypatch.setattr(serve, 'check_rate_limit', lambda aid: True)
    monkeypatch.setattr(serve, 'verify_agent_key', lambda aid, key: True)
    monkeypatch.setattr(serve, '_login_safe_url', lambda url: True)
    monkeypatch.setattr(serve, '_is_allowlisted_host', lambda host: True)
    monkeypatch.setattr(serve, '_sanitize_filename', lambda name: name or 'download.bin')
    monkeypatch.setattr(serve, '_download_stream_sync',
                        lambda *a, **k: {'bytes': 3, 'finalUrl': a[0], 'status': 200})
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: None)
    monkeypatch.setattr(serve, '_append_passport_decision', lambda *a, **k: None)


def test_download_rate_limit_and_validation(monkeypatch):
    monkeypatch.setattr(serve, 'check_rate_limit', lambda aid: False)
    assert asyncio.run(serve.download_file(
        _req({'agentId': 'a'}))).status_code == 429

    _download_env(monkeypatch)
    assert asyncio.run(serve.download_file(_req({'agentId': 'a'}))).status_code == 400

    resp = asyncio.run(serve.download_file(_req({
        'agentId': 'a', 'url': 'https://example.com/f.bin', 'maxBytes': 'bad'})))
    assert resp.status_code == 200


def test_download_login_handle_and_failure(monkeypatch):
    _download_env(monkeypatch)
    monkeypatch.setattr(serve, 'resolve_capability_handle',
                        lambda *a: {'kind': 'login', 'credential_name': 'c'})
    resp = asyncio.run(serve.download_file(_req({
        'agentId': 'a', 'url': 'https://example.com/f', 'capabilityHandle': 'h'})))
    assert resp.status_code == 400

    monkeypatch.setattr(serve, 'resolve_capability_handle',
                        lambda *a: {'kind': 'token', 'credential_name': 'c',
                                    'secret': 's'})
    monkeypatch.setattr(serve, '_capability_auth_headers',
                        lambda name, secret: {'Authorization': 'Bearer x'})
    monkeypatch.setattr(serve, '_download_stream_sync',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('net down')))
    resp = asyncio.run(serve.download_file(_req({
        'agentId': 'a', 'url': 'https://example.com/f', 'capabilityHandle': 'h'})))
    assert resp.status_code == 400 and 'download failed' in _value(resp)['error']


# ---------------------------------------------------------------------------
# SMS endpoints
# ---------------------------------------------------------------------------

def test_sms_inbound_body_required(monkeypatch):
    monkeypatch.setattr(serve, '_load_env',
                        lambda: {'SMS_FORWARD_SECRET': 'sek'})
    req = _req({'from': 'a', 'body': '   '})
    req.headers = {'X-SMS-Forward-Secret': 'sek'}
    assert asyncio.run(serve.sms_inbound(req)).status_code == 400
    req = _req({'from': '', 'body': 'hi'})
    req.headers = {'X-SMS-Forward-Secret': 'sek'}
    assert asyncio.run(serve.sms_inbound(req)).status_code == 400


def test_sms_inbox_limit_conversion(monkeypatch):
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (True, 'player'))
    conn = mock.Mock()
    conn.execute.return_value.fetchall.return_value = [(1,)]
    monkeypatch.setattr(serve, '_db', lambda: _fake_db(conn))
    monkeypatch.setattr(serve, '_sms_message_row',
                        lambda row: {'id': 1, 'body': 'hi', 'otpCode': None})
    resp = asyncio.run(serve.sms_inbox(_req({}, query={'limit': 'bad'})))
    assert resp.status_code == 200
    assert conn.execute.call_args[0][1] == (20,)


def test_sms_grant_gaps(monkeypatch):
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (True, 'player'))
    assert asyncio.run(serve.sms_grant(
        _req({'agentId': 'a', 'messageId': 'bad'}))).status_code == 400

    conn = mock.Mock()
    conn.execute.return_value.fetchone.return_value = (1,)
    monkeypatch.setattr(serve, '_db', lambda: _fake_db(conn))
    minted = []
    monkeypatch.setattr(serve, '_sms_mint_grant',
                        lambda *a: minted.append(a) or 'gid')
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: None)
    monkeypatch.setattr(serve, '_append_passport_decision', lambda *a, **k: None)
    resp = asyncio.run(serve.sms_grant(
        _req({'agentId': 'a', 'messageId': 1, 'ttlSec': 'bad'})))
    assert resp.status_code == 200 and _value(resp)['expiresInSec'] == 300
    assert minted

    conn.execute.return_value.fetchone.return_value = None
    assert asyncio.run(serve.sms_grant(
        _req({'agentId': 'a', 'messageId': 1}))).status_code == 404


def test_sms_read_gaps(monkeypatch):
    monkeypatch.setattr(serve, 'check_rate_limit', lambda aid: False)
    assert asyncio.run(serve.sms_read(_req({'agentId': 'a'}))).status_code == 429
    monkeypatch.setattr(serve, 'check_rate_limit', lambda aid: True)
    monkeypatch.setattr(serve, 'verify_agent_key', lambda aid, key: False)
    assert asyncio.run(serve.sms_read(_req({'agentId': 'a'}))).status_code == 401
    monkeypatch.setattr(serve, 'verify_agent_key', lambda aid, key: True)
    assert asyncio.run(serve.sms_read(_req({'agentId': 'a'}))).status_code == 400


# ---------------------------------------------------------------------------
# tank-account endpoints
# ---------------------------------------------------------------------------

def test_tank_create_and_list_gaps(monkeypatch):
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (False, None))
    assert asyncio.run(serve.tank_account_create(_req({}))).status_code == 403
    assert asyncio.run(serve.tank_account_list(_req({}))).status_code == 403

    monkeypatch.setattr(serve, '_operator_gate', lambda request: (True, 'player'))
    bad = mock.Mock()
    bad.json = mock.AsyncMock(side_effect=ValueError('bad'))
    assert asyncio.run(serve.tank_account_create(bad)).status_code == 400
    assert asyncio.run(serve.tank_account_create(_req('not-dict'))).status_code == 400

    monkeypatch.setattr(serve.tank_accounts, 'create',
                        lambda **kwargs: (_ for _ in ()).throw(ValueError('bad input')))
    assert asyncio.run(serve.tank_account_create(_req({}))).status_code == 400
    monkeypatch.setattr(serve.tank_accounts, 'create',
                        lambda **kwargs: (_ for _ in ()).throw(RuntimeError('vault down')))
    assert asyncio.run(serve.tank_account_create(_req({}))).status_code == 503

    monkeypatch.setattr(serve.tank_accounts, 'list_accounts', lambda: [{'id': 'x'}])
    assert _value(asyncio.run(serve.tank_account_list(_req({}))))['accounts'] == [{'id': 'x'}]


def test_tank_get_code_grant_gaps(monkeypatch):
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (False, None))
    assert asyncio.run(serve.tank_account_get('x', _req({}))).status_code == 403
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (True, 'player'))
    monkeypatch.setattr(serve.tank_accounts, 'get', lambda aid: None)
    assert asyncio.run(serve.tank_account_get('x', _req({}))).status_code == 404
    monkeypatch.setattr(serve.tank_accounts, 'get', lambda aid: {'id': aid})
    assert asyncio.run(serve.tank_account_get('x', _req({}))).status_code == 200

    monkeypatch.setattr(serve.tank_accounts, 'code',
                        lambda *a, **k: (_ for _ in ()).throw(KeyError('x')))
    assert asyncio.run(serve.tank_account_code('x', _req({}))).status_code == 404
    monkeypatch.setattr(serve.tank_accounts, 'code',
                        lambda *a, **k: (_ for _ in ()).throw(ValueError('bad')))
    assert asyncio.run(serve.tank_account_code('x', _req({}))).status_code == 400

    monkeypatch.setattr(serve, '_operator_gate', lambda request: (False, None))
    assert asyncio.run(serve.tank_account_grant('x', _req({}))).status_code == 403
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (True, 'player'))
    bad = mock.Mock()
    bad.json = mock.AsyncMock(side_effect=ValueError('bad'))
    assert asyncio.run(serve.tank_account_grant('x', bad)).status_code == 400
    assert asyncio.run(serve.tank_account_grant('x', _req('nope'))).status_code == 400
    monkeypatch.setattr(serve.tank_accounts, 'mint_grant',
                        lambda *a, **k: (_ for _ in ()).throw(KeyError('x')))
    assert asyncio.run(serve.tank_account_grant(
        'x', _req({'agentId': 'a'}))).status_code == 404
    monkeypatch.setattr(serve.tank_accounts, 'mint_grant',
                        lambda *a, **k: (_ for _ in ()).throw(ValueError('bad')))
    assert asyncio.run(serve.tank_account_grant(
        'x', _req({'agentId': 'a'}))).status_code == 400


def test_tank_agent_code_gaps(monkeypatch):
    monkeypatch.setattr(serve, '_agent_id_for_key', lambda key: None)
    assert asyncio.run(serve.tank_account_agent_code('x', _req({}))).status_code == 401
    monkeypatch.setattr(serve, '_agent_id_for_key', lambda key: 'ben')
    monkeypatch.setattr(serve, 'check_rate_limit', lambda aid: False)
    assert asyncio.run(serve.tank_account_agent_code('x', _req({}))).status_code == 429
    monkeypatch.setattr(serve, 'check_rate_limit', lambda aid: True)
    bad = mock.Mock()
    bad.json = mock.AsyncMock(side_effect=ValueError('bad'))
    assert asyncio.run(serve.tank_account_agent_code('x', bad)).status_code == 400
    assert asyncio.run(serve.tank_account_agent_code(
        'x', _req('nope'))).status_code == 400
    assert asyncio.run(serve.tank_account_agent_code('x', _req({}))).status_code == 400
    monkeypatch.setattr(serve.tank_accounts, 'consume_grant', lambda *a: True)
    monkeypatch.setattr(serve.tank_accounts, 'code',
                        lambda *a, **k: (_ for _ in ()).throw(KeyError('x')))
    assert asyncio.run(serve.tank_account_agent_code(
        'x', _req({'grant': 'g'}))).status_code == 404
    monkeypatch.setattr(serve.tank_accounts, 'code',
                        lambda *a, **k: (_ for _ in ()).throw(ValueError('bad')))
    assert asyncio.run(serve.tank_account_agent_code(
        'x', _req({'grant': 'g'}))).status_code == 400


def test_tank_rotate_and_delete_gaps(monkeypatch):
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (False, None))
    assert asyncio.run(serve.tank_account_rotate('x', _req({}))).status_code == 403
    assert asyncio.run(serve.tank_account_delete('x', _req({}))).status_code == 403
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (True, 'player'))
    bad = mock.Mock()
    bad.json = mock.AsyncMock(side_effect=ValueError('bad'))
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (True, 'player'))

    monkeypatch.setattr(serve.tank_accounts, 'rotate',
                        lambda *a, **k: (_ for _ in ()).throw(KeyError('x')))
    resp = asyncio.run(serve.tank_account_rotate('x', bad))
    assert resp.status_code == 404
    assert asyncio.run(serve.tank_account_rotate(
        'x', _req('not-a-dict'))).status_code == 404
    monkeypatch.setattr(serve.tank_accounts, 'rotate',
                        lambda *a, **k: (_ for _ in ()).throw(ValueError('bad')))
    assert asyncio.run(serve.tank_account_rotate('x', _req({}))).status_code == 400
    monkeypatch.setattr(serve.tank_accounts, 'rotate',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('down')))
    assert asyncio.run(serve.tank_account_rotate('x', _req({}))).status_code == 503

    monkeypatch.setattr(serve.tank_accounts, 'delete',
                        lambda *a, **k: (_ for _ in ()).throw(KeyError('x')))
    assert asyncio.run(serve.tank_account_delete('x', _req({}))).status_code == 404
    monkeypatch.setattr(serve.tank_accounts, 'delete', lambda *a, **k: {'ok': True})
    assert asyncio.run(serve.tank_account_delete('x', _req({}))).status_code == 200


# ---------------------------------------------------------------------------
# CAPTCHA endpoints
# ---------------------------------------------------------------------------

def test_captcha_policies_gaps(monkeypatch):
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (False, None))
    assert asyncio.run(serve.captcha_policies(_req({}))).status_code == 403
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (True, 'player'))
    monkeypatch.setattr(serve.captcha, 'list_policies', lambda: [{'host': 'x'}])
    resp = asyncio.run(serve.captcha_policies(_req({})))
    assert _value(resp)['policies'] == [{'host': 'x'}]


def test_captcha_policy_set_gaps(monkeypatch):
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (False, None))
    assert asyncio.run(serve.captcha_policy_set(_req({}))).status_code == 403
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (True, 'player'))
    bad = mock.Mock()
    bad.json = mock.AsyncMock(side_effect=ValueError('bad'))
    assert asyncio.run(serve.captcha_policy_set(bad)).status_code == 400
    assert asyncio.run(serve.captcha_policy_set(_req('nope'))).status_code == 400
    monkeypatch.setattr(serve.captcha, 'set_policy',
                        lambda *a, **k: (_ for _ in ()).throw(ValueError('bad')))
    assert asyncio.run(serve.captcha_policy_set(_req({}))).status_code == 400
    monkeypatch.setattr(serve.captcha, 'set_policy',
                        lambda *a, **k: {'host': 'x'})
    assert asyncio.run(serve.captcha_policy_set(_req({}))).status_code == 200


def test_captcha_answer_gaps(monkeypatch):
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (False, None))
    assert asyncio.run(serve.captcha_answer('r', _req({}))).status_code == 403
    monkeypatch.setattr(serve, '_operator_gate', lambda request: (True, 'player'))
    bad = mock.Mock()
    bad.json = mock.AsyncMock(side_effect=ValueError('bad'))
    assert asyncio.run(serve.captcha_answer('r', bad)).status_code == 400
    assert asyncio.run(serve.captcha_answer('r', _req('nope'))).status_code == 400
    monkeypatch.setattr(serve.captcha, 'set_answer',
                        lambda *a, **k: (_ for _ in ()).throw(ValueError('bad')))
    assert asyncio.run(serve.captcha_answer('r', _req({}))).status_code == 400
    monkeypatch.setattr(serve.captcha, 'set_answer', lambda *a, **k: None)
    assert asyncio.run(serve.captcha_answer('r', _req({}))).status_code == 404
    monkeypatch.setattr(serve.captcha, 'set_answer', lambda *a, **k: {'id': 'r'})
    assert asyncio.run(serve.captcha_answer('r', _req({}))).status_code == 200
