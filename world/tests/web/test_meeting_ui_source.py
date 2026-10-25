import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


class MeetingUISourceTests(unittest.TestCase):
    def test_jev_choice_results_are_unwrapped(self):
        html = _read('world/index.html')
        self.assertEqual(html.count('requestJevChoice('), 3)
        self.assertEqual(html.count('decision && decision.choice'), 3)
        self.assertNotIn('let chosenId = await requestJevChoice', html)

    def test_agent_mission_falls_back_when_profile_is_missing(self):
        world_js = _read('world/js/world.js')
        self.assertIn('function agentMission(def)', world_js)
        self.assertIn('general think tank work', world_js)
        for rel in ('world/index.html', 'world/js/tasks.js', 'world/js/handoffs.js'):
            text = _read(rel)
            self.assertNotIn('.profile.mission}', text)
            self.assertIn('agentMission(', text)

    def test_task_and_handoff_candidates_use_agent_mission(self):
        self.assertEqual(_read('world/js/tasks.js').count('agentMission(d)'), 2)
        self.assertEqual(_read('world/js/handoffs.js').count('agentMission(d)'), 2)


if __name__ == '__main__':
    unittest.main()
