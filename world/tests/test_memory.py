"""Tests for the conversation memory (STM + LTM) wired into the Telegram ask path.

Ports the magi framework's memory architecture onto the think tank's own
SQLite DB (world/memory.py). STM stores each ask turn per chat session and
injects recent context into the next ask -- the fix for the admin failing to
answer a follow-up question. LTM distills durable facts/preferences from the
conversation and recalls the relevant ones on each ask.

Hermetic: DB redirected to a throwaway temp dir; the model-call boundary
(_call_agent_tool_loop) and the /api/chat loopback (_chat_call) are mocked so
nothing is ever billed.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402
import memory  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-memory-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _state():
    roster = [
        {'id': 'maya', 'name': 'Maya', 'role': 'admin', 'isAdmin': True},
        {'id': 'ben', 'name': 'Ben', 'role': 'engineer'},
        {'id': 'cora', 'name': 'Cora', 'role': 'engineer'},
    ]
    return {
        'agentRoster': roster,
        'agents': {a['id']: {'id': a['id'], 'name': a['name'], 'role': a['role'],
                             'offDuty': False, 'busy': False, 'task': None,
                             'pairWith': None,
                             'profile': {'mission': 'help the think tank'}}
                   for a in roster},
        'workQueue': [],
        'tasks': {},
        'products': {},
        'completedDeliverables': [],
    }


def _clear_memory():
    """Wipe the three memory tables so a test class starts from an empty
    session store (the DB is module-wide, shared across all classes here)."""
    memory._ensure_tables()
    with serve._db() as conn:
        conn.execute('DELETE FROM stm_messages')
        conn.execute('DELETE FROM stm_sessions')
        conn.execute('DELETE FROM ltm_memories')


class StmStore(unittest.TestCase):
    SESSION = '111'

    def setUp(self):
        _clear_memory()

    def test_append_and_history_round_trip(self):
        memory.stm_append(self.SESSION, 'user', 'hello')
        memory.stm_append(self.SESSION, 'assistant', 'hi there')
        memory.stm_append(self.SESSION, 'user', 'and the second one?')
        summary, history = memory.stm_history(self.SESSION)
        self.assertEqual(summary, '')
        self.assertEqual([(h['role'], h['content']) for h in history],
                         [('user', 'hello'), ('assistant', 'hi there'),
                          ('user', 'and the second one?')])

    def test_prune_folds_oldest_turns_into_summary(self):
        with unittest.mock.patch.object(serve, 'STM_MAX_TURNS', 6):
            for i in range(8):
                memory.stm_append(self.SESSION, 'user', f'question {i}')
                memory.stm_append(self.SESSION, 'assistant', f'answer {i}')
        summary, history = memory.stm_history(self.SESSION)
        # Only the newest STM_MAX_TURNS messages survive as raw turns.
        self.assertEqual(len(history), 6)
        self.assertEqual(history[0]['content'], 'question 5')
        self.assertEqual(history[-1]['content'], 'answer 7')
        # The pruned turns were folded into a rolling recap, not lost.
        self.assertIn('question 0', summary)
        self.assertIn('answer 4', summary)

    def test_disabled_memory_stores_nothing(self):
        with unittest.mock.patch.object(serve, 'MEMORY_ENABLED', False):
            memory.stm_append(self.SESSION, 'user', 'hello')
            summary, history = memory.stm_history(self.SESSION)
        self.assertEqual((summary, history), ('', []))

    def test_null_session_is_a_noop(self):
        memory.stm_append(None, 'user', 'hello')
        self.assertEqual(memory.stm_history(None), ('', []))
        self.assertIsNone(memory.memory_context(None, 'hello'))


class LtmRecall(unittest.TestCase):
    SESSION = '111'

    def setUp(self):
        _clear_memory()
        memory._insert_memories(self.SESSION, [
            ('preference', 'The player prefers email over phone'),
            ('decision', 'We agreed to use the observatory room for research'),
            ('fact', 'The production server runs on port 8010'),
        ])

    def test_recall_returns_relevant_memories(self):
        out = memory.ltm_recall('what did we decide about the observatory?')
        self.assertIsNotNone(out)
        self.assertIn('observatory', out)
        self.assertIn('decision', out)

    def test_recall_none_for_unrelated_question(self):
        self.assertIsNone(memory.ltm_recall('what is the weather in paris?'))

    def test_recall_respects_limit(self):
        with unittest.mock.patch.object(serve, 'LTM_MAX_RECALL', 2):
            out = memory.ltm_recall('server port email preference observatory research')
        self.assertIsNotNone(out)
        self.assertLessEqual(out.count('\n') + 1, 2)

    def test_insert_dedupes_identical_content(self):
        memory._insert_memories(self.SESSION, [
            ('preference', 'The player prefers email over phone')])
        out = memory.ltm_recall('email phone preference')
        self.assertEqual(out.count('email over phone'), 1)


class LtmParse(unittest.TestCase):
    def test_parse_valid_json_list(self):
        parsed = memory._parse_memories(
            '{"memories": [{"type": "preference", "content": "likes short replies"}, '
            '{"type": "fact", "content": "runs the village"}]}')
        self.assertEqual(parsed, [('preference', 'likes short replies'),
                                  ('fact', 'runs the village')])

    def test_parse_fenced_json(self):
        parsed = memory._parse_memories(
            '```json\n{"memories": [{"type": "decision", "content": "ship on fridays"}]}\n```')
        self.assertEqual(parsed, [('decision', 'ship on fridays')])

    def test_parse_bad_reply_fails_closed_to_empty(self):
        self.assertEqual(memory._parse_memories('not json at all'), [])
        self.assertEqual(memory._parse_memories(''), [])
        self.assertEqual(memory._parse_memories(None), [])

    def test_parse_unknown_type_defaults_to_fact(self):
        parsed = memory._parse_memories(
            '{"memories": [{"type": "bogus", "content": "x"}]}')
        self.assertEqual(parsed, [('fact', 'x')])


class Cleanup(unittest.TestCase):
    SESSION = '222'

    def setUp(self):
        _clear_memory()

    def test_expired_session_pruned_ltm_survives(self):
        memory.stm_append(self.SESSION, 'user', 'hello')
        memory._insert_memories(self.SESSION, [('fact', 'durable fact that must survive')])
        old = time.time() - 10000
        with serve._db() as conn:
            conn.execute('UPDATE stm_sessions SET updated_at = ? WHERE session_id = ?',
                         (old, self.SESSION))
        with unittest.mock.patch.object(serve, 'STM_TTL_HOURS', 1):
            deleted = memory.cleanup_expired_sessions()
        self.assertEqual(deleted, 1)
        self.assertEqual(memory.stm_history(self.SESSION), ('', []))
        # LTM is global and long-term -- session expiry never deletes it.
        self.assertIsNotNone(memory.ltm_recall('durable fact'))


class AskCoreMemory(unittest.TestCase):
    """The real bug: a follow-up Telegram message must be answered in the
    context of the earlier exchange. Exercises the real _ask_core with the
    model-call boundary mocked (no network, no spend)."""

    def setUp(self):
        _clear_memory()

    def _run_ask(self, state, question, session_id=None):
        captured = {}
        with unittest.mock.patch.object(serve, '_resolve_model_tier',
                                        return_value='test-model'), \
             unittest.mock.patch.object(serve, '_call_agent_tool_loop',
                                        side_effect=lambda *a, **k: captured.__setitem__(
                                            'messages', list(a[1])) or 'ok'):
            result = asyncio.run(serve._ask_core(
                state, question, agent_id_hint='ben', session_id=session_id))
        self.assertEqual(result['reply'], 'ok')
        return captured['messages']

    def test_followup_is_answered_in_context(self):
        state = _state()
        first = self._run_ask(state, 'How tall is Everest?', session_id='333')
        # Fresh question: no prior history, "fresh question" phrasing.
        self.assertEqual(len(first), 2)
        self.assertIn("Player's fresh question", first[1]['content'])

        second = self._run_ask(state, 'and what about K2?', session_id='333')
        # Follow-up: the earlier exchange is injected before the new question,
        # and the system prompt now says the conversation is ongoing.
        self.assertIn("Player's latest message", second[-1]['content'])
        self.assertIn('How tall is Everest?', second[1]['content'])
        self.assertIn('ok', second[2]['content'])
        self.assertTrue(any('ongoing conversation' in m['content'] for m in second))

    def test_stateless_ask_stores_nothing(self):
        state = _state()
        self._run_ask(state, 'stateless question', session_id=None)
        self.assertEqual(memory.stm_history('333'), ('', []))

    def test_parked_ask_still_records_the_user_turn(self):
        state = _state()
        import sim as _sim
        with unittest.mock.patch.object(_sim, '_eligible_candidates', return_value=[]), \
             unittest.mock.patch.object(serve, '_resolve_model_tier',
                                        return_value='test-model'):
            result = asyncio.run(serve._ask_core(
                state, 'queue me please', agent_id_hint='ben', session_id='444'))
        self.assertTrue(result.get('queued'))
        summary, history = memory.stm_history('444')
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['content'], 'queue me please')


class RouteForwardsSession(unittest.TestCase):
    def test_ask_lane_receives_the_chat_session(self):
        state = _state()
        seen = {}
        async def fake_ask_core(state, question, agent_id_hint=None, location=None,
                                max_tokens=300, allow_admin_pin=False, allow_park=True,
                                session_id=None):
            seen['session_id'] = session_id
            seen['allow_admin_pin'] = allow_admin_pin
            return {'reply': 'ok', 'agent': 'maya', 'tools': []}
        with unittest.mock.patch.object(serve, '_lane_decider', return_value='ask'), \
             unittest.mock.patch.object(serve, '_ask_core', side_effect=fake_ask_core):
            result = asyncio.run(serve._route_player_request(state, 'status?', session_id='111'))
        self.assertEqual(result['reply'], 'ok')
        self.assertEqual(seen['session_id'], '111')
        self.assertTrue(seen['allow_admin_pin'])


if __name__ == '__main__':
    unittest.main()