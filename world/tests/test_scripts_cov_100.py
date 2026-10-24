import json
import sqlite3
import sys
from pathlib import Path

import bot_law_watch
import check_file_growth
import check_models
import test_health as th


def test_bot_law_watch(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bot_law_watch, 'OUT_DIR', str(tmp_path))
    monkeypatch.setattr(bot_law_watch, 'SEEN_PATH', str(tmp_path / 'seen.json'))
    monkeypatch.setattr(bot_law_watch, 'LATEST_PATH', str(tmp_path / 'latest.md'))
    monkeypatch.setattr(bot_law_watch, 'QUERIES', ['q'])
    monkeypatch.setattr(bot_law_watch, '_get_json', lambda u: {'results': [
        {'title': 'Bot law', 'html_url': 'https://a', 'publication_date': 'd', 'abstract': 'x'}]})
    monkeypatch.setenv('TAVILY_API_KEY', 'k')
    monkeypatch.setattr(bot_law_watch.urllib.request, 'urlopen', lambda *a, **k: type('R', (), {
        '__enter__': lambda s: s, '__exit__': lambda s, *a: False,
        'read': lambda s: b'{"results":[{"title":"Robot rule","url":"https://b","content":"y"}]}'})())
    bot_law_watch.main()
    out = capsys.readouterr().out
    assert '"notify": true' in out
    # second run dedupes
    bot_law_watch.main()
    assert '"notify": false' in capsys.readouterr().out
    # failure paths
    monkeypatch.setattr(bot_law_watch, '_federal_register', lambda: (_ for _ in ()).throw(RuntimeError('x')))
    monkeypatch.setattr(bot_law_watch, '_tavily', lambda: (_ for _ in ()).throw(RuntimeError('y')))
    monkeypatch.setattr(bot_law_watch, '_load_seen', lambda: set())
    bot_law_watch.main()
    assert 'failed' in capsys.readouterr().err
    assert bot_law_watch._relevant({'title': 'bot law', 'summary': ''}) is True
    assert bot_law_watch._relevant({'title': 'x', 'summary': ''}) is False


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
    monkeypatch.setenv('TAVILY_API_KEY', '')
    assert bot_law_watch._tavily() == []
    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"x": 1}'
    monkeypatch.setattr(bot_law_watch.urllib.request, 'urlopen', lambda *a, **k: R())
    assert bot_law_watch._get_json('https://x') == {'x': 1}
    th.test_missing_database_is_not_created(tmp_path, capsys)
    th.test_existing_database_is_read_without_changes(tmp_path, capsys)
    th.test_empty_checkpoint_and_summary(tmp_path, capsys)
    th.test_default_database_is_independent_of_current_directory(tmp_path, monkeypatch)
