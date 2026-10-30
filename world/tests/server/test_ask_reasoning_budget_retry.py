"""2026-10-30 Tier-1 finding: reasoning models can spend the ENTIRE
max_tokens budget on their reasoning field and return content=null with
finish_reason="length" (observed live on anthropic/claude-haiku-5.5). The
empty reply counted as a failure and repeated failures opened the low
tier's circuit breaker, leaving the ask lane unable to answer at all. The
loop now retries ONCE with a raised budget when length+reasoning is the
cause."""
import os
import sys
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve  # noqa: E402


def _resp(content=None, finish='stop', reasoning=None, tool_calls=None):
    msg = {'role': 'assistant'}
    if content is not None:
        msg['content'] = content
    if reasoning is not None:
        msg['reasoning'] = reasoning
    if tool_calls is not None:
        msg['tool_calls'] = tool_calls
    return {'choices': [{'finish_reason': finish, 'message': msg}],
            'usage': {'cost': 0.0}}


def test_reasoning_exhaustion_retries_once_with_raised_budget():
    calls = []
    with unittest.mock.patch.object(
            serve, '_post_openrouter_raw',
            side_effect=lambda model, msgs, tools=None, max_tokens=None, tool_choice=None:
                calls.append(max_tokens) or (
                    _resp(None, 'length', reasoning='thought long and hard')
                    if len(calls) == 1 else _resp('the actual answer'))), \
         unittest.mock.patch.object(serve, 'record_model_result', lambda *a, **k: None), \
         unittest.mock.patch.object(serve, '_accrue_spend', lambda *a, **k: None), \
         unittest.mock.patch.object(serve, '_accrue_task_spend', lambda *a, **k: None):
        out = serve._call_agent_tool_loop('test-model', [{'role': 'user', 'content': 'q'}],
                                          tools=[], execute_tool=lambda n, a: '',
                                          max_tokens=300)
    assert out == 'the actual answer'
    assert calls == [300, 300 * 3 + 1024], calls


def test_vacuous_empty_without_reasoning_gets_no_retry():
    calls = []
    with unittest.mock.patch.object(
            serve, '_post_openrouter_raw',
            side_effect=lambda *a, **k: calls.append(1) or _resp(None, 'stop')), \
         unittest.mock.patch.object(serve, 'record_model_result', lambda *a, **k: None), \
         unittest.mock.patch.object(serve, '_accrue_spend', lambda *a, **k: None):
        out = serve._call_agent_tool_loop('test-model', [{'role': 'user', 'content': 'q'}],
                                          tools=[], execute_tool=lambda n, a: '',
                                          max_tokens=300)
    assert out == '' or out is None
    assert len(calls) == 1, 'no reasoning present -> no retry'


def test_retry_that_yields_tool_calls_falls_through_to_execution():
    executed = []
    responses = [
        _resp(None, 'length', reasoning='thinking...'),
        _resp(tool_calls=[{'id': 'c1', 'function': {'name': 'probe_tool',
                                                    'arguments': '{"x": 1}'}}]),
        _resp('final answer after the tool ran'),
    ]
    with unittest.mock.patch.object(
            serve, '_post_openrouter_raw', side_effect=lambda *a, **k: responses.pop(0)), \
         unittest.mock.patch.object(serve, 'record_model_result', lambda *a, **k: None), \
         unittest.mock.patch.object(serve, '_accrue_spend', lambda *a, **k: None), \
         unittest.mock.patch.object(serve, '_accrue_task_spend', lambda *a, **k: None):
        out = serve._call_agent_tool_loop(
            'test-model', [{'role': 'user', 'content': 'q'}], tools=[],
            execute_tool=lambda n, a: executed.append((n, a)) or 'tool result',
            max_tokens=200, max_iterations=2)
    assert executed == [('probe_tool', {'x': 1})]
    assert out == 'final answer after the tool ran'
