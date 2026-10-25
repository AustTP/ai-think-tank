"""Per-story git worktree isolation (concurrent-edit fix).

The root problem: every coding/review/fix pass for every deliverable story
shared ONE sandbox dir (WORKROOM_SANDBOX_ID). Two agents writing different
stories into the same tree overwrote each other, and one agent's half-written
file failed every other agent's whole-tree lint. These tests pin the fix:
each deliverable story gets its own linked git worktree (branch
story/<task-id> of the shared workroom's main), reviews/fixes resolve to the
SAME worktree via the reviewOf chain, and the branch is merged back into the
base only when the peer gate is satisfied (_close_gated_story). Real git in
temp sandboxes; no mocks of the git plumbing under test.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import content  # noqa: E402
import sim  # noqa: E402


def _git(cwd, *args):
    return subprocess.run(['git'] + list(args), cwd=cwd, capture_output=True, text=True)


class _TempSandboxes(unittest.TestCase):
    """Patch serve.SANDBOXES_DIR to a fresh temp dir for the test body."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='tt-story-wt-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._sb = mock.patch.object(serve, 'SANDBOXES_DIR', self.tmp)
        self._sb.start()
        self.addCleanup(self._sb.stop)

    def _base_dir(self, sandbox_id='workroom-shared'):
        d = os.path.join(self.tmp, sandbox_id)
        os.makedirs(d, exist_ok=True)
        serve._ensure_sandbox_git_repo(d)
        if not serve._git_identity_configured(d):
            _git(d, 'config', 'user.email', 'test@example.com')
            _git(d, 'config', 'user.name', 'Test')
        if _git(d, 'rev-parse', '--verify', 'HEAD').returncode != 0:
            with open(os.path.join(d, 'shared.py'), 'w') as f:
                f.write('BASE = 1\n')
            _git(d, 'add', '-A')
            _git(d, 'commit', '-q', '-m', 'seed')
        return d


class StorySandboxIdHelpers(unittest.TestCase):
    def test_story_sandbox_id_prefixes(self):
        self.assertEqual(serve.story_sandbox_id('task-444'), 'story-task-444')

    def test_story_sandbox_id_rejects_traversal(self):
        with self.assertRaises(ValueError):
            serve.story_sandbox_id('../escape')
        with self.assertRaises(ValueError):
            serve.story_sandbox_id('a/b')

    def test_is_story_sandbox_id_accepts_both_forms(self):
        self.assertTrue(serve.is_story_sandbox_id('story-task-1'))
        self.assertTrue(serve.is_story_sandbox_id('winter-story-task-1'))
        self.assertFalse(serve.is_story_sandbox_id('workroom-shared'))
        self.assertFalse(serve.is_story_sandbox_id('spike-task-1'))
        self.assertFalse(serve.is_story_sandbox_id(None))

    def test_story_id_and_village_prefix(self):
        self.assertEqual(serve._story_id_from_sandbox('story-task-9'), 'task-9')
        self.assertEqual(serve._story_id_from_sandbox('winter-story-task-9'), 'task-9')
        self.assertIsNone(serve._story_id_from_sandbox('workroom-shared'))
        self.assertEqual(serve._village_prefix('winter-story-task-9'), 'winter-')
        self.assertEqual(serve._village_prefix('story-task-9'), '')

    def test_branch_and_base_resolution(self):
        self.assertEqual(serve._story_branch_name('winter-story-task-5'), 'story/task-5')
        self.assertEqual(serve._base_sandbox_for_story('winter-story-task-5'),
                         'winter-workroom-shared')
        self.assertEqual(serve._base_sandbox_for_story('story-task-5'),
                         'workroom-shared')


class ValidSandboxIdsExcludesStoryWorktrees(unittest.TestCase):
    def test_story_worktrees_filtered_from_product_candidates(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, 'workroom-shared'))
        os.makedirs(os.path.join(tmp, 'research-shared'))
        os.makedirs(os.path.join(tmp, 'story-task-444'))
        os.makedirs(os.path.join(tmp, 'winter-story-task-445'))
        with mock.patch.object(serve, 'SANDBOXES_DIR', tmp):
            ids = serve._valid_sandbox_ids()
        self.assertEqual(ids, ['research-shared', 'workroom-shared'])
        self.assertNotIn('story-task-444', ids)
        self.assertNotIn('winter-story-task-445', ids)

    def test_all_story_dirs_fall_back_to_default(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, 'story-task-1'))
        with mock.patch.object(serve, 'SANDBOXES_DIR', tmp):
            self.assertEqual(serve._valid_sandbox_ids(), ['workroom-shared'])


class EnsureStoryWorktree(_TempSandboxes):
    def test_creates_linked_worktree_on_branch(self):
        self._base_dir()
        path = serve.ensure_story_worktree('story-task-444')
        self.assertEqual(path, os.path.join(self.tmp, 'story-task-444'))
        self.assertTrue(os.path.isfile(os.path.join(path, '.git')))  # FILE, not dir
        self.assertTrue(serve._worktree_is_linked(path, os.path.join(self.tmp, 'workroom-shared')))
        # Branch is the story branch and it carries the base's seed file.
        self.assertEqual(_git(path, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip(),
                         'story/task-444')
        self.assertTrue(os.path.isfile(os.path.join(path, 'shared.py')))

    def test_same_story_id_reuses_one_worktree(self):
        self._base_dir()
        first = serve.ensure_story_worktree('story-task-444')
        # Author wrote a file; a second ensure (review pass, same story) must
        # NOT recreate or wipe the tree.
        with open(os.path.join(first, 'author.py'), 'w') as f:
            f.write('AUTHOR = 1\n')
        second = serve.ensure_story_worktree('story-task-444')
        self.assertEqual(first, second)
        self.assertTrue(os.path.isfile(os.path.join(second, 'author.py')))

    def test_two_stories_get_separate_worktrees(self):
        self._base_dir()
        a = serve.ensure_story_worktree('story-task-444')
        b = serve.ensure_story_worktree('story-task-445')
        self.assertNotEqual(a, b)
        with open(os.path.join(a, 'only_a.py'), 'w') as f:
            f.write('A = 1\n')
        _git(a, 'add', '-A')
        _git(a, 'commit', '-q', '-m', 'a work')
        # B never sees A's file; the base never sees it either until merge.
        self.assertFalse(os.path.exists(os.path.join(b, 'only_a.py')))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'workroom-shared', 'only_a.py')))

    def test_existing_non_linked_dir_is_not_clobbered(self):
        self._base_dir()
        d = os.path.join(self.tmp, 'story-task-446')
        os.makedirs(d)
        with open(os.path.join(d, 'precious.py'), 'w') as f:
            f.write('DO NOT LOSE ME\n')
        with self.assertRaises(ValueError):
            serve.ensure_story_worktree('story-task-446')
        with open(os.path.join(d, 'precious.py')) as f:
            self.assertEqual(f.read(), 'DO NOT LOSE ME\n')

    def test_worktree_dotgit_file_is_not_reinitialized(self):
        # Regression: a linked worktree's .git is a FILE (gitdir: pointer).
        # The old isdir check in _ensure_sandbox_git_repo returned False there
        # and re-ran `git init` inside the worktree, silently detaching it
        # from the main repo's history -- so later merges vanished.
        self._base_dir()
        path = serve.ensure_story_worktree('story-task-447')
        self.assertTrue(os.path.isfile(os.path.join(path, '.git')))
        # Directly exercise the fixed predicate + the ensure path a sandbox
        # endpoint would take: neither may reinit the worktree.
        serve._ensure_sandbox_git_repo(path)
        self.assertTrue(os.path.isfile(os.path.join(path, '.git')))
        self.assertTrue(serve._worktree_is_linked(path, os.path.join(self.tmp, 'workroom-shared')))
        serve.ensure_story_worktree('story-task-447')
        self.assertTrue(serve._worktree_is_linked(path, os.path.join(self.tmp, 'workroom-shared')))
        self.assertEqual(_git(path, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip(),
                         'story/task-447')


class MergeStoryBranch(_TempSandboxes):
    def test_merge_lands_story_file_on_base_main(self):
        base = self._base_dir()
        path = serve.ensure_story_worktree('story-task-444')
        with open(os.path.join(path, 'author.py'), 'w') as f:
            f.write('AUTHOR = 1\n')
        _git(path, 'add', '-A')
        _git(path, 'commit', '-q', '-m', 'story work')
        result = serve._merge_story_branch('story-task-444')
        self.assertTrue(result['ok'], result)
        self.assertTrue(os.path.isfile(os.path.join(base, 'author.py')))
        # Merge is a real no-ff merge commit on main, not a fast-forward.
        log = _git(base, 'log', '--oneline', '-2').stdout
        self.assertIn('story/task-444', log)

    def test_conflict_auto_resolves_story_side_wins(self):
        base = self._base_dir()
        path = serve.ensure_story_worktree('story-task-448')
        # Author rewrites shared.py in the story tree.
        with open(os.path.join(path, 'shared.py'), 'w') as f:
            f.write('STORY_WINS = 1\n')
        _git(path, 'add', '-A')
        _git(path, 'commit', '-q', '-m', 'story rewrite')
        # Meanwhile the base tree (another concurrent pass in the shared
        # room) also rewrote the same file.
        with open(os.path.join(base, 'shared.py'), 'w') as f:
            f.write('BASE_SIDE = 1\n')
        _git(base, 'add', '-A')
        _git(base, 'commit', '-q', '-m', 'base rewrite')
        result = serve._merge_story_branch('story-task-448')
        self.assertTrue(result['ok'], result)
        with open(os.path.join(base, 'shared.py')) as f:
            self.assertEqual(f.read(), 'STORY_WINS = 1\n')

    def test_missing_branch_reports_not_ok_without_raising(self):
        self._base_dir()
        result = serve._merge_story_branch('story-task-never-coded')
        self.assertFalse(result['ok'])
        self.assertIn('does not exist', result['reason'])

    def test_not_a_story_sandbox_id(self):
        self._base_dir()
        result = serve._merge_story_branch('workroom-shared')
        self.assertFalse(result['ok'])


class ReviewSandboxResolution(unittest.TestCase):
    """content._review_sandbox_id: coding/review/fix passes resolve the SAME
    per-story worktree via the reviewOf chain; non-code lanes and ensure
    failures fall back to the shared sandbox."""

    def _snapshot(self, tasks):
        return {
            'tasks': {t['id']: t for t in tasks},
            'agents': {
                'cora': {'name': 'Cora'},
                'ben': {'name': 'Ben'},
                'winter-agent': {'name': 'W', 'villageId': 'winter'},
            },
        }

    def test_coding_story_gets_story_sandbox(self):
        task = {'id': 'task-444', 'room': 'pressoffice', 'assignedTo': 'cora'}
        snap = self._snapshot([task])
        ensured = []
        with mock.patch.object(serve, 'ensure_story_worktree',
                               side_effect=lambda sid: ensured.append(sid)), \
             mock.patch.object(serve, '_village_sandbox_id',
                               side_effect=lambda aid, sid, state=None: sid):
            sid = content._review_sandbox_id(snap, task, 'cora')
        self.assertEqual(sid, 'story-task-444')
        self.assertEqual(ensured, ['story-task-444'])

    def test_review_resolves_to_root_story_sandbox(self):
        parent = {'id': 'task-444', 'room': 'pressoffice', 'assignedTo': 'cora'}
        review = {'id': 'task-444-rv1', 'room': 'pressoffice', 'reviewOf': 'task-444',
                  'reviewAuthorId': 'cora', 'assignedTo': 'ben'}
        snap = self._snapshot([parent, review])
        with mock.patch.object(serve, 'ensure_story_worktree'), \
             mock.patch.object(serve, '_village_sandbox_id',
                               side_effect=lambda aid, sid, state=None: sid):
            sid = content._review_sandbox_id(snap, review, 'ben')
        self.assertEqual(sid, 'story-task-444')

    def test_fix_task_resolves_to_root_story_sandbox(self):
        parent = {'id': 'task-444', 'room': 'pressoffice', 'assignedTo': 'cora'}
        review = {'id': 'task-444-rv1', 'room': 'pressoffice', 'reviewOf': 'task-444',
                  'reviewAuthorId': 'cora'}
        fix = {'id': 'task-444-fix', 'room': 'pressoffice', 'reviewOf': 'task-444-rv1',
               'assignedTo': 'cora'}
        snap = self._snapshot([parent, review, fix])
        with mock.patch.object(serve, 'ensure_story_worktree'), \
             mock.patch.object(serve, '_village_sandbox_id',
                               side_effect=lambda aid, sid, state=None: sid):
            sid = content._review_sandbox_id(snap, fix, 'cora')
        self.assertEqual(sid, 'story-task-444')

    def test_village_remap_uses_author_not_reviewer(self):
        # A reviewer in a different village than the author must still resolve
        # the AUTHOR's worktree; remapping by the reviewer's village would
        # point at a different (empty) tree.
        parent = {'id': 'task-444', 'room': 'pressoffice', 'assignedTo': 'winter-agent'}
        review = {'id': 'task-444-rv1', 'room': 'pressoffice', 'reviewOf': 'task-444',
                  'reviewAuthorId': 'winter-agent', 'assignedTo': 'ben'}
        snap = self._snapshot([parent, review])
        with mock.patch.object(serve, 'ensure_story_worktree'), \
             mock.patch.object(serve, '_village_sandbox_id',
                               side_effect=lambda aid, sid, state=None: sid):
            sid = content._review_sandbox_id(snap, review, 'ben')
        self.assertEqual(sid, 'story-task-444')
        # And with the real remapper: author in winter -> winter-story-...
        with mock.patch.object(serve, 'ensure_story_worktree'), \
             mock.patch.object(serve, '_village_sandbox_id',
                               wraps=serve._village_sandbox_id):
            sid = content._review_sandbox_id(snap, review, 'ben')
        self.assertEqual(sid, 'winter-story-task-444')

    def test_non_coding_lane_stays_on_shared(self):
        task = {'id': 'task-r1', 'room': 'observatory', 'assignedTo': 'cora'}
        snap = self._snapshot([task])
        sid = content._review_sandbox_id(snap, task, 'cora')
        self.assertEqual(sid, content.WORKROOM_SANDBOX_ID)

    def test_ensure_failure_falls_back_to_shared(self):
        task = {'id': 'task-444', 'room': 'pressoffice', 'assignedTo': 'cora'}
        snap = self._snapshot([task])
        with mock.patch.object(serve, 'ensure_story_worktree',
                               side_effect=RuntimeError('git down')), \
             mock.patch.object(serve, '_village_sandbox_id',
                               side_effect=lambda aid, sid, state=None: sid):
            sid = content._review_sandbox_id(snap, task, 'cora')
        self.assertEqual(sid, content.WORKROOM_SANDBOX_ID)

    def test_review_cycle_safe_on_cycle(self):
        # reviewOf cycle must not recurse forever; root falls back to shared.
        a = {'id': 'task-a', 'room': 'pressoffice', 'reviewOf': 'task-b',
             'assignedTo': 'cora'}
        b = {'id': 'task-b', 'room': 'pressoffice', 'reviewOf': 'task-a',
             'assignedTo': 'cora'}
        snap = self._snapshot([a, b])
        # Root resolution lands on task-b (or a) which is still coding-class;
        # the point is it terminates, not that it picks shared.
        with mock.patch.object(serve, 'ensure_story_worktree'), \
             mock.patch.object(serve, '_village_sandbox_id',
                               side_effect=lambda aid, sid, state=None: sid):
            sid = content._review_sandbox_id(snap, a, 'cora')
        self.assertIn(sid, ('story-task-a', 'story-task-b'))


class CloseGatedStoryMerges(unittest.TestCase):
    """sim._close_gated_story now merges the story worktree back into the
    base workroom after the gate is satisfied. Merge failure must never
    block the close."""

    def _coding_parent(self, **over):
        p = {'id': 'task-p1', 'title': 'Ship the brief', 'taskType': 'code',
             'room': 'pressoffice', 'projectLabel': 'PR-1', 'assignedTo': 'cora',
             '_peerGate': {'approvals': 2, 'approvers': ['ben', 'priya'],
                           'closed': False}}
        p.update(over)
        return p

    def test_coding_parent_triggers_merge(self):
        st = {'playerInbox': [], 'agents': {}, 'villages': []}
        parent = self._coding_parent()
        with mock.patch.object(sim, '_revoke_task_access'), \
             mock.patch.object(sim, '_deliver_card_report'), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch.object(serve, '_merge_story_branch',
                               return_value={'ok': True}) as merge:
            ok = sim._close_gated_story(st, parent)
        self.assertTrue(ok)
        self.assertEqual(parent['status'], 'done')
        merge.assert_called_once_with('story-task-p1')

    def test_non_coding_parent_skips_merge(self):
        st = {'playerInbox': [], 'agents': {}, 'villages': []}
        parent = self._coding_parent(room='observatory', productId=None)
        with mock.patch.object(sim, '_revoke_task_access'), \
             mock.patch.object(sim, '_deliver_card_report'), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch.object(serve, '_merge_story_branch') as merge:
            ok = sim._close_gated_story(st, parent)
        self.assertTrue(ok)
        merge.assert_not_called()

    def test_merge_failure_never_blocks_close(self):
        st = {'playerInbox': [], 'agents': {}, 'villages': []}
        parent = self._coding_parent()
        with mock.patch.object(sim, '_revoke_task_access'), \
             mock.patch.object(sim, '_deliver_card_report'), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch.object(serve, '_merge_story_branch',
                               return_value={'ok': False, 'reason': 'boom'}):
            ok = sim._close_gated_story(st, parent)
        self.assertTrue(ok)
        self.assertEqual(parent['status'], 'done')
        self.assertTrue(parent['_peerGate']['closed'])

    def test_merge_exception_never_blocks_close(self):
        st = {'playerInbox': [], 'agents': {}, 'villages': []}
        parent = self._coding_parent()
        with mock.patch.object(sim, '_revoke_task_access'), \
             mock.patch.object(sim, '_deliver_card_report'), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch.object(serve, '_merge_story_branch',
                               side_effect=RuntimeError('serve exploded')):
            ok = sim._close_gated_story(st, parent)
        self.assertTrue(ok)
        self.assertEqual(parent['status'], 'done')

    def test_village_parent_merges_village_sandbox(self):
        st = {
            'playerInbox': [],
            'agents': {'cora': {'villageId': 'winter'}},
            'villages': [{'id': 'winter'}],
        }
        parent = self._coding_parent(assignedTo='cora')
        with mock.patch.object(sim, '_revoke_task_access'), \
             mock.patch.object(sim, '_deliver_card_report'), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch.object(serve, '_merge_story_branch',
                               return_value={'ok': True}) as merge:
            sim._close_gated_story(st, parent)
        merge.assert_called_once_with('winter-story-task-p1')


class EndToEndStoryMerge(_TempSandboxes):
    """Full lifecycle against real git: two stories isolate, both merge on
    gate close, and the second merge sees the first's files (no lost update
    between sequential deliveries in the shared workroom)."""

    def _write_commit(self, path, name, body):
        with open(os.path.join(path, name), 'w') as f:
            f.write(body)
        _git(path, 'add', '-A')
        _git(path, 'commit', '-q', '-m', f'add {name}')

    def test_two_stories_merge_without_losing_either(self):
        base = self._base_dir()
        a = serve.ensure_story_worktree('story-task-444')
        b = serve.ensure_story_worktree('story-task-445')
        self._write_commit(a, 'story_a.py', 'A = 1\n')
        self._write_commit(b, 'story_b.py', 'B = 1\n')
        r1 = serve._merge_story_branch('story-task-444')
        r2 = serve._merge_story_branch('story-task-445')
        self.assertTrue(r1['ok'], r1)
        self.assertTrue(r2['ok'], r2)
        self.assertTrue(os.path.isfile(os.path.join(base, 'story_a.py')))
        self.assertTrue(os.path.isfile(os.path.join(base, 'story_b.py')))
        # And a NEW story branched after both merges inherits both files.
        c = serve.ensure_story_worktree('story-task-446')
        self.assertTrue(os.path.isfile(os.path.join(c, 'story_a.py')))
        self.assertTrue(os.path.isfile(os.path.join(c, 'story_b.py')))


if __name__ == '__main__':
    unittest.main()
