"""Programs: the multi-team roll-up entity (2026-11-07 operator direction).

A program binds the pieces of one large ask across teams; the engine rolls
piece status up every pass, mints the owner's planning card once, and mints
the change-request card only when every piece is done/validated. Directors
own team/sprint creation -- the engine never mints piece cards itself."""
import unittest
from unittest import mock

import serve
import sim


def _program(**over):
    p = {
        'id': 'prog-1', 'title': 'Integrate the dankornas recommendations',
        'ownerAgentId': 'theo',
        'pieces': [
            {'title': 'Piece A: orchestration lane', 'teamId': 'ben',
             'taskType': 'code', 'deliverable': 'code', 'dependsOn': []},
            {'title': 'Piece B: memory service', 'teamId': 'ada',
             'taskType': 'code', 'deliverable': 'code',
             'dependsOn': ['Piece A: orchestration lane']},
        ],
        'status': 'planning', 'planningCardMinted': False,
        'crRequested': False,
    }
    p.update(over)
    return p


def _state(programs=None):
    return {
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True},
            {'id': 'nora', 'villageChief': True, 'chiefOfVillage': 'main'},
        ],
        'teams': [{'id': 'ben', 'directorId': 'ben'}],
        'tasks': {}, 'workQueue': [],
        'programs': programs if programs is not None else [],
    }


class PieceStateTests(unittest.TestCase):
    def _run(self, program, tasks=None, queue=None):
        st = _state()
        st['tasks'] = tasks or {}
        st['workQueue'] = queue or []
        return sim._program_piece_states(program, st)

    def test_unminted_queued_open_done_cancelled(self):
        p = _program()
        states, all_done = self._run(p)
        self.assertEqual(states[p['pieces'][0]['title']], ('unminted', None))
        self.assertFalse(all_done)
        states, _ = self._run(p, queue=[{'title': p['pieces'][0]['title']}])
        self.assertEqual(states[p['pieces'][0]['title']], ('queued', None))
        states, _ = self._run(p, tasks={
            'task-1': {'id': 'task-1', 'title': p['pieces'][0]['title'], 'status': 'working'}})
        self.assertEqual(states[p['pieces'][0]['title']], ('working', 'task-1'))
        states, _ = self._run(p, tasks={
            'task-1': {'title': p['pieces'][0]['title'], 'status': 'done'},
            'task-2': {'title': p['pieces'][1]['title'], 'status': 'done'}})
        self.assertTrue(all(s == 'done' for s, _ in states.values()))
        states, all_done = self._run(p, tasks={
            'task-1': {'id': 'task-1', 'title': p['pieces'][0]['title'], 'status': 'cancelled'}})
        self.assertEqual(states[p['pieces'][0]['title']], ('cancelled', 'task-1'))
        self.assertFalse(all_done)


class EnginePassTests(unittest.TestCase):
    def test_planning_card_minted_once_directed_to_owner(self):
        st = _state([_program()])
        with mock.patch.object(sim, 'queue_work', return_value=1) as qw:
            sim._programs_pass(st, now_ms=10_000)
            sim._programs_pass(st, now_ms=20_000)
        self.assertEqual(qw.call_count, 1, 'the planning card mints once')
        item = qw.call_args[0][1][0]
        self.assertEqual(item['assignedTo'], 'theo')
        self.assertTrue(item['directedTo'])
        self.assertEqual(item['priority'], 'high')
        self.assertIn('[PLAN]', item['title'])
        self.assertIn('Confer with the senior directors', item['instructions'])
        self.assertIn('create the necessary teams', item['instructions'])
        self.assertIn('sprints', item['instructions'])
        self.assertEqual(st['programs'][0]['planningCardMinted'], True)
        self.assertEqual(st['programs'][0]['pieceStatus']
                         ['Piece A: orchestration lane'], 'unminted')

    def test_cr_card_only_when_every_piece_is_done(self):
        st = _state([_program(status='planning-card-minted',
                              planningCardMinted=True)])
        with mock.patch.object(sim, 'queue_work', return_value=1) as qw:
            sim._programs_pass(st, now_ms=10_000)
        self.assertFalse(qw.called, 'one piece still open: no CR yet')
        st['tasks'] = {
            'task-1': {'id': 'task-1', 'title': 'Piece A: orchestration lane',
                       'status': 'done'},
            'task-2': {'id': 'task-2', 'title': 'Piece B: memory service',
                       'status': 'done'}}
        with mock.patch.object(sim, 'queue_work', return_value=1) as qw:
            sim._programs_pass(st, now_ms=20_000)
        self.assertTrue(qw.called)
        item = qw.call_args[0][1][0]
        self.assertIn('[CHANGE-REQUEST]', item['title'])
        self.assertEqual(item['assignedTo'], 'theo')
        self.assertIn('selfedit push window', item['instructions'])
        self.assertEqual(st['programs'][0]['status'], 'ready-for-cr')
        self.assertTrue(st['programs'][0]['crRequested'])

    def test_engine_never_mints_piece_cards_itself(self):
        st = _state([_program()])
        with mock.patch.object(sim, 'queue_work', return_value=1) as qw:
            sim._programs_pass(st, now_ms=10_000)
        self.assertEqual(len(qw.call_args[0][1]), 1,
                         'only the planning card; pieces belong to directors')


class ProgramApiTests(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        return TestClient(serve.app)

    def test_create_validates_and_lists(self):
        st = _state()
        c = self._client()
        with mock.patch.object(serve, 'verify_session', return_value=True), \
                mock.patch.object(serve, 'get_state_from_db',
                                  return_value=st), \
                mock.patch.object(serve, 'save_state_to_db',
                                  return_value=True):
            r = c.post('/api/intent/programs', json={'title': 'x'})
            self.assertEqual(r.status_code, 400)
            r = c.post('/api/intent/programs', json={
                'title': 'Program', 'ownerAgentId': 'theo',
                'pieces': [{'title': 'Piece A'}]})
            self.assertEqual(r.status_code, 200, r.text)
            pid = r.json()['program']['id']
            r = c.get('/api/programs')
            self.assertEqual(r.json()['programs'][0]['id'], pid)
            self.assertEqual(r.json()['programs'][0]['rollup'][0]['state'],
                             'unminted')

    def test_delete(self):
        st = _state([_program()])
        c = self._client()
        with mock.patch.object(serve, 'verify_session', return_value=True), \
                mock.patch.object(serve, 'get_state_from_db',
                                  return_value=st), \
                mock.patch.object(serve, 'save_state_to_db',
                                  return_value=True):
            r = c.delete('/api/programs/prog-1')
            self.assertEqual(r.status_code, 200)
            self.assertEqual(st['programs'], [])


class CadenceWindowTests(unittest.TestCase):
    def test_fine_cadences_accepted_by_validation(self):
        for cadence in ('30min', 'hourly'):
            item, err = serve._validate_scheduled_item({
                'title': 'x', 'ownerRole': 'regional-vp', 'cadence': cadence})
            self.assertIsNone(err, cadence)
            self.assertEqual(item['cadence'], cadence)

    def test_engine_knows_the_fine_cadences(self):
        self.assertIn('30min', sim.SCHEDULED_CADENCE_MS)
        self.assertEqual(sim.SCHEDULED_CADENCE_MS['30min'], 30 * 60 * 1000)

    def test_digest_window_reflects_since(self):
        import datetime
        import json
        now = datetime.datetime(2026, 11, 7, 21, 30,
                                tzinfo=datetime.timezone.utc).timestamp()
        rows = [(now - 600, json.dumps({'preview':
                    '[AI Think Tank] Failure: A content run was abandoned\n\nAgent: z'}))]
        written = {}

        class FakeFile:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def write(self, text):
                written['text'] = text

        with mock.patch.object(serve, '_db') as db, \
                mock.patch.object(serve, '_safe_library_path',
                                  return_value='/tmp/digest.md'), \
                mock.patch.object(serve, 'log_action'), \
                mock.patch('builtins.open', return_value=FakeFile()), \
                mock.patch.object(serve.os.path, 'dirname',
                                  return_value='/tmp'):
            conn = mock.MagicMock()
            conn.execute.return_value.fetchall.return_value = rows
            db.return_value.__enter__.return_value = conn
            serve.write_telegram_alert_digest(
                now=now, since_ts=now - 600)
        self.assertIn('last 0.25h', written['text'],
                      'a 10-minute window rounds up to the quarter-hour floor')


if __name__ == '__main__':
    unittest.main()
