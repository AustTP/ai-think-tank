"""Group repeated investigations without discarding their reports."""
def key(task):
    return tuple(' '.join(str(task.get(field) or '').lower().split())
                 for field in ('title', 'projectId', 'projectLabel', 'goal'))


def group(state, task):
    return [other for other in (state.get('tasks') or {}).values()
            if other.get('taskType') == 'spike' and other.get('status') == 'done'
            and not other.get('reviewOf') and key(other) == key(task)]
