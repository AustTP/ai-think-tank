"""Provision a persistent independent review pool, separate from delivery teams."""
import copy
import time


def create_reviewers(state):
    roster = state.setdefault('agentRoster', [])
    agents = state.setdefault('agents', {})
    created = []
    for name, color in [('Quinn', '#8b5cf6'), ('Rhea', '#14b8a6'),
                        ('Soren', '#f59e0b'), ('Tessa', '#ec4899')]:
        aid = f'reviewer-{name.lower()}'
        if any(d.get('id') == aid for d in roster) or aid in agents:
            continue
        profile = {
            'mission': 'Review completed code and research independently so delivery workers can keep working.',
            'instructions': [
                'Take peer reviews only; do not claim implementation, investigation, or repair work.',
                'Read the actual artifact and acceptance criteria before giving a verdict.',
                'For code, require passing quality checks; for research, verify sources and concrete claims.',
                'Report specific defects with evidence and a clear repair request. Never approve to clear a queue.',
            ],
            'notes': [],
        }
        definition = {'id': aid, 'name': name, 'color': color, 'role': 'Dedicated Peer Reviewer',
                      'model': 'small', 'dedicatedReviewer': True, 'villageId': 'main',
                      'approvedCount': 0, 'droppedCount': 0, 'weekApprovals': 0,
                      'profile': profile, 'mailbox': []}
        roster.append(definition)
        agents[aid] = {
            **copy.deepcopy(definition), 'x': 510.0, 'y': 272.0, 'dir': 'south',
            'busy': False, 'task': None, 'offDuty': True, 'visible': False,
            'inRoom': None, 'roomX': None, 'roomY': None, 'meetingId': None,
            'conversationLog': [], 'hiredAt': int(time.time() * 1000),
        }
        created.append(aid)
    return created
