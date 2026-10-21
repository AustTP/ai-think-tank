"""Coding-executor reliability: a reply that is prose or JSON must never be
executed as a shell command, and a fixable failure gets one corrective round."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import content  # noqa: E402
import structured_edits  # noqa: E402


def test_shell_command_recognition():
    assert content._looks_like_shell("cat > a.py << 'EOF'\nprint(1)\nEOF")
    assert content._looks_like_shell('python -c "print(1)"')
    assert content._looks_like_shell("mkdir -p tests && cat > tests/test_a.py << 'EOF'\nEOF")
    assert content._looks_like_shell('./run.sh')
    for bad in ('Let me explain the fix first.',
                'Here is the code you asked for:',
                '{"edits": [], "commands": []}',
                '[1, 2, 3]',
                'The answer is:',
                ''):
        assert not content._looks_like_shell(bad), bad


def test_syntax_error_detection():
    assert content._is_command_syntax_error('bash: -c: line 1: syntax error near unexpected token `(\'')
    assert content._is_command_syntax_error('bash: -c: line 1: unexpected EOF')
    assert content._is_command_syntax_error('bash: Let: command not found')
    assert not content._is_command_syntax_error('1 failed, 2 passed')
    assert not content._is_command_syntax_error('')


def test_probe_request_parses_a_fenced_or_narrated_object():
    payload = {'probeRequest': {'path': 'index.html', 'actions': [], 'probes': ['x']}}
    assert content._parse_probe_request('```json\n' + json.dumps(payload) + '\n```')['path'] == 'index.html'
    assert content._parse_probe_request('Here it is:\n' + json.dumps(payload))['probes'] == ['x']
    # A bare shell command is still not a probe.
    assert content._parse_probe_request("cat > a.py << 'EOF'\nEOF") is None


def test_structured_edit_accepts_a_fenced_object():
    payload = {'edits': [{'path': 'a.py', 'old': None, 'new': 'print(1)\n'}], 'commands': []}
    cmd = structured_edits.command('```json\n' + json.dumps(payload) + '\n```')
    assert cmd.startswith('python -c ')


def test_structured_edit_rejects_prose():
    try:
        structured_edits.command('Let me make that change now.')
    except ValueError:
        pass
    else:
        raise AssertionError('prose must not become a command')


def test_prose_replies_are_never_executed(monkeypatch):
    """The live storm: non-command replies were run as shell. Two corrections,
    then an honest failure with no /api/execute call at all."""
    replies = iter(['Let me explain the fix first.',
                    'Here is the code you asked for:',
                    'The answer is below.'])
    calls = []

    def fake_http(method, base, path, payload, key):
        if path == '/api/chat':
            return {'reply': next(replies)}
        calls.append(path)
        return {'allowed': True, 'exitCode': 0, 'stdout': '', 'stderr': ''}

    monkeypatch.setattr(content._serve, '_http_json', fake_http)
    monkeypatch.setattr(content._serve, '_coding_tier_slug', lambda: 'model')
    monkeypatch.setattr(content._serve, 'get_or_create_agent_key', lambda a: 'key')
    monkeypatch.setattr(content, '_review_sandbox_id', lambda *a: 'sandbox-x')
    monkeypatch.setattr(content, '_agent_name', lambda *a: 'Ben')
    task = {'id': 'task-x', 'title': 'Fix the HTML title', 'taskType': 'code'}
    results = []
    content._run_coding_content_inner(
        {}, 'ben', task, result_sink=lambda tid, result: results.append(result))
    assert '/api/execute' not in calls
    assert len(results) == 1
    assert 'did not return an executable shell command' in results[0]['note']
    assert results[0]['ok'] is False
