"""Coverage-gap tests for serve_parts 050, 080, 090 and 100."""
import asyncio
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

import serve
import sim


class _TempMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cov-b5-avatars-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.patcher = mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'db.sqlite'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            BROWSER_PROFILE_DIR=os.path.join(self.tmp, 'profiles'),
            HTTP_CACHE_DIR=os.path.join(self.tmp, '.http_cache'),
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        serve.init_db()


class _Resp:
    def __init__(self, payload=b'png'):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return self.payload


def _state():
    return {
        'agentRoster': [
            {'id': 'd1', 'name': 'Director', 'isDirector': True},
            {'id': 'w1', 'name': 'Worker', 'role': 'worker', 'director': 'd1'},
            {'id': 'w2', 'name': 'Other', 'role': 'worker', 'director': 'd1'},
            {'id': 'admin', 'name': 'Admin', 'isAdmin': True},
        ],
        'agents': {
            'd1': {'name': 'Director', 'role': 'director'},
            'w1': {'name': 'Worker', 'role': 'worker'},
        },
    }


class AvatarHelpersTest(_TempMixin, unittest.TestCase):
    def test_orientation_and_sex_and_description(self):
        self.assertEqual(serve._avatar_orientation_name('front'), 'south')
        self.assertEqual(serve._avatar_orientation_name('weird key'), 'weird_key')
        self.assertEqual(serve._avatar_sex('Marcus Lee'), 'male')
        self.assertEqual(serve._avatar_sex('Priya'), 'female')
        self.assertEqual(serve._avatar_sex('Zzyzx'), 'neutral')
        self.assertIn('suit', serve._avatar_description('Marcus', 'director', True))
        self.assertIn('vest', serve._avatar_description('Marcus', '', False))

    def test_available_helpers(self):
        with mock.patch.object(serve, '_pixellab_credits_remaining', return_value=1.0):
            self.assertTrue(serve._pixellab_avatar_available())
        with mock.patch.object(serve, '_pixellab_credits_remaining', return_value=None):
            self.assertFalse(serve._pixellab_avatar_available())
        with mock.patch.object(serve, '_pixellab_token', side_effect=RuntimeError('x')):
            self.assertFalse(serve._pixellab_configured())
        self.assertEqual(serve._avatar_files('missing-agent'), [])

    def test_ensure_avatar_polls_and_downloads(self):
        rotation = {'south': 'https://x.test/1.png', 'north': 'https://x.test/2.png',
                    'east': 'https://x.test/3.png', 'west': 'https://x.test/4.png'}
        with mock.patch.object(serve, '_avatar_set_complete', side_effect=[False, True]), \
             mock.patch.object(serve, '_pixellab_avatar_available', return_value=True), \
             mock.patch.object(serve, 'get_or_create_agent_key', return_value='k'), \
             mock.patch.object(serve, '_api_base_url', return_value='base'), \
             mock.patch.object(serve, '_api_execute', return_value={
                 'ok': True, 'data': {'rotation_urls': {'south': 'https://x.test/1.png'}},
                 'ids': {'character_id': 'c1'}}), \
             mock.patch.object(serve, '_pixellab_call', side_effect=[
                 ({'rotation_urls': {'south': 'https://x.test/1.png'}}, None),
                 ({'rotation_urls': rotation}, None)]), \
             mock.patch.object(serve.time, 'sleep'), \
             mock.patch.object(serve.urllib.request, 'urlopen',
                               side_effect=[RuntimeError('first'), _Resp(),
                                            _Resp(), _Resp(), _Resp()]):
            self.assertTrue(serve._ensure_agent_avatar('w1', 'Worker', 'worker'))

    def test_ensure_avatar_retry_exhausted(self):
        rotation = {'south': 'https://x.test/1.png', 'north': 'https://x.test/2.png',
                    'east': 'https://x.test/3.png', 'west': 'https://x.test/4.png'}
        with mock.patch.object(serve, '_avatar_set_complete', return_value=False), \
             mock.patch.object(serve, '_pixellab_avatar_available', return_value=True), \
             mock.patch.object(serve, 'get_or_create_agent_key', return_value='k'), \
             mock.patch.object(serve, '_api_base_url', return_value='base'), \
             mock.patch.object(serve, '_api_execute', return_value={
                 'ok': True, 'data': {'rotation_urls': rotation},
                 'ids': {'character_id': 'c1'}}), \
             mock.patch.object(serve.time, 'sleep'), \
             mock.patch.object(serve.urllib.request, 'urlopen',
                               side_effect=RuntimeError('always')):
            self.assertFalse(serve._ensure_agent_avatar('w1', 'Worker', 'worker'))


class GenerateAvatarBranchesTest(_TempMixin, unittest.TestCase):
    def _call(self, actor='d1', target='w1', **patches):
        defaults = {
            'get_state_from_db': _state(),
            '_is_director_or_admin': True,
            '_is_admin': False,
            'supervisor_team_for': {'members': ['w1', 'w2']},
            '_avatar_set_complete': False,
            '_avatar_files': [],
            '_pixellab_credits_remaining': 5.0,
            '_think_tank_spend_cap_exceeded': False,
            '_agent_spend_cap_exceeded': False,
            '_ensure_agent_avatar': True,
            'log_action': None,
        }
        defaults.update(patches)
        mocks = {}
        for key, value in defaults.items():
            mocks[key] = value if isinstance(value, mock.Mock) else mock.Mock(return_value=value)
        mocks.setdefault('log_action', mock.Mock())
        with mock.patch.multiple(serve, **mocks):
            return serve.generate_agent_avatar(actor, target)

    def test_required_fields_and_permission(self):
        self.assertEqual(self._call(actor='')['error'], 'agentId is required')
        self.assertIn('only a director', self._call(_is_director_or_admin=False)['error'])

    def test_unknown_agent_and_missing_agent_state(self):
        self.assertEqual(self._call(target='nope')['error'], 'unknown agent')
        self.assertEqual(self._call(target='w2')['error'], 'agent state not found')

    def test_already_complete(self):
        result = self._call(_avatar_set_complete=True)
        self.assertTrue(result['ok'])
        self.assertTrue(result['alreadyComplete'])

    def test_in_flight(self):
        with mock.patch.object(serve, '_AVATAR_IN_FLIGHT', {'w1'}):
            self.assertIn('already generating', self._call()['error'])

    def test_spend_caps(self):
        self.assertIn('think tank spend cap',
                      self._call(_think_tank_spend_cap_exceeded=True)['error'])
        caps = mock.Mock(side_effect=[True, False])
        self.assertIn('your spend cap',
                      self._call(_agent_spend_cap_exceeded=caps)['error'])
        caps = mock.Mock(side_effect=[False, True])
        self.assertIn("agent's spend cap",
                      self._call(_agent_spend_cap_exceeded=caps)['error'])

    def test_credits_missing_and_generation_failure(self):
        self.assertIn('not configured',
                      self._call(_pixellab_credits_remaining=None)['error'])
        self.assertIn('generation failed',
                      self._call(_ensure_agent_avatar=False)['error'])

    def test_avatar_task_open_and_team(self):
        self.assertTrue(serve._avatar_task_open(
            {'tasks': {'t': {'title': '[AVATAR] X (w1)', 'status': 'open'}}}, 'w1'))
        state = {'teams': [{'id': 't1', 'members': ['x']},
                           {'id': 't2', 'directorId': 'someone'},
                           {'id': 't3', 'directorId': 'd2'}]}
        self.assertEqual(serve._avatar_task_team_id(state, 'w9', 'd2'), 't3')
        self.assertEqual(serve._avatar_task_team_id(
            {'teams': [{'id': 't1', 'directorId': 'd2'}]}, 'w1', 'd2'), 't1')

    def test_queue_avatar_tasks_guards(self):
        with mock.patch.object(serve, '_pixellab_configured', return_value=False):
            self.assertEqual(serve._queue_avatar_tasks(), 0)

        with mock.patch.object(serve, '_pixellab_configured', return_value=True), \
             mock.patch.object(serve, '_avatar_generation_available', return_value=False):
            self.assertEqual(serve._queue_avatar_tasks(), 0)

        state = {'agents': {'w1': {'name': 'W'}},
                 'agentRoster': [{'id': 'w1', 'director': 'd1'}]}
        with mock.patch.object(serve, '_pixellab_configured', return_value=True), \
             mock.patch.object(serve, '_avatar_generation_available', return_value=True), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, '_state_abort'), \
             mock.patch.object(serve, '_avatar_set_complete', return_value=True), \
             mock.patch.object(sim, 'automation_paused', return_value=False):
            self.assertEqual(serve._queue_avatar_tasks(), 0)

    def test_queue_avatar_tasks_cooldown(self):
        state = {'agents': {'w1': {'name': 'W'}},
                 'agentRoster': [{'id': 'w1', 'director': 'd1'}],
                 '_avatarTaskAt': {'w1': 999000}}
        with mock.patch.object(serve, '_pixellab_configured', return_value=True), \
             mock.patch.object(serve, '_avatar_generation_available', return_value=True), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, '_state_abort'), \
             mock.patch.object(serve, '_avatar_set_complete', return_value=False), \
             mock.patch.object(serve, '_avatar_task_open', return_value=False), \
             mock.patch.object(serve.time, 'time', return_value=1000.0), \
             mock.patch.object(sim, 'automation_paused', return_value=False):
            self.assertEqual(serve._queue_avatar_tasks(), 0)

    def test_queue_avatar_tasks_responsible_missing(self):
        state = {'agents': {'w1': {'name': 'W'}},
                 'agentRoster': [{'id': 'w1', 'director': 'ghost'}]}
        with mock.patch.object(serve, '_pixellab_configured', return_value=True), \
             mock.patch.object(serve, '_avatar_generation_available', return_value=True), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, '_state_abort'), \
             mock.patch.object(serve, '_avatar_set_complete', return_value=False), \
             mock.patch.object(serve, '_avatar_task_open', return_value=False), \
             mock.patch.object(sim, 'automation_paused', return_value=False):
            self.assertEqual(serve._queue_avatar_tasks(), 0)

    def test_queue_avatar_tasks_batch_break(self):
        state = {'agents': {'w1': {'name': 'W'}, 'w2': {'name': 'W2'},
                            'd1': {'name': 'D'}},
                 'agentRoster': [{'id': 'w1', 'director': 'd1'},
                                 {'id': 'w2', 'director': 'd1'},
                                 {'id': 'd1', 'isDirector': True}]}

        def complete(agent_id):
            return agent_id != 'w1'

        with mock.patch.object(serve, '_pixellab_configured', return_value=True), \
             mock.patch.object(serve, '_avatar_generation_available', return_value=True), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, '_state_abort'), \
             mock.patch.object(serve, '_avatar_set_complete', side_effect=complete), \
             mock.patch.object(serve, '_avatar_task_open', return_value=False), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'AVATAR_TASK_BATCH_MAX', 1), \
             mock.patch.object(sim, 'automation_paused', return_value=False), \
             mock.patch.object(sim, 'queue_work') as queue:
            self.assertEqual(serve._queue_avatar_tasks(), 1)
        queue.assert_called_once()


class EscalationAdminTest(unittest.TestCase):
    def _call(self, state, decision, **patches):
        esc = {'kind': 'routine', 'question': 'q'}
        roster = {'admin': {'name': 'Admin'}}
        with mock.patch.object(serve, '_admin_agent_id',
                               return_value=patches.get('admin_id', 'admin')), \
             mock.patch.object(serve, '_jev_quorum_choice_sync',
                               return_value=(decision, patches.get('confidence', 0.9), 0)), \
             mock.patch.object(serve, '_jev_directory_score',
                               return_value=patches.get('composite', 1.0)), \
             mock.patch.object(serve, '_escalation_floor',
                               return_value=patches.get('floor', 0.0)), \
             mock.patch.object(serve, '_effective_safety_confidence',
                               return_value=patches.get('safety', 0.0)), \
             mock.patch.object(serve, '_jev_judge_model',
                               return_value=patches.get('judge_model')), \
             mock.patch.object(serve, '_escalation_judge_crosscheck',
                               return_value=patches.get('judge', ('approve', 0.9, 0))), \
             mock.patch.object(serve, 'log_action'):
            return serve._escalation_admin_attempt(
                state, 'e1', esc, 'routine', 'd1', roster)

    def test_missing_admin_and_unsure(self):
        self.assertFalse(self._call({}, 'approve', admin_id=None))
        esc = {'kind': 'routine', 'question': 'q', 'adminAskedAt': 1}
        with mock.patch.object(serve, '_admin_agent_id', return_value='admin'), \
             mock.patch.object(serve, 'log_action'):
            self.assertFalse(serve._escalation_admin_attempt(
                {}, 'e1', esc, 'routine', 'd1', {'admin': {'name': 'Admin'}}))
        self.assertFalse(self._call({}, 'maybe'))
        self.assertFalse(self._call({}, 'approve', composite=0.0, floor=0.5,
                                    safety=0.0))

    def test_judge_refuses(self):
        self.assertFalse(self._call({}, 'approve', judge_model='jm',
                                    judge=(None, 0.0, 0)))
        self.assertFalse(self._call({}, 'approve', judge_model='jm',
                                    judge=('deny', 0.9, 0)))

    def test_requester_director(self):
        state = {'agentRoster': [{'id': 'a1', 'director': 'd1'}]}
        self.assertEqual(
            serve._escalation_requester_director(state, {'agentId': 'a1'}), 'd1')
        self.assertIsNone(serve._escalation_requester_director(
            {'agentRoster': [{'id': 'a1'}]}, {'agentId': 'a1'}))


class ResolveEscalationsTest(unittest.TestCase):
    def _run(self, esc, **patches):
        self.edit = mock.Mock()

        with mock.patch.object(serve, '_load_escalations',
                               return_value={'e1': esc}), \
             mock.patch.object(serve, 'get_state_from_db', return_value={
                 'agentRoster': [{'id': 'd1'}, {'id': 'a1', 'director': 'd1'},
                                 {'id': 'admin1', 'isAdmin': True}]}), \
             mock.patch.object(serve, '_senior_most_director_id',
                               return_value=patches.get('senior', 'd1')), \
             mock.patch.object(serve, '_escalation_requester_director',
                               return_value='d1'), \
             mock.patch.object(serve, '_escalation_floor',
                               return_value=patches.get('floor', 0.0)), \
             mock.patch.object(serve, '_escalation_judge_drift',
                               return_value=patches.get('drift', 0)), \
             mock.patch.object(serve, '_escalation_admin_attempt',
                               return_value=patches.get('admin', False)), \
             mock.patch.object(serve, '_jev_quorum_choice_sync',
                               return_value=('approve', 0.9, 0)), \
             mock.patch.object(serve, '_jev_directory_score',
                               return_value=patches.get('score', 1.0)), \
             mock.patch.object(serve, '_effective_safety_confidence',
                               return_value=0.0), \
             mock.patch.object(serve, '_jev_judge_model', return_value=None), \
             mock.patch.object(serve, '_escalation_jev_errors', self.edit), \
             mock.patch.object(serve, 'notify_human_of_escalation'), \
             mock.patch.object(serve, '_save_escalations'), \
             mock.patch.object(serve, 'DIRECTOR_ESCALATION_REASK_COOLDOWN_S',
                               patches.get('cooldown', 0)), \
             mock.patch.object(serve, 'DIRECTOR_ESCALATION_MAX_ASKS',
                               patches.get('max_asks', 3)):
            return serve._resolve_pending_escalations_sync()

    def test_judge_drift_goes_straight_to_the_human(self):
        esc = {'status': 'pending', 'kind': 'routine', 'agentId': 'a1'}
        self.assertEqual(self._run(esc, drift=999), 0)
        self.assertTrue(esc['humanNotified'])
        self.assertEqual(self._run(esc, drift=999), 0)
        self.assertFalse(esc.get('_immediateTried'))

    def test_judge_drift_does_not_use_the_admin(self):
        esc = {'status': 'pending', 'kind': 'routine', 'agentId': 'a1',
               '_immediateTried': True}
        self.assertEqual(self._run(esc, drift=999, admin=True), 0)
        self.assertTrue(esc['humanNotified'])

    def test_unsure_immediate_advances_to_next_rung(self):
        esc = {'status': 'pending', 'kind': 'routine', 'agentId': 'a1'}
        self.assertEqual(self._run(esc, floor=0.9, score=0.0), 0)
        self.assertTrue(esc['_immediateTried'])

    def test_admin_resolves_after_the_director(self):
        esc = {'status': 'pending', 'kind': 'routine', 'agentId': 'a1',
               '_immediateTried': True}
        self.assertEqual(self._run(esc, admin=True), 1)
        self.assertFalse(esc.get('_adminTried'))

    def test_unsure_senior_marks_senior_tried(self):
        esc = {'status': 'pending', 'kind': 'routine', 'agentId': 'a1',
               '_immediateTried': True}
        self.assertEqual(self._run(esc, senior='d2', floor=0.9, score=0.0), 0)
        self.assertTrue(esc['_seniorTried'])

    def test_below_floor_resets_immediate(self):
        esc = {'status': 'pending', 'kind': 'routine', 'agentId': 'a1',
               'askCount': 0, 'lastAskedAt': 0}
        self.assertEqual(self._run(esc, floor=0.9, score=0.0), 0)
        self.assertTrue(esc['_immediateTried'])


class LifespanTest(unittest.TestCase):
    def test_failures_and_webhook_start_stop(self):
        async def _noop(*args, **kwargs):
            return None

        loop_names = (
            '_health_check_loop', '_openrouter_credits_probe_loop',
            '_director_approval_loop', '_backup_loop', '_log_prune_loop',
            '_model_tier_refresh_loop', '_model_tier_boot_backfill',
            '_photo_album_watch_loop', '_tier_price_watch_loop',
            '_tier_failure_recheck_loop', '_calibration_loop',
            '_weekly_review_loop', '_colab_failover_loop',
            '_colab_compute_idle_loop', '_telegram_poll_loop',
            '_webhook_poll_loop', '_pending_ask_drain_loop',
            '_avatar_task_loop', '_rss_poll_loop', '_ws_supervisor_loop',
            '_sim_watchdog_loop')
        fake_sim = types.ModuleType('sim')
        fake_sim._sim_loop = _noop
        with mock.patch.object(serve, '_ensure_runtime_schemas',
                               side_effect=RuntimeError('schema')), \
             mock.patch.object(serve, '_backfill_directors_in_db'), \
             mock.patch.object(serve, '_backfill_teams_in_db'), \
             mock.patch.object(serve, '_backfill_agent_identity_in_db'), \
             mock.patch.object(serve, '_init_templates_in_db', return_value=False), \
             mock.patch.object(serve, '_seed_watchlist_topics', return_value=False), \
             mock.patch.object(serve, 'get_state_from_db', return_value={'agents': {}}), \
             mock.patch.object(serve, 'active_webhook_channels',
                               return_value=['discord']), \
             mock.patch.object(serve, '_load_api_services'), \
             mock.patch.object(serve, '_load_rss_feeds'), \
             mock.patch.object(serve, '_load_ws_feeds'), \
             mock.patch.dict(sys.modules, {'sim': fake_sim}):
            for name in loop_names:
                mock.patch.object(serve, name, _noop).start()
                self.addCleanup(mock.patch.stopall)

            async def _run():
                async with serve._lifespan(serve.app):
                    await asyncio.sleep(0.01)

            asyncio.run(_run())


class CreditsPauseTest(unittest.TestCase):
    def test_invalid_remaining_and_notification_failures(self):
        with mock.patch.object(serve, '_CREDITS_PAUSE',
                               {'active': False, 'since': 0.0}), \
             mock.patch.object(serve, '_CREDITS_WATCHDOG_STATE', {'low': False}), \
             mock.patch.object(serve, 'send_player_telegram_sync',
                               side_effect=RuntimeError('tg')), \
             mock.patch.object(serve, 'log_action', side_effect=RuntimeError('log')):
            self.assertEqual(
                serve._set_credits_pause(True, credits={'remaining': 'bad'}), 'paused')
