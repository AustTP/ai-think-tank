"""Local active-work routing; review/correction calls retain remote models."""
import hashlib
import json
import re
import threading
import urllib.parse
import urllib.request
import training_capture
import qwen_tool_calls

_LOCK = threading.Lock()  # Serialize requests to the shared 12GB GPU runtime.
_STUDIO_ALIASES = {}
_PROTECTED = re.compile(r'\b(review\w*|correct\w*|repair\w*|fix\w*|debug\w*|qa|audit\w*|validat\w*)\b', re.I)


def agent_seed(agent_id, agent_ids=()):
    """Stable unique 31-bit seeds for the roster, with collision resolution."""
    used, seeds = set(), {}
    for identifier in sorted(set(map(str, agent_ids)) | {str(agent_id)}):
        seed = int.from_bytes(hashlib.sha256(('local-worker:' + identifier).encode()).digest()[:4], 'big') & 0x7fffffff
        while seed in used:
            seed = (seed + 1) & 0x7fffffff
        used.add(seed)
        seeds[identifier] = seed
    return seeds[str(agent_id)]


def route(config, state, agent_id, task_id=None, purpose=None, messages=()):
    if str(config.get('LOCAL_WORK_MODEL_ENABLED', '')).lower() not in ('true', '1', 'yes') or not agent_id:
        return None
    agents = state.get('agents') or {}
    agent = agents.get(agent_id, {})
    if not agent or _PROTECTED.search(str(agent.get('role', ''))) or str(agent.get('role', '')).lower() == 'code director':
        return None
    if purpose and _PROTECTED.search(str(purpose)):
        return None
    task = (state.get('tasks') or {}).get(task_id) if task_id else agent.get('task')
    if isinstance(task, str):
        task = (state.get('tasks') or {}).get(task)
    if task is None and purpose == 'work':
        task = {'status': 'working', 'taskType': 'planning', 'title': ''}
    if not isinstance(task, dict) or task.get('status') in ('done', 'failed', 'cancelled', 'stopped', 'needs_review', 'blocked', 'repairing', 'investigating'):
        return None
    if task.get('skillReview') or any(task.get(k) for k in ('reviewOf', 'recoveryOf', 'repairOf', 'investigationOf', '_peerRejectedBy', 'failNote')):
        return None
    if _PROTECTED.search(str(task.get('taskType', '')) + ' ' + str(task.get('title', ''))):
        return None
    # A worker's own retry may be a correction within a normal task. Examine
    # only the latest user instruction, not retrieved tool data or older history.
    last_user = next((m.get('content', '') for m in reversed(messages) if m.get('role') == 'user'), '')
    if isinstance(last_user, str) and re.search(r'(?i)\b(fix|correct|repair|debug|failed|syntax error|format error)\b', last_user[:1500]):
        return None
    model = config.get('LOCAL_WORK_MODEL_PATH', '').strip()
    base = config.get('LOCAL_WORK_MODEL_BASE_URL', '').rstrip('/')
    url = urllib.parse.urlsplit(base)
    if not model or url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.username or url.password:
        raise ValueError('Local work model requires a model path and loopback HTTP base URL')
    return {'model': model, 'base_url': base, 'api_key': config.get('LOCAL_WORK_MODEL_API_KEY', ''),
            'timeout': float(config.get('LOCAL_WORK_MODEL_TIMEOUT', 600)),
            'manage_studio': str(config.get('LOCAL_WORK_MODEL_MANAGE_STUDIO', '')).lower() in ('true', '1', 'yes'),
            'seed': agent_seed(agent_id, agents), 'agent_id': agent_id}


def complete(selection, messages, max_tokens, tools=None, tool_choice=None, response_format=None):
    import evidence_policy
    messages = evidence_policy.apply(messages)
    import context_budget
    messages = context_budget.compact(messages, max_tokens or 1500, tools)
    return _complete(selection, messages, max_tokens, tools, tool_choice, response_format)


@training_capture.record_call('unsloth-local')
def _complete(selection, messages, max_tokens, tools=None, tool_choice=None, response_format=None):
    body = {'model': selection['model'], 'messages': messages, 'max_tokens': max_tokens or 1500,
            'seed': selection['seed'], 'temperature': 0.6, 'stream': False,
            'enable_thinking': False,
            'chat_template_kwargs': {'enable_thinking': False}}
    if tools is not None:
        body['tools'] = tools
    if tool_choice is not None:
        body['tool_choice'] = tool_choice
    if response_format:
        body['response_format'] = response_format
    headers = {'Content-Type': 'application/json'}
    if selection['api_key']:
        headers['Authorization'] = 'Bearer ' + selection['api_key']
    request = urllib.request.Request(selection['base_url'] + '/chat/completions',
                                     data=json.dumps(body).encode(), headers=headers, method='POST')
    with _LOCK:
        alias_key, loaded_alias = None, None
        if selection.get('manage_studio'):
            origin = urllib.parse.urlsplit(selection['base_url'])
            studio = origin.scheme + '://' + origin.netloc
            status_request = urllib.request.Request(studio + '/api/inference/status', headers=headers)
            with urllib.request.urlopen(status_request, timeout=15) as response:
                status = json.load(response)
            alias_key = (studio, selection['model'])
            if status.get('active_model') not in (selection['model'], _STUDIO_ALIASES.get(alias_key, selection['model'])):
                load_request = urllib.request.Request(studio + '/api/inference/load',
                    data=json.dumps({'model_path': selection['model'], 'is_lora': True,
                                     'load_in_4bit': True, 'max_seq_length': 8192}).encode(),
                    headers=headers, method='POST')
                with urllib.request.urlopen(load_request, timeout=selection['timeout']) as response:
                    json.load(response)
                with urllib.request.urlopen(status_request, timeout=15) as response:
                    loaded_alias = json.load(response).get('active_model')
        with urllib.request.urlopen(request, timeout=selection['timeout']) as response:
            result = json.load(response)
    if not result.get('choices') or not result['choices'][0].get('message'):
        raise RuntimeError('Local worker model returned no completion')
    expected = re.split(r'[/\\]', selection['model'])[-1]
    returned = result.get('model')
    if returned and returned not in (selection['model'], expected):
        raise RuntimeError('Studio returned a different model than the configured worker adapter')
    if alias_key and loaded_alias and returned in (selection['model'], expected):
        _STUDIO_ALIASES[alias_key] = loaded_alias
    result = qwen_tool_calls.normalize(result, tools)
    # Local inference has no provider charge; never accept a supplied cost field.
    result.setdefault('usage', {})['cost'] = 0.0
    return result
