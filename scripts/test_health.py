import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


spec = importlib.util.spec_from_file_location('repository_health', Path(__file__).resolve().parents[1] / 'health.py')
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


def test_missing_database_is_not_created(tmp_path, capsys):
    database = tmp_path / 'missing.db'
    assert health.main(['--database', str(database)]) == 1
    assert not database.exists()
    assert 'Cannot read' in capsys.readouterr().err


def test_existing_database_is_read_without_changes(tmp_path, capsys):
    database = tmp_path / 'state.db'
    state = {'tasks': {'a': {'title': 'Active', 'status': 'working'},
                       'b': {'title': 'Failed work', 'status': 'failed'}},
             'agents': {}, 'sim': {'tick': 2, 'lastTickEpochS': 1}}
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE kv_state (id INTEGER PRIMARY KEY, blob TEXT)')
        connection.execute('INSERT INTO kv_state VALUES (1, ?)', (json.dumps(state),))
    before = database.read_bytes()
    assert health.main(['--database', str(database)]) == 0
    assert database.read_bytes() == before
    output = capsys.readouterr().out
    assert 'Active' in output and '100% of in-flight' in output
    assert 'UTC' in output


def test_empty_checkpoint_and_summary(tmp_path, capsys):
    database = tmp_path / 'empty.db'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE kv_state (id INTEGER PRIMARY KEY, blob TEXT)')
    with pytest.raises(ValueError, match='no saved simulation state'):
        health.load_state(database)
    health.print_summary({})
    assert 'not recorded' in capsys.readouterr().out


def test_default_database_is_independent_of_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert health.DEFAULT_DATABASE == Path(__file__).resolve().parents[1] / 'think_tank.db'
