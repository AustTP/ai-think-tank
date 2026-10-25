"""Tests for the live pulse desk: watches, matching, dedupe, brief, tools."""
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import agents.pulse as pulse
import serve
import core.tool_policy as tool_policy


class PulseDesk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pulse-test-')
        self.patcher = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
        )
        self.patcher.start()
        pulse._TABLES_READY = False
        pulse._HAS_WATCHES = None
        pulse._LAST_OBSERVE.clear()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_watch_create_match_dedupe_and_brief(self):
        watch = pulse.create_watch('cora', 'Tankers near Hormuz', 'ais', 'tanker')
        self.assertEqual(watch['source'], 'ais')
        now = int(time.time() * 1000)
        self.assertEqual(pulse.observe('ais', 'vessel', {'ts': now, 'name': 'TANKER A'}), 1)
        self.assertEqual(pulse.observe('ais', 'vessel', {'ts': now, 'name': 'TANKER A'}), 0)
        time.sleep(0.3)
        self.assertEqual(pulse.observe('ais', 'vessel', {'ts': now + 1, 'name': 'TANKER B'}), 1)
        self.assertEqual(pulse.observe('ais', 'vessel', {'ts': now + 2, 'name': 'ferry'}), 0)
        rows = pulse.events(watch_id=watch['watchId'])
        self.assertEqual(len(rows), 2)
        self.assertIn('TANKER B', rows[0]['summary'])
        brief = pulse.brief('cora', hours=24)
        self.assertIn('Tankers near Hormuz', brief)
        self.assertIn('2 events', brief)
        self.assertTrue(pulse.delete_watch(watch['watchId'], 'cora'))
        self.assertEqual(pulse.list_watches('cora'), [])

    def test_any_source_and_other_agent_isolation(self):
        watch = pulse.create_watch('cora', 'anything about rates', 'any', 'rate')
        pulse.observe('rss:fed', 'headline', {'title': 'Rates hold steady'})
        pulse.observe('x', 'trends', {'data': 'unrelated'})
        self.assertEqual(len(pulse.events(watch_id=watch['watchId'])), 1)
        self.assertEqual(len(pulse.events(agent_id='nora')), 0)
        self.assertEqual(len(pulse.events(agent_id='cora')), 1)

    def test_tools_create_list_read_and_wrap(self):
        created = pulse.execute_pulse_tool('cora', 'pulse_watch', {
            'action': 'create', 'label': 'Tankers', 'source': 'ais', 'match': 'tanker'})
        self.assertIn('"ok": true', created.lower())
        pulse.observe('ais', 'vessel', {'ts': int(time.time() * 1000) + 3, 'name': 'TANKER C'})
        read = pulse.execute_pulse_tool('cora', 'read_pulse', {'action': 'brief'})
        self.assertIn('Tankers', read)
        self.assertIn('TANKER C', read)
        self.assertIn('data', read.lower())
        listed = pulse.execute_pulse_tool('cora', 'pulse_watch', {'action': 'list'})
        self.assertIn('Tankers', listed)

    def test_policy_tiers(self):
        self.assertEqual(tool_policy.tool_trust_tier('read_pulse'), 'research')
        self.assertEqual(tool_policy.tool_trust_tier('pulse_watch'), 'prepare')
        self.assertEqual(tool_policy.tool_replay_safety('read_pulse'), 'safe')
        self.assertEqual(tool_policy.tool_replay_safety('pulse_watch'), 'unsafe')


if __name__ == '__main__':
    unittest.main()
