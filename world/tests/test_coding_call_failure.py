import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import content


class CodingCallFailure(unittest.TestCase):
    def test_preserves_service_failure(self):
        result = content._coding_call_failure({'error': 'request failed: timed out'}, 'small')
        self.assertIn('timed out', result['note'])
        self.assertTrue(result['modelRequestFailed'])
        self.assertFalse(result['ok'])

    def test_budget_failure_remains_distinct(self):
        result = content._coding_call_failure({'budgetExhausted': True, 'taskSpendUsd': .5}, 'small')
        self.assertTrue(result['budgetExhausted'])
        self.assertEqual(result['taskSpendUsd'], .5)

    def test_missing_reply(self):
        for response in (None, {}, {'reply': ''}):
            self.assertIn('no reply', content._coding_call_failure(response, 'small')['note'])


class ShellFormat(unittest.TestCase):
    def test_multiple_blocks_are_rejected(self):
        with self.assertRaises(ValueError):
            content._extract_shell_command("```sh\necho one\n```\nLet us explain\n```sh\necho two\n```")

    def test_prose_is_never_a_command(self):
        with self.assertRaises(ValueError):
            content._extract_shell_command("Let us fix the code.")

    def test_one_block_keeps_only_shell(self):
        self.assertEqual(content._extract_shell_command("Explanation\n```sh\necho ok\n```\nMore explanation"), "echo ok")


class PythonWriteSyntax(unittest.TestCase):
    def test_balanced_but_incomplete_python_is_rejected(self):
        self.assertTrue(content._python_heredoc_errors("cat > test_file.py <<'EOF'\ndef test_example\nEOF"))

    def test_valid_python_and_unicode(self):
        self.assertEqual(content._python_heredoc_errors("cat > file.py <<'EOF'\nx = '?'\nEOF"), [])

    def test_reverse_redirection_and_nested_text(self):
        self.assertEqual(content._python_heredoc_errors("cat <<'EOF' > file.py\ns = 'cat > other.py'\nEOF"), [])

    def test_later_broken_write_is_checked(self):
        self.assertTrue(content._python_heredoc_errors("cat > one.py <<'EOF'\nx = 1\nEOF\ncat > two.py <<'EOF'\nx = hd.\nEOF"))
