"""Player ask routing tests.

Hermetic: the model call (_post_openrouter_raw), the web fetches, the DB, and
the player session are all mocked or sandboxed so no live network/think tank is
touched. DB_PATH is redirected below so even an unmocked write lands in a
throwaway temp file.
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve

from _ask_helpers import _roster, _state, _search_tool_call, json_dumps

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-ask-test-')
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


class AskParking(unittest.TestCase):
    """The parked-ask drain: _pending_ask_drain_pass answers the oldest parked
    ask with the first agent that frees up (honoring a non-admin pin, never the
    admin), and _apply_pending_ask_results delivers the reply into the player
    inbox + email queue inside the sim's single read-modify-write. The drain
    runs the real _ask_core on its own thread via asyncio.run -- mocked here so
    no model/network is touched; the result is stashed in the same in-memory
    holder the sim pass consumes."""

    def setUp(self):
        serve._pending_ask_inflight.clear()
        with serve._pending_ask_results_lock:
            serve._pending_ask_results.clear()

    def _all_busy(self, state):
        for d in state['agentRoster']:
            if not d.get('isAdmin'):
                state['agents'][d['id']]['busy'] = True
                state['agents'][d['id']]['task'] = 't-x'

    def _parked(self, state, question='hi', agent_id=None):
        state.setdefault('_pendingAsks', []).append({
            'id': 'ask-111', 'question': question, 'location': None,
            'agentId': agent_id, 'ts': int(time.time() * 1000)})

    def _drain_and_wait(self, state, timeout=5.0):
        serve._pending_ask_drain_pass(state)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with serve._pending_ask_results_lock:
                if serve._pending_ask_results:
                    return list(serve._pending_ask_results.values())[0]
            time.sleep(0.02)
        return None

    def test_drain_skips_when_everyone_still_busy(self):
        s = _state()
        self._all_busy(s)
        self._parked(s)
        result = self._drain_and_wait(s, timeout=0.5)
        self.assertIsNone(result)  # nothing stashed -- no agent freed up yet
        self.assertEqual(serve._pending_ask_inflight, set())

    def test_drain_picks_first_freed_agent_and_honors_non_admin_pin(self):
        s = _state()
        self._all_busy(s)
        # Player pinned dax (non-admin) when everyone was busy; dax frees up.
        self._parked(s, agent_id='dax')
        s['agents']['dax']['busy'] = False
        s['agents']['dax']['task'] = None
        seen = {}
        async def fake_ask_core(state, question, agent_id_hint=None, location=None,
                                max_tokens=300, allow_admin_pin=False, allow_park=True,
                                approved_tools=None):
            seen['agent'] = agent_id_hint
            seen['allow_park'] = allow_park
            return {'reply': 'Here is your answer.', 'agent': agent_id_hint, 'tools': []}
        with unittest.mock.patch.object(serve, '_ask_core', side_effect=fake_ask_core):
            result = self._drain_and_wait(s)
        self.assertIsNotNone(result)
        self.assertEqual(seen['agent'], 'dax')  # the player's pin is honored
        self.assertFalse(seen['allow_park'])  # the drain's inner call must not re-park
        self.assertEqual(result['agentId'], 'dax')

    def test_drain_never_uses_the_admin(self):
        s = _state()
        self._all_busy(s)
        self._parked(s, agent_id='maya')  # pin to the ADMIN is ignored
        s['agents']['ben']['busy'] = False
        s['agents']['ben']['task'] = None
        seen = {}
        async def fake_ask_core(state, question, agent_id_hint=None, location=None,
                                max_tokens=300, allow_admin_pin=False, allow_park=True,
                                approved_tools=None):
            seen['agent'] = agent_id_hint
            return {'reply': 'ok', 'agent': agent_id_hint, 'tools': []}
        with unittest.mock.patch.object(serve, '_ask_core', side_effect=fake_ask_core):
            result = self._drain_and_wait(s)
        self.assertIsNotNone(result)
        self.assertEqual(seen['agent'], 'ben')  # first eligible worker, not maya

    def test_apply_delivers_inbox_and_email_and_drops_ask(self):
        s = _state()
        self._parked(s, question='what should I wear?')
        serve._store_pending_ask_result({'askId': 'ask-111', 'reply': 'Wear shorts.',
                                         'agentId': 'ben', 'tools': []})
        delivered = serve._apply_pending_ask_results(s)
        self.assertEqual(delivered, 1)
        self.assertEqual(s.get('_pendingAsks'), [])  # the ask is dropped
        inbox = s.get('playerInbox') or []
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]['status'], 'answered')
        self.assertTrue(inbox[0]['queued'])
        self.assertEqual(inbox[0]['question'], 'what should I wear?')
        self.assertEqual(inbox[0]['answer'], 'Wear shorts.')
        self.assertEqual(inbox[0]['agentId'], 'ben')
        outbox = s.get('emailOutbox') or []
        self.assertTrue(any(e.get('kind') == 'ask_answered' for e in outbox))

    def test_apply_keeps_ask_parked_on_transient_busy(self):
        s = _state()
        self._parked(s)
        # The drain's inner call found everyone busy again -- the ask must stay
        # parked for the next pass, not be dropped.
        serve._store_pending_ask_result({'askId': 'ask-111',
                                         'error': 'No agent is free to answer right now. Try again shortly.',
                                         'status': 409})
        delivered = serve._apply_pending_ask_results(s)
        self.assertEqual(delivered, 0)
        self.assertEqual(len(s.get('_pendingAsks') or []), 1)
        self.assertEqual(s.get('playerInbox') or [], [])


class AdminAgentId(unittest.TestCase):
    def test_finds_the_admin(self):
        s = _state()
        s['agentRoster'][0]['isAdmin'] = True  # maya
        self.assertEqual(serve._admin_agent_id(s), 'maya')

    def test_no_admin_returns_none(self):
        s = _state()
        for d in s['agentRoster']:
            d['isAdmin'] = False
        self.assertIsNone(serve._admin_agent_id(s))
