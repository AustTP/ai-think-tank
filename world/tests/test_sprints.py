"""Tests for Phase C: sprints + task feed, and the scrum-master condition.

Covers the pure sim sprint helpers (queue_sprint / sprint_progress /
close_sprint / next_sprint_id), the team scrum-master helper, and the endpoint
layer (sprint create gate refuses any team lacking a scrum master; setting one
unblocks it). Runs against a hermetic temp DB + no network.
"""
import os
import shutil
import tempfile
import unittest
import unittest.mock

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim
import serve


def _make_team_state():
    """A minimal roster + teams map with two teams (faye's, dev's)."""
    return {
        'sim': {'owner': 'server'},
        'agentRoster': [
            {'id': 'faye', 'name': 'Faye', 'role': 'admin', 'isAdmin': True},
            {'id': 'dev', 'name': 'Dev', 'role': 'Control Room', 'isDirector': True},
            {'id': 'ben', 'name': 'Ben', 'role': 'engineer', 'director': 'dev'},
            {'id': 'ada', 'name': 'Ada', 'role': 'engineer', 'director': 'dev'},
            {'id': 'sam', 'name': 'Sam', 'role': 'researcher', 'director': 'faye'},
        ],
        'teams': [
            {'id': 'faye', 'name': "Faye's Crew", 'directorId': 'faye'},
            {'id': 'dev', 'name': "Dev's Crew", 'directorId': 'dev'},
        ],
        'agents': {
            'faye': {'id': 'faye', 'name': 'Faye', 'busy': False, 'offDuty': False},
            'dev': {'id': 'dev', 'name': 'Dev', 'busy': True, 'offDuty': False},
            'ben': {'id': 'ben', 'name': 'Ben', 'busy': False, 'offDuty': False, 'role': 'engineer'},
            'ada': {'id': 'ada', 'name': 'Ada', 'busy': False, 'offDuty': False},
            'sam': {'id': 'sam', 'name': 'Sam', 'busy': False, 'offDuty': False},
        },
        'reports': [],
        'workQueue': [],
        'tasks': {},
        'sprints': {},
    }


class SprintHelpers(unittest.TestCase):
    def test_queue_sprint_seeds_tagged_items(self):
        state = _make_team_state()
        record = sim.queue_sprint(
            state, sim.next_sprint_id(state), 'Launch', 'Ship April', 'faye',
            [{'title': 'Build a thing', 'room': 'pressoffice', 'priority': 'high'},
             {'title': 'Research X', 'room': 'observatory'}],
            ['pressoffice', 'observatory'], team_ids=['dev'])
        self.assertEqual(record['id'], 'spr-1')
        rec = state['sprints']['spr-1']
        self.assertEqual(rec['status'], 'active')
        self.assertEqual(rec['teamIds'], ['dev'])
        queued = state['workQueue']
        self.assertEqual(len(queued), 2)
        self.assertTrue(all(i.get('sprintId') == 'spr-1' for i in queued))
        # Highest priority surfaced as a real int via normalize_priority.
        self.assertEqual(queued[0]['priority'], 2)  # high

    def test_queue_sprint_drops_invalid_rooms_and_empty_goal_sprints(self):
        state = _make_team_state()
        # Only valid-room items kept.
        record = sim.queue_sprint(
            state, sim.next_sprint_id(state), 'S', 'g', 'faye',
            [{'title': 'ok', 'room': 'pressoffice'},
             {'title': 'bad', 'room': 'not-a-room'},
             {'title': '', 'room': 'pressoffice'}],
            ['pressoffice'])
        self.assertEqual(record['id'], 'spr-1')
        queued_titles = [i['title'] for i in state['workQueue']]
        self.assertEqual(queued_titles, ['ok'])
        # No valid items == no sprint created.
        none = sim.queue_sprint(state, sim.next_sprint_id(state), 'S', 'g',
                                'faye', [], ['pressoffice'])
        self.assertIsNone(none)

    def test_sprint_progress_derives_counts(self):
        state = _make_team_state()
        sim.queue_sprint(
            state, sim.next_sprint_id(state), 'S', 'g', 'faye',
            [{'title': 'fin', 'room': 'pressoffice'},
             {'title': 'active', 'room': 'observatory'},
             {'title': 'queued', 'room': 'pressoffice'}],
            ['pressoffice', 'observatory'])
        # fin: assigned + done; active: assigned + working (dequeued);
        # queued: still in the queue.
        state['workQueue'] = [{'title': 'queued', 'room': 'pressoffice', 'sprintId': 'spr-1'}]
        state['tasks'] = {
            't1': {'title': 'fin', 'room': 'pressoffice', 'status': 'done'},
            't2': {'title': 'active', 'room': 'observatory', 'status': 'working'},
        }
        p = sim.sprint_progress(state, 'spr-1')
        self.assertEqual(p, {'total': 3, 'queued': 1, 'inProgress': 1,
                             'done': 1, 'pct': 33, 'landed': ['fin']})

    def test_sprint_progress_landed_in_item_order(self):
        # The "what landed" digest: done titles, in sprint-item order (not task
        # creation order), for both an active and a fully-landed sprint.
        state = _make_team_state()
        sim.queue_sprint(
            state, sim.next_sprint_id(state), 'S', 'g', 'faye',
            [{'title': 'c-done', 'room': 'pressoffice'},
             {'title': 'a-working', 'room': 'observatory'},
             {'title': 'b-done', 'room': 'pressoffice'}],
            ['pressoffice', 'observatory'])
        state['workQueue'] = []  # both done items assigned+done; working one active
        state['tasks'] = {
            't3': {'title': 'b-done', 'room': 'pressoffice', 'status': 'done'},
            't1': {'title': 'c-done', 'room': 'pressoffice', 'status': 'done'},
            't2': {'title': 'a-working', 'room': 'observatory', 'status': 'working'},
        }
        p = sim.sprint_progress(state, 'spr-1')
        # Item order preserved: c-done then b-done, even though b-done's task
        # was created first.
        self.assertEqual(p['landed'], ['c-done', 'b-done'])

    def test_sprint_progress_landed_empty_when_nothing_done(self):
        state = _make_team_state()
        sim.queue_sprint(
            state, sim.next_sprint_id(state), 'S', 'g', 'faye',
            [{'title': 'a', 'room': 'pressoffice'}], ['pressoffice'])
        p = sim.sprint_progress(state, 'spr-1')
        self.assertEqual(p['landed'], [])
        self.assertEqual(p['done'], 0)

    def test_close_sprint_is_container_only(self):
        state = _make_team_state()
        sim.queue_sprint(
            state, sim.next_sprint_id(state), 'S', 'g', 'faye',
            [{'title': 'a', 'room': 'pressoffice'}], ['pressoffice'])
        self.assertEqual(state['sprints']['spr-1']['status'], 'active')
        sim.close_sprint(state, 'spr-1')
        self.assertEqual(state['sprints']['spr-1']['status'], 'closed')
        # Queued item is untouched -- close is a container action, not a cancel.
        self.assertEqual(len(state['workQueue']), 1)
        self.assertIsNone(sim.close_sprint(state, 'nope'))

    def test_next_sprint_id_is_monotonic(self):
        state = _make_team_state()
        self.assertEqual(sim.next_sprint_id(state), 'spr-1')
        sim.queue_sprint(state, 'spr-1', 'S', 'g', 'faye',
                         [{'title': 'a', 'room': 'pressoffice'}], ['pressoffice'])
        self.assertEqual(sim.next_sprint_id(state), 'spr-2')


class ScrumMasterCondition(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-sprints-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
            _FERNET_EDEK_DIR=os.path.join(self.tmp, '.secret_keys'),
            _FERNET_EDEK_PATH=os.path.join(self.tmp, '.secret_keys', 'edek.key'),
        )
        self._cm.start()
        serve.init_db()
        serve.save_state_to_db(_make_team_state())

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _client(self):
        """TestClient with a real logged-in player session cookie, so the
        /api/intent prefix passes its session-auth middleware."""
        from fastapi.testclient import TestClient
        c = TestClient(serve.app)
        sid = serve.create_session()
        c.cookies.set(serve.SESSION_COOKIE_NAME, sid)
        return c

    def test_teams_missing_scrum_master(self):
        state = serve.get_state_from_db()
        # Scrum masters scale with team size: both teams here are
        # below SCRUM_MASTER_MIN_TEAM_SIZE (faye has 1 worker, dev has 2), so
        # neither REQUIRES a designated scrum master -- the director stands in.
        missing = serve._teams_missing_scrum_master(state, ['faye', 'dev'])
        ids = {m['id'] for m in missing}
        self.assertEqual(ids, set(), 'small teams below the threshold need no scrum master')
        # Grow dev's team to the threshold and confirm IT is now flagged.
        import sim as _sim
        roster = state['agentRoster']
        for i, nme in enumerate(['w1', 'w2']):
            roster.append({'id': nme, 'name': nme, 'role': 'engineer', 'director': 'dev'})
            state['agents'][nme] = {'id': nme, 'name': nme, 'busy': False, 'offDuty': False}
        self.assertGreaterEqual(_sim._team_member_count(state, 'dev'),
                                _sim.SCRUM_MASTER_MIN_TEAM_SIZE)
        missing2 = serve._teams_missing_scrum_master(state, ['faye', 'dev'])
        self.assertEqual({m['id'] for m in missing2}, {'dev'})
        # Designate dev's, then nothing is missing.
        t = next(x for x in state['teams'] if x['id'] == 'dev')
        t['scrumMasterId'] = 'ben'
        missing3 = serve._teams_missing_scrum_master(state, ['faye', 'dev'])
        self.assertEqual([m['id'] for m in missing3], [])

    def test_sprint_create_allows_small_team_without_scrum_master(self):
        c = self._client()
        # Both teams are below SCRUM_MASTER_MIN_TEAM_SIZE, so the director
        # stands in as facilitator -- a sprint on them is NOT blocked.
        resp = c.post('/api/intent/sprint', json={
            'name': 'April', 'goal': 'Ship it', 'teamIds': ['faye', 'dev'],
            'items': [{'title': 'build', 'room': 'pressoffice',
                       'instructions': 'build it'}],
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'])

    def test_sprint_create_gate_blocks_big_team_without_scrum_master(self):
        c = self._client()
        # Grow dev's team to the threshold, clear its scrum master, then the
        # gate blocks with 409 + the culprit.
        state = serve.get_state_from_db()
        import sim as _sim
        for i, nme in enumerate(['w1', 'w2']):
            state['agentRoster'].append({'id': nme, 'name': nme, 'role': 'engineer', 'director': 'dev'})
            state['agents'][nme] = {'id': nme, 'name': nme, 'busy': False, 'offDuty': False}
        serve.save_state_to_db(state)
        resp = c.post('/api/intent/sprint', json={
            'name': 'April', 'goal': 'Ship it', 'teamIds': ['dev'],
            'items': [{'title': 'build', 'room': 'pressoffice',
                       'instructions': 'build it'}],
        })
        self.assertEqual(resp.status_code, 409)
        self.assertIn('scrum master', resp.json()['error'].lower())
        self.assertEqual(len(resp.json()['missingTeams']), 1)

    def test_sprint_create_succeeds_when_teams_have_scrum_masters(self):
        # Designate scrum masters for both teams, then sprint creation passes.
        state = serve.get_state_from_db()
        for t in state['teams']:
            t['scrumMasterId'] = 'ben' if t['id'] != 'faye' else 'sam'
        serve.save_state_to_db(state)
        c = self._client()
        resp = c.post('/api/intent/sprint', json={
            'name': 'April', 'goal': 'Ship it', 'teamIds': ['dev'],
            'items': [{'title': 'build', 'room': 'pressoffice',
                       'instructions': 'build it'}],
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['sprint']['teamIds'], ['dev'])
        # The sprint was persisted and its item is in the live queue.
        live = serve.get_state_from_db()
        self.assertIn('spr-1', live['sprints'])
        self.assertEqual(live['workQueue'][0]['sprintId'], 'spr-1')

    def test_sprint_close_and_read_endpoints(self):
        state = serve.get_state_from_db()
        for t in state['teams']:
            t['scrumMasterId'] = 'ben'
        serve.save_state_to_db(state)
        c = self._client()
        created = c.post('/api/intent/sprint', json={
            'name': 'April', 'goal': 'Ship it', 'teamIds': ['dev'],
            'items': [{'title': 'build', 'room': 'pressoffice'},
                      {'title': 'research', 'room': 'observatory'}],
        })
        self.assertEqual(created.status_code, 200)
        sid = created.json()['sprint']['id']
        # Read lists it with derived progress (both queued).
        lst = c.get('/api/intent/sprints')
        self.assertEqual(lst.status_code, 200)
        spr = lst.json()['sprints'][sid]
        self.assertEqual(spr['progress']['queued'], 2)
        # Close flips status.
        closed = c.post(f'/api/intent/sprint/{sid}/close')
        self.assertEqual(closed.status_code, 200)
        live = serve.get_state_from_db()
        self.assertEqual(live['sprints'][sid]['status'], 'closed')


@unittest.mock.patch('sim._free_outdoor_spot', return_value={'x': 5, 'y': 5})
class TeamSizeCap(unittest.TestCase):
    """Team may not exceed MAX_TEAM_MEMBERS (6) members, excluding the scrum
    master -- the cap is enforced on the DERIVED team membership at hire time."""

    def _cap_state(self, report_count, scrum_id='scrum'):
        """A roster where faye directs `report_count` workers (one of them the
        scrum master) so the non-scrum team size is report_count - 1."""
        roster = [
            {'id': 'faye', 'name': 'Faye', 'role': 'admin', 'isAdmin': True, 'isDirector': True},
            {'id': 'dev', 'name': 'Dev', 'role': 'Control Room', 'isDirector': True},
        ]
        agents = {
            'faye': {'id': 'faye', 'name': 'Faye', 'busy': False, 'offDuty': False},
            'dev': {'id': 'dev', 'name': 'Dev', 'busy': False, 'offDuty': False},
        }
        for i in range(report_count):
            aid = f'w{i}'
            roster.append({'id': aid, 'name': aid, 'role': 'engineer', 'director': 'faye'})
            agents[aid] = {'id': aid, 'name': aid, 'busy': False, 'offDuty': False, 'role': 'engineer'}
        teams = [{'id': 'faye', 'name': 'Faye Team', 'directorId': 'faye',
                  'scrumMasterId': scrum_id if scrum_id in {r['id'] for r in roster} else None}]
        return {
            'sim': {'owner': 'server'},
            'agentRoster': roster, 'agents': agents, 'teams': teams,
            'reports': [], 'workQueue': [], 'tasks': {}, 'sprints': {},
        }

    def test_count_excludes_scrum_master(self, _spot):
        state = self._cap_state(7, scrum_id='w0')  # 7 reports, 1 is scrum -> 6 non-scrum
        self.assertEqual(sim._team_member_count(state, 'faye'), 6)
        self.assertTrue(sim._team_under_cap(state, 'faye'))  # at 6 = OK
        self.assertFalse(sim._team_under_cap(state, 'faye', extra=1))  # 7th blocked

    def test_start_auto_hire_refuses_at_cap(self, _spot):
        state = self._cap_state(7, scrum_id='w0')  # exactly at the 6 non-scrum cap
        now_ms = 1_000_000
        # A free director with members + workload normally triggers a hire; the
        # cap must prevent it before a _pendingHire is even set.
        started = sim._start_auto_hire(state, now_ms, None, None)
        self.assertFalse(started)
        self.assertNotIn('_pendingHire', state)

    def test_start_hire_works_under_cap(self, _spot):
        state = self._cap_state(6, scrum_id='w0')  # 5 non-scrum -> one slot free
        now_ms = 1_000_000
        # A decider that just picks the first offered candidate.
        def decider(_s, _prompt, candidates):
            return candidates[0]['id']
        started = sim._start_auto_hire(state, now_ms, None, decider)
        # Under the cap a hire proceeds and sets the pending record.
        self.assertTrue(started)
        self.assertIn('_pendingHire', state)


@unittest.mock.patch('sim._free_outdoor_spot', return_value={'x': 5, 'y': 5})
class DirectorNameChooser(unittest.TestCase):
    """Hiring DIRECTORS choose each new employee's name (no predetermined names),
    and a name can never be re-used across the think tank's whole history --
    not just while its owner is employed."""

    def _hire_state(self):
        """A minimal server-owned state with faye as an admin/director and one
        worker, plus a pending hire set to complete."""
        roster = [
            {'id': 'faye', 'name': 'Faye', 'role': 'admin', 'isAdmin': True, 'isDirector': True},
            {'id': 'w0', 'name': 'W0', 'role': 'engineer', 'director': 'faye'},
        ]
        agents = {
            'faye': {'id': 'faye', 'name': 'Faye', 'busy': True, 'offDuty': False},
            'w0': {'id': 'w0', 'name': 'W0', 'busy': True, 'offDuty': False, 'role': 'engineer'},
        }
        state = {
            'sim': {'owner': 'server'},
            'agentRoster': roster, 'agents': agents,
            'teams': [{'id': 'faye', 'name': 'Faye Team', 'directorId': 'faye',
                       'scrumMasterId': 'faye'}],
            'reports': [], 'workQueue': [], 'tasks': {}, 'sprints': {},
            '_pendingHire': {
                'adminId': 'faye', 'adminName': 'Faye', 'directorId': 'faye',
                'helpForId': 'w0', 'helpForName': 'W0', 'at': 0,
            },
        }
        return state

    def test_hire_uses_director_chosen_name(self, _spot):
        state = self._hire_state()
        with unittest.mock.patch('sim._hire_name_chooser', return_value='Nadia'):
            new_id = sim._complete_auto_hire(state, state['_pendingHire'], {}, 1000)
        self.assertEqual(new_id, 'nadia')
        self.assertEqual(state['agents']['nadia']['name'], 'Nadia')
        self.assertIn('nadia', [d['id'] for d in state['agentRoster']])

    def test_hire_chooser_name_reserved_all_time(self, _spot):
        state = self._hire_state()
        with unittest.mock.patch('sim._hire_name_chooser', return_value='Nadia'):
            sim._complete_auto_hire(state, state['_pendingHire'], {}, 1000)
        self.assertIn('nadia', state['_usedNames'])

    def test_hire_rejects_chooser_used_name(self, _spot):
        # Defense-in-depth: a chooser returning an all-time-used name is not
        # trusted -- the hire falls back to the pool instead of colliding.
        state = self._hire_state()
        state['_usedNames'] = ['nadia']
        with unittest.mock.patch('sim._hire_name_chooser', return_value='Nadia'):
            new_id = sim._complete_auto_hire(state, state['_pendingHire'], {}, 1000)
        self.assertEqual(new_id, 'maya')
        self.assertIn('maya', state['_usedNames'])

    def test_hire_falls_back_to_pool_on_chooser_outage(self, _spot):
        state = self._hire_state()
        with unittest.mock.patch('sim._hire_name_chooser', return_value=None):
            new_id = sim._complete_auto_hire(state, state['_pendingHire'], {}, 1000)
        self.assertEqual(new_id, 'maya')

    def test_hire_never_reuses_retired_name(self, _spot):
        # maya was fired long ago and reserved forever -- the pool skips her.
        state = self._hire_state()
        state['_usedNames'] = ['maya']
        with unittest.mock.patch('sim._hire_name_chooser', return_value=None):
            new_id = sim._complete_auto_hire(state, state['_pendingHire'], {}, 1000)
        self.assertEqual(new_id, 'leo')

    def test_new_team_admin_names_director_director_names_employees(self, _spot):
        state = self._hire_state()

        def chooser(_s, who, used, role):
            return {'Director': 'Elio', 'Engineer': 'Nadia'}[role]

        team = sim.spawn_new_team_for_request(
            state, 'Build the full platform end-to-end now', now_ms=1000,
            admin_id='faye', employees=1, chooser=chooser)
        self.assertEqual(team['id'], 'elio')
        self.assertEqual(state['agents']['nadia']['name'], 'Nadia')
        self.assertIn('elio', state['_usedNames'])
        self.assertIn('nadia', state['_usedNames'])
        # Team ownership still resolves upward to the admin.
        dir_roster = next(d for d in state['agentRoster'] if d['id'] == 'elio')
        self.assertEqual(dir_roster['director'], 'faye')

    def test_governance_pass_backfills_roster_names(self, _spot):
        # A pre-existing roster (hired before all-time tracking shipped) is
        # seeded into _usedNames on the first pass so those names stay reserved.
        state = self._hire_state()
        state['lastHireAt'] = 1000  # inside cooldown: no new hire starts
        sim._governance_pass(state, now=1.0, now_ms=1000, grid={})
        self.assertIn('faye', state['_usedNames'])
        self.assertIn('w0', state['_usedNames'])


if __name__ == '__main__':
    unittest.main()