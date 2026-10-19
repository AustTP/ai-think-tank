"""Worker-only eligibility for recordings and training exports."""
import re
from pathlib import Path

ADMIN_FLAGS = ('isAdmin', 'isDirector', 'isReviewer', 'isDedicatedReviewer',
               'isTaskMaster', 'isLibrarian', 'isScrumMaster')
EXCLUDED_IDS = {'theo', 'nora', 'code-director', 'iris', 'owen', 'task-master',
                'librarian', 'risk-manager'}


def worker_interaction(payload, agent_id=None):
    context = payload.get('context') or {}
    agent_id = agent_id or context.get('agent_id')
    if not agent_id or agent_id in EXCLUDED_IDS or str(agent_id).startswith('reviewer-'):
        return False
    if any(context.get(flag) for flag in ADMIN_FLAGS):
        return False
    if context.get('review_of') or 'review' in str(context.get('task_type') or '').lower():
        return False
    role = context.get('role')
    if not role and re.fullmatch(r'[\w-]+', str(agent_id)):
        profile = Path(__file__).resolve().parents[1] / 'agents' / agent_id / 'AGENTS.md'
        if profile.is_file():
            role = profile.read_text(encoding='utf-8').splitlines()[0].split('--')[-1]
    if not role:
        return False
    return not re.search(r'review|admin|director|manager|scrum|task[ -]?master|librarian|control room|coordinat', str(role), re.I)
