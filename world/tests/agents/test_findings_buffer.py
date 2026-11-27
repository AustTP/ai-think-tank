"""The Muffin shared-findings buffer (arXiv 2610.31355) must stay a hint
channel: one finding per completed run, reads only peers' findings, and a
buffer failure can never break a dispatch or a result merge.

Covers agents/findings_buffer.py behavior plus source contracts on the two
seams that wire it in (the sim result merge and the content dispatcher).
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


def _buffer():
    import agents.findings_buffer as findings_buffer
    return findings_buffer


class RootKey(unittest.TestCase):
    def test_review_child_shares_request_buffer(self):
        # A review card (reviewOf) must land in the SAME buffer as the story
        # it reviews: review N sees review N-1's finding.
        fb = _buffer()
        state = {'tasks': {
            'task-a': {'id': 'task-a', 'workOrderId': 'request-9'},
            'task-b': {'id': 'task-b', 'reviewOf': 'task-a'},
        }}
        self.assertEqual(fb._root_key(state, state['tasks']['task-b']),
                         'request-9')

    def test_falls_back_to_root_story_then_task(self):
        fb = _buffer()
        state = {'tasks': {'task-a': {'id': 'task-a'}}}
        self.assertEqual(fb._root_key(state, state['tasks']['task-a']),
                         'task-a')
        self.assertEqual(fb._root_key(None, {'id': 'task-z'}), 'task-z')

    def test_never_mutates_the_task(self):
        # story_tracking.stamp() mutates; this must not (dispatch-time read).
        fb = _buffer()
        task = {'id': 'task-b', 'reviewOf': 'task-a'}
        state = {'tasks': {'task-a': {'id': 'task-a', 'workOrderId': 'r1'}}}
        fb._root_key(state, task)
        self.assertEqual(task, {'id': 'task-b', 'reviewOf': 'task-a'})


class AppendAndRead(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_append_write_dedup_and_read_skips_self(self):
        fb = _buffer()
        task = {'id': 'task-b', 'reviewOf': 'task-a'}
        state = {'tasks': {
            'task-a': {'id': 'task-a', 'workOrderId': 'request-9'},
            'task-b': task,
        }}
        with mock.patch('serve.LIBRARY_DIR', self._tmp.name):
            self.assertTrue(fb.append(state, 'leo', task,
                                      'review found no sources; pilot is fine',
                                      library_path='projects/pilot.md'))
            # Same run merged twice: one finding per task id.
            self.assertFalse(fb.append(state, 'leo', task, 'again'))
            entries = fb.recent(state, 'clara', task)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['agent'], 'leo')
            self.assertEqual(entries[0]['task'], 'task-b')
            self.assertEqual(entries[0]['libraryPath'], 'projects/pilot.md')
            self.assertEqual(fb.recent(state, 'leo', task), [])
            raw = json.loads(open(os.path.join(
                self._tmp.name, 'findings', 'request-9.jsonl'),
                encoding='utf-8').read().splitlines()[0])
            self.assertEqual(raw['agent'], 'leo')

    def test_empty_note_and_disabled_append_are_noops(self):
        fb = _buffer()
        task = {'id': 'task-x'}
        with mock.patch('serve.LIBRARY_DIR', self._tmp.name):
            self.assertFalse(fb.append({}, 'leo', task, '   '))
            with mock.patch.object(fb, 'enabled', return_value=False):
                self.assertFalse(fb.append({}, 'leo', task, 'real finding'))
            self.assertEqual(os.listdir(self._tmp.name), [])

    def test_recent_oldest_first_and_k_bound(self):
        fb = _buffer()
        state = {'tasks': {t: {'id': t, 'workOrderId': 'request-9'}
                           for t in ('task-a', 'task-b', 'task-c', 'task-d')}}
        for tid, agent in (('task-a', 'leo'), ('task-b', 'clara'),
                           ('task-c', 'nora'), ('task-d', 'ruth')):
            fb.append(state, agent, state['tasks'][tid], f'note by {agent}')
        # Nora reads the 2 most recent peer findings (clara, ruth),
        # oldest-first; her own entry is skipped.
        got = fb.recent(state, 'nora', state['tasks']['task-c'], k=2)
        self.assertEqual([e['agent'] for e in got], ['clara', 'ruth'])
        # Clara reads peers oldest-last; her own entry is skipped.
        got = fb.recent(state, 'clara', state['tasks']['task-b'], k=3)
        self.assertEqual([e['agent'] for e in got], ['leo', 'nora', 'ruth'])

    def test_digest_renders_bullets(self):
        fb = _buffer()
        text = fb.digest([{'agent': 'leo', 'finding': 'a\nb',
                           'libraryPath': 'p.md'},
                          {'agent': 'clara', 'finding': 'plain'}])
        self.assertEqual(text.splitlines(),
                         ['- [leo] a b (file: p.md)', '- [clara] plain'])


class Seams(unittest.TestCase):
    def _src(self, rel):
        import agents as _agents_mod
        root = os.path.dirname(os.path.dirname(os.path.abspath(_agents_mod.__file__)))
        return open(os.path.join(root, rel), encoding='utf-8').read()

    def test_merge_site_appends_real_notes_only(self):
        # The sim result merge drops ONE finding per completion, and skips
        # noop placeholders, budget-exhaustion stubs, infra failures and
        # ok:False model-failure stubs -- none of those is a finding.
        src = self._src('village/sim_parts/020_work_request_room_thin.py')
        start = src.index('def _apply_content_result')
        body = src[start:src.index('\ndef ', start + 10)]
        self.assertIn('findings_buffer.append', body)
        self.assertIn("result.get('ok') is not False", body)
        self.assertIn("not result.get('noop')", body)
        self.assertIn("not result.get('budgetExhausted')", body)
        self.assertIn("not result.get('infra')", body)
        self.assertIn('library_path=result.get', body)

    def test_dispatcher_folds_peer_findings_into_perception(self):
        # base_ctx is a perception STRING by contract; the fold must be a
        # string append next to the hybrid pre-pass, best-effort. A
        # non-empty fold logs a findings_digest row so live deploys can
        # show the read side fires (hybrid_mode precedent).
        src = self._src('agents/content_parts/10_body_02.py')
        self.assertIn('_findings_prepare', src)
        fold = src[src.index('_fb_digest = _findings_prepare'):]
        self.assertIn('Shared findings from other agents', fold)
        self.assertLess(fold.index('base_ctx = ((base_ctx or \'\')'),
                        fold.index('executor(snapshot, agent_id, task, base_ctx)'),
                        'the fold happens before the executor runs')
        hook = self._src('agents/content_parts/16_body_02_findings.py')
        self.assertIn("'findings_digest'", hook)
        self.assertLess(hook.index('_serve.log_action'),
                        hook.index('return text'),
                        'the digest row logs only for a non-empty fold')

    def test_part_file_is_loaded_by_the_exec_merger(self):
        # content.py execs every agents/content_parts/*.py in sorted order:
        # the new part must exist there, not as a stray module.
        import agents as _agents_mod
        root = os.path.dirname(os.path.dirname(os.path.abspath(_agents_mod.__file__)))
        self.assertTrue(os.path.isfile(os.path.join(
            root, 'agents', 'content_parts', '16_body_02_findings.py')))


if __name__ == '__main__':
    unittest.main()
