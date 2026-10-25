import io
import json
import os
import sys
import types

import integrations.mcp_client as m


def _stdio(**kw):
    s = {'id': 's', 'name': 'S', 'description': '', 'transport': 'stdio',
         'enabled': True, 'request_timeout_s': 60, 'startup_timeout_s': 30,
         'max_response_bytes': 10000, 'max_tools': 500, 'cwd': '.',
         'inherit_env': False, 'env': {}, 'env_from': {}, 'headers': {},
         'auth': {'type': 'none'}, 'tools': {'allow': ['*'], 'read_only': [],
         'prepare': [], 'blocked': []}, 'command': ['x']}
    s.update(kw); return s


def _http(**kw):
    s = _stdio(transport='http', url='http://example.com', allow_private=False)
    s.pop('command', None); s.update(kw); return s


def test_registry_and_policy(tmp_path, monkeypatch):
    assert m._as_list(None) == [] and m._as_list('x') == ['x'] and m._as_list([1, ' ']) == ['1']
    assert m._clamp_int('x', 3, 1, 5) == 3
    assert m._normalize_tools({})['allow'] == ['*']
    assert m._normalize_tools({'allow': 'a', 'read_only': 'r'})['read_only'] == ['r']
    for raw in ('x', {'transport': 'x'}, {'transport': 'stdio'}, {'transport': 'stdio', 'command': 'x'},
                {'transport': 'stdio', 'command': []}, {'transport': 'http', 'url': 'ftp://x'},
                {'transport': 'http', 'url': ''}, {'transport': 'http', 'url': 'http://x', 'auth': 'x'},
                {'transport': 'stdio', 'command': ['x'], 'env': []}):
        try:
            m._normalize_server('s', raw)
        except m.McpError:
            pass
    assert m._normalize_server('s', {'command': ['x']})['transport'] == 'stdio'
    assert m._normalize_server('s', {'transport': 'http', 'url': 'http://x'})['transport'] == 'http'
    p = tmp_path / 'm.json'; monkeypatch.setattr(m, 'MCP_SERVERS_PATH', str(p))
    monkeypatch.setattr(m, '_SERVER_CACHE', None)
    assert m._load_registry() == {}
    p.write_text(json.dumps({'servers': {'s': {'command': ['x']}}}))
    assert m.get_server('s')['id'] == 's'
    assert '[id: s]' in m.servers_text()
    p.write_text(json.dumps({'servers': {}}))
    m.reload_servers()
    assert 'No MCP' in m.servers_text()
    for bad in ({'servers': []}, {'servers': {'../x': {}}}, 'x'):
        p.write_text(bad if isinstance(bad, str) else json.dumps(bad))
        try:
            m._load_registry()
        except m.McpError:
            pass
    try:
        m.get_server('')
    except m.McpError:
        pass
    monkeypatch.setattr(m, 'get_server', lambda i: _stdio(tools={'allow': ['a*'], 'read_only': ['ar'], 'prepare': ['ap'], 'blocked': ['ab']}))
    assert m.classify_tool('s', 'ab') == 'blocked'
    assert m.classify_tool('s', 'x') == 'blocked'
    assert m.classify_tool('s', 'ap') == 'prepare'
    assert m.classify_tool('s', 'ar') == 'read'
    assert m.classify_tool('s', 'a') == 'commit'
    try:
        m.classify_tool('s', '')
    except m.McpError:
        pass


def test_format_and_run(monkeypatch):
    assert m._truncate('x', 0) == 'x'
    assert 'truncated' in m._truncate('x' * 100, 3)
    assert m._format_result(1, 10) == '1'
    res = {'content': [1, {'type': 'text', 'text': 't'}, {'type': 'image', 'mimeType': 'i', 'data': 'd'},
                       {'type': 'audio', 'data': 'd'}, {'type': 'resource', 'resource': {'uri': 'u'}}, {'type': 'x'}],
           'structuredContent': {'a': 1}, 'isError': True}
    text = m._format_result(res, 10000)
    assert 'structuredContent' in text and 'image' in text
    assert m._format_result({'content': 'c'}, 10) == 'c'
    assert m._format_result({}, 100) == '(empty MCP result)'
    assert m._error_retryable([1]) is None
    assert m._error_retryable({'content': [{'type': 'text', 'text': '{"error":{"retryable":true}}'}]}) is True
    assert m._error_retryable({'content': [{'type': 'text', 'text': 'bad'}]}) is None
    # run_tool
    monkeypatch.setattr(m, 'get_server', lambda i: _stdio())
    monkeypatch.setattr(m, 'classify_tool', lambda s, t: 'read')
    class Sess:
        def call_tool(self, name, args): return {'content': [{'type': 'text', 'text': 'ok'}]}
    monkeypatch.setattr(m, '_get_session', lambda s, refresh=False: Sess())
    assert m.run_tool('s', 't')['ok'] is True
    assert 'arguments' in m.run_tool('s', 't', [])['error']
    monkeypatch.setattr(m, 'classify_tool', lambda s, t: 'blocked')
    assert m.run_tool('s', 't')['policy_denied'] is True
    monkeypatch.setattr(m, 'classify_tool', lambda s, t: 'commit')
    assert m.run_tool('s', 't', read_only=True)['policy_denied'] is True
    monkeypatch.setattr(m, 'classify_tool', lambda s, t: 'read')
    monkeypatch.setattr(m, '_get_session', lambda s, refresh=False: (_ for _ in ()).throw(m.McpError('x')))
    assert 'x' in m.run_tool('s', 't')['error']
    # list tools text
    class L:
        def list_tools(self): return [{'name': 'a', 'description': 'd', 'inputSchema': {'properties': {'x': 1}, 'required': ['x']}}, {}]
    monkeypatch.setattr(m, 'get_server', lambda i: _stdio())
    monkeypatch.setattr(m, '_get_session', lambda s, refresh=False: L())
    assert 'args: x' in m.list_tools_text('s')
    monkeypatch.setattr(m, 'get_server', lambda i: _stdio(enabled=False))
    assert 'disabled' in m.list_tools_text('s')


def test_http_session(monkeypatch):
    s = m.HttpSession(_http())
    assert s.alive is False
    monkeypatch.setattr(s, '_private_host', lambda: False)
    assert s._headers()['Content-Type'] == 'application/json'
    s._session_id = 'id'; assert s._headers()['Mcp-Session-Id'] == 'id'
    # auth errors
    s.spec['auth'] = {'type': 'bad'}
    try: s._credential()
    except m.McpError: pass
    s.spec['auth'] = {'type': 'header_bearer', 'credential': {'type': 'x'}}
    try: s._credential()
    except m.McpError: pass
    monkeypatch.setenv('KEY', '')
    s.spec['auth'] = {'type': 'header_bearer', 'credential': {'type': 'env', 'key': 'KEY'}}
    try: s._credential()
    except m.McpError: pass
    monkeypatch.setenv('KEY', 'v')
    assert s._credential() == 'v'
    s.spec['auth'] = {'type': 'header_bearer', 'credential': {'type': 'env', 'key': 'KEY'}}
    assert s._headers()['Authorization'] == 'Bearer v'
    s.spec['auth'] = {'type': 'header_key', 'value_format': 'x'}
    try: s._headers()
    except m.McpError: pass
    s.spec['auth'] = {'type': 'header_key', 'credential': {'type': 'env', 'key': 'KEY'}}
    assert s._headers()['X-API-Key'] == 'v'
    # post JSON
    s.spec['auth'] = {'type': 'none'}
    class R:
        headers = {'Content-Type': 'application/json'}
        def read(self, n): return b'{"result":1}'
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(m.urllib.request, 'build_opener', lambda h: types.SimpleNamespace(open=lambda req, timeout: R()))
    assert s._post({'id': '1'}, 1) == {'result': 1}
    # SSE
    class R2(R):
        headers = {'Content-Type': 'text/event-stream'}
        def read(self, n): return b'data: {"id":"1","result":2}\n\n'
    monkeypatch.setattr(m.urllib.request, 'build_opener', lambda h: types.SimpleNamespace(open=lambda req, timeout: R2()))
    assert s._post({'id': '1'}, 1)['result'] == 2
    # no response
    class R3(R):
        def read(self, n): return b''
    monkeypatch.setattr(m.urllib.request, 'build_opener', lambda h: types.SimpleNamespace(open=lambda req, timeout: R3()))
    assert s._post({'id': '1'}, 1) is None
    # errors
    import urllib.error
    monkeypatch.setattr(m.urllib.request, 'build_opener', lambda h: types.SimpleNamespace(open=lambda req, timeout: (_ for _ in ()).throw(urllib.error.URLError('x'))))
    try: s._post({'id': '1'}, 1)
    except m.McpError: pass
    class BadJSON(R):
        def read(self, n): return b'bad'
    monkeypatch.setattr(m.urllib.request, 'build_opener', lambda h: types.SimpleNamespace(open=lambda req, timeout: BadJSON()))
    try: s._post({'id': '1'}, 1)
    except m.McpError: pass
    # parse sse
    assert m.HttpSession._parse_sse(b'data: {"id":"1"}\n\n', '1') == {'id': '1'}
    try:
        m.HttpSession._parse_sse(b'data: bad\n\n', '1')
    except m.McpError:
        pass
    try: m.HttpSession._parse_sse(b'data: {"id":"2"}\n\n', '1')
    except m.McpError: pass
