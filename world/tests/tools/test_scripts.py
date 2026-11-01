import json
import sqlite3
import sys
from pathlib import Path

import check_file_growth
import check_models
import test_health as th


def test_check_file_growth(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'root'; root.mkdir()
    (root / 'a.py').write_text('x\n')
    (root / 'b.py').write_text('y\n')
    monkeypatch.setattr(check_file_growth, 'ROOT', root)
    monkeypatch.setattr(check_file_growth, 'BUDGETS', tmp_path / 'budgets.json')
    (tmp_path / 'budgets.json').write_text(json.dumps({'a.py': 0, 'b.py': 1, 'missing.py': 1}))
    assert check_file_growth.main() == 1
    (tmp_path / 'budgets.json').write_text(json.dumps({'a.py': 1, 'b.py': 2}))
    assert check_file_growth.main() == 0
    assert 'OVER BUDGET' in capsys.readouterr().out
    assert check_file_growth.line_count(root / 'a.py') == 1


def test_check_models(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['check_models.py'])
    monkeypatch.setattr(check_models, 'model_config', type('M', (), {
        'load_bands': staticmethod(lambda p: {}),
        'validate': staticmethod(lambda c, d: ([], [])),
        'table': staticmethod(lambda rows: 'table')}))
    assert check_models.main() == 0
    assert 'no selected tiers' in capsys.readouterr().out
    monkeypatch.setattr(check_models.model_config, 'validate', staticmethod(lambda c, d: (['bad'], [{'x': 1}])))
    assert check_models.main() == 1
    assert 'problems' in capsys.readouterr().out


def test_health_script(tmp_path, capsys):
    db = tmp_path / 'missing.db'
    assert th.health.main(['--database', str(db)]) == 1
    db2 = tmp_path / 's.db'
    state = {'tasks': {'a': {'title': 'Active', 'status': 'working'}},
             'agents': {}, 'sim': {'tick': 2, 'lastTickEpochS': 1}}
    with sqlite3.connect(db2) as c:
        c.execute('CREATE TABLE kv_state (id INTEGER PRIMARY KEY, blob TEXT)')
        c.execute('INSERT INTO kv_state VALUES (1, ?)', (json.dumps(state),))
    assert th.health.main(['--database', str(db2)]) == 0
    empty = tmp_path / 'e.db'
    with sqlite3.connect(empty) as c:
        c.execute('CREATE TABLE kv_state (id INTEGER PRIMARY KEY, blob TEXT)')
    try:
        th.health.load_state(empty)
    except ValueError:
        pass
    th.health.print_summary({})
    assert 'not recorded' in capsys.readouterr().out


def test_scripts_remaining(tmp_path, monkeypatch, capsys):
    th.test_missing_database_is_not_created(tmp_path, capsys)
    th.test_existing_database_is_read_without_changes(tmp_path, capsys)
    th.test_empty_checkpoint_and_summary(tmp_path, capsys)
    th.test_default_database_is_independent_of_current_directory(tmp_path, monkeypatch)
