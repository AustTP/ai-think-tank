"""Tests for the conversation memory (STM + LTM) wired into the Telegram ask path.

Covers world/memory.py's conversation memory on the think tank's own SQLite
DB. STM stores each ask turn per chat session and
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
            ('preference', 'The player prefers email over phone', 0.9, 'user_stated'),
            ('decision', 'We agreed to use the observatory room for research', 0.9, 'user_stated'),
            ('fact', 'The production server runs on port 8010', 0.9, 'user_stated'),
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
            ('preference', 'The player prefers email over phone', 0.9, 'user_stated')])
        out = memory.ltm_recall('email phone preference')
        self.assertEqual(out.count('email over phone'), 1)

    def test_recall_filters_below_min_confidence(self):
        memory._insert_memories(self.SESSION, [
            ('fact', 'the vault door is coded 4518', 0.1, 'user_stated')])
        out = memory.ltm_recall('vault door code')
        self.assertIsNone(out)

    def test_recall_decays_old_memories(self):
        memory._insert_memories(self.SESSION, [
            ('fact', 'the orchard gate sticks in winter', 0.9, 'user_stated')])
        old = time.time() - (2 * 86400)
        with serve._db() as conn:
            conn.execute('UPDATE ltm_memories SET created_at = ? WHERE session_id = ?',
                         (old, self.SESSION))
        with unittest.mock.patch.object(serve, 'LTM_MAX_AGE_DAYS', 1):
            out = memory.ltm_recall('orchard gate winter')
        self.assertIsNone(out)

    def test_recall_boosts_user_stated_over_inferred(self):
        memory._insert_memories(self.SESSION, [
            ('fact', 'the greenhouse faces north', 0.9, 'user_stated'),
            ('fact', 'the greenhouse faces north', 0.6, 'agent_inferred')])
        out = memory.ltm_recall('greenhouse facing')
        self.assertIsNotNone(out)
        first = out.splitlines()[0]
        self.assertNotIn('REDACTED', first)


class RedactAndTrust(unittest.TestCase):
    SESSION = '333'

    def setUp(self):
        _clear_memory()

    def test_redact_secrets_scrubs_credentials(self):
        out = memory._redact_secrets(
            'my password is hunter2 and the api key is sk-proj-1234567890abcdef')
        self.assertIn('[REDACTED:PASSWORD_STATEMENT]', out)
        self.assertIn('[REDACTED:API_KEY]', out)
        self.assertNotIn('hunter2', out)
        self.assertNotIn('sk-proj', out)

    def test_redact_secrets_scrubs_bearer_and_jwt(self):
        out = memory._redact_secrets(
            'Authorization: Bearer abcdef123456 and eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature')
        self.assertIn('[REDACTED:BEARER_TOKEN]', out)
        self.assertIn('[REDACTED:JWT]', out)

    def test_insert_redacts_before_store(self):
        memory._insert_memories(self.SESSION, [
            ('fact', 'the api key is sk-abc12345678901234567', 0.9, 'user_stated')])
        out = memory.ltm_recall('api key')
        self.assertIsNotNone(out)
        self.assertNotIn('sk-abc12345678901234567', out)
        self.assertIn('[REDACTED:API_KEY]', out)


class LtmParse(unittest.TestCase):
    def test_parse_valid_json_list(self):
        parsed = memory._parse_memories(
            '{"memories": [{"type": "preference", "content": "likes short replies"}, '
            '{"type": "fact", "content": "runs the village"}]}')
        self.assertEqual(parsed, [('preference', 'likes short replies', 0.6, 'agent_inferred'),
                                  ('fact', 'runs the village', 0.6, 'agent_inferred')])

    def test_parse_fenced_json(self):
        parsed = memory._parse_memories(
            '```json\n{"memories": [{"type": "decision", "content": "ship on fridays"}]}\n```')
        self.assertEqual(parsed, [('decision', 'ship on fridays', 0.6, 'agent_inferred')])

    def test_parse_bad_reply_fails_closed_to_empty(self):
        self.assertEqual(memory._parse_memories('not json at all'), [])
        self.assertEqual(memory._parse_memories(''), [])
        self.assertEqual(memory._parse_memories(None), [])

    def test_parse_unknown_type_defaults_to_fact(self):
        parsed = memory._parse_memories(
            '{"memories": [{"type": "bogus", "content": "x"}]}')
        self.assertEqual(parsed, [('fact', 'x', 0.6, 'agent_inferred')])

    def test_parse_trust_fields_and_fallbacks(self):
        parsed = memory._parse_memories(
            '{"memories": [{"type": "fact", "content": "user said it", "confidence": 0.95, "source": "user_stated"}, '
            '{"type": "fact", "content": "inferred", "confidence": "bogus", "source": "bogus"}, '
            '{"type": "fact", "content": "clamped", "confidence": 7}]}')
        self.assertEqual(parsed, [
            ('fact', 'user said it', 0.95, 'user_stated'),
            ('fact', 'inferred', 0.6, 'agent_inferred'),
            ('fact', 'clamped', 1.0, 'agent_inferred')])


class Cleanup(unittest.TestCase):
    SESSION = '222'

    def setUp(self):
        _clear_memory()

    def test_expired_session_pruned_ltm_survives(self):
        memory.stm_append(self.SESSION, 'user', 'hello')
        memory._insert_memories(self.SESSION, [('fact', 'durable fact that must survive', 0.9, 'user_stated')])
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


class Consolidation(unittest.TestCase):
    SESSION = '555'

    def setUp(self):
        _clear_memory()
        memory.stm_append(self.SESSION, 'user', 'hello')

    def _bump_turns(self, n):
        with serve._db() as conn:
            conn.execute('UPDATE stm_sessions SET turn_count = ? WHERE session_id = ?',
                         (n, self.SESSION))

    def test_find_clusters_groups_near_duplicates(self):
        mems = [
            (1, 'fact', 'The player works at the observatory north building'),
            (2, 'fact', 'The player is employed at the observatory north building'),
            (3, 'preference', 'Prefers short emails in the morning'),
        ]
        clusters = memory._find_consolidation_clusters(mems, max_clusters=4)
        self.assertEqual(len(clusters), 1)
        self.assertEqual([m[0] for m in clusters[0]], [1, 2])

    def test_consolidate_merges_near_duplicates(self):
        memory._insert_memories(self.SESSION, [
            ('fact', 'The player works at the observatory north building', 0.9, 'user_stated'),
            ('fact', 'The player is employed at the observatory north building', 0.9, 'user_stated'),
            ('preference', 'Prefers short emails in the morning', 0.9, 'user_stated'),
        ])
        self._bump_turns(20)
        reply = ('{"merged": {"type": "fact", "content": '
                 '"The player works at the observatory in the north building", "confidence": 0.9}}')
        with unittest.mock.patch.object(memory, '_chat_call', return_value=reply):
            merged = memory.ltm_consolidate(self.SESSION)
        self.assertEqual(merged, 1)
        out = memory.ltm_recall('observatory north building work')
        self.assertIsNotNone(out)
        self.assertLessEqual(out.count('- ['), 2)
        with serve._db() as conn:
            active = conn.execute('SELECT COUNT(*) FROM ltm_memories WHERE session_id = ? AND active = 1',
                                  (self.SESSION,)).fetchone()[0]
        self.assertEqual(active, 2)

    def test_consolidate_keeps_distinct_when_model_says_no(self):
        memory._insert_memories(self.SESSION, [
            ('fact', 'The player works at the observatory north building', 0.9, 'user_stated'),
            ('fact', 'The player is employed at the observatory north building', 0.9, 'user_stated'),
        ])
        self._bump_turns(20)
        with unittest.mock.patch.object(memory, '_chat_call', return_value='{"merged": false}'):
            merged = memory.ltm_consolidate(self.SESSION)
        self.assertEqual(merged, 0)
        with serve._db() as conn:
            active = conn.execute('SELECT COUNT(*) FROM ltm_memories WHERE session_id = ? AND active = 1',
                                  (self.SESSION,)).fetchone()[0]
        self.assertEqual(active, 2)

    def test_consolidate_respects_cadence(self):
        memory._insert_memories(self.SESSION, [
            ('fact', 'The player works at the observatory north building', 0.9, 'user_stated'),
            ('fact', 'The player is employed at the observatory north building', 0.9, 'user_stated'),
        ])
        self._bump_turns(5)
        merged = memory.ltm_consolidate(self.SESSION)
        self.assertEqual(merged, 0)

    def test_consolidate_disabled_is_noop(self):
        with unittest.mock.patch.object(serve, 'LTM_CONSOLIDATE_ENABLED', False):
            self.assertEqual(memory.ltm_consolidate(self.SESSION), 0)
            self.assertFalse(memory.ltm_consolidate_due(self.SESSION))


class EnsureTablesMigrations(unittest.TestCase):
    def setUp(self):
        _clear_memory()

    def _columns(self, table):
        with serve._db() as conn:
            return {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}

    def test_stm_sessions_migration_adds_consolidate_turn(self):
        with serve._db() as conn:
            conn.execute('DROP TABLE IF EXISTS stm_sessions')
            conn.execute('''CREATE TABLE stm_sessions (
                session_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                turn_count INTEGER NOT NULL DEFAULT 0,
                token_count INTEGER NOT NULL DEFAULT 0,
                last_extract_turn INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )''')
        memory._ensure_tables()
        self.assertIn('last_consolidate_turn', self._columns('stm_sessions'))

    def test_ltm_memories_migration_adds_trust_columns(self):
        with serve._db() as conn:
            conn.execute('DROP TABLE IF EXISTS ltm_memories')
            conn.execute('''CREATE TABLE ltm_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                mem_type TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )''')
        memory._ensure_tables()
        cols = self._columns('ltm_memories')
        for col in ('confidence', 'source_type', 'fact_checked'):
            self.assertIn(col, cols)

    def test_ensure_tables_swallows_db_errors(self):
        with unittest.mock.patch.object(serve, '_db',
                                        side_effect=RuntimeError('boom')):
            memory._ensure_tables()


class StmFailureEdges(unittest.TestCase):
    SESSION = '111'

    def setUp(self):
        _clear_memory()

    def test_history_fails_closed_to_empty(self):
        with unittest.mock.patch.object(serve, '_db',
                                        side_effect=RuntimeError('boom')):
            self.assertEqual(memory.stm_history(self.SESSION), ('', []))

    def test_append_fails_closed_to_none(self):
        with unittest.mock.patch.object(serve, '_db',
                                        side_effect=RuntimeError('boom')):
            self.assertIsNone(memory.stm_append(self.SESSION, 'user', 'hi'))


class EnforceLimitsEdges(unittest.TestCase):
    SESSION = '111'

    def setUp(self):
        _clear_memory()

    def test_turn_budget_with_no_messages_is_a_noop(self):
        with serve._db() as conn:
            conn.execute(
                'INSERT OR IGNORE INTO stm_sessions (session_id, summary, '
                'turn_count, token_count, last_extract_turn, created_at, '
                'updated_at) VALUES (?, ?, 5, 0, 0, ?, ?)',
                (self.SESSION, '', time.time(), time.time()))
        with unittest.mock.patch.object(serve, 'STM_MAX_TURNS', 2):
            memory._enforce_limits(self.SESSION)
        self.assertEqual(memory.stm_history(self.SESSION), ('', []))

    def test_token_budget_prunes_oldest_turn(self):
        with unittest.mock.patch.object(serve, 'STM_MAX_TOKENS', 4):
            memory.stm_append(self.SESSION, 'user', 'first message here')
            memory.stm_append(self.SESSION, 'assistant', 'second message here')
        summary, history = memory.stm_history(self.SESSION)
        self.assertEqual([h['content'] for h in history], ['second message here'])
        self.assertIn('first message here', summary)

    def test_summary_is_capped(self):
        with unittest.mock.patch.object(serve, 'STM_MAX_TURNS', 1):
            for i in range(8):
                memory.stm_append(
                    self.SESSION, 'user', f'we decided {"x" * 189} turn {i}')
        summary, _ = memory.stm_history(self.SESSION)
        self.assertEqual(len(summary), memory._SUMMARY_CAP_CHARS)

    def test_enforce_limits_fails_closed(self):
        with unittest.mock.patch.object(serve, '_db',
                                        side_effect=RuntimeError('boom')):
            memory._enforce_limits(self.SESSION)


class SummaryUpgradeEdges(unittest.TestCase):
    SESSION = '111'

    def setUp(self):
        _clear_memory()

    def _set_summary(self, value):
        memory.stm_append(self.SESSION, 'user', 'hello')
        with serve._db() as conn:
            conn.execute('UPDATE stm_sessions SET summary = ? WHERE session_id = ?',
                         (value, self.SESSION))

    def test_rule_based_summary_of_empty_text(self):
        self.assertEqual(
            memory._rule_based_summary(''),
            'Context continues from earlier in this conversation.')

    def test_spawn_upgrade_swallows_thread_errors(self):
        with unittest.mock.patch.object(serve, 'STM_MAX_TURNS', 1), \
             unittest.mock.patch.object(memory.threading, 'Thread',
                                        side_effect=RuntimeError('boom')):
            memory.stm_append(self.SESSION, 'user', 'one')
            memory.stm_append(self.SESSION, 'user', 'two')

    def test_upgrade_worker_missing_session(self):
        memory._summary_upgrade_worker('no-such-session')

    def test_upgrade_worker_replaces_recap(self):
        self._set_summary('old recap worth condensing')
        with unittest.mock.patch.object(memory, '_chat_call',
                                        return_value='new recap'):
            memory._summary_upgrade_worker(self.SESSION)
        self.assertEqual(memory.stm_history(self.SESSION)[0], 'new recap')

    def test_upgrade_worker_fails_closed(self):
        self._set_summary('old recap worth condensing')
        with unittest.mock.patch.object(memory, '_chat_call',
                                        side_effect=RuntimeError('boom')):
            memory._summary_upgrade_worker(self.SESSION)
        self.assertEqual(memory.stm_history(self.SESSION)[0],
                         'old recap worth condensing')


class LtmRecallEdges(unittest.TestCase):
    def setUp(self):
        _clear_memory()

    def test_empty_question_is_none(self):
        self.assertIsNone(memory.ltm_recall(''))

    def test_zero_limit_is_none(self):
        self.assertIsNone(memory.ltm_recall('vault door', limit=0))

    def test_recall_fails_closed_on_db_error(self):
        with unittest.mock.patch.object(serve, '_db',
                                        side_effect=RuntimeError('boom')):
            self.assertIsNone(memory.ltm_recall('vault door'))


class LtmExtractLaunch(unittest.TestCase):
    SESSION = '111'

    def setUp(self):
        _clear_memory()

    def test_null_session_is_none(self):
        self.assertIsNone(memory.ltm_maybe_extract(None))

    def test_extract_cadence_disabled(self):
        with unittest.mock.patch.object(serve, 'LTM_EXTRACT_EVERY_TURNS', 0):
            self.assertIsNone(memory.ltm_maybe_extract(self.SESSION))

    def test_launch_spawns_workers(self):
        memory.stm_append(self.SESSION, 'user', 'hello')
        with serve._db() as conn:
            conn.execute(
                'UPDATE stm_sessions SET turn_count = 20, '
                'last_extract_turn = 0, last_consolidate_turn = 0 '
                'WHERE session_id = ?', (self.SESSION,))
        with unittest.mock.patch.object(memory, '_ltm_extract_worker'), \
             unittest.mock.patch.object(memory, '_ltm_consolidate_worker'):
            self.assertIsNone(memory.ltm_maybe_extract(self.SESSION))

    def test_launch_fails_closed(self):
        with unittest.mock.patch.object(memory, '_session_row',
                                        side_effect=RuntimeError('boom')):
            self.assertIsNone(memory.ltm_maybe_extract(self.SESSION))


class LtmExtractWorker(unittest.TestCase):
    SESSION = '111'

    def setUp(self):
        _clear_memory()

    def _turns(self):
        with serve._db() as conn:
            return conn.execute('SELECT turn_count, last_extract_turn '
                                  'FROM stm_sessions WHERE session_id = ?',
                                  (self.SESSION,)).fetchone()

    def test_fresh_session_has_nothing_to_extract(self):
        memory._ltm_extract_worker(self.SESSION)
        self.assertEqual(self._turns(), (0, 0))

    def test_worker_with_no_new_messages_marks_turn(self):
        memory.stm_append(self.SESSION, 'user', 'hello')
        with serve._db() as conn:
            conn.execute('UPDATE stm_sessions SET turn_count = 5 '
                         'WHERE session_id = ?', (self.SESSION,))
            conn.execute('DELETE FROM stm_messages')
        memory._ltm_extract_worker(self.SESSION)
        self.assertEqual(self._turns(), (5, 5))

    def test_worker_distills_new_turns_into_ltm(self):
        memory.stm_append(self.SESSION, 'user', 'the vault code is 4518')
        memory.stm_append(self.SESSION, 'assistant', 'noted')
        with serve._db() as conn:
            conn.execute('UPDATE stm_sessions SET turn_count = 5 '
                         'WHERE session_id = ?', (self.SESSION,))
        reply = '{"memories": [{"type": "fact", "content": "the vault code is 4518"}]}'
        with unittest.mock.patch.object(memory, '_chat_call',
                                        return_value=reply):
            memory._ltm_extract_worker(self.SESSION)
        self.assertIn('4518', memory.ltm_recall('vault code'))
        self.assertEqual(self._turns(), (5, 5))

    def test_worker_fails_closed_on_chat_error(self):
        memory.stm_append(self.SESSION, 'user', 'hello')
        with serve._db() as conn:
            conn.execute('UPDATE stm_sessions SET turn_count = 5 '
                         'WHERE session_id = ?', (self.SESSION,))
        with unittest.mock.patch.object(memory, '_chat_call',
                                        side_effect=RuntimeError('boom')):
            memory._ltm_extract_worker(self.SESSION)
        self.assertEqual(self._turns(), (5, 0))


class ParseMemoriesEdges(unittest.TestCase):
    def test_parse_plain_list(self):
        self.assertEqual(
            memory._parse_memories('[{"type": "fact", "content": "x"}]'),
            [('fact', 'x', 0.6, 'agent_inferred')])

    def test_parse_scalar_json_is_empty(self):
        self.assertEqual(memory._parse_memories('"just a string"'), [])

    def test_parse_skips_non_dict_items(self):
        self.assertEqual(
            memory._parse_memories('{"memories": ["not-a-dict"]}'), [])

    def test_parse_skips_empty_content(self):
        self.assertEqual(
            memory._parse_memories('{"memories": [{"type": "fact", "content": ""}]}'),
            [])


class InsertMemoriesEdges(unittest.TestCase):
    SESSION = '111'

    def setUp(self):
        _clear_memory()

    def test_per_session_cap_deactivates_oldest(self):
        with unittest.mock.patch.object(serve, 'LTM_MAX_PER_SESSION', 2):
            memory._insert_memories(self.SESSION, [
                ('fact', 'alpha one', 0.9, 'user_stated'),
                ('fact', 'beta two', 0.9, 'user_stated'),
                ('fact', 'gamma three', 0.9, 'user_stated')])
        with serve._db() as conn:
            rows = conn.execute('SELECT active FROM ltm_memories '
                                  'WHERE session_id = ? ORDER BY id',
                                  (self.SESSION,)).fetchall()
        self.assertEqual([r[0] for r in rows], [0, 1, 1])

    def test_insert_fails_closed_on_db_error(self):
        with unittest.mock.patch.object(serve, '_db',
                                        side_effect=RuntimeError('boom')):
            memory._insert_memories(self.SESSION, [
                ('fact', 'alpha one', 0.9, 'user_stated')])


class ConsolidationClusterEdges(unittest.TestCase):
    def test_cluster_skips_consumed_members(self):
        mems = [
            (1, 'fact', 'The player works at the observatory north building'),
            (2, 'preference', 'Prefers short emails in the morning'),
            (3, 'fact', 'The player is employed at the observatory north building'),
        ]
        clusters = memory._find_consolidation_clusters(mems, max_clusters=4)
        self.assertEqual(len(clusters), 1)
        self.assertEqual([m[0] for m in clusters[0]], [1, 3])

    def test_cluster_skips_empty_word_sets(self):
        mems = [(1, 'fact', 'the a an'), (2, 'fact', 'the a an')]
        self.assertEqual(memory._find_consolidation_clusters(mems), [])

    def test_cluster_count_is_capped(self):
        mems = [
            (1, 'fact', 'observatory north building work research'),
            (2, 'fact', 'observatory north building study research'),
            (3, 'fact', 'greenhouse south garden plant water'),
            (4, 'fact', 'greenhouse south garden soil water'),
            (5, 'fact', 'library east shelf book read'),
            (6, 'fact', 'library east shelf book page'),
        ]
        clusters = memory._find_consolidation_clusters(mems, max_clusters=2)
        self.assertEqual(len(clusters), 2)


class ConsolidateDueEdges(unittest.TestCase):
    SESSION = '111'

    def setUp(self):
        _clear_memory()

    def test_null_session_is_not_due(self):
        self.assertFalse(memory.ltm_consolidate_due(None))

    def test_cadence_disabled_is_not_due(self):
        with unittest.mock.patch.object(serve, 'LTM_CONSOLIDATE_EVERY_TURNS', 0):
            self.assertFalse(memory.ltm_consolidate_due(self.SESSION))

    def test_session_below_cadence_is_not_due(self):
        memory.stm_append(self.SESSION, 'user', 'hello')
        self.assertFalse(memory.ltm_consolidate_due(self.SESSION))

    def test_due_check_fails_closed(self):
        with unittest.mock.patch.object(serve, '_db',
                                        side_effect=RuntimeError('boom')):
            self.assertFalse(memory.ltm_consolidate_due(self.SESSION))


class ConsolidateWorkerEdges(unittest.TestCase):
    def test_worker_on_missing_session_is_noop(self):
        memory._ltm_consolidate_worker('no-such-session')

    def test_worker_fails_closed(self):
        with unittest.mock.patch.object(memory, 'ltm_consolidate',
                                        side_effect=RuntimeError('boom')):
            memory._ltm_consolidate_worker('111')


class ConsolidateEdges(unittest.TestCase):
    SESSION = '111'

    def setUp(self):
        _clear_memory()
        memory.stm_append(self.SESSION, 'user', 'hello')

    def _bump_turns(self, n):
        with serve._db() as conn:
            conn.execute('UPDATE stm_sessions SET turn_count = ? WHERE session_id = ?',
                         (n, self.SESSION))

    def _insert_near_duplicates(self):
        memory._insert_memories(self.SESSION, [
            ('fact', 'The player works at the observatory north building', 0.9, 'user_stated'),
            ('fact', 'The player is employed at the observatory north building', 0.9, 'user_stated')])
        self._bump_turns(20)

    def test_null_session_merges_nothing(self):
        self.assertEqual(memory.ltm_consolidate(None), 0)

    def test_cadence_disabled_merges_nothing(self):
        with unittest.mock.patch.object(serve, 'LTM_CONSOLIDATE_EVERY_TURNS', 0):
            self.assertEqual(memory.ltm_consolidate(self.SESSION), 0)

    def test_missing_session_merges_nothing(self):
        self.assertEqual(memory.ltm_consolidate('no-such-session'), 0)

    def test_scan_fails_closed(self):
        with unittest.mock.patch.object(serve, '_db',
                                        side_effect=RuntimeError('boom')):
            self.assertEqual(memory.ltm_consolidate(self.SESSION), 0)

    def test_merge_skipped_when_model_replies_none(self):
        self._insert_near_duplicates()
        with unittest.mock.patch.object(memory, '_chat_call', return_value=None):
            self.assertEqual(memory.ltm_consolidate(self.SESSION), 0)

    def test_merge_skipped_when_merged_is_not_a_dict(self):
        self._insert_near_duplicates()
        with unittest.mock.patch.object(
                memory, '_chat_call', return_value='{"merged": "not-a-dict"}'):
            self.assertEqual(memory.ltm_consolidate(self.SESSION), 0)

    def test_merge_with_unknown_type_defaults_to_fact(self):
        self._insert_near_duplicates()
        reply = '{"merged": {"type": "bogus", "content": "merged content here"}}'
        with unittest.mock.patch.object(memory, '_chat_call', return_value=reply):
            self.assertEqual(memory.ltm_consolidate(self.SESSION), 1)
        self.assertIn('[fact]', memory.ltm_recall('merged content'))

    def test_merge_skipped_when_content_is_empty(self):
        self._insert_near_duplicates()
        reply = '{"merged": {"type": "fact", "content": ""}}'
        with unittest.mock.patch.object(memory, '_chat_call', return_value=reply):
            self.assertEqual(memory.ltm_consolidate(self.SESSION), 0)

    def test_merge_with_bogus_confidence_uses_system_weight(self):
        self._insert_near_duplicates()
        reply = ('{"merged": {"type": "fact", "content": "merged content", '
                 '"confidence": "bogus"}}')
        with unittest.mock.patch.object(memory, '_chat_call', return_value=reply):
            self.assertEqual(memory.ltm_consolidate(self.SESSION), 1)
        with serve._db() as conn:
            row = conn.execute('SELECT confidence, source_type FROM ltm_memories '
                                 'WHERE session_id = ? AND content = ?',
                                 (self.SESSION, 'merged content')).fetchone()
        self.assertEqual(row, (0.8, 'system_generated'))

    def test_merge_fails_closed_on_chat_error(self):
        self._insert_near_duplicates()
        with unittest.mock.patch.object(memory, '_chat_call',
                                        side_effect=RuntimeError('boom')):
            self.assertEqual(memory.ltm_consolidate(self.SESSION), 0)


class CleanupEdges(unittest.TestCase):
    SESSION = '111'

    def setUp(self):
        _clear_memory()

    def test_disabled_memory_cleans_nothing(self):
        with unittest.mock.patch.object(serve, 'MEMORY_ENABLED', False):
            self.assertEqual(memory.cleanup_expired_sessions(), 0)

    def test_negative_ttl_cleans_nothing(self):
        with unittest.mock.patch.object(serve, 'STM_TTL_HOURS', -1):
            self.assertEqual(memory.cleanup_expired_sessions(), 0)

    def test_cleanup_fails_closed(self):
        with unittest.mock.patch.object(serve, '_db',
                                        side_effect=RuntimeError('boom')):
            self.assertEqual(memory.cleanup_expired_sessions(), 0)


class ChatCallEdges(unittest.TestCase):
    def test_missing_api_key_returns_none(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', None):
            self.assertIsNone(memory._chat_call('system', 'user'))

    def test_missing_state_returns_none(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=None):
            self.assertIsNone(memory._chat_call('system', 'user'))

    def test_missing_model_returns_none(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=_state()), \
             unittest.mock.patch.object(serve, '_admin_agent_id',
                                        return_value='maya'), \
             unittest.mock.patch.object(serve, 'get_or_create_agent_key',
                                        return_value='k'), \
             unittest.mock.patch.object(serve, '_resolve_model_tier',
                                        return_value=None):
            self.assertIsNone(memory._chat_call('system', 'user'))

    def test_successful_call_returns_reply(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=_state()), \
             unittest.mock.patch.object(serve, '_admin_agent_id',
                                        return_value='maya'), \
             unittest.mock.patch.object(serve, 'get_or_create_agent_key',
                                        return_value='k'), \
             unittest.mock.patch.object(serve, '_resolve_model_tier',
                                        return_value='test-model'), \
             unittest.mock.patch.object(serve, '_http_json',
                                        return_value={'reply': 'condensed'}):
            self.assertEqual(memory._chat_call('system', 'user'), 'condensed')

    def test_chat_call_fails_closed(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, 'get_state_from_db',
                                        side_effect=RuntimeError('boom')):
            self.assertIsNone(memory._chat_call('system', 'user'))


if __name__ == '__main__':
    unittest.main()