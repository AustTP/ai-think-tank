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


class TelegramBridge(unittest.TestCase):
    """The Telegram poll loop's actual decision logic (_telegram_process_update),
    tested without a real asyncio loop or network call -- getUpdates/sendMessage
    are real outbound calls to Telegram, never mocked here beyond that boundary."""

    def setUp(self):
        self._cm = unittest.mock.patch.multiple(
            serve, TELEGRAM_ALLOWED_CHAT_IDS={'111'})
        self._cm.start()
        self.addCleanup(self._cm.stop)

    def _update(self, chat_id=111, text='hi'):
        return {'update_id': 1, 'message': {'chat': {'id': chat_id}, 'text': text}}

    def test_non_allowlisted_chat_is_ignored(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db') as gsd:
            outcome = asyncio.run(serve._telegram_process_update(self._update(chat_id=999)))
        self.assertIsNone(outcome)
        gsd.assert_not_called()  # never even reads state for a sender we don't trust

    def test_message_with_no_text_is_ignored(self):
        update = {'update_id': 1, 'message': {'chat': {'id': 111}}}  # e.g. a sticker
        outcome = asyncio.run(serve._telegram_process_update(update))
        self.assertIsNone(outcome)

    def test_no_state_replies_with_a_clear_message_not_a_crash(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=None):
            outcome = asyncio.run(serve._telegram_process_update(self._update()))
        self.assertEqual(outcome[0], '111')
        self.assertIn('not up right now', outcome[1])

    def test_allowlisted_chat_reaches_the_admin_via_ask_core(self):
        # Theo routing: _telegram_process_update now goes through
        # _route_player_request's classifier before reaching _ask_core -- mock
        # the classifier to the 'ask' lane so this stays a hermetic unit test
        # of the dispatch wiring, not a real Jev call.
        s = _state()
        s['agentRoster'][0]['isAdmin'] = True  # maya
        seen = {}
        async def fake_ask_core(state, question, agent_id_hint=None, location=None, max_tokens=300, allow_admin_pin=False, allow_park=True, session_id=None, approved_tools=None):
            seen['question'] = question
            seen['agent_id_hint'] = agent_id_hint
            seen['allow_admin_pin'] = allow_admin_pin
            return {'reply': 'All quiet.', 'agent': 'maya', 'tools': []}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s), \
             unittest.mock.patch.object(serve, '_lane_decider', return_value='ask'), \
             unittest.mock.patch.object(serve, '_ask_core', side_effect=fake_ask_core):
            outcome = asyncio.run(serve._telegram_process_update(self._update(text='status?')))
        self.assertEqual(outcome, ('111', 'All quiet.'))
        self.assertEqual(seen['question'], 'status?')
        self.assertEqual(seen['agent_id_hint'], 'maya')  # pinned to the ADMIN, not round-robin
        self.assertTrue(seen['allow_admin_pin'])  # the internal ask lane trusts this pin

    def test_multi_intent_request_routes_to_the_senior_director_escape_hatch(self):
        # Multi-category request: a message bundling several unrelated asks
        # ("fix the outage AND set up a nightly check AND how tall is Everest?")
        # cannot be force-fit into one lane -- the classifier routes it to the
        # 'unclear' lane, whose handler pins the senior-most free authority (the
        # admin here) so a HUMAN director splits it instead of the think tank
        # silently executing one half and dropping the rest.
        s = _state()
        s['agentRoster'][0]['isAdmin'] = True  # maya
        seen = {}
        async def fake_ask_core(state, question, agent_id_hint=None, location=None, max_tokens=300, allow_admin_pin=False, allow_park=True, session_id=None, approved_tools=None):
            seen['question'] = question
            seen['agent_id_hint'] = agent_id_hint
            seen['allow_admin_pin'] = allow_admin_pin
            return {'reply': 'That is several things at once -- let me take them one at a time.', 'agent': 'maya', 'tools': []}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s), \
             unittest.mock.patch.object(serve, '_lane_decider', return_value='unclear'), \
             unittest.mock.patch.object(serve, '_ask_core', side_effect=fake_ask_core):
            outcome = asyncio.run(serve._telegram_process_update(
                self._update(text='Fix the outage, set up a nightly check, and how tall is Everest?')))
        self.assertEqual(outcome[0], '111')
        # The 'unclear' lane answers via the senior-most free authority, never
        # dispatching to a team or round-robin worker.
        self.assertEqual(seen['agent_id_hint'], 'maya')
        self.assertTrue(seen['allow_admin_pin'])

    def test_unrecognized_lane_falls_back_to_unclear_never_crashes(self):
        # Defense-in-depth: a classifier returning a bogus/unknown lane id
        # (e.g. a stale lane removed from _ROUTING_LANES) must degrade to the
        # 'unclear' handler -- the request is still answered by a human
        # director, never dropped or 500'd.
        s = _state()
        s['agentRoster'][0]['isAdmin'] = True  # maya
        seen = {}
        async def fake_ask_core(state, question, agent_id_hint=None, location=None, max_tokens=300, allow_admin_pin=False, allow_park=True, session_id=None, approved_tools=None):
            seen['question'] = question
            seen['agent_id_hint'] = agent_id_hint
            return {'reply': 'Let me get a director on that.', 'agent': 'maya', 'tools': []}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s), \
             unittest.mock.patch.object(serve, '_lane_decider', return_value='totally-bogus-lane'), \
             unittest.mock.patch.object(serve, '_ask_core', side_effect=fake_ask_core):
            outcome = asyncio.run(serve._telegram_process_update(self._update(text='weird request')))
        self.assertEqual(outcome, ('111', 'Let me get a director on that.'))
        self.assertEqual(seen['question'], 'weird request')
        self.assertEqual(seen['agent_id_hint'], 'maya')  # unclear fallback: the free authority

    def test_unclear_lane_prefers_senior_most_director_over_admin(self):
        # Routing reconciliation: an 'unclear' lane ask is a director's
        # judgment call -- the SENIOR-MOST director answers first, the admin
        # only as fallback. With a senior-most director (nora, a non-admin
        # director with no own director) free, she must be pinned, not maya.
        s = _state()
        s['agentRoster'][0]['isAdmin'] = True  # maya
        s['agentRoster'].append({'id': 'nora', 'name': 'Nora', 'role': 'Personnel',
                                 'isDirector': True, 'director': None})
        s['agents']['nora'] = {'id': 'nora', 'name': 'Nora', 'role': 'Personnel',
                               'offDuty': False, 'busy': False, 'task': None,
                               'pairWith': None, 'profile': {'mission': 'run personnel'}}
        seen = {}
        async def fake_ask_core(state, question, agent_id_hint=None, location=None, max_tokens=300, allow_admin_pin=False, allow_park=True, session_id=None, approved_tools=None):
            seen['agent_id_hint'] = agent_id_hint
            return {'reply': 'Let me split that up for you.', 'agent': 'nora', 'tools': []}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s), \
             unittest.mock.patch.object(serve, '_lane_decider', return_value='unclear'), \
             unittest.mock.patch.object(serve, '_ask_core', side_effect=fake_ask_core):
            outcome = asyncio.run(serve._telegram_process_update(
                self._update(text='fix the outage and also how tall is Everest?')))
        self.assertEqual(outcome[0], '111')
        self.assertEqual(seen['agent_id_hint'], 'nora',
                         'senior-most director, not the admin, answers the unclear lane')
        # Busy senior-most director -> the admin is the fallback authority.
        s2 = _state()
        s2['agentRoster'][0]['isAdmin'] = True
        s2['agentRoster'].append({'id': 'nora', 'name': 'Nora', 'role': 'Personnel',
                                  'isDirector': True, 'director': None})
        s2['agents']['nora'] = {'id': 'nora', 'name': 'Nora', 'role': 'Personnel',
                                'offDuty': False, 'busy': True, 'task': None,
                                'pairWith': None, 'profile': {'mission': 'run personnel'}}
        seen2 = {}
        async def fake_ask_core2(state, question, agent_id_hint=None, location=None, max_tokens=300, allow_admin_pin=False, allow_park=True, session_id=None, approved_tools=None):
            seen2['agent_id_hint'] = agent_id_hint
            return {'reply': 'On it.', 'agent': 'maya', 'tools': []}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=s2), \
             unittest.mock.patch.object(serve, '_lane_decider', return_value='unclear'), \
             unittest.mock.patch.object(serve, '_ask_core', side_effect=fake_ask_core2):
            asyncio.run(serve._telegram_process_update(self._update(text='weird bundle')))
        self.assertEqual(seen2['agent_id_hint'], 'maya',
                         'busy senior-most director defers to the admin fallback')


class ReasoningStarvationRecovery(unittest.TestCase):
    """A reasoning model burning the WHOLE max_tokens budget on hidden
    thinking returns content:null + finish_reason=length -- the live incident
    behind the ask lane's "couldn't settle" replies (Ada's/Ben's truncated
    operator feedback at max_tokens=600 on anthropic/claude-haiku-5.5 with
    reasoning capped to 150). Both call layers must retry ONCE with reasoning
    off, merging the wasted first call's cost so spend stays exact."""

    def _starved(self, cost=0.001):
        return {'choices': [{'message': {'content': None, 'reasoning': 'thinking...'},
                             'finish_reason': 'length'}],
                'usage': {'cost': cost}}

    def _good(self, cost=0.002, text='real answer'):
        return {'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}],
                'usage': {'cost': cost}}

    def test_plain_call_retries_with_reasoning_off_and_merges_cost(self):
        starved, good = self._starved(), self._good()
        with unittest.mock.patch.object(serve, '_openrouter_chat_post',
                                        side_effect=[starved, good]) as post:
            out = serve._call_openrouter_sync('anthropic/claude-haiku-5.5',
                                              [{'role': 'user', 'content': 'q'}], 300)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[1].kwargs.get('reasoning')
                         or post.call_args_list[1].args[0].get('reasoning'),
                         {'enabled': False})
        self.assertEqual(out['choices'][0]['message']['content'], 'real answer')
        self.assertAlmostEqual(out['usage']['cost'], 0.003,
                               msg='wasted starved call cost must be merged into the winner')

    def test_tool_loop_layer_retries_starved_response(self):
        starved, good = self._starved(), self._good(text='settled via tool loop')
        with unittest.mock.patch.object(serve, '_openrouter_chat_post',
                                        side_effect=[starved, good]) as post:
            out = serve._post_openrouter_raw('anthropic/claude-haiku-5.5',
                                             [{'role': 'user', 'content': 'q'}], max_tokens=600)
        self.assertEqual(post.call_count, 2)
        retry_body = post.call_args_list[1].args[0]
        self.assertEqual(retry_body['reasoning'], {'enabled': False})
        # The retry must preserve the original request's tools/tool_choice.
        self.assertEqual(retry_body.get('tools'), None)
        self.assertEqual(out['choices'][0]['message']['content'], 'settled via tool loop')

    def test_healthy_response_is_not_retried(self):
        with unittest.mock.patch.object(serve, '_openrouter_chat_post',
                                        side_effect=[self._good()]) as post:
            out = serve._call_openrouter_sync('m', [{'role': 'user', 'content': 'q'}], 300)
        self.assertEqual(post.call_count, 1, 'a healthy reply must not pay for a second call')
        self.assertEqual(out['choices'][0]['message']['content'], 'real answer')

    def test_persistent_starvation_keeps_original_without_raising(self):
        # Both recovery attempts starve too (e.g. a mandatory-reasoning model
        # that rejects disabled reasoning): keep the original document, never
        # raise -- callers already have their own empty-content handling.
        with unittest.mock.patch.object(serve, '_openrouter_chat_post',
                                        side_effect=[self._starved(), self._starved(0.0005),
                                                     self._starved(0.0005)]):
            out = serve._call_openrouter_sync('z-ai/glm-5.3-flash',
                                              [{'role': 'user', 'content': 'q'}], 300)
        self.assertIsNone(out['choices'][0]['message']['content'])

    def test_retry_network_failure_keeps_original(self):
        with unittest.mock.patch.object(serve, '_openrouter_chat_post',
                                        side_effect=[self._starved(), RuntimeError('net down')]):
            out = serve._call_openrouter_sync('m', [{'role': 'user', 'content': 'q'}], 300)
        self.assertIsNone(out['choices'][0]['message']['content'])

    def test_starved_detection_shape(self):
        self.assertTrue(serve._reasoning_starved(self._starved()))
        self.assertFalse(serve._reasoning_starved(self._good()))
        self.assertFalse(serve._reasoning_starved(
            {'choices': [{'message': {'content': '', 'tool_calls': [{'id': 't'}]},
                          'finish_reason': 'length'}]}),
            'a tool_call turn is a normal settle, not starvation')
        self.assertFalse(serve._reasoning_starved({'choices': []}))
