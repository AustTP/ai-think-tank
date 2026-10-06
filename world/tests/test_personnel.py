"""Tests for the personnel-review hardening (2026-10-06):

1. Strike window: a serious/severe negative review counts toward the two-strike
   firing bar only inside REPORT_STRIKE_WINDOW_MS. A lone negative no longer
   hangs over an employee forever; minor/major reports never count as strikes.
2. Fired-employee reentry blocklist: a fired identity can never be hired back.
3. Severity classified at filing, and the player is only notified when the
   evidence crosses a real bar (severe, or the second strike landing).
4. Reviewer independence: no agent may read any employee's reports/ directory.

Hermetic: the module patches serve's paths to a temp dir and never makes a
real model call (deterministic injected deciders + chooser).
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
import sim

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-personnel-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _seed(**over):
    now = int(time.time() * 1000)
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': [
            {'id': 'faye', 'name': 'Faye', 'role': 'Control Room', 'isAdmin': True},
            {'id': 'nora', 'name': 'Nora', 'role': 'Personnel', 'isDirector': True},
            {'id': 'ada', 'name': 'Ada', 'role': 'Research', 'model': 'small', 'director': 'faye'},
            {'id': 'ben', 'name': 'Ben', 'role': 'Banking', 'model': 'small', 'director': 'faye'},
        ],
        'agents': {
            'faye': {'id': 'faye', 'x': 0, 'y': 0, 'busy': False, 'task': None,
                     'inRoom': None, 'offDuty': False, 'dir': 'south', 'path': [],
                     'approvedCount': 0, 'droppedCount': 0, 'hiredAt': 0,
                     'visible': True, 'name': 'Faye', 'role': 'Control Room'},
            'nora': {'id': 'nora', 'x': 100, 'y': 100, 'busy': False, 'task': None,
                     'inRoom': None, 'offDuty': False, 'dir': 'south', 'path': [],
                     'approvedCount': 0, 'droppedCount': 0, 'hiredAt': 0,
                     'visible': True, 'name': 'Nora', 'role': 'Personnel'},
            'ada': {'id': 'ada', 'x': 200, 'y': 200, 'busy': False, 'task': None,
                    'inRoom': None, 'offDuty': False, 'dir': 'south', 'path': [],
                    'approvedCount': 0, 'droppedCount': 0, 'hiredAt': now,
                    'visible': True, 'name': 'Ada', 'role': 'Research'},
            'ben': {'id': 'ben', 'x': 300, 'y': 300, 'busy': False, 'task': None,
                    'inRoom': None, 'offDuty': False, 'dir': 'south', 'path': [],
                    'approvedCount': 0, 'droppedCount': 0, 'hiredAt': now,
                    'visible': True, 'name': 'Ben', 'role': 'Banking'},
        },
        'reports': [],
        'workQueue': [],
        'lastHireAt': 0,
        'lastFiringReviewAt': 0,
    }
    state.update(over)
    return state


class StrikeSeverity(unittest.TestCase):
    """Which report severities count as firing strikes, and how the grace
    window keeps a lone negative from hanging over an employee forever."""

    def test_is_strike_severity_excludes_major_and_minor(self):
        self.assertTrue(sim._is_strike_severity('severe'))
        self.assertTrue(sim._is_strike_severity('serious'))
        self.assertFalse(sim._is_strike_severity('major'))
        self.assertFalse(sim._is_strike_severity('minor'))
        self.assertFalse(sim._is_strike_severity(''))

    def test_report_strikes_counts_only_fresh_serious_or_severe(self):
        now = 10 ** 12
        state = _seed(reports=[
            {'aboutId': 'ben', 'fromId': 'ada', 'severity': 'serious', 'ts': now - 1000},
            {'aboutId': 'ben', 'fromId': 'nora', 'severity': 'severe', 'ts': now},
            {'aboutId': 'ben', 'fromId': 'faye', 'severity': 'major', 'ts': now},
            {'aboutId': 'ben', 'fromId': 'faye', 'severity': 'minor', 'ts': now},
        ])
        strikes = sim._report_strikes(state, 'ben', now)
        self.assertEqual(strikes['count'], 2)
        self.assertEqual(strikes['severe'], 1)

    def test_stale_report_expires_out_of_the_window(self):
        now = 10 ** 12
        old = {'aboutId': 'ben', 'fromId': 'ada', 'severity': 'serious',
               'ts': now - sim.REPORT_STRIKE_WINDOW_MS - 1}
        self.assertEqual(sim._report_strikes(_seed(reports=[old]), 'ben', now)['count'], 0)
        # Exactly at the window boundary it still counts.
        edge = {'aboutId': 'ben', 'fromId': 'ada', 'severity': 'serious',
                'ts': now - sim.REPORT_STRIKE_WINDOW_MS}
        self.assertEqual(sim._report_strikes(_seed(reports=[edge]), 'ben', now)['count'], 1)

    def test_report_without_ts_counts_as_fresh(self):
        # State-seeded / pre-window reports carry no timestamp; a hard fire must
        # never slip through a missing ts, so they count as fresh.
        r = {'aboutId': 'ben', 'fromId': 'ada', 'severity': 'severe'}
        self.assertEqual(sim._report_strikes(_seed(reports=[r]), 'ben', 10 ** 12)['count'], 1)

    def test_lone_serious_review_is_not_a_firing_signal(self):
        now = 10 ** 12
        state = _seed(reports=[{'aboutId': 'ben', 'fromId': 'ada', 'severity': 'serious', 'ts': now}])
        self.assertFalse(sim._has_firing_signal(state, 'ben', now),
                         'a single negative review must not convene a firing review')

    def test_two_fresh_strikes_are_a_firing_signal(self):
        now = 10 ** 12
        state = _seed(reports=[
            {'aboutId': 'ben', 'fromId': 'ada', 'severity': 'serious', 'ts': now},
            {'aboutId': 'ben', 'fromId': 'nora', 'severity': 'serious', 'ts': now},
        ])
        self.assertTrue(sim._has_firing_signal(state, 'ben', now))

    def test_two_majors_are_not_a_firing_signal(self):
        now = 10 ** 12
        state = _seed(reports=[
            {'aboutId': 'ben', 'fromId': 'ada', 'severity': 'major', 'ts': now},
            {'aboutId': 'ben', 'fromId': 'nora', 'severity': 'major', 'ts': now},
        ])
        self.assertFalse(sim._has_firing_signal(state, 'ben', now),
                         'small things of no harm must never snowball into a firing')

    def test_stale_strike_expires_out_of_the_signal(self):
        now = 10 ** 12
        state = _seed(reports=[
            {'aboutId': 'ben', 'fromId': 'ada', 'severity': 'serious',
             'ts': now - sim.REPORT_STRIKE_WINDOW_MS - 1},
            {'aboutId': 'ben', 'fromId': 'nora', 'severity': 'serious',
             'ts': now - sim.REPORT_STRIKE_WINDOW_MS - 2},
        ])
        self.assertFalse(sim._has_firing_signal(state, 'ben', now),
                         'two honest mistakes spaced beyond the window never fire anyone')

    def test_severe_alone_is_a_firing_signal(self):
        now = 10 ** 12
        state = _seed(reports=[{'aboutId': 'ben', 'fromId': 'ada', 'severity': 'severe', 'ts': now}])
        self.assertTrue(sim._has_firing_signal(state, 'ben', now))

    def test_dropoff_remains_a_signal_independent_of_reports(self):
        state = _seed()
        state['agents']['ben'].update({'droppedCount': 10, 'approvedCount': 0})
        self.assertTrue(sim._has_firing_signal(state, 'ben', 10 ** 12))

    def test_consultation_defers_a_single_strike(self):
        now = 10 ** 12
        state = _seed(reports=[{'aboutId': 'ben', 'fromId': 'ada', 'severity': 'serious', 'ts': now}])
        blocks, _info = sim._consultation_blocks_firing(state, 'ben', now)
        self.assertTrue(blocks, 'one strike is not enough to justify a fire')

    def test_consultation_allows_two_fresh_strikes(self):
        now = 10 ** 12
        state = _seed(reports=[
            {'aboutId': 'ben', 'fromId': 'ada', 'severity': 'serious', 'ts': now},
            {'aboutId': 'ben', 'fromId': 'nora', 'severity': 'serious', 'ts': now},
        ])
        blocks, _info = sim._consultation_blocks_firing(state, 'ben', now)
        self.assertFalse(blocks, 'two independent strikes clear the corroboration bar')

    def test_consultation_allows_severe_alone(self):
        now = 10 ** 12
        state = _seed(reports=[{'aboutId': 'ben', 'fromId': 'ada', 'severity': 'severe', 'ts': now}])
        blocks, _info = sim._consultation_blocks_firing(state, 'ben', now)
        self.assertFalse(blocks, 'a severe report alone justifies a firing review')

    def test_consultation_stale_strikes_defer(self):
        now = 10 ** 12
        state = _seed(reports=[
            {'aboutId': 'ben', 'fromId': 'ada', 'severity': 'serious',
             'ts': now - sim.REPORT_STRIKE_WINDOW_MS - 1},
            {'aboutId': 'ben', 'fromId': 'nora', 'severity': 'serious',
             'ts': now - sim.REPORT_STRIKE_WINDOW_MS - 2},
        ])
        blocks, _info = sim._consultation_blocks_firing(state, 'ben', now)
        self.assertTrue(blocks, 'expired evidence cannot corroborate a firing')

    def test_active_collaborator_still_blocks(self):
        now = 10 ** 12
        state = _seed(reports=[
            {'aboutId': 'ben', 'fromId': 'ada', 'severity': 'severe', 'ts': now},
        ])
        state['agents']['ada']['pairWith'] = 'ben'
        blocks, _info = sim._consultation_blocks_firing(state, 'ben', now)
        self.assertTrue(blocks, 'an active collaborator defers even a severe fire')


class FiredBlocklist(unittest.TestCase):
    """A fired employee can never reenter the think tank."""

    def _fire_ben(self, state, now):
        pending = {'reviewer1Id': 'faye', 'reviewer2Id': 'nora',
                   'candidateId': 'ben', 'at': now}
        # Two fresh strikes so the consultation guardrail doesn't defer.
        state['reports'] = [
            {'aboutId': 'ben', 'fromId': 'ada', 'severity': 'serious', 'ts': now},
            {'aboutId': 'ben', 'fromId': 'nora', 'severity': 'serious', 'ts': now},
        ]
        sim._resolve_firing_review(state, pending, now, decider=lambda *a, **k: 'fire')
        return state

    def test_fire_records_a_durable_blocklist_entry(self):
        now = 10 ** 12
        state = self._fire_ben(_seed(), now)
        self.assertNotIn('ben', state['agents'], 'fire removes the agent')
        self.assertNotIn('ben', [d['id'] for d in state['agentRoster']])
        entry = (state.get('firedAgents') or {}).get('ben')
        self.assertIsNotNone(entry, 'the fired agent is recorded permanently')
        self.assertEqual(entry.get('name'), 'Ben')
        self.assertEqual(entry.get('at'), now)
        self.assertTrue(sim._is_fired(state, 'ben'))
        self.assertTrue(sim._is_fired(state, 'ben', 'Ben'))

    def test_is_fired_matches_by_id_or_name_case_insensitively(self):
        state = _seed(firedAgents={'zoe': {'name': 'Zoe', 'at': 1}})
        self.assertTrue(sim._is_fired(state, 'zoe'))
        self.assertTrue(sim._is_fired(state, 'nope', 'zoe'))
        self.assertTrue(sim._is_fired(state, 'nope', 'ZOE'))
        self.assertFalse(sim._is_fired(state, 'ada'))
        self.assertFalse(sim._is_fired(state, 'nope', 'ada'))
        self.assertFalse(sim._is_fired(state, 'nope', 'unrelated'))

    def test_complete_auto_hire_refuses_a_fired_identity(self):
        state = _seed(firedAgents={'ben': {'name': 'Ben', 'at': 1}})
        # Post-fire state: ben is gone from the roster and the live map, and
        # _usedNames must not already reserve 'ben' -- the blocklist is the
        # LAST line of defense, tested in isolation from the name reservation.
        state['agentRoster'] = [d for d in state['agentRoster'] if d['id'] != 'ben']
        state['agents'].pop('ben', None)
        state['_usedNames'] = []
        state['_pendingHire'] = {'adminId': 'faye', 'helpForId': 'ada',
                                 'helpForName': 'Ada', 'adminName': 'Faye',
                                 'directorId': 'faye'}
        grid, _doors = sim._load_outdoor_geometry()
        # Even if the generative chooser tries to rehire the SAME name, the gate
        # must refuse -- a fired employee can never come back.
        with unittest.mock.patch('sim._hire_name_chooser', return_value='Ben'):
            result = sim._complete_auto_hire(state, state['_pendingHire'], grid, 10 ** 12)
        self.assertIsNone(result, 'a fired identity must never be hired again')
        self.assertIsNone(state.get('_pendingHire'), 'the forbidden hire is dropped')
        self.assertNotIn('ben', state.get('agents', {}))
        self.assertNotIn('ben', [d['id'] for d in state.get('agentRoster', [])])

    def test_non_fired_hire_still_succeeds(self):
        state = _seed(firedAgents={'zoe': {'name': 'Zoe', 'at': 1}})
        state['_usedNames'] = []
        state['_pendingHire'] = {'adminId': 'faye', 'helpForId': 'ada',
                                 'helpForName': 'Ada', 'adminName': 'Faye',
                                 'directorId': 'faye'}
        grid, _doors = sim._load_outdoor_geometry()
        with unittest.mock.patch('sim._hire_name_chooser', return_value='Rowan'):
            result = sim._complete_auto_hire(state, state['_pendingHire'], grid, 10 ** 12)
        self.assertEqual(result, 'rowan', 'an unrelated hire proceeds normally')
        self.assertIn('rowan', state['agents'])
        self.assertIsNone(state.get('_pendingHire'))


class SeverityClassifier(unittest.TestCase):
    def test_severe_keywords(self):
        self.assertEqual(serve._classify_report_severity('sabotaged the pipeline', ''), 'severe')
        self.assertEqual(serve._classify_report_severity('', 'fabricated data in the report'), 'severe')
        self.assertEqual(serve._classify_report_severity('stole credentials', ''), 'severe')

    def test_serious_keywords(self):
        self.assertEqual(serve._classify_report_severity('missed the deadline', ''), 'serious')
        self.assertEqual(serve._classify_report_severity('unresponsive to the team', ''), 'serious')
        self.assertEqual(serve._classify_report_severity('dropped a critical handoff', ''), 'serious')

    def test_major_keywords(self):
        self.assertEqual(serve._classify_report_severity('miscommunicated the plan', ''), 'major')
        self.assertEqual(serve._classify_report_severity('', 'unclear and confusing notes'), 'major')

    def test_defaults_minor_for_no_harm(self):
        self.assertEqual(serve._classify_report_severity('fine work today', ''), 'minor')
        self.assertEqual(serve._classify_report_severity('', ''), 'minor')

    def test_most_serious_signal_wins(self):
        self.assertEqual(serve._classify_report_severity('miscommunication led to a missed deadline', ''),
                         'serious')


class ReportFiling(unittest.TestCase):
    """post_report classifies severity at filing and only notifies the player
    when the evidence crosses a real bar."""

    def setUp(self):
        state = {
            'sim': {'owner': 'server'},
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada', 'director': 'faye'},
                {'id': 'ben', 'name': 'Ben', 'director': 'faye'},
                {'id': 'zoe', 'name': 'Zoe', 'director': 'faye'},
            ],
            'agents': {'ada': {'id': 'ada'}, 'ben': {'id': 'ben'}, 'zoe': {'id': 'zoe'}},
            'reports': [], 'workQueue': [],
        }
        serve.save_state_to_db(state)
        self._key_patch = unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True)
        self._key_patch.start()

    def tearDown(self):
        self._key_patch.stop()

    def _post(self, about_id, from_id, quote, note):
        from unittest.mock import AsyncMock, MagicMock
        req = MagicMock()
        req.headers = {'X-Agent-Key': 'k'}
        req.json = AsyncMock(return_value={
            'aboutId': about_id, 'fromId': from_id, 'quote': quote, 'note': note,
        })
        return asyncio.run(serve.post_report(req))

    def _reports(self):
        return (serve.get_state_from_db() or {}).get('reports') or []

    def test_severity_classified_at_filing(self):
        with unittest.mock.patch.object(serve, 'create_escalation', return_value='e'), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            resp = self._post('ben', 'ada', 'missed the deadline', 'held up the team')
        self.assertEqual(resp.status_code, 200)
        report = self._reports()[0]
        self.assertEqual(report['severity'], 'serious')
        self.assertEqual(report['aboutId'], 'ben')
        self.assertFalse(any(c.args[0] == 'personnel_strike' for c in log.call_args_list),
                         'a first strike alone must not notify the player')

    def test_second_strike_notifies_the_player(self):
        self._post('ben', 'ada', 'missed the deadline', 'held up the team')
        with unittest.mock.patch.object(serve, 'create_escalation', return_value='e') as esc, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            self._post('ben', 'zoe', 'unresponsive to the team', 'ignored requests')
        self.assertTrue(any(c.args[0] == 'personnel_strike' for c in esc.call_args_list),
                        'the second strike inside the window must notify the player')
        self.assertTrue(any(c.args[0] == 'player' and c.args[1] == 'personnel_strike'
                            for c in log.call_args_list))

    def test_severe_report_notifies_immediately(self):
        with unittest.mock.patch.object(serve, 'create_escalation', return_value='e') as esc, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            self._post('ben', 'ada', 'sabotaged the pipeline', 'deliberately broke it')
        report = self._reports()[0]
        self.assertEqual(report['severity'], 'severe')
        self.assertTrue(any(c.args[0] == 'personnel_strike' for c in esc.call_args_list))

    def test_minor_report_is_silent(self):
        with unittest.mock.patch.object(serve, 'create_escalation', return_value='e') as esc, \
             unittest.mock.patch.object(serve, 'log_action') as log:
            self._post('ben', 'ada', 'fine work today', 'nothing to note')
        self.assertEqual(self._reports()[0]['severity'], 'minor')
        self.assertFalse(any(c.args[0] == 'personnel_strike' for c in esc.call_args_list))


class ReviewerIndependence(unittest.TestCase):
    """No agent may read any employee's reports/ directory through the file
    API; the player keeps full visibility."""

    def test_agent_off_limits_for_any_reports_dir(self):
        self.assertTrue(serve._agent_reports_off_limits('ada'))
        self.assertTrue(serve._agent_reports_off_limits('nadia'))
        self.assertFalse(serve._agent_reports_off_limits('player'))
        self.assertFalse(serve._agent_reports_off_limits('unknown'))
        self.assertFalse(serve._agent_reports_off_limits(None))

    def test_own_reports_rule_unchanged(self):
        # The subject is still blocked from its own reports (pre-existing rule).
        self.assertTrue(serve._owns_reports_dir('ben', 'ben'))
        self.assertFalse(serve._owns_reports_dir('ben', 'ada'))


if __name__ == '__main__':
    unittest.main()
