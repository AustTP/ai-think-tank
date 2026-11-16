"""Content dispatch shares ONE frozen base snapshot per save revision.

The old per-job json.loads(json.dumps(state)) made a worker burst hold N
whole-state copies at once -- the GBs-per-minute transient churn that
ratcheted the VM tank's RSS into the cgroup kill every ~20 minutes
(2026-11-16). Each job now gets a per-job shell (own tasks mapping, private
copy of its own task) over a shared base that rebuilds only when a save
bumps the revision or the live state object identity changes.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import sim  # noqa: E402
import serve  # noqa: E402


def _dispatch(state, task, seen, done):
    def executor(snapshot, agent_id, task, base_ctx=None):
        seen[task['id']] = snapshot
        done[task['id']].set()

    ok = sim._dispatch_content_work(executor, state, 'cora', task, 0.0)
    assert ok
    done[task['id']].wait(5)


class DispatchSnapshotSharing(unittest.TestCase):
    def _state(self):
        return {'sim': {'owner': 'server'},
                'agents': {'cora': {'id': 'cora', 'inRoom': 'lab'}},
                'tasks': {'t1': {'id': 't1', 'room': 'lab'},
                          't2': {'id': 't2', 'room': 'lab'}},
                'workQueue': [], 'agentRoster': []}

    def test_two_jobs_share_one_base_but_get_private_task_shells(self):
        state = self._state()
        seen, done = {}, {'t1': threading.Event(), 't2': threading.Event()}
        _dispatch(state, {'id': 't1', 'room': 'lab'}, seen, done)
        _dispatch(state, {'id': 't2', 'room': 'lab'}, seen, done)
        s1, s2 = seen['t1'], seen['t2']
        # one shared frozen base between the two bursts
        self.assertIs(s1['agents'], s2['agents'])
        # private shells: distinct tasks mappings, and neither aliases the
        # LIVE state's task objects
        self.assertIsNot(s1['tasks'], s2['tasks'])
        self.assertIsNot(s1['tasks']['t1'], state['tasks']['t1'])
        self.assertIsNot(s2['tasks']['t2'], state['tasks']['t2'])
        # each job privately owns its own task copy
        s1['tasks']['t1']['workUntil'] = 'mutated-by-job-1'
        self.assertNotIn('workUntil', s2['tasks']['t1'])
        self.assertNotIn('workUntil', state['tasks']['t1'])

    def test_base_rebuilds_after_a_save_bumps_the_revision(self):
        state = self._state()
        seen, done = {}, {'t1': threading.Event(), 't2': threading.Event()}
        _dispatch(state, {'id': 't1', 'room': 'lab'}, seen, done)
        before = seen['t1']
        serve._STATE_SAVE_REV[0] += 1  # a save persisted elsewhere
        _dispatch(state, {'id': 't2', 'room': 'lab'}, seen, done)
        self.assertIsNot(seen['t2']['agents'], before['agents'])

    def test_new_state_object_identity_forces_a_rebuild(self):
        # tests (and any caller) handing a DIFFERENT live state dict must
        # never read a snapshot frozen for the old one
        state_a, state_b = self._state(), self._state()
        state_b['agents']['pax'] = {'inRoom': 'warroom'}
        seen, done = {}, {'t1': threading.Event(), 't2': threading.Event()}
        _dispatch(state_a, {'id': 't1', 'room': 'lab'}, seen, done)
        _dispatch(state_b, {'id': 't2', 'room': 'lab'}, seen, done)
        self.assertNotIn('pax', seen['t1']['agents'])
        self.assertIn('pax', seen['t2']['agents'])


if __name__ == '__main__':
    unittest.main()
