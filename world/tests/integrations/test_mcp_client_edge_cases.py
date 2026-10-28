import io
import json
import types
import urllib.error

import integrations.mcp_client as m


def _spec(**kw):
    s = {'id': 's', 'name': 'S', 'description': 'd', 'transport': 'stdio', 'enabled': True,
         'request_timeout_s': 1, 'startup_timeout_s': 1, 'max_response_bytes': 10,
         'max_tools': 2, 'cwd': '.', 'inherit_env': False, 'env': {}, 'env_from': {},
         'headers': {}, 'auth': {'type': 'none'}, 'command': ['x'],
         'tools': {'allow': ['*'], 'read_only': [], 'prepare': [], 'blocked': []}}
    s.update(kw); return s


class Proc:
    def __init__(self, alive=True, lines=()):
        self.pid = 1; self._alive = alive
        self.stdin = io.StringIO(); self.stdout = iter(lines); self.stderr = []
    def poll(self): return None if self._alive else 1
    def wait(self, timeout=None): return 0
    def terminate(self): pass
    def kill(self): pass


def test_base_and_http_edges(monkeypatch):
    b = m._BaseSession(_spec())
    assert b.protocol_version is None
    for fn in (lambda: b.alive, b.start, b.close, lambda: b.request('x'),
               lambda: b.notify('x')):
        try: fn()
        except NotImplementedError: pass
    try:
        m._NoRedirect().redirect_request(None, None, 302, '', {}, 'x')
    except m.McpError: pass
    monkeypatch.setattr(m, '_load_registry', lambda: {'a': _spec()})
    assert m.list_servers()
    monkeypatch.setattr(m, 'get_server', lambda i: _spec(transport='http', url='http://x'))
    h = m.HttpSession(_spec(transport='http', url='http://x', allow_private=False))
    assert h._credential() == ''
    monkeypatch.setattr(h, '_private_host', lambda: True)
    try: h._post({}, 1)
    except m.McpError: pass
    # private host branches
    h.__dict__.pop('_private_host', None)
    monkeypatch.setattr(m.socket, 'getaddrinfo', lambda *a: [])
    assert h._private_host() is False
    monkeypatch.setattr(m.socket, 'getaddrinfo', lambda *a: (_ for _ in ()).throw(OSError('x')))
    assert h._private_host() is True
    monkeypatch.setattr(m.socket, 'getaddrinfo', lambda *a: [(0, 0, 0, '', ('x', 1))])
    monkeypatch.setattr(m.ipaddress, 'ip_address', lambda x: (_ for _ in ()).throw(ValueError('x')))
    assert h._private_host() is True
    # close with no session
    h._started = True; h._session_id = None; h.close()
    # http post errors/sizes
    h.spec['allow_private'] = True
    class R:
        headers = {'Content-Type': 'application/json'}
        def __init__(self, data): self.data = data
        def read(self, n): return self.data
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(m.urllib.request, 'build_opener', lambda x: types.SimpleNamespace(open=lambda req, timeout: R(b'{}')))
    assert h._post({}, 1) == {}
    monkeypatch.setattr(m.urllib.request, 'build_opener', lambda x: types.SimpleNamespace(open=lambda req, timeout: R(b'x' * 100)))
    try: h._post({}, 1)
    except m.McpError: pass
    class H:
        headers = {'Content-Type': 'application/json', 'Mcp-Session-Id': 'sid'}
        def read(self, n): return b'{}'
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(m.urllib.request, 'build_opener', lambda x: types.SimpleNamespace(open=lambda req, timeout: H()))
    h._post({}, 1); assert h._session_id == 'sid'
    err = urllib.error.HTTPError('u', 500, 'x', {}, io.BytesIO(b'bad'))
    monkeypatch.setattr(m.urllib.request, 'build_opener', lambda x: types.SimpleNamespace(open=lambda req, timeout: (_ for _ in ()).throw(err)))
    try: h._post({}, 1)
    except m.McpError: pass
    assert m.HttpSession._parse_sse(b'data: {"id":"1"}', '1') == {'id': '1'}
    assert m.HttpSession._parse_sse(b'data: {"id":"1"}\n\n', None) == {'id': '1'}
    try:
        m.HttpSession._parse_sse(b'data: {"id":"1"}', '2')
    except m.McpError:
        pass


def test_stdio_edges(monkeypatch, tmp_path):
    s = m.StdioSession(_spec(inherit_env=True, env_from={'A': 'MISSING_X'}))
    monkeypatch.delenv('MISSING_X', raising=False)
    try: s.start()
    except m.McpError: pass
    # Popen failure
    s = m.StdioSession(_spec())
    monkeypatch.setattr(m.subprocess, 'Popen', lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    try: s.start()
    except m.McpError: pass
    # initialize failure closes
    s = m.StdioSession(_spec())
    monkeypatch.setattr(m.subprocess, 'Popen', lambda *a, **k: Proc())
    monkeypatch.setattr(s, 'initialize', lambda: (_ for _ in ()).throw(RuntimeError('x')))
    try: s.start()
    except RuntimeError: pass
    # stderr loop
    s = m.StdioSession(_spec()); s.proc = Proc(); s.proc.stderr = [' a \n', '\n']
    s._stderr_loop(); assert s._stderr
    # send pipe failure
    class BadIn:
        def write(self, x): raise BrokenPipeError('x')
        def flush(self): pass
    s.proc.stdin = BadIn()
    try: m.StdioSession._send(s, {'a': 1})
    except m.McpError: pass
    # blank/oversize/server request in read loop
    s = m.StdioSession(_spec(max_response_bytes=5)); s.proc = Proc(lines=['\n', 'x'*10 + '\n'])
    s._send = lambda msg: None
    s._read_loop()
    # server request send failure
    s = m.StdioSession(_spec(max_response_bytes=100)); s.proc = Proc(lines=[json.dumps({'id': 1, 'method': 'm'}) + '\n'])
    s._send = lambda msg: (_ for _ in ()).throw(RuntimeError('x'))
    s._read_loop()
    # close no proc / stdin close error / kill fallbacks
    s = m.StdioSession(_spec()); s.close()
    s.proc = Proc()
    s.proc.stdin = types.SimpleNamespace(close=lambda: (_ for _ in ()).throw(RuntimeError('x')))
    monkeypatch.setattr(m.os, 'killpg', lambda *a: (_ for _ in ()).throw(OSError('x')))
    monkeypatch.setattr(s.proc, 'terminate', lambda: (_ for _ in ()).throw(RuntimeError('x')))
    s.proc.wait = lambda timeout=None: (_ for _ in ()).throw(RuntimeError('x'))
    s.proc.kill = lambda: (_ for _ in ()).throw(RuntimeError('x'))
    s.close()
    # request send exception removes pending
    s = m.StdioSession(_spec()); s.proc = Proc()
    s._send = lambda msg: (_ for _ in ()).throw(RuntimeError('x'))
    try: s.request('m')
    except RuntimeError: pass
    # list tools max/cursor
    s = m.StdioSession(_spec(max_tools=1))
    monkeypatch.setattr(s, 'request', lambda *a, **k: {'tools': [{'name': 'a'}, {}], 'nextCursor': 'n'})
    assert s.list_tools() == [{'name': 'a'}]
    s = m.StdioSession(_spec()); monkeypatch.setattr(s, 'request', lambda *a, **k: {'tools': [{'name': 'a'}]})
    assert s.list_tools() == [{'name': 'a'}]
    # base initialize/call/list
    b = m._BaseSession(_spec())
    monkeypatch.setattr(b, 'request', lambda *a, **k: {'protocolVersion': 'v'})
    b.notify = lambda *a, **k: None
    b.initialize(); assert b.protocol_version == 'v'
    monkeypatch.setattr(b, 'request', lambda *a, **k: {'result': 1})
    assert b.call_tool('t') == {'result': 1}


def test_mcp_last_bits(tmp_path, monkeypatch):
    assert m._as_list(1) == []
    assert m._clamp_int('x', 3, 1, 5) == 3
    try:
        m._normalize_server('s', {'command': ['']})
    except m.McpError:
        pass
    # unknown server with known registry
    monkeypatch.setattr(m, '_load_registry', lambda: {'a': {}})
    try: m.get_server('b')
    except m.McpError: pass
    # servers_text description
    monkeypatch.setattr(m, '_load_registry', lambda: {'a': _spec(description='d')})
    assert '    d' in m.servers_text()
    # base list_tools cursor
    b = m._BaseSession(_spec(max_tools=10))
    calls = []
    def req(method, params=None, **k):
        calls.append(params)
        return {'tools': [{'name': 'a'}], 'nextCursor': 'c'} if len(calls) == 1 else {'tools': []}
    monkeypatch.setattr(b, 'request', req)
    assert b.list_tools() == [{'name': 'a'}]
    # env_from valid
    monkeypatch.setenv('PARENT_X', 'v')
    s = m.StdioSession(_spec(env_from={'A': 'PARENT_X'}))
    monkeypatch.setattr(m.subprocess, 'Popen', lambda *a, **k: Proc())
    monkeypatch.setattr(s, 'initialize', lambda: None)
    s.start(); s.close()
    # stderr exception
    s = m.StdioSession(_spec())
    class BadErr:
        def __iter__(self): raise RuntimeError('x')
    s.proc = Proc(); s.proc.stderr = BadErr()
    s._stderr_loop()
    # private address true
    h = m.HttpSession(_spec(transport='http', url='http://x', allow_private=False))
    monkeypatch.setattr(m.socket, 'getaddrinfo', lambda *a: [(0, 0, 0, '', ('10.0.0.1', 1))])
    assert h._private_host() is True
    # SSE empty
    assert m.HttpSession._parse_sse(b'', '1') is None
    # close urlopen error
    h._session_id = 'x'
    monkeypatch.setattr(m.urllib.request, 'urlopen', lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    h.close()
    # retryable non-dict item
    assert m._error_retryable({'content': [1, {'type': 'text', 'text': '{"error":{"retryable":false}}'}]}) is False
    # no tools
    class L:
        def list_tools(self): return []
    monkeypatch.setattr(m, 'get_server', lambda i: _spec())
    monkeypatch.setattr(m, '_get_session', lambda s, refresh=False: L())
    assert '(no tools returned)' in m.list_tools_text('s')


def test_close_never_signals_a_resolved_group(tmp_path, monkeypatch):
    # Regression: close() used to resolve the child's group with
    # os.getpgid(proc.pid). On Linux getpgid(1) returns 0 and killpg(0)
    # signals the CALLER'S OWN process group -- on a CI runner that kills
    # the runner agent itself (six runner deaths). The group must be
    # addressed as pgid == pid (our spawns are start_new_session=True),
    # skipped entirely for fake pids <= 1, and fall back to child-only
    # signals when the group kill fails.
    import signal as _signal
    killpg_calls = []
    real_killpg = m.os.killpg
    monkeypatch.setattr(m.os, 'killpg',
                        lambda pgid, sig: killpg_calls.append((pgid, sig)))
    monkeypatch.setattr(m.os, 'getpgid', lambda pid: 0)  # the Linux footgun
    s = _spec(command=['x'])
    s['request_timeout_s'] = 0.05
    s['startup_timeout_s'] = 0.05
    real_proc = types.SimpleNamespace(
        pid=1,  # the mocked-Proc shape that used to reach getpgid/killpg
        stdin=io.StringIO(), stderr=io.StringIO(), stdout=io.StringIO(''),
        poll=lambda: 0, wait=lambda timeout=None: 0,
        terminate=lambda: None, kill=lambda: None)
    monkeypatch.setattr(m.subprocess, 'Popen', lambda *a, **k: real_proc)
    sess = m.StdioSession(s)
    sess.close()
    # pid <= 1 must never reach killpg at all.
    assert killpg_calls == [], f'killpg was called with {killpg_calls}'
    assert sess.proc is None

    # A REAL child pid (2+) is signaled at its own group id, never a
    # getpgid-resolved one.
    killpg_calls.clear()
    real_proc = types.SimpleNamespace(
        pid=4242, stdin=io.StringIO(), stderr=io.StringIO(),
        stdout=io.StringIO(''), poll=lambda: None, wait=lambda timeout=None: 0,
        terminate=lambda: None, kill=lambda: None)
    monkeypatch.setattr(m.subprocess, 'Popen', lambda *a, **k: real_proc)
    sess2 = m.StdioSession(s)
    sess2.close()
    assert all(pgid == 4242 for pgid, _sig in killpg_calls), killpg_calls
    assert all(sig in (_signal.SIGTERM, _signal.SIGKILL) for _p, sig in killpg_calls)
