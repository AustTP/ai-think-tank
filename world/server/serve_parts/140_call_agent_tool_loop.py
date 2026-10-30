

def _sanitize_tool_call_arguments(tool_calls):
    """Return (calls, repaired_ids) with every tool call carrying syntactically
    valid JSON arguments. A tool call whose `arguments` string was cut off
    mid-emit (the reply hit max_tokens, so the JSON is unterminated) is kept
    -- the model still asked for that tool -- but its arguments are replaced
    with `{}` and its id is reported so the caller can skip executing it.

    Root cause this exists for (live, 2026-10-25): a low-tier model emitting
    `write_spike_report` with an ~8KB report inside the JSON `arguments`
    string ran out of tokens, leaving invalid JSON. The loop replayed that
    assistant message verbatim on the next turn, so the upstream rejected
    every following request with 400 "Assistant tool call function.arguments
    must be valid JSON" -- and because the transcript was checkpointed, every
    retry of the task resumed the same poisoned conversation and failed the
    same way. A malformed call must never be able to poison the transcript.
    """
    cleaned = []
    repaired_ids = set()
    for call in tool_calls or []:
        if not isinstance(call, dict):
            cleaned.append(call)
            continue
        raw = ((call.get('function') or {}).get('arguments'))
        valid = True
        if raw not in (None, ''):
            try:
                json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                valid = False
        if not valid:
            fixed = dict(call)
            fixed['function'] = dict(call.get('function') or {})
            fixed['function']['arguments'] = '{}'
            call_id = call.get('id')
            if call_id:
                repaired_ids.add(call_id)
            cleaned.append(fixed)
        else:
            cleaned.append(call)
    return cleaned, repaired_ids


def _model_error_detail(data):
    """The provider's error text when a response body carries `error` instead
    of choices, else ''. OpenRouter can answer with a body-level error object
    (and a 200) for an upstream failure; treating that as "the model returned
    no content" hid real 400s behind a generic "nothing usable" note."""
    if not isinstance(data, dict):
        return ''
    error = data.get('error')
    if not error:
        return ''
    if isinstance(error, dict):
        message = error.get('message') or error.get('code') or ''
        return str(message).strip()
    return str(error).strip()


def _call_agent_tool_loop(model, messages, tools, execute_tool, max_iterations=3, max_tokens=None,
                          service='__player_ask__', force_first_tool=False, return_transcript=False,
                          task_id=None, village_id=None, escalate_model=None,
                          conversation_policy=None, on_before_tools=None,
                          on_after_tools=None):
    """Agentic tool-calling loop for /api/intent/ask. Sends `messages` with
    `tools`; while the model replies with tool_calls, executes each via
    `execute_tool(name, args)`, appends the results back as `role:tool`
    messages, and re-invokes. Returns the FINAL assistant text (the last turn
    that carries no tool_calls) or None if the model never settled within
    `max_iterations`. `execute_tool` must return a plain string; tool error
    text inside that string is fine (the model sees it and can course-correct).

    `return_transcript=True` returns `(text_or_None, current_messages)`
    instead of just the text -- the plan/execute/synthesize spike pipeline
    Needs the FULL gathered tool-call transcript (every page
    actually fetched) to hand to a separate, stronger synthesis call, not
    just whatever short text this loop's own final turn happened to settle
    on.

    `force_first_tool` sets tool_choice on the FIRST call only (
    real bug: a spike given real browse_page/search_web access still just
    answered from training knowledge on its first turn, calling no tool at
    all -- confirmed via the action log showing zero browse calls for a
    DreyX.com investigation. Offering tools is not the same as the model
    choosing to use them. This makes at least one real lookup mandatory
    before the model may settle). `True` forces tool_choice='required' (any
    tool); a tool-name STRING forces that SPECIFIC tool (e.g. 'search_web') --
    added after a second real gap: even with force_first_tool=True, a spike
    always reached for browse_page on the target's own pages and never
    called search_web at all, missing facts that only exist in OTHER sites'
    coverage of the target (confirmed: a manual search surfaced named
    sources for DreyX that 3 rounds of browsing the site itself never found).
    Spikes always opt in (guaranteed investigation); the ask lane leaves this
    off for most questions (plenty genuinely need no tool at all) but opts in
    conditionally too, specifically when the question detectably
    wants x_trending_topics/search_linkedin_posts -- the exact same "offering
    a tool is not the same as using it" gap, confirmed for THIS lane
    too: a natural "what's trending on X" question, correctly routed to the
    ask lane, still reached for search_web instead of the real tool it was
    just given access to.

    `escalate_model` (optional): when a tool is forced on turn 1 and the
    model replies with NO tool call (prose instead of the tool), retry that
    same first turn with each stronger model in turn before giving up.
    Accepts a single model slug or an ordered list (e.g. [mid, high]) --
    each is tried only if the previous one also failed to call the tool.
    Model tiers refresh every ~30 minutes, so a static "always upgrade"
    would either waste money when the cheap model behaves or break silently
    when it doesn't -- this pays the upgrade cost ONLY on the failure path,
    adapting to whatever model currently sits in each tier.

    Tool results are EXTERNAL data once returned by the tool, so they carry
    the same nonce+HMAC injection boundary as any fetched web page: code later in
    the prompt stack that pipes tool output into another model must wrap first
    (see the endpoint). This function itself only plumbers role:tool messages.
    """
    from core.style import sanitize_text
    messages = _apply_plain_writing(messages)
    current_messages = list(messages)
    denied_tools = set()
    raw_execute_tool = execute_tool

    def execute_tool(name, args):
        try:
            allowed = True if conversation_policy is None else bool(conversation_policy(name))
        except Exception:
            allowed = False
        if not allowed or name in denied_tools:
            denied_tools.add(name)
            try:
                log_action('system', 'tool_policy_denied',
                           {'tool': name, 'taskId': task_id}, authorized=True)
            except Exception:
                pass
            return (f'{name} is a commit-tier tool and this conversation has not approved it. '
                    'This is a hard policy refusal, not a transient error. Do not retry it. '
                    'State plainly that the action needs player approval and finish the work '
                    'you can do without it.')
        return raw_execute_tool(name, args)

    for i in range(max_iterations):
        # Item 4 per-task gate inside the loop too: a spike that is already
        # over its task budget must not keep paying for further tool rounds.
        if task_id and _task_budget_exhausted(task_id):
            return (None, current_messages) if return_transcript else None
        tool_choice = None
        if force_first_tool and i == 0:
            tool_choice = ({'type': 'function', 'function': {'name': force_first_tool}}
                           if isinstance(force_first_tool, str) else 'required')
        data = _post_openrouter_raw(model, current_messages, tools=tools, max_tokens=max_tokens,
                                    tool_choice=tool_choice)
        error_detail = _model_error_detail(data)
        if error_detail:
            # A provider-side rejection is a real model-call failure, not an
            # empty answer. Raising here (instead of returning '') lets the
            # caller record the actual reason and keeps the failure out of the
            # misleading "returned nothing usable" bucket.
            record_model_result(model, success=False)
            raise RuntimeError(f'model call failed: {error_detail}')
        _accrue_spend(service, ((data or {}).get('usage') or {}).get('cost', 0.0), village_id=village_id)
        if task_id and isinstance(((data or {}).get('usage') or {}).get('cost'), (int, float)):
            task_cost = ((data or {}).get('usage') or {}).get('cost', 0.0)
            if task_cost:
                _accrue_task_spend(task_id, task_cost)
        choice = ((data or {}).get('choices') or [{}])[0]
        message = choice.get('message') or {}
        tool_calls = message.get('tool_calls') or []
        # A tool call cut off at the token limit has unterminated JSON
        # arguments. Repair it before it can enter the transcript, and skip
        # executing it so a half-emitted command never runs.
        truncated_ids = set()
        if tool_calls:
            tool_calls, truncated_ids = _sanitize_tool_call_arguments(tool_calls)
            message = dict(message)
            message['tool_calls'] = tool_calls
            if truncated_ids:
                try:
                    log_action('system', 'tool_call_truncated',
                               {'taskId': task_id, 'model': model,
                                'finishReason': choice.get('finish_reason'),
                                'count': len(truncated_ids)}, authorized=True)
                except Exception:
                    pass
        # Dynamic tier escalation: the forced tool did not fire. Models rotate
        # in the tiers every ~30 minutes, so instead of unconditionally
        # upgrading (waste when the cheap model behaves), retry this same
        # first turn with each stronger model in order -- low -> mid -> high,
        # stopping at the first one that actually calls the tool.
        if not tool_calls and tool_choice and escalate_model:
            chain = escalate_model if isinstance(escalate_model, (list, tuple)) else [escalate_model]
            for esc in chain:
                if not esc or esc == model:
                    continue
                model = esc
                data = _post_openrouter_raw(model, current_messages, tools=tools, max_tokens=max_tokens,
                                            tool_choice=tool_choice)
                error_detail = _model_error_detail(data)
                if error_detail:
                    record_model_result(model, success=False)
                    raise RuntimeError(f'model call failed: {error_detail}')
                _accrue_spend(service, ((data or {}).get('usage') or {}).get('cost', 0.0), village_id=village_id)
                if task_id and isinstance(((data or {}).get('usage') or {}).get('cost'), (int, float)):
                    task_cost = ((data or {}).get('usage') or {}).get('cost', 0.0)
                    if task_cost:
                        _accrue_task_spend(task_id, task_cost)
                choice = ((data or {}).get('choices') or [{}])[0]
                message = choice.get('message') or {}
                tool_calls = message.get('tool_calls') or []
                if tool_calls:
                    tool_calls, truncated_ids = _sanitize_tool_call_arguments(tool_calls)
                    message = dict(message)
                    message['tool_calls'] = tool_calls
                    break
        if not tool_calls:
            content = sanitize_text((message.get('content') or '').strip())
            if not content:
                # Reasoning-budget exhaustion (2026-10-30 Tier-1 finding):
                # reasoning models (anthropic/claude-haiku-5.5 observed live)
                # can spend the ENTIRE max_tokens on their reasoning field
                # and return content=null with finish_reason="length". The
                # empty reply then counts as a failure upstream and repeated
                # failures open the model's circuit breaker. Retry ONCE with
                # a raised budget before giving up; other empty shapes (e.g.
                # a provider returning a vacuous stop) stay failures.
                _finish_reason = ((data or {}).get('choices') or [{}])[0].get('finish_reason')
                _reasoning_text = str(message.get('reasoning')
                                      or message.get('reasoning_content') or '')
                if _finish_reason == 'length' and _reasoning_text.strip():
                    _retry_budget = (int(max_tokens) if max_tokens else 1024) * 3 + 1024
                    print(f'[ask-debug] empty content (finish_reason=length, '
                          f'reasoning present); retrying {model} once with '
                          f'max_tokens={_retry_budget}', flush=True)
                    data = _post_openrouter_raw(model, current_messages, tools=tools,
                                                max_tokens=_retry_budget,
                                                tool_choice=tool_choice)
                    error_detail = _model_error_detail(data)
                    if error_detail:
                        record_model_result(model, success=False)
                        raise RuntimeError(f'model call failed: {error_detail}')
                    _accrue_spend(service, ((data or {}).get('usage') or {}).get('cost', 0.0),
                                  village_id=village_id)
                    if task_id and isinstance(((data or {}).get('usage') or {}).get('cost'), (int, float)) \
                            and ((data or {}).get('usage') or {}).get('cost'):
                        _accrue_task_spend(task_id, ((data or {}).get('usage') or {}).get('cost'))
                    choice = ((data or {}).get('choices') or [{}])[0]
                    message = choice.get('message') or {}
                    tool_calls = message.get('tool_calls') or []
                    if tool_calls:
                        tool_calls, truncated_ids = _sanitize_tool_call_arguments(tool_calls)
                        message = dict(message)
                        message['tool_calls'] = tool_calls
                    else:
                        content = sanitize_text((message.get('content') or '').strip())
                if not content and not tool_calls:
                    print(f'[ask-debug] empty content for model={model} raw={json.dumps(data)[:2000]}', flush=True)
            if not tool_calls:
                current_messages.append(message)
                return (content, current_messages) if return_transcript else content
        # Executed results -> role:tool, back into the conversation.
        current_messages.append(message)
        if on_before_tools:
            try:
                on_before_tools(tool_calls, current_messages)
            except Exception:
                pass
        for call in tool_calls:
            try:
                name = (call.get('function') or {}).get('name')
                if call.get('id') in truncated_ids:
                    # The arguments were cut off at the token limit, so there
                    # is nothing safe to run. Tell the model exactly what
                    # happened and how to get it in under the cap.
                    output = (f'{name} was NOT run: its arguments were cut off at the token limit, '
                              'so the JSON never closed. Nothing happened. Re-send the call with a '
                              'shorter argument value, or split the content into smaller calls '
                              '(for a long report, write it in sections).')
                    current_messages.append({'role': 'tool', 'tool_call_id': call.get('id'),
                                             'content': output})
                    continue
                args_raw = ((call.get('function') or {}).get('arguments')) or '{}'
                try:
                    args = json.loads(args_raw) if args_raw else {}
                except json.JSONDecodeError:
                    args = {}
                output = execute_tool(name, args)
            except Exception as e:  # noqa: BLE001 - surface tool failure to the model
                output = f'__TOOL_ERROR__: {e}'
            current_messages.append({'role': 'tool', 'tool_call_id': call.get('id'), 'content': output})
        if on_after_tools:
            try:
                on_after_tools(current_messages)
            except Exception:
                pass
    return (None, current_messages) if return_transcript else None


# Higgsfield AI image/video generation tools (2026-10-06). Real, metered
# generation through the think tank's Higgsfield account: the cost is
# estimated up-front via the real /estimate endpoint (a verified number, never
# a guess) and accrued to the spend cap on SUCCESS only -- failed/nsfw/canceled
# requests are not charged by Higgsfield, so they never hit the ledger.
# Conditional-availability rule (same as search_web): absent unless both key
# halves are configured, so the surface never advertises a tool that would
# fail at call time.
_HIGGSFIELD_IMAGE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'generate_image',
        'description': (
            "Generate a real image from a text prompt via the think tank's real Higgsfield "
            "account (SOUL V2). This is a real, metered generation billed against the account's "
            "credits -- only call this when the investigation genuinely needs a real generated "
            "image, not a description of what one might look like. It takes up to ~90 real "
            "seconds. The output link is saved to the Library under media/generated/ for the "
            "player; Higgsfield keeps generated output for at least 7 days, so the player should "
            "download it for long-term retention."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'prompt': {'type': 'string',
                           'description': 'A detailed text description of the image to generate, e.g. "a quiet alpine lake at sunrise, editorial photography".'},
            },
            'required': ['prompt'],
        },
    },
}

_HIGGSFIELD_VIDEO_TOOL = {
    'type': 'function',
    'function': {
        'name': 'generate_video',
        'description': (
            "Generate a real short video from a text prompt via the think tank's real Higgsfield "
            "account (Kling 3.0 Standard). This is a real, metered generation billed against the "
            "account's credits and can take several minutes -- only call this when the "
            "investigation genuinely needs a real generated video, never as a cheap look. The "
            "output link is saved to the Library under media/generated/ for the player; Higgsfield "
            "keeps generated output for at least 7 days, so the player should download it for "
            "long-term retention."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'prompt': {'type': 'string',
                           'description': 'A detailed text description of the video to generate, e.g. "a slow cinematic tracking shot along a sunlit coastal road".'},
                'duration': {'type': 'integer', 'description': 'Output length in seconds, 3 to 15 (default 5).'},
                'aspect_ratio': {'type': 'string', 'description': 'One of "16:9", "9:16", "1:1" (default "16:9").'},
                'sound': {'type': 'string', 'description': '"on" or "off" (default "on").'},
            },
            'required': ['prompt'],
        },
    },
}

# VPN exit documentation for the browse_page tool schema -- built from
# the live registry so the model always sees exactly the exits that
# exist (each with its when-to-use guidance), never a hand-copied
# stale list. Empty when no exits are configured.
_VPN_EXIT_DOCS = '; '.join(
    f'"{name}" = {e.get("city", "unknown city")}, {e.get("country", "?").upper()} -- {e.get("useFor", "")}'.strip()
    for name, e in sorted(VPN_EXITS.items())
)
_VPN_EXIT_SUFFIX = f' ({_VPN_EXIT_DOCS})' if _VPN_EXIT_DOCS else ''

# The tools /api/intent/ask exposes to its dispatched agent. Kept as data next
# to the loop so the schema (what the model is allowed to call) stays a single,
# reviewable structure, and so tests build the exact shape they mock against.
try:  # pulse desk tools: watch live feeds and read matches
    from agents.pulse import PULSE_TOOLS as _PULSE_TOOLS
except Exception:  # pragma: no cover - optional module during bootstrap
    _PULSE_TOOLS = []
try:  # generic MCP client: operator-registered servers and tool calls
    import integrations.mcp_client as mcp_client
    _MCP_TOOLS = mcp_client.MCP_TOOLS
except Exception:  # pragma: no cover - optional module during bootstrap
    mcp_client = None
    _MCP_TOOLS = []
try:  # durable event streams: Redis Streams and SQS
    import server.durable_streams as durable_streams
    _DURABLE_STREAM_TOOLS = durable_streams.DURABLE_STREAM_TOOLS
except Exception:  # pragma: no cover - optional module during bootstrap
    durable_streams = None
    _DURABLE_STREAM_TOOLS = []
try:  # S3-compatible object storage
    import core.object_store as object_store
    _OBJECT_STORE_TOOLS = object_store.OBJECT_STORE_TOOLS
except Exception:  # pragma: no cover - optional module during bootstrap
    object_store = None
    _OBJECT_STORE_TOOLS = []
try:  # allowlisted CLI transport
    import tools.cli_tools as cli_tools
    _CLI_TOOLS = cli_tools.CLI_TOOLS
except Exception:  # pragma: no cover - optional module during bootstrap
    cli_tools = None
    _CLI_TOOLS = []
