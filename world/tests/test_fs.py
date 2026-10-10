"""Scoped file + Notes access (world/fs.py): containment, the trash rule, cap
enforcement, and Notes ownership. The trust root is a player-issued grant
(scope + caps) in state; three hard rules hold regardless of any grant:

1. CONTAINMENT -- realpath must stay inside the granted scope (no `..`, no
   symlink escape, no absolute path).
2. TRASH RULE -- ~/.Trash is never a valid target; deletes move INTO the trash
   (reversible) and can never empty it.
3. NOTES OWNERSHIP -- existing notes are read-only; an agent may only
   modify/delete notes it created (tracked in noteOwnership).

Hermetic: state is injected via patched get_state_from_db/save_state_to_db and
the scope + trash are throwaway temp dirs (TRASH_DIR env override). No real
filesystem outside the temp dirs is touched.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402
import fs  # noqa: E402


def _grant(scope, caps=None):
    return {'id': 'g-1', 'scope': scope, 'label': 'Test',
            'caps': dict(caps or {'read': True, 'write': True, 'delete': True})}


class FsScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fs-test-')
        self.scope = os.path.join(self.tmp, 'Desktop')
        os.makedirs(self.scope)
        self.trash = os.path.join(self.tmp, 'Trash')
        os.makedirs(self.trash)
        self._env = unittest.mock.patch.dict(os.environ, {'TRASH_DIR': self.trash})
        self._env.start()
        self.state = {'fileGrants': [_grant(self.scope)]}
        self._gs = unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self.state)
        self._ss = unittest.mock.patch.object(serve, 'save_state_to_db', side_effect=lambda s: None)
        self._gs.start()
        self._ss.start()

    def tearDown(self):
        self._gs.stop()
        self._ss.stop()
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_and_read(self):
        with open(os.path.join(self.scope, 'notes.txt'), 'w') as f:
            f.write('hello world')
        entries = fs.list_scope(self.scope)
        self.assertEqual([e['name'] for e in entries], ['notes.txt'])
        self.assertEqual(fs.read_file(self.scope, 'notes.txt'), 'hello world')

    def test_list_subdirectory(self):
        os.makedirs(os.path.join(self.scope, 'sub'))
        with open(os.path.join(self.scope, 'sub', 'inner.txt'), 'w') as f:
            f.write('inner')
        entries = fs.list_scope(self.scope, 'sub')
        self.assertEqual([e['name'] for e in entries], ['inner.txt'])
        self.assertEqual([e['type'] for e in entries], ['file'])
        with self.assertRaises(FileNotFoundError):
            fs.list_scope(self.scope, 'missing')

    def test_write_and_move(self):
        p = fs.write_file(self.scope, 'draft.docx', 'v1')
        self.assertTrue(os.path.isfile(p))
        fs.move_file(self.scope, 'draft.docx', 'final.docx')
        self.assertTrue(os.path.isfile(os.path.join(self.scope, 'final.docx')))
        self.assertFalse(os.path.exists(os.path.join(self.scope, 'draft.docx')))

    def test_parent_escape_refused(self):
        with self.assertRaises(PermissionError):
            fs.read_file(self.scope, '../secret.txt')

    def test_absolute_path_refused(self):
        with self.assertRaises(PermissionError):
            fs.read_file(self.scope, '/etc/passwd')

    def test_symlink_escape_refused(self):
        outside = os.path.join(self.tmp, 'outside.txt')
        with open(outside, 'w') as f:
            f.write('secret')
        link = os.path.join(self.scope, 'link')
        os.symlink(outside, link)
        with self.assertRaises(PermissionError):
            fs.read_file(self.scope, 'link')

    def test_trash_itself_refused(self):
        with self.assertRaises(PermissionError):
            fs.read_file(self.scope, '../Trash/whatever')

    def test_delete_moves_to_trash_not_unlink(self):
        with open(os.path.join(self.scope, 'old.txt'), 'w') as f:
            f.write('x')
        dest = fs.delete_to_trash(self.scope, 'old.txt')
        self.assertFalse(os.path.exists(os.path.join(self.scope, 'old.txt')))
        self.assertTrue(os.path.isfile(dest))
        self.assertTrue(os.path.realpath(dest).startswith(os.path.realpath(self.trash)))

    def test_delete_collision_appends_suffix(self):
        for name in ('a.txt', 'a-1.txt'):
            with open(os.path.join(self.scope, name), 'w') as f:
                f.write('x')
        with open(os.path.join(self.trash, 'a.txt'), 'w') as f:
            f.write('existing')
        dest = fs.delete_to_trash(self.scope, 'a.txt')
        self.assertEqual(os.path.basename(dest), 'a-1.txt')

    def test_cap_denied(self):
        self.state['fileGrants'] = [_grant(self.scope, {'read': True, 'write': False, 'delete': False})]
        with self.assertRaises(PermissionError):
            fs.write_file(self.scope, 'x.txt', 'x')
        with self.assertRaises(PermissionError):
            fs.delete_to_trash(self.scope, 'x.txt')

    def test_no_grant_denied(self):
        self.state['fileGrants'] = []
        with self.assertRaises(PermissionError):
            fs.list_scope(self.scope)


class NotesOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.state = {'noteOwnership': {}}
        self._gs = unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self.state)
        self._ss = unittest.mock.patch.object(serve, 'save_state_to_db', side_effect=lambda s: None)
        self._gs.start()
        self._ss.start()

    def tearDown(self):
        self._gs.stop()
        self._ss.stop()

    def test_create_records_ownership(self):
        with unittest.mock.patch.object(fs, '_osascript', return_value=('note-1', None)):
            nid = fs.notes_create('agent-1', 'Title', 'Body')
        self.assertEqual(nid, 'note-1')
        self.assertEqual(self.state['noteOwnership']['note-1']['agentId'], 'agent-1')

    def test_modify_own_allowed(self):
        self.state['noteOwnership'] = {'note-1': {'agentId': 'agent-1', 'createdAt': 1}}
        with unittest.mock.patch.object(fs, '_osascript', return_value=('ok', None)):
            self.assertTrue(fs.notes_modify('agent-1', 'note-1', 'new body'))

    def test_modify_others_denied(self):
        self.state['noteOwnership'] = {'note-1': {'agentId': 'agent-2', 'createdAt': 1}}
        with self.assertRaises(PermissionError):
            fs.notes_modify('agent-1', 'note-1', 'new body')

    def test_delete_own_allowed_others_denied(self):
        self.state['noteOwnership'] = {'note-1': {'agentId': 'agent-1', 'createdAt': 1}}
        with unittest.mock.patch.object(fs, '_osascript', return_value=('ok', None)):
            self.assertTrue(fs.notes_delete('agent-1', 'note-1'))
        self.assertNotIn('note-1', self.state['noteOwnership'])
        self.state['noteOwnership'] = {'note-2': {'agentId': 'agent-2', 'createdAt': 1}}
        with self.assertRaises(PermissionError):
            fs.notes_delete('agent-1', 'note-2')

    def test_read_allowed_for_any_note(self):
        with unittest.mock.patch.object(fs, '_osascript',
                                        return_value=('A\tbody a\nB\tbody b\n', None)):
            notes = fs.notes_list()
        self.assertEqual([n['title'] for n in notes], ['A', 'B'])


class GrantApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fs-grant-')
        self.scope = os.path.join(self.tmp, 'Desktop')
        os.makedirs(self.scope)
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
        )
        self._cm.start()
        serve.init_db()
        self.state = {'fileGrants': []}
        self._gs = unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self.state)
        self._gs.start()

    def tearDown(self):
        self._gs.stop()
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _player_env(self, **patches):
        p = {'verify_session': lambda c: True}
        p.update(patches)
        return unittest.mock.patch.multiple(serve, **p)

    def test_add_grant_requires_player(self):
        from fastapi.testclient import TestClient
        with self._player_env(verify_session=lambda c: False):
            r = TestClient(serve.app).post('/api/file-grants',
                                           json={'scope': self.scope})
        self.assertEqual(r.status_code, 401)

    def test_add_grant_rejects_empty_trash(self):
        from fastapi.testclient import TestClient
        with self._player_env():
            r = TestClient(serve.app).post('/api/file-grants',
                                           json={'scope': self.scope, 'caps': {'emptyTrash': True}})
        self.assertEqual(r.status_code, 400)

    def test_add_grant_success_and_duplicate(self):
        from fastapi.testclient import TestClient
        client = TestClient(serve.app)
        with self._player_env():
            r = client.post('/api/file-grants', json={'scope': self.scope, 'label': 'Desktop'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['grant']['caps']['emptyTrash'])
        with self._player_env():
            r2 = client.post('/api/file-grants', json={'scope': self.scope})
        self.assertEqual(r2.status_code, 409)

    def test_revoke_grant(self):
        from fastapi.testclient import TestClient
        self.state['fileGrants'] = [{'id': 'g-1', 'scope': self.scope, 'caps': {'read': True}}]
        client = TestClient(serve.app)
        with self._player_env():
            r = client.delete('/api/file-grants/g-1')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.state['fileGrants'], [])


class AppScriptTests(unittest.TestCase):
    """Gated AppleScript app control (world/fs.py app_script): grant check,
    denied-primitive validation, and one-app-only wrapping."""

    def setUp(self):
        self.state = {'appGrants': [{'id': 'a-1', 'bundleId': 'com.apple.Notes',
                                     'label': 'Notes', 'caps': {'use': True}}]}
        self._gs = unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self.state)
        self._gs.start()

    def tearDown(self):
        self._gs.stop()

    def test_no_grant_denied(self):
        self.state['appGrants'] = []
        with self.assertRaises(PermissionError):
            fs.app_script('com.apple.Notes', 'count of notes')

    def test_unknown_app_denied(self):
        with self.assertRaises(PermissionError):
            fs.app_script('com.apple.Unknown', 'count of windows')

    def test_use_cap_required(self):
        self.state['appGrants'] = [{'id': 'a-1', 'bundleId': 'com.apple.Notes',
                                    'label': 'Notes', 'caps': {'use': False}}]
        with self.assertRaises(PermissionError):
            fs.app_script('com.apple.Notes', 'count of notes')

    def test_denied_primitives_fail_closed(self):
        for bad in ('do shell script "ls"',
                    'open location "https://evil.example"',
                    'with administrator privileges',
                    'tell application "Finder" to quit',
                    'current application'):
            with self.subTest(primitive=bad):
                with self.assertRaises(PermissionError):
                    fs.app_script('com.apple.Notes', bad)

    def test_empty_body_denied(self):
        with self.assertRaises(PermissionError):
            fs.app_script('com.apple.Notes', '  ')

    def test_valid_script_wrapped_and_returned(self):
        seen = {}
        def fake_osascript(script):
            seen['script'] = script
            return '3 notes', None
        with unittest.mock.patch.object(fs, '_osascript', side_effect=fake_osascript):
            out = fs.app_script('com.apple.Notes', 'count of notes')
        self.assertEqual(out, '3 notes')
        self.assertEqual(seen['script'],
                         'tell application id "com.apple.Notes"\ncount of notes\nend tell')

    def test_osascript_error_raised(self):
        with unittest.mock.patch.object(fs, '_osascript', return_value=(None, 'not authorized')):
            with self.assertRaises(PermissionError):
                fs.app_script('com.apple.Notes', 'count of notes')


class AppGrantApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fs-appgrant-')
        self._cm = unittest.mock.patch.multiple(
            serve, DB_PATH=os.path.join(self.tmp, 'test.db'), THINK_TANK_DIR=self.tmp)
        self._cm.start()
        serve.init_db()
        self.state = {'appGrants': []}
        self._gs = unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self.state)
        self._gs.start()

    def tearDown(self):
        self._gs.stop()
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _player_env(self, **patches):
        p = {'verify_session': lambda c: True}
        p.update(patches)
        return unittest.mock.patch.multiple(serve, **p)

    def test_add_requires_player(self):
        from fastapi.testclient import TestClient
        with self._player_env(verify_session=lambda c: False):
            r = TestClient(serve.app).post('/api/app-grants',
                                           json={'bundleId': 'com.apple.Notes'})
        self.assertEqual(r.status_code, 401)

    def test_add_rejects_bad_bundle_id(self):
        from fastapi.testclient import TestClient
        for bad in ('Notes', 'com apple notes', '../etc', 'a..b'):
            with self._player_env():
                r = TestClient(serve.app).post('/api/app-grants', json={'bundleId': bad})
            self.assertEqual(r.status_code, 400, msg=bad)

    def test_add_success_and_duplicate(self):
        from fastapi.testclient import TestClient
        client = TestClient(serve.app)
        with self._player_env():
            r = client.post('/api/app-grants', json={'bundleId': 'com.apple.Notes', 'label': 'Notes'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['grant']['caps']['use'])
        with self._player_env():
            r2 = client.post('/api/app-grants', json={'bundleId': 'com.apple.Notes'})
        self.assertEqual(r2.status_code, 409)

    def test_list_and_revoke(self):
        from fastapi.testclient import TestClient
        client = TestClient(serve.app)
        with self._player_env():
            client.post('/api/app-grants', json={'bundleId': 'com.apple.Notes'})
            r = client.get('/api/app-grants')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()['grants']), 1)
        gid = r.json()['grants'][0]['id']
        with self._player_env():
            r2 = client.delete(f'/api/app-grants/{gid}')
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(self.state['appGrants'], [])


if __name__ == '__main__':
    unittest.main()