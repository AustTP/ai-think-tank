from unittest.mock import patch

import pytest
import content
import notify


def test_tool_call_text_is_repaired_before_shell_execution():
    with pytest.raises(ValueError, match='structured tool call'):
        content._extract_shell_command('<tool_call>\n<function=write_file>\n...')
    assert content._extract_shell_command("cat > example.txt << 'EOF'\n<tool_call>data</tool_call>\nEOF").startswith('cat >')


def test_human_approval_queues_one_checked_repair():
    task = {'id': 't', 'assignedTo': 'ada', 'status': 'blocked', 'failureCategory': 'policy'}
    state = {'tasks': {'t': task}}
    approvals = {'approval': {'kind': 'blocked command', 'status': 'approved', 'resolvedBy': 'admin',
                             'resolvedAt': 10, 'note': '{"taskId":"t","agentId":"ada"}'}}
    with patch('review_guidance.apply_guidance') as retry:
        notify.reconcile_command_approvals(state, approvals)
        notify.reconcile_command_approvals(state, approvals)
        assert retry.call_count == 1
        assert retry.call_args.args[1] == 't'
        assert retry.call_args.args[2]['action'] == 'retry'


def test_later_human_pause_is_respected():
    task = {'id': 't', 'assignedTo': 'ada', 'status': 'blocked', 'failureCategory': 'policy',
            'interventionHistory': [{'at': 11000, 'action': 'pause'}]}
    state = {'tasks': {'t': task}}
    approvals = {'approval': {'kind': 'blocked command', 'status': 'approved', 'resolvedBy': 'admin',
                             'resolvedAt': 10, 'note': '{"taskId":"t","agentId":"ada"}'}}
    with patch('review_guidance.apply_guidance') as retry:
        notify.reconcile_command_approvals(state, approvals)
        retry.assert_not_called()
