"""The credits pause is a HOLD, not a run failure.

When the OpenRouter account runs dry, every model call raises
OpenRouterCreditsPaused. The dispatch thread's contract (030__dispatch_
content_work) holds the card intact and requeues it -- but only if the
exception actually reaches it. Executor-level catch-alls that swallow the
pause turned a paused run into an empty run: produced nothing, got
abandoned, refused by the deliverable gate, and re-minted every ~3 minutes
(the 2026-11-05 pump that kept paging the player while the tank was broke).
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import serve  # noqa: E402


def _fake_paused():
    class _Paused(RuntimeError):
        credits_paused = True
    return _Paused('OpenRouter credits are exhausted; work is paused')


class CreditsPausePropagates(unittest.TestCase):
    def _exec(self, fn):
        import content  # the exec-merged content_parts namespace
        return fn(content)

    def test_plain_completion_reraises_pause(self):
        paused = _fake_paused()

        def run(agents):
            with unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                            side_effect=paused):
                agents._plain_completion('m', [{'role': 'user', 'content': 'x'}],
                                         16, task_id=None)

        self.assertRaises(paused.__class__, self._exec, run)

    def test_plain_completion_still_swallows_other_errors(self):
        def run(agents):
            with unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                            side_effect=RuntimeError('429 busy')):
                out = agents._plain_completion(
                    'm', [{'role': 'user', 'content': 'x'}], 16, task_id=None)
            self.assertEqual(out, '')

        self._exec(run)

    def test_vision_call_reraises_pause(self):
        paused = _fake_paused()

        def run(content):
            class _Boom(Exception):
                pass
            with unittest.mock.patch.object(serve, '_http_json',
                                            side_effect=paused):
                content._describe_screenshot_for_note
                # find the vision wrapper dynamically: any content function
                # whose except now re-raises must surface the pause
                fn = getattr(content, '_vision_describe', None)
                if fn is None:
                    raise _Boom('vision wrapper name changed')
            return fn

        # The dynamic-name path is brittle; assert statically instead.
        import re
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))),
            'agents', 'content_parts', '50_body_06.py')).read()
        m = re.search(r'except Exception as e:\n(\s+)if getattr\(e, .credits_paused., False\):\n\s+raise', src)
        self.assertIsNotNone(m, 'vision call site lost its pause re-raise')

    def test_executor_catchalls_are_guarded(self):
        # Static guard: every executor-level catch-all must re-raise the
        # credits pause first. This keeps future editors honest without
        # threading a live model call.
        import re
        # 10_body_02's one failed-result catch is a Library SAVE failure,
        # not a model call -- the pause cannot reach it.
        for f in ('30_body_04.py', '80_body_09.py', '50_body_06.py'):
            src = open(os.path.join(agents_source_dir(), 'content_parts',
                                    f)).read()
            for m in re.finditer(r"except Exception as e:\n", src):
                tail = src[m.end():m.end() + 300]
                # Only catch-alls that record a FAILED result can misreport
                # the pause as a run failure; generic tool-wrap catches that
                # return error strings to the agent are fine.
                if "'ok': False" not in tail:
                    continue
                self.assertIn('credits_paused', tail,
                              f'{f}: unguarded failed-result catch-all near '
                              f'offset {m.start()}')


def agents_source_dir():
    import agents
    return os.path.dirname(os.path.abspath(agents.__file__))


if __name__ == '__main__':
    unittest.main()
