"""Read-only view of the iPhone's open Safari tabs.

macOS keeps a local iCloud Tabs mirror in Safari's container. This module
opens that SQLite database read-only, filters to one device (default iPhone),
and returns only tab metadata: title, URL, pinned state, and device name.
It never reads page content, history, cookies, credentials, or other apps,
and it never writes to Safari.
"""

import os
import sqlite3
from urllib.parse import urlsplit

CONTAINER_DB = os.path.expanduser(
    '~/Library/Containers/com.apple.Safari/Data/Library/Safari/CloudTabs.db')
LEGACY_DB = os.path.expanduser('~/Library/Safari/CloudTabs.db')


def _db_path():
    for path in (CONTAINER_DB, LEGACY_DB):
        if os.path.isfile(path):
            return path
    return None


def list_tabs(device_substr='iPhone'):
    """Return the open tabs for the first matching device, or []."""
    path = _db_path()
    if not path:
        return []
    try:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            'SELECT d.device_name, t.title, t.url, t.is_pinned '
            'FROM cloud_tabs t JOIN cloud_tab_devices d '
            'ON d.device_uuid = t.device_uuid '
            'WHERE d.device_name LIKE ? '
            'ORDER BY d.device_name, t.rowid',
            (f'%{device_substr}%',)).fetchall()
        con.close()
    except (sqlite3.Error, OSError):
        return []
    out = []
    for row in rows:
        url = (row['url'] or '').strip()
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            continue
        out.append({
            'device': row['device_name'],
            'title': (row['title'] or '').strip(),
            'url': url,
            'pinned': bool(row['is_pinned']),
        })
    return out


def x_tabs(device_substr='iPhone'):
    """The Safari tabs whose host is x.com or twitter.com."""
    out = []
    for tab in list_tabs(device_substr):
        host = urlsplit(tab['url']).hostname or ''
        if host == 'x.com' or host.endswith('.x.com') or \
                host == 'twitter.com' or host.endswith('.twitter.com'):
            out.append(tab)
    return out
