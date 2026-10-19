"""Serialize budgeted requests and bound calls using configured token prices."""
import asyncio
import json
import math
import weakref
from contextlib import asynccontextmanager

import story_tracking

_LOOPS = weakref.WeakKeyDictionary()


@asynccontextmanager
async def guard(state, task_id):
    task = state.get('tasks', {}).get(task_id)
    if task:
        story_tracking.stamp(state, task)
    request_id = task.get('requestId') or task.get('workOrderId') if task else (
        task_id[len('request/'):] if task_id.startswith('request/') else None)
    request = next((r for r in state.get('backlogRequests', []) if r.get('id') == request_id), {})
    parent = story_tracking.root(state, task) if task else {}
    if request.get('requestBudgetUsd') is None and not parent.get('budgetUsd'):
        yield
        return
    locks = _LOOPS.setdefault(asyncio.get_running_loop(), {})
    lock_key = request_id if request.get('requestBudgetUsd') is not None else 'story/' + str(parent.get('id'))
    async with locks.setdefault(lock_key, asyncio.Lock()):
        yield


def allowance(state, ledger, task_id, messages, requested_tokens, prices):
    status = story_tracking.budget_status(state, ledger, task_id)
    if status['scope'] not in ('request', 'story'):
        return requested_tokens
    if not prices or len(prices) != 2 or any(not isinstance(p, (int, float)) or not math.isfinite(p) or p < 0 for p in prices):
        raise ValueError('A budgeted request requires known model token pricing.')
    prompt_rate, completion_rate = prices
    # UTF-8 bytes plus framing is a conservative text-token upper estimate.
    input_bound = len(json.dumps(messages, ensure_ascii=False).encode('utf-8')) + 256
    remaining = status['budgetUsd'] - status['used'] - input_bound * prompt_rate
    if remaining <= 0:
        raise ValueError('The request budget cannot cover this prompt.')
    output = requested_tokens if completion_rate == 0 else min(requested_tokens, int(remaining / completion_rate))
    if output < min(64, requested_tokens):
        raise ValueError('The request budget cannot cover a useful response.')
    return output
