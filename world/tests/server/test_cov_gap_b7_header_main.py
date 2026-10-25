"""Cover the two __main__-only blocks in the serve header part.

The blocks are unreachable through the normal `serve` import. These tests
compile the shipped source with blank-line padding so the executed line numbers
match the real part, then run only the tail blocks in isolated namespaces.
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_PART = os.path.join(_REPO, 'world', 'server', 'serve_parts', '00_header.py')


def _exec_lines(first, last, globals_extra):
    source = open(_PART, encoding='utf-8').read().splitlines()
    body = '\n'.join(source[first - 1:last])
    padded = '\n' * (first - 1) + body + '\n'
    ns = {'__name__': '__main__', '__file__': _PART}
    ns.update(globals_extra)
    exec(compile(padded, _PART, 'exec'), ns)
    return ns


def _exec_policy_block(globals_extra):
    source = open(_PART, encoding='utf-8').read()
    start = source.index("if __name__ == '__main__':\n    try:\n        _policy_mode")
    end = source.index('\n\ndef _reload_google_rate_limits', start)
    padded = '\n' * source.count('\n', 0, start) + source[start:end]
    ns = {'__name__': '__main__', '__file__': _PART}
    ns.update(globals_extra)
    exec(compile(padded, _PART, 'exec'), ns)
    return ns


class HeaderMainBlocksTest(unittest.TestCase):
    def test_main_alias_block(self):
        ns = _exec_lines(32, 38, {'sys': sys})
        self.assertIn('serve', sys.modules)

    def test_policy_mode_from_env(self):
        old = os.environ.get('OPTIMIZATION_POLICY_MODE')
        try:
            ns = _exec_policy_block({
                'os': os,
                '_load_env': lambda: {'OPTIMIZATION_POLICY_MODE': 'apply'},
            })
            self.assertEqual('apply',
                             ns['os'].environ['OPTIMIZATION_POLICY_MODE'])
        finally:
            if old is None:
                os.environ.pop('OPTIMIZATION_POLICY_MODE', None)
            else:
                os.environ['OPTIMIZATION_POLICY_MODE'] = old

    def test_policy_mode_exception_is_swallowed(self):
        def _boom():
            raise RuntimeError('unreadable env')
        _exec_policy_block({'os': os, '_load_env': _boom})


if __name__ == '__main__':
    unittest.main()
