import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def _ui_source():
    # The UI's script was extracted from index.html into js modules so no
    # single script exceeds 1,000 lines; source checks scan all of them.
    rels = ('world/index.html', 'world/js/main.js', 'world/js/ui_panels.js',
            'world/js/ui_inbox.js', 'world/js/ui_agent.js', 'world/js/ui_loop.js')
    return '\n'.join(_read(rel) for rel in rels)


class MeetingUISourceTests(unittest.TestCase):
    def test_jev_choice_results_are_unwrapped(self):
        html = _ui_source()
        self.assertEqual(html.count('requestJevChoice('), 3)
        self.assertEqual(html.count('decision && decision.choice'), 3)
        self.assertNotIn('let chosenId = await requestJevChoice', html)

    def test_agent_mission_falls_back_when_profile_is_missing(self):
        world_js = _read('world/js/world.js')
        self.assertIn('function agentMission(def)', world_js)
        self.assertIn('general think tank work', world_js)
        # The UI and task sources were split into smaller modules; the
        # contract is on the combined source, not on any one file.
        rels = ('world/js/main.js', 'world/js/ui_panels.js', 'world/js/ui_inbox.js',
                'world/js/ui_agent.js', 'world/js/ui_loop.js', 'world/js/tasks.js',
                'world/js/task_movement.js', 'world/js/handoffs.js')
        text = '\n'.join(_read(rel) for rel in rels)
        self.assertNotIn('.profile.mission}', text)
        self.assertIn('agentMission(', text)

    def test_task_and_handoff_candidates_use_agent_mission(self):
        task_js = '\n'.join(_read(rel) for rel in ('world/js/tasks.js', 'world/js/task_movement.js'))
        self.assertEqual(task_js.count('agentMission(d)'), 2)
        self.assertEqual(_read('world/js/handoffs.js').count('agentMission(d)'), 2)


if __name__ == '__main__':
    unittest.main()
