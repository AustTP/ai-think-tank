"""Read-only access to the macOS Photos library, by album.

Why this exists: the iPhone is where photos are taken and tagged (added to an
album). With iCloud Photos on, that album syncs into the Mac's Photos library,
and the think tank can read the synced originals here. No AppleScript and no
Photos.app automation permission are involved: this queries the library's own
SQLite database read-only and copies files that are already downloaded.

Limits, stated plainly:
  * iCloud Photos must be enabled on both devices and the album must have
    finished syncing.
  * "Optimize Mac Storage" can leave an asset cloud-only (`originals/...`
    missing). Those are reported as not-local, never guessed at.
  * The Photos schema is Apple's; the album<->asset join table is discovered by
    shape (`Z_<n>ALBUMS` / `Z_<m>ASSETS`) instead of hardcoding a version.
"""

import datetime
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile

APPLE_EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)


def library_root():
    override = (os.environ.get('APPLE_PHOTOS_LIBRARY') or '').strip()
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser('~/Pictures/Photos Library.photoslibrary')


def _db_path():
    return os.path.join(library_root(), 'database', 'Photos.sqlite')


def _open():
    path = _db_path()
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f'Photos library database not found at {path}; set '
            'APPLE_PHOTOS_LIBRARY if the library lives elsewhere')
    db = sqlite3.connect('file:' + path + '?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def _join_table(db):
    """Find the album<->asset join table by column shape. Returns
    (table, album_column, asset_column)."""
    for (table,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Z\\_%' ESCAPE '\\'"):
        cols = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
        albums = [c for c in cols if re.fullmatch(r'Z_\d+ALBUMS', c)]
        assets = [c for c in cols if re.fullmatch(r'Z_\d+ASSETS', c)]
        if albums and assets:
            return table, albums[0], assets[0]
    raise RuntimeError('could not find the Photos album<->asset join table')


def _resolve_title(db, album):
    """Exact title first, then a trimmed case-insensitive match, so an album
    typed as 'think tank' still finds 'Think Tank'."""
    wanted = (album or '').strip()
    if not wanted:
        return None
    titles = [r[0] for r in db.execute(
        'SELECT ZTITLE FROM ZGENERICALBUM WHERE ZTITLE IS NOT NULL')]
    if wanted in titles:
        return wanted
    low = wanted.lower()
    for title in titles:
        if title.strip().lower() == low:
            return title
    return None


def list_albums():
    with _open() as db:
        return [r[0] for r in db.execute(
            'SELECT ZTITLE FROM ZGENERICALBUM WHERE ZTITLE IS NOT NULL ORDER BY ZTITLE')]


def album_assets(album, limit=None):
    """Photo assets in one album, newest-added first. Returns a list of dicts:
    {uuid, filename, date, favorite, localPath, local}. Videos, trashed and
    hidden assets are excluded. `local` is False when the original has not
    been downloaded to this Mac (cloud-only)."""
    with _open() as db:
        title = _resolve_title(db, album)
        if title is None:
            return []
        table, album_col, asset_col = _join_table(db)
        rows = db.execute(
            f'SELECT a.Z_PK, ast.ZUUID, ast.ZFILENAME, ast.ZDIRECTORY, '
            f'ast.ZFAVORITE, ast.ZDATECREATED, ast.ZADDEDDATE, ast.ZKIND '
            f'FROM ZGENERICALBUM a JOIN "{table}" j ON j."{album_col}" = a.Z_PK '
            f'JOIN ZASSET ast ON ast.Z_PK = j."{asset_col}" '
            f'WHERE a.ZTITLE = ? AND ast.ZKIND = 0 AND ast.ZTRASHEDSTATE = 0 '
            f'AND ast.ZHIDDEN = 0 ORDER BY ast.ZADDEDDATE DESC',
            (title,)).fetchall()
    originals = os.path.join(library_root(), 'originals')
    out = []
    for r in rows:
        path = os.path.join(originals, r['ZDIRECTORY'] or '', r['ZFILENAME'] or '')
        created = APPLE_EPOCH + datetime.timedelta(seconds=r['ZDATECREATED'] or 0)
        out.append({
            'uuid': r['ZUUID'],
            'filename': r['ZFILENAME'],
            'date': created.isoformat(),
            'favorite': bool(r['ZFAVORITE']),
            'localPath': path,
            'local': bool(r['ZFILENAME']) and os.path.isfile(path),
        })
        if limit is not None and len(out) >= int(limit):
            break
    return out


def normalize_to_jpeg(source_path, max_dim=1600):
    """Convert any Photos original (JPEG/PNG/HEIC/...) to a bounded JPEG in a
    temp file using macOS `sips`. Raises if the conversion fails."""
    tmp_dir = tempfile.mkdtemp(prefix='tt-photo-')
    dest = os.path.join(tmp_dir, 'photo.jpg')
    done = subprocess.run(
        ['sips', '-s', 'format', 'jpeg', '-Z', str(int(max_dim)),
         source_path, '--out', dest],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
    if done.returncode != 0 or not os.path.isfile(dest):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(
            'could not convert the photo to JPEG: '
            + (done.stderr or b'').decode('utf-8', 'replace')[-300:])
    return dest, tmp_dir
