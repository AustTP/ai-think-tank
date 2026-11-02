"""Knowledge sources: sources of truth (append-only, attributed) and
template repositories (read-only pointers to accumulate around), global
and team-scoped.

Operator direction (2026-11-01): teams need durable references that
outlive a task -- documents the player provides or the team originates,
that agents can append to, plus template repositories (the player's
intel-forge repo, the bug_bounty directory on the Desktop) a team
materializes into its own workspace while the template stays pristine.
Global and team level, mirroring the bookmarks registry exactly: the
max-scan id, the same membership rules, the same curation split
(player/admin for globals), and fresh-state transactions for mutations.
Appends never rewrite existing content. Local origins must sit inside a
player-granted file scope.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402


def _state():
    return {
        'teams': [
            {'id': 't1', 'directorId': 'wren', 'scrumMasterId': None,
             'members': ['wren']},
            {'id': 't2', 'directorId': 'dana', 'scrumMasterId': None,
             'members': []},
        ],
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True},
            {'id': 'wren', 'isAdmin': False},
            {'id': 'dana', 'isAdmin': False},
        ],
        'knowledge_sources': [],
    }


def _tool(agent_id, args, state=None, save=None, log=None):
    state = state if state is not None else _state()
    save = save or mock.Mock()
    log = log or mock.Mock()
    with mock.patch.object(serve, 'get_state_from_db', return_value=state), \
         mock.patch.object(serve, '_state_begin', return_value=state), \
         mock.patch.object(serve, '_state_abort'), \
         mock.patch.object(serve, 'save_state_to_db', save), \
         mock.patch.object(serve, 'log_action', log):
        out = serve._knowledge_tool(agent_id, args)
    return out, state, save, log


class KnowledgeTool(unittest.TestCase):
    def test_register_github_template_via_endpoint_shape(self):
        # The tool has no register action (curation is the player's); the
        # endpoint is tested separately. Here: list is empty-state friendly.
        out, *_ = _tool('wren', {'action': 'list'})
        self.assertIn('No knowledge sources yet', out)

    def test_list_shows_global_and_own_team_only(self):
        state = _state()
        state['knowledge_sources'] = [
            {'id': 'ks-1', 'kind': 'truth', 'scope': 'global', 'title': 'Law notes',
             'origin': {'type': 'inline', 'seed': 'x'}, 'appends': []},
            {'id': 'ks-2', 'kind': 'template', 'scope': 't1', 'title': 'intel forge',
             'origin': {'type': 'github', 'url': 'https://github.com/o/intel-forge'}},
            {'id': 'ks-3', 'kind': 'template', 'scope': 't2', 'title': 'other team',
             'origin': {'type': 'inline', 'seed': 'x'}},
        ]
        out, *_ = _tool('wren', {'action': 'list'}, state=state)
        self.assertIn('ks-1', out)
        self.assertIn('ks-2', out)
        self.assertNotIn('ks-3', out)

    def test_read_truth_renders_seed_and_appends(self):
        state = _state()
        state['knowledge_sources'] = [
            {'id': 'ks-1', 'kind': 'truth', 'scope': 'global', 'title': 'Law notes',
             'origin': {'type': 'inline', 'seed': 'US law only for now.'},
             'appends': [{'by': 'wren', 'at': '2026-11-02T00:00:00Z',
                          'text': 'EU sources absent upstream.'}]},
        ]
        out, *_ = _tool('wren', {'action': 'read', 'id': 'ks-1'}, state=state)
        self.assertIn('US law only for now.', out)
        self.assertIn('EU sources absent upstream.', out)
        self.assertIn('append by wren', out)

    def test_read_template_gives_materialize_instructions(self):
        state = _state()
        state['knowledge_sources'] = [
            {'id': 'ks-2', 'kind': 'template', 'scope': 't1', 'title': 'intel forge',
             'origin': {'type': 'github', 'url': 'https://github.com/o/intel-forge'}},
        ]
        out, *_ = _tool('wren', {'action': 'read', 'id': 'ks-2'}, state=state)
        self.assertIn('github_repo', out)
        self.assertIn('stays pristine', out)

    def test_append_is_attributed_and_never_rewrites(self):
        state = _state()
        state['knowledge_sources'] = [
            {'id': 'ks-1', 'kind': 'truth', 'scope': 'global', 'title': 'Law notes',
             'origin': {'type': 'inline', 'seed': 'seed text'},
             'appends': [{'by': 'player', 'at': 't0', 'text': 'original'}]},
        ]
        out, state, save, log = _tool('wren', {'action': 'append', 'id': 'ks-1',
                                               'text': 'found a new source'}, state=state)
        self.assertIn('Appended to [ks-1]', out)
        appends = state['knowledge_sources'][0]['appends']
        self.assertEqual(len(appends), 2)
        self.assertEqual(appends[0]['text'], 'original')  # seed append untouched
        self.assertEqual(appends[1]['by'], 'wren')
        self.assertEqual(appends[1]['text'], 'found a new source')
        save.assert_called_once()
        log.assert_called_once()

    def test_team_truth_append_denied_to_outsiders(self):
        state = _state()
        state['knowledge_sources'] = [
            {'id': 'ks-2', 'kind': 'truth', 'scope': 't2', 'title': 't2 notes',
             'origin': {'type': 'inline', 'seed': 'x'}, 'appends': []},
        ]
        out, state, save, _log = _tool('wren', {'action': 'append', 'id': 'ks-2',
                                                'text': 'hi'}, state=state)
        # The visibility filter already hides other teams' sources; the
        # membership check inside append is defense in depth.
        self.assertIn('no source', out)
        save.assert_not_called()

    def test_template_cannot_be_appended(self):
        state = _state()
        state['knowledge_sources'] = [
            {'id': 'ks-2', 'kind': 'template', 'scope': 't1', 'title': 'intel forge',
             'origin': {'type': 'github', 'url': 'https://github.com/o/intel-forge'}},
        ]
        out, state, save, _log = _tool('wren', {'action': 'append', 'id': 'ks-2',
                                                'text': 'hi'}, state=state)
        self.assertIn('read-only pointers', out)
        save.assert_not_called()

    def test_unknown_id_is_a_denial_not_a_crash(self):
        out, *_ = _tool('wren', {'action': 'read', 'id': 'ks-99'})
        self.assertIn('no source', out)


class RegistrationValidation(unittest.TestCase):
    def test_github_origin_must_match_the_repo_tool_regex(self):
        origin, err = serve._normalize_knowledge_origin(
            {'type': 'github', 'url': 'https://github.com/o/r'}, 'template', _state())
        self.assertIsNone(err)
        origin, err = serve._normalize_knowledge_origin(
            {'type': 'github', 'url': 'https://gitlab.com/o/r'}, 'template', _state())
        self.assertIsNotNone(err)

    def test_local_origin_must_sit_in_a_granted_scope(self):
        state = _state()
        with mock.patch.object(serve, '_effective_file_grants',
                               return_value=[{'id': 'g1', 'scope': '/Users/x/Desktop'}]):
            origin, err = serve._normalize_knowledge_origin(
                {'type': 'local', 'path': '/Users/x/Desktop/bug_bounty'}, 'template', state)
            self.assertIsNone(err)
            origin, err = serve._normalize_knowledge_origin(
                {'type': 'local', 'path': '/etc'}, 'template', state)
            self.assertIsNotNone(err)

    def test_inline_truth_needs_seed_text(self):
        origin, err = serve._normalize_knowledge_origin(
            {'type': 'inline', 'seed': 'body'}, 'truth', _state())
        self.assertIsNone(err)
        origin, err = serve._normalize_knowledge_origin(
            {'type': 'inline'}, 'truth', _state())
        self.assertIsNotNone(err)

    def test_next_id_is_max_scan(self):
        state = _state()
        state['knowledge_sources'] = [{'id': 'ks-4'}]
        self.assertEqual(serve._knowledge_next_id(state), 'ks-5')


class KnowledgeEndpoints(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        return TestClient(serve.app)

    def _post(self, state, path, body, actor='theo'):
        save = mock.Mock()
        with mock.patch.object(serve, '_operator_gate', return_value=(True, actor)), \
             mock.patch.object(serve, 'verify_session', return_value=True), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, '_state_abort'), \
             mock.patch.object(serve, 'save_state_to_db', save), \
             mock.patch.object(serve, 'log_action'):
            r = self._client().post(path, json=body)
        return r, save

    def test_player_registers_a_global_template(self):
        state = _state()
        r, save = self._post(_state(), '/api/knowledge', {
            'title': 'intel forge', 'kind': 'template', 'scope': 'global',
            'origin': {'type': 'github', 'url': 'https://github.com/o/intel-forge'}}, actor='player')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['source']['id'], 'ks-1')
        save.assert_called_once()

    def test_non_admin_agent_cannot_register(self):
        r, _save = self._post(_state(), '/api/knowledge', {
            'title': 'x', 'kind': 'truth', 'origin': {'type': 'inline', 'seed': 'y'}},
            actor='dana')
        self.assertEqual(r.status_code, 403)

    def test_unknown_team_scope_is_404(self):
        r, _save = self._post(_state(), '/api/knowledge', {
            'title': 'x', 'kind': 'truth', 'scope': 'tx',
            'origin': {'type': 'inline', 'seed': 'y'}})
        self.assertEqual(r.status_code, 404)

    def test_member_appends_via_endpoint(self):
        state = _state()
        state['knowledge_sources'] = [
            {'id': 'ks-1', 'kind': 'truth', 'scope': 't1', 'title': 't1 notes',
             'origin': {'type': 'inline', 'seed': 'x'}, 'appends': []}]
        r, save = self._post(state, '/api/knowledge/append',
                             {'id': 'ks-1', 'text': 'note'}, actor='wren')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['entries'], 1)
        save.assert_called_once()

    def test_outsider_cannot_append_team_truth(self):
        state = _state()
        state['knowledge_sources'] = [
            {'id': 'ks-1', 'kind': 'truth', 'scope': 't2', 'title': 't2 notes',
             'origin': {'type': 'inline', 'seed': 'x'}, 'appends': []}]
        r, save = self._post(state, '/api/knowledge/append',
                             {'id': 'ks-1', 'text': 'note'}, actor='wren')
        self.assertEqual(r.status_code, 403)
        save.assert_not_called()

    def test_append_to_global_truth_open_to_any_agent(self):
        state = _state()
        state['knowledge_sources'] = [
            {'id': 'ks-1', 'kind': 'truth', 'scope': 'global', 'title': 'Law notes',
             'origin': {'type': 'inline', 'seed': 'x'}, 'appends': []}]
        r, save = self._post(state, '/api/knowledge/append',
                             {'id': 'ks-1', 'text': 'note'}, actor='dana')
        self.assertEqual(r.status_code, 200)
        save.assert_called_once()


if __name__ == '__main__':
    unittest.main()
