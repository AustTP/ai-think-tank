"""Village team capacity (12 top-level teams per village).

The operator fixed the SENIOR layer: no additional top-level directors
beyond the cap; all future directors are subdirectors of the existing 12.
village_team_count counts only top-level teams (director reporting directly
to the admin) -- the admin's aide cluster is not a team and subdirector
sub-teams do not consume village capacity. At the cap the request-driven
team spawn refuses before any mutation and records the sanctioned next
step (the senior-most directors' generate_village tool).
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402

governance = sim.governance  # core.governance, under sim's shared namespace


def _state(top_level=0, sub_teams=0, admin_aide_teams=0):
    """A minimal state fixture: an admin, top-level directors with teams,
    subdirector teams (director reports to a director), and the admin's
    own aide cluster teams."""
    roster = [{'id': 'theo', 'name': 'Theo', 'isAdmin': True}]
    teams = []
    for _ in range(admin_aide_teams):
        teams.append({'id': 'theo-aides', 'directorId': 'theo', 'members': []})
    for i in range(top_level):
        did = f'dir{i}'
        roster.append({'id': did, 'name': did.title(), 'isDirector': True,
                       'director': 'theo'})
        teams.append({'id': did, 'directorId': did, 'members': []})
    parent = 'dir0' if top_level else 'theo'
    for i in range(sub_teams):
        did = f'sub{i}'
        roster.append({'id': did, 'name': did.title(), 'isDirector': True,
                       'director': parent})
        teams.append({'id': did, 'directorId': did, 'members': []})
    return {'agentRoster': roster, 'teams': teams}


class VillageTeamCount(unittest.TestCase):
    def test_counts_only_top_level_teams(self):
        st = _state(top_level=3, sub_teams=4, admin_aide_teams=1)
        self.assertEqual(governance.village_team_count(st, 'theo'), 3)

    def test_empty_and_adminless_states(self):
        self.assertEqual(governance.village_team_count({}), 0)
        st = _state(top_level=2)
        self.assertEqual(governance.village_team_count(st, None), 2)

    def test_cap_refuses_request_driven_spawn(self):
        st = _state(top_level=sim.MAX_TEAMS_PER_VILLAGE)
        with unittest.mock.patch.object(serve, 'log_action'):
            team = sim.spawn_new_team_for_request(st, 'a large ask')
        self.assertIsNone(team)
        note = st.get('_teamSpawnBlockedAtVillageCap')
        self.assertIsNotNone(note)
        self.assertEqual(note['teams'], sim.MAX_TEAMS_PER_VILLAGE)
        self.assertIn('generate_village', note.get('next', '')
                      if 'next' in note else 'generate_village')

    def test_directorless_supporting_director_counts(self):
        # The supporting director sits at the senior tier with an absent
        # director pointer; her team consumes village capacity like any
        # other top-level team.
        st = _state(top_level=2)
        st['agentRoster'].append({'id': 'nora', 'name': 'Nora',
                                  'isDirector': True, 'director': None})
        st['teams'].append({'id': 'nora', 'directorId': 'nora',
                            'members': []})
        self.assertEqual(governance.village_team_count(st, 'theo'), 3)

    def test_cap_boundary_counts(self):
        under = _state(top_level=sim.MAX_TEAMS_PER_VILLAGE - 1)
        at = _state(top_level=sim.MAX_TEAMS_PER_VILLAGE)
        over = _state(top_level=sim.MAX_TEAMS_PER_VILLAGE + 3, sub_teams=5)
        self.assertEqual(
            governance.village_team_count(under, 'theo'),
            sim.MAX_TEAMS_PER_VILLAGE - 1)
        self.assertEqual(
            governance.village_team_count(at, 'theo'),
            sim.MAX_TEAMS_PER_VILLAGE)
        self.assertEqual(governance.village_team_count(over, 'theo'),
                         sim.MAX_TEAMS_PER_VILLAGE + 3)


if __name__ == '__main__':
    unittest.main()
