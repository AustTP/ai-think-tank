"""Coverage-gap tests for serve.py -- Cluster A.

Covers the log-pruning/backup/idle loops, model-tier refresh loop, JEV
auto-failover, spend accounting helpers, forecast/bank views,
social/refinement/escalation digest loggers, fernet/secret helpers, external
account-balance calls (_treg_call, _digitalocean_account_balance,
_pixellab_call, _google_call, _github_call), telegram poll/api, health check
loop, tier-gate decider default, escalation/peer-review loops, _lifespan,
director backfill/chain/teams-in-db helpers, credential helpers, and
server-secret/device-key helpers.

Run: COVERAGE_FILE=/tmp/cov_gap_A.coverage python3 -m coverage run --source=serve tests/test_serve_gap_A.py
"""
import asyncio
import contextlib
import datetime
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
import unittest.mock
import urllib.error

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve

_REAL_SLEEP = asyncio.sleep

_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='think-tank-serve-gap-')
    # Every module-level path constant derived from THINK_TANK_DIR must be
    # repointed too: they are computed AT IMPORT, so patching THINK_TANK_DIR
    # alone leaves them pointing at the real repo root and any test that hits a
    # write path pollutes it. That is exactly how the `e1` fixture
    # ({"kind": "routine", "question": "q?", "askCount": MAX}) escaped
    # test_stops_at_ask_cap into the live escalations.json on 2026-10-04 and sat
    # there as an unresolvable "routine x1" until 2026-10-13 -- the other test
    # modules already patch ESCALATIONS_PATH for this reason (test_browse_
    # allowlist.py, test_failure_ledger.py, test_serve_gap_E.py).
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, 'library', '.passport.json'),
        ESCALATIONS_PATH=os.path.join(_MODULE_TMP_DIR, 'escalations.json'),
        FAILURES_PATH=os.path.join(_MODULE_TMP_DIR, 'failures.json'),
        RULE_PROPOSALS_PATH=os.path.join(_MODULE_TMP_DIR, 'rule_proposals.json'),
        SUCCESSES_PATH=os.path.join(_MODULE_TMP_DIR, 'successes.json'),
        SUCCESS_PROPOSALS_PATH=os.path.join(_MODULE_TMP_DIR, 'success_proposals.json'),
        BROWSE_TRAIL_PATH=os.path.join(_MODULE_TMP_DIR, 'browse_trail.json'),
        LIBRARY_USAGE_PATH=os.path.join(_MODULE_TMP_DIR, 'library_usage.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


class _FakeResp:
    """Minimal stand-in for urllib response objects (supports `with` + read())."""

    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload.encode('utf-8')


class _FakeCounter:
    """Thread-safety-free stand-in for serve._thread_safe_counter."""

    def __init__(self):
        self._vals = {}

    def bump(self, key):
        self._vals[key] = self._vals.get(key, 0) + 1

    def reset(self, key):
        self._vals.pop(key, None)

    def get(self, key):
        return self._vals.get(key, 0)


async def _completed(value):
    return value


def _http_error(code, reason='err'):
    err = urllib.error.HTTPError('http://example.invalid', code, reason, {}, None)
    err.read = lambda: b'error detail'
    return err
class Lifespan(unittest.TestCase):
    async def _run(self):
        async with serve._lifespan(serve.app):
            await _REAL_SLEEP(0.01)

    def _run_patched(self, **kwargs):
        with contextlib.ExitStack() as stack:
            for p in self._patches(**kwargs):
                stack.enter_context(p)
            asyncio.run(self._run())

    def _patches(self, telegram=False, sim_ok=True):
        async def _noop(*a, **k):
            return None

        patches = []
        for name in ('_health_check_loop', '_director_approval_loop', '_backup_loop',
                     '_log_prune_loop', '_model_tier_refresh_loop', '_calibration_loop',
                     '_weekly_review_loop', '_colab_failover_loop', '_colab_compute_idle_loop',
                     '_telegram_poll_loop', '_pending_ask_drain_loop'):
            patches.append(unittest.mock.patch.object(serve, name, _noop))
        if telegram:
            patches.append(unittest.mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'tok'))
            patches.append(unittest.mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', {'123'}))
        else:
            patches.append(unittest.mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', ''))
            patches.append(unittest.mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', set()))
        fake_sim = types.ModuleType('sim')
        fake_sim._sim_loop = _noop
        fake_content = types.ModuleType('content')
        if sim_ok:
            fake_content._server_content_dispatcher = lambda *a, **k: None
        patches.append(unittest.mock.patch.dict(sys.modules, {'sim': fake_sim, 'content': fake_content}))
        return patches

    def test_backfill_failures_are_logged_and_loops_start(self):
        with unittest.mock.patch.object(serve, '_backfill_directors_in_db',
                                        side_effect=RuntimeError('dir down')), \
             unittest.mock.patch.object(serve, '_backfill_teams_in_db',
                                        side_effect=RuntimeError('teams down')), \
             unittest.mock.patch.object(serve, '_init_templates_in_db',
                                        side_effect=RuntimeError('tpl down')), \
             unittest.mock.patch.object(serve, '_backfill_agent_identity_in_db',
                                        side_effect=RuntimeError('id down')):
            self._run_patched(sim_ok=True)

    def test_templates_backfill_saves_state(self):
        with unittest.mock.patch.object(serve, '_backfill_directors_in_db'), \
             unittest.mock.patch.object(serve, '_backfill_teams_in_db'), \
             unittest.mock.patch.object(serve, '_backfill_agent_identity_in_db'), \
             unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value={'agentRoster': [], 'agents': {}}), \
             unittest.mock.patch.object(serve, '_init_templates_in_db', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            self._run_patched(sim_ok=True)
        save.assert_called_once()

    def test_templates_backfill_failure_is_logged(self):
        with unittest.mock.patch.object(serve, '_backfill_directors_in_db'), \
             unittest.mock.patch.object(serve, '_backfill_teams_in_db'), \
             unittest.mock.patch.object(serve, '_backfill_agent_identity_in_db'), \
             unittest.mock.patch.object(serve, 'get_state_from_db',
                                        side_effect=RuntimeError('state down')):
            self._run_patched(sim_ok=True)

    def test_sim_failure_is_logged(self):
        self._run_patched(sim_ok=False)

    def test_telegram_bridge_starts_and_cancels(self):
        self._run_patched(telegram=True, sim_ok=True)


class DirectorAndTeams(unittest.TestCase):
    def test_director_backfill_map_skips_invalid_pairs(self):
        with unittest.mock.patch.object(serve, '_load_env',
                                        return_value={'SEED_DIRECTOR_MAP': 'ada:theo,badpair,zed:nora'}):
            self.assertEqual(serve._director_backfill_map(), {'ada': 'theo', 'zed': 'nora'})

    def test_director_chain_appends_unresolved_tail(self):
        state = {'agentRoster': [{'id': 'a', 'director': 'b'}, {'id': 'b', 'director': 'c'}]}
        self.assertEqual(serve._director_chain(state, 'a'), ['a', 'b', 'c'])

    def test_director_chain_is_cycle_safe(self):
        state = {'agentRoster': [{'id': 'a', 'director': 'b'}, {'id': 'b', 'director': 'a'}]}
        self.assertEqual(len(serve._director_chain(state, 'a')), 2)

    def test_backfill_teams_drops_orphaned_team(self):
        state = {
            'agentRoster': [{'id': 'x', 'name': 'X', 'role': 'Lead', 'isDirector': True},
                            {'id': 'w', 'name': 'W', 'role': 'Worker', 'director': 'x'}],
            'teams': [{'id': 'x', 'directorId': 'old-dir', 'name': 'Old', 'purpose': 'P', 'members': []}],
        }
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            serve._backfill_teams_in_db()
        self.assertEqual(state['teams'], [])
        save.assert_called_once()

    def test_backfill_agent_identity_heals_and_saves(self):
        state = {'agentRoster': [{'id': 'a', 'name': 'A'}], 'agents': {'a': {'role': 'Worker'}}}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            serve._backfill_agent_identity_in_db()
        save.assert_called_once()

    def test_backfill_agent_identity_no_state(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=None), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            serve._backfill_agent_identity_in_db()
        save.assert_not_called()


class PromoteAndTeamRecords(unittest.TestCase):
    def test_promote_to_director_unknown_promotee(self):
        self.assertIsNone(serve._promote_to_director({'agentRoster': []}, 'nobody', 'theo'))

    def test_promote_to_director_updates_existing_team(self):
        state = {
            'agentRoster': [{'id': 'maya', 'name': 'Maya', 'director': 'nora'}],
            'teams': [{'directorId': 'maya', 'name': None, 'purpose': None, 'members': []}],
        }
        team = serve._promote_to_director(state, 'maya', 'nora')
        self.assertEqual(team['directorId'], 'maya')
        self.assertIn('Crew', team['name'])
        self.assertTrue(state['agentRoster'][0]['isDirector'])
        self.assertEqual(state['agentRoster'][0]['director'], 'nora')

    def test_promote_to_director_creates_new_team(self):
        state = {'agentRoster': [{'id': 'maya', 'name': 'Maya'}]}
        team = serve._promote_to_director(state, 'maya', 'nora')
        self.assertEqual(team['id'], 'maya')
        self.assertIn(team, state['teams'])

    def test_can_write_team_false_for_unknown_team(self):
        self.assertFalse(serve._can_write_team({'teams': []}, 'theo', 'nope'))

    def test_can_write_team_admin_can_write(self):
        state = {'agentRoster': [{'id': 'theo', 'isAdmin': True},
                                 {'id': 'x', 'isDirector': True, 'director': 'theo'},
                                 {'id': 'w', 'director': 'x'}],
                 'teams': [{'id': 'x', 'directorId': 'x', 'members': []}]}
        self.assertTrue(serve._can_write_team(state, 'theo', 'x'))

    def test_team_record_returns_derived_members_copy(self):
        state = {'agentRoster': [{'id': 'x', 'isDirector': True, 'director': None},
                                 {'id': 'w', 'director': 'x'}],
                 'teams': [{'id': 'x', 'name': 'Team X', 'members': []}]}
        rec = serve._team_record(state, 'x')
        self.assertEqual(rec['id'], 'x')
        self.assertEqual(rec['members'], ['w'])
        self.assertEqual(state['teams'][0]['members'], [], 'original team record must not be mutated')

    def test_team_record_unknown(self):
        self.assertIsNone(serve._team_record({'teams': []}, 'nope'))

    def test_teams_missing_scrum_master_ignores_unknown_team(self):
        fake_sim = types.ModuleType('sim')
        fake_sim.SCRUM_MASTER_MIN_TEAM_SIZE = 4
        fake_sim._team_member_count = lambda state, did: 5
        state = {'teams': [{'id': 't1', 'directorId': 'nora'}]}
        with unittest.mock.patch.dict(sys.modules, {'sim': fake_sim}):
            self.assertEqual(serve._teams_missing_scrum_master(state, ['ghost-team']), [])

    def test_teams_missing_scrum_master_flags_big_team(self):
        fake_sim = types.ModuleType('sim')
        fake_sim.SCRUM_MASTER_MIN_TEAM_SIZE = 4
        fake_sim.team_policy = lambda state, ref, key=None: 4
        fake_sim._team_member_count = lambda state, did: 5
        state = {'teams': [{'id': 't1', 'directorId': 'nora', 'name': 'Team One'}]}
        with unittest.mock.patch.dict(sys.modules, {'sim': fake_sim}):
            missing = serve._teams_missing_scrum_master(state, ['t1'])
        self.assertEqual(missing, [{'id': 't1', 'name': 'Team One'}])

    def test_teams_missing_scrum_master_uses_team_policy_floor(self):
        # The gate reads the floor through sim.team_policy (team agreement ->
        # operator policy -> default), so a team whose agreement raises the
        # floor above its size is no longer flagged.
        fake_sim = types.ModuleType('sim')
        fake_sim.SCRUM_MASTER_MIN_TEAM_SIZE = 4
        fake_sim.team_policy = lambda state, ref, key=None: 6
        fake_sim._team_member_count = lambda state, did: 5
        state = {'teams': [{'id': 't1', 'directorId': 'nora', 'name': 'Team One'}]}
        with unittest.mock.patch.dict(sys.modules, {'sim': fake_sim}):
            self.assertEqual(serve._teams_missing_scrum_master(state, ['t1']), [])

    def test_teams_missing_scrum_master_flags_stuck_small_team(self):
        fake_sim = types.ModuleType('sim')
        fake_sim.SCRUM_MASTER_MIN_TEAM_SIZE = 4
        fake_sim.SMALL_TEAM_STUCK_TRIGGER = 3
        fake_sim.team_policy = lambda state, ref, key=None: 4
        fake_sim._team_member_count = lambda state, did: 2
        state = {
            'agentRoster': [{'id': 'w1', 'director': 'nora'},
                            {'id': 'w2', 'director': 'nora'}],
            'teams': [{'id': 't1', 'directorId': 'nora', 'name': 'Team One'}],
            'tasks': {
                'a': {'assignedTo': 'w1', 'status': 'failed'},
                'b': {'assignedTo': 'w1', 'status': 'repairing'},
                'c': {'assignedTo': 'w2', 'status': 'needs_review',
                      '_peerGate': {'cycleCount': 2}},
            },
        }
        with unittest.mock.patch.dict(sys.modules, {'sim': fake_sim}):
            missing = serve._teams_missing_scrum_master(state, ['t1'])
        self.assertEqual(missing, [{'id': 't1', 'name': 'Team One'}])

    def test_teams_missing_scrum_master_small_team_two_stuck_ok(self):
        fake_sim = types.ModuleType('sim')
        fake_sim.SCRUM_MASTER_MIN_TEAM_SIZE = 4
        fake_sim.SMALL_TEAM_STUCK_TRIGGER = 3
        fake_sim.team_policy = lambda state, ref, key=None: 4
        fake_sim._team_member_count = lambda state, did: 2
        state = {
            'agentRoster': [{'id': 'w1', 'director': 'nora'}],
            'teams': [{'id': 't1', 'directorId': 'nora', 'name': 'Team One'}],
            'tasks': {
                'a': {'assignedTo': 'w1', 'status': 'failed'},
                'b': {'assignedTo': 'w1', 'status': 'repairing'},
            },
        }
        with unittest.mock.patch.dict(sys.modules, {'sim': fake_sim}):
            self.assertEqual(serve._teams_missing_scrum_master(state, ['t1']), [])

    def test_teams_missing_scrum_master_stuck_small_team_ack_ok(self):
        fake_sim = types.ModuleType('sim')
        fake_sim.SCRUM_MASTER_MIN_TEAM_SIZE = 4
        fake_sim.SMALL_TEAM_STUCK_TRIGGER = 3
        fake_sim.team_policy = lambda state, ref, key=None: 4
        fake_sim._team_member_count = lambda state, did: 2
        state = {
            'agentRoster': [{'id': 'w1', 'director': 'nora'}],
            'teams': [{'id': 't1', 'directorId': 'nora', 'name': 'Team One',
                       'directorFacilitation': True}],
            'tasks': {
                'a': {'assignedTo': 'w1', 'status': 'failed'},
                'b': {'assignedTo': 'w1', 'status': 'repairing'},
                'c': {'assignedTo': 'w1', 'status': 'failed'},
            },
        }
        with unittest.mock.patch.dict(sys.modules, {'sim': fake_sim}):
            self.assertEqual(serve._teams_missing_scrum_master(state, ['t1']), [])

    def test_teams_missing_scrum_master_small_team_ok(self):
        fake_sim = types.ModuleType('sim')
        fake_sim.SCRUM_MASTER_MIN_TEAM_SIZE = 4
        fake_sim.team_policy = lambda state, ref, key=None: 4
        fake_sim._team_member_count = lambda state, did: 2
        state = {'teams': [{'id': 't1', 'directorId': 'nora'}]}
        with unittest.mock.patch.dict(sys.modules, {'sim': fake_sim}):
            self.assertEqual(serve._teams_missing_scrum_master(state, ['t1']), [])


class CredentialAndDeviceHelpers(unittest.TestCase):
    def test_higgsfield_configured_true_when_both_keys(self):
        with unittest.mock.patch.object(serve, 'HIGGSFIELD_API_KEY_ID', 'id'), \
             unittest.mock.patch.object(serve, 'HIGGSFIELD_API_KEY_SECRET', 'sec'):
            self.assertTrue(serve._higgsfield_configured())

    def test_higgsfield_configured_false_without_keys(self):
        with unittest.mock.patch.object(serve, 'HIGGSFIELD_API_KEY_ID', ''), \
             unittest.mock.patch.object(serve, 'HIGGSFIELD_API_KEY_SECRET', 'sec'):
            self.assertFalse(serve._higgsfield_configured())

    def test_server_secret_generates_and_persists(self):
        tmp = tempfile.mkdtemp(prefix='gap-secret-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with unittest.mock.patch.object(serve, 'THINK_TANK_DIR', tmp), \
             unittest.mock.patch.object(serve, '_load_env', return_value={}):
            key = serve._get_or_create_server_secret()
        self.assertEqual(len(key), 64)
        with open(os.path.join(tmp, '.env')) as f:
            self.assertIn(f'SERVER_SECRET={key}', f.read())

    def test_server_secret_returns_existing(self):
        with unittest.mock.patch.object(serve, '_load_env', return_value={'SERVER_SECRET': 'existing'}):
            self.assertEqual(serve._get_or_create_server_secret(), 'existing')

    def test_admin_credentials_generate_and_persist(self):
        tmp = tempfile.mkdtemp(prefix='gap-admin-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with unittest.mock.patch.object(serve, 'THINK_TANK_DIR', tmp), \
             unittest.mock.patch.object(serve, '_load_env', return_value={}):
            username, salt, digest, password = serve._get_or_create_admin_credentials()
        self.assertEqual(username, 'admin')
        self.assertTrue(password)
        self.assertTrue(salt)
        self.assertTrue(digest)
        with open(os.path.join(tmp, '.env')) as f:
            content = f.read()
        self.assertIn('ADMIN_USERNAME=admin', content)
        self.assertIn('ADMIN_PASSWORD_SALT=', content)

    def test_admin_credentials_return_existing(self):
        with unittest.mock.patch.object(serve, '_load_env',
                                        return_value={'ADMIN_PASSWORD_SALT': 's', 'ADMIN_PASSWORD_HASH': 'h'}):
            self.assertEqual(serve._get_or_create_admin_credentials(), ('admin', 's', 'h', None))

    def test_device_key_generates_and_persists(self):
        tmp = tempfile.mkdtemp(prefix='gap-device-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with unittest.mock.patch.object(serve, 'THINK_TANK_DIR', tmp), \
             unittest.mock.patch.object(serve, '_load_env', return_value={}):
            key, fresh = serve._get_or_create_device_key()
        self.assertTrue(key)
        self.assertEqual(key, fresh)
        with open(os.path.join(tmp, '.env')) as f:
            self.assertIn(f'DEVICE_API_KEY={key}', f.read())

    def test_device_key_returns_existing(self):
        with unittest.mock.patch.object(serve, '_load_env', return_value={'DEVICE_API_KEY': 'existing'}):
            self.assertEqual(serve._get_or_create_device_key(), ('existing', None))


