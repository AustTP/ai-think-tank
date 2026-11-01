"""GET /api/board.md -- the board as one Markdown dump (2026-11-01).

The export exists so the operator can paste live world state into any
external model session. Pinned here: the section structure (in progress /
awaiting review / parked / queued / recently completed with the worker's
note / pending large requests), the newline-flattening that keeps one messy
field from breaking the Markdown, the player-session guard, and the fact
that parked queue cards surface in Parked (not Queued).
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402

_STATE = {
    'agents': {'w1': {'id': 'w1', 'name': 'W One'},
               'w2': {'id': 'w2', 'name': 'W Two'}},
    'tasks': {
        'task-1': {'id': 'task-1', 'title': 'Research the market',
                   'status': 'working', 'assignedTo': 'w1', 'room': 'library',
                   'teamId': 'ops', 'phase': 'prep', 'taskType': 'research'},
        'task-2': {'id': 'task-2', 'title': 'Build the portal',
                   'status': 'needs_review', 'assignedTo': 'w2',
                   'teamId': 'ops', 'phase': 'build'},
        'task-3': {'id': 'task-3', 'title': 'Ship the walkthrough',
                   'status': 'done', 'assignedTo': 'w1',
                   'note': 'Shipped it and linked the demo.',
                   'updatedAt': 5},
        'task-4': {'id': 'task-4', 'title': 'Old idea', 'status': 'done'},
    },
    'workQueue': [
        {'title': 'Parked experiment', 'lane': 'parking-lot', 'notBefore': 1},
        {'title': 'Write the page', 'lane': 'build', 'notBefore': 1,
         'phase': 'build', 'sequence': 0, 'dependsOn': 'task-1'},
    ],
    'backlogRequests': [
        {'id': 'wrq-1', 'status': 'pending', 'goal': 'Build the onboarding portal',
         'filedBy': 'admin', 'teamId': 'ops'},
    ],
}


def _state():
    import copy
    return copy.deepcopy(_STATE)


class BoardMarkdownTest(unittest.TestCase):
    def setUp(self):
        from starlette.testclient import TestClient
        self._patch = unittest.mock.patch.object(
            serve, 'get_state_from_db', side_effect=lambda: _state())
        self._patch.start()
        self._session = unittest.mock.patch.object(
            serve, '_require_player_session', side_effect=lambda request: True)
        self._session.start()
        # The require_login middleware (190) runs before the route and checks
        # the session cookie itself; the board dump is behind BOTH layers.
        self._mw = unittest.mock.patch.object(serve, 'verify_session',
                                              side_effect=lambda sid: True)
        self._mw.start()
        self.client = TestClient(serve.app)

    def tearDown(self):
        self._patch.stop()
        self._session.stop()
        self._mw.stop()

    def test_requires_player_session(self):
        # Unpatch BOTH layers: the require_login middleware and the handler's
        # own player-session guard. An unauthenticated caller gets 401 from
        # whichever layer sees them first -- the guarantee is the refusal.
        self._session.stop()
        self._mw.stop()
        with self.client:
            resp = self.client.get('/api/board.md')
        self.assertEqual(resp.status_code, 401)
        self._session.start()
        self._mw.start()

    def test_markdown_has_every_board_section(self):
        with self.client:
            resp = self.client.get('/api/board.md')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers['content-type'].startswith('text/markdown'))
        text = resp.text
        self.assertIn('# Think tank board', text)
        self.assertIn('## In progress (1)', text)
        self.assertIn('Research the market** -- W One -- library', text)
        self.assertIn('phase prep', text)
        self.assertIn('## Awaiting peer review (1)', text)
        self.assertIn('Build the portal', text)
        self.assertIn('## Parked (1) -- never auto-assigned', text)
        self.assertIn('Parked experiment', text)
        self.assertIn('## Queued (1 due now)', text)
        self.assertIn('Write the page', text)
        self.assertIn('serial #0', text)
        self.assertIn('depends on task-1', text)
        self.assertIn('## Recently completed', text)
        self.assertIn('> Shipped it and linked the demo.', text)
        self.assertIn('## Pending large requests (1)', text)
        self.assertIn('Build the onboarding portal', text)
        # The parked QUEUE card must not leak into the Queued section twice.
        queued_section = text.split('## Queued')[1].split('## Recently')[0]
        self.assertNotIn('Parked experiment', queued_section)

    def test_newlines_in_fields_cannot_break_the_markdown(self):
        state = _state()
        state['tasks']['task-1']['title'] = 'Research\n## injected heading\nmore'
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        side_effect=lambda: state):
            with self.client:
                resp = self.client.get('/api/board.md')
        text = resp.text
        self.assertIn('Research / ## injected heading / more', text)
        self.assertNotIn('Research\n## injected', text)


if __name__ == '__main__':
    unittest.main()
