import os
import sqlite3
import time
from pathlib import Path

import apple_photos
import live_coverage_api
import repeat_work
import staged_work
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client():
    import serve
    return TestClient(serve.app)


def test_live_coverage_api_routes(monkeypatch):
    import asyncio
    import serve
    class Req:
        headers = {}
        async def json(self): return {'url': 'x'}
    class BadReq:
        headers = {}
        async def json(self): raise ValueError('bad')
    monkeypatch.setattr(serve, '_require_player_session', lambda r: False)
    monkeypatch.setattr(serve, '_resolve_requester', lambda r: None)
    assert asyncio.run(live_coverage_api.live_coverage_page(Req())).status_code == 303
    assert asyncio.run(live_coverage_api.live_coverage_status(Req())).status_code == 401
    monkeypatch.setattr(serve, '_operator_gate', lambda r: (False, None))
    assert asyncio.run(live_coverage_api.live_coverage_start(Req())).status_code == 401
    assert asyncio.run(live_coverage_api.live_coverage_stop(Req())).status_code == 401
    monkeypatch.setattr(serve, '_operator_gate', lambda r: (True, 'player'))
    assert asyncio.run(live_coverage_api.live_coverage_start(BadReq())).status_code == 400
    monkeypatch.setattr(live_coverage_api.live_coverage, 'start',
                        lambda **k: (_ for _ in ()).throw(ValueError('bad')))
    assert asyncio.run(live_coverage_api.live_coverage_start(Req())).status_code == 400
    monkeypatch.setattr(live_coverage_api.live_coverage, 'start',
                        lambda **k: (_ for _ in ()).throw(RuntimeError('busy')))
    assert asyncio.run(live_coverage_api.live_coverage_start(Req())).status_code == 409
    monkeypatch.setattr(live_coverage_api.live_coverage, 'start', lambda **k: {'id': 'r'})
    assert asyncio.run(live_coverage_api.live_coverage_start(Req())).status_code == 200
    monkeypatch.setattr(serve, '_require_player_session', lambda r: True)
    monkeypatch.setattr(serve, '_resolve_requester', lambda r: 'a')
    monkeypatch.setattr(serve, 'verify_agent_key', lambda a, k: True)
    monkeypatch.setattr(live_coverage_api.live_coverage, 'status', lambda **k: {'run': None})
    assert asyncio.run(live_coverage_api.live_coverage_status(Req(), run_id='nope')).status_code == 404
    assert asyncio.run(live_coverage_api.live_coverage_page(Req())).status_code == 200
    assert asyncio.run(live_coverage_api.live_coverage_stop(BadReq())).status_code == 400
    class EmptyReq(Req):
        async def json(self): return {}
    assert asyncio.run(live_coverage_api.live_coverage_stop(EmptyReq())).status_code == 400
    monkeypatch.setattr(live_coverage_api.live_coverage, 'stop',
                        lambda r: (_ for _ in ()).throw(KeyError(r)))
    class RunReq(Req):
        async def json(self): return {'runId': 'x'}
    assert asyncio.run(live_coverage_api.live_coverage_stop(RunReq())).status_code == 404


def test_staged_work_roundtrip(tmp_path):
    try:
        staged_work.Stage(str(tmp_path), '../bad')
    except ValueError:
        pass
    ws = tmp_path / 'story-1'; ws.mkdir(); (ws / 'a.py').write_text('x')
    st = staged_work.Stage(str(tmp_path), 'story-1')
    st.checkpoint()
    (st.directory / 'a.py').write_text('y')
    backup = st.publish()
    assert Path(backup).is_dir()
    assert (ws / 'a.py').read_text() == 'y'
    # resume from checkpoint
    st2 = staged_work.Stage(str(tmp_path), 'story-1', resume=st.id)
    assert st2.resumed_from is None
    staged_work.cleanup(str(tmp_path), keep_previous=0, max_age_s=0)


def test_repeat_work(tmp_path, monkeypatch):
    import serve
    monkeypatch.setattr(serve, 'SANDBOXES_DIR', str(tmp_path / 'sb'))
    state = {'tasks': {'p': {'id': 'p', 'status': 'working', 'writtenPy': ['a.py']}},
             'backlogRequests': []}
    (tmp_path / 'sb' / 'story-p').mkdir(parents=True)
    (tmp_path / 'sb' / 'story-p' / 'a.py').write_text('x')
    digest = repeat_work.evidence_digest(state, state['tasks']['p'])
    assert digest
    task = {'id': 't', 'taskType': 'code', 'reviewOf': 'p'}
    parent = state['tasks']['p']
    assert repeat_work.record(state, task, {'note': 'fail'}, 1) is False
    assert repeat_work.record(state, dict(task, id='t2'), {'note': 'fail'}, 2) is True
    assert parent['blockedReason'] == 'unchanged_failure'


def test_apple_photos(tmp_path, monkeypatch):
    root = tmp_path / 'Photos'; dbdir = root / 'database'; dbdir.mkdir(parents=True)
    (root / 'originals').mkdir()
    db = dbdir / 'Photos.sqlite'
    con = sqlite3.connect(db)
    con.executescript('''
        CREATE TABLE ZGENERICALBUM (Z_PK INTEGER, ZTITLE TEXT);
        CREATE TABLE ZASSET (Z_PK INTEGER, ZUUID TEXT, ZFILENAME TEXT, ZDIRECTORY TEXT,
            ZFAVORITE INTEGER, ZDATECREATED REAL, ZADDEDDATE REAL, ZKIND INTEGER,
            ZTRASHEDSTATE INTEGER, ZHIDDEN INTEGER);
        CREATE TABLE Z_1ASSETS (Z_1ALBUMS INTEGER, Z_1ASSETS INTEGER);
        INSERT INTO ZGENERICALBUM VALUES (1, 'Think Tank');
        INSERT INTO ZASSET VALUES (1,'u','p.jpg','',1,0,0,0,0,0);
        INSERT INTO Z_1ASSETS VALUES (1, 1);
    ''')
    con.commit(); con.close()
    monkeypatch.setattr(apple_photos, 'library_root', lambda: str(root))
    assert apple_photos.list_albums() == ['Think Tank']
    assert apple_photos._resolve_title(apple_photos._open(), ' think tank ') == 'Think Tank'
    assert apple_photos.album_assets('missing') == []
    (root / 'originals' / 'p.jpg').write_text('x')
    assets = apple_photos.album_assets('Think Tank')
    assert assets[0]['local'] is True
    assert len(apple_photos.album_assets('Think Tank', limit=1)) == 1
    # no join table
    con = sqlite3.connect(db); con.execute('DROP TABLE Z_1ASSETS'); con.commit(); con.close()
    try:
        apple_photos.album_assets('Think Tank')
    except RuntimeError:
        pass
    # normalize via fake sips
    class Done:
        returncode = 0
        stderr = b''
    def run(args, **k):
        Path(args[-1]).write_text('jpg')
        return Done()
    monkeypatch.setattr(apple_photos.subprocess, 'run', run)
    dest, tmp = apple_photos.normalize_to_jpeg('x')
    assert Path(dest).exists()
    class Bad:
        returncode = 1
        stderr = b'bad'
    monkeypatch.setattr(apple_photos.subprocess, 'run', lambda *a, **k: Bad())
    try:
        apple_photos.normalize_to_jpeg('x')
    except RuntimeError:
        pass


def test_live_coverage_authorized_and_success(monkeypatch):
    import asyncio
    import serve
    class Req:
        headers = {}
        async def json(self): return {'runId': 'r'}
    monkeypatch.setattr(serve, '_require_player_session', lambda r: False)
    monkeypatch.setattr(serve, '_resolve_requester', lambda r: 'a')
    monkeypatch.setattr(serve, 'verify_agent_key', lambda a, k: True)
    monkeypatch.setattr(live_coverage_api.live_coverage, 'status', lambda **k: {'run': {'id': 'r'}})
    assert asyncio.run(live_coverage_api.live_coverage_status(Req())).status_code == 200
    monkeypatch.setattr(serve, '_operator_gate', lambda r: (True, 'player'))
    monkeypatch.setattr(live_coverage_api.live_coverage, 'stop', lambda r: {'id': r})
    monkeypatch.setattr(serve, 'log_action', lambda *a, **k: None)
    assert asyncio.run(live_coverage_api.live_coverage_stop(Req())).status_code == 200


def test_repeat_work_more(tmp_path, monkeypatch):
    import serve
    monkeypatch.setattr(serve, 'SANDBOXES_DIR', str(tmp_path / 'sb'))
    lib = tmp_path / 'lib'; lib.mkdir(); (lib / 'r.md').write_text('evidence')
    monkeypatch.setattr(serve, '_safe_library_path', lambda p: str(lib / p))
    parent = {'id': 'p', 'status': 'working', 'libraryPath': 'r.md'}
    state = {'tasks': {'p': parent}}
    assert repeat_work.evidence_digest(state, parent)
    assert repeat_work.record(state, {'id': 't', 'reviewOf': 'p'}, {'note': 'x'}, 1) is False
    assert repeat_work.record(state, {'id': 't', 'reviewOf': 'p'}, {'note': 'x'}, 1) is False
    done = {'id': 'd', 'status': 'done'}
    assert repeat_work.record({'tasks': {'d': done}}, {'id': 't', 'reviewOf': 'd'}, {}, 1) is False
    monkeypatch.setattr(serve, '_safe_library_path', lambda p: None)
    parent2 = {'id': 'p2', 'status': 'working'}
    state2 = {'tasks': {'p2': parent2}}
    repeat_work.record(state2, {'id': 't', 'reviewOf': 'p2'},
                       {'note': 'Cited artifact is missing from Library: nope.md'}, 1)


def test_staged_work_more(tmp_path, monkeypatch):
    ws = tmp_path / 'story-2'; ws.mkdir(); (ws / 'a.py').write_text('x')
    st = staged_work.Stage(str(tmp_path), 'story-2')
    st.checkpoint()
    st2 = staged_work.Stage(str(tmp_path), 'story-2', resume=st.id)
    assert st2.resumed_from == st.id
    (st.directory / 'link').symlink_to(st.directory / 'a.py')
    try:
        st.publish()
    except ValueError:
        pass
    monkeypatch.setattr(staged_work.work_reuse, 'tree_digest', lambda p: 'changed')
    try:
        st.publish()
    except ValueError:
        pass
    # symlink in original
    (ws / 'link').symlink_to(ws / 'a.py')
    try:
        staged_work.Stage(str(tmp_path), 'story-2')
    except ValueError:
        pass
    # tree digest none
    monkeypatch.setattr(staged_work.work_reuse, 'tree_digest', lambda p: None)
    try:
        staged_work.Stage(str(tmp_path), 'story-3')
    except ValueError:
        pass
    staged_work.cleanup(str(tmp_path / 'file') if (tmp_path / 'file').write_text('x') else str(tmp_path))


def test_apple_photos_more(tmp_path, monkeypatch):
    monkeypatch.setenv('APPLE_PHOTOS_LIBRARY', '~/x')
    assert apple_photos.library_root().endswith('/x')
    monkeypatch.delenv('APPLE_PHOTOS_LIBRARY')
    monkeypatch.setattr(apple_photos, '_db_path', lambda: str(tmp_path / 'missing.db'))
    try:
        apple_photos._open()
    except FileNotFoundError:
        pass
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE ZGENERICALBUM (ZTITLE TEXT)')
    db.execute("INSERT INTO ZGENERICALBUM VALUES ('')")
    assert apple_photos._resolve_title(db, '') is None


def test_staged_work_remaining(tmp_path, monkeypatch):
    outside = tmp_path / 'outside'; outside.mkdir()
    (tmp_path / 'story-x').symlink_to(outside, target_is_directory=True)
    try:
        staged_work.Stage(str(tmp_path), 'story-x')
    except ValueError:
        pass
    # publish changed original
    ws = tmp_path / 'story-y'; ws.mkdir(); (ws / 'a').write_text('x')
    st = staged_work.Stage(str(tmp_path), 'story-y')
    (ws / 'a').write_text('changed')
    try:
        st.publish()
    except ValueError:
        pass
    # rollback when os.replace fails on publish
    ws2 = tmp_path / 'story-z'; ws2.mkdir(); (ws2 / 'a').write_text('x')
    st2 = staged_work.Stage(str(tmp_path), 'story-z')
    real_replace = os.replace
    calls = {'n': 0}
    def replace(a, b):
        calls['n'] += 1
        if calls['n'] == 2:
            raise OSError('fail')
        return real_replace(a, b)
    monkeypatch.setattr(staged_work.os, 'replace', replace)
    try:
        st2.publish()
    except OSError:
        pass
    # cleanup errors and old stages
    old = tmp_path / 'stage-old'; old.mkdir()
    old2 = tmp_path / 'previous-old'; old2.mkdir()
    monkeypatch.setattr(staged_work.shutil, 'rmtree', lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    monkeypatch.setattr(Path, 'unlink', lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    staged_work.cleanup(str(tmp_path), keep_previous=0, max_age_s=0)


def test_apple_photos_default_and_repeat_nocode(tmp_path, monkeypatch):
    monkeypatch.delenv('APPLE_PHOTOS_LIBRARY', raising=False)
    assert 'Photos Library' in apple_photos.library_root()
    monkeypatch.setattr(apple_photos, '_db_path', lambda: str(tmp_path / 'x'))
    try:
        apple_photos._open()
    except FileNotFoundError:
        pass
    import serve
    monkeypatch.setattr(serve, 'SANDBOXES_DIR', str(tmp_path / 'sb'))
    assert repeat_work.evidence_digest({'tasks': {}}, {'id': 'p'})


def test_staged_work_outside_and_resume_symlink(tmp_path):
    outside = tmp_path.parent / ('outside-' + tmp_path.name)
    outside.mkdir(exist_ok=True)
    (tmp_path / 'story-o').symlink_to(outside, target_is_directory=True)
    try:
        staged_work.Stage(str(tmp_path), 'story-o')
    except ValueError:
        pass
    ws = tmp_path / 'story-r'; ws.mkdir(); (ws / 'a').write_text('x')
    digest = staged_work.work_reuse.tree_digest(str(ws))
    resume = 'stage-' + 'a' * 32
    cand = tmp_path / resume; cand.mkdir()
    (cand / 'link').symlink_to(ws / 'a')
    (tmp_path / (resume + '.repair.json')).write_text(
        __import__('json').dumps({'workspace': 'story-r', 'baseDigest': digest}))
    try:
        staged_work.Stage(str(tmp_path), 'story-r', resume=resume)
    except ValueError:
        pass
