"""Split part of test_serve.py (serve.py behavior tests)."""
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse

import asyncio
import openpyxl

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
import sim  # noqa: E402
from test_serve_helpers import setUpModule, tearDownModule  # noqa: E402


class FlatOrgChartDerivations(unittest.TestCase):
    # Pure derivations over a compliant flat chart: the admin, one
    # director directly under the admin, and workers under that director.
    def _state(self):
        return {
            'agentRoster': [
                {'id': 'theo', 'isAdmin': True, 'isDirector': True},
                {'id': 'nora', 'isDirector': True, 'director': 'theo'},
                {'id': 'ada', 'director': 'nora'},
                {'id': 'ben', 'director': 'nora'},
            ],
        }

    def test_supervisor_of(self):
        s = self._state()
        self.assertEqual(serve._supervisor_of(s, 'ada'), 'nora')
        self.assertEqual(serve._supervisor_of(s, 'nora'), 'theo')
        # The admin answers to the player, not to any agent.
        self.assertEqual(serve._supervisor_of(s, 'theo'), 'player')

    def test_is_direct_supervisor(self):
        s = self._state()
        self.assertTrue(serve._is_direct_supervisor(s, 'nora', 'ada'))
        # A grand-supervisor is not a DIRECT supervisor; nor is a
        # worker, nor oneself.
        self.assertFalse(serve._is_direct_supervisor(s, 'theo', 'ada'))
        self.assertFalse(serve._is_direct_supervisor(s, 'ada', 'nora'))
        self.assertFalse(serve._is_direct_supervisor(s, 'ada', 'ada'))

    def test_subtree_agent_ids(self):
        s = self._state()
        self.assertEqual(serve._subtree_agent_ids(s, 'nora'),
                         ['nora', 'ada', 'ben'])
        self.assertEqual(serve._subtree_agent_ids(s, 'theo'),
                         ['theo', 'nora', 'ada', 'ben'])

    def test_flat_chart_violations_mixed_roster(self):
        # Only a director (flagged, or with direct reports) whose own
        # pointer climbs to a non-admin is a violation: a pointer-less
        # director answers to the admin by fallback, and a pure worker
        # under a director is the legal worker shape.
        s = {
            'agentRoster': [
                {'id': 'theo', 'isAdmin': True, 'isDirector': True,
                 'director': None},
                {'id': 'nora', 'isDirector': True, 'director': 'theo'},
                {'id': 'ruth', 'isDirector': True, 'director': 'nora'},
                {'id': 'ada', 'director': 'ruth'},
                {'id': 'dev', 'director': None},
                {'id': 'eli', 'director': 'dev'},
            ],
        }
        self.assertEqual(serve._flat_director_chart_violations(s), ['ruth'])


class FlatOrgChart(unittest.TestCase):
    # The org chart is FLAT: every director sits DIRECTLY under the
    # admin, never under another director. The boot repair sweep
    # flattens any director-under-director pointer up to the admin, and
    # POST /api/state (the one client-owned roster writer) rejects a
    # roster that would create one.
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-flat-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        self._env_patch = unittest.mock.patch.object(
            serve, '_load_env', return_value={
                'SEED_ROSTER': '',
                'SEED_DIRECTOR_MAP': '',
                'SEED_ADMIN_IDS': 'theo',
                'SEED_SENIOR_DIRECTOR': 'nora',
            })
        self._env_patch.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        self._env_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _install_state(self, roster):
        # No 'sim' key: with sim.owner == 'server' the merge would
        # carry the installed roster forward over the incoming one.
        state = {
            'agentRoster': roster,
            'agents': {d['id']: {'profile': {}, 'role': 'Research'} for d in roster},
        }
        serve.save_state_to_db(state)
        return state

    def test_backfill_flattens_director_under_director(self):
        # ruth is a flagged director pointing at nora (a director). The
        # sweep must move her up to the admin (theo); ada's
        # worker->director pointer is legal and untouched.
        self._install_state([
            {'id': 'theo', 'isAdmin': True, 'isDirector': True, 'director': None},
            {'id': 'nora', 'isDirector': True, 'director': None},
            {'id': 'ruth', 'isDirector': True, 'director': 'nora'},
            {'id': 'ada', 'director': 'ruth'},
        ])
        serve._backfill_directors_in_db()
        by_id = {d['id']: d for d in serve.get_state_from_db()['agentRoster']}
        self.assertEqual(by_id['ruth']['director'], 'theo',
                         'a director must sit directly under the admin')
        self.assertEqual(by_id['ada']['director'], 'ruth')

    def test_backfill_flattens_derived_director_under_director(self):
        # dev is a director by STRUCTURE (a direct report, nadia) but
        # carries no isDirector flag, and points at nora. The sweep
        # flattens derived directors too.
        self._install_state([
            {'id': 'theo', 'isAdmin': True, 'isDirector': True, 'director': None},
            {'id': 'nora', 'isDirector': True, 'director': None},
            {'id': 'dev', 'director': 'nora'},
            {'id': 'nadia', 'director': 'dev'},
        ])
        serve._backfill_directors_in_db()
        by_id = {d['id']: d for d in serve.get_state_from_db()['agentRoster']}
        self.assertEqual(by_id['dev']['director'], 'theo')
        self.assertEqual(by_id['nadia']['director'], 'dev')

    def test_post_state_rejects_director_under_director(self):
        # A roster that would place a director under a director is
        # rejected 400 naming the offender, and never saved.
        self._install_state([
            {'id': 'theo', 'isAdmin': True, 'isDirector': True, 'director': None},
            {'id': 'nora', 'isDirector': True, 'director': None},
        ])
        roster = [
            {'id': 'theo', 'isAdmin': True, 'isDirector': True, 'director': None},
            {'id': 'nora', 'isDirector': True, 'director': None},
            {'id': 'ruth', 'isDirector': True, 'director': 'nora'},
        ]
        saved = []
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
                unittest.mock.patch.object(serve, 'save_state_to_db',
                                           side_effect=lambda s: saved.append(s)):
            c = TestClient(serve.app, raise_server_exceptions=False)
            r = c.post('/api/state', json={'agentRoster': roster})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('ruth', r.json()['error'])
        self.assertEqual(saved, [])

    def test_post_state_accepts_flat_roster(self):
        self._install_state([
            {'id': 'theo', 'isAdmin': True, 'isDirector': True, 'director': None},
            {'id': 'nora', 'isDirector': True, 'director': None},
        ])
        roster = [
            {'id': 'theo', 'isAdmin': True, 'isDirector': True, 'director': None},
            {'id': 'nora', 'isDirector': True, 'director': 'theo'},
            {'id': 'ada', 'director': 'nora'},
        ]
        saved = []
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
                unittest.mock.patch.object(serve, 'save_state_to_db',
                                           side_effect=lambda s: saved.append(s)):
            c = TestClient(serve.app)
            r = c.post('/api/state', json={'agentRoster': roster})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.text, 'saved')
        self.assertEqual(len(saved), 1)


class DirectorOwnedTemplates(unittest.TestCase):
    # Directors + admin may author the role-template library; rank-and-file may
    # not. Director-ness is structural (has direct reports, or is admin), not
    # reliant on the under-populated isDirector boolean -- matching the
    # walk-the-chain model. Edits retroactively re-stamp live holders.
    def _state(self, agents=None, templates=None):
        roster = [
            {'id': 'faye', 'isAdmin': True, 'isDirector': True},
            {'id': 'nora', 'isDirector': True},
            {'id': 'dev', 'director': 'faye'},
            {'id': 'sam', 'director': 'faye'},
            {'id': 'maya', 'director': 'sam'},   # sam has a direct report -> director
            {'id': 'nadia', 'director': 'dev'},  # dev has a direct report -> director
            {'id': 'zara', 'director': 'nora'},
        ]
        agents = agents or {d['id']: {'profile': {}, 'role': 'Research'} for d in roster}
        state = {'agentRoster': roster, 'agents': agents}
        if templates is not None:
            state['templates'] = templates
        return state

    def test_director_is_structural_not_flag_dependent(self):
        # dev and sam carry NO isDirector flag (the backfill only stamps top
        # tier) but each has a direct report -- the walk-the-chain definition
        # still makes them directors for template authorship.
        s = self._state()
        self.assertTrue(serve._is_director(s, 'dev'))
        self.assertTrue(serve._is_director(s, 'sam'))
        self.assertTrue(serve._is_director_or_admin(s, 'dev'))
        self.assertTrue(serve._is_director_or_admin(s, 'faye'))
        self.assertTrue(serve._is_director_or_admin(s, 'nora'))

    def test_rank_and_file_is_not_a_director(self):
        s = self._state()
        self.assertFalse(serve._is_director(s, 'maya'), 'maya has no direct reports')
        self.assertFalse(serve._is_director_or_admin(s, 'nadia'))
        self.assertFalse(serve._is_director_or_admin(s, 'zara'))

    def test_profile_for_role_prefers_db_then_seed_then_default(self):
        # DB template wins.
        s = self._state(templates={'Research': {'mission': 'DB mission', 'instructions': ['db rule'], 'notes': []}})
        p = serve._profile_for_role(s, 'Research')
        self.assertEqual(p['mission'], 'DB mission')
        self.assertEqual(p['instructions'], ['db rule'])
        # No DB entry -> seed constant is the fallback.
        s2 = self._state(templates={})
        p2 = serve._profile_for_role(s2, 'Research')
        self.assertEqual(p2['mission'], serve._SEED_PROFILES['Research']['mission'])
        # Unknown role with no DB entry and no seed -> generic fallback.
        p3 = serve._profile_for_role(s2, 'Fishing Guide')
        self.assertEqual(p3['mission'], serve._DEFAULT_PROFILE['mission'])
        # Handed out by value, never by reference to the shared constant.
        p4 = serve._profile_for_role(s2, 'Research')
        p4['mission'] = 'MUTATED'
        self.assertNotEqual(p4['mission'], serve._SEED_PROFILES['Research']['mission'])
        # Calling _profile_for_role must not backfill/populate the library.
        self.assertEqual(s2['templates'], {})

    def test_init_templates_backfills_seed_once_only(self):
        s = self._state(templates=None)
        self.assertTrue(serve._init_templates_in_db(s))
        self.assertEqual(set(s['templates']), set(serve._SEED_PROFILES))
        # Second run is a no-op and never clobbers a director's edits.
        s['templates']['Research']['mission'] = 'edited by nora'
        self.assertFalse(serve._init_templates_in_db(s))
        self.assertEqual(s['templates']['Research']['mission'], 'edited by nora')

    def test_init_templates_runs_on_an_existing_db_state(self):
        # A live think_tank.db has no 'templates' key until the one-time startup
        # migration adds it -- must backfill from seed without a seed event.
        s = self._state(templates=None)
        self.assertNotIn('templates', s)  # simulates a pre-think tank DB without the key
        self.assertTrue(serve._init_templates_in_db(s))
        self.assertEqual(set(s['templates']), set(serve._SEED_PROFILES))
        # And it leaves unrelated state untouched.
        self.assertEqual(len(s['agentRoster']), 7)
        self.assertEqual(len(s['agents']), 7)

    def test_init_templates_never_overwrites_existing_library(self):
        # Migration must not clobber a library that was already written.
        s = self._state(templates={'Research': {'mission': 'director-authored', 'instructions': [], 'notes': []}})
        self.assertFalse(serve._init_templates_in_db(s))
        self.assertEqual(s['templates']['Research']['mission'], 'director-authored')

    def test_autosave_merge_carries_server_owned_fields_forward(self):
        # ROOT CAUSE of the live bug: agents.js saveState POSTs a fixed field
        # list every 5s, and post_state used to save_state_to_db(data) wholesale
        # -- so the server-owned templates (and any future server-only key) were
        # dropped on the very next autosave. _merge_server_owned must carry any
        # existing key the incoming payload omits.
        existing = {
            'agents': {'a': {'role': 'Research'}},
            'agentRoster': [{'id': 'a'}],
            'templates': {'Research': {'mission': 'keep me', 'instructions': [], 'notes': []}},
            'modelTiersDirty': True,
        }
        incoming = {'agents': {'a': {'role': 'Research'}}, 'agentRoster': [{'id': 'a'}]}  # saveState payload
        merged = serve._merge_server_owned(existing, incoming)
        self.assertEqual(merged['agents'], existing['agents'], 'client-owned game fields replaced')
        self.assertEqual(merged['templates']['Research']['mission'], 'keep me', 'server-owned templates survive')
        self.assertIs(merged['modelTiersDirty'], True, 'any omitted key is carried forward')
        self.assertEqual(set(merged), set(existing), 'merge is a superset of existing')

    def test_autosave_merge_client_changes_still_win(self):
        existing = {'templates': {}, 'agents': {'a': {'role': 'Research'}}}
        incoming = {'agents': {'a': {'role': 'Banking'}}, 'templates': {'X': {'mission': 'm', 'instructions': [], 'notes': []}}}
        merged = serve._merge_server_owned(existing, incoming)
        self.assertEqual(merged['agents']['a']['role'], 'Banking', 'client wins on client-owned fields')
        self.assertEqual(merged['templates']['X']['mission'], 'm', 'client-provided templates replace')

    def test_autosave_merge_server_cadence_stamp_wins_over_stale_client(self):
        # The sentinel return: a stale /api/state POST (an old tab
        # with pre-fix clients.js) carries lastSkillReviewAt: 1e18 in its copy
        # of the blob. _merge_server_owned previously accepted the client's copy
        # (top-level keys the client SENDS win), so the server's corrected
        # now_ms was reverted to the sentinel every 5s, permanently re-disabling
        # the standing skill-review sweep. Server cadence stamps must win.
        existing = {'sim': {'owner': 'server'},
                    'lastSkillReviewAt': 1_725_000_000_000}
        stale_incoming = {'sim': {'owner': 'server'},
                          'lastSkillReviewAt': 1_000_000_000_000_000_000}
        merged = serve._merge_server_owned(existing, stale_incoming)
        self.assertEqual(merged['lastSkillReviewAt'], existing['lastSkillReviewAt'],
                         'server cadence stamp wins over a stale client copy')
        # And a client that omits it entirely must not drop the server's value.
        omitting = {'sim': {'owner': 'server'}}
        merged2 = serve._merge_server_owned(existing, omitting)
        self.assertEqual(merged2['lastSkillReviewAt'], existing['lastSkillReviewAt'],
                         'omitted cadence stamp is carried forward')

    def test_server_ownership_merge_preserves_server_positions(self):
        # Phase-2 flip defense: once sim.owner == 'server', the client's 5s
        # autosave (a renderer's saveState) must NOT clobber positions the
        # server just advanced. Non-spatial client fields still land; spatial
        # server-owned fields are carried forward verbatim.
        existing = {
            'sim': {'owner': 'server', 'tick': 12},
            'agents': {'ada': {'x': 100, 'y': 50, 'dir': 'east', 'path': [{'x': 200, 'y': 50}],
                               'busy': True, 'inRoom': 'weatherstation'}},
        }
        # Client autosave reports ada at a stale position and busy=False.
        incoming = {'agents': {'ada': {'x': 10, 'y': 10, 'dir': 'north', 'path': None,
                                       'busy': False, 'inRoom': None}}}
        merged = serve._merge_server_owned(existing, incoming)
        a = merged['agents']['ada']
        self.assertEqual(a['x'], 100, 'server x is authoritative under the flip')
        self.assertEqual(a['y'], 50, 'server y is authoritative under the flip')
        self.assertEqual(a['dir'], 'east', 'server dir is authoritative')
        self.assertEqual(a['path'], [{'x': 200, 'y': 50}], 'server path is authoritative')
        # Client non-spatial fields still land (the renderer's own state edits).
        self.assertFalse(a['busy'], 'client-owned non-spatial fields still win')
        self.assertIsNone(a['inRoom'])

    def test_client_ownership_merge_still_allows_client_positions(self):
        # Before the flip (owner != 'server'), the client is still the mover and
        # its positions must win -- only the server-ownership branch protects them.
        existing = {'sim': {'owner': 'client'}, 'agents': {'ada': {'x': 100, 'y': 50}}}
        incoming = {'agents': {'ada': {'x': 10, 'y': 10}}}
        merged = serve._merge_server_owned(existing, incoming)
        self.assertEqual(merged['agents']['ada']['x'], 10, 'client owns movement before the flip')
        self.assertEqual(merged['agents']['ada']['y'], 10)

    def test_server_ownership_merge_preserves_task_lifecycle_state(self):
        # Phase 3: under server ownership the server is the authoritative writer
        # of workQueue + the durable task mirror. A client autosave (which also
        # sends workQueue) must not clobber the server's mid-cycle requeues/
        # assignments with a stale copy.
        existing = {
            'sim': {'owner': 'server', 'tick': 12},
            'agents': {'ada': {'x': 100, 'y': 50, 'busy': True, 'task': 'task-1'}},
            'workQueue': [{'title': 'Server queued', 'room': 'pressoffice'}],
            'tasks': {'task-1': {'id': 'task-1', 'status': 'walking', 'assignedTo': 'ada'}},
        }
        # Client autosave: stale/empty queue + no tasks key at all.
        incoming = {'agents': {'ada': {'x': 10, 'y': 10, 'busy': True, 'task': 'task-1'}},
                    'workQueue': []}
        merged = serve._merge_server_owned(existing, incoming)
        self.assertEqual(merged['workQueue'], [{'title': 'Server queued', 'room': 'pressoffice'}],
                         'server queue wins under the flip')
        self.assertEqual(merged['tasks'], {'task-1': {'id': 'task-1', 'status': 'walking',
                                                      'assignedTo': 'ada'}},
                         'server task mirror is carried into the merged state')
        # Position still protected.
        self.assertEqual(merged['agents']['ada']['x'], 100)

    def test_apply_template_restamps_every_live_holder(self):
        template = {'mission': 'New mission', 'instructions': ['new rule'], 'notes': []}
        agents = {
            'ada': {'role': 'Research', 'profile': {'mission': 'old', 'instructions': [], 'notes': []}},
            'ben': {'role': 'Research', 'profile': {'mission': 'old', 'instructions': [], 'notes': []}},
            'cora': {'role': 'Banking', 'profile': {'mission': 'keep', 'instructions': [], 'notes': []}},
        }
        s = self._state(agents=agents, templates={'Research': template})
        # Patch side-effecting writes so the pure re-stamp logic can be tested.
        with unittest.mock.patch.object(serve, 'save_state_to_db'), \
             unittest.mock.patch.object(serve, 'log_action'):
            applied = serve._apply_template_to_role(s, 'Research', template, 'nora')
        self.assertEqual(set(applied), {'ada', 'ben'})
        self.assertEqual(s['agents']['ada']['profile']['mission'], 'New mission')
        self.assertEqual(s['agents']['ben']['profile']['instructions'], ['new rule'])
        # Unrelated role untouched, notes copied fresh by value.
        self.assertEqual(s['agents']['cora']['profile']['mission'], 'keep')
        self.assertEqual(s['agents']['ada']['profile']['notes'], [])

    def test_apply_template_noop_when_role_has_no_holders(self):
        template = {'mission': 'm', 'instructions': [], 'notes': []}
        s = self._state(agents={'ada': {'role': 'Research', 'profile': {}}}, templates={'Orchard': template})
        with unittest.mock.patch.object(serve, 'save_state_to_db') as save, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            applied = serve._apply_template_to_role(s, 'Orchard', template, 'nora')
        self.assertEqual(applied, [])
        save.assert_not_called()
        log.assert_not_called()


class RoomDefinitions(unittest.TestCase):
    # Phase 4/5: the planner-facing description of each room lives in DB
    # state (director-editable), not a hardcoded copy -- so a director's
    # purpose edit reaches BOTH the server intent planner and the browser
    # delegate path. Labels are locked; only purposes change.
    def _state(self):
        return {'agentRoster': [
            {'id': 'faye', 'isAdmin': True},
            {'id': 'nora', 'isDirector': True},
            {'id': 'zara', 'director': 'nora'},
        ], 'agents': {}}

    def test_room_definitions_backfills_all_seed_rooms_on_a_legacy_db(self):
        # A live think_tank.db predates roomDefinitions -- the lazy backfill must
        # add every seed room (delegable work rooms PLUS the hangout, which is
        # enterable-but-not-delegable) without a seed event.
        s = {}
        defs = serve._room_definitions(s)
        self.assertEqual(set(defs), set(serve._DEFAULT_ROOM_DEFINITIONS))
        self.assertEqual(defs['pressoffice']['label'], 'Work Room')
        self.assertTrue(defs['pressoffice']['purpose'])
        self.assertFalse(serve._DEFAULT_ROOM_DEFINITIONS == ({}), 'seed defaults exist')

    def test_room_definitions_never_overwrites_a_director_purpose(self):
        # Backfill fills only missing rooms; an existing, director-edited
        # purpose must survive a re-read.
        s = {'roomDefinitions': {'pressoffice': {'label': 'Work Room', 'purpose': 'director rewrote this'}}}
        defs = serve._room_definitions(s)
        self.assertEqual(defs['pressoffice']['purpose'], 'director rewrote this')
        # And the rooms the director didn't touch still get defaults.
        self.assertEqual(defs['observatory']['purpose'], serve._DEFAULT_ROOM_DEFINITIONS['observatory']['purpose'])

    def test_room_definitions_fills_a_missing_label_from_seed(self):
        # Labels can't be edited, so a label hole (e.g. from a hand-edited DB)
        # is repaired from the seed; the purpose is left as-is.
        s = {'roomDefinitions': {'media': {'purpose': 'kept'}}}
        defs = serve._room_definitions(s)
        self.assertEqual(defs['media']['label'], 'Studio')
        self.assertEqual(defs['media']['purpose'], 'kept')

    def test_merge_server_owned_keeps_server_roster_over_client_copy(self):
        # ROOT CAUSE defense for Phase 4: governance (auto-hire/fire) mutates
        # the canonical roster server-side. The client's 5s autosave sends ITS
        # stale AGENT_ROSTER copy, which would revert a server hire/fire.
        existing = {
            'sim': {'owner': 'server'},
            'agentRoster': [{'id': 'faye', 'isAdmin': True}, {'id': 'NEW_HIRE', 'name': 'maya'}],
            'agents': {'faye': {}, 'maya': {}},
        }
        incoming = {'agentRoster': [{'id': 'faye', 'isAdmin': True}, {'id': 'fired_dev'}], 'agents': {'faye': {}}}
        merged = serve._merge_server_owned(existing, incoming)
        self.assertEqual([d['id'] for d in merged['agentRoster']], ['faye', 'NEW_HIRE'],
                         'server roster (with governance hire) wins over the stale client copy')


class AgentFileVisibility(unittest.TestCase):
    # Self-reports rule: an agent must NOT be able to view or
    # modify its OWN reports/ directory, but can write into others'. Also: no
    # app source visible, dotfiles/hidden filtered, traversal contained.
    def test_visible_plain_path(self):
        self.assertTrue(serve._agent_rel_path_is_visible(['notes', 'todo.md']))

    def test_hidden_dotfile_and_skipped_dirs_hidden(self):
        self.assertFalse(serve._agent_rel_path_is_visible(['.git']))
        self.assertFalse(serve._agent_rel_path_is_visible(['.DS_Store']))
        self.assertFalse(serve._agent_rel_path_is_visible(['conversations']))
        self.assertFalse(serve._agent_rel_path_is_visible(['.env']))

    def test_is_under_reports_detects_reports_root(self):
        self.assertTrue(serve.is_under_reports('reports/report-1.md'))
        self.assertTrue(serve.is_under_reports('reports/sub/x.md'))
        self.assertFalse(serve.is_under_reports('agent.json'))
        self.assertFalse(serve.is_under_reports('prototypes/x.py'))

    def test_owns_reports_dir_only_applies_to_self(self):
        self.assertTrue(serve._owns_reports_dir('nadia', 'nadia'))
        self.assertFalse(serve._owns_reports_dir('nadia', 'dev'))   # someone else is fine
        self.assertFalse(serve._owns_reports_dir('nadia', 'player'))
        self.assertFalse(serve._owns_reports_dir('nadia', None))
        self.assertFalse(serve._owns_reports_dir('nadia', 'unknown'))

    def test_path_components_under_root(self):
        self.assertEqual(serve.path_components('/a/b', '/a/b'), [])
        self.assertEqual(serve.path_components('/a/b/reports', '/a/b'), ['reports'])
        self.assertEqual(serve.path_components('/a/b/reports/x', '/a/b'), ['reports', 'x'])


class AppSourceIsolation(unittest.TestCase):
    # "They should not be able to view the source code for the application
    # itself": library reads are confined to LIBRARY_DIR, agent files to
    # AGENTS_DIR (both outside world/), and the sandbox mounts only the
    # sandbox dir. Verify the confinement helpers reject a path that would
    # escape to serve.py / think_tank.db / world JS.
    def setUp(self):
        self.prev_db = serve.DB_PATH
        self.prev_agents = serve.AGENTS_DIR
        self.tmp = tempfile.mkdtemp(prefix='think-tank-appsrc-test-')
        serve.AGENTS_DIR = os.path.join(self.tmp, 'agents')

    def tearDown(self):
        serve.DB_PATH = self.prev_db
        serve.AGENTS_DIR = self.prev_agents
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_library_path_rejects_escapes_to_app_files(self):
        # A library-relative path must resolve strictly inside LIBRARY_DIR.
        self.assertIsNone(serve._safe_library_path('../../../world/serve.py'))
        self.assertIsNone(serve._safe_library_path('../../.env'))
        self.assertIsNone(serve._safe_library_path('..'))
        self.assertIsNone(serve._safe_library_path('../../think_tank.db'))

    def test_agent_file_read_denies_traversal_to_server_source(self):
        # list/read containment is enforced via path normalization in the
        # handlers, but the visibility helper here confirms the valid-shaped
        # inputs that name app-source are not under any whitelisted agent path.
        self.assertFalse(serve._agent_rel_path_is_visible(['..', 'serve.py']))
        self.assertFalse(serve._agent_rel_path_is_visible(['..', '..', 'world', 'index.html']))

    def test_agent_folders_empty_until_state_saved(self):
        # Nothing under AGENTS_DIR is served by any public route -- the only
        # way agent files exist is the server's own materialization, which is
        # itself confined to AGENTS_DIR (outside world/ where serve.py lives).
        self.assertFalse(os.path.commonpath([serve.AGENTS_DIR, os.path.dirname(serve.__file__)])
                         == serve.AGENTS_DIR)


class ReportCrossWrite(unittest.TestCase):
    # Agent-filed reports land in ANOTHER agent's reports/ directory; an agent
    # cannot file about itself (keeps its own off-limits reports/ dir free of
    # self-referential writes). Pure-logic verification of the gating helpers
    # plus the materialization path via a temp DB.
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-report-test-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()
        serve.save_state_to_db({
            'agentRoster': [{'id': 'ada'}, {'id': 'nadia'}, {'id': 'dev'}],
            'agents': {'ada': {}, 'nadia': {}, 'dev': {}},
            'reports': [], 'nextReportId': 1,
        })

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post_report(self, about_id, from_id, quote='q', note='n', key=None):
        import json as _j
        from unittest.mock import MagicMock
        req = MagicMock()
        req.headers = {'X-Agent-Key': key} if key else {}
        req.json = unittest.mock.AsyncMock(return_value={
            'aboutId': about_id, 'fromId': from_id, 'quote': quote, 'note': note,
        })
        # Post the body through a plain wrapper -- but post_report is async;
        # run it via asyncio.
        return req

    def test_materialization_puts_report_in_target_reports_dir(self):
        # A report about 'nadia' by 'dev' is materialized into
        # agents/nadia/reports/ -- the target's own (hidden-from-her) dir.
        state = serve.get_state_from_db()
        report = {
            'id': 'report-1-1234-nadia', 'aboutId': 'nadia', 'fromId': 'dev',
            'quote': 'q', 'note': 'n', 'ts': 1, 'severity': 'minor',
        }
        state['reports'] = [report]
        serve.save_state_to_db(state)
        target_md = os.path.join(serve.AGENTS_DIR, 'nadia', 'reports', 'report-1-1234-nadia.md')
        self.assertTrue(os.path.isfile(target_md), 'report must land in the SUBJECT\'s reports dir')
        with open(target_md) as f:
            self.assertIn('filed by: dev', f.read().lower())


class PassportDecisionChain(unittest.TestCase):
    # Hash-chain covers CHAINED KEY DECISIONS: consequential
    # actions (hire, fire, promote, grant, report_filed) append decision blocks
    # off the SAME head as promoted-file blocks, so the whole ledger is one
    # tamper-evident chain.
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-passport-test-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()
        os.makedirs(os.path.join(self.tmp, 'library'), exist_ok=True)

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_decision_blocks_chain_off_the_same_head(self):
        # First block: from an empty chain, prev links to the empty head (None).
        b1 = serve._append_passport_decision('grant_access', 'nora', {'agent': 'nadia', 'capability': 'curl'})
        head_after_b1 = serve._load_passport()['head']
        self.assertIsNotNone(b1)
        self.assertIsNone(b1['prev'], 'first decision block links to the empty head')

        # Each subsequent block chains off the head left by the LAST append --
        # so altering or deleting any earlier block breaks every later link.
        b2 = serve._append_passport_decision('hire', 'faye', {'hired': 'zara'})
        self.assertEqual(b2['prev'], head_after_b1, 'block 2 must chain off block 1')
        head_after_b2 = serve._load_passport()['head']
        b3 = serve._append_passport_decision('report_filed', 'dev', {'about': 'nadia'})
        self.assertEqual(b3['prev'], head_after_b2, 'block 3 must chain off block 2')

        passport = serve._load_passport()
        self.assertEqual(passport['count'], 3)
        self.assertEqual(len(passport['blocks']), 3)
        # The head advances monotonically as decisions append, never stalling.
        self.assertNotEqual(head_after_b2, head_after_b1)
        self.assertIsNotNone(passport['head'])

    def test_no_decisions_reads_fresh_chain(self):
        p = serve._load_passport()
        self.assertEqual(p['count'], 0)
        self.assertEqual(p['head'], None)
        self.assertEqual(p['blocks'], [])

    def test_hashed_actions_subset_excludes_routine(self):
        self.assertIn('hire', serve._HASHED_ACTIONS)
        self.assertIn('firing_review', serve._HASHED_ACTIONS)
        self.assertIn('grant_access', serve._HASHED_ACTIONS)
        # File writes/modifies chain too (on hashing
        # every file write) -- both the write and its promotion.
        self.assertIn('library_write', serve._HASHED_ACTIONS)
        self.assertIn('library_promote', serve._HASHED_ACTIONS)
        self.assertIn('report_filed', serve._HASHED_ACTIONS)
        # Routine heartbeat actions must NOT be chain-worthy.
        for a in ('chat', 'browse', 'execute', 'task_completed', 'mail_sent'):
            self.assertNotIn(a, serve._HASHED_ACTIONS)

    def test_library_write_chains_when_an_agent_writes_a_file(self):
        # Regression for the added rule: an agent writing/modifying a library
        # file lands a `library_write` block on the passport chain.
        import asyncio
        from unittest.mock import MagicMock, AsyncMock
        # Give dev a real key in this isolated DB.
        dev_key = serve.get_or_create_agent_key('dev')
        os.makedirs(os.path.join(serve.LIBRARY_DIR, 'shared'), exist_ok=True)
        body = {
            'agentId': 'dev', 'path': 'shared/field-notes.md',
            'content': 'inspected the bank vault door handle', 'source': 'firsthand',
        }
        req = MagicMock(headers={'X-Agent-Key': dev_key})
        req.json = AsyncMock(return_value=body)
        resp = asyncio.run(serve.write_library_file(req))
        self.assertEqual(resp.body.decode(), 'saved')
        blocks = [b for b in serve._load_passport()['blocks'] if b.get('kind') == 'library_write']
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]['actor'], 'dev')
        self.assertEqual(blocks[0]['payload']['path'], 'shared/field-notes.md')
        # The written file exists on disk under the shared commons.
        self.assertTrue(os.path.isfile(os.path.join(serve.LIBRARY_DIR, 'shared', 'field-notes.md')))

    def test_library_write_chains_on_modify(self):
        # Modifying an existing file (second write to the same path) chains
        # another block -- each write is a distinct, hashed event.
        import asyncio
        from unittest.mock import MagicMock, AsyncMock
        dev_key = serve.get_or_create_agent_key('dev')
        os.makedirs(os.path.join(serve.LIBRARY_DIR, 'shared'), exist_ok=True)
        for content in ('draft v1', 'draft v2 -- revised'):
            req = MagicMock(headers={'X-Agent-Key': dev_key})
            req.json = AsyncMock(return_value={
                'agentId': 'dev', 'path': 'shared/field-notes.md', 'content': content, 'source': 'firsthand',
            })
            asyncio.run(serve.write_library_file(req))
        writes = [b for b in serve._load_passport()['blocks'] if b.get('kind') == 'library_write']
        self.assertEqual(len(writes), 2, 'each write/modify must be its own chained block')


class WikiDeleteGuard(unittest.TestCase):
    """The live wiki tree (library/wiki/) is the curated, director-gated
    knowledge layer. The generic library write/promote endpoints must not become
    a back door for an agent to overwrite or blank a live wiki page -- those only
    change through write_wiki_page (version-bumped + passport-chained)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='wiki-delete-guard-test-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()
        os.makedirs(os.path.join(self.tmp, 'library'), exist_ok=True)

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_generic_library_write_cannot_touch_live_wiki(self):
        import asyncio
        from unittest.mock import MagicMock, AsyncMock
        dev_key = serve.get_or_create_agent_key('dev')
        req = MagicMock(headers={'X-Agent-Key': dev_key})
        req.json = AsyncMock(return_value={
            'agentId': 'dev', 'path': 'wiki/observations/foo.md',
            'content': 'sneaky', 'source': 'firsthand',
        })
        resp = asyncio.run(serve.write_library_file(req))
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(os.path.exists(os.path.join(serve.LIBRARY_DIR, 'wiki', 'observations', 'foo.md')))

    def test_generic_library_write_cannot_drop_files_into_propose_quarantine(self):
        # pending_review/wiki/ is the propose lane's own namespace; the generic
        # endpoint must not bypass propose's id/path validation.
        import asyncio
        from unittest.mock import MagicMock, AsyncMock
        dev_key = serve.get_or_create_agent_key('dev')
        req = MagicMock(headers={'X-Agent-Key': dev_key})
        req.json = AsyncMock(return_value={
            'agentId': 'dev', 'path': 'pending_review/wiki/general/evil.md',
            'content': 'sneaky', 'source': 'external',
        })
        resp = asyncio.run(serve.write_library_file(req))
        self.assertEqual(resp.status_code, 403)

    def test_promote_cannot_clobber_a_live_wiki_page(self):
        import asyncio
        from unittest.mock import MagicMock, AsyncMock
        dev_key = serve.get_or_create_agent_key('dev')
        wiki_page = os.path.join(serve.LIBRARY_DIR, 'wiki', 'observations', 'foo.md')
        os.makedirs(os.path.dirname(wiki_page), exist_ok=True)
        with open(wiki_page, 'w') as f:
            f.write('ORIGINAL TRUSTED BODY')
        pending = os.path.join(serve.LIBRARY_DIR, 'pending_review', 'wiki', 'observations', 'foo.md')
        os.makedirs(os.path.dirname(pending), exist_ok=True)
        with open(pending, 'w') as f:
            f.write('untrusted attacker content')
        req = MagicMock(headers={'X-Agent-Key': dev_key})
        req.json = AsyncMock(return_value={
            'agentId': 'dev', 'path': 'pending_review/wiki/observations/foo.md',
        })
        resp = asyncio.run(serve.library_promote(req))
        self.assertEqual(resp.status_code, 403)
        with open(wiki_page, 'r') as f:
            self.assertEqual(f.read(), 'ORIGINAL TRUSTED BODY',
                             'the live wiki page must survive the promote attempt')


class JevChoiceExtraction(unittest.TestCase):
    # Jev returns typed decisions with calibrated confidence + cost (see
    # DESIGN.md and the Jev research). Formerly every call site read only
    # `choice`, throwing away the confidence that is Jev's whole reason to
    # exist. _jev_choice extracts all three with defaults that preserve old
    # behavior when a field is absent.
    def test_extracts_choice_confidence_and_cost(self):
        data = {
            'answers': {'choice': {'choice': 'allow', 'confidence': 0.92, 'probabilities': {'allow': 0.92, 'block': 0.08}}},
            'usage': {'cost': 0.000014},
        }
        self.assertEqual(serve._jev_choice(data), ('allow', 0.92, 0.000014))

    def test_missing_confidence_fails_closed_to_zero(self):
        # No/absent confidence -> zero trust, so a malformed response can't
        # bypass the low-confidence human floors by looking fully confident.
        data = {'answers': {'choice': {'choice': 'allow'}}, 'usage': {}}
        self.assertEqual(serve._jev_choice(data), ('allow', 0.0, 0.0))

    def test_bad_confidence_fails_closed_to_zero(self):
        # Out-of-range or non-numeric confidence is treated as absent -> zero.
        data = {'answers': {'choice': {'choice': 'deny', 'confidence': 'high'}}}
        self.assertEqual(serve._jev_choice(data), ('deny', 0.0, 0.0))

    def test_empty_response_is_safe(self):
        self.assertEqual(serve._jev_choice(None), (None, 0.0, 0.0))
        self.assertEqual(serve._jev_choice({}), (None, 0.0, 0.0))

    def test_answer_uses_first_question_key(self):
        data = {'answers': {'pick': {'choice': 'worker_3', 'confidence': 0.7}}}
        self.assertEqual(serve._jev_choice(data), ('worker_3', 0.7, 0.0))


class JevSafetyGate(unittest.TestCase):
    # The "act when confident, escalate when unsure" contract Jev exists to
    # provide. A confident allow passes; a LOW-confidence allow is escalated
    # to a human instead of acted on (never flat-allowed); any block denies.
    @classmethod
    def setUpClass(cls):
        serve.init_db()

    def _call(self, decision, confidence):
        # Patch the escalation side effects (email + file I/O) so a test
        # never blasts mail or mutates the real escalations.json.
        with unittest.mock.patch.object(serve, '_send_escalation_email_sync'), \
             unittest.mock.patch.object(serve, '_load_escalations', return_value={}) as mock_load, \
             unittest.mock.patch.object(serve, '_save_escalations') as mock_save:
            allowed = serve._jev_safety_gate(
                'test-agent', 'browse', 'This page', 'http://example.com/page',
                'need to check weather', decision, confidence, 0.0, None,
            )
        return allowed, mock_save

    def test_confident_allow_passes(self):
        allowed, _ = self._call('allow', 0.95)
        self.assertTrue(allowed)

    def test_confident_approve_passes(self):
        allowed, _ = self._call('approve', 0.9)
        self.assertTrue(allowed)

    def test_low_confidence_allow_is_blocked_without_escalation(self):
        # Low-confidence allow is "unsure": block + log blocked_unsure. The
        # agent adapts to the block; no player escalation is filed.
        with unittest.mock.patch.object(serve, 'log_action') as mock_log:
            allowed, save_called = self._call('allow', 0.4)
        self.assertFalse(allowed, 'a low-confidence allow must NOT be flat-allowed')
        save_called.assert_not_called()
        self.assertEqual(mock_log.call_args[0][2]['decision'], 'blocked_unsure')

    def test_low_confidence_approve_is_blocked_without_escalation(self):
        with unittest.mock.patch.object(serve, 'log_action') as mock_log:
            allowed, save_called = self._call('approve', 0.3)
        self.assertFalse(allowed)
        save_called.assert_not_called()
        self.assertEqual(mock_log.call_args[0][2]['decision'], 'blocked_unsure')

    def test_block_denies_without_escalation(self):
        # A firm block is just a block -- no need to escalate an already-clear no.
        allowed, save_called = self._call('block', 0.99)
        self.assertFalse(allowed)
        save_called.assert_not_called()

    def test_no_answer_fails_closed(self):
        allowed, save_called = self._call(None, 1.0)
        self.assertFalse(allowed)
        save_called.assert_not_called()

    def test_confident_allow_records_cost_and_confidence_in_log(self):
        with unittest.mock.patch.object(serve, 'log_action') as mock_log, \
             unittest.mock.patch.object(serve, '_send_escalation_email_sync'):
            self.assertTrue(serve._jev_safety_gate(
                'test-agent', 'download', 'This file', 'http://example.com/f', 'purpose', 'allow', 0.88, 0.00001, 'auth-ok',
            ))
        details = mock_log.call_args[0][2]
        self.assertEqual(details['confidence'], 0.88)
        self.assertEqual(details['cost'], 0.00001)


class DecideRateLimiter(unittest.TestCase):
    # Sliding-window gate on /api/decide -- the defense against the ~48k
    # unattributed Jev-call leak. Exercises _decide_allowed directly with an
    # injectable clock so bursts/throttling are deterministic, no real
    # monotonic time involved.
    def setUp(self):
        serve._decide_stamps.clear()

    # Simulate a "real time" timeline in seconds; each _t is when the next
    # call arrives. Bucket key for an agent: calls attributed to that agent
    # throttle independently.
    def test_burst_allows_up_to_cap_then_blocks(self):
        # Space calls so (cap + 1) land inside one burst window -- the last
        # one MUST be throttled. Spacing is derived from the constants so the
        # invariant holds regardless of their exact values, and any faster
        # (>= min-interval) spacing only makes the case stricter.
        spacing = serve.DECIDE_BURST_WINDOW_S / (serve.DECIDE_BURST_ALLOW + 1)
        for i in range(serve.DECIDE_BURST_ALLOW):
            allowed, reason = serve._decide_allowed('ada', now=100.0 + i * spacing)
            self.assertTrue(allowed, f'call {i} within burst should be allowed: {reason}')
        allowed, reason = serve._decide_allowed('ada', now=100.0 + serve.DECIDE_BURST_ALLOW * spacing)
        self.assertFalse(allowed, 'burst-exhausted call must be throttled')

    def test_sustained_spam_is_stopped(self):
        # Several bursts worth of calls all arriving faster than the window
        # drains: exactly one burst's worth may get through, the rest refused.
        spacing = serve.DECIDE_BURST_WINDOW_S / (serve.DECIDE_BURST_ALLOW + 1)
        hits = refuses = 0
        for i in range(3 * serve.DECIDE_BURST_ALLOW):
            ok, _ = serve._decide_allowed('ada', now=200.0 + i * spacing)
            if ok:
                hits += 1
            else:
                refuses += 1
        self.assertEqual(hits, serve.DECIDE_BURST_ALLOW, 'at most one burst of allowed calls')
        self.assertGreater(refuses, 0, 'sustained spam must get throttled')

    def test_agents_throttle_independently(self):
        # One agent exhausting its burst must not starve another.
        spacing = serve.DECIDE_BURST_WINDOW_S / (serve.DECIDE_BURST_ALLOW + 1)
        for i in range(serve.DECIDE_BURST_ALLOW):
            serve._decide_allowed('ada', now=300.0 + i * spacing)
        allowed, _ = serve._decide_allowed('nora', now=300.0)
        self.assertTrue(allowed, "a different agent's first call must not be throttled")

    def test_anonymous_calls_share_a_system_bucket(self):
        # Unknown/missing actors collapse into one "__system__" bucket so an
        # unbounded set of anonymous identities can't dodge the throttle.
        spacing = serve.DECIDE_BURST_WINDOW_S / (serve.DECIDE_BURST_ALLOW + 1)
        for i in range(serve.DECIDE_BURST_ALLOW):
            self.assertTrue(serve._decide_allowed(None, now=400.0 + i * spacing)[0])
        self.assertTrue(serve._decide_allowed('player', now=400.0 + serve.DECIDE_BURST_ALLOW * spacing)[1].startswith('decide rate limited'))
        # same system bucket is exhausted for 'unknown' too
        allowed, _ = serve._decide_allowed('unknown', now=400.0 + serve.DECIDE_BURST_ALLOW * spacing)
        self.assertFalse(allowed)

    def test_window_drains_and_allows_again(self):
        # After the burst window passes with no calls, a fresh burst is
        # allowed -- the throttle is a sliding window, not a hard lifetime cap.
        spacing = serve.DECIDE_BURST_WINDOW_S / (serve.DECIDE_BURST_ALLOW + 1)
        for i in range(serve.DECIDE_BURST_ALLOW):
            serve._decide_allowed('ada', now=500.0 + i * spacing)
        allowed, reason = serve._decide_allowed('ada', now=500.0 + serve.DECIDE_BURST_WINDOW_S + 1.0)
        self.assertTrue(allowed, f'window should drain and re-allow, got: {reason}')
