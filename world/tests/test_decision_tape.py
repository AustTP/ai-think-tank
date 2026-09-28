"""Decision tape (2026-09-24): every Jev decision the chokepoint makes is recorded
as a raw model-facing row -- prompt, candidates, parsed choice/confidence/cost,
and the full response -- before the caller discards them. Complements the state-side
audit (action_log + passport hash-chain) with the observed-answer side.

Scope (user's call): raw model-facing only. Implemented entirely at the chokepoint
`_call_openrouter_decision_sync`; zero behavior change to call sites. Outcomes stay
in action_log/passport; the tape is correlatable to those by ts + agent_id.

Hermetic: temp DB + patched `_urlopen_with_resilience` (the chokepoint's only network
touch). No live Jev, no real network.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402  (sys.path insert above is the repo test convention)


def _jev_response(choice='fire', confidence=0.9, cost=0.0001):
    """A fake /api/alpha/decisions response shaped like what _jev_choice expects."""
    return {'answers': {'q': {'choice': choice, 'confidence': confidence}},
            'usage': {'cost': cost}}


class DecisionTapeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='decision-tape-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            VILLAGE_DIR=self.tmp,
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rows(self, conn):
        return conn.execute(
            'SELECT ts, kind, model, prompt, criteria, choice, confidence, cost, raw, ok '
            'FROM decision_tape ORDER BY id'
        ).fetchall()

    def test_success_writes_parsed_tape_row(self):
        questions = {'choice': {'type': 'choice',
                                'instructions': 'Hire or fire this worker?',
                                'criteria': {'fire': 'drop them', 'keep': 'retain'}}}
        with unittest.mock.patch('serve._urlopen_with_resilience',
                                 return_value=json.dumps(_jev_response()).encode()):
            data = serve._call_openrouter_decision_sync('typesafe/jev-1.13', {}, questions)
        self.assertEqual(data, _jev_response())
        with serve._db() as conn:
            rows = self._rows(conn)
        self.assertEqual(len(rows), 1)
        _ts, kind, model, prompt, criteria, choice, conf, cost, raw, ok = rows[0]
        self.assertEqual(kind, 'personnel')
        self.assertEqual(model, 'typesafe/jev-1.13')
        self.assertIn('Hire or fire', prompt)
        self.assertIn('fire', json.loads(criteria))
        self.assertEqual(choice, 'fire')
        self.assertEqual(conf, 0.9)
        self.assertEqual(cost, 0.0001)
        self.assertEqual(json.loads(raw), _jev_response())
        self.assertEqual(ok, 1)

    def test_failure_writes_ok0_row_and_re_raises(self):
        questions = {'choice': {'type': 'choice',
                                'instructions': 'Grade this deliverable 0-10',
                                'criteria': {'idx': 'the grade'}}}
        with unittest.mock.patch('serve._urlopen_with_resilience',
                                 side_effect=RuntimeError('jev down')):
            with self.assertRaises(RuntimeError):
                serve._call_openrouter_decision_sync('typesafe/jev-1.13', {}, questions)
        with serve._db() as conn:
            rows = self._rows(conn)
        self.assertEqual(len(rows), 1)
        _ts, kind, _model, _prompt, _criteria, choice, conf, cost, raw, ok = rows[0]
        self.assertEqual(kind, 'grade')
        self.assertIsNone(choice)
        self.assertIsNone(conf)
        self.assertIsNone(cost)
        self.assertEqual(json.loads(raw).get('error'), 'decision call raised')
        self.assertEqual(ok, 0)

    def test_unrecognized_prompt_defaults_to_other(self):
        questions = {'choice': {'type': 'choice', 'instructions': 'Completely novel ask',
                                'criteria': {'a': 'x'}}}
        with unittest.mock.patch('serve._urlopen_with_resilience',
                                 return_value=json.dumps(_jev_response()).encode()):
            serve._call_openrouter_decision_sync('typesafe/jev-1.13', {}, questions)
        with serve._db() as conn:
            kind = conn.execute('SELECT kind FROM decision_tape').fetchone()[0]
        self.assertEqual(kind, 'other')

    def test_tape_write_failure_does_not_affect_caller(self):
        # A tape insert that fails (here: drop the table first) must not break the
        # decision call -- the decision has already happened, the tape is best-effort.
        with serve._db() as conn:
            conn.execute('DROP TABLE decision_tape')
        with unittest.mock.patch('serve._urlopen_with_resilience',
                                 return_value=json.dumps(_jev_response()).encode()):
            data = serve._call_openrouter_decision_sync(
                'typesafe/jev-1.13', {},
                {'choice': {'type': 'choice', 'instructions': 'accept or reject',
                            'criteria': {'accept': 'yes'}}})
        self.assertEqual(data, _jev_response())


class DecisionTapeFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='decision-tape-feed-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            VILLAGE_DIR=self.tmp,
        )
        self._cm.start()
        serve.init_db()
        # Seed two rows of distinct kind/confidence directly.
        with serve._db() as conn:
            now = 1000.0
            for i, (kind, choice, conf) in enumerate([
                    ('personnel', 'fire', 0.5),
                    ('escalation', 'approve', 0.95),
            ]):
                conn.execute(
                    'INSERT INTO decision_tape (ts, kind, model, prompt, criteria, choice, confidence, cost, raw, ok) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (now + i, kind, 'typesafe/jev-1.13', 'p', '{}', choice, conf, 0.0, '{}', 1))
        self.routes = {r.path: r for r in serve.app.routes if hasattr(r, 'path')}

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_feed_filters_by_kind(self):
        resp = _call_feed(self.routes['/api/decisions'], kind='personnel')
        entries = resp['entries']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['kind'], 'personnel')
        self.assertEqual(entries[0]['choice'], 'fire')

    def test_feed_filters_confidence_cap(self):
        # min_conf=0.7 -> only decisions AT OR BELOW 0.7 (the "unsure but acted" case).
        resp = _call_feed(self.routes['/api/decisions'], min_conf=0.7)
        entries = resp['entries']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['confidence'], 0.5)

    def test_feed_newest_first_and_capped(self):
        resp = _call_feed(self.routes['/api/decisions'], limit=1)
        self.assertEqual(len(resp['entries']), 1)
        self.assertEqual(resp['entries'][0]['confidence'], 0.95)


def _call_feed(route, **params):
    # FastAPI's TestClient would be the natural home; this slim helper drives the
    # endpoint function directly (awaiting async ones), mirroring how other hermetic
    # tests avoid a server. Uses asyncio.run (robust to a prior test having closed
    # the main-thread event loop) rather than the deprecated get_event_loop().
    import asyncio
    import inspect
    sig = inspect.signature(route.endpoint)
    kwargs = {k: v for k, v in params.items() if k in sig.parameters}
    from fastapi.responses import JSONResponse
    res = route.endpoint(**kwargs)
    if inspect.iscoroutine(res):
        res = asyncio.run(res)
    if isinstance(res, JSONResponse):
        return json.loads(res.body)
    return res


if __name__ == '__main__':
    unittest.main(verbosity=2)