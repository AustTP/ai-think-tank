"""Team memory at sprint close (2026-11-01).

The org's primary working unit keeps its own memory: closing a sprint
auto-writes a retrospective wiki page scoped to that team (teamId), and
only that team's tasks read it -- a worker never sees another team's or
another village's memory. Uses the same wiki pipeline (review-after expiry,
provenance ledger) instead of a parallel mechanism.

Hermetic: DB redirected to a throwaway temp dir (the test_rag_institutional
pattern); everything here is pure state plus the temp DB.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-team-memory-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        BROWSE_TRAIL_PATH=os.path.join(_TMP_DIR, 'browse_trail.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _seed():
    return {
        'agents': {'ada': {'name': 'Ada', 'room': 'pressoffice'},
                   'ben': {'name': 'Ben', 'room': 'observatory'}},
        'teams': {'team-1': {'scrumMasterId': 'ada'},
                  'team-2': {'scrumMasterId': 'ben'}},
        'wiki': {},
        'memoryLedger': [],
    }


def _sprint(state, sprint_id='spr-1', team_ids=('team-1',), items=None):
    rec = {
        'id': sprint_id, 'name': 'Sprint A', 'goal': 'Ship the thing',
        'ownerId': 'ada', 'createdAt': 1_000,
        'teamIds': list(team_ids), 'workerCount': 3, 'status': 'active',
        'items': items or [('Ship the API', 'pressoffice'),
                           ('Write the docs', 'pressoffice')],
    }
    (state.setdefault('sprints', {}))[sprint_id] = rec
    return rec


class SprintCloseWritesTeamMemory(unittest.TestCase):
    def test_close_writes_team_scoped_page(self):
        state = _seed()
        _sprint(state)
        sim.close_sprint(state, 'spr-1', now_ms=1_795_000_000_000)
        page = state['wiki']['pages'].get('team-team-1-sprint-spr-1')
        self.assertIsNotNone(page)
        self.assertEqual(page['teamId'], 'team-1')
        self.assertEqual(page['category'], 'team-memory')
        self.assertEqual(page['villageId'], sim.DEFAULT_VILLAGE)
        self.assertEqual(page['editedBy'], 'ada')  # the team's scrum master
        self.assertIn('team-memory', state['wiki']['categories'])
        # 'Write the docs' was queued but unfinished -> carried over.
        self.assertNotIn('Write the docs', str(page))

    def test_multi_team_sprint_writes_one_page_per_team(self):
        state = _seed()
        _sprint(state, team_ids=('team-1', 'team-2'))
        sim.close_sprint(state, 'spr-1', now_ms=1_795_000_000_000)
        self.assertIn('team-team-1-sprint-spr-1', state['wiki']['pages'])
        self.assertIn('team-team-2-sprint-spr-1', state['wiki']['pages'])
        self.assertEqual(state['wiki']['pages']['team-team-2-sprint-spr-1']['teamId'],
                         'team-2')

    def test_org_wide_sprint_writes_nothing(self):
        state = _seed()
        _sprint(state, team_ids=())
        sim.close_sprint(state, 'spr-1', now_ms=1_795_000_000_000)
        self.assertEqual((state.get('wiki') or {}).get('pages') or {}, {})

    def test_close_is_idempotent_on_memory(self):
        state = _seed()
        _sprint(state)
        sim.close_sprint(state, 'spr-1', now_ms=1_795_000_000_000)
        version = state['wiki']['pages']['team-team-1-sprint-spr-1']['version']
        sim.close_sprint(state, 'spr-1', now_ms=1_795_000_100_000)
        self.assertEqual(
            state['wiki']['pages']['team-team-1-sprint-spr-1']['version'], version)

    def test_provenance_ledger_records_the_write(self):
        state = _seed()
        _sprint(state)
        sim.close_sprint(state, 'spr-1', now_ms=1_795_000_000_000)
        entries = [e for e in state['memoryLedger']
                   if e.get('pageId') == 'team-team-1-sprint-spr-1']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['scope'], 'team')
        self.assertEqual(entries[0]['claim'][:7], 'Sprint ')


class TeamScopedReading(unittest.TestCase):
    def _state_with_page(self):
        state = _seed()
        _sprint(state)
        sim.close_sprint(state, 'spr-1', now_ms=1_795_000_000_000)
        return state

    def test_own_team_reads_the_retrospective(self):
        state = self._state_with_page()
        pages = sim.wiki_read_pages(state, 'bank', village_id='main', team_id='team-1')
        self.assertTrue(any(p['id'] == 'team-team-1-sprint-spr-1' for p in pages))

    def test_other_team_never_reads_it(self):
        state = self._state_with_page()
        pages = sim.wiki_read_pages(state, 'bank', village_id='main', team_id='team-2')
        self.assertFalse(any(p.get('teamId') == 'team-1' for p in pages))

    def test_team_page_skipped_without_team_context(self):
        state = self._state_with_page()
        pages = sim.wiki_read_pages(state, 'bank', village_id='main')
        self.assertFalse(any(p.get('teamId') == 'team-1' for p in pages))

    def test_injection_reaches_the_teams_task(self):
        state = self._state_with_page()
        ctx = sim.inject_wiki_context(state, {'room': 'bank', 'teamId': 'team-1',
                                              'agentId': 'ada'})
        self.assertIn('sprint spr-1 retrospective', ctx)

    def test_injection_hides_it_from_another_team(self):
        state = self._state_with_page()
        ctx = sim.inject_wiki_context(state, {'room': 'bank', 'teamId': 'team-2',
                                              'agentId': 'ben'})
        self.assertNotIn('sprint spr-1 retrospective', ctx)

    def test_expired_page_not_injected(self):
        state = self._state_with_page()
        page = state['wiki']['pages']['team-team-1-sprint-spr-1']
        page['reviewAfterMs'] = 1  # long expired
        ctx = sim.inject_wiki_context(state, {'room': 'bank', 'teamId': 'team-1',
                                              'agentId': 'ada'})
        self.assertNotIn('sprint spr-1 retrospective', ctx)

    def test_team_page_beats_think_tank_wide_but_not_exact_room(self):
        state = self._state_with_page()
        wiki = state['wiki']
        cats = wiki.setdefault('categories', {})
        cats.setdefault('think_tank', {'label': 'Think tank'})
        sim.wiki_write_page(
            state, 'wide-note', 'A wide note', 'think_tank', 'body',
            edited_by='ada', village_id='main')
        # The retrospective (team) + the wide page both match a bank task;
        # team memory outranks the wide one.
        pages = sim.wiki_read_pages(state, 'bank', village_id='main', team_id='team-1')
        ids = [p['id'] for p in pages]
        self.assertIn('team-team-1-sprint-spr-1', ids)
        self.assertIn('wide-note', ids)
        self.assertLess(ids.index('team-team-1-sprint-spr-1'), ids.index('wide-note'))


if __name__ == '__main__':
    unittest.main()
