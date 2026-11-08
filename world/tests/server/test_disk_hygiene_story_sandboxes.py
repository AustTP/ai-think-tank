"""Disk-hygiene sweeper: story/spike sandbox reaping (2026-11-08).

The original sweep only reaped stage-* dirs; story-task-*/spike-task-*
workspaces (a per-task git repo each, ~13 GB over 1,063 dirs) outlived
their tasks forever. Same rules now: a workspace whose task is terminal
or unknown is scratch once the grace window passes. Superseded
previous-* restart snapshots reap on plain mtime age."""
import os
import sys
import unittest

import serve  # noqa: E402


def _touch_old(path, age_s):
    import time
    now = time.time()
    os.utime(path, (now - age_s, now - age_s))


class StorySandboxReaping(unittest.TestCase):
    def setUp(self):
        self._old_tt = serve.THINK_TANK_DIR
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix='hygiene-')
        serve.THINK_TANK_DIR = self.tmp
        sb = os.path.join(self.tmp, 'sandboxes')
        os.makedirs(sb)
        for name in ('story-task-1', 'story-task-2', 'spike-task-3',
                     'story-task-4'):
            os.makedirs(os.path.join(sb, name))
        os.makedirs(os.path.join(sb, 'previous-aaa'))
        os.makedirs(os.path.join(sb, 'previous-bbb'))
        os.makedirs(os.path.join(sb, 'workroom-shared'))
        # story-task-1: dead task, old -> reaped. story-task-2: live task
        # -> kept. spike-task-3: archived task, old -> reaped.
        # story-task-4: dead task but touched recently -> kept (grace).
        _touch_old(os.path.join(sb, 'story-task-1'), 10 * 3600)
        _touch_old(os.path.join(sb, 'story-task-2'), 10 * 3600)
        _touch_old(os.path.join(sb, 'spike-task-3'), 10 * 3600)
        _touch_old(os.path.join(sb, 'story-task-4'), 60)
        _touch_old(os.path.join(sb, 'previous-aaa'), 72 * 3600)
        _touch_old(os.path.join(sb, 'previous-bbb'), 60)
        self._old_status = serve._sandbox_task_status

        def fake_status(task_id):
            if task_id == 'task-2':
                return None  # live: never delete
            if task_id in ('task-1', 'task-3', 'task-4'):
                return 'terminal' if task_id == 'task-3' else 'absent'
            return 'absent'

        serve._sandbox_task_status = fake_status

    def tearDown(self):
        serve._sandbox_task_status = self._old_status
        serve.THINK_TANK_DIR = self._old_tt
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_terminal_story_spike_workspaces_reaped(self):
        serve._prune_sandboxes()
        sb = os.path.join(self.tmp, 'sandboxes')
        names = set(os.listdir(sb))
        self.assertNotIn('story-task-1', names)
        self.assertNotIn('spike-task-3', names)
        self.assertIn('story-task-2', names)   # live task: never deleted
        self.assertIn('story-task-4', names)   # within the grace window
        self.assertNotIn('previous-aaa', names)  # older than 48h: reaped
        self.assertIn('previous-bbb', names)     # fresh snapshot: kept
        self.assertIn('workroom-shared', names)  # always spared


if __name__ == '__main__':
    unittest.main()
