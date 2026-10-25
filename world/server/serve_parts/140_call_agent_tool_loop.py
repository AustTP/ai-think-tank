

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
        _accrue_spend(service, ((data or {}).get('usage') or {}).get('cost', 0.0), village_id=village_id)
        if task_id and isinstance(((data or {}).get('usage') or {}).get('cost'), (int, float)):
            task_cost = ((data or {}).get('usage') or {}).get('cost', 0.0)
            if task_cost:
                _accrue_task_spend(task_id, task_cost)
        choice = ((data or {}).get('choices') or [{}])[0]
        message = choice.get('message') or {}
        tool_calls = message.get('tool_calls') or []
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
                _accrue_spend(service, ((data or {}).get('usage') or {}).get('cost', 0.0), village_id=village_id)
                if task_id and isinstance(((data or {}).get('usage') or {}).get('cost'), (int, float)):
                    task_cost = ((data or {}).get('usage') or {}).get('cost', 0.0)
                    if task_cost:
                        _accrue_task_spend(task_id, task_cost)
                choice = ((data or {}).get('choices') or [{}])[0]
                message = choice.get('message') or {}
                tool_calls = message.get('tool_calls') or []
                if tool_calls:
                    break
        if not tool_calls:
            content = sanitize_text((message.get('content') or '').strip())
            if not content:
                print(f'[ask-debug] empty content for model={model} raw={json.dumps(data)[:2000]}', flush=True)
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
