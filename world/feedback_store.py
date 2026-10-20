"""SQLite storage for the player feedback surface and taste profile."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS player_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id TEXT NOT NULL,
    player TEXT NOT NULL,
    sentiment TEXT,
    works TEXT,
    notes TEXT NOT NULL DEFAULT '',
    ratings TEXT NOT NULL DEFAULT '{}',
    quick_actions TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_player_feedback_item
    ON player_feedback (work_item_id, created_at DESC);
CREATE TABLE IF NOT EXISTS player_taste (
    player TEXT PRIMARY KEY,
    weights TEXT NOT NULL DEFAULT '{}',
    notes TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
"""


class FeedbackStore:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add(self, work_item_id, player, sentiment=None, works=None,
            notes='', ratings=None, quick_actions=None):
        if not work_item_id:
            raise ValueError('workItemId is required')
        if sentiment not in (None, 'like', 'dislike'):
            raise ValueError('sentiment must be like or dislike')
        if works not in (None, 'works', 'broken'):
            raise ValueError('works must be works or broken')
        ratings = ratings if isinstance(ratings, dict) else {}
        quick_actions = quick_actions if isinstance(quick_actions, list) else []
        with self._conn() as conn:
            cur = conn.execute(
                'INSERT INTO player_feedback '
                '(work_item_id, player, sentiment, works, notes, ratings, '
                'quick_actions, created_at) VALUES (?,?,?,?,?,?,?,?)',
                (work_item_id, player, sentiment, works, str(notes or '')[:4000],
                 json.dumps(ratings), json.dumps(quick_actions), time.time()))
            return int(cur.lastrowid)

    def list(self, work_item_id=None, player=None, limit=50):
        sql = 'SELECT * FROM player_feedback'
        clauses, args = [], []
        if work_item_id:
            clauses.append('work_item_id = ?')
            args.append(work_item_id)
        if player:
            clauses.append('player = ?')
            args.append(player)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC LIMIT ?'
        args.append(max(1, min(int(limit), 200)))
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            for key in ('ratings', 'quick_actions'):
                try:
                    item[key] = json.loads(item.get(key) or '{}')
                except (TypeError, ValueError):
                    item[key] = {} if key == 'ratings' else []
            out.append(item)
        return out

    def get_taste(self, player):
        with self._conn() as conn:
            row = conn.execute('SELECT * FROM player_taste WHERE player = ?',
                               (player,)).fetchone()
        if not row:
            return {'player': player, 'weights': {}, 'notes': '',
                    'updatedAt': None}
        item = dict(row)
        try:
            item['weights'] = json.loads(item.get('weights') or '{}')
        except (TypeError, ValueError):
            item['weights'] = {}
        item['updatedAt'] = item.pop('updated_at', None)
        return item

    def set_taste(self, player, weights, notes=''):
        weights = weights if isinstance(weights, dict) else {}
        clean = {}
        for key, value in weights.items():
            try:
                clean[str(key)[:60]] = max(0, min(100, int(value)))
            except (TypeError, ValueError):
                continue
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO player_taste (player, weights, notes, updated_at) '
                'VALUES (?,?,?,?) ON CONFLICT(player) DO UPDATE SET '
                'weights=excluded.weights, notes=excluded.notes, '
                'updated_at=excluded.updated_at',
                (player, json.dumps(clean), str(notes or '')[:4000], time.time()))
        return self.get_taste(player)
