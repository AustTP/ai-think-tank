import io
import json
import threading
import types

import integrations.mcp_client as m


def _spec(**kw):
    s = {'id': 's', 'name': 'S', 'description': '', 'transport': 'stdio', 'enabled': True,
         'request_timeout_s': 1, 'startup_timeout_s': 1, 'max_response_bytes': 100,
         'max_tools': 2, 'cwd': '.', 'inherit_env': False, 'env': {}, 'env_from': {},
         'headers': {}, 'auth': {'type': 'none'}, 'command': ['x'],
         'tools': {'allow': ['*'], 'read_only': [], 'prepare': [], 'blocked': []}}
    s.update(kw); return s


class Proc:
    def __init__(self, alive=True, lines=()):
        self.pid = 1
        self._alive = alive
        self.stdin = io.StringIO()
        self.stdout = iter(lines)
        self.stderr = iter(())
    def poll(self): return None if self._alive else 1
    def wait(self, timeout=None): return 0
    def terminate(self): pass
    def kill(self): pass


def test_stdio_start_request_notify(monkeypatch, tmp_path):
    s = m.StdioSession(_spec())
    monkeypatch.setattr(s, 'initialize', lambda: None)
    monkeypatch.setattr(m.subprocess, 'Popen', lambda *a, **k: Proc(lines=()))
    s.start()
    assert s.proc
    s.close()
    s = m.StdioSession(_spec())
    s.proc = Proc()
    # _send/notify
    s.notify('n', {})
    assert 'n' in s.proc.stdin.getvalue()
    # request via _send interception
    def send(msg):
        with s._pending_lock:
            entry = s._pending[msg['id']]
            entry['message'] = {'jsonrpc': '2.0', 'id': msg['id'], 'result': {'ok': 1}}
            entry['event'].set()
    monkeypatch.setattr(s, '_send', send)
    assert s.request('m') == {'ok': 1}
    # error dict/string
    def send_err(msg):
        with s._pending_lock:
            entry = s._pending[msg['id']]
            entry['message'] = {'id': msg['id'], 'error': {'message': 'bad'}}
            entry['event'].set()
    monkeypatch.setattr(s, '_send', send_err)
    try: s.request('m')
    except m.McpError: pass
    def send_err2(msg):
        with s._pending_lock:
            entry = s._pending[msg['id']]
            entry['message'] = {'id': msg['id'], 'error': 'bad'}
            entry['event'].set()
    monkeypatch.setattr(s, '_send', send_err2)
    try: s.request('m')
    except m.McpError: pass
    # timeout
    monkeypatch.setattr(s, '_send', lambda msg: None)
    try: s.request('m', timeout=0.001)
    except m.McpError: pass
    # error entry
    def send_entry(msg):
        with s._pending_lock:
            entry = s._pending[msg['id']]
            entry['error'] = m.McpError('entry')
            entry['event'].set()
    monkeypatch.setattr(s, '_send', send_entry)
    try: s.request('m')
    except m.McpError: pass
    # not alive
    s.proc = Proc(alive=False)
    try: s.request('m')
    except m.McpError: pass
    assert s.alive is False
    s.notify if False else None
    try: m.StdioSession._send(s, {})
    except m.McpError: pass


def test_stdio_read_loop_and_close(monkeypatch):
    s = m.StdioSession(_spec(max_response_bytes=50))
    s.proc = Proc(lines=[json.dumps({'id': 'x', 'result': 1}) + '\n',
                         'bad\n', '[]\n', json.dumps({'id': 2, 'method': 'm'}) + '\n',
                         'x' * 100 + '\n'])
    event = threading.Event()
    s._pending['x'] = {'event': event, 'message': None, 'error': None}
    s._send = lambda msg: None
    s._read_loop()
    assert event.is_set()
    # pending error on exit
    s._pending['y'] = {'event': threading.Event(), 'message': None, 'error': None}
    s.proc = Proc(lines=[])
    s._read_loop()
    assert s._pending == {}
    # close paths
    class P(Proc):
        def wait(self, timeout=None): raise RuntimeError('x')
    s.proc = P()
    monkeypatch.setattr(m.os, 'killpg', lambda *a: (_ for _ in ()).throw(OSError('x')))
    s.close()
    assert s.proc is None


def test_manager_and_http_methods(monkeypatch):
    m._SESSIONS.clear()
    monkeypatch.setattr(m, 'get_server', lambda i: _spec(enabled=False))
    try: m._get_session('s')
    except m.McpError: pass
    monkeypatch.setattr(m, 'get_server', lambda i: _spec())
    class Sess:
        def __init__(self, spec): self.spec = spec; self.alive = True; self.closed = 0
        def start(self): pass
        def close(self): self.closed += 1; self.alive = False
    monkeypatch.setattr(m, 'StdioSession', Sess)
    a = m._get_session('s'); assert a is m._get_session('s')
    b = m._get_session('s', refresh=True); assert b is not a and a.closed == 1
    m._SESSIONS['bad'] = types.SimpleNamespace(close=lambda: (_ for _ in ()).throw(RuntimeError('x')))
    m.close_all(); assert m._SESSIONS == {}
    # http methods
    h = m.HttpSession(_spec(transport='http', url='http://x'))
    calls = []
    monkeypatch.setattr(h, '_post', lambda payload, timeout: calls.append(payload) or {'result': 1})
    assert h.request('m') == 1
    h.notify('n')
    monkeypatch.setattr(h, '_post', lambda *a: None)
    try: h.request('m')
    except m.McpError: pass
    monkeypatch.setattr(h, '_post', lambda *a: {'error': {'message': 'x'}})
    try: h.request('m')
    except m.McpError: pass
    monkeypatch.setattr(h, '_post', lambda *a: {'error': 'x'})
    try: h.request('m')
    except m.McpError: pass
    h._started = False
    monkeypatch.setattr(h, 'initialize', lambda: None)
    h.start(); assert h._started is True
    h._session_id = 'sid'
    monkeypatch.setattr(m.urllib.request, 'urlopen', lambda *a, **k: types.SimpleNamespace(__enter__=lambda s: s, __exit__=lambda *a: None))
    h.close(); assert h._session_id is None
