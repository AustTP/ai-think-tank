"""The two 2026-11-01 operator asks: player-sent URLs auto-allowlist, and a
bookmarks library with per-team scoping plus globals every team can read.

The allowlist side pins _grant_player_direct_links: new hosts grant
(through the real _grant_allowlist, mocked at its settings seam), known
hosts are idempotent no-ops, punctuation around a URL is stripped, and
non-URLs grant nothing. The bookmarks side pins both the HTTP endpoints
(player-only mutations, global/team scope, bm-N max-scan ids) and the
agent tool (team membership rules, admin-only globals, fresh-state
transaction on mutations).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class PlayerDirectLinks(unittest.TestCase):
    def test_new_hosts_grant_and_known_hosts_do_not(self):
        calls = []

        def fake_grant(host, actor='player', via=''):
            calls.append((host, actor, via))
            return host

        with mock.patch.object(serve, '_effective_allowlist_domains',
                               return_value={'known.example'}), \
             mock.patch.object(serve, '_grant_allowlist', side_effect=fake_grant):
            granted = serve._grant_player_direct_links(
                'Read https://fresh.example/docs, and https://known.example/x. '
                'Also https://fresh.example again.')
        self.assertEqual(granted, ['fresh.example'])
        self.assertEqual(calls, [('fresh.example', 'player', 'player direct link')])

    def test_trailing_punctuation_is_stripped(self):
        with mock.patch.object(serve, '_effective_allowlist_domains',
                               return_value=set()), \
             mock.patch.object(serve, '_grant_allowlist',
                               side_effect=lambda h, **k: h) as grant:
            serve._grant_player_direct_links('see https://a.example/x.')
        self.assertEqual(grant.call_args.args[0], 'a.example')

    def test_no_urls_grants_nothing(self):
        with mock.patch.object(serve, '_grant_allowlist') as grant:
            granted = serve._grant_player_direct_links('no links here, just text')
        self.assertEqual(granted, [])
        grant.assert_not_called()

    def test_garbled_url_is_skipped_not_fatal(self):
        with mock.patch.object(serve, '_effective_allowlist_domains',
                               return_value=set()), \
             mock.patch.object(serve, '_grant_allowlist',
                               side_effect=lambda h, **k: h):
            # http://[bad raises in urlparse on some builds; the loop must skip it
            granted = serve._grant_player_direct_links(
                'weird http://[::1 broken and fine https://ok.example')
        self.assertEqual(granted, ['ok.example'])


def _state():
    return {
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True},
            {'id': 'dana', 'director': 'theo'},
            {'id': 'wren'},
        ],
        'agents': {
            'theo': {'villageId': 'main'},
            'wren': {'villageId': 'main'},
        },
        'teams': [
            {'id': 't1', 'directorId': 'wren', 'scrumMasterId': None,
             'members': ['wren']},
            {'id': 't2', 'directorId': 'dana', 'scrumMasterId': None,
             'members': []},
        ],
        'bookmarks': [
            {'id': 'bm-3', 'scope': 'global', 'url': 'https://old.example',
             'title': 'old', 'addedBy': 'player', 'addedAt': 'x'},
            {'id': 'bm-7', 'scope': 't1', 'url': 'https://team.example',
             'title': 'team', 'addedBy': 'wren', 'addedAt': 'x'},
        ],
    }


def _bookmark_tool(agent_id, args, state=None):
    state = state or _state()
    with mock.patch.object(serve, '_state_begin', return_value=state), \
         mock.patch.object(serve, '_state_abort'), \
         mock.patch.object(serve, 'get_state_from_db', return_value=state), \
         mock.patch.object(serve, 'save_state_to_db') as save, \
         mock.patch.object(serve, 'log_action') as log:
        out = serve._bookmarks_tool(agent_id, args)
    return out, state, save, log


class BookmarksTool(unittest.TestCase):
    def test_list_shows_global_and_own_team_only(self):
        out, _state, _save, _log = _bookmark_tool('wren', {'action': 'list'})
        self.assertIn('bm-3', out)      # global
        self.assertIn('bm-7', out)      # wren is in t1
        self.assertIn('global', out)
        self.assertIn('team t1', out)

    def test_member_adds_team_bookmark_with_max_scan_id(self):
        out, state, save, log = _bookmark_tool('wren', {
            'action': 'add', 'url': 'https://new.example',
            'title': 'New thing', 'scope': 't1'})
        self.assertIn('bm-8', out)      # max existing id 7 -> 8, NOT len-based
        self.assertIn('team t1', out)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(log.call_count, 1)
        added = state['bookmarks'][-1]
        self.assertEqual(added['scope'], 't1')
        self.assertEqual(added['addedBy'], 'wren')

    def test_add_scope_defaults_to_the_single_team(self):
        out, state, _save, _log = _bookmark_tool('wren', {
            'action': 'add', 'url': 'https://solo.example'})
        self.assertIn('team t1', out)
        self.assertEqual(state['bookmarks'][-1]['scope'], 't1')

    def test_non_member_cannot_add_to_a_team(self):
        out, state, save, _log = _bookmark_tool('dana', {
            'action': 'add', 'url': 'https://x.example', 'scope': 't1'})
        self.assertTrue(out.startswith('Denied'), out)
        self.assertEqual(save.call_count, 0)
        self.assertEqual(len(state['bookmarks']), 2)

    def test_worker_cannot_add_global_admin_can(self):
        out, _state, save, _log = _bookmark_tool('wren', {
            'action': 'add', 'url': 'https://g.example', 'scope': 'global'})
        self.assertTrue(out.startswith('Denied'), out)
        out2, state2, save2, _log2 = _bookmark_tool('theo', {
            'action': 'add', 'url': 'https://g.example', 'scope': 'global'})
        self.assertIn('global', out2)
        self.assertEqual(save2.call_count, 1)
        self.assertEqual(state2['bookmarks'][-1]['scope'], 'global')

    def test_duplicate_link_in_scope_returns_existing(self):
        out, state, save, _log = _bookmark_tool('wren', {
            'action': 'add', 'url': 'https://team.example/', 'scope': 't1'})
        self.assertIn('bm-7', out)      # the existing entry, trailing / ignored
        self.assertEqual(save.call_count, 0)
        self.assertEqual(len(state['bookmarks']), 2)

    def test_member_deletes_own_team_bookmark_not_other_teams(self):
        out, state, save, _log = _bookmark_tool('wren', {
            'action': 'delete', 'id': 'bm-7'})
        self.assertIn('Deleted', out)
        self.assertEqual(save.call_count, 1)
        self.assertFalse(any(b['id'] == 'bm-7' for b in state['bookmarks']))
        out2, state2, save2, _log2 = _bookmark_tool('dana', {
            'action': 'delete', 'id': 'bm-7'})
        self.assertTrue(out2.startswith('Denied'), out2)
        self.assertEqual(save2.call_count, 0)

    def test_worker_cannot_delete_global_admin_can(self):
        out, _state, save, _log = _bookmark_tool('wren', {
            'action': 'delete', 'id': 'bm-3'})
        self.assertTrue(out.startswith('Denied'), out)
        out2, state2, save2, _log2 = _bookmark_tool('theo', {
            'action': 'delete', 'id': 'bm-3'})
        self.assertIn('Deleted', out2)
        self.assertEqual(save2.call_count, 1)
        self.assertFalse(any(b['id'] == 'bm-3' for b in state2['bookmarks']))


class BookmarkEndpoints(unittest.TestCase):
    def _post(self, state, path, body, actor='player'):
        # verify_session is mocked because the require_login middleware runs
        # before any handler (same reason the villages-chief tests mock it).
        with mock.patch.object(serve, 'verify_session', return_value=True), \
             mock.patch.object(serve, '_operator_gate',
                               return_value=(True, actor)), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, '_state_abort'), \
             mock.patch.object(serve, 'save_state_to_db') as save, \
             mock.patch.object(serve, 'log_action'):
            r = TestClient(serve.app).post(path, json=body)
        return r, save

    def test_player_seeds_a_global_bookmark(self):
        state = _state()
        r, save = self._post(state, '/api/bookmarks',
                             {'url': 'https://idealab.ai',
                              'title': 'iDEALAB', 'scope': 'global'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['bookmark']['id'], 'bm-8')
        self.assertEqual(r.json()['bookmark']['scope'], 'global')
        self.assertEqual(save.call_count, 1)

    def test_non_player_actor_cannot_curate(self):
        r, _save = self._post(_state(), '/api/bookmarks',
                              {'url': 'https://x.example'}, actor='dana')
        self.assertEqual(r.status_code, 403)

    def test_unknown_team_scope_is_404(self):
        r, _save = self._post(_state(), '/api/bookmarks',
                              {'url': 'https://x.example', 'scope': 'nope'})
        self.assertEqual(r.status_code, 404)

    def test_bad_url_is_400(self):
        r, _save = self._post(_state(), '/api/bookmarks', {'url': 'not a url'})
        self.assertEqual(r.status_code, 400)

    def test_delete_removes_and_404s_when_missing(self):
        state = _state()
        r, save = self._post(state, '/api/bookmarks/delete', {'id': 'bm-3'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(save.call_count, 1)
        self.assertFalse(any(b['id'] == 'bm-3' for b in state['bookmarks']))
        r2, _save2 = self._post(_state(), '/api/bookmarks/delete', {'id': 'bm-99'})
        self.assertEqual(r2.status_code, 404)

    def test_list_requires_the_gate(self):
        with mock.patch.object(serve, 'verify_session', return_value=True), \
             mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)), \
             mock.patch.object(serve, 'get_state_from_db', return_value={}):
            r = TestClient(serve.app).get('/api/bookmarks')
        self.assertEqual(r.status_code, 401)


if __name__ == '__main__':
    unittest.main()
