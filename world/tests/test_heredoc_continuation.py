import pytest
import content
from unittest.mock import patch


def test_delimiters_are_tracked_in_shell_but_not_file_contents():
    command = "cat > app.py << 'PYTHON'\ntext = '<<EOF'\nPYTHON\ncat > tests/test_app.py << 'TESTS'\n"
    assert content._heredoc_balance(command) == (2, 1, False)
    assert content._heredoc_balance(command + 'assert True\nTESTS') == (2, 2, True)


def test_adapter_delimiters_and_indented_false_closers():
    assert not content._heredoc_balance("cat > a.py << 'FILE_hash'\npass\n")[2]
    assert not content._heredoc_balance("cat > a.py << 'EOF'\npass\n EOF\n")[2]
    assert content._heredoc_balance("cat > a.py <<- 'END'\npass\n\tEND\n")[2]


def test_continuation_carries_tail_and_exact_delimiter_in_latest_prompt():
    command = "cat > app.py << 'PY'\ndef run():\n    return"
    prompt = content._continuation_prompt(command)
    assert command in prompt and 'Required closing delimiter(s)' in prompt
    merged = content._merge_continuation(command, ' 1\nPY\n')
    assert content._heredoc_balance(merged)[2]
    assert content._python_heredoc_errors(merged) == []


@pytest.mark.parametrize('fenced', [False, True])
def test_extraction_preserves_unfinished_body_whitespace(fenced):
    command = "cat > app.py << 'PY'\ndef run():\n    return 1\n"
    reply = '```bash\n' + command + '```' if fenced else command
    extracted = content._extract_shell_command(reply)
    assert extracted == command
    merged = content._merge_continuation(extracted, 'PY\n')
    assert content._heredoc_balance(merged)[2]
    assert content._python_heredoc_errors(merged) == []


def test_delimiter_only_continuation_gets_its_own_line():
    command = "cat > app.py << 'PY'\nvalue = 1"
    merged = content._merge_continuation(command, 'PY\n')
    assert merged == command + '\nPY\n'
    assert content._heredoc_balance(merged)[2]
    assert content._python_heredoc_errors(merged) == []


def test_mid_line_continuation_preserves_spaces():
    command = content._extract_shell_command("cat > app.py << 'PY'\nvalue = 'hello ")
    merged = content._merge_continuation(command, "world'\nPY\n")
    assert "value = 'hello world'" in merged
    assert content._python_heredoc_errors(merged) == []


@pytest.mark.parametrize('reply', ["cat >> app.py << 'EOF'\npass", '<tool_call>...', '```python\npass\n```', 'return 1'])
def test_restarts_wrappers_and_duplicates_are_not_concatenated(reply):
    with pytest.raises(ValueError):
        content._merge_continuation("cat > app.py << 'EOF'\ndef run():\n    return 1\n", reply)


@pytest.mark.parametrize('chunks, succeeds', [
    (["cat > note.txt << 'DATA'\npartial", "cat >> note.txt << 'DATA'\nrestart", ' complete\nDATA\n'], True),
    (["cat > note.txt << 'DATA'\npartial\n", 'DATA\n'], True),
    (["cat > note.txt << 'DATA'\npartial", 'DATA\n'], True),
    (["cat > note.txt << 'DATA'\npartial", ' more', ' complete\nDATA\n'], True),
    (["cat > note.txt << 'DATA'\npartial"] + [' more'] * content.CODE_CONTINUATION_ATTEMPTS, False),
])
def test_restarted_continuation_is_rejected_and_only_complete_command_executes(tmp_path, chunks, succeeds):
    replies = iter(chunks)
    calls = []
    results = []
    def http(method, base, path, payload, *args, **kwargs):
        calls.append((path, payload))
        if path == '/api/chat':
            # Earlier replies occur once each; cumulative copies balloon context.
            assistant_messages = [m['content'] for m in payload['messages'] if m['role'] == 'assistant']
            assert assistant_messages == chunks[:len(assistant_messages)]
            return {'reply': next(replies)}
        assert path == '/api/execute'
        assert content._heredoc_balance(payload['command'])[2]
        assert 'restart' not in payload['command']
        return {'allowed': True, 'exitCode': 0, 'timedOut': False}
    with patch.object(content._serve, 'SANDBOXES_DIR', str(tmp_path)), \
            patch.object(content._serve, 'get_or_create_agent_key', return_value='test'), \
            patch.object(content._serve, '_coding_tier_slug', return_value='model'), \
            patch.object(content._serve, '_http_json', side_effect=http), \
            patch.object(content, '_find_phantom_script_refs', return_value=[]), \
            patch.object(content, '_find_dangling_selector_refs', return_value=[]), \
            patch.object(content, '_run_quality_pipeline', return_value={'ok': True, 'note': 'passed', 'results': []}):
        assert content._run_coding_content_inner({}, 'oliver', {'id': 't', 'title': 'Write a short text file'},
            sandbox_override='stage-test', result_sink=lambda tid, result: results.append(result)) == succeeds
    assert len([path for path, _ in calls if path == '/api/execute']) == int(succeeds)
    if succeeds:
        assert results[-1]['pipelineOk']
    else:
        assert 'not executed' in results[-1]['note']
