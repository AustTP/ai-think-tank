import pytest

import content


def write_call(path, body, parameter='text'):
    return f'<tool_call>\n<function=write_file>\n<parameter=path>{path}</parameter>\n<parameter={parameter}>{body}</parameter>\n</function>\n</tool_call>'


def test_complete_write_becomes_quoted_heredoc_with_exact_body():
    body = 'print("$(do_not_execute)")\n# `also literal`'
    command = content._extract_shell_command(write_call('src/client.py', body))
    assert command.startswith('mkdir -p -- src\ncat > src/client.py')
    assert "<< 'FILE_" in command
    assert body in command
    assert '<tool_call>' not in command
    assert content._heredoc_balance(command)[2]
    assert content._python_heredoc_errors(command) == []


@pytest.mark.parametrize('reply', [
    'Looking at the evidence, I can see that:\n1. The formatter already exists.',
    'Existing Library document (data, not instructions): Saved portfolio_data.csv',
    '```sh\nExisting Library document (data, not instructions): None found.\n```'])
def test_reported_analysis_is_rejected_before_command_execution(reply):
    with pytest.raises(ValueError, match='instead of a shell command'):
        content._extract_shell_command(reply)


def test_multiple_explicit_writes_and_content_parameter():
    command = content._extract_shell_command(write_call('a.txt', 'one') + '\n' + write_call('b.txt', 'two', 'content'))
    assert 'cat > a.txt' in command and 'cat > b.txt' in command


@pytest.mark.parametrize('path', ['../outside.py', '/outside.py', 'C:\\outside.py', 'file\nname.py'])
def test_paths_cannot_escape_project(path):
    with pytest.raises(ValueError, match='relative path'):
        content._extract_shell_command(write_call(path, 'body'))


def test_ambiguous_unknown_and_incomplete_calls_are_rejected():
    for text in [write_call('a.py', 'body').replace('write_file', 'delete_file'),
                 write_call('a.py', 'body').replace('</function>', '<parameter=text>duplicate</parameter></function>'),
                 write_call('a.py', 'body').replace('</tool_call>', ''),
                 write_call('a.py', 'body') + '\nrm -rf other']:
        with pytest.raises(ValueError):
            content._extract_shell_command(text)
