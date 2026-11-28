"""Function titles (operator directive 2026-11-28): a hire's `role` is what
the agent DOES, never a reporting line. New hires get a function title picked
by the hiring director (with a work-derived fallback), and the one-time
backfill retitles every stale hire-era "Assistant to X" badge from recent
real work."""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import sim  # noqa: E402


def _grid():
    return {(x, y): None for x in range(20) for y in range(20)}


def _state(agents=None, tasks=None, teams=None):
    return {'agents': agents or {}, 'tasks': tasks or {}, 'teams': teams or [],
            'agentRoster': [], '_usedNames': []}


class TitleDerivation(unittest.TestCase):
    def test_dominant_done_work_picks_the_noun(self):
        state = _state(tasks={
            'a': {'status': 'done', 'assignedTo': 'ruth', 'taskType': 'review',
                  'doneAt': 5},
            'b': {'status': 'done', 'assignedTo': 'ruth', 'taskType': 'review',
                  'doneAt': 4},
            'c': {'status': 'done', 'assignedTo': 'ruth', 'taskType': 'code',
                  'doneAt': 3},
        })
        self.assertEqual(sim._title_from_recent_work(state, 'ruth'),
                         'Review Analyst')

    def test_neutral_types_are_skipped(self):
        state = _state(tasks={
            'a': {'status': 'done', 'assignedTo': 'x', 'taskType': 'task',
                  'doneAt': 5},
            'b': {'status': 'done', 'assignedTo': 'x', 'taskType': 'meeting',
                  'doneAt': 4},
            'c': {'status': 'done', 'assignedTo': 'x', 'taskType': 'media',
                  'doneAt': 3},
        })
        self.assertEqual(sim._title_from_recent_work(state, 'x'),
                         'Media Producer')

    def test_no_signal_falls_back_to_room_then_associate(self):
        state = _state()
        self.assertEqual(sim._title_from_recent_work(
            state, 'quiet', room='pressoffice'), 'Staff Writer')
        self.assertEqual(sim._title_from_recent_work(
            state, 'quiet', room='somewhere-else'), 'Associate')

    def test_work_for_other_agents_is_ignored(self):
        state = _state(tasks={
            'a': {'status': 'done', 'assignedTo': 'other', 'taskType': 'code',
                  'doneAt': 5},
        })
        self.assertEqual(sim._title_from_recent_work(state, 'ruth'),
                         'Associate')


class Backfill(unittest.TestCase):
    def test_stale_assistant_badges_are_retitled(self):
        agents = {'maya': {'id': 'maya', 'name': 'Maya'}}
        state = _state(agents=agents)
        entry = {'id': 'ruth', 'name': 'Ruth', 'role': 'Assistant to Nora',
                 'director': 'maya'}
        state['agentRoster'] = [entry]
        state['tasks'] = {
            'a': {'status': 'done', 'assignedTo': 'ruth', 'taskType': 'code',
                  'doneAt': 5},
            'b': {'status': 'done', 'assignedTo': 'ruth', 'taskType': 'code',
                  'doneAt': 4},
        }
        state['agents']['ruth'] = {'id': 'ruth', 'name': 'Ruth',
                                   'role': 'Assistant to Nora'}
        changed = sim._backfill_assistant_titles(state)
        self.assertEqual(changed, 1)
        self.assertEqual(entry['role'], 'Software Engineer')
        self.assertEqual(state['agents']['ruth']['role'], 'Software Engineer')
        mission = state['agents']['ruth']['profile']['mission']
        self.assertIn('Reports to Maya', mission)
        self.assertIn('relieve Nora', mission)
        # Both surfaces agree: the roster entry's embedded profile copy (what
        # the roster_agent DB mirror persists) carries the same rewrite.
        self.assertEqual(entry['profile']['mission'], mission)
        # Idempotent: a retitled agent no longer matches the stale prefix.
        self.assertEqual(sim._backfill_assistant_titles(state), 0)

    def test_partially_applied_first_run_still_fixes_the_mirror_mission(self):
        # A first run that retitled the role but left the roster entry's
        # embedded profile copy on the old hire template is repaired by a
        # rerun: the stale mission alone triggers the rewrite.
        entry = {'id': 'ruth', 'name': 'Ruth', 'role': 'Review Analyst',
                 'director': 'maya',
                 'profile': {'mission':
                             'Support the nora team by picking up overflow '
                             'work for Ben (Banking).'}}
        state = _state()
        state['agentRoster'] = [entry]
        state['agents'] = {'maya': {'id': 'maya', 'name': 'Maya'}}
        self.assertEqual(sim._backfill_assistant_titles(state), 1)
        self.assertEqual(entry['role'], 'Review Analyst')
        self.assertIn('Reports to Maya', entry['profile']['mission'])
        self.assertEqual(sim._backfill_assistant_titles(state), 0)

    def test_directors_and_current_roles_are_untouched(self):
        entry = {'id': 'ben', 'name': 'Ben',
                 'role': "The Writers' Room Director", 'director': 'theo'}
        state = _state()
        state['agentRoster'] = [entry]
        self.assertEqual(sim._backfill_assistant_titles(state), 0)
        self.assertEqual(entry['role'], "The Writers' Room Director")


class HireTitles(unittest.TestCase):
    def _hire(self, state):
        pending = {'adminId': 'ben', 'adminName': 'Ben', 'directorId': 'ben',
                   'helpForId': 'clara', 'helpForName': 'Clara',
                   'at': 1_000_000}
        state['agents']['ben'] = {'id': 'ben', 'name': 'Ben'}
        state['agents']['clara'] = {'id': 'clara', 'name': 'Clara',
                                    'role': 'Assistant to Ben'}
        with unittest.mock.patch.object(sim, '_hire_name_chooser',
                                        return_value='pia'), \
                unittest.mock.patch.object(sim, '_hire_title_chooser',
                                           return_value='Staff Writer'), \
                unittest.mock.patch.object(sim, '_free_outdoor_spot',
                                           return_value={'x': 10, 'y': 10}):
            new_id = sim._complete_auto_hire(state, pending, grid=_grid(),
                                             now_ms=1_000_000)
        return new_id

    def test_new_hire_wears_a_function_title(self):
        state = _state()
        self.assertEqual(self._hire(state), 'pia')
        agent = state['agents']['pia']
        self.assertEqual(agent['role'], 'Staff Writer')
        self.assertNotIn('Assistant to', agent['role'])
        self.assertIn('Reports to Clara', agent['profile']['mission'])
        roster_entry = state['agentRoster'][-1]
        self.assertEqual(roster_entry['role'], 'Staff Writer')

    def test_title_chooser_outage_falls_back_to_work_derived(self):
        state = _state()
        state['tasks'] = {
            'a': {'status': 'done', 'assignedTo': 'ben', 'taskType': 'review',
                  'teamId': 'ben', 'doneAt': 5},
            'b': {'status': 'done', 'assignedTo': 'ben', 'taskType': 'review',
                  'teamId': 'ben', 'doneAt': 4},
        }
        pending = {'adminId': 'ben', 'adminName': 'Ben', 'directorId': 'ben',
                   'helpForId': 'clara', 'helpForName': 'Clara',
                   'at': 1_000_000}
        state['agents']['ben'] = {'id': 'ben', 'name': 'Ben'}
        state['agents']['clara'] = {'id': 'clara', 'name': 'Clara',
                                    'role': 'Assistant to Ben'}
        with unittest.mock.patch.object(sim, '_hire_name_chooser',
                                        return_value='pia'), \
                unittest.mock.patch.object(sim, '_hire_title_chooser',
                                           return_value=None), \
                unittest.mock.patch.object(sim, '_free_outdoor_spot',
                                           return_value={'x': 10, 'y': 10}):
            self.assertEqual(sim._complete_auto_hire(state, pending,
                                                     grid=_grid(),
                                                     now_ms=1_000_000), 'pia')
        self.assertEqual(state['agents']['pia']['role'], 'Review Analyst')


if __name__ == '__main__':
    unittest.main()
