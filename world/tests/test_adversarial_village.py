"""Automated tests for the adversarial (winter) village feature.

Covers the pure lifecycle helpers: state record creation, per-side delivery
detection, the completion tick, drain/disable, the autosave-merge survival of
the server-owned record, and the launch path (with the sim's team-spawn and
task-filing mocked out so no model/DB/sim ceremony runs).

Same hermetic pattern as test_avatars.py / test_serve.py: a module-level patch
redirects DB paths to a temp dir so nothing touches a real DB.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve  # noqa: E402
import sim  # noqa: E402

_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='think-tank-adv-')
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


def _base_state():
    return {
        'agents': {
            'theo': {'id': 'theo', 'name': 'Theo', 'villageId': 'main', 'busy': False, 'offDuty': False},
        },
        'agentRoster': [
            {'id': 'theo', 'name': 'Theo', 'isAdmin': True},
        ],
        'villages': [{'id': 'main', 'name': 'Main Village'}],
        'teams': [],
        'backlogRequests': [],
        'tasks': {},
        'workQueue': [],
    }


class AdversarialState(unittest.TestCase):
    def test_defaults_to_disabled(self):
        st = serve._adversarial_state(_base_state())
        self.assertFalse(st['enabled'])
        self.assertEqual(st['status'], 'disabled')
        self.assertEqual(st['villageId'], 'winter')

    def test_reuses_existing_record(self):
        state = _base_state()
        first = serve._adversarial_state(state)
        first['enabled'] = True
        first['taskGoal'] = 'Build a bridge'
        second = serve._adversarial_state(state)
        self.assertIs(first, second)
        self.assertTrue(second['enabled'])
        self.assertEqual(second['taskGoal'], 'Build a bridge')


class SideDelivered(unittest.TestCase):
    def test_no_tasks_means_not_delivered(self):
        state = _base_state()
        self.assertFalse(serve._adv_side_delivered(state, 'adv-1', 'main'))
        self.assertFalse(serve._adv_side_delivered(state, 'adv-1', 'winter'))

    def test_all_done_means_delivered(self):
        state = _base_state()
        state['tasks'] = {
            't1': {'adversarialTaskId': 'adv-1', 'villageId': 'main', 'status': 'done'},
            't2': {'adversarialTaskId': 'adv-1', 'villageId': 'main', 'status': 'needs_review'},
        }
        self.assertTrue(serve._adv_side_delivered(state, 'adv-1', 'main'))

    def test_any_open_means_not_delivered(self):
        state = _base_state()
        state['tasks'] = {
            't1': {'adversarialTaskId': 'adv-1', 'villageId': 'main', 'status': 'done'},
            't2': {'adversarialTaskId': 'adv-1', 'villageId': 'main', 'status': 'walking'},
        }
        self.assertFalse(serve._adv_side_delivered(state, 'adv-1', 'main'))

    def test_other_village_tasks_do_not_count(self):
        state = _base_state()
        state['tasks'] = {
            't1': {'adversarialTaskId': 'adv-1', 'villageId': 'winter', 'status': 'done'},
        }
        # Winter is done, main has nothing -> main not delivered.
        self.assertFalse(serve._adv_side_delivered(state, 'adv-1', 'main'))
        self.assertTrue(serve._adv_side_delivered(state, 'adv-1', 'winter'))

    def test_other_task_ids_do_not_count(self):
        state = _base_state()
        state['tasks'] = {
            't1': {'adversarialTaskId': 'adv-OTHER', 'villageId': 'main', 'status': 'done'},
        }
        self.assertFalse(serve._adv_side_delivered(state, 'adv-1', 'main'))


class Tick(unittest.TestCase):
    def test_disabled_is_noop(self):
        state = _base_state()
        state['teams'].append({'id': 't', 'directorId': 't', 'members': []})
        state['villages'].append({'id': 'v', 'name': 'V'})
        teams_before = list(state['teams'])
        serve._adversarial_village_tick(state)
        # No team/village mutation happened; the record (if created) is disabled.
        self.assertEqual(state['teams'], teams_before)
        st = state['adversarialVillage']
        self.assertFalse(st['enabled'])

    def test_completes_and_disables_when_both_sides_done(self):
        state = _base_state()
        state['adversarialVillage'] = serve._adversarial_state(state)
        state['adversarialVillage'].update({
            'enabled': True, 'status': 'active', 'taskId': 'adv-1',
            'taskGoal': 'Goal', 'winterDirectorId': 'wd',
        })
        # Winter village + team exist.
        state['villages'].append({'id': 'winter', 'name': 'Winter Village'})
        state['teams'].append({'id': 'wd', 'directorId': 'wd', 'members': ['we1']})
        state['agents']['wd'] = {'id': 'wd', 'villageId': 'winter'}
        state['agents']['we1'] = {'id': 'we1', 'villageId': 'winter'}
        state['tasks'] = {
            'm1': {'adversarialTaskId': 'adv-1', 'villageId': 'main', 'status': 'done'},
            'w1': {'adversarialTaskId': 'adv-1', 'villageId': 'winter', 'status': 'done'},
        }
        serve._adversarial_village_tick(state)
        st = state['adversarialVillage']
        self.assertFalse(st['enabled'])
        self.assertEqual(st['status'], 'disabled')
        # Winter team dissolved, agents returned to main, village removed.
        self.assertNotIn('wd', {t['id'] for t in state['teams']})
        self.assertEqual(state['agents']['wd']['villageId'], 'main')
        self.assertEqual(state['agents']['we1']['villageId'], 'main')
        self.assertNotIn('winter', {v['id'] for v in state['villages']})

    def test_stays_active_until_both_sides_done(self):
        state = _base_state()
        state['adversarialVillage'] = serve._adversarial_state(state)
        state['adversarialVillage'].update({
            'enabled': True, 'status': 'active', 'taskId': 'adv-1', 'winterDirectorId': 'wd',
        })
        state['tasks'] = {
            'm1': {'adversarialTaskId': 'adv-1', 'villageId': 'main', 'status': 'done'},
            # Winter still working.
            'w1': {'adversarialTaskId': 'adv-1', 'villageId': 'winter', 'status': 'walking'},
        }
        serve._adversarial_village_tick(state)
        self.assertTrue(state['adversarialVillage']['enabled'])
        self.assertEqual(state['adversarialVillage']['status'], 'active')


class Disable(unittest.TestCase):
    def test_drains_team_and_removes_village(self):
        state = _base_state()
        state['adversarialVillage'] = serve._adversarial_state(state)
        state['adversarialVillage'].update({
            'enabled': True, 'status': 'active', 'taskId': 'adv-1', 'winterDirectorId': 'wd',
        })
        state['villages'].append({'id': 'winter', 'name': 'Winter Village'})
        state['teams'].append({'id': 'wd', 'directorId': 'wd', 'members': ['we1']})
        state['agents']['wd'] = {'id': 'wd', 'villageId': 'winter'}
        state['agents']['we1'] = {'id': 'we1', 'villageId': 'winter'}
        serve._disable_adversarial_village(state, reason='admin')
        st = state['adversarialVillage']
        self.assertFalse(st['enabled'])
        self.assertEqual(st['status'], 'disabled')
        self.assertNotIn('winter', {v['id'] for v in state['villages']})
        self.assertNotIn('wd', {t['id'] for t in state['teams']})
        self.assertEqual(state['agents']['we1']['villageId'], 'main')


class AutosaveMerge(unittest.TestCase):
    def test_server_owned_field_survives_client_autosave(self):
        existing = _base_state()
        existing['adversarialVillage'] = serve._adversarial_state(existing)
        existing['adversarialVillage']['enabled'] = True
        # Client autosave omits the server-owned field entirely.
        incoming = {'agents': {'theo': {'id': 'theo'}}, 'player': {}}
        merged = serve._merge_server_owned(existing, incoming)
        self.assertTrue(merged['adversarialVillage']['enabled'])


class QueueAndTaskTagging(unittest.TestCase):
    def test_queue_item_carries_adversarial_tags(self):
        state = _base_state()
        sim.queue_work(state, [{
            'title': 'Adv card', 'room': 'pressoffice',
            'adversarialTaskId': 'adv-1', 'villageId': 'winter',
        }])
        self.assertEqual(state['workQueue'][0]['adversarialTaskId'], 'adv-1')
        self.assertEqual(state['workQueue'][0]['villageId'], 'winter')


class Launch(unittest.TestCase):
    def test_launch_files_same_goal_to_both_villages(self):
        state = _base_state()
        # The winter side's request is normally filed by spawn_new_team_for_request
        # (mocked away here); seed one so the tagging path runs.
        state['backlogRequests'].append({'id': 'wrq-w', 'teamId': 'wd', 'filedBy': 'wd'})
        filed = {}

        def fake_file(state, authority_id, goal, team_id, now_ms=None):
            req = {'id': 'wrq-x', 'teamId': team_id, 'filedBy': authority_id}
            filed['authority'] = authority_id
            filed['team'] = team_id
            filed['goal'] = goal
            return req

        with unittest.mock.patch.object(serve, '_spawn_winter_team', return_value='wd'), \
                unittest.mock.patch.object(serve, '_free_authority', return_value='theo'), \
                unittest.mock.patch.object(serve, '_staffing_team_for_authority',
                                           return_value={'id': 'mainteam'}), \
                unittest.mock.patch.object(sim, 'file_large_request', side_effect=fake_file):
            ok, msg = serve._launch_adversarial_village(state, 'theo', 'Design a winter logo',
                                                        now_ms=1000)
        self.assertTrue(ok)
        st = state['adversarialVillage']
        self.assertTrue(st['enabled'])
        self.assertEqual(st['status'], 'active')
        self.assertEqual(st['winterDirectorId'], 'wd')
        self.assertEqual(st['taskGoal'], 'Design a winter logo')
        # The main-side request was filed and tagged.
        self.assertEqual(filed['goal'], 'Design a winter logo')
        self.assertEqual(filed['authority'], 'theo')
        self.assertEqual(st['mainRequestId'], 'wrq-x')
        # At least one backlog request is tagged winter for the winter side.
        winter_tagged = [r for r in state['backlogRequests']
                         if r.get('villageId') == 'winter' and r.get('adversarialTaskId') == st['taskId']]
        self.assertEqual(len(winter_tagged), 1)

    def test_launch_rejects_missing_task(self):
        state = _base_state()
        ok, msg = serve._launch_adversarial_village(state, 'theo', '', now_ms=1000)
        self.assertFalse(ok)
        self.assertFalse(state['adversarialVillage']['enabled'])

    def test_launch_rejects_when_already_enabled(self):
        state = _base_state()
        state['adversarialVillage'] = serve._adversarial_state(state)
        state['adversarialVillage']['enabled'] = True
        with unittest.mock.patch.object(serve, '_spawn_winter_team', return_value='wd'):
            ok, msg = serve._launch_adversarial_village(state, 'theo', 'Some goal', now_ms=1000)
        self.assertFalse(ok)
        self.assertIn('already active', msg.lower())


if __name__ == '__main__':
    unittest.main()
