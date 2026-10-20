"""Keep accepted player recovery decisions safe from stale whole-state saves."""
import copy


def guidance_at(task):
    return max((item.get('at') or 0 for item in task.get('interventionHistory') or []), default=0)


def preserve_guidance(current, incoming):
    tasks = incoming.get('tasks')
    if not isinstance(tasks, dict):
        return incoming
    fresh = current.get('tasks') or {}
    protected = {tid for tid, task in fresh.items() if tid in tasks
                 and guidance_at(task) > guidance_at(tasks[tid])}
    if not protected:
        return incoming
    result = copy.deepcopy(incoming)
    def belongs(task, mapping):
        seen = set()
        while task:
            tid = task.get('id')
            if tid in protected or task.get('rootStoryId') in protected:
                return True
            if tid and tid in seen:
                break
            if tid:
                seen.add(tid)
            parent = next((task.get(k) for k in ('reviewOf', 'investigationOf', 'recoveryOf') if task.get(k)), None)
            if parent in protected:
                return True
            task = mapping.get(parent)
        return False
    affected = {tid for tid, task in fresh.items() if belongs(task, fresh)}
    for tid in affected:
        result['tasks'][tid] = copy.deepcopy(fresh[tid])
    result['workQueue'] = [q for q in result.get('workQueue') or [] if not belongs(q, tasks)]
    result['workQueue'].extend(copy.deepcopy(q) for q in current.get('workQueue') or [] if belongs(q, fresh))
    for aid, agent in (current.get('agents') or {}).items():
        task = agent.get('task')
        tid = task.get('id') if isinstance(task, dict) else task
        incoming_agent = (result.get('agents') or {}).get(aid) or {}
        previous = incoming_agent.get('task')
        previous_id = previous.get('id') if isinstance(previous, dict) else previous
        if tid in affected or previous_id in affected:
            result.setdefault('agents', {})[aid] = copy.deepcopy(agent)
    result['taskCounter'] = max(result.get('taskCounter') or 0, current.get('taskCounter') or 0)
    messages = {m.get('id'): m for m in current.get('playerInbox') or [] if m.get('taskId') in affected}
    result['playerInbox'] = [copy.deepcopy(messages.get(m.get('id'), m)) for m in result.get('playerInbox') or []]
    return result
