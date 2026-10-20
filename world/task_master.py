"""Worker selection helper for recovery coordination."""
AGENT_ID = 'task-master'


def workers(state):
    masters = {t.get('scrumMasterId') for t in state.get('teams') or []}
    excluded = ('isAdmin', 'isDirector', 'dedicatedReviewer',
                'tradingSpecialist', 'taskMaster', 'librarian')
    agents = state.get('agents') or {}
    return [d['id'] for d in state.get('agentRoster') or []
            if d.get('id') in agents and d['id'] not in masters
            and not any(d.get(flag) for flag in excluded)]
