"""External-eval ports: composite multi-signal trust gate + HALF_OPEN
circuit-breaker probe.

From the codebase eval (memory: project_external_eval_sep2026): the think tank's
approval/escalation gates were single-signal and its circuit breaker had no
recovery probe. Two sharp, contained ideas were pulled in:

  1. serve._jes_directory_score / _escalation_floor -- the DIRECTOR auto-approval
     path now blends Jev confidence with a per-kind error-history penalty and a
     per-kind risk FLOOR, so a safety-critical escalation kind (a human's own
     "blocked" verdict on a command/pipeline step) is never auto-approved, and a
     kind that Jev keeps failing on needs stronger confidence to win the human's
     delegation. Fail-closed invariant preserved: a classifier failure leaves the
     escalation pending and raises the bar next time.

  2. serve.is_model_circuit_broken / record_model_result -- the model circuit
     breaker now has a HALF_OPEN recovery probe: once the cooldown elapses, a
     single probe is allowed through and only a SUCCESS closes the circuit; a
     probe failure re-opens it with a fresh cooldown.

Hermetic: pure functions + a mocked _call_openrouter_decision_sync; no live Jev,
no network. The "no state mutation beyond the module counters" claim was
wrong: is_model_circuit_broken/record_model_result log model_circuit_broken/
model_circuit_recovered via a real `log_action` call, which -- unless DB_PATH
is redirected below -- writes into whatever real think_tank.db sits at serve.py's
default path. Found: this file alone added 58 real rows (mostly
`model_circuit_broken` for a fake model "m") to a live production think_tank.db
during a routine test run.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402  (sys.path insert above is the repo test convention)
import sim  # noqa: E402
import content  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-composite-trust-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# Composite director trust gate
# ---------------------------------------------------------------------------

def decision_payload(choice, confidence):
    """A fake /api/alpha/decisions response shaped like what _jev_choice expects."""
    return {'answers': {'q': {'choice': choice, 'confidence': confidence}}, 'usage': {'cost': 0.0}}


def _stub_grade(g):
    def _f(*_a, **_k):
        return g
    return _f


class PeerReviewWorkerPickerTests(unittest.TestCase):
    """serve._peer_review_pass raises HELP SIGNALS, never reports (2026-10-06):
    a watch over the real action_log, and when the evidence shows a worker
    genuinely out of line (an underperformer or a standout) a weighted-
    probability pick decides whether THAT watch raises a help signal to the
    worker's DIRECTOR. The pass NEVER files a report: a worker's productivity
    is tracked structurally (approvedCount, droppedCount, morale, the firing
    drop-off signal), so a bug-hunter deep in an investigation is never dinged
    and the reports ledger holds only genuine agent/player-written reviews.
    These tests pin the new contract: evidence gates (quiet/nominal village
    raises nothing), dedup/staleness coverage, the probability gate, the
    director destination, and that no report is ever written."""

    def setUp(self):
        # These tests share one global DB, so isolate each one: clear the
        # action_log rows (the evidence the picker reads) and the reports +
        # helpSignals lists so a prior test can't leak its ada/ben activity or
        # its raised signals into the next assertion.
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')
        state = serve.get_state_from_db()
        if state is not None:
            state['reports'] = []
            state.pop('helpSignals', None)
            serve.save_state_to_db(state)

    def _seed_roster_and_activity(self, now):
        state = {
            'agentRoster': [
                {'id': 'maya', 'name': 'Maya', 'isDirector': True, 'isAdmin': False},
                {'id': 'ada', 'name': 'Ada', 'director': 'maya'},
                {'id': 'ben', 'name': 'Ben', 'director': 'maya'},
            ],
            'agents': {'maya': {'id': 'maya'}, 'ada': {'id': 'ada'}, 'ben': {'id': 'ben'}},
            'reports': [],
        }
        serve.save_state_to_db(state)
        # Real action_log rows: ada does plenty of real work, ben does none --
        # a clear, real divergence for a watch to notice. ada = standout
        # (real 5 >= 5 and >= group max), ben = quiet (real 0 < 2).
        for _ in range(5):
            serve.log_action('ada', 'task_completed', {}, authorized=True)
        return state

    def _force_probability_gate(self):
        """The probability gate raises only when random.random() <
        PEER_REVIEW_FILE_PROBABILITY; force the 'raise this watch' branch."""
        return unittest.mock.patch.object(serve.random, 'random', return_value=0.0)

    def test_a_help_signal_goes_to_the_director_not_a_report(self):
        self._seed_roster_and_activity(time.time())
        with self._force_probability_gate(), \
             unittest.mock.patch.object(serve.random, 'choices',
                                        return_value=[{'id': 'ben', 'name': 'Ben', 'role': '',
                                                       'actions': 0, 'real': 0, 'last': None,
                                                       'already': False, 'director': 'maya'}]):
            serve._peer_review_loop_pass()
        saved = serve.get_state_from_db()
        self.assertEqual(saved.get('reports') or [], [],
                         'the machine never writes a report about productivity')
        signals = saved.get('helpSignals') or []
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]['aboutId'], 'ben')
        self.assertEqual(signals[0]['directorId'], 'maya',
                         'the signal goes to the worker\'s director, not a random peer')
        self.assertEqual(signals[0]['direction'], 'quiet')
        mailbox = (saved.get('agents') or {}).get('maya', {}).get('mailbox') or []
        self.assertTrue(any(m.get('kind') == 'help_signal' for m in mailbox),
                        'the director is told in-world')

    def test_a_standout_raises_an_overloaded_signal(self):
        # "Get someone help if they're carrying the load": a standout is a
        # HELP signal (offer help / reallocation), never a praise report.
        self._seed_roster_and_activity(time.time())
        with self._force_probability_gate(), \
             unittest.mock.patch.object(serve.random, 'choices',
                                        return_value=[{'id': 'ada', 'name': 'Ada', 'role': '',
                                                       'actions': 5, 'real': 5, 'last': None,
                                                       'already': False, 'director': 'maya'}]):
            serve._peer_review_loop_pass()
        saved = serve.get_state_from_db()
        signals = saved.get('helpSignals') or []
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]['aboutId'], 'ada')
        self.assertEqual(signals[0]['direction'], 'overloaded')

    def test_the_signal_is_probabilistic_not_a_fixed_metronome(self):
        # The old design filed a report EVERY cadence (fixed clock). Now the
        # probability gate means a given watch may raise nothing even when the
        # evidence is genuine. Patch random.random above the threshold.
        self._seed_roster_and_activity(time.time())
        with unittest.mock.patch.object(serve.random, 'random', return_value=0.99):
            n = serve._peer_review_loop_pass()
        self.assertEqual(n, 0, 'the probability gate must let a genuine watch pass without signaling')
        saved = serve.get_state_from_db()
        self.assertEqual(saved.get('helpSignals') or [], [])

    def test_the_probability_gate_still_signals_when_it_passes(self):
        # Same evidence, but the watch "happens" -- the signal must raise.
        self._seed_roster_and_activity(time.time())
        with self._force_probability_gate():
            n = serve._peer_review_loop_pass()
        self.assertEqual(n, 1, 'an evidence-backed watch that passes the gate must raise a signal')
        saved = serve.get_state_from_db()
        self.assertEqual(len(saved.get('helpSignals') or []), 1)

    def _seed_recent_signal(self, about_id, ts_ms=None, direction='quiet'):
        """Append a help signal about `about_id` to the stored state. ts_ms
        defaults to 'now' (fresh inside the staleness window)."""
        state = serve.get_state_from_db()
        signals = state.setdefault('helpSignals', [])
        signals.append({
            'id': f'help-seed-{about_id}',
            'aboutId': about_id, 'directorId': 'maya', 'direction': direction,
            'real': 0, 'actions': 0,
            'ts': int(time.time() * 1000) if ts_ms is None else ts_ms,
            'text': 'seeded for the dedup test',
        })
        state['helpSignals'] = signals
        serve.save_state_to_db(state)

    def test_does_not_re_signal_a_worker_within_the_stale_window(self):
        # Both workers covered by a FRESH signal -> the loop raises nothing.
        self._seed_roster_and_activity(time.time())
        self._seed_recent_signal('ada')
        self._seed_recent_signal('ben')
        with self._force_probability_gate():
            n = serve._peer_review_loop_pass()
        self.assertEqual(n, 0, 'an all-covered roster must not raise another signal')
        saved = serve.get_state_from_db()
        self.assertEqual(len(saved.get('helpSignals') or []), 2)

    def test_a_covered_worker_is_skipped_while_a_fresh_peer_is_signaled(self):
        # ada already fresh-covered; ben is not -> the pool is ben ONLY and he
        # gets the signal.
        self._seed_roster_and_activity(time.time())
        self._seed_recent_signal('ada')
        with self._force_probability_gate():
            serve._peer_review_loop_pass()
        saved = serve.get_state_from_db()
        signals = saved.get('helpSignals') or []
        self.assertEqual(len(signals), 2)
        self.assertEqual(signals[-1]['aboutId'], 'ben',
                         'the fresh candidate, not the already-covered worker, gets the signal')

    def test_a_stale_signal_allows_a_worker_back_into_the_pool(self):
        # Both workers' only signals are OLDER than the staleness window, so
        # both are fresh again and a new signal may be raised.
        self._seed_roster_and_activity(time.time())
        stale_ms = int((time.time() - serve.PEER_REVIEW_SIGNAL_STALE_S - 60) * 1000)
        self._seed_recent_signal('ada', ts_ms=stale_ms)
        self._seed_recent_signal('ben', ts_ms=stale_ms)
        with self._force_probability_gate():
            serve._peer_review_loop_pass()
        saved = serve.get_state_from_db()
        self.assertEqual(len(saved.get('helpSignals') or []), 3,
                         'staleness must let a worker back into the pool for a fresh signal')

    def test_a_quiet_village_raises_no_signal_on_a_baseline(self):
        # EVIDENCE GATE: a village where NO ONE has done any real work is just
        # idle -- nobody is underperforming (all equally quiet) and nobody is a
        # standout, so there is nothing legitimate to signal. No signal must be
        # raised.
        state = {
            'agentRoster': [
                {'id': 'maya', 'name': 'Maya', 'isDirector': True, 'isAdmin': False},
                {'id': 'ada', 'name': 'Ada', 'director': 'maya'},
                {'id': 'ben', 'name': 'Ben', 'director': 'maya'},
            ],
            'agents': {'maya': {'id': 'maya'}, 'ada': {'id': 'ada'}, 'ben': {'id': 'ben'}},
            'reports': [],
        }
        serve.save_state_to_db(state)
        # NOTE: deliberately NO real action_log rows -- a fully quiet village.
        with self._force_probability_gate():
            n = serve._peer_review_loop_pass()
        self.assertEqual(n, 0, 'a fully idle village must not raise a help signal')
        saved = serve.get_state_from_db()
        self.assertEqual(saved.get('helpSignals') or [], [])

    def test_a_nominal_village_without_divergence_raises_no_signal(self):
        # EVIDENCE GATE (target): even when the village is active, a worker at
        # only nominal output (real in [2,4], comparable to peers) is NOT a
        # signal. Only a genuine underperformer (<2 while peers demonstrably
        # worked) or a standout (>=5, the group max) qualifies.
        state = {
            'agentRoster': [
                {'id': 'maya', 'name': 'Maya', 'isDirector': True, 'isAdmin': False},
                {'id': 'ada', 'name': 'Ada', 'director': 'maya'},
                {'id': 'ben', 'name': 'Ben', 'director': 'maya'},
            ],
            'agents': {'maya': {'id': 'maya'}, 'ada': {'id': 'ada'}, 'ben': {'id': 'ben'}},
            'reports': [],
        }
        serve.save_state_to_db(state)
        # Both workers at nominal, comparable real work (2 and 3) -- active but
        # with NO genuine divergence to signal.
        for _ in range(2):
            serve.log_action('ada', 'task_completed', {}, authorized=True)
        for _ in range(3):
            serve.log_action('ben', 'task_completed', {}, authorized=True)
        with self._force_probability_gate():
            n = serve._peer_review_loop_pass()
        self.assertEqual(n, 0, 'comparable nominal output is not evidence for a help signal')
        saved = serve.get_state_from_db()
        self.assertEqual(saved.get('helpSignals') or [], [])


