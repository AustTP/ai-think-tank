"""Injection rails for scoped local file access (2026-11-27 review item).

Composes the mechanical rails between attacker-influenced content (a
browsed page's text, a fetched body, a spike's arguments) and the player's
folders. Content alone can never reach the filesystem because every hop is
a code check that ignores what the text says:

1. CONVERSATION POLICY: `local_file` is commit-tier; the per-conversation
   allowlist (core/tool_policy.task_tool_policy) refuses it unless the task
   carries allowCommit or lists it in approvedTools, and the tool loop
   hard-refuses and logs `tool_policy_denied` (140_call_agent_tool_loop).
2. FS RULES: even a dispatched call's every arg (scope/path/from/to/
   content, whatever produced them) is re-checked per call in core/fs --
   containment, trash rule, and the grant's capability set.
3. GRANT MINTING: grants come only from a player session or a scrum
   master/director agent key (governance.may_grant_entitlements); an
   agent's own HTTP client cannot mint one, and emptyTrash is never
   granted. The ask lane surfaces the tool only when a grant already
   exists, so page content alone can never summon it.
"""
import asyncio
import os
import shutil
import tempfile
import unittest
from unittest import mock

import sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import serve  # noqa: E402
import core.tool_policy as tool_policy  # noqa: E402


class CommitTierGate(unittest.TestCase):
    def test_local_file_is_commit_tier(self):
        self.assertEqual(tool_policy.tool_trust_tier('local_file'), 'commit')

    def test_unapproved_task_refused_even_with_grant(self):
        policy = tool_policy.task_tool_policy(
            {'id': 't1', 'taskType': 'research', 'allowCommit': False})
        self.assertFalse(policy('local_file'))

    def test_approved_tools_list_gates_exactly(self):
        with_list = tool_policy.task_tool_policy(
            {'id': 't1', 'taskType': 'research',
             'approvedTools': ['local_file']})
        self.assertTrue(with_list('local_file'))
        without = tool_policy.task_tool_policy(
            {'id': 't1', 'taskType': 'research', 'approvedTools': ['other']})
        self.assertFalse(without('local_file'))

    def test_pipeline_task_types_still_count_as_approval(self):
        for task_type in ('code', 'review', 'qa'):
            policy = tool_policy.task_tool_policy({'taskType': task_type})
            self.assertTrue(policy('local_file'), task_type)


class ArgsCannotEscapeFsRules(unittest.TestCase):
    """Whatever produced the args -- model, prompt-injected page text, a
    spike payload -- core/fs re-checks every call against the grant."""

    def setUp(self):
        self.scope = tempfile.mkdtemp(prefix='inj-scope-')
        self.outside = tempfile.mkdtemp(prefix='inj-outside-')
        self.trash = tempfile.mkdtemp(prefix='inj-trash-')
        self.grant = {
            'id': 'g1', 'scope': self.scope,
            'caps': {'read': True, 'write': True, 'delete': True,
                     'emptyTrash': False},
        }
        patches = [
            mock.patch.object(serve, 'get_state_from_db',
                              return_value={'fileGrants': [self.grant]}),
            mock.patch.object(serve, '_effective_file_grants',
                              lambda state: [self.grant]),
            mock.patch.dict(os.environ, {'TRASH_DIR': self.trash}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.scope, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)
        shutil.rmtree(self.trash, ignore_errors=True)

    def test_parent_escape_denied(self):
        out = serve._local_file_tool('a1', {
            'action': 'write', 'scope': self.scope,
            'path': os.path.join('..', '..', 'etc', 'passwd'),
            'content': 'injected',
        })
        self.assertTrue(out.startswith('Denied:'), out)
        self.assertIn('escapes the granted scope', out)

    def test_absolute_path_denied(self):
        out = serve._local_file_tool('a1', {
            'action': 'write', 'scope': self.scope,
            'path': os.path.join(self.outside, 'x.txt'), 'content': 'x',
        })
        self.assertTrue(out.startswith('Denied:'), out)
        self.assertIn('path must be relative', out)

    def test_symlink_escape_denied(self):
        link = os.path.join(self.scope, 'escape')
        os.symlink(self.outside, link)
        out = serve._local_file_tool('a1', {
            'action': 'write', 'scope': self.scope,
            'path': os.path.join('escape', 'x.txt'), 'content': 'x',
        })
        self.assertTrue(out.startswith('Denied:'), out)
        self.assertIn('escapes the granted scope', out)

    def test_delete_is_reversible_and_emptytrash_is_not_an_action(self):
        wrote = serve._local_file_tool('a1', {
            'action': 'write', 'scope': self.scope,
            'path': 'note.txt', 'content': 'hi',
        })
        self.assertTrue(wrote.startswith('wrote '), wrote)
        out = serve._local_file_tool('a1', {
            'action': 'delete', 'scope': self.scope, 'path': 'note.txt'})
        self.assertTrue(out.startswith('moved to trash:'), out)
        self.assertIn(self.trash, out)
        self.assertTrue(os.path.exists(os.path.join(
            self.trash, 'note.txt')))
        unknown = serve._local_file_tool('a1', {
            'action': 'emptyTrash', 'scope': self.scope})
        self.assertIn('Unknown local_file action', unknown)
        # The source file is gone from the scope but nothing was unlinked.
        self.assertFalse(os.path.exists(os.path.join(self.scope, 'note.txt')))

    def test_cap_denied(self):
        self.grant['caps']['write'] = False
        out = serve._local_file_tool('a1', {
            'action': 'write', 'scope': self.scope,
            'path': 'x.txt', 'content': 'x'})
        self.assertIn('grant does not allow write', out)

    def test_no_grant_for_this_scope(self):
        other = tempfile.mkdtemp(prefix='inj-other-')
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        out = serve._local_file_tool('a1', {
            'action': 'list', 'scope': other})
        self.assertTrue(out.startswith('Denied:'), out)
        self.assertIn(self.scope, out)


class GrantsCannotBeSelfMinted(unittest.TestCase):
    def _request(self, agent_key=None):
        req = mock.Mock()
        req.cookies = {}
        req.headers = {'X-Agent-Key': agent_key} if agent_key else {}
        return req

    def test_agent_key_without_entitlement_resolves_to_none(self):
        req = self._request('k')
        with mock.patch.object(serve, '_resolve_requester',
                               return_value='a1'), \
             mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={'agentRoster': [], 'teams': []}):
            self.assertIsNone(serve._grant_actor(req))

    def test_handler_refuses_when_actor_is_none(self):
        req = self._request('k')
        with mock.patch.object(serve, '_grant_actor', return_value=None):
            resp = asyncio.run(serve.add_file_grant(req))
            self.assertEqual(resp.status_code, 401)

    def test_player_mint_with_emptytrash_cap_refused(self):
        # emptyTrash is refused at mint time even for a legitimate player,
        # before any state machinery runs (the handler checks caps first).
        scope = tempfile.mkdtemp(prefix='inj-mint-')
        self.addCleanup(shutil.rmtree, scope, ignore_errors=True)
        req = self._request()

        async def _json():
            return {'scope': scope, 'caps': {'emptyTrash': True}}
        req.json = _json
        with mock.patch.object(serve, '_grant_actor', return_value='player'), \
             mock.patch.object(serve, '_state_begin') as state_begin:
            resp = asyncio.run(serve.add_file_grant(req))
            self.assertEqual(resp.status_code, 400)
            self.assertIn('emptyTrash', resp.body.decode())
            state_begin.assert_not_called()


if __name__ == '__main__':
    unittest.main()