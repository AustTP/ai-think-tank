import sqlite3

import integrations.safari_tabs as safari_tabs


def test_no_db(monkeypatch):
    monkeypatch.setattr(safari_tabs, '_db_path', lambda: None)
    assert safari_tabs.list_tabs() == []


def test_db_error(monkeypatch):
    monkeypatch.setattr(safari_tabs, '_db_path', lambda: '/nope')
    monkeypatch.setattr(safari_tabs.sqlite3, 'connect', lambda *a, **k: (_ for _ in ()).throw(sqlite3.Error('x')))
    assert safari_tabs.list_tabs() == []


def test_filters_scheme_and_x_hosts(tmp_path, monkeypatch):
    db = tmp_path / 'CloudTabs.db'
    con = sqlite3.connect(db)
    con.executescript('''
        CREATE TABLE cloud_tab_devices (device_uuid TEXT, device_name TEXT);
        CREATE TABLE cloud_tabs (device_uuid TEXT, title TEXT, url TEXT, is_pinned INTEGER, rowid INTEGER);
        INSERT INTO cloud_tab_devices VALUES ('u','iPhone');
        INSERT INTO cloud_tabs VALUES ('u','valid','https://x.com/a',1,1);
        INSERT INTO cloud_tabs VALUES ('u','bad','file:///etc/passwd',0,2);
        INSERT INTO cloud_tabs VALUES ('u','twitter','https://mobile.twitter.com/b',0,3);
    ''')
    con.commit(); con.close()
    monkeypatch.setattr(safari_tabs, '_db_path', lambda: str(db))
    tabs = safari_tabs.list_tabs()
    assert len(tabs) == 2
    assert tabs[0]['pinned'] is True
    assert [t['title'] for t in safari_tabs.x_tabs()] == ['valid', 'twitter']


def test_db_path_prefers_container(tmp_path, monkeypatch):
    db = tmp_path / 'CloudTabs.db'
    db.write_bytes(b'')
    monkeypatch.setattr(safari_tabs, 'CONTAINER_DB', str(db))
    monkeypatch.setattr(safari_tabs, 'LEGACY_DB', str(tmp_path / 'missing.db'))
    assert safari_tabs._db_path() == str(db)


def test_db_path_none(tmp_path, monkeypatch):
    monkeypatch.setattr(safari_tabs, 'CONTAINER_DB', str(tmp_path / 'a'))
    monkeypatch.setattr(safari_tabs, 'LEGACY_DB', str(tmp_path / 'b'))
    assert safari_tabs._db_path() is None
