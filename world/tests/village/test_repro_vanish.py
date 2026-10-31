"""Regression guard: accepted carryaway cards must survive queue -> task.

DEV-57/ADA-34 were accepted by the groomer (queue_work ran), the queue drained
to 0, yet no task was ever created and nothing was archived. This test mirrors
the exact queue-item shape the groomer minted and drives the due-item loop.
Originally filed as a repro; it passes deterministically, so it stays as the
guard against reintroducing the vanish bug.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import sim  # noqa: E402

_NOW_MS = 1_725_000_000_000
_GROOM_AT = _NOW_MS - 60_000  # carryaway a minute ago -> item is due


def _agent(aid, **kw):
    base = {'id': aid, 'x': 0, 'y': 0, 'dir': 'down', 'offDuty': True,
            'visible': False, 'busy': False, 'task': None, 'path': None,
            'pathIndex': 0, 'pathTarget': None, 'pathActive': False,
            'pairWith': None, 'handoff': None, 'inRoom': None}
    base.update(kw)
    return base


def _state():
    return {
        'sim': {'owner': 'server'},
        'agentRoster': [{'id': 'ada'}, {'id': 'ben'}],
        'agents': {'ada': _agent('ada'), 'ben': _agent('ben')},
        'researchTopics': [],
        'tasks': {},
        'workQueue': [],
    }


def _groomed_item(**over):
    """Field-for-field the queue_work(...) payload from 120__file_free_spike
    (grooming carryaway accept branch) for a player-filed 'task' card."""
    item = {
        'title': '[TASK] Charlotte arrival brief: one-page first-month cost brief',
        'room': 'pressoffice',
        'goal': '[TASK] Charlotte arrival brief: one-page first-month cost brief',
        'instructions': ('Filed by player. Continue the quality-of-life gaps '
                         'from your pre-move briefing.'),
        'taskType': 'code',
        'deliverable': 'note',
        'approvedTools': ['read', 'write', 'shell'],
        'teamId': 'team-1',
        'userStory': 'As a newcomer moving to Charlotte next month, I want a '
                     'one-page first-month cost brief so that I can plan spending.',
        'acceptanceCriteria': ['One page', 'Notes assumptions and unknowns'],
        'never': [],
        'dependsOn': None,
        'issueKey': 'iss-DEV-57',
        'priority': 0,
        'assignedTo': None,
        'directedTo': None,
        'lane': None,
        'budgetUsd': 0.86,
        'budgetBand': 'task',
        'playerAuthored': True,
    }
    item.update(over)
    return item


class VanishRepro(unittest.TestCase):
    def test_groomed_note_card_survives_assignment(self):
        state = _state()
        sim.queue_work(state, [_groomed_item()])
        self.assertEqual(len(state['workQueue']), 1)
        item = state['workQueue'][0]
        # queue_work must not have swallowed the note contract
        self.assertEqual(item.get('taskType'), 'code')
        self.assertEqual(item.get('deliverable'), 'note')
        # Make the item due and run the assignment loop
        state['workQueue'][0]['notBefore'] = _GROOM_AT
        state['workQueue'][0]['createdAt'] = _GROOM_AT
        state['workQueue'][0]['attempts'] = 0
        state['agents']['ada']['offDuty'] = False
        state['agents']['ada']['inRoom'] = 'pressoffice'
        n = sim._stale_work_step(state, _NOW_MS / 1000, _NOW_MS)
        print('loop ran, remaining queue:', len(state['workQueue']))
        print('tasks:', {k: t.get('status') for k, t in state['tasks'].items()})
        print('ada task:', (state['agents'].get('ada') or {}).get('task'))
        self.assertTrue(state['workQueue'] or state['tasks'],
                        'card vanished: queue empty AND no task created '
                        '(the exact production failure for DEV-57/ADA-34)')


if __name__ == '__main__':
    unittest.main()
