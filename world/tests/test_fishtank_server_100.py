import http.client
import json
import sqlite3
import threading

import fishtank_server


def _server():
    srv = fishtank_server.ThreadingHTTPServer(('127.0.0.1', 0), fishtank_server.Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def _get(port, path, method='GET'):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    c.request(method, path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body, dict(r.getheaders())


def test_fishtank_endpoints(tmp_path, monkeypatch):
    db = tmp_path / 't.db'
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE kv_state (id INTEGER PRIMARY KEY, blob TEXT)')
    con.execute('INSERT INTO kv_state VALUES (1, ?)', (json.dumps({
        'sim': {'tick': 7, 'agents': {
            'a': {'visible': True, 'x': '1', 'y': '2', 'dir': 'north'},
            'b': {'visible': False, 'x': 1, 'y': 1},
            'c': {'visible': True, 'inRoom': 'x', 'x': 1, 'y': 1},
            'd': {'visible': True, 'x': 'bad', 'y': 1},
            'e': {'visible': True, 'x': 3, 'y': 4, 'dir': 'bad'},
            'f': 'nope',
        }},
        'agents': {'a': {'villageId': 'main'}, 'e': {'villageId': 'other'}},
    }),))
    con.commit(); con.close()
    monkeypatch.setattr(fishtank_server, 'DB_PATH', db)
    assets = tmp_path / 'assets'; assets.mkdir()
    (assets / 'think_tank_background.png').write_bytes(b'png')
    monkeypatch.setattr(fishtank_server, 'ASSETS_DIR', assets)
    agents = tmp_path / 'agents'; (agents / 'a' / 'avatars').mkdir(parents=True)
    (agents / 'a' / 'avatars' / 'north.png').write_bytes(b'n')
    monkeypatch.setattr(fishtank_server, 'AGENTS_DIR', agents)
    srv, port = _server()
    try:
        status, body, headers = _get(port, '/')
        assert status == 200 and b'Think Tank Floor' in body and 'Content-Security-Policy' in headers
        assert _get(port, '/', 'HEAD')[1] == b''
        status, body, _ = _get(port, '/api/fishtank/state')
        payload = json.loads(body)
        assert status == 200 and payload['tick'] == 7
        assert [a['x'] for a in payload['agents']] == [1]
        assert _get(port, '/assets/think_tank_background.png')[0] == 200
        assert _get(port, '/assets/other.png')[0] == 404
        token = fishtank_server._token('a')
        assert _get(port, f'/avatar/{token}/north.png')[0] == 200
        assert _get(port, '/avatar/no/south.png')[0] == 404
        assert _get(port, '/favicon.ico')[0] == 204
        assert _get(port, '/nope')[0] == 404
    finally:
        srv.shutdown(); srv.server_close()


def test_load_snapshot_empty_and_bad(tmp_path, monkeypatch):
    db = tmp_path / 'empty.db'
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE kv_state (id INTEGER PRIMARY KEY, blob TEXT)')
    con.commit(); con.close()
    monkeypatch.setattr(fishtank_server, 'DB_PATH', db)
    assert fishtank_server._load_snapshot() == {'tick': 0, 'agents': []}
    con = sqlite3.connect(db)
    con.execute('INSERT INTO kv_state VALUES (1, ?)', ('not json',))
    con.commit(); con.close()
    try:
        fishtank_server._load_snapshot()
    except Exception:
        pass


def test_load_snapshot_direction_and_file_oserror(tmp_path, monkeypatch):
    db = tmp_path / 't.db'
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE kv_state (id INTEGER PRIMARY KEY, blob TEXT)')
    con.execute('INSERT INTO kv_state VALUES (1, ?)', (json.dumps({
        'sim': {'tick': 1, 'agents': {'g': {'visible': True, 'x': 3, 'y': 4, 'dir': 'bad'}}},
        'agents': {'g': {'villageId': 'main'}},
    }),))
    con.commit(); con.close()
    monkeypatch.setattr(fishtank_server, 'DB_PATH', db)
    snap = fishtank_server._load_snapshot()
    assert snap['agents'][0]['d'] == 'south'
    assets = tmp_path / 'assets'; assets.mkdir()
    monkeypatch.setattr(fishtank_server, 'ASSETS_DIR', assets)
    class H:
        command = 'GET'
        def send_response(self, s): pass
        def send_header(self, k, v): pass
        def end_headers(self): pass
        def _send(self, *a, **k): pass
        wfile = type('W', (), {'write': lambda self, b: None})()
    fishtank_server.Handler._file(H(), assets / 'missing.png')


def test_send_broken_pipe():
    class H:
        command = 'GET'
        def send_response(self, s): pass
        def send_header(self, k, v): pass
        def end_headers(self): pass
        class W:
            def write(self, b): raise BrokenPipeError()
        wfile = W()
    fishtank_server.Handler._send(H(), 200, 'text/plain', b'x')


def test_api_failure_and_main(tmp_path, monkeypatch, capsys):
    class H:
        command = 'GET'
        def __init__(self): self.path = '/api/fishtank/state'; self.sent = []
        def send_response(self, s): self.sent.append(s)
        def send_header(self, k, v): pass
        def end_headers(self): pass
        def _send(self, status, *a, **k): self.sent.append(status)
        wfile = type('W', (), {'write': lambda self, b: None})()
    monkeypatch.setattr(fishtank_server, '_load_snapshot', lambda: (_ for _ in ()).throw(RuntimeError('x')))
    h = H(); fishtank_server.Handler.do_GET(h)
    assert h.sent == [503]
    class Srv:
        daemon_threads = False
        def __init__(self, *a): pass
        def serve_forever(self): raise KeyboardInterrupt()
        def server_close(self): self.closed = True
    monkeypatch.setattr(fishtank_server, 'ThreadingHTTPServer', Srv)
    monkeypatch.setattr('sys.argv', ['fishtank_server.py', '--port', '0'])
    fishtank_server.main()
    assert 'Fishtank view' in capsys.readouterr().out
