"""Player clarify routing: player -> on-call agent -> KB-first -> completing agent.

The clarify endpoint answers a question about COMPLETED work (unlike a sprint,
which posts NEW work). Routing is DERIVED, not stored: product -> owning team's
current ON-CALL agent (whether they worked on it or not) answers KNOWLEDGE-BASE-
FIRST -- via the same Library search the agents themselves use -- and only falls
back to the completing agent if the on-call genuinely can't answer.

Two layers, hermetic:
  - sim.clarify_router_plan: the pure routing decision (no model, no DB).
  - the /api/intent/clarify endpoint: KB-first + in-character model call, with
    the model + KB search + DB state all mocked so no live village is touched.
The "DB state...mocked" claim only covered the READ side (get_state_from_db
is patched per-test); save_state_to_db and log_action were never mocked and
go straight to serve.py's real DB_PATH via TestClient(serve.app), so a write
during any of these tests landed in a real village.db. Found 2026-09-25 via a
live production village.db that picked up "player clarify" log rows after a
routine test run; DB_PATH is now redirected below so even an unmocked write
lands in a throwaway temp file.
"""

import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim  # noqa: E402
import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='village-clarify-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        VILLAGE_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _director(rid, is_admin=False, name=None, director='maya', **over):
    d = {'id': rid, 'name': name or rid.title(), 'role': 'admin' if is_admin else 'engineer',
         'isAdmin': is_admin, 'director': director}
    d.update(over)
    return d


def _state():
    roster = [
        _director('maya', is_admin=True, name='Maya', director='maya'),
        _director('ben', name='Ben'),
        _director('cora', name='Cora'),
        _director('dax', name='Dax'),
        _director('zia', name='Zia'),
    ]
    return {
        'agentRoster': roster,
        'agents': {a['id']: {'id': a['id'], 'name': a['name'], 'role': a['role'],
                             'offDuty': False, 'profile': {'mission': 'help the village'}}
                   for a in roster},
        'teams': [{'id': 'mayateam', 'directorId': 'maya', 'scrumMasterId': 'cora'}],
        'products': {'p1': {'id': 'p1', 'name': 'Parser', 'teamId': 'maya',
                            'nameKeeps': ['parser']}},
        'completedDeliverables': [
            {'id': 't1', 'title': 'Refactor the Parser', 'room': 'pressoffice',
             'agentId': 'dax', 'grade': 8.0, 'gradedAt': 1000, 'lastReviewed': '2026-09-01T00:00:00Z'},
            {'id': 't2', 'title': 'Parser docs', 'room': 'library',
             'agentId': 'ben', 'grade': 7.0, 'gradedAt': 2000, 'lastReviewed': '2026-09-05T00:00:00Z'},
        ],
        'sprints': {},
        'workQueue': [],
        'tasks': {},
    }


class ClarifyRouterPlanTests(unittest.TestCase):
    # --- pure routing decision (sim, no model, no DB) ---

    def test_routes_to_owning_team_on_call_excluding_scrum_master_and_admin(self):
        s = _state()
        plan = sim.clarify_router_plan(s, 'p1', 'how was the parser built?')
        self.assertIn(plan['onCall'], ('ben', 'dax', 'zia'))  # maya=admin, cora=scrum master
        self.assertEqual(plan['product'], s['products']['p1'])

    def test_completing_agent_is_most_recent_matching_deliverable(self):
        s = _state()
        plan = sim.clarify_router_plan(s, 'p1', 'about the parser')
        self.assertEqual(plan['completing'], 'ben')  # t2 gradedAt 2000 wins over t1's 1000

    def test_unknown_product_degrades_cleanly(self):
        plan = sim.clarify_router_plan(_state(), 'nope', 'any question')
        self.assertIsNone(plan['onCall'])
        self.assertIsNone(plan['completing'])
        self.assertEqual(plan['product'], {})

    def test_alias_match_via_name_keeps(self):
        s = _state()
        s['products']['p1']['name'] = 'Parsing Engine'  # id is p1, not 'parser'
        # A question about "parse" still matches because of the nameKeeps alias.
        self.assertEqual(sim.clarify_router_plan(s, 'p1', 'how does parsing work?')['completing'], 'ben')


class ClarifyInCharacterMessagesTests(unittest.TestCase):
    def test_messages_include_agent_identity_and_kb_block(self):
        agent = {'id': 'ben', 'name': 'Ben', 'role': 'Banking',
                 'profile': {'mission': 'keep the books straight'}}
        msgs = serve._clarify_in_character_messages(agent, [
            {'path': 'projects/p1/README.md', 'snippet': 'streams input'},
        ], 'Parser', 'how was it built?')
        self.assertIn('Ben', msgs[0]['content'])
        self.assertIn('Banking', msgs[0]['content'])
        self.assertIn('projects/p1/README.md', msgs[1]['content'])
        self.assertIn('how was it built?', msgs[1]['content'])

    def test_no_kb_hits_renders_an_empty_knowledge_marker(self):
        agent = {'id': 'ben', 'name': 'Ben', 'role': 'Banking', 'profile': {'mission': 'x'}}
        msgs = serve._clarify_in_character_messages(agent, [], 'Parser', 'q')
        self.assertIn('no relevant Library files', msgs[1]['content'])


class ClarifyEndpointTests(unittest.TestCase):
    """Endpoint via TestClient with a real player session, all live side effects
    (DB state, model, KB search) mocked so a real village is never touched."""

    def setUp(self):
        # Clarify shares the rate-limited ask lane (2026-09-28) -- clear the
        # bucket per test so this suite is hermetic regardless of order.
        serve._rate_limit_calls.pop(serve.ASK_LANE_RATE_LIMIT_KEY, None)

    def _client(self, state):
        from fastapi.testclient import TestClient
        c = TestClient(serve.app)
        sid = serve.create_session()
        c.cookies.set(serve.SESSION_COOKIE_NAME, sid)
        return c, unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state)

    def _post(self, client, patches, body):
        return client.post('/api/intent/clarify', json=body)

    def test_kb_first_answer_returned_without_escalation(self):
        s = _state()
        c, state_patch = self._client(s)
        fake = {'choices': [{'message': {'content': 'We streamed the input in two phases.'}}]}
        with state_patch, \
             unittest.mock.patch.object(serve, '_library_search_matches',
                                        return_value=[{'path': 'projects/p1/README.md', 'snippet': 'two phases'}]):
            with unittest.mock.patch.object(serve, '_call_openrouter_sync', return_value=fake) as mc:
                resp = self._post(c, None, {'productId': 'p1', 'question': 'how was it parsed?'})
        self.assertEqual(resp.status_code, 200)
        j = resp.json()
        self.assertIn('streamed', j['reply'])
        self.assertIn(j['onCall'], ('ben', 'dax', 'zia'))
        self.assertIsNone(j['escalatedTo'])
        # The model got the on-call's persona + the KB block.
        sent_msgs = mc.call_args.args[1]
        self.assertEqual(sent_msgs[0]['role'], 'system')
        self.assertIn('two phases', sent_msgs[1]['content'])  # the KB snippet is injected

    def test_on_call_cannot_answer_escalates_to_completing_agent(self):
        s = _state()
        # Make the completing agent the admin (maya) -- she landed the work but is
        # never in the on-call pool (admins excluded), so completing is always a
        # DIFFERENT agent than the on-call and escalation is deterministic.
        s['completedDeliverables'] = [
            {'id': 't1', 'title': 'Refactor the Parser', 'room': 'pressoffice',
             'agentId': 'maya', 'grade': 8.0, 'gradedAt': 1000, 'lastReviewed': '2026-09-01T00:00:00Z'}]
        c, state_patch = self._client(s)
        first = {'choices': [{'message': {'content': serve._CLARIFY_ESCALATE_TOKEN}}]}
        second = {'choices': [{'message': {'content': 'I landed that. It relies on the parser spec.'}}]}
        with state_patch, \
             unittest.mock.patch.object(serve, '_library_search_matches', return_value=[]):
            with unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                            side_effect=[first, second]) as mc:
                resp = self._post(c, None, {'productId': 'p1', 'question': 'q'})
        self.assertEqual(resp.status_code, 200)
        j = resp.json()
        self.assertEqual(j['escalatedTo'], 'maya')  # the completing agent
        self.assertEqual(mc.call_count, 2)
        # The second call names the completing agent as the speaker.
        self.assertIn('Maya', mc.call_args_list[1].args[1][0]['content'])

    def test_on_call_cannot_answer_and_no_completing_returns_graceful_note(self):
        s = _state()
        s['completedDeliverables'] = []  # nothing completed -> no completing agent
        c, state_patch = self._client(s)
        first = {'choices': [{'message': {'content': serve._CLARIFY_ESCALATE_TOKEN}}]}
        with state_patch, \
             unittest.mock.patch.object(serve, '_library_search_matches', return_value=[]):
            with unittest.mock.patch.object(serve, '_call_openrouter_sync', return_value=first) as mc:
                resp = self._post(c, None, {'productId': 'p1', 'question': 'q'})
        self.assertEqual(resp.status_code, 200)
        j = resp.json()
        self.assertEqual(mc.call_count, 1)  # no second call -- nobody to escalate to
        self.assertFalse(serve._CLARIFY_ESCALATE_TOKEN in (j['reply'] or ''))  # token never leaks

    def test_missing_body_fields_are_rejected(self):
        c, state_patch = self._client(_state())
        with state_patch:
            r = c.post('/api/intent/clarify', json={'productId': 'p1'})
            self.assertEqual(r.status_code, 400)
            r2 = c.post('/api/intent/clarify', json={'question': 'q'})
            self.assertEqual(r2.status_code, 400)

    def test_no_on_call_agent_returns_404(self):
        c, state_patch = self._client(_state())
        s = {'completedDeliverables': [], 'sprints': {}, 'teams': [], 'products': {}, 'workQueue': []}
        with unittest.mock.patch('serve.get_state_from_db', return_value=s):
            resp = c.post('/api/intent/clarify', json={'productId': 'p1', 'question': 'q'})
        self.assertEqual(resp.status_code, 404)


if __name__ == '__main__':
    unittest.main(verbosity=2)