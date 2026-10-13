"""Bound local prompts while preserving instructions and valid tool exchanges."""
import copy
import json


def select_tools(messages, tools, tool_choice=None):
    if not tools:
        return tools
    core = {'search_library','read_library_file','search_web','browse_page','execute_script',
            'save_library_document','file_issue','request_allowlist'}
    topic = ' '.join(str(m.get('content','')) for m in messages if m.get('role')=='user').lower()
    topic += ' ' + next((str(m.get('content',''))[:600] for m in messages if m.get('role')=='system'), '').lower()
    forced = ((tool_choice or {}).get('function') or {}).get('name') if isinstance(tool_choice,dict) else None
    selected=[]
    for tool in tools:
        name=(tool.get('function') or {}).get('name','')
        words=name.replace('_',' ').split()
        if name in core or name==forced or any(w in topic for w in words if len(w)>3 and w not in ('search','read','create','list','append','generate')):
            selected.append(copy.deepcopy(tool))
    if not selected:
        selected=copy.deepcopy(tools[:4])
    def shorten(value):
        if isinstance(value,dict):
            return {k:(v[:96] if k=='description' and isinstance(v,str) else shorten(v)) for k,v in value.items()}
        if isinstance(value,list): return [shorten(v) for v in value]
        return value
    return shorten(selected)


def size(value):
    return len(json.dumps(value, ensure_ascii=False).encode('utf-8'))


def compact(messages, max_tokens, tools=None, context_length=8192):
    # Conservative byte allowance leaves headroom for templates and tokenization.
    budget = int((context_length - max_tokens - 768) * 2.5) - size(tools or []) - 192
    if budget <= 0:
        raise ValueError('Local context budget exhausted by output reservation and tool schemas')
    result = copy.deepcopy(messages)
    for m in result:
        if m.get('role') == 'tool' and isinstance(m.get('content'), str):
            raw = m['content'].encode('utf-8')
            if len(raw) > 1800:
                m['content'] = ('UNTRUSTED TOOL DATA (excerpt; remaining content omitted, '
                                'retrieve the relevant section if needed):\n' +
                                raw[:1800].decode('utf-8', errors='ignore'))
    groups = []
    for m in result:
        if m.get('role') == 'tool' and groups and groups[-1][0].get('tool_calls'):
            groups[-1].append(m)
        else:
            groups.append([m])
    user_indices = [i for i, g in enumerate(groups) if g[0].get('role') == 'user']
    required = {i for i, g in enumerate(groups) if g[0].get('role') == 'system'}
    if user_indices:
        required.update((user_indices[0], user_indices[-1]))
    kept = {i: groups[i] for i in required}
    if size([m for i in sorted(kept) for m in kept[i]]) > budget:
        raise ValueError('Task instructions exceed local context budget; split the task rather than discard requirements')
    for i in reversed(range(len(groups))):
        if i in kept:
            continue
        candidate = {**kept, i: groups[i]}
        if size([m for j in sorted(candidate) for m in candidate[j]]) <= budget:
            kept = candidate
    if len(kept) < len(groups):
        # Explain omissions without inventing a summary of removed evidence.
        for i in sorted(kept):
            if kept[i][0].get('role') == 'system':
                kept[i][0]['content'] += '\nOlder context omitted to fit the local budget. Retrieve missing evidence with tools; do not infer it.'
                break
    return [m for i in sorted(kept) for m in kept[i]]
