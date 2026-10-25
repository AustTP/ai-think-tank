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


class PlayerIntentEndpoints(unittest.TestCase):
    """End-to-end tests of the HTTP routes added in Phase 4/5 -- GET /api/rooms,
    POST /api/intent/assign-big-task, and POST /api/rooms/{room}/purpose --
    driven through TestClient with the DB, model, and identity seams patched so
    nothing touches the real think_tank.db or makes a real model call. TestClient
    is used WITHOUT a context manager so the app's lifespan (which starts the
    real sim/health/mail loops) never runs."""

    def _state(self, room_purpose=None):
        roster = [
            {'id': 'faye', 'isAdmin': True},
            {'id': 'nora', 'isDirector': True},
            {'id': 'zara', 'director': 'nora'},
        ]
        room_defs = {}
        for r, d in serve._DEFAULT_ROOM_DEFINITIONS.items():
            room_defs[r] = {'label': d['label'], 'purpose': d['purpose']}
        if room_purpose:
            room_defs['pressoffice']['purpose'] = room_purpose
        return {
            'agentRoster': roster,
            'agents': {d['id']: {'name': d['id'], 'busy': False, 'offDuty': False, 'role': 'x'} for d in roster},
            'teams': [
                {'id': 'nora', 'name': "Nora's Crew", 'directorId': 'nora', 'scrumMasterId': 'zara'},
            ],
            'workQueue': [],
            'researchTopics': [],
            'roomDefinitions': room_defs,
        }

    def test_get_rooms_returns_backfilled_definitions(self):
        state = self._state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state):
            c = TestClient(serve.app)
            r = c.get('/api/rooms')
        self.assertEqual(r.status_code, 200)
        rooms = r.json()['rooms']
        # /api/rooms surfaces every DEFINED room (delegable work rooms + the
        # non-delegable hangout), not just the delegable subset.
        self.assertEqual(set(rooms), set(serve._DEFAULT_ROOM_DEFINITIONS))
        self.assertIn('hangout', rooms)
        self.assertIn('pressoffice', rooms)
        self.assertEqual(rooms['pressoffice']['label'], 'Work Room')
        self.assertIn('software development', rooms['pressoffice']['purpose'])
        # No DB write should occur on a plain read.
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = TestClient(serve.app)
            c.get('/api/rooms')
            save.assert_not_called()

    def test_assign_big_task_staffs_ask_to_free_team_and_files_breakdown(self):
        # The free admin (Faye) has no team of her own, so the ask is staffed
        # to the first free team (Nora's crew) and FILED as a pending large-
        # request breakdown. No subtasks are predicted or queued up front -- the
        # receiving team's breakdown ceremony (scrum master + workers) cards
        # them into stories/spikes on a later pass.
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self._state()), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, '_http_json') as http, \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/intent/assign-big-task', json={'goal': 'make the think tank better'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertTrue(body['staffed'])
        self.assertEqual(body['admin'], 'faye')
        self.assertEqual(body['team'], 'nora')
        self.assertEqual(body['request'], 'wrq-1')
        self.assertNotIn('subtasks', body)
        # The ask was filed as a pending breakdown request for Nora's team --
        # nothing queued, no model call (nothing is predicted up front).
        req = saved['backlogRequests'][0]
        self.assertEqual(req['id'], 'wrq-1')
        self.assertEqual(req['status'], 'pending')
        self.assertEqual(req['breakdown'], True)
        self.assertEqual(req['origin'], 'large_request')
        self.assertEqual(req['teamId'], 'nora')
        self.assertEqual(saved.get('workQueue', []), [])
        http.assert_not_called()
        log.assert_called_once()

    def test_assign_big_task_prefers_authority_own_free_team(self):
        # The free authority is director Nora (the admin is busy); her OWN team
        # is free, so the ask is staffed to HER team rather than bounced around.
        state = self._state()
        state['agents']['faye']['busy'] = True
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/intent/assign-big-task', json={'goal': 'task'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['admin'], 'nora')
        self.assertEqual(r.json()['team'], 'nora')
        self.assertEqual(r.json()['request'], 'wrq-1')

    def test_assign_big_task_no_free_authority_errors(self):
        state = self._state()
        # Everyone busy -> no authority -> error, no model call.
        for a in state['agents'].values():
            a['busy'] = True
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, '_http_json') as http, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/intent/assign-big-task', json={'goal': 'task'})
        self.assertIn('free right now', r.json()['error'])
        http.assert_not_called()

    def test_assign_big_task_wakes_resting_admin(self):
        # tasks.js: "a new request from you wakes the admin." With the admin
        # merely RESTING (offDuty, not busy) and no one else free, a delegation
        # request should wake her and proceed -- otherwise an idle think tank is
        # permanently un-delegable. Contrast the busy case above, which must
        # still error (you can't interrupt real work).
        state = self._state()
        for d in state['agentRoster']:
            state['agents'][d['id']]['offDuty'] = True
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, '_http_json') as http, \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/intent/assign-big-task', json={'goal': 'make the think tank better'})
        self.assertEqual(r.status_code, 200, r.text)
        # Faye (the admin) was woken on-duty and staffed the ask to Nora's team.
        self.assertEqual(r.json()['admin'], 'faye')
        self.assertEqual(r.json()['staffed'], True)
        self.assertEqual(r.json()['team'], 'nora')
        self.assertEqual(state['agents']['faye']['offDuty'], False)
        self.assertEqual(state['agents']['faye']['visible'], True)
        self.assertEqual(len(saved.get('workQueue', [])), 0)
        http.assert_not_called()

    def test_room_purpose_update_gated_to_director_and_persisted(self):
        state = self._state()
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='nora'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            c = TestClient(serve.app)
            r = c.post('/api/rooms/pressoffice/purpose', json={'purpose': 'nora rewrote this room'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['label'], 'Work Room', 'label is not editable')
        self.assertEqual(body['purpose'], 'nora rewrote this room')
        # Persisted to state.roomDefinitions through the merge-protected path.
        self.assertEqual(saved['roomDefinitions']['pressoffice']['purpose'], 'nora rewrote this room')
        log.assert_called_once()
        self.assertEqual(log.call_args[0][1], 'room_purpose_update')

    def test_room_purpose_update_blocks_rank_and_file(self):
        state = self._state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='zara'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = TestClient(serve.app)
            r = c.post('/api/rooms/pressoffice/purpose', json={'purpose': 'should fail'})
        self.assertEqual(r.status_code, 403)
        save.assert_not_called()

    def test_room_purpose_update_rejects_unknown_room(self):
        state = self._state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='nora'), \
             unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/rooms/townhall/purpose', json={'purpose': 'x'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('unknown room', r.json()['error'])


# --- Phase E3.4: player quality-veto --------------------------------

    def _veto_state(self):
        # Two non-admin engineers under one director so _pick_reviewer_ids can
        # return a real pair for author 'ben'. Our test roster admin must be
        # director=None (self-reference leaks into the reviewer pool).
        roster = [
            {'id': 'maya', 'isAdmin': True, 'director': None},
            {'id': 'ben', 'role': 'engineer', 'director': 'robin'},
            {'id': 'ada', 'role': 'engineer', 'director': 'robin'},
            {'id': 'cora', 'role': 'engineer', 'director': 'robin'},
            {'id': 'robin', 'isDirector': True, 'director': None},
        ]
        agents = {
            'maya': {'id': 'maya', 'name': 'Maya', 'busy': True, 'offDuty': False, 'role': 'x',
                     'mailbox': []},
            'ben': {'id': 'ben', 'name': 'Ben', 'busy': False, 'offDuty': False, 'role': 'engineer',
                    'task': None, 'mailbox': []},
            'ada': {'id': 'ada', 'name': 'Ada', 'busy': False, 'offDuty': False, 'role': 'engineer',
                    'task': None, 'mailbox': []},
            'cora': {'id': 'cora', 'name': 'Cora', 'busy': False, 'offDuty': False, 'role': 'engineer',
                     'task': None, 'mailbox': []},
            'robin': {'id': 'robin', 'name': 'Robin', 'busy': True, 'offDuty': False, 'role': 'director',
                      'mailbox': []},
        }
        return {
            'agentRoster': roster,
            'agents': agents,
            'workQueue': [],
            'tasks': {
                'story-1': {'id': 'story-1', 'title': 'Build the checkout flow',
                            'room': 'pressoffice', 'instructions': 'implement it',
                            'projectLabel': 'storefront', 'taskType': 'code',
                            'assignedTo': 'ben', 'status': 'done',
                            'createdAt': 1000, 'goal': 'storefront'},
                'story-live': {'id': 'story-live', 'title': 'In-flight story',
                               'room': 'pressoffice', 'instructions': 'x',
                               'projectLabel': 'storefront', 'taskType': 'code',
                               'assignedTo': 'ada', 'status': 'working',
                               'createdAt': 2000, 'goal': 'storefront'},
            },
            'reports': [],
            'researchTopics': [],
        }

    def test_reject_reopens_done_story_to_needs_review_for_author(self):
        state = self._veto_state()
        # Seed a prior reviewer pair on the done gate -- a player veto should
        # re-verify with the SAME pair (they have the context on this story),
        # not cold strangers.
        state['tasks']['story-1']['_peerGate'] = {
            'reviewerIds': ['ada', 'cora'], 'approvals': 2, 'approvers': ['ada', 'cora'],
            'closed': True, 'enteredMs': 1000,
        }
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action'):
            c = TestClient(serve.app)
            r = c.post('/api/intent/story/story-1/reject', json={'reason': 'wrong currency'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['author'], 'ben')
        self.assertEqual(len(body['reviewers']), 2)
        # A veto re-verifies with the reviewers who already know this story.
        self.assertEqual(sorted(body['reviewers']), ['ada', 'cora'])
        # The story is re-gated (re-enters peer review), author preserved.
        task = saved['tasks']['story-1']
        self.assertEqual(task['status'], 'needs_review')
        self.assertEqual(task['assignedTo'], 'ben')
        gate = task['_peerGate']
        self.assertEqual(gate['approvals'], 0)
        self.assertEqual(sorted(gate['reviewerIds']), ['ada', 'cora'])
        # Two review subtasks were queued, pinned back to the author.
        reviews = [t for t in saved['workQueue'] if t.get('reviewOf') == 'story-1']
        self.assertEqual(len(reviews), 2)
        self.assertTrue(all(rv.get('reviewAuthorId') == 'ben' for rv in reviews))
        # The author was told the PLAYER sent their work back.
        author_mail = saved['agents']['ben']['mailbox']
        self.assertTrue(any(m.get('kind') == 'player_veto' for m in author_mail))

    def test_reject_carries_reason_to_author_mailbox(self):
        # Todo-1 gap: a veto with no reason must still tell the author their
        # work was sent back (and not claim a reason exists), while a veto WITH
        # a reason must carry that reason VERBATIM to the author's mailbox so
        # they can actually rework toward it -- the whole point of the re-entry
        # loop is that the author learns what the player flagged.
        state = self._veto_state()
        state['tasks']['story-1']['_peerGate'] = {
            'reviewerIds': ['ada', 'cora'], 'approvals': 2, 'approvers': ['ada', 'cora'],
            'closed': True, 'enteredMs': 1000,
        }
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action'):
            c = TestClient(serve.app)
            r = c.post('/api/intent/story/story-1/reject',
                       json={'reason': 'needs an edge case for empty carts'})
        self.assertEqual(r.status_code, 200, r.text)
        note = saved['agents']['ben']['mailbox'][0]
        self.assertEqual(note['kind'], 'player_veto')
        self.assertIn('needs an edge case for empty carts', note['text'],
                      'the player\'s veto reason must reach the author verbatim')

    def test_reject_without_reason_still_notes_the_veto_not_a_reason(self):
        state = self._veto_state()
        state['tasks']['story-1']['_peerGate'] = {
            'reviewerIds': ['ada', 'cora'], 'approvals': 2, 'approvers': ['ada', 'cora'],
            'closed': True, 'enteredMs': 1000,
        }
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action'):
            c = TestClient(serve.app)
            r = c.post('/api/intent/story/story-1/reject', json={})
        self.assertEqual(r.status_code, 200, r.text)
        note = saved['agents']['ben']['mailbox'][0]
        self.assertEqual(note['kind'], 'player_veto')
        self.assertNotIn('They said:', note['text'],
                         'no reason given -> must not fabricate a quoted reason')

    def test_reject_cascades_rereview_to_dependent_story(self):
        # Ripple re-review: the dependent story was built on story-1's output
        # (task['dependsOn']). A player veto of story-1 must re-open the shipped
        # dependent through its OWN peer gate too -- same prior reviewers.
        state = self._veto_state()
        state['tasks']['story-1']['_peerGate'] = {
            'reviewerIds': ['ada', 'cora'], 'approvals': 2, 'approvers': ['ada', 'cora'],
            'closed': True, 'enteredMs': 1000,
        }
        state['tasks']['story-dep'] = {
            'id': 'story-dep', 'title': 'Build on the checkout flow',
            'room': 'pressoffice', 'instructions': 'extend it',
            'projectLabel': 'storefront', 'taskType': 'code',
            'assignedTo': 'ada', 'status': 'done', 'createdAt': 3000,
            'goal': 'storefront', 'dependsOn': 'story-1',
            '_peerGate': {'reviewerIds': ['ben', 'cora'], 'approvals': 2,
                          'approvers': ['ben', 'cora'], 'closed': True, 'enteredMs': 2000},
        }
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action'):
            c = TestClient(serve.app)
            r = c.post('/api/intent/story/story-1/reject', json={'reason': 'wrong currency'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['cascaded'], 1, 'the dependent must be re-opened')
        dep = saved['tasks']['story-dep']
        self.assertEqual(dep['status'], 'needs_review',
                         'a shipped dependent must re-enter review when its dependency is re-opened')
        self.assertEqual(sorted(dep['_peerGate']['reviewerIds']), ['ben', 'cora'],
                         'the dependent re-verifies with its own prior reviewers')
        self.assertEqual(dep['_peerGate']['approvals'], 0)

    def test_reject_cascade_skips_non_gated_and_undone_dependents(self):
        # Fail-closed ripple: an unshipped dependent stays put (its gate was
        # never passed -- nothing built on the questioned output yet), and a
        # non-gated lane (spike) is never forced into a gate.
        state = self._veto_state()
        state['tasks']['story-1']['_peerGate'] = {
            'reviewerIds': ['ada', 'cora'], 'approvals': 2, 'approvers': ['ada', 'cora'],
            'closed': True, 'enteredMs': 1000,
        }
        state['tasks']['story-wip'] = {
            'id': 'story-wip', 'title': 'WIP dependent', 'room': 'pressoffice',
            'instructions': 'x', 'projectLabel': 'storefront', 'taskType': 'code',
            'assignedTo': 'cora', 'status': 'working', 'dependsOn': 'story-1', 'createdAt': 3000,
        }
        state['tasks']['story-spike'] = {
            'id': 'story-spike', 'title': 'Spike dependent', 'room': 'pressoffice',
            'instructions': 'x', 'projectLabel': 'storefront', 'taskType': 'spike',
            'assignedTo': 'ben', 'status': 'done', 'dependsOn': 'story-1', 'createdAt': 3000,
        }
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action'):
            c = TestClient(serve.app)
            r = c.post('/api/intent/story/story-1/reject', json={'reason': 'x'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['cascaded'], 0)
        self.assertEqual(saved['tasks']['story-wip']['status'], 'working')
        self.assertEqual(saved['tasks']['story-spike']['status'], 'done')

    def test_reject_live_story_is_conflict_not_mutated(self):
        state = self._veto_state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = TestClient(serve.app)
            r = c.post('/api/intent/story/story-live/reject', json={'reason': 'x'})
        self.assertEqual(r.status_code, 409)
        save.assert_not_called()

    def test_reject_unknown_story_is_404(self):
        state = self._veto_state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = TestClient(serve.app)
            r = c.post('/api/intent/story/nope/reject', json={})
        self.assertEqual(r.status_code, 404)
        save.assert_not_called()

    def test_reject_chains_passport_story_vetoed(self):
        state = self._veto_state()
        saved = {}
        logged = []
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action',
                                        side_effect=lambda *a, **k: logged.append((a, k))), \
             unittest.mock.patch.object(serve, '_append_passport_decision') as passport:
            c = TestClient(serve.app)
            r = c.post('/api/intent/story/story-1/reject', json={'reason': 'needs edge case'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(logged[0][0][1], 'story_vetoed')
        self.assertEqual(logged[0][0][2]['author'], 'ben')
        self.assertEqual(passport.call_args[0][0], 'story_vetoed')

# --- Phase E3.5: spike->triage (player promotes findings into real work) -----

    def _spike_state(self):
        # A done spike (findings on the note) + a working spike (not finished) +
        # a non-spike done task (nothing to promote).
        return {
            'agentRoster': [],
            'agents': {},
            'workQueue': [],
            'tasks': {
                'spike-done': {'id': 'spike-done', 'title': 'Can we use WebRTC?',
                               'room': 'pressoffice', 'taskType': 'spike',
                               'status': 'done', 'assignedTo': 'ada',
                               'goal': 'Evaluate WebRTC', 'createdAt': 1000,
                               'note': 'WebRTC works for 1:1 but not mesh. Recommend a relay server.'},
                'spike-live': {'id': 'spike-live', 'title': 'Still investigating auth',
                               'room': 'observatory', 'taskType': 'spike',
                               'status': 'working', 'assignedTo': 'ben', 'createdAt': 2000},
                'story-done': {'id': 'story-done', 'title': 'Build login', 'room': 'pressoffice',
                               'taskType': 'code', 'status': 'done', 'assignedTo': 'ben', 'createdAt': 3000},
            },
            'reports': [],
            'researchTopics': [],
        }

    def test_promote_done_spike_queues_deliverable_embedding_finding(self):
        state = self._spike_state()
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db',
                                        side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, '_append_passport_decision') as passport:
            c = TestClient(serve.app)
            r = c.post('/api/intent/spike/spike-done/promote', json={'taskType': 'code'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # A real deliverable was queued into pressoffice/code (both explicit
        # now -- taskType is required, not defaulted, see BoundedReviewEscalation's
        # sibling gap: PromoteSpikeTaskType below).
        item = body['queued']
        self.assertEqual(item['room'], 'pressoffice')
        self.assertEqual(item['taskType'], 'code')
        self.assertIn('WebRTC', item['title'])
        # The queued item inherits the spike's goal and embeds its written
        # finding in the instructions (the durable work brief).
        self.assertEqual(item['goal'], 'Evaluate WebRTC')
        self.assertEqual(len(saved.get('workQueue', [])), 1)
        queued = saved['workQueue'][0]
        self.assertEqual(queued['taskType'], 'code')
        self.assertIn('relay server', queued['instructions'])
        # The spike itself stays an immutable done record.
        self.assertEqual(saved['tasks']['spike-done']['status'], 'done')
        self.assertEqual(saved['tasks']['spike-done']['taskType'], 'spike')
        self.assertEqual(log.call_args[0][1], 'spike_promoted')
        self.assertEqual(passport.call_args[0][0], 'spike_promoted')

    def test_promote_working_spike_is_conflict(self):
        state = self._spike_state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = TestClient(serve.app)
            r = c.post('/api/intent/spike/spike-live/promote', json={})
        self.assertEqual(r.status_code, 409)
        save.assert_not_called()

    def test_promote_non_spike_is_conflict(self):
        state = self._spike_state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = TestClient(serve.app)
            r = c.post('/api/intent/spike/story-done/promote', json={})
        self.assertEqual(r.status_code, 409)
        save.assert_not_called()

    def test_promote_unknown_spike_is_404(self):
        state = self._spike_state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            c = TestClient(serve.app)
            r = c.post('/api/intent/spike/nope/promote', json={})
        self.assertEqual(r.status_code, 404)
        save.assert_not_called()

    def test_promote_with_library_path_embeds_the_real_findings_not_the_short_note(self):
        """Gap: task.note is a deliberately
        short pointer ("see the Library entry just filed"); the FULL
        findings (source lists, CSVs, feasibility data) only ever lived in
        the Library file. Promoting a spike must pull the real content
        forward when its exact path was recorded, not just the pointer."""
        tmp = tempfile.mkdtemp(prefix='think tank-promote-lib-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, 'archive'), exist_ok=True)
        real_findings = ('# Spike: Can we use WebRTC?\n\nBy: ada\n\n'
                         'Source,URL,Basis\nlibwebrtc,https://webrtc.org,verified\n'
                         'Full real findings far more detailed than the short note.')
        with open(os.path.join(tmp, 'archive', '123-spike-spike-done.md'), 'w') as f:
            f.write(real_findings)
        state = self._spike_state()
        state['tasks']['spike-done']['libraryPath'] = 'archive/123-spike-spike-done.md'
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db', side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'LIBRARY_DIR', tmp), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, '_append_passport_decision'):
            c = TestClient(serve.app)
            r = c.post('/api/intent/spike/spike-done/promote', json={'taskType': 'code'})
        self.assertEqual(r.status_code, 200, r.text)
        queued = saved['workQueue'][0]
        self.assertIn('libwebrtc,https://webrtc.org,verified', queued['instructions'])
        self.assertIn('Full real findings far more detailed', queued['instructions'])

    def test_promote_falls_back_to_the_short_note_when_the_library_file_is_missing(self):
        # libraryPath recorded, but the file itself isn't there (deleted,
        # moved, or a stale path) -- must degrade to the note, not error out.
        state = self._spike_state()
        state['tasks']['spike-done']['libraryPath'] = 'archive/does-not-exist.md'
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db', side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, '_append_passport_decision'):
            c = TestClient(serve.app)
            r = c.post('/api/intent/spike/spike-done/promote', json={'taskType': 'code'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('relay server', saved['workQueue'][0]['instructions'])


# --- Publish: player-triggered push of released work ------------------------

    def _publish_state(self):
        return {
            'agentRoster': [],
            'agents': {},
            'workQueue': [],
            'tasks': {},
            'reports': [],
            'researchTopics': [],
        }

    def test_publish_stages_and_chains_passport(self):
        """The endpoint stages the produced surface and pushes to the configured
        private repo, chaining artifact_published into passport + action log.
        git/db/gh are all mocked so nothing leaves the machine under test."""
        state = self._publish_state()
        logged = []
        git_calls = []
        push_calls = []
        staged = ['projects/finger-drums/LOG.md', 'wiki/engineering/trust.md']
        with tempfile.TemporaryDirectory() as tmp, \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db', side_effect=lambda s: s), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'PUBLISH_REPO', 'testowner/test-repo'), \
             unittest.mock.patch.object(serve, 'PUBLISH_STAGING', os.path.join(tmp, 'staging')), \
             unittest.mock.patch.object(serve, '_stage_released_work', return_value=staged) as stage, \
             unittest.mock.patch.object(serve, '_write_publish_readme',
                                        side_effect=lambda s: os.makedirs(os.path.join(tmp, 'staging'), exist_ok=True)), \
             unittest.mock.patch.object(serve, '_run_git_sync', side_effect=lambda *a, **k: git_calls.append(a[1])), \
             unittest.mock.patch.object(serve, 'subprocess') as sub, \
             unittest.mock.patch.object(serve, 'log_action',
                                        side_effect=lambda *a, **k: logged.append((a, k))), \
             unittest.mock.patch.object(serve, '_append_passport_decision') as passport:
            # gh auth token + the push itself both succeed.
            def _run(cmd, **k):
                if cmd[:2] == ['gh', 'auth']:
                    return unittest.mock.Mock(stdout='gho_token')
                push_calls.append(cmd)
                return unittest.mock.Mock(returncode=0, stdout='done', stderr='')
            sub.run.side_effect = _run
            c = TestClient(serve.app)
            r = c.post('/api/intent/publish', json={})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['ok'], True)
        self.assertEqual(body['repo'], 'testowner/test-repo')
        self.assertEqual(body['entries'], 2)
        # The staging dir was git-initialized, committed, remote-added, and pushed.
        self.assertEqual(git_calls[0][0], 'init')
        self.assertIn(['add', '-A'], git_calls)
        self.assertIn(['commit', '-q', '-m', unittest.mock.ANY], git_calls)
        self.assertTrue(any(c[0] == 'remote' for c in git_calls))
        # The push itself goes direct to subprocess (longer timeout), and the
        # origin URL carries the gh token only transiently.
        self.assertTrue(any(c[0] == 'git' and 'push' in c for c in push_calls))
        self.assertTrue(any('HEAD:main' in c for c in push_calls))
        # Passport + action log chained.
        self.assertEqual(passport.call_args[0][0], 'artifact_published')
        self.assertEqual(logged[0][0][1], 'artifact_published')

    def test_publish_conflict_when_nothing_staged(self):
        """With no released projects/wiki, publish fails closed (409) and never
        touches git/gh."""
        state = self._publish_state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'PUBLISH_REPO', 'testowner/test-repo'), \
             unittest.mock.patch.object(serve, '_stage_released_work', return_value=[]), \
             unittest.mock.patch.object(serve, '_run_git_sync') as git, \
             unittest.mock.patch.object(serve, 'subprocess') as sub:
            c = TestClient(serve.app)
            r = c.post('/api/intent/publish', json={})
        self.assertEqual(r.status_code, 409)
        git.assert_not_called()
        sub.run.assert_not_called()

    def test_publish_failed_push_returns_502_not_ok(self):
        """A rejected push (nonzero exit from git) surfaces as a 502 and does NOT
        chain artifact_published -- a failed publish is not a shipped one."""
        state = self._publish_state()
        with tempfile.TemporaryDirectory() as tmp, \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'PUBLISH_REPO', 'testowner/test-repo'), \
             unittest.mock.patch.object(serve, 'PUBLISH_STAGING', os.path.join(tmp, 'staging')), \
             unittest.mock.patch.object(serve, '_stage_released_work',
                                        return_value=['projects/x']), \
             unittest.mock.patch.object(serve, '_write_publish_readme',
                                        side_effect=lambda s: os.makedirs(os.path.join(tmp, 'staging'), exist_ok=True)), \
             unittest.mock.patch.object(serve, '_run_git_sync', side_effect=lambda *a, **k: None), \
             unittest.mock.patch.object(serve, 'subprocess') as sub, \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, '_append_passport_decision') as passport:
            def _run(cmd, **k):
                if cmd[:2] == ['gh', 'auth']:
                    return unittest.mock.Mock(stdout='gho_token')
                return unittest.mock.Mock(returncode=1, stdout='', stderr='rejected: non-fast-forward')
            sub.run.side_effect = _run
            c = TestClient(serve.app)
            r = c.post('/api/intent/publish', json={})
        self.assertEqual(r.status_code, 502, r.text)
        self.assertIn('push rejected', r.json()['error'])
        # A failed publish must not be passport-chained or logged as shipped.
        log.assert_not_called()
        passport.assert_not_called()

    # --- Phase B: passport integrity watchdog ---------------------------------

    def _build_3block_passport(self):
        # A clean 3-block chain: two promoted FILES (real bytes on disk) plus
        # one DECISION. Returns (tmpdir, PASSPORT_PATH). Mirrors how
        # _append_passport / _append_passport_decision link blocks in serve.py.
        tmp = tempfile.mkdtemp(prefix='think tank-passport-verify-')
        lib = os.path.join(tmp, 'library')
        os.makedirs(lib, exist_ok=True)
        f1 = os.path.join(lib, 'trust.md')
        f2 = os.path.join(lib, 'policy.md')
        with open(f1, 'w') as f:
            f.write('trust is earned\n')
        with open(f2, 'w') as f:
            f.write('policy v2 - updated in place\n')
        p = {'version': 1, 'count': 3, 'head': None, 'blocks': []}
        h1 = serve._sha256_file(f1)
        p['blocks'].append({'index': 1, 'path': 'trust.md', 'sha256': h1,
                            'owner': 'alice', 'promotedBy': 'player', 'prev': None, 'ts': 1})
        p['head'] = h1
        b2 = {'index': 2, 'path': 'policy.md', 'sha256': serve._sha256_file(f2),
              'owner': 'bob', 'promotedBy': 'director', 'prev': p['head'],
              'ts': 2}
        p['blocks'].append(b2)
        h2 = serve._block_link_value(b2)
        p['head'] = h2
        b3 = {'index': 3, 'kind': 'product_released', 'actor': 'player',
              'payload': {'pid': 'p1'}, 'prev': p['head'], 'ts': 3}
        p['blocks'].append(b3)
        p['head'] = serve._block_link_value(b3)
        passport_path = os.path.join(lib, '.passport.json')
        with open(passport_path, 'w') as f:
            json.dump(p, f)
        return tmp, passport_path

    def test_passport_verify_ok_on_intact_chain(self):
        tmp, pp = self._build_3block_passport()
        try:
            # LIBRARY_DIR must be patched too -- _safe_library_path resolves
            # file blocks against serve.LIBRARY_DIR, not PASSPORT_PATH's dir.
            with unittest.mock.patch.object(serve, 'LIBRARY_DIR', os.path.dirname(pp)), \
                 unittest.mock.patch.object(serve, 'PASSPORT_PATH', pp), \
                 unittest.mock.patch.object(serve, 'verify_session', return_value=True):
                c = TestClient(serve.app)
                r = c.get('/api/intent/passport/verify')
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body['ok'], True)
            self.assertEqual(body['count'], 3)
            self.assertEqual(body['badBlocks'], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_passport_verify_catches_mutated_file(self):
        """Editing a promoted file's bytes AFTER it was chained breaks the
        re-hash -- the watchdog must report exactly which block went bad."""
        tmp, pp = self._build_3block_passport()
        try:
            lib = os.path.dirname(pp)
            with open(os.path.join(lib, 'policy.md'), 'w') as f:
                f.write('policy v3 - silently rewritten\n')
            with unittest.mock.patch.object(serve, 'LIBRARY_DIR', lib), \
                 unittest.mock.patch.object(serve, 'PASSPORT_PATH', pp):
                ver = serve.verify_passport()
            self.assertEqual(ver['ok'], False)
            self.assertEqual(ver['badBlocks'], [{'index': 2, 'path': 'policy.md',
                                                 'reason': 'file_mismatch'}])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_passport_verify_catches_broken_link(self):
        """Tampering with an earlier block (here a decision payload) severs the
        link to the next block -- reported as broken_link, not silently ok."""
        tmp, pp = self._build_3block_passport()
        try:
            with open(pp) as f:
                p = json.load(f)
            # Append a 4th (DECISION) block so the chain has a follower, then
            # alter the 3rd DECISION's payload. A clean hash chain cannot detect
            # tampering of the FINAL block via links (that needs an externally-
            # pinned head); adding a follower makes the severed link observable.
            b4 = {'index': 4, 'kind': 'library_write', 'actor': 'player',
                  'payload': {'path': 'think tank/x.md'}, 'prev': p['head'], 'ts': 4}
            p['blocks'].append(b4)
            p['count'] = 4
            p['head'] = serve._block_link_value(b4)
            # Mutate the 3rd (decision, so no file_mismatch masks the link check).
            p['blocks'][2]['payload']['pid'] = 'p2'
            with open(pp, 'w') as f:
                json.dump(p, f)
            with unittest.mock.patch.object(serve, 'LIBRARY_DIR', os.path.dirname(pp)), \
                 unittest.mock.patch.object(serve, 'PASSPORT_PATH', pp):
                ver = serve.verify_passport()
            self.assertEqual(ver['ok'], False)
            self.assertEqual(ver['badBlocks'][0]['reason'], 'broken_link')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PromoteSpikeTaskType(unittest.TestCase):
    """Gap: promote-spike used to silently
    default a missing taskType to 'code' -- a real test promotion of a
    purely informational finding got promoted into taskType='code' anyway
    and burned 45+ review/fix cycles because no reviewer could approve
    'code' that didn't correspond to any real recommendation. taskType is
    now REQUIRED, and 'spike' is a real, valid choice for a non-actionable
    finding (stays out of the peer gate entirely)."""

    def _client(self):
        from fastapi.testclient import TestClient
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        return c

    def _state(self):
        return {
            'agentRoster': [], 'agents': {}, 'workQueue': [],
            'tasks': {'spike-done': {'id': 'spike-done', 'title': 'DreyX research',
                                     'room': 'observatory', 'taskType': 'spike',
                                     'status': 'done', 'assignedTo': 'ada',
                                     'goal': 'Investigate DreyX', 'createdAt': 1000,
                                     'note': 'Purely informational -- no concrete recommendation.'}},
            'reports': [], 'researchTopics': [],
        }

    def test_missing_task_type_is_rejected_not_defaulted(self):
        state = self._state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            r = self._client().post('/api/intent/spike/spike-done/promote', json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn('taskType is required', r.json()['error'])
        save.assert_not_called()

    def test_invalid_task_type_is_rejected(self):
        state = self._state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            r = self._client().post('/api/intent/spike/spike-done/promote', json={'taskType': 'nonsense'})
        self.assertEqual(r.status_code, 400)
        save.assert_not_called()

    def test_spike_task_type_builds_a_real_spike_not_a_deliverable(self):
        state = self._state()
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db', side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, '_append_passport_decision'):
            r = self._client().post('/api/intent/spike/spike-done/promote', json={'taskType': 'spike'})
        self.assertEqual(r.status_code, 200, r.text)
        queued = saved['workQueue'][0]
        self.assertEqual(queued['taskType'], 'spike')
        self.assertIn('budgetMs', queued)
        self.assertIn('Investigate further', queued['instructions'])
        # Never "pursue the recommendation into real work" -- that phrasing
        # presumes actionability a purely informational finding doesn't have.
        self.assertNotIn('Pursue the recommendation', queued['instructions'])

    def test_code_task_type_still_works_explicitly(self):
        state = self._state()
        saved = {}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db', side_effect=lambda s: saved.update(s)), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, '_append_passport_decision'):
            r = self._client().post('/api/intent/spike/spike-done/promote', json={'taskType': 'code'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(saved['workQueue'][0]['taskType'], 'code')
