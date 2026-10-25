"""Coverage-gap tests for serve_parts/070_is_latency_sensitive.py.

Direct helper tests (no live network, no model calls). The module-wide patcher
redirects every derived path into a throwaway temp dir and seeds a fresh DB so
nothing touches the live think_tank.db.
"""
import contextlib
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest.mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve  # noqa: E402
import sim  # noqa: E402
import core.config as config  # noqa: E402

_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='tt-cov-b1-070-')
    _MODULE_PATCHER = __import__('unittest').mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, 'library', '.passport.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


def _state(**overrides):
    state = {
        'agents': {'nora': {}, 'ben': {}, 'theo': {}, 'ruth': {}, 'cora': {}},
        'agentRoster': [
            {'id': 'theo', 'name': 'Theo', 'isAdmin': True, 'isDirector': True},
            {'id': 'nora', 'name': 'Nora', 'isDirector': True},
            {'id': 'ben', 'name': 'Ben', 'isDirector': True, 'director': 'theo'},
            {'id': 'ruth', 'name': 'Ruth', 'director': 'nora'},
        ],
        'teams': [
            {'id': 'nora', 'directorId': 'nora', 'members': ['ruth']},
            {'id': 'ben', 'directorId': 'ben', 'members': ['cora']},
        ],
        'tasks': {},
        'workQueue': [],
    }
    state.update(overrides)
    return state


def _patch_state(monkeypatch, state):
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: state)
    monkeypatch.setattr(serve, 'save_state_to_db', lambda s: None)
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: None)


# ---------------------------------------------------------------------------
# tier selection
# ---------------------------------------------------------------------------

def test_slug_for_band_all_fallbacks(monkeypatch):
    monkeypatch.setattr(serve, '_coding_tier_slug', lambda: None)
    monkeypatch.setattr(serve, '_high_tier_slug', lambda: None)
    monkeypatch.setattr(serve, '_mid_tier_slug', lambda: 'mid')
    monkeypatch.setattr(serve, '_low_tier_slug', lambda: 'low')
    assert serve._slug_for_band('coding') == 'mid'
    assert serve._slug_for_band('high') == 'mid'
    assert serve._slug_for_band('mid') == 'mid'
    assert serve._slug_for_band('anything') == 'low'

    monkeypatch.setattr(serve, '_coding_tier_slug', lambda: 'code')
    monkeypatch.setattr(serve, '_high_tier_slug', lambda: 'high')
    monkeypatch.setattr(serve, '_mid_tier_slug', lambda: None)
    monkeypatch.setattr(serve, '_low_tier_slug', lambda: None)
    assert serve._slug_for_band('coding') == 'code'
    assert serve._slug_for_band('high') == 'high'
    assert serve._slug_for_band('mid') is None
    assert serve._slug_for_band('other') is None


def _tier_env(monkeypatch, route, apply=True):
    monkeypatch.setattr(serve.policies, 'choose_route', lambda *a, **k: route)
    monkeypatch.setattr(serve.policies, 'apply_enabled', lambda: apply)
    monkeypatch.setattr(serve.policies, 'record_shadow', lambda *a, **k: None)
    monkeypatch.setattr(serve, '_high_tier_budget_exceeded', lambda: False)
    monkeypatch.setattr(serve, '_coding_tier_slug', lambda: 'code')
    monkeypatch.setattr(serve, '_high_tier_slug', lambda: 'high')
    monkeypatch.setattr(serve, '_mid_tier_slug', lambda: 'mid')
    monkeypatch.setattr(serve, '_low_tier_slug', lambda: 'low')


def test_resolve_model_tier_apply_bands(monkeypatch):
    _tier_env(monkeypatch, {'band': 'high', 'best_of': 1})
    assert serve._resolve_model_tier('routine work') == 'high'
    monkeypatch.setattr(serve, '_high_tier_budget_exceeded', lambda: True)
    assert serve._resolve_model_tier('routine work') == 'mid'


def test_resolve_model_tier_apply_disabled_code_lane(monkeypatch):
    _tier_env(monkeypatch, {'band': 'low', 'best_of': 1}, apply=False)
    assert serve._resolve_model_tier('routine work', task_type='code') == 'code'


def test_resolve_model_tier_decider_outage_fails_closed(monkeypatch):
    _tier_env(monkeypatch, {'band': 'low', 'best_of': 1}, apply=False)
    calls = {'n': 0}

    def boom(*a, **k):
        calls['n'] += 1
        raise RuntimeError('jev down')
    assert serve._resolve_model_tier('write a report', decider=boom) == 'low'
    assert calls['n'] == 1


def test_resolve_model_tier_high_budget_exceeded(monkeypatch):
    _tier_env(monkeypatch, {'band': 'low', 'best_of': 1}, apply=False)
    monkeypatch.setattr(serve, '_high_tier_budget_exceeded', lambda: True)
    assert serve._resolve_model_tier(
        'write a report', allow_high=True,
        decider=lambda *a, **k: ('high', 0.99)) == 'mid'


def test_resolve_model_tier_mid_decision(monkeypatch):
    _tier_env(monkeypatch, {'band': 'low', 'best_of': 1}, apply=False)
    assert serve._resolve_model_tier(
        'write a report', decider=lambda *a, **k: ('mid', 0.99)) == 'mid'


def test_is_latency_sensitive_task_type():
    assert serve._is_latency_sensitive('anything', 'pulse') is True
    assert serve._is_latency_sensitive('live alert feed') is True
    assert serve._is_latency_sensitive('ordinary work') is False


def test_resolve_model_tier_latency_and_best_effort(monkeypatch):
    _tier_env(monkeypatch, {'band': 'low', 'best_of': 1}, apply=False)
    assert serve._resolve_model_tier('pulse triage') == 'low'
    high = lambda *a, **k: ('high', 0.99)
    assert serve._resolve_model_tier('write a report', best_effort=True,
                                     decider=high) == 'high'
    monkeypatch.setattr(serve, '_high_tier_budget_exceeded', lambda: True)
    assert serve._resolve_model_tier('write a report', best_effort=True,
                                     decider=high) == 'mid'


def test_resolve_model_tier_high_decision_without_budget_cap(monkeypatch):
    _tier_env(monkeypatch, {'band': 'low', 'best_of': 1}, apply=False)
    assert serve._resolve_model_tier(
        'write a report', allow_high=True,
        decider=lambda *a, **k: ('high', 0.99)) == 'high'


# ---------------------------------------------------------------------------
# team lookup and live reads
# ---------------------------------------------------------------------------

def test_supervisor_team_for_missing_inputs():
    assert serve.supervisor_team_for(None, 'nora') is None
    assert serve.supervisor_team_for(_state(), '') is None
    assert serve.supervisor_team_for(_state(), 'nora')['id'] == 'nora'


def _live_state():
    return _state(agents={}, workloads={})


def test_live_read_ls_file_and_escape(monkeypatch, tmp_path):
    (tmp_path / 'hello.txt').write_text('hello\n')
    (tmp_path / '.env').write_text('SECRET=1\n')
    monkeypatch.setattr(serve, 'THINK_TANK_DIR', str(tmp_path))
    _patch_state(monkeypatch, _live_state())
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda state, aid: True)

    out = serve.supervisor_live_read('nora', {'op': 'ls'})
    assert '.env' not in out['entries']
    out = serve.supervisor_live_read('nora', {'op': 'ls', 'path': 'hello.txt'})
    assert out['entries'] == ['hello.txt']
    out = serve.supervisor_live_read('nora', {'op': 'ls', 'path': '../escape'})
    assert 'escapes' in out['error']
    out = serve.supervisor_live_read('nora', {'op': 'ls', 'path': '.git/x'})
    assert 'not readable' in out['error']
    out = serve.supervisor_live_read('nora', {'op': 'ls', 'path': '.env'})
    assert 'not readable' in out['error']


def test_live_read_read_file_variants(monkeypatch, tmp_path):
    target = tmp_path / 'notes.txt'
    target.write_text('one\ntwo\nthree\nfour\n')
    monkeypatch.setattr(serve, 'THINK_TANK_DIR', str(tmp_path))
    _patch_state(monkeypatch, _live_state())
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda state, aid: True)

    out = serve.supervisor_live_read('nora', {'op': 'read', 'path': 'notes.txt'})
    assert out['totalLines'] == 4 and 'one' in out['text']
    out = serve.supervisor_live_read(
        'nora', {'op': 'read', 'path': 'notes.txt', 'start': 2, 'end': 3})
    assert out['start'] == 2 and out['end'] == 3
    out = serve.supervisor_live_read('nora', {'op': 'read', 'path': 'notes.txt', 'start': 2})
    assert out['start'] == 2
    out = serve.supervisor_live_read('nora', {'op': 'read', 'path': '.'})
    assert out['error'] == 'not a file'
    out = serve.supervisor_live_read('nora', {'op': 'read', 'path': '../escape'})
    assert 'escapes' in out['error']
    monkeypatch.setattr(serve, 'LIVE_READ_MAX_BYTES', 1)
    out = serve.supervisor_live_read('nora', {'op': 'read', 'path': 'notes.txt'})
    assert 'too large' in out['error']


def test_live_read_grep_variants(monkeypatch, tmp_path):
    root = tmp_path / 'world'
    root.mkdir()
    (root / 'a.py').write_text('needle here\n' * 60)
    (root / 'b.py').write_text('nothing\n')
    (root / 'link.py').symlink_to(root / 'a.py')
    (root / 'huge.py').write_text('needle\n' * 40)
    monkeypatch.setattr(serve, 'THINK_TANK_DIR', str(tmp_path))
    _patch_state(monkeypatch, _live_state())
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda state, aid: True)

    out = serve.supervisor_live_read('nora', {'op': 'grep', 'pattern': ''})
    assert 'pattern' in out['error']
    out = serve.supervisor_live_read('nora', {'op': 'grep', 'pattern': 'x' * 121})
    assert 'pattern' in out['error']
    out = serve.supervisor_live_read(
        'nora', {'op': 'grep', 'pattern': 'needle', 'path': '../escape'})
    assert 'escapes' in out['error']

    monkeypatch.setattr(serve, 'LIVE_READ_MAX_BYTES', 100)
    out = serve.supervisor_live_read(
        'nora', {'op': 'grep', 'pattern': 'needle', 'path': 'world'})
    assert out['matches'] == []
    assert out['truncated'] is False

    monkeypatch.setattr(serve, 'LIVE_READ_MAX_BYTES', 8_000_000)
    out = serve.supervisor_live_read(
        'nora', {'op': 'grep', 'pattern': 'needle', 'path': 'world'})
    assert out['truncated'] is True
    assert len(out['matches']) == 50

    real_open = open

    def flaky_open(path, *a, **k):
        if str(path).endswith('b.py'):
            raise OSError('nope')
        return real_open(path, *a, **k)

    monkeypatch.setattr('builtins.open', flaky_open)
    (root / 'a.py').write_text('needle here\n' * 2)
    (root / 'b.py').write_text('needle here\n')
    (root / 'huge.py').write_text('needle here\n')
    out = serve.supervisor_live_read(
        'nora', {'op': 'grep', 'pattern': 'needle', 'path': 'world'})
    assert out['truncated'] is False


def test_live_read_team_log(monkeypatch):
    rows = [(1, 'ruth', 'did work', 'details')]
    seen = {}

    class _Conn:
        def execute(self, sql, params):
            seen['sql'] = sql
            seen['params'] = params
            return self

        def fetchall(self):
            return rows

    @contextlib.contextmanager
    def fake_db():
        yield _Conn()

    _patch_state(monkeypatch, _live_state())
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda state, aid: True)
    monkeypatch.setattr(serve, '_is_admin', lambda state, aid: False)
    monkeypatch.setattr(serve, '_direct_reports', lambda state, aid: ['ruth'])
    monkeypatch.setattr(serve, '_db', fake_db)

    out = serve.supervisor_live_read('nora', {'op': 'team_log', 'limit': 5})
    assert out['entries'][0]['action'] == 'did work'
    assert seen['params'][-1] == 5

    monkeypatch.setattr(serve, '_is_admin', lambda state, aid: True)
    out = serve.supervisor_live_read('theo', {'op': 'log', 'limit': 1000})
    assert out['entries'][0]['agent'] == 'ruth'

    monkeypatch.setattr(serve, '_is_admin', lambda state, aid: False)
    out = serve.supervisor_live_read('ruth', {'op': 'team_log'})
    assert 'supervise' in out['error']


def test_live_read_unauthorised_and_unknown_op(monkeypatch):
    _patch_state(monkeypatch, _live_state())
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda state, aid: False)
    assert 'only a director' in serve.supervisor_live_read('ruth', {})['error']
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda state, aid: True)
    assert 'unknown op' in serve.supervisor_live_read('nora', {'op': 'wat'})['error']


# ---------------------------------------------------------------------------
# supervisor delegation
# ---------------------------------------------------------------------------

def test_delegate_permission_and_team_errors(monkeypatch):
    state = _state()
    _patch_state(monkeypatch, state)
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda s, a: False)
    ok, out = serve.supervisor_delegate('ruth', 'a title', 'some instructions')
    assert not ok and 'delegate' in out['error']

    monkeypatch.setattr(serve, '_is_director_or_admin', lambda s, a: True)
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: True)
    monkeypatch.setattr(serve, 'supervisor_team_for', lambda s, a: None)
    ok, out = serve.supervisor_delegate('theo', 'a title', 'some instructions')
    assert not ok and 'name a team' in out['error']

    monkeypatch.setattr(serve, '_is_admin', lambda s, a: False)
    ok, out = serve.supervisor_delegate('ruth', 'a title', 'some instructions')
    assert not ok and 'do not supervise' in out['error']


def test_delegate_validation_errors(monkeypatch):
    state = _state()
    _patch_state(monkeypatch, state)
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda s, a: True)
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: False)

    ok, out = serve.supervisor_delegate('nora', 'short', 'too short')
    assert not ok and 'title and instructions' in out['error']

    ok, out = serve.supervisor_delegate(
        'nora', 'a valid title', 'long enough instructions', task_type='weird')
    assert not ok and 'taskType' in out['error']

    ok, out = serve.supervisor_delegate(
        'nora', 'a valid title', 'long enough instructions', budget_usd='nope')
    assert not ok and 'number' in out['error']

    ok, out = serve.supervisor_delegate(
        'nora', 'a valid title', 'long enough instructions', budget_usd=99)
    assert not ok and 'between' in out['error']


def test_delegate_success_counts_open_work(monkeypatch):
    state = _state(
        workQueue=[{'teamId': 'nora', 'title': '[DELEGATE] existing'}],
        tasks={'t1': {'teamId': 'nora', 'title': '[DELEGATE] old', 'status': 'working'}},
    )
    _patch_state(monkeypatch, state)
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda s, a: True)
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: False)
    monkeypatch.setattr(serve, '_direct_reports', lambda s, a: ['ruth'] if a == 'nora' else [])

    def sim_down(*a, **k):
        raise RuntimeError('sim unavailable')
    monkeypatch.setattr(sim, 'on_call_agent', sim_down)
    monkeypatch.setattr(sim, 'queue_work', lambda s, items: s['workQueue'].extend(items))

    ok, out = serve.supervisor_delegate(
        'nora', 'Investigate flaky tests', 'Trace the failing suite and report the cause')
    assert ok is True, out
    assert out['assignee'] == 'ruth'
    assert state['workQueue'][-1]['title'].startswith('[DELEGATE]')


def test_delegate_named_team_and_assignee_mismatch(monkeypatch):
    state = _state()
    _patch_state(monkeypatch, state)
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda s, a: True)
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: False)
    monkeypatch.setattr(sim, 'queue_work', lambda s, items: s['workQueue'].extend(items))

    ok, out = serve.supervisor_delegate(
        'nora', 'Investigate flaky tests', 'Trace the failing suite and report the cause',
        team='nora', assignee='ruth')
    assert ok is True, out

    ok, out = serve.supervisor_delegate(
        'nora', 'Investigate flaky tests', 'Trace the failing suite and report the cause',
        assignee='cora')
    assert not ok and 'on your team' in out['error']


def test_delegate_refuses_over_the_open_cap(monkeypatch):
    state = _state()
    state['tasks'] = {
        f't{i}': {'teamId': 'nora', 'title': '[DELEGATE] x', 'status': 'working'}
        for i in range(serve.DELEGATE_MAX_OPEN)
    }
    _patch_state(monkeypatch, state)
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda s, a: True)
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: False)
    ok, out = serve.supervisor_delegate(
        'nora', 'Investigate flaky tests', 'Trace the failing suite and report the cause')
    assert not ok and 'open delegations' in out['error']


# ---------------------------------------------------------------------------
# backlog directives
# ---------------------------------------------------------------------------

def test_backlog_directive_paths(monkeypatch):
    monkeypatch.setattr(serve, '_state_abort', lambda: None)
    monkeypatch.setattr(serve, '_state_begin', lambda: None)
    ok, out = serve.supervisor_backlog_directive('theo', 'nora', 'a title', 'a real reason')
    assert not ok and 'state unavailable' in out['error']

    state = _state()
    monkeypatch.setattr(serve, '_state_begin', lambda: state)
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: False)
    ok, out = serve.supervisor_backlog_directive('nora', 'nora', 'a title', 'a real reason')
    assert not ok and 'chief of staff' in out['error']
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: True)
    ok, out = serve.supervisor_backlog_directive('theo', 'ghost', 'a title', 'a real reason')
    assert not ok and 'real team' in out['error']

    ok, out = serve.supervisor_backlog_directive('theo', 'nora', 'short', 'why')
    assert not ok and 'reason' in out['error']

    ok, out = serve.supervisor_backlog_directive(
        'theo', 'nora', 'a valid title', 'a concrete reason here', task_type='nope')
    assert not ok and 'taskType' in out['error']

    monkeypatch.setattr(sim, 'file_issue', lambda *a, **k: None)
    ok, out = serve.supervisor_backlog_directive(
        'theo', 'nora', 'a valid title', 'a concrete reason here')
    assert not ok and 'could not file' in out['error']


def test_backlog_directive_success(monkeypatch):
    state = _state()
    state['backlogRequests'] = []
    state['agents']['nora'] = {'id': 'nora', 'mailbox': []}
    monkeypatch.setattr(serve, '_state_abort', lambda: None)
    monkeypatch.setattr(serve, '_state_begin', lambda: state)
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: True)
    _patch_state(monkeypatch, state)

    class _Sim:
        WORK_PRIORITY = {'high': 2}

        @staticmethod
        def file_issue(state, *args, **kwargs):
            state['backlogRequests'].append({'issueKey': 'TT-1'})
            return {'key': 'TT-1'}

        @staticmethod
        def _append_mailbox(agent, message):
            agent.setdefault('mailbox', []).append(message)
    monkeypatch.setattr(sim, 'file_issue', staticmethod(_Sim.file_issue))
    monkeypatch.setattr(sim, '_append_mailbox', staticmethod(_Sim._append_mailbox))
    monkeypatch.setattr(sim, 'WORK_PRIORITY', _Sim.WORK_PRIORITY)
    ok, out = serve.supervisor_backlog_directive(
        'theo', 'nora', 'a valid title', 'a concrete reason here')
    assert ok is True, out
    assert out['issueKey'] == 'TT-1'
    assert state['backlogRequests'][0]['priority'] == 2


