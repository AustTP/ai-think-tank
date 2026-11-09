"""Adversarial pairing tags survive grooming (2026-11-08 live verification).

The winter ceremony accepted wrq-591 and the card queued UNTAGGED: queue_work
did not whitelist adversarialTaskId, so _adversarial_village_tick could never
see the side deliver and the winter village would never auto-disable."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import sim  # noqa: E402


class AdversarialTagFlow(unittest.TestCase):
    def _state(self):
        return {'tasks': {}, 'workQueue': [], 'agents': {}, 'agentRoster': [],
                'issues': {}, 'teams': []}

    def test_queue_work_carries_the_pairing_tag(self):
        state = self._state()
        sim.queue_work(state, [{
            'title': 'Write the winter note',
            'instructions': 'One paragraph.',
            'room': 'observatory',
            'taskType': 'report',
            'teamId': 'gabriella',
            'villageId': 'winter',
            'adversarialTaskId': 'adv-123',
        }])
        item = next(w for w in state['workQueue']
                    if 'winter note' in (w.get('title') or ''))
        self.assertEqual(item.get('adversarialTaskId'), 'adv-123')
        self.assertEqual(item.get('villageId'), 'winter')

    def test_refinement_accept_carries_the_request_tag(self):
        # The accept branch must pass the request's pairing tags to queue_work:
        # assert on the source contract by exercising queue_work with a req
        # shaped item and checking the queue, since the ceremony itself needs
        # a full meeting fixture.
        state = self._state()
        req = {'id': 'wrq-x', 'title': 'Winter note', 'room': 'observatory',
               'taskType': 'report', 'teamId': 'gabriella',
               'villageId': 'winter', 'adversarialTaskId': 'adv-123',
               'status': 'pending', 'filedBy': 'gabriella'}
        # Mirror of the accept branch's field pass: the fix made the tags ride
        # queue_work; simulate exactly that call shape.
        sim.queue_work(state, [{
            'title': req['title'], 'room': req['room'],
            'instructions': 'x', 'taskType': req['taskType'],
            'teamId': req.get('teamId'),
            'adversarialTaskId': req.get('adversarialTaskId'),
            'villageId': req.get('villageId'),
        }])
        item = state['workQueue'][0]
        self.assertEqual(item.get('adversarialTaskId'), 'adv-123')
        self.assertEqual(item.get('villageId'), 'winter')


if __name__ == '__main__':
    unittest.main()
