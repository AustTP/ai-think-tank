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


# ---------------------------------------------------------------------------
# db records helpers
# ---------------------------------------------------------------------------

def test_db_records_cell_and_quote():
    long_text = 'x' * (serve.DB_RECORDS_MAX_CELL_CHARS + 5)
    assert serve._db_records_cell(long_text).endswith('[truncated]')
    assert serve._db_records_cell(b'abc') == '<3 bytes>'
    assert serve._db_records_cell(3) == 3
    assert serve._db_records_quote('a"b') == '"a""b"'


def test_db_records_table_and_where_validation(tmp_path):
    db = tmp_path / 'r.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE records (id INTEGER PRIMARY KEY, name TEXT)')
    conn.commit()
    with pytest.raises(ValueError):
        serve._db_records_table(conn, '')
    with pytest.raises(ValueError):
        serve._db_records_table(conn, 'agent_keys')
    with pytest.raises(ValueError):
        serve._db_records_table(conn, 'missing')
    assert serve._db_records_table(conn, 'records') == 'records'

    sql, params = serve._db_records_where(conn, 'records', {'name': 'a'})
    assert sql == '"name" = ?' and params == ['a']
    sql, params = serve._db_records_where(
        conn, 'records', [{'column': 'name', 'op': 'like', 'value': 'a%'}])
    assert 'LIKE' in sql and params == ['a%']
    with pytest.raises(ValueError):
        serve._db_records_where(conn, 'records', [1])
    with pytest.raises(ValueError):
        serve._db_records_where(conn, 'records', [{'bad': 1}])
    with pytest.raises(ValueError):
        serve._db_records_where(conn, 'records', 'name = x')
    with pytest.raises(ValueError):
        serve._db_records_where(conn, 'records', {'nope': 1})
    with pytest.raises(ValueError):
        serve._db_records_where(conn, 'records', [{'column': 'name', 'op': 'DROP'}])
    conn.close()


def _records_db(tmp_path, monkeypatch):
    db = tmp_path / 'records.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE records (id INTEGER PRIMARY KEY, name TEXT, n INTEGER)')
    conn.execute('CREATE TABLE agent_keys (id INTEGER PRIMARY KEY)')
    conn.execute("INSERT INTO records (name, n) VALUES ('a', 1)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(serve, 'DB_PATH', str(db))
    _patch_state(monkeypatch, _state())
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: True)


def test_db_records_full_surface(monkeypatch, tmp_path):
    _records_db(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: calls.append(a))

    listed = serve.supervisor_db_records('theo', {'op': 'tables'})
    assert [t['table'] for t in listed['tables']] == ['records']
    described = serve.supervisor_db_records(
        'theo', {'op': 'describe', 'table': 'records'})
    assert [c['name'] for c in described['columns']] == ['id', 'name', 'n']

    assert 'non-empty list' in serve.supervisor_db_records(
        'theo', {'op': 'select', 'table': 'records', 'columns': 'id'})['error']
    assert 'unknown column' in serve.supervisor_db_records(
        'theo', {'op': 'select', 'table': 'records', 'columns': ['bogus']})['error']
    assert 'unknown orderBy' in serve.supervisor_db_records(
        'theo', {'op': 'select', 'table': 'records',
                 'orderBy': 'bogus'})['error']
    out = serve.supervisor_db_records(
        'theo', {'op': 'select', 'table': 'records', 'limit': 'bad',
                 'orderBy': 'n', 'desc': True})
    assert out['limit'] == 50
    out = serve.supervisor_db_records(
        'theo', {'op': 'select', 'table': 'records', 'where': {'name': 'a'}})
    assert out['returned'] == 1

    assert 'non-empty values' in serve.supervisor_db_records(
        'theo', {'op': 'insert', 'table': 'records', 'values': []})['error']
    assert 'unknown column' in serve.supervisor_db_records(
        'theo', {'op': 'insert', 'table': 'records',
                 'values': {'bogus': 1}})['error']
    out = serve.supervisor_db_records(
        'theo', {'op': 'insert', 'table': 'records',
                 'values': {'name': 'b', 'n': 2}})
    assert out['inserted'] == 1

    assert 'non-empty where' in serve.supervisor_db_records(
        'theo', {'op': 'delete', 'table': 'records'})['error']
    assert 'non-empty values' in serve.supervisor_db_records(
        'theo', {'op': 'update', 'table': 'records',
                 'values': [], 'where': {'name': 'b'}})['error']
    assert 'unknown column' in serve.supervisor_db_records(
        'theo', {'op': 'update', 'table': 'records',
                 'values': {'bogus': 1}, 'where': {'name': 'b'}})['error']
    out = serve.supervisor_db_records(
        'theo', {'op': 'update', 'table': 'records',
                 'values': {'n': 9}, 'where': {'name': 'b'}})
    assert out['updated'] == 1

    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('audit down')))
    out = serve.supervisor_db_records(
        'theo', {'op': 'delete', 'table': 'records', 'where': {'name': 'b'}})
    assert out['deleted'] == 1

    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: None)
    assert 'unknown op' in serve.supervisor_db_records(
        'theo', {'op': 'explode', 'table': 'records'})['error']
    assert 'table is required' in serve.supervisor_db_records(
        'theo', {'op': 'select'})['error']
    monkeypatch.setattr(serve, '_db_records_table',
                        lambda conn, table: (_ for _ in ()).throw(RuntimeError('boom')))
    assert 'operation failed' in serve.supervisor_db_records(
        'theo', {'op': 'select', 'table': 'records'})['error']
    assert calls


def test_db_records_requires_admin(monkeypatch):
    _patch_state(monkeypatch, _state())
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: False)
    assert 'chief of staff' in serve.supervisor_db_records('nora', {})['error']


# ---------------------------------------------------------------------------
# state records
# ---------------------------------------------------------------------------

def _state_env(monkeypatch, state):
    monkeypatch.setattr(serve, '_state_begin', lambda: state)
    monkeypatch.setattr(serve, '_state_abort', lambda: None)
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: True)
    monkeypatch.setattr(serve, 'save_state_to_db', lambda s: None)
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: None)


def test_state_records_unavailable_and_collections(monkeypatch):
    _state_env(monkeypatch, None)
    assert 'state unavailable' in serve.supervisor_state_records('theo', {})['error']

    state = _state(tasks={'t1': {}}, teams=[{'id': 'nora'}])
    _state_env(monkeypatch, state)
    out = serve.supervisor_state_records('theo', {'op': 'collections'})
    assert {'collection': 'tasks', 'type': 'dict', 'count': 1} in out['collections']
    assert {'collection': 'teams', 'type': 'list', 'count': 1} in out['collections']


def test_state_records_path_errors(monkeypatch):
    state = _state(tasks={'t1': {'status': 'walking', 'nested': {'x': 1}}})
    _state_env(monkeypatch, state)
    assert 'allowed collection' in serve.supervisor_state_records(
        'theo', {'op': 'get', 'path': 'bogus/x'})['error']
    assert 'name a record' in serve.supervisor_state_records(
        'theo', {'op': 'get', 'path': 'tasks'})['error']
    assert 'path not found at' in serve.supervisor_state_records(
        'theo', {'op': 'get', 'path': 'tasks/nope/x'})['error']
    state['tasks'] = [{'id': 'a'}]
    assert 'list index not found at' in serve.supervisor_state_records(
        'theo', {'op': 'get', 'path': 'tasks/x/id'})['error']
    assert 'list index not found at' in serve.supervisor_state_records(
        'theo', {'op': 'get', 'path': 'tasks/9/id'})['error']
    state['tasks'] = {'a': 5}
    assert 'cannot traverse' in serve.supervisor_state_records(
        'theo', {'op': 'get', 'path': 'tasks/a/x/y'})['error']


def test_state_records_get_variants(monkeypatch):
    state = _state(tasks={'t1': {'status': 'walking'}, 'list': [{'x': 1}]})
    _state_env(monkeypatch, state)
    assert 'path not found' == serve.supervisor_state_records(
        'theo', {'op': 'get', 'path': 'tasks/t1/nope'})['error']
    state['tasks']['list'] = ['a', 'b']
    assert serve.supervisor_state_records(
        'theo', {'op': 'get', 'path': 'tasks/list/1'})['value'] == 'b'
    assert 'list index not found' == serve.supervisor_state_records(
        'theo', {'op': 'get', 'path': 'tasks/list/9'})['error']
    state['tasks']['scalar'] = 7
    assert 'cannot read' in serve.supervisor_state_records(
        'theo', {'op': 'get', 'path': 'tasks/scalar/x'})['error']

    state['tasks']['t1']['raw'] = {1, 2}
    out = serve.supervisor_state_records(
        'theo', {'op': 'get', 'path': 'tasks/t1/raw'})
    assert out['value'] == {1, 2}

    monkeypatch.setattr(serve, 'STATE_RECORDS_MAX_VALUE_CHARS', 2)
    out = serve.supervisor_state_records(
        'theo', {'op': 'get', 'path': 'tasks/t1/status'})
    assert out['truncated'] is True


def test_state_records_set_and_delete_variants(monkeypatch):
    state = _state(tasks={'t1': {'status': 'walking'}, 'list': ['a', 'b']})
    _state_env(monkeypatch, state)
    assert 'set requires a value' in serve.supervisor_state_records(
        'theo', {'op': 'set', 'path': 'tasks/t1/status'})['error']
    assert 'JSON-serializable' in serve.supervisor_state_records(
        'theo', {'op': 'set', 'path': 'tasks/t1/status',
                 'value': {1, 2}})['error']
    monkeypatch.setattr(serve, 'STATE_RECORDS_MAX_VALUE_CHARS', 2)
    assert 'too large' in serve.supervisor_state_records(
        'theo', {'op': 'set', 'path': 'tasks/t1/status', 'value': 'abcd'})['error']
    monkeypatch.setattr(serve, 'STATE_RECORDS_MAX_VALUE_CHARS', 200_000)

    out = serve.supervisor_state_records(
        'theo', {'op': 'set', 'path': 'tasks/t1/status', 'value': 'failed'})
    assert out['ok'] and state['tasks']['t1']['status'] == 'failed'
    out = serve.supervisor_state_records(
        'theo', {'op': 'set', 'path': 'tasks/list/0', 'value': 'z'})
    assert out['ok'] and state['tasks']['list'][0] == 'z'
    assert 'list index not found' in serve.supervisor_state_records(
        'theo', {'op': 'set', 'path': 'tasks/list/9', 'value': 'z'})['error']
    state['tasks']['scalar'] = 1
    assert 'cannot write' in serve.supervisor_state_records(
        'theo', {'op': 'set', 'path': 'tasks/scalar/x', 'value': 1})['error']

    assert 'path not found' == serve.supervisor_state_records(
        'theo', {'op': 'delete', 'path': 'tasks/t1/nope'})['error']
    state['tasks']['t1']['extra'] = 'gone'
    out = serve.supervisor_state_records(
        'theo', {'op': 'delete', 'path': 'tasks/t1/extra'})
    assert out['ok'] and out['removed'] == 'gone'
    out = serve.supervisor_state_records(
        'theo', {'op': 'delete', 'path': 'tasks/list/0'})
    assert out['ok'] and out['removed'] == 'z'
    assert 'list index not found' in serve.supervisor_state_records(
        'theo', {'op': 'delete', 'path': 'tasks/list/9'})['error']
    assert 'cannot delete' in serve.supervisor_state_records(
        'theo', {'op': 'delete', 'path': 'tasks/scalar/x'})['error']
    assert 'unknown op' in serve.supervisor_state_records(
        'theo', {'op': 'explode', 'path': 'tasks/t1/status'})['error']


def test_state_records_requires_admin(monkeypatch):
    _state_env(monkeypatch, _state())
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: False)
    assert 'chief of staff' in serve.supervisor_state_records('nora', {})['error']


# ---------------------------------------------------------------------------
# schedules
# ---------------------------------------------------------------------------

def _sched_env(monkeypatch, state):
    monkeypatch.setattr(serve, '_state_begin', lambda: state)
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: state)
    monkeypatch.setattr(serve, '_state_abort', lambda: None)
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda s, a: True)
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: False)
    monkeypatch.setattr(serve, 'save_state_to_db', lambda s: None)
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: None)
    monkeypatch.setattr(config, 'note_watchlist_topic', lambda r: None)
    monkeypatch.setattr(config, 'remove_watchlist_topic', lambda *a: None)
    monkeypatch.setattr(config, 'update_watchlist_topic', lambda *a: None)


def test_schedules_create_and_update_errors(monkeypatch):
    state = _state(researchTopics=[])
    _sched_env(monkeypatch, state)
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda s, a: False)
    assert 'only a director' in serve.supervisor_schedules('ruth', {'op': 'create'})['error']

    monkeypatch.setattr(serve, '_is_director_or_admin', lambda s, a: True)
    assert 'startUrl' in serve.supervisor_schedules(
        'nora', {'op': 'create', 'topic': 't', 'startUrl': 'ftp://x'})['error']
    assert 'cadenceMs' in serve.supervisor_schedules(
        'nora', {'op': 'create', 'topic': 't', 'startUrl': 'https://x',
                 'cadenceMs': 'bad'})['error']
    monkeypatch.setattr(sim, 'add_research_topic', lambda *a, **k: None)
    assert 'could not create' in serve.supervisor_schedules(
        'nora', {'op': 'create', 'topic': 't', 'startUrl': 'https://x'})['error']
    assert 'unknown schedule' in serve.supervisor_schedules(
        'nora', {'op': 'update', 'id': 'ghost'})['error']


def test_schedules_list_and_create_success(monkeypatch):
    record = {'id': 'r1', 'topic': 't', 'startUrl': 'https://x',
              'createdBy': 'nora'}
    state = _state(researchTopics=[record])
    _sched_env(monkeypatch, state)
    out = serve.supervisor_schedules('nora', {'op': 'list'})
    assert out['topics'][0]['id'] == 'r1'
    monkeypatch.setattr(sim, 'add_research_topic',
                        lambda *a, **k: {'id': 'r2', 'topic': 't2',
                                         'startUrl': 'https://y'})
    out = serve.supervisor_schedules(
        'nora', {'op': 'create', 'topic': 't2', 'startUrl': 'https://y'})
    assert out['ok'] and out['topic']['id'] == 'r2'
    assert 'unknown op' in serve.supervisor_schedules(
        'nora', {'op': 'wat', 'id': 'r1'})['error']


def test_schedules_permission_and_delete(monkeypatch):
    record = {'id': 'r1', 'topic': 't', 'startUrl': 'https://x',
              'createdBy': 'ben'}
    state = _state(researchTopics=[record])
    _sched_env(monkeypatch, state)
    assert 'only change schedules you created' in serve.supervisor_schedules(
        'nora', {'op': 'update', 'id': 'r1', 'topic': 'new'})['error']
    record['createdBy'] = 'nora'
    out = serve.supervisor_schedules('nora', {'op': 'delete', 'id': 'r1'})
    assert out['ok'] is True
    assert state['researchTopics'] == []


def test_schedules_update_success_and_validation(monkeypatch):
    record = {'id': 'r1', 'topic': 'old', 'startUrl': 'https://old',
              'createdBy': 'nora', 'cadenceMs': 900000}
    state = _state(researchTopics=[record])
    _sched_env(monkeypatch, state)

    assert 'absolute URL' in serve.supervisor_schedules(
        'nora', {'op': 'update', 'id': 'r1', 'startUrl': 'ftp://x'})['error']
    assert 'cadenceMs' in serve.supervisor_schedules(
        'nora', {'op': 'update', 'id': 'r1', 'cadenceMs': 'bad'})['error']

    out = serve.supervisor_schedules('nora', {
        'op': 'update', 'id': 'r1', 'topic': 'new topic',
        'startUrl': 'https://new', 'cadenceMs': 1200000, 'enabled': True})
    assert out['ok'] is True
    assert record['topic'] == 'new topic'
    assert record['startUrl'] == 'https://new'
    assert record['cadenceMs'] == 1200000
    assert record['enabled'] is True


# ---------------------------------------------------------------------------
# budget grants
# ---------------------------------------------------------------------------

def test_grant_budget_error_paths(monkeypatch):
    state = _state(tasks={})
    _patch_state(monkeypatch, state)
    monkeypatch.setattr(serve, '_is_director', lambda s, a: True)
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: False)

    ok, out = serve.supervisor_grant_budget('', 't', 1.0, 'a reason long enough')
    assert not ok and 'unknown supervisor' in out['error']
    ok, out = serve.supervisor_grant_budget('nora', 'missing', 1.0, 'a reason long enough')
    assert not ok and 'unknown card' in out['error']

    monkeypatch.setattr(serve, 'supervisor_team_for', lambda s, a: None)
    state['tasks']['t'] = {'id': 't', 'status': 'working', 'teamId': 'nora',
                           'budgetUsd': 0.1, 'ownSpendUsd': 0.0}
    ok, out = serve.supervisor_grant_budget('nora', 't', 1.0, 'a reason long enough')
    assert not ok and 'do not supervise' in out['error']

    monkeypatch.setattr(serve, 'supervisor_team_for',
                        lambda s, a: {'id': 'nora', 'members': ['ruth']})
    ok, out = serve.supervisor_grant_budget('nora', 't', 'lots', 'a reason long enough')
    assert not ok and 'number' in out['error']
    ok, out = serve.supervisor_grant_budget('nora', 't', 99, 'a reason long enough')
    assert not ok and 'between' in out['error']
    ok, out = serve.supervisor_grant_budget('nora', 't', 1.0, 'short')
    assert not ok and 'reason' in out['error']
    ok, out = serve.supervisor_grant_budget('nora', 't', 0.05, 'a reason long enough')
    assert not ok and 'exceed the current' in out['error']

    monkeypatch.setattr(serve, '_is_director', lambda s, a: False)
    ok, out = serve.supervisor_grant_budget('nora', 't', 1.0, 'a reason long enough')
    assert not ok and 'supervisor' in out['error']
    monkeypatch.setattr(serve, '_is_director', lambda s, a: True)

    state['tasks']['other'] = {'id': 'other', 'status': 'working',
                               'teamId': 'ben', 'assignedTo': 'cora',
                               'budgetUsd': 0.1, 'ownSpendUsd': 0.0}
    ok, out = serve.supervisor_grant_budget('nora', 'other', 1.0,
                                            'a reason long enough')
    assert not ok and 'another team' in out['error']

    state['tasks']['t']['ownSpendUsd'] = 0.8
    ok, out = serve.supervisor_grant_budget('nora', 't', 0.5, 'a reason long enough')
    assert not ok and 'already spent' in out['error']

    state['tasks']['t']['status'] = 'done'
    ok, out = serve.supervisor_grant_budget('nora', 't', 0.9, 'a reason long enough')
    assert not ok and 'already closed' in out['error']

    state['tasks']['t'].update({'status': 'failed', 'failNote': 'lint broke',
                                'ownSpendUsd': 0.0})
    ok, out = serve.supervisor_grant_budget('nora', 't', 0.9, 'a reason long enough')
    assert not ok and 'other than budget' in out['error']


def test_grant_budget_reopens_budget_failure(monkeypatch):
    state = _state(tasks={'t': {
        'id': 't', 'title': 'Card', 'status': 'failed', 'teamId': 'nora',
        'assignedTo': 'ruth', 'budgetUsd': 0.1, 'ownSpendUsd': 0.1,
        'failNote': 'budget exhausted'}})
    _patch_state(monkeypatch, state)
    monkeypatch.setattr(serve, '_is_director', lambda s, a: True)
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: False)
    monkeypatch.setattr(serve, 'supervisor_team_for',
                        lambda s, a: {'id': 'nora', 'members': ['ruth']})
    monkeypatch.setattr(sim, 'queue_work',
                        lambda s, items: s['workQueue'].extend(items))
    ok, out = serve.supervisor_grant_budget('nora', 't', 0.9, 'a reason long enough')
    assert ok is True
    assert out['reopened'] is True
    assert state['workQueue'][0]['budgetUsd'] == 0.9


# ---------------------------------------------------------------------------
# senior-most director
# ---------------------------------------------------------------------------

def test_senior_most_director_id():
    assert serve._senior_most_director_id(None) is None
    assert serve._senior_most_director_id({'agentRoster': [
        {'id': 'theo', 'isDirector': True, 'isAdmin': True},
        {'id': 'ben', 'isDirector': True, 'director': 'theo'},
        {'id': 'nora', 'isDirector': True},
    ]}) == 'nora'
    assert serve._senior_most_director_id({'agentRoster': [
        {'id': 'ben', 'isDirector': False}]}) is None


def test_live_read_git_status_diff_and_log(monkeypatch, tmp_path):
    import subprocess
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
    (root / 'a.txt').write_text('one\n')
    subprocess.run(['git', 'add', 'a.txt'], cwd=root, check=True)
    subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.com',
                    'commit', '-qm', 'first'], cwd=root, check=True)
    (root / 'a.txt').write_text('two\n')
    (root / 'b.txt').write_text('untracked\n')
    monkeypatch.setattr(serve, 'THINK_TANK_DIR', str(root))
    _patch_state(monkeypatch, _live_state())
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda state, aid: True)

    status = serve.supervisor_live_read('nora', {'op': 'git_status'})
    assert any('a.txt' in line for line in status['entries'])
    assert any('b.txt' in line for line in status['entries'])
    diff = serve.supervisor_live_read('nora', {'op': 'git_diff', 'path': 'a.txt'})
    assert '+two' in diff['text']
    log = serve.supervisor_live_read('nora', {'op': 'git_log', 'limit': 5})
    assert 'first' in log['text']
    assert 'path' in serve.supervisor_live_read(
        'nora', {'op': 'git_diff'})['error']


def test_live_task_trace_scoping_and_admin(monkeypatch):
    state = _state()
    state['tasks'] = {'t1': {'id': 't1', 'title': 'card', 'teamId': 'nora',
                             'assignedTo': 'ruth', 'status': 'failed',
                             'acceptanceCriteria': 'must work'}}
    state['workQueue'] = [{'id': 'q1', 'reviewOf': 't1'}]
    _patch_state(monkeypatch, state)
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda s, a: True)
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: a == 'theo')
    monkeypatch.setattr(serve, '_direct_reports', lambda s, a: ['ruth'] if a == 'nora' else [])

    rows = [(1, 'ruth', 'task_failed', '{"taskId":"t1"}')]

    class _Conn:
        def execute(self, sql, params):
            assert 'action_log' in sql
            return self
        def fetchall(self):
            return rows

    @contextlib.contextmanager
    def fake_db():
        yield _Conn()

    monkeypatch.setattr(serve, '_db', fake_db)
    out = serve.supervisor_live_read('nora', {'op': 'task_trace', 'taskId': 't1'})
    assert out['task']['title'] == 'card'
    assert out['children'] == []
    assert out['queued'] == ['q1']
    assert out['actionLog'][0]['action'] == 'task_failed'
    out = serve.supervisor_live_read('ben', {'op': 'task_trace', 'taskId': 't1'})
    assert 'outside your team' in out['error']
    out = serve.supervisor_live_read('theo', {'op': 'task_trace', 'taskId': 't1'})
    assert out['task']['acceptanceCriteria'] == 'must work'


def test_live_find_artifact_files_and_archive(monkeypatch, tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    (library / 'brief.md').write_text('orbital briefing\n')
    (tmp_path / 'secret.txt').write_text('orbital\n')
    monkeypatch.setattr(serve, 'THINK_TANK_DIR', str(tmp_path))
    _patch_state(monkeypatch, _live_state())
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda state, aid: True)
    monkeypatch.setattr(serve, '_is_admin', lambda state, aid: aid == 'theo')

    class _Conn:
        def execute(self, sql, params=None):
            assert 'task_archive' in sql
            return self
        def __iter__(self):
            return iter([('task-1', '{"title":"Orbital review","status":"cancelled"}')])

    @contextlib.contextmanager
    def fake_db():
        yield _Conn()

    monkeypatch.setattr(serve, '_db', fake_db)
    out = serve.supervisor_live_read('nora', {'op': 'find_artifact', 'query': 'orbital'})
    assert 'only the chief' in out['error']
    out = serve.supervisor_live_read('theo', {'op': 'find_artifact', 'query': 'orbital'})
    assert out['files'][0]['path'] == 'library/brief.md'
    assert out['archivedTasks'][0]['title'] == 'Orbital review'


def test_live_git_error_and_bad_path(monkeypatch, tmp_path):
    (tmp_path / 'a.txt').write_text('x\n')
    monkeypatch.setattr(serve, 'THINK_TANK_DIR', str(tmp_path))
    _patch_state(monkeypatch, _live_state())
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda state, aid: True)
    real_git = serve._run_live_git
    monkeypatch.setattr(serve, '_run_live_git', lambda args: {'error': 'boom'})
    assert serve.supervisor_live_read('nora', {'op': 'git_status'})['error'] == 'boom'
    assert serve.supervisor_live_read('nora', {'op': 'git_diff', 'path': 'a.txt'})['error'] == 'boom'
    assert serve.supervisor_live_read('nora', {'op': 'git_log'})['error'] == 'boom'
    monkeypatch.setattr(serve, '_safe_live_path',
                        lambda path: (_ for _ in ()).throw(ValueError('outside')))
    assert 'outside' in serve.supervisor_live_read('nora', {'op': 'git_diff', 'path': 'a.txt'})['error']

    class Bad:
        returncode = 1
        stderr = 'nope'
        stdout = ''
    monkeypatch.setattr(serve, '_run_live_git', real_git)
    monkeypatch.setattr('subprocess.run', lambda *a, **k: Bad())
    assert serve._run_live_git(['status'])['error'] == 'nope'
    monkeypatch.setattr('subprocess.run',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('missing')))
    assert 'unavailable' in serve._run_live_git(['status'])['error']


def test_live_task_trace_validation_and_instructions(monkeypatch):
    state = _state()
    state['tasks'] = {'t1': {'id': 't1', 'title': 'card', 'teamId': 'nora',
                             'instructions': 'do it'}}
    _patch_state(monkeypatch, state)
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda s, a: True)
    monkeypatch.setattr(serve, '_is_admin', lambda s, a: True)
    monkeypatch.setattr(serve, '_direct_reports', lambda s, a: [])
    assert 'required' in serve.supervisor_live_read('theo', {'op': 'task_trace'})['error']
    assert 'unknown task' in serve.supervisor_live_read('theo', {'op': 'task_trace', 'taskId': 'x'})['error']
    assert serve.supervisor_live_read('theo', {'op': 'task_trace', 'taskId': 't1'})['task']['instructions'] == 'do it'


def test_live_find_artifact_limits_and_errors(monkeypatch, tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    for i in range(26):
        (library / f'orbital-{i}.md').write_text('x\n')
    (library / 'large.md').write_text('orbital\n')
    monkeypatch.setattr(serve, 'THINK_TANK_DIR', str(tmp_path))
    _patch_state(monkeypatch, _live_state())
    monkeypatch.setattr(serve, '_is_director_or_admin', lambda state, aid: True)
    monkeypatch.setattr(serve, '_is_admin', lambda state, aid: True)
    monkeypatch.setattr(serve, 'LIVE_READ_MAX_BYTES', 5)

    class _Conn:
        def execute(self, sql, params=None):
            return self
        def __iter__(self):
            rows = [('bad', 'orbital {not json')]
            rows += [(f't{i}', '{"title":"orbital","status":"done"}') for i in range(20)]
            return iter(rows)

    @contextlib.contextmanager
    def fake_db():
        yield _Conn()

    monkeypatch.setattr(serve, '_db', fake_db)
    assert '2-120' in serve.supervisor_live_read('theo', {'op': 'find_artifact', 'query': 'x'})['error']
    out = serve.supervisor_live_read('theo', {'op': 'find_artifact', 'query': 'orbital'})
    assert len(out['files']) == 25
    assert len(out['archivedTasks']) == 15
    assert out['archivedTasks'][0]['title'] is None
