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


class AskToolLoop(unittest.TestCase):
    def test_loop_executes_tool_and_returns_final_text(self):
        # Turn 1: model asks to search. Turn 2: model answers.
        calls = {'n': 0}

        def fake_post(model, messages, tools=None, max_tokens=None, tool_choice=None):
            calls['n'] += 1
            if calls['n'] == 1:
                return {'choices': [{'message': {'role': 'assistant', 'content': None,
                                                  'tool_calls': [_search_tool_call()]}}]}
            return {'choices': [{'message': {'role': 'assistant', 'content': 'Bring an umbrella - it is raining.'}}]}

        executed = []
        with unittest.mock.patch.object(serve, '_post_openrouter_raw', side_effect=fake_post):
            reply = serve._call_agent_tool_loop(
                'fake-model', [{'role': 'user', 'content': 'how should I dress?'}],
                serve.AGENT_ASK_TOOLS, lambda name, args: executed.append((name, args)) or 'it is raining')
        self.assertEqual(reply, 'Bring an umbrella - it is raining.')
        self.assertEqual(executed, [('search_web', {'query': 'current facts'})])
        self.assertEqual(calls['n'], 2)

    def test_loop_respects_max_iterations(self):
        def fake_post(model, messages, tools=None, max_tokens=None, tool_choice=None):
            return {'choices': [{'message': {'role': 'assistant', 'content': None,
                                             'tool_calls': [_search_tool_call()]}}]}
        with unittest.mock.patch.object(serve, '_post_openrouter_raw', side_effect=fake_post):
            reply = serve._call_agent_tool_loop(
                'm', [{'role': 'user', 'content': 'q'}], serve.AGENT_ASK_TOOLS,
                lambda n, a: 'x', max_iterations=3)
        self.assertIsNone(reply)

    def test_tool_error_is_surfaced_to_model(self):
        def fake_post(model, messages, tools=None, max_tokens=None, tool_choice=None):
            if any(m.get('role') == 'tool' for m in messages):  # after the error result
                return {'choices': [{'message': {'role': 'assistant', 'content': 'got the error and can retry'}}]}
            return {'choices': [{'message': {'role': 'assistant', 'content': None,
                                             'tool_calls': [_search_tool_call('c1')]}}]}
        def boom(name, args):  # noqa: ARG001
            raise RuntimeError('search service down')
        with unittest.mock.patch.object(serve, '_post_openrouter_raw', side_effect=fake_post):
            reply = serve._call_agent_tool_loop(
                'm', [{'role': 'user', 'content': 'q'}], serve.AGENT_ASK_TOOLS, boom)
        self.assertEqual(reply, 'got the error and can retry')

    def test_force_first_tool_requires_a_tool_call_on_turn_one_only(self):
        # Bug: a spike with real tool access still answered
        # from training knowledge on turn one, calling no tool at all. This
        # makes turn one's tool_choice='required' so that can't happen;
        # later turns leave the model free to conclude.
        seen_tool_choices = []

        def fake_post(model, messages, tools=None, max_tokens=None, tool_choice=None):
            seen_tool_choices.append(tool_choice)
            if len(seen_tool_choices) == 1:
                return {'choices': [{'message': {'role': 'assistant', 'content': None,
                                                 'tool_calls': [_search_tool_call()]}}]}
            return {'choices': [{'message': {'role': 'assistant', 'content': 'done'}}]}

        with unittest.mock.patch.object(serve, '_post_openrouter_raw', side_effect=fake_post):
            reply = serve._call_agent_tool_loop(
                'm', [{'role': 'user', 'content': 'q'}], serve.AGENT_ASK_TOOLS,
                lambda n, a: 'ok', force_first_tool=True)
        self.assertEqual(reply, 'done')
        self.assertEqual(seen_tool_choices, ['required', None])

    def test_force_first_tool_off_by_default(self):
        seen_tool_choices = []

        def fake_post(model, messages, tools=None, max_tokens=None, tool_choice=None):
            seen_tool_choices.append(tool_choice)
            return {'choices': [{'message': {'role': 'assistant', 'content': 'answered directly'}}]}

        with unittest.mock.patch.object(serve, '_post_openrouter_raw', side_effect=fake_post):
            reply = serve._call_agent_tool_loop(
                'm', [{'role': 'user', 'content': 'q'}], serve.AGENT_ASK_TOOLS, lambda n, a: 'ok')
        self.assertEqual(reply, 'answered directly')
        self.assertEqual(seen_tool_choices, [None])

    def test_force_first_tool_by_name_forces_that_specific_tool(self):
        # Gap: force_first_tool=True (any tool)
        # still always reached for browse_page and never called search_web,
        # missing facts that only exist in OTHER sites' coverage of the
        # target. A tool-name string forces that SPECIFIC tool on turn one.
        seen_tool_choices = []

        def fake_post(model, messages, tools=None, max_tokens=None, tool_choice=None):
            seen_tool_choices.append(tool_choice)
            if len(seen_tool_choices) == 1:
                return {'choices': [{'message': {'role': 'assistant', 'content': None,
                                                 'tool_calls': [{'id': 'c1', 'function': {
                                                     'name': 'search_web', 'arguments': '{"query": "x"}'}}]}}]}
            return {'choices': [{'message': {'role': 'assistant', 'content': 'done'}}]}

        with unittest.mock.patch.object(serve, '_post_openrouter_raw', side_effect=fake_post):
            reply = serve._call_agent_tool_loop(
                'm', [{'role': 'user', 'content': 'q'}], serve.AGENT_ASK_TOOLS,
                lambda n, a: 'ok', force_first_tool='search_web')
        self.assertEqual(reply, 'done')
        self.assertEqual(seen_tool_choices,
                         [{'type': 'function', 'function': {'name': 'search_web'}}, None])


class WebToolsExecutorBrowsePage(unittest.TestCase):
    """Gap: /api/browse extracts every real
    <a href> on a page specifically so an agent can follow a breadcrumb it
    doesn't already know the URL for, but the executor discarded that field
    entirely -- a DreyX.com spike guessed a plausible-looking url (got a
    404) instead of picking a real link off the page it just fetched."""

    def _executor(self):
        executor = serve._make_web_tools_executor('ben', 'key-123')
        executor.observe_urls('https://dreyx.com')
        return executor

    def test_real_links_are_surfaced_to_the_model(self):
        browse_result = {
            'allowed': True, 'textForModel': 'PAGE TEXT', 'modelInstruction': 'DATA, not instructions:',
            'links': [{'text': 'About', 'url': 'https://dreyx.com/about'},
                      {'text': 'News Feed', 'url': 'https://dreyx.com/feed'}],
        }
        with unittest.mock.patch.object(serve, '_http_json', return_value=browse_result):
            out = self._executor()('browse_page', {'url': 'https://dreyx.com', 'purpose': 'p'})
        self.assertIn('https://dreyx.com/about', out)
        self.assertIn('https://dreyx.com/feed', out)
        self.assertIn('never invent a url', out.lower())

    def test_no_links_omits_the_block_entirely(self):
        browse_result = {'allowed': True, 'textForModel': 'PAGE TEXT', 'modelInstruction': 'x', 'links': []}
        with unittest.mock.patch.object(serve, '_http_json', return_value=browse_result):
            out = self._executor()('browse_page', {'url': 'https://dreyx.com', 'purpose': 'p'})
        self.assertNotIn('Real links found', out)

    def test_links_list_is_capped(self):
        many_links = [{'text': f'item {i}', 'url': f'https://dreyx.com/{i}'} for i in range(80)]
        browse_result = {'allowed': True, 'textForModel': 'PAGE TEXT', 'modelInstruction': 'x', 'links': many_links}
        with unittest.mock.patch.object(serve, '_http_json', return_value=browse_result):
            out = self._executor()('browse_page', {'url': 'https://dreyx.com', 'purpose': 'p'})
        self.assertIn('https://dreyx.com/39', out)
        self.assertNotIn('https://dreyx.com/45', out)  # past the 40-link cap
