"""Tests for the weekly diff-against-expectation review (world/serve.py).

The weekly review is the Bot Ops accountability loop for the think tank: every
UTC week it reads GROUND TRUTH (the action_log + decision_tape tables) -- never
an agent's self-reported summary -- and renders a digest with week-over-week
deltas, so the player reviews what actually happened vs. what was asked. One row
per UTC week (dedup key = period_start).

Because log_action / _append_decision_tape stamp time.time() internally, tests
seed the two tables via direct SQL INSERT with explicit `ts` so the review
windows are fully hermetic.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve


WEEK = 7 * 86400


class WeeklyReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-weekly-review-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_action(self, agent_id, action, ts):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO action_log (agent_id, action, details, authorized, ts) '
                'VALUES (?, ?, ?, ?, ?)',
                (agent_id, action, None, 1, ts),
            )

    def _seed_decision(self, kind, ok, confidence, cost, ts):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO decision_tape '
                '(ts, kind, model, prompt, criteria, choice, confidence, cost, raw, ok) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (ts, kind, 'test-model', 'p', 'c', 'x', confidence, cost, '{}', int(ok)),
            )

    def _seed_library_write(self, agent_id, path, source, ts):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO action_log (agent_id, action, details, authorized, ts) '
                'VALUES (?, ?, ?, ?, ?)',
                (agent_id, 'library_write', json.dumps({'path': path, 'source': source}), 1, ts),
            )

    def test_period_start_is_the_utc_week_boundary(self):
        now = 2000000.0  # a fixed epoch (well past the 1970 epoch)
        start = serve._weekly_period_start(now)
        self.assertEqual(start, int((now - (now % WEEK)) * 1000))
        # Round trip: start lies exactly on a multiple of the week.
        self.assertEqual(start % (WEEK * 1000), 0)

    def test_build_weekly_review_reads_ground_truth_and_deltas(self):
        now = 2000000.0
        since = now - WEEK
        prior_since = since - WEEK
        # Current window (exclusive > since, inclusive <= now).
        self._seed_action('ada', 'task_completed', now - 1000)     # progress -> shipped
        self._seed_action('ben', 'story_vetoed', now - 2000)       # ceremony
        self._seed_decision('browse', True, 0.9, 0.01, now - 500)
        # Prior window (exclusive > prior_since, inclusive <= prior_until).
        self._seed_action('ada', 'task_completed', since - 1000)
        self._seed_decision('browse', False, 0.5, 0.5, since - 1000)

        review = serve._build_weekly_review(now=now)
        self.assertIsNotNone(review)
        d = review['digest']
        self.assertEqual(d['period_start_ms'], serve._weekly_period_start(now))
        self.assertEqual(d['total_actions'], 2)
        self.assertEqual(d['shipped_actions'], 1, 'only task_completed ships')
        self.assertEqual(d['ceremony_actions'], 1, 'story_vetoed is ceremony')
        self.assertEqual(d['decisions'], 1)
        self.assertEqual(d['decisions_ok'], 1)
        self.assertEqual(d['decision_cost_usd'], 0.01)
        self.assertEqual(d['decision_kinds']['browse'], {'n': 1, 'ok': 1})
        self.assertEqual(d['per_agent']['ada'], {'actions': 1, 'shipped': 1})
        self.assertEqual(d['per_agent']['ben'], {'actions': 1, 'shipped': 0})
        # Prior week had 1 action (shipped) + 1 decision (ok=0, cost 0.5).
        deltas = d['deltas']
        self.assertEqual(deltas['actions'], 1)
        self.assertEqual(deltas['shipped'], 0)
        self.assertEqual(deltas['ceremony'], 1)
        self.assertEqual(deltas['decisions'], 0)
        self.assertEqual(deltas['decision_cost_usd'], round(0.01 - 0.5, 4))
        # The markdown report is present and carries the ground-truth framing.
        self.assertIn('# Weekly Review', review['markdown'])
        self.assertIn('Ground truth', review['markdown'])

    def test_build_weekly_review_returns_none_when_nothing_to_review(self):
        self.assertIsNone(serve._build_weekly_review(now=2000000.0))

    def test_build_weekly_review_reports_library_write_provenance(self):
        # Firsthand work goes to the trusted tree; external (web-browsed)
        # content is gated into pending_review/. A write marked external that
        # landed in the trusted tree is the memory-poisoning signal.
        now = 2000000.0
        self._seed_library_write('ada', 'shared/notes.md', 'firsthand', now - 100)
        self._seed_library_write('ben', 'pending_review/x.md', 'external', now - 100)
        self._seed_library_write('ben', 'wiki/cat/p.md', 'external', now - 100)
        review = serve._build_weekly_review(now=now)
        self.assertIsNotNone(review)
        prov = review['digest']['provenance']
        self.assertEqual(prov['library_writes'], 3)
        self.assertEqual(prov['by_source'], {'external': 2, 'firsthand': 1})
        self.assertEqual(prov['external_in_trusted'], ['wiki/cat/p.md'])
        self.assertEqual(prov['unvetted_pending_n'], 0)
        self.assertIn('## Knowledge provenance audit', review['markdown'])
        self.assertIn('Memory-poisoning signal', review['markdown'])

    def test_build_weekly_review_lists_unvetted_pending_content_alone(self):
        # The audit's real job: a quiet week with only external content parked
        # in pending_review/ must still produce a review (the early-return
        # "nothing to review" check includes the provenance findings).
        now = 2000000.0
        pending = os.path.join(self.tmp, 'library', 'pending_review', 'shared')
        os.makedirs(pending)
        with open(os.path.join(pending, 'scraped.md'), 'w') as f:
            f.write('# scraped from the web\n')
        review = serve._build_weekly_review(now=now)
        self.assertIsNotNone(review)
        prov = review['digest']['provenance']
        self.assertEqual(prov['unvetted_pending'], ['pending_review/shared/scraped.md'])
        self.assertEqual(prov['library_writes'], 0)
        self.assertIn('Unvetted external content', review['markdown'])
        self.assertIn('`pending_review/shared/scraped.md`', review['markdown'])

    def test_provenance_audit_is_best_effort(self):
        # The memory audit must never raise: a broken DB read or an
        # unreadable pending_review tree both return the default dict.
        now = 2000000.0
        with unittest.mock.patch.object(os.path, 'isdir', return_value=True), \
             unittest.mock.patch.object(os, 'walk', side_effect=OSError('fs unreachable')):
            prov = serve._library_provenance_audit(now, WEEK)
        self.assertEqual(prov['unvetted_pending'], [])
        self.assertEqual(prov['writes'], 0)
        with unittest.mock.patch.object(serve, '_db', side_effect=RuntimeError('db unreachable')):
            prov = serve._library_provenance_audit(now, WEEK)
        self.assertEqual(prov['writes'], 0)
        self.assertEqual(prov['external_in_trusted'], [])

    def test_generate_weekly_review_is_idempotent_per_week(self):
        now = 2000000.0
        self._seed_action('ada', 'task_completed', now - 1000)
        self._seed_decision('browse', True, 0.9, 0.01, now - 500)
        first = serve._generate_weekly_review(now=now)
        self.assertIsNotNone(first)
        second = serve._generate_weekly_review(now=now)
        self.assertIsNotNone(second)
        with serve._db() as conn:
            rows = conn.execute(
                'SELECT count(*) FROM weekly_reviews WHERE period_start = ?',
                (serve._weekly_period_start(now),),
            ).fetchone()
        self.assertEqual(rows[0], 1, 'one row per UTC week, never two')

    def test_generate_weekly_review_persists_digest_and_markdown(self):
        now = 2000000.0
        self._seed_action('ada', 'task_completed', now - 1000)
        review = serve._generate_weekly_review(now=now)
        with serve._db() as conn:
            row = conn.execute(
                'SELECT digest, markdown FROM weekly_reviews '
                'WHERE period_start = ?',
                (review['digest']['period_start_ms'],),
            ).fetchone()
        self.assertIsNotNone(row)
        stored_digest = json.loads(row[0])
        self.assertEqual(stored_digest['total_actions'], 1)
        self.assertEqual(row[1], review['markdown'])

    def test_generate_weekly_review_never_raises_on_empty(self):
        # Empty window -> None, and it must NOT insert a row or raise.
        self.assertIsNone(serve._generate_weekly_review(now=2000000.0))
        with serve._db() as conn:
            n = conn.execute('SELECT count(*) FROM weekly_reviews').fetchone()[0]
        self.assertEqual(n, 0)


class WeeklyReviewEndpoint(unittest.TestCase):
    """GET /api/reviews (+ ?generate=1). TestClient without a context manager so
    the app lifespan never runs; auth bypassed exactly like the rest of
    test_serve.py."""
    from fastapi.testclient import TestClient

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-weekly-review-ep-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_get_reviews_returns_persisted_reports_newest_first(self):
        now = time.time()
        with serve._db() as conn:
            for ts in (now - 1000, now - 2000):
                conn.execute(
                    'INSERT INTO action_log (agent_id, action, details, authorized, ts) '
                    'VALUES (?, ?, ?, ?, ?)', ('ada', 'task_completed', None, 1, ts))
        serve._generate_weekly_review()
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = self.TestClient(serve.app)
            r = c.get('/api/reviews')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['reviews'][0]['periodStartMs'],
                         serve._weekly_period_start(now))
        self.assertEqual(body['reviews'][0]['digest']['total_actions'], 2)
        self.assertIn('# Weekly Review', body['reviews'][0]['markdown'])

    def test_get_reviews_generate_forces_a_fresh_build(self):
        now = time.time()
        # No review yet -- the table is empty.
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = self.TestClient(serve.app)
            r = c.get('/api/reviews')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['count'], 0)
        # Seed ground truth, then force-generation builds the current week live.
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO action_log (agent_id, action, details, authorized, ts) '
                'VALUES (?, ?, ?, ?, ?)', ('ada', 'task_completed', None, 1, now - 1000))
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = self.TestClient(serve.app)
            r = c.get('/api/reviews?generate=1')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['reviews'][0]['digest']['total_actions'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
