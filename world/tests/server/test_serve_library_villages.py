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


class HealthAlertPersistence(unittest.TestCase):
    # The dedup/persistence half -- a real think_tank.db round-trip (same
    # convention as TempAccessGrants below), scoped to a distinctive
    # category so cleanup can't touch a real alert this table might
    # already hold.
    CATEGORY = 'test-health-alert'

    @classmethod
    def setUpClass(cls):
        serve.init_db()

    def tearDown(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM health_alerts WHERE category = ?', (self.CATEGORY,))

    def test_a_new_alert_is_persisted(self):
        serve._persist_new_health_alerts([{'category': self.CATEGORY, 'severity': 'warning', 'message': 'm1'}])
        with serve._db() as conn:
            rows = conn.execute('SELECT message FROM health_alerts WHERE category = ?', (self.CATEGORY,)).fetchall()
        self.assertEqual([r[0] for r in rows], ['m1'])

    def test_the_same_standing_alert_is_not_duplicated_within_the_dedup_window(self):
        item = {'category': self.CATEGORY, 'severity': 'warning', 'message': 'm2'}
        serve._persist_new_health_alerts([item])
        serve._persist_new_health_alerts([item])
        with serve._db() as conn:
            rows = conn.execute('SELECT message FROM health_alerts WHERE category = ?', (self.CATEGORY,)).fetchall()
        self.assertEqual(len(rows), 1)

    def test_a_since_resolved_and_now_new_alert_is_persisted_again(self):
        item = {'category': self.CATEGORY, 'severity': 'warning', 'message': 'm3'}
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO health_alerts (category, severity, message, ts) VALUES (?, ?, ?, ?)',
                (self.CATEGORY, 'warning', 'm3', time.time() - serve.HEALTH_ALERT_DEDUP_WINDOW_S - 10),
            )
        serve._persist_new_health_alerts([item])
        with serve._db() as conn:
            rows = conn.execute('SELECT message FROM health_alerts WHERE category = ?', (self.CATEGORY,)).fetchall()
        self.assertEqual(len(rows), 2)

    def test_a_volatile_message_does_not_spam_a_standing_incident(self):
        # The Jev incident, made concrete: the alert message embeds
        # rolling failure counts, so it CHANGES every cycle even though the
        # incident is the same one. The dedup key is (category, severity), not
        # the message -- the older exact-message key pushed a fresh alert every
        # 5 minutes for the whole outage (53/98 -> 53/86 -> 44/68...).
        serve._persist_new_health_alerts(
            [{'category': self.CATEGORY, 'severity': 'warning', 'message': '44/68 Jev calls failed'}])
        again = serve._persist_new_health_alerts(
            [{'category': self.CATEGORY, 'severity': 'warning', 'message': '53/98 Jev calls failed'}])
        self.assertEqual(again, [],
                         'a same-severity standing category must not re-push because the counts moved')
        with serve._db() as conn:
            rows = conn.execute('SELECT message FROM health_alerts WHERE category = ?', (self.CATEGORY,)).fetchall()
        self.assertEqual(len(rows), 1)

    def test_severity_escalation_pushes_again_within_the_window(self):
        # The same incident worsening from info to warning (or warning to
        # critical) is a different row, so it IS re-persisted and re-pushed --
        # dedup throttles repeats, not escalations.
        serve._persist_new_health_alerts(
            [{'category': self.CATEGORY, 'severity': 'info', 'message': 'mild'}])
        escalated = serve._persist_new_health_alerts(
            [{'category': self.CATEGORY, 'severity': 'warning', 'message': 'worse'}])
        self.assertEqual([a['severity'] for a in escalated], ['warning'])
        with serve._db() as conn:
            rows = conn.execute('SELECT severity FROM health_alerts WHERE category = ?', (self.CATEGORY,)).fetchall()
        self.assertEqual(sorted(r[0] for r in rows), ['info', 'warning'])


class HealthAlertPush(unittest.TestCase):
    # Outbound half: newly-persisted warnings are pushed to the
    # player on every configured channel; info stays dashboard-only and an
    # unconfigured channel must fail closed, never raise.

    def test_warning_alert_is_pushed_to_both_channels(self):
        sent = []
        with unittest.mock.patch.object(serve, '_send_player_email_sync',
                                        side_effect=lambda s, b: sent.append(('email', s, b)) or True), \
             unittest.mock.patch.object(serve, 'send_player_telegram_sync',
                                        side_effect=lambda s, b: sent.append(('telegram', s, b)) or True):
            serve._push_new_health_alerts(
                [{'category': 'jev', 'severity': 'warning', 'message': '2/3 Jev calls failed'}])
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][0], 'email')
        self.assertEqual(sent[1][0], 'telegram')
        self.assertIn('jev', sent[0][1])
        self.assertIn('2/3 Jev calls failed', sent[0][2])

    def test_critical_alert_is_pushed(self):
        sent = []
        with unittest.mock.patch.object(serve, '_send_player_email_sync', side_effect=lambda s, b: sent.append(s) or True), \
             unittest.mock.patch.object(serve, 'send_player_telegram_sync', side_effect=lambda s, b: True):
            serve._push_new_health_alerts([{'category': 'security', 'severity': 'critical', 'message': 'intrusion'}])
        self.assertEqual(len(sent), 1)

    def test_info_alert_is_not_pushed(self):
        with unittest.mock.patch.object(serve, '_send_player_email_sync') as email, \
             unittest.mock.patch.object(serve, 'send_player_telegram_sync') as telegram:
            serve._push_new_health_alerts([{'category': 'work_queue', 'severity': 'info', 'message': 'queue stalled'}])
        email.assert_not_called()
        telegram.assert_not_called()

    def test_unconfigured_channels_fail_closed_without_raising(self):
        # No channel configured (both return False, the same as when the
        # bridge/credential is absent) -- must not raise and must log a line.
        with unittest.mock.patch.object(serve, '_send_player_email_sync', return_value=False), \
             unittest.mock.patch.object(serve, 'send_player_telegram_sync', return_value=False):
            serve._push_new_health_alerts(
                [{'category': 'jev', 'severity': 'warning', 'message': 'down'}])  # must not raise


class LibraryIngest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.results = []

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # Anything actually written into the real Library by these tests
        # needs real cleanup too -- same discipline as every other real-
        # file-touching test has run.
        for r in self.results:
            if r.get('ok') and r.get('path'):
                target = serve._safe_library_path(r['path'])
                if target and os.path.isfile(target):
                    os.remove(target)

    def test_python_file_ingested_as_plain_text(self):
        src = os.path.join(self.tmpdir, 'script.py')
        with open(src, 'w') as f:
            f.write('def f(): return 1\n')
        serve._ingest_one_file(src, 'test-ingest', self.results)
        self.assertTrue(self.results[0]['ok'])
        target = serve._safe_library_path(self.results[0]['path'])
        with open(target) as f:
            self.assertIn('def f()', f.read())

    def test_excel_file_extracted_to_readable_text(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['name', 'score'])
        ws.append(['Ada', 95])
        src = os.path.join(self.tmpdir, 'data.xlsx')
        wb.save(src)
        serve._ingest_one_file(src, 'test-ingest', self.results)
        self.assertTrue(self.results[0]['ok'])
        target = serve._safe_library_path(self.results[0]['path'])
        with open(target) as f:
            content = f.read()
        self.assertIn('Ada', content)
        self.assertIn('95', content)

    def test_image_copied_byte_identical(self):
        src = os.path.join(self.tmpdir, 'icon.png')
        raw = b'\x89PNG\r\n\x1a\nnot a real png but real bytes'
        with open(src, 'wb') as f:
            f.write(raw)
        serve._ingest_one_file(src, 'test-ingest', self.results)
        self.assertTrue(self.results[0]['ok'])
        target = serve._safe_library_path(self.results[0]['path'])
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), raw)

    def test_unsupported_extension_is_skipped_not_silently_copied(self):
        src = os.path.join(self.tmpdir, 'weird.xyz123')
        with open(src, 'w') as f:
            f.write('mystery content')
        serve._ingest_one_file(src, 'test-ingest', self.results)
        self.assertFalse(self.results[0]['ok'])
        self.assertIn('unsupported', self.results[0]['note'])

    def test_oversized_file_is_skipped(self):
        src = os.path.join(self.tmpdir, 'big.py')
        with open(src, 'w') as f:
            f.write('x')
        original_cap = serve.INGEST_MAX_FILE_BYTES
        serve.INGEST_MAX_FILE_BYTES = 0  # force the cap to trigger without writing a real 15MB fixture
        try:
            serve._ingest_one_file(src, 'test-ingest', self.results)
        finally:
            serve.INGEST_MAX_FILE_BYTES = original_cap
        self.assertFalse(self.results[0]['ok'])
        self.assertIn('exceeds', self.results[0]['note'])

    def test_directory_walk_mirrors_structure_and_skips_junk_dirs(self):
        os.makedirs(os.path.join(self.tmpdir, 'sub', 'node_modules'))
        with open(os.path.join(self.tmpdir, 'sub', 'a.py'), 'w') as f:
            f.write('a = 1')
        with open(os.path.join(self.tmpdir, 'sub', 'node_modules', 'skip_me.py'), 'w') as f:
            f.write('should not be ingested')
        serve._ingest_walk(self.tmpdir, 'test-ingest-dir', self.results)
        paths = [r['path'] for r in self.results if r['ok']]
        self.assertTrue(any(p.endswith('sub/a.py') for p in paths))
        self.assertFalse(any('node_modules' in p for p in paths))


class DesignReferenceEndpoints(unittest.TestCase):
    """Player-only designmind-style brief generation: /api/design-reference
    (image upload) and /api/design-reference-url (live URL). Both write a
    design-references/<name>.txt brief any agent can read, and both fail
    closed for non-player callers and on bad input. Vision + screenshot seams
    are patched so no real model call or network fetch happens."""

    def _post(self, path, body, as_agent=False):
        with unittest.mock.patch.object(serve, 'log_action', return_value=None), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            headers = {}
            if as_agent:
                headers['X-Agent-Key'] = 'agent-secret-key'
            return c.post(path, json=body, headers=headers)

    def test_design_reference_image_upload_writes_image_and_brief(self):
        brief_out = {'ok': True, 'brief': 'PALETTE: #111\nTYPOGRAPHY: sans\nLAYOUT: grid\nSTYLE: minimal'}
        written = []
        with unittest.mock.patch.object(serve, '_design_brief_from_image', return_value=brief_out), \
             unittest.mock.patch.object(serve, '_write_file',
                                        side_effect=lambda path, content: written.append((path, content))):
            r = self._post('/api/design-reference',
                           {'agentId': 'player', 'name': 'landing page', 'imageBase64': 'aGVsbG8='})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertIn('PALETTE: #111', body['text'])
        self.assertEqual(body['briefPath'], 'design-references/landing_page.txt')
        self.assertEqual(body['image'], 'design-references/landing_page.png')
        # The brief text goes through _write_file; the image bytes are written
        # directly via open() in the endpoint (binary).
        self.assertEqual(len(written), 1)
        self.assertIn('PALETTE: #111', written[0][1])

    def test_design_reference_rejects_non_player(self):
        with unittest.mock.patch.object(serve, '_design_brief_from_image') as brief:
            r = self._post('/api/design-reference', {'agentId': 'cora', 'name': 'x', 'imageBase64': 'QUJD'})
        self.assertEqual(r.status_code, 403)
        brief.assert_not_called()

    def test_design_reference_validates_input(self):
        # '../evil' sanitizes to 'evil' (path separators stripped), so this is
        # a valid, safe name -- the request proceeds to the vision seam.
        with unittest.mock.patch.object(serve, '_design_brief_from_image',
                                        return_value={'ok': True, 'brief': 'PALETTE: #000'}):
            r = self._post('/api/design-reference', {'agentId': 'player', 'name': '../evil', 'imageBase64': 'QUJD'})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()['briefPath'], 'design-references/evil.txt')
        r = self._post('/api/design-reference', {'agentId': 'player', 'name': '', 'imageBase64': 'QUJD'})
        self.assertEqual(r.status_code, 400)
        r = self._post('/api/design-reference', {'agentId': 'player', 'name': 'ok', 'imageBase64': ''})
        self.assertEqual(r.status_code, 400)
        r = self._post('/api/design-reference', {'agentId': 'player', 'name': 'ok', 'imageBase64': '!!!not-base64!!!'})
        self.assertEqual(r.status_code, 400)

    def test_design_reference_degrades_when_vision_fails(self):
        with unittest.mock.patch.object(serve, '_design_brief_from_image',
                                        return_value={'ok': False, 'note': 'vision tier missing'}):
            r = self._post('/api/design-reference', {'agentId': 'player', 'name': 'x', 'imageBase64': 'QUJD'})
        self.assertEqual(r.status_code, 502)
        self.assertIn('vision tier missing', r.json()['error'])

    def test_design_reference_url_writes_brief_from_live_screenshot(self):
        brief_out = {'ok': True, 'brief': 'STYLE: bold editorial'}
        written = []
        with unittest.mock.patch.object(serve, '_screenshot_url_sync', return_value='U0hPVA=='), \
             unittest.mock.patch.object(serve, '_design_brief_from_image', return_value=brief_out), \
             unittest.mock.patch.object(serve, '_write_file',
                                        side_effect=lambda path, content: written.append((path, content))), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True):
            r = self._post('/api/design-reference-url',
                           {'agentId': 'player', 'url': 'https://example.com/page'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertIn('STYLE: bold editorial', body['text'])
        self.assertEqual(body['briefPath'], 'design-references/example.com.txt')
        self.assertEqual(len(written), 1)

    def test_design_reference_url_blocks_private_hosts(self):
        with unittest.mock.patch.object(serve, '_screenshot_url_sync') as shot:
            r = self._post('/api/design-reference-url',
                           {'agentId': 'player', 'url': 'http://127.0.0.1:8000/admin'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('private or internal', r.json()['error'])
        shot.assert_not_called()

    def test_design_reference_url_degrades_when_screenshot_fails(self):
        with unittest.mock.patch.object(serve, '_screenshot_url_sync', return_value=None), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True):
            r = self._post('/api/design-reference-url', {'agentId': 'player', 'url': 'https://example.com'})
        self.assertEqual(r.status_code, 502)
        self.assertIn('could not screenshot', r.json()['error'])

    def test_design_reference_upload_scopes_to_project_dir(self):
        brief_out = {'ok': True, 'brief': 'PALETTE: #abc'}
        written = []
        with unittest.mock.patch.object(serve, '_design_brief_from_image', return_value=brief_out), \
             unittest.mock.patch.object(serve, '_write_file',
                                        side_effect=lambda path, content: written.append((path, content))):
            r = self._post('/api/design-reference',
                           {'agentId': 'player', 'name': 'hero', 'imageBase64': 'aGVsbG8=', 'project': 'product-1'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['briefPath'], 'design-references/product-1/hero.txt')
        self.assertEqual(body['image'], 'design-references/product-1/hero.png')

    def test_design_reference_url_scopes_to_project_dir(self):
        brief_out = {'ok': True, 'brief': 'STYLE: minimal'}
        with unittest.mock.patch.object(serve, '_screenshot_url_sync', return_value='U0hPVA=='), \
             unittest.mock.patch.object(serve, '_design_brief_from_image', return_value=brief_out), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_write_file') as write:
            r = self._post('/api/design-reference-url',
                           {'agentId': 'player', 'url': 'https://example.com', 'project': 'product-1'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['briefPath'], 'design-references/product-1/example.com.txt')
        write.assert_called_once()
        rel = write.call_args[0][0]
        self.assertIn('product-1', rel)

    def test_design_project_rel_maps_projects_to_subdirs(self):
        self.assertEqual(serve._design_project_rel('product-1'), 'design-references/product-1')
        self.assertEqual(serve._design_project_rel(None), 'design-references')
        self.assertEqual(serve._design_project_rel(''), 'design-references')
        self.assertEqual(serve._design_project_rel('general'), 'design-references')
        # '../evil' sanitizes to a single safe segment 'evil' (dots are
        # preserved as filename characters, never as a traversal).
        self.assertEqual(serve._design_project_rel('../evil'), 'design-references/evil')

    def test_design_project_rel_scopes_non_main_villages(self):
        # THE boundary: a non-default village gets its own design-references/
        # <village>/ subtree so its taste never collides with the main village's
        # or another village's. 'main' keeps the flat root (backward compatible).
        self.assertEqual(serve._design_project_rel('product-1', village='north'),
                         'design-references/north/product-1')
        self.assertEqual(serve._design_project_rel(None, village='north'),
                         'design-references/north')
        self.assertEqual(serve._design_project_rel('product-1', village='main'),
                         'design-references/product-1')
        # '../evil' sanitizes to a single safe village segment.
        self.assertEqual(serve._design_project_rel('product-1', village='../evil'),
                         'design-references/evil/product-1')

    def test_design_reference_upload_scopes_to_village_dir(self):
        # A design brief for the 'north' village lands under
        # design-references/north/, never the main village's tree.
        brief_out = {'ok': True, 'brief': 'STYLE: brutalist'}
        written = []
        with unittest.mock.patch.object(serve, '_design_brief_from_image', return_value=brief_out), \
             unittest.mock.patch.object(serve, '_write_file',
                                        side_effect=lambda path, content: written.append((path, content))):
            r = self._post('/api/design-reference',
                           {'agentId': 'player', 'name': 'hero', 'imageBase64': 'aGVsbG8=', 'village': 'north'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['briefPath'], 'design-references/north/hero.txt')
        self.assertEqual(r.json()['image'], 'design-references/north/hero.png')

    def test_design_context_for_project_is_village_scoped(self):
        # _design_context_for_project must resolve a taste doc inside the
        # reader's village dir -- main and north read different files.
        taste_main = []
        with unittest.mock.patch.object(serve, '_safe_library_path',
                                        side_effect=lambda p: os.path.join('/tmp/lib', p)), \
             unittest.mock.patch.object(serve, '_list_design_briefs', return_value=[]):
            with unittest.mock.patch('os.path.isfile',
                                     side_effect=lambda p: p == '/tmp/lib/design-references/product-1/taste.md'), \
                 unittest.mock.patch('builtins.open', unittest.mock.mock_open(read_data='MAIN TASTE')):
                ctx_main = serve._design_context_for_project('product-1', village='main')
            with unittest.mock.patch('os.path.isfile',
                                     side_effect=lambda p: p == '/tmp/lib/design-references/north/product-1/taste.md'), \
                 unittest.mock.patch('builtins.open', unittest.mock.mock_open(read_data='NORTH TASTE')):
                ctx_north = serve._design_context_for_project('product-1', village='north')
        self.assertIn('MAIN TASTE', ctx_main)
        self.assertIn('NORTH TASTE', ctx_north)

    def test_design_reference_taste_folds_into_village_dir(self):
        # The taste fold for a village writes to that village's subtree.
        taste_out = 'STYLE: brutalist'
        brief_files = ['design-references/north/hero.txt', 'design-references/north/nav.txt']
        written = []
        with unittest.mock.patch.object(serve, '_list_design_briefs', return_value=brief_files), \
             unittest.mock.patch.object(serve, '_safe_library_path',
                                        side_effect=lambda p: p if p.startswith('design-references') else None), \
             unittest.mock.patch('os.path.isfile', return_value=True), \
             unittest.mock.patch.object(serve, '_mid_tier_slug', return_value='mid-x'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        return_value={'choices': [{'message': {'content': taste_out}}]}), \
             unittest.mock.patch.object(serve, '_write_file',
                                        side_effect=lambda path, content: written.append((path, content))), \
             unittest.mock.patch('builtins.open', unittest.mock.mock_open(read_data='STYLE: brutalist')):
            r = self._post('/api/design-reference/taste',
                           {'agentId': 'player', 'project': 'product-1', 'village': 'north'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['tastePath'], 'design-references/north/product-1/taste.md')
        self.assertTrue(any('north' in str(p) and 'taste.md' in str(p) for p, _ in written))

    def test_list_design_briefs_excludes_taste_and_missing_dirs(self):
        self.assertEqual(serve._list_design_briefs('no-such-project'), [])

    def test_design_reference_taste_folds_briefs(self):
        taste_out = 'PALETTE: #111\nTYPOGRAPHY: sans\nLAYOUT: grid\nSTYLE: minimal'
        brief_files = ['design-references/product-1/hero.txt', 'design-references/product-1/nav.txt']
        written = []
        with unittest.mock.patch.object(serve, '_list_design_briefs', return_value=brief_files), \
             unittest.mock.patch.object(serve, '_safe_library_path',
                                        side_effect=lambda p: p if p.startswith('design-references') else None), \
             unittest.mock.patch('os.path.isfile', return_value=True), \
             unittest.mock.patch.object(serve, '_mid_tier_slug', return_value='mid-x'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        return_value={'choices': [{'message': {'content': taste_out}}]}), \
             unittest.mock.patch.object(serve, '_write_file',
                                        side_effect=lambda path, content: written.append((path, content))), \
             unittest.mock.patch('builtins.open', unittest.mock.mock_open(read_data='PALETTE: #111')):
            r = self._post('/api/design-reference/taste', {'agentId': 'player', 'project': 'product-1'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['tastePath'], 'design-references/product-1/taste.md')
        self.assertEqual(body['briefCount'], 2)
        self.assertIn('PALETTE: #111', body['text'])
        self.assertTrue(any('taste.md' in str(p) for p, _ in written))

    def test_design_reference_taste_rejects_empty_project(self):
        with unittest.mock.patch.object(serve, '_list_design_briefs', return_value=[]):
            r = self._post('/api/design-reference/taste', {'agentId': 'player', 'project': 'product-1'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('no design briefs', r.json()['error'])

    def test_design_context_for_project_reads_taste_doc(self):
        with unittest.mock.patch.object(serve, '_safe_library_path',
                                        side_effect=lambda p: p if p.startswith('design-references') else None), \
             unittest.mock.patch('os.path.isfile', return_value=True), \
             unittest.mock.patch.object(serve, '_list_design_briefs', return_value=[]), \
             unittest.mock.patch('builtins.open', unittest.mock.mock_open(read_data='PALETTE: #222')):
            ctx = serve._design_context_for_project('product-1')
        self.assertIn('PALETTE: #222', ctx)
        self.assertIn('READ THIS FIRST', ctx)

    def test_design_context_for_project_empty_without_taste(self):
        with unittest.mock.patch.object(serve, '_safe_library_path', return_value=None), \
             unittest.mock.patch.object(serve, '_list_design_briefs', return_value=[]):
            self.assertEqual(serve._design_context_for_project('no-taste-project'), '')

    def test_design_context_skips_expired_taste(self):
        # A taste doc whose Review-after header has passed is NOT injected as
        # trusted design context (recheck before acting). A future one is.
        import datetime as _dt
        expired = _dt.date.today() - _dt.timedelta(days=1)
        future = _dt.date.today() + _dt.timedelta(days=10)
        for label, review, expected in (
                ('expired', expired.strftime('%Y-%m-%d'), False),
                ('future', future.strftime('%Y-%m-%d'), True)):
            taste = f'# Design taste\n- Owner: player\n- Review after: {review}\n\nPALETTE: #111'
            with self.subTest(label=label):
                with unittest.mock.patch.object(serve, '_safe_library_path',
                                                side_effect=lambda p: p if p.startswith('design-references') else None), \
                     unittest.mock.patch('os.path.isfile', return_value=True), \
                     unittest.mock.patch.object(serve, '_list_design_briefs', return_value=[]), \
                     unittest.mock.patch('builtins.open', unittest.mock.mock_open(read_data=taste)):
                    ctx = serve._design_context_for_project('product-1')
                if expected:
                    self.assertIn('PALETTE: #111', ctx)
                else:
                    self.assertEqual(ctx, '')

    def test_taste_is_expired_parses_review_date(self):
        import datetime as _dt
        past = _dt.date.today() - _dt.timedelta(days=3)
        future = _dt.date.today() + _dt.timedelta(days=3)
        self.assertTrue(serve._taste_is_expired(f'- Review after: {past.strftime("%Y-%m-%d")}'))
        self.assertFalse(serve._taste_is_expired(f'- Review after: {future.strftime("%Y-%m-%d")}'))
        self.assertFalse(serve._taste_is_expired('no review header'))
        self.assertFalse(serve._taste_is_expired('- Review after: not-a-date'))

    def test_design_taste_fold_writes_owner_and_review_after(self):
        taste_out = 'STYLE: minimal'
        brief_files = ['design-references/product-1/hero.txt']
        written = []
        with unittest.mock.patch.object(serve, '_list_design_briefs', return_value=brief_files), \
             unittest.mock.patch.object(serve, '_safe_library_path',
                                        side_effect=lambda p: p if p.startswith('design-references') else None), \
             unittest.mock.patch('os.path.isfile', return_value=True), \
             unittest.mock.patch.object(serve, '_mid_tier_slug', return_value='mid-x'), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync',
                                        return_value={'choices': [{'message': {'content': taste_out}}]}), \
             unittest.mock.patch.object(serve, '_write_file',
                                        side_effect=lambda path, content: written.append((path, content))), \
             unittest.mock.patch('builtins.open', unittest.mock.mock_open(read_data='STYLE: minimal')):
            r = self._post('/api/design-reference/taste',
                           {'agentId': 'player', 'project': 'product-1',
                            'owner': 'maya', 'reviewAfterDays': 1})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(written[-1][1].splitlines()[3], '- Owner: maya')


class VillageEndpoints(unittest.TestCase):
    """The village boundary API: create a village, list it (with members), and
    move agents into it (all player-only). Combined with the read-time scoping
    in wiki + design taste, these make the boundary real end to end."""

    def _state(self):
        return {
            'villages': [{'id': 'main', 'name': 'Main Village'}],
            'agents': {
                'ada': {'id': 'ada', 'name': 'Ada'},
                'ben': {'id': 'ben', 'name': 'Ben'},
            },
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada'},
                {'id': 'ben', 'name': 'Ben'},
            ],
            'wiki': {'pages': {}, 'categories': {}},
        }

    def _client(self):
        return TestClient(serve.app)

    def test_create_village(self):
        state = self._state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action', return_value=None):
            r = self._client().post('/api/villages', json={'name': 'North'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['village']['name'], 'North')
        self.assertTrue(any(v['name'] == 'North' for v in state['villages']))

    def test_create_village_rejects_duplicate_and_blank(self):
        state = self._state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action', return_value=None):
            r = self._client().post('/api/villages', json={'name': 'Main Village'})
            self.assertEqual(r.status_code, 409)
            r = self._client().post('/api/villages', json={'name': '   '})
            self.assertEqual(r.status_code, 400)

    def test_create_village_rejects_non_player(self):
        state = self._state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, 'log_action', return_value=None):
            r = self._client().post('/api/villages', json={'name': 'North'})
        self.assertEqual(r.status_code, 401)

    def test_assign_agent_to_village(self):
        state = self._state()
        import sim as _sim
        _sim.create_village(state, 'North')
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action', return_value=None):
            r = self._client().post('/api/villages/assign', json={'agentId': 'ada', 'villageId': 'vlg-1'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(state['agents']['ada']['villageId'], 'vlg-1')

    def test_assign_rejects_unknown_agent_or_village(self):
        state = self._state()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action', return_value=None):
            r = self._client().post('/api/villages/assign', json={'agentId': 'nobody', 'villageId': 'main'})
            self.assertEqual(r.status_code, 400)
            r = self._client().post('/api/villages/assign', json={'agentId': 'ada', 'villageId': 'nope'})
            self.assertEqual(r.status_code, 400)

    def test_list_villages_groups_members(self):
        state = self._state()
        import sim as _sim
        _sim.create_village(state, 'North')
        _sim.set_agent_village(state, 'ada', 'vlg-1')
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True), \
             unittest.mock.patch.object(serve, 'log_action', return_value=None):
            r = self._client().get('/api/villages')
        self.assertEqual(r.status_code, 200, r.text)
        villages = {v['id']: v for v in r.json()['villages']}
        self.assertEqual(villages['vlg-1']['name'], 'North')
        self.assertIn('ada', villages['vlg-1']['members'])
        self.assertIn('ben', villages['main']['members'])


class ServerOwnedSeed(unittest.TestCase):
    # Server-owned roster: no static agent names live in any JS
    # file; serve.py seeds the default roster into an empty database on first
    # boot and stamps the director tier before any client reads it. These test
    # that against a THROWAWAY database (fresh temp file), never the real
    # think_tank.db.
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-seed-test-')
        # Redirect all state/disk side effects into the temp sandbox.
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            SANDBOXES_DIR=os.path.join(self.tmp, 'sandboxes'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        # Seed identities are config-driven (SEED_ROSTER / SEED_DIRECTOR_MAP /
        # SEED_ADMIN_IDS / SEED_SENIOR_DIRECTOR in .env, so this
        # hermetic test injects them rather than relying on a real .env.
        # The map deliberately includes ada/dev/eli -> theo (a pure admin
        # direct-report shape) to exercise the R2 repair sweep: those three
        # are pure workers, so the sweep must re-point them to nora.
        self._env_patch = unittest.mock.patch.object(
            serve, '_load_env', return_value={
                'SEED_ROSTER': ('ada|Ada|#e06666|Research|small,'
                                'ben|Ben|#6fa8dc|Banking|small,'
                                'cora|Cora|#93c47d|Post Office|small,'
                                'dev|Dev|#ffd966|Studio|small,'
                                'eli|Eli|#c27ba0|Weather Station|small,'
                                'theo|Theo|#b48ce0|Control Room|mid,'
                                'nora|Nora|#e69138|Personnel|mid'),
                'SEED_DIRECTOR_MAP': 'ada:theo,dev:theo,eli:theo,ben:nora,cora:nora',
                'SEED_ADMIN_IDS': 'theo',
                'SEED_SENIOR_DIRECTOR': 'nora',
            })
        self._env_patch.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        self._env_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_roster_is_typed_and_free_of_authority_fields(self):
        roster = serve._default_roster_definitions()
        self.assertEqual(len(roster), 7)
        names = {d['name'] for d in roster}
        self.assertEqual(names, {'Ada', 'Ben', 'Cora', 'Dev', 'Eli', 'Theo', 'Nora'})
        for d in roster:
            self.assertTrue(d['id'])
            self.assertTrue(d['name'])
            self.assertNotIn('isAdmin', d, 'authority graph must not be a seed literal -- stamped from the DB')
            self.assertNotIn('isDirector', d)
            self.assertNotIn('director', d)

    def test_seed_populates_an_empty_db(self):
        refreshed = serve.get_state_from_db()
        self.assertIsNone(refreshed, 'fresh temp DB starts empty')
        self.assertTrue(serve._seed_default_roster())
        state = serve.get_state_from_db()
        self.assertIsNotNone(state)
        self.assertEqual(len(state['agentRoster']), 7)
        self.assertEqual(len(state['agents']), 7)
        # Director tier stamped server-side, not carried in the seed literal.
        theo = next(d for d in state['agentRoster'] if d['id'] == 'theo')
        nora = next(d for d in state['agentRoster'] if d['id'] == 'nora')
        self.assertTrue(theo.get('isAdmin'))
        self.assertTrue(theo.get('isDirector'))
        self.assertFalse(theo.get('director'))
        self.assertTrue(nora.get('isDirector'))
        self.assertFalse(nora.get('isAdmin'))
        self.assertFalse(nora.get('director'), 'senior-most director has no own director')

    def test_seed_is_idempotent(self):
        self.assertTrue(serve._seed_default_roster())
        self.assertFalse(serve._seed_default_roster(), 'must not reseed an already-populated DB')
        self.assertEqual(len(serve.get_state_from_db()['agentRoster']), 7)

    def test_seed_and_backfill_together_stamp_full_chain(self):
        # After seeding an empty DB, the walk-the-chain pointers backfill so
        # the base roster is authoritative and connected from the first read.
        # (nadia/maya/priya are NOT part of the base seed -- they exist only
        # once real hires add them; the backfill stamps whomever IS present.)
        # R2: the SEED_DIRECTOR_MAP points ada/dev/eli at the admin (theo),
        # but the repair sweep re-points pure workers up to the senior director
        # (nora) so no admin ever DIRECTLY directors a pure worker.
        serve._seed_default_roster()
        state = serve.get_state_from_db()
        by_id = {d['id']: d for d in state['agentRoster']}
        self.assertEqual(by_id['dev']['director'], 'nora')
        self.assertEqual(by_id['eli']['director'], 'nora')
        self.assertEqual(by_id['ben']['director'], 'nora')
        self.assertEqual(by_id['ada']['director'], 'nora')
        self.assertEqual(by_id['cora']['director'], 'nora')
        self.assertFalse(by_id['nora'].get('director'))       # senior-most director
        self.assertFalse(by_id['theo'].get('director'))       # admin at top
        self.assertTrue(by_id['theo']['isAdmin'])
        self.assertTrue(by_id['nora']['isDirector'])          # stamped, not literal
        # R2 invariant holds after backfill: the admin has NO pure-worker
        # direct reports (theo's direct reports must all be directors).
        theo_reports = [d['id'] for d in state['agentRoster']
                        if d.get('director') == 'theo']
        for rid in theo_reports:
            rd = by_id[rid]
            self.assertTrue(
                rd.get('isDirector') or
                any(x.get('director') == rid for x in state['agentRoster']),
                f'{rid} reports to admin but is neither flagged a director '
                f'nor has direct reports (R2 violation)')


class WalkTheChainDirectorAuth(unittest.TestCase):
    # Derive a small realistic roster and assert the walk-the-chain derivations
    # (faye admin, nora senior director, dev/sam mid-directors with direct
    # reports), using pure functions off an in-memory state dict.
    def _state(self):
        return {
            'agentRoster': [
                {'id': 'faye', 'isAdmin': True, 'isDirector': True},
                {'id': 'nora', 'isDirector': True},
                {'id': 'dev', 'director': 'faye'},
                {'id': 'sam', 'director': 'faye'},
                {'id': 'maya', 'director': 'sam'},
                {'id': 'nadia', 'director': 'dev'},
                {'id': 'zara', 'director': 'nora'},
                {'id': 'tom', 'director': 'nora'},
            ],
        }

    def test_admin_and_senior_director_detection(self):
        s = self._state()
        self.assertTrue(serve._is_admin(s, 'faye'))
        self.assertFalse(serve._is_admin(s, 'nora'))
        self.assertEqual(serve._senior_most_director_id(s), 'nora')

    def test_direct_reports_and_chain(self):
        s = self._state()
        self.assertEqual(serve._direct_reports(s, 'dev'), ['nadia'])
        self.assertEqual(serve._direct_reports(s, 'sam'), ['maya'])
        self.assertEqual(
            serve._director_chain(s, 'maya'),
            ['maya', 'sam', 'faye'],
        )
        self.assertEqual(
            serve._director_chain(s, 'nadia'),
            ['nadia', 'dev', 'faye'],
        )

    def test_can_write_self_director_above_admin(self):
        s = self._state()
        self.assertTrue(serve._can_write_agent(s, 'nadia', 'nadia'))          # self, always
        self.assertTrue(serve._can_write_agent(s, 'dev', 'nadia'))            # director writing subordinate
        self.assertTrue(serve._can_write_agent(s, 'faye', 'nadia'))           # admin to anyone
        self.assertTrue(serve._can_write_agent(s, 'faye', 'zara'))            # admin to another chain
        self.assertTrue(serve._can_write_agent(s, 'sam', 'maya'))             # mid-director to own report
        # Write authority flows DOWN the chain: a subordinate cannot write the
        # director's own files, nor a peer's across teams.
        self.assertFalse(serve._can_write_agent(s, 'nadia', 'dev'))           # subordinate -> director denied
        self.assertFalse(serve._can_write_agent(s, 'maya', 'nadia'))          # cross-chain subordinate denied

    def test_can_write_denies_cross_team_and_player(self):
        s = self._state()
        self.assertFalse(serve._can_write_agent(s, 'nadia', 'zara'))          # unrelated
        self.assertFalse(serve._can_write_agent(s, 'zara', 'maya'))           # another chain
        self.assertFalse(serve._can_write_agent(s, 'player', 'nadia'))        # player is never an agent


class AdminNeverDirectorsPureWorkers(unittest.TestCase):
    # R2: an admin must never DIRECTLY director a pure worker (no reports,
    # not isDirector). The boot repair sweep in _backfill_directors_in_db
    # re-points such pure workers up to the senior director (or any non-admin
    # director). Mid-level directors under the admin are legal and untouched.
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-r2-sweep-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        # Env: theo admin, nora senior director, NO seed director map (the
        # violating shape is built directly in the DB state below).
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
        # Build state with agents mirroring the roster so save/load round-trips.
        state = {
            'agentRoster': roster,
            'agents': {d['id']: {'profile': {}, 'role': 'Research'} for d in roster},
        }
        serve.save_state_to_db(state)
        return state

    def test_pure_worker_under_admin_is_repointed_to_senior_director(self):
        # Violating seed: ada (pure worker, no reports, no isDirector flag)
        # has the admin (theo) as her director. The sweep must move her to
        # nora.
        self._install_state([
            {'id': 'theo', 'isAdmin': True, 'isDirector': True, 'director': None},
            {'id': 'nora', 'isDirector': True, 'director': None},
            {'id': 'ada', 'director': 'theo'},
            {'id': 'ben', 'director': 'nora'},
        ])
        serve._backfill_directors_in_db()
        by_id = {d['id']: d for d in serve.get_state_from_db()['agentRoster']}
        self.assertEqual(by_id['ada']['director'], 'nora',
                         'pure worker under admin must be re-pointed to senior director')
        self.assertEqual(by_id['ben']['director'], 'nora')  # untouched

    def test_mid_level_director_under_admin_is_left_alone(self):
        # dev has a direct report (nadia) -- dev is a director by structure.
        # admin -> director is the legal shape; the sweep must NOT re-point.
        self._install_state([
            {'id': 'theo', 'isAdmin': True, 'isDirector': True, 'director': None},
            {'id': 'nora', 'isDirector': True, 'director': None},
            {'id': 'dev', 'director': 'theo'},
            {'id': 'nadia', 'director': 'dev'},
        ])
        serve._backfill_directors_in_db()
        by_id = {d['id']: d for d in serve.get_state_from_db()['agentRoster']}
        self.assertEqual(by_id['dev']['director'], 'theo',
                         'a director with reports under the admin is the legal shape')
        self.assertEqual(by_id['nadia']['director'], 'dev')

    def test_flagged_director_under_admin_is_left_alone(self):
        # isDirector flag alone (even without reports yet) marks a director;
        # admin -> flagged-director is legal, no re-point.
        self._install_state([
            {'id': 'theo', 'isAdmin': True, 'isDirector': True, 'director': None},
            {'id': 'nora', 'isDirector': True, 'director': None},
            {'id': 'sam', 'isDirector': True, 'director': 'theo'},
        ])
        serve._backfill_directors_in_db()
        by_id = {d['id']: d for d in serve.get_state_from_db()['agentRoster']}
        self.assertEqual(by_id['sam']['director'], 'theo')

    def test_sweep_is_idempotent(self):
        self._install_state([
            {'id': 'theo', 'isAdmin': True, 'isDirector': True, 'director': None},
            {'id': 'nora', 'isDirector': True, 'director': None},
            {'id': 'ada', 'director': 'theo'},
        ])
        serve._backfill_directors_in_db()
        first = serve.get_state_from_db()
        serve._backfill_directors_in_db()
        second = serve.get_state_from_db()
        self.assertEqual(
            {d['id']: d.get('director') for d in first['agentRoster']},
            {d['id']: d.get('director') for d in second['agentRoster']},
            'a second backfill run must be a no-op once the shape is legal')

    def test_safe_director_guard_repoints_pure_worker(self):
        s = {
            'agentRoster': [
                {'id': 'theo', 'isAdmin': True, 'isDirector': True},
                {'id': 'nora', 'isDirector': True},
                {'id': 'ada', 'director': 'nora'},  # pure worker
            ],
        }
        # Proposing the admin as ada's director must fall back to nora.
        self.assertEqual(serve._safe_director(s, 'ada', 'theo'), 'nora')
        # A non-admin target passes through unchanged.
        self.assertEqual(serve._safe_director(s, 'ada', 'nora'), 'nora')
        # A director (flagged) proposed under the admin also passes through.
        self.assertEqual(serve._safe_director(s, 'nora', 'theo'), 'theo')
        # None (top-level) passes through unchanged.
        self.assertIsNone(serve._safe_director(s, 'nora', None))
