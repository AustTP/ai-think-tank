import io
import json
import sqlite3
import sys
import time
from contextlib import redirect_stdout

import cost_report


def _db(tmp_path):
    db = tmp_path / 't.db'
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE action_log (ts REAL, action TEXT, details TEXT)')
    return db, con


def test_summarize_skips_bad_and_unattributed(tmp_path):
    db, con = _db(tmp_path)
    now = time.time()
    con.executemany('INSERT INTO action_log VALUES (?,?,?)', [
        (now, 'chat', 'not json'),
        (now, 'chat', json.dumps({'cost': 'x'})),
        (now, 'chat', json.dumps({'cost': 0})),
        (now, 'chat', json.dumps({'cost': 0.5})),
        (now, 'task_completed', json.dumps({})),
        (now, 'task_completed', json.dumps({'taskId': 'done'})),
        (now, 'chat', json.dumps({'cost': 0.25, 'task_id': 'done', 'service': 'jev'})),
    ])
    con.commit(); con.close()
    r = cost_report.summarize(str(db), 0)
    assert r['days'] == 0
    assert r['totalCost'] == 0.75
    assert r['completedCards'] == 1
    assert r['cards'] == [{'taskId': 'done', 'title': 'jev', 'cost': 0.25, 'calls': 1}]


def test_main_prints_report(tmp_path, monkeypatch):
    db, con = _db(tmp_path)
    con.execute('INSERT INTO action_log VALUES (?,?,?)',
                (time.time(), 'task_completed', json.dumps({'taskId': 't1'})))
    con.execute('INSERT INTO action_log VALUES (?,?,?)',
                (time.time(), 'chat', json.dumps({'cost': 0.1, 'taskId': 't1', 'title': 'Thing'})))
    con.commit(); con.close()
    monkeypatch.setattr(sys, 'argv', ['cost_report.py', '--days', '1', '--db', str(db)])
    out = io.StringIO()
    with redirect_stdout(out):
        cost_report.main()
    text = out.getvalue()
    assert 'Total model/Jev cost: $0.1000' in text
    assert 'Average per completed card: $0.1000' in text
    assert '$0.1000' in text and 'Thing' in text
