"""Generic MCP client: registry, stdio protocol, and per-tool policy."""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import integrations.mcp_client as mcp_client


FAKE_SERVER = r'''
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [
    {"name": "echo", "description": "Echo text.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "open", "description": "Open a session.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "mutate", "description": "Change state.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "blocked", "description": "Never allowed.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fail", "description": "Fails with a structured error.",
     "inputSchema": {"type": "object", "properties": {}}},
]

def send(message):
    print(json.dumps(message), flush=True)

for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"},
        }})
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        if name == "echo":
            result = {"content": [{"type": "text", "text": (params.get("arguments") or {}).get("text", "")}]}
        elif name == "open":
            result = {"content": [{"type": "text", "text": "opened"}]}
        elif name == "mutate":
            result = {"content": [{"type": "text", "text": "mutated"}]}
        elif name == "blocked":
            result = {"content": [{"type": "text", "text": "should not run"}], "isError": True}
        elif name == "fail":
            result = {"content": [{"type": "text", "text": json.dumps(
                {"error": {"retryable": False, "message": "no target"}})}],
                "isError": True}
        else:
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "unknown tool"}})
            continue
        send({"jsonrpc": "2.0", "id": request_id, "result": result})
    elif request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "unknown method"}})
'''


@pytest.fixture
def fake_server(tmp_path, monkeypatch):
    script = tmp_path / 'fake_mcp.py'
    script.write_text(FAKE_SERVER, encoding='utf-8')
    config = {
        'servers': {
            'fake': {
                'name': 'Fake MCP',
                'description': 'Test server.',
                'transport': 'stdio',
                'command': [sys.executable, str(script)],
                'enabled': True,
                'tools': {
                    'allow': ['*'],
                    'read_only': ['echo'],
                    'prepare': ['open'],
                    'blocked': ['blocked'],
                },
            },
        },
    }
    path = tmp_path / 'mcp_servers.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(mcp_client, 'MCP_SERVERS_PATH', str(path))
    mcp_client.reload_servers()
    mcp_client.close_all()
    yield
    mcp_client.close_all()


def test_registry_lists_server_without_starting_it(fake_server):
    text = mcp_client.servers_text()
    assert 'Fake MCP' in text
    assert 'enabled, stdio' in text


def test_tool_classification(fake_server):
    assert mcp_client.classify_tool('fake', 'echo') == 'read'
    assert mcp_client.classify_tool('fake', 'open') == 'prepare'
    assert mcp_client.classify_tool('fake', 'mutate') == 'commit'
    assert mcp_client.classify_tool('fake', 'blocked') == 'blocked'
    assert mcp_client.classify_tool('fake', 'not_listed') == 'commit'


def test_list_tools_starts_session_and_shows_tiers(fake_server):
    text = mcp_client.list_tools_text('fake')
    assert 'echo [read]' in text
    assert 'open [prepare]' in text
    assert 'mutate [commit]' in text
    assert 'args: text' in text


def test_read_tool_runs_read_only_tool(fake_server):
    out = mcp_client.run_tool('fake', 'echo', {'text': 'hello'}, read_only=True)
    assert out['ok'] is True
    assert out['tier'] == 'read'
    assert 'hello' in out['text']


def test_read_accepts_prepare_tool(fake_server):
    out = mcp_client.run_tool('fake', 'open', {}, read_only=True)
    assert out['ok'] is True
    assert 'opened' in out['text']


def test_read_refuses_commit_tool(fake_server):
    out = mcp_client.run_tool('fake', 'mutate', {}, read_only=True)
    assert out['ok'] is False
    assert out['policy_denied'] is True
    assert 'commit-tier' in out['error']


def test_call_runs_commit_tool(fake_server):
    out = mcp_client.run_tool('fake', 'mutate', {})
    assert out['ok'] is True
    assert out['tier'] == 'commit'
    assert 'mutated' in out['text']


def test_blocked_tool_never_runs(fake_server):
    out = mcp_client.run_tool('fake', 'blocked', {})
    assert out['policy_denied'] is True
    assert 'should not run' not in out.get('text', '')


def test_structured_error_marks_retryable_false(fake_server):
    out = mcp_client.run_tool('fake', 'fail', {})
    assert out['ok'] is False
    assert out['retryable'] is False
    assert 'no target' in out['text']


def test_unknown_server_is_an_error(fake_server):
    with pytest.raises(mcp_client.McpError):
        mcp_client.get_server('missing')


def test_registry_is_outside_the_static_served_tree():
    assert not mcp_client.MCP_SERVERS_PATH.startswith(mcp_client.WORLD_DIR + os.sep)


def test_shipped_rea_entry_and_tiers():
    servers = mcp_client.list_servers()
    assert 'rea' in servers
    rea = servers['rea']
    assert rea['transport'] == 'stdio'
    assert rea['command'][:2] == ['npx', '-y']
    assert mcp_client.classify_tool('rea', 'analyze_function') == 'read'
    assert mcp_client.classify_tool('rea', 'open_binary') == 'prepare'
    assert mcp_client.classify_tool('rea', 'set_address_name') == 'commit'
    assert mcp_client.classify_tool('rea', 'capture_process_scenario') == 'commit'


class _FakeHttpHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, payload, status=200, session=False):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        if session:
            self.send_header('Mcp-Session-Id', 'test-session')
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        message = json.loads(self.rfile.read(length) or b'{}')
        method = message.get('method')
        request_id = message.get('id')
        if method == 'notifications/initialized':
            self.send_response(202)
            self.end_headers()
            return
        if method == 'initialize':
            self._reply({'jsonrpc': '2.0', 'id': request_id, 'result': {
                'protocolVersion': '2025-06-18',
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'fake-http', 'version': '1'},
            }}, session=True)
            return
        if method == 'tools/list':
            self._reply({'jsonrpc': '2.0', 'id': request_id, 'result': {'tools': [
                {'name': 'echo', 'description': 'Echo text.',
                 'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}}},
            ]}})
            return
        if method == 'tools/call':
            text = ((message.get('params') or {}).get('arguments') or {}).get('text', '')
            self._reply({'jsonrpc': '2.0', 'id': request_id, 'result': {
                'content': [{'type': 'text', 'text': text}]}})
            return
        self._reply({'jsonrpc': '2.0', 'id': request_id,
                     'error': {'code': -32601, 'message': 'unknown method'}})

    def do_DELETE(self):
        self.send_response(200)
        self.end_headers()


@pytest.fixture
def fake_http_server(tmp_path, monkeypatch):
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), _FakeHttpHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    config = {
        'servers': {
            'fakehttp': {
                'name': 'Fake HTTP MCP',
                'transport': 'http',
                'url': f'http://127.0.0.1:{httpd.server_port}/mcp',
                'allow_private': True,
                'tools': {'allow': ['*'], 'read_only': ['echo'], 'prepare': [], 'blocked': []},
            },
        },
    }
    path = tmp_path / 'mcp_servers.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(mcp_client, 'MCP_SERVERS_PATH', str(path))
    mcp_client.reload_servers()
    mcp_client.close_all()
    yield
    mcp_client.close_all()
    httpd.shutdown()
    httpd.server_close()


def test_sse_parser_matches_request_id():
    raw = (b'event: message\n'
           b'data: {"jsonrpc":"2.0","id":"7","result":{"ok":true}}\n\n')
    assert mcp_client.HttpSession._parse_sse(raw, '7')['result'] == {'ok': True}


def test_http_transport_runs_tool(fake_http_server):
    out = mcp_client.run_tool('fakehttp', 'echo', {'text': 'http hello'}, read_only=True)
    assert out['ok'] is True
    assert 'http hello' in out['text']
    assert mcp_client.list_tools_text('fakehttp').startswith('MCP tools on Fake HTTP MCP')
