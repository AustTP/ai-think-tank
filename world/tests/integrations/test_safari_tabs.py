import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import integrations.safari_tabs as safari_tabs  # noqa: E402


class SafariTabsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='safari-tabs-test-')
        self.db = os.path.join(self.tmp, 'CloudTabs.db')
        con = sqlite3.connect(self.db)
        con.executescript('''
            CREATE TABLE cloud_tab_devices (
                device_uuid TEXT PRIMARY KEY, system_fields BLOB NOT NULL,
                device_name TEXT, has_duplicate_device_name BOOLEAN DEFAULT 0,
                is_ephemeral_device BOOLEAN DEFAULT 0, last_modified REAL NOT NULL);
            CREATE TABLE cloud_tabs (
                tab_uuid TEXT PRIMARY KEY, system_fields BLOB NOT NULL,
                device_uuid TEXT NOT NULL, position BLOB NOT NULL, title TEXT,
                url TEXT NOT NULL, is_showing_reader BOOLEAN DEFAULT 0,
                is_pinned BOOLEAN DEFAULT 0, reader_scroll_position_page_index INTEGER,
                scene_id TEXT);
        ''')
        con.execute("INSERT INTO cloud_tab_devices VALUES ('iphone', x'00', 'Austin iPhone', 0, 0, 0)")
        con.execute("INSERT INTO cloud_tab_devices VALUES ('mac', x'00', 'MacBook', 0, 0, 0)")
        con.execute("INSERT INTO cloud_tabs VALUES ('t1', x'00', 'iphone', x'00', 'Post one', 'https://x.com/a/status/1', 0, 0, NULL, NULL)")
        con.execute("INSERT INTO cloud_tabs VALUES ('t2', x'00', 'iphone', x'00', 'Other', 'https://example.com/a', 0, 0, NULL, NULL)")
        con.execute("INSERT INTO cloud_tabs VALUES ('t3', x'00', 'mac', x'00', 'Mac tab', 'https://x.com/b/status/2', 0, 0, NULL, NULL)")
        con.commit()
        con.close()
        self.old_container = safari_tabs.CONTAINER_DB
        self.old_legacy = safari_tabs.LEGACY_DB
        safari_tabs.CONTAINER_DB = self.db
        safari_tabs.LEGACY_DB = '/nonexistent/CloudTabs.db'

    def tearDown(self):
        safari_tabs.CONTAINER_DB = self.old_container
        safari_tabs.LEGACY_DB = self.old_legacy

    def test_filters_device_and_scheme(self):
        tabs = safari_tabs.list_tabs('iPhone')
        self.assertEqual([t['url'] for t in tabs], ['https://x.com/a/status/1', 'https://example.com/a'])

    def test_x_tabs_only_x_hosts(self):
        tabs = safari_tabs.x_tabs('iPhone')
        self.assertEqual([t['title'] for t in tabs], ['Post one'])
