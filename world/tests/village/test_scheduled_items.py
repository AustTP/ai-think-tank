"""Scheduled items: owner-role recurring cards (2026-11-07 operator
direction). The engine resolves an owner role to live agents and mints a
directed card each cadence; the digest lands the last 24h of Telegram
alerts in the Library first."""
import datetime
import json
import os
import unittest
from unittest import mock

import serve
import sim


def _state():
    return {
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True},
            {'id': 'nora', 'villageChief': True, 'chiefOfVillage': 'main'},
            {'id': 'ben', 'director': 'nora'},
            {'id': 'zoe', 'director': 'ben'},
        ],
        'teams': [{'id': 'ben', 'directorId': 'ben'}],
        'workQueue': [],
    }


class ResolveOwnerTests(unittest.TestCase):
    def test_chief_of_staff_resolves_to_admin(self):
        self.assertEqual(sim._resolve_scheduled_owners(
            _state(), {'ownerRole': 'chief-of-staff'}), ['theo'])

    def test_regional_vp_resolves_per_village_and_all(self):
        st = _state()
        self.assertEqual(sim._resolve_scheduled_owners(
            st, {'ownerRole': 'regional-vp', 'villageId': 'main'}), ['nora'])
        self.assertEqual(sim._resolve_scheduled_owners(
            st, {'ownerRole': 'regional-vp'}), ['nora'])
        self.assertEqual(sim._resolve_scheduled_owners(
            st, {'ownerRole': 'regional-vp', 'villageId': 'north'}), [])

    def test_senior_director_resolves_all_and_by_team(self):
        st = _state()
        self.assertEqual(sim._resolve_scheduled_owners(
            st, {'ownerRole': 'senior-director'}), ['ben'])
        self.assertEqual(sim._resolve_scheduled_owners(
            st, {'ownerRole': 'senior-director', 'teamId': 'ben'}), ['ben'])
        self.assertEqual(sim._resolve_scheduled_owners(
            st, {'ownerRole': 'senior-director', 'teamId': 'ghost'}), [])

    def test_worker_needs_an_agent_id(self):
        st = _state()
        self.assertEqual(sim._resolve_scheduled_owners(
            st, {'ownerRole': 'worker', 'agentId': 'zoe'}), ['zoe'])
        self.assertEqual(sim._resolve_scheduled_owners(
            st, {'ownerRole': 'worker'}), [])
        self.assertEqual(sim._resolve_scheduled_owners(
            st, {'ownerRole': 'worker', 'agentId': 'ghost'}), [])


class OnceCadenceTests(unittest.TestCase):
    """One-shot scheduled items (owner direction 2026-11-11): fire once at
    notBeforeMs. ownerRole 'player' surfaces to the operator's inbox and the
    email/telegram outbox instead of minting an agent card."""

    def _item(self, not_before, owner_role='player'):
        return {'id': 'sch-once-1', 'title': 'Review the Eidetic Engine plan',
                'ownerRole': owner_role, 'cadence': 'once',
                'notBeforeMs': not_before,
                'instructions': 'Schedule the work order.',
                'enabled': True}

    def test_player_item_fires_once_into_the_operator_inbox(self):
        st = _state()
        st['playerInbox'] = []
        st['emailOutbox'] = []
        st['scheduledItems'] = [self._item(1_700_000_000_000)]
        with mock.patch.object(serve, 'log_action'), \
             mock.patch.object(sim, '_queue_player_email') as mail:
            sim._scheduled_items_pass(st, 1_700_000_000_001)
        self.assertEqual(len(st['playerInbox']), 1)
        self.assertIn('Eidetic', st['playerInbox'][0]['question'])
        mail.assert_called_once()
        item = st['scheduledItems'][0]
        self.assertFalse(item['enabled'], 'a once item must not re-fire')

    def test_player_item_waits_until_not_before(self):
        st = _state()
        st['playerInbox'] = []
        st['emailOutbox'] = []
        st['scheduledItems'] = [self._item(1_700_000_000_000)]
        with mock.patch.object(serve, 'log_action'), \
             mock.patch.object(sim, '_queue_player_email') as mail:
            sim._scheduled_items_pass(st, 1_700_000_000_000 - 1)
        self.assertEqual(st['playerInbox'], [])
        mail.assert_not_called()
        self.assertTrue(st['scheduledItems'][0]['enabled'])

    def test_agent_once_item_mints_one_directed_card_then_disables(self):
        st = _state()
        st['scheduledItems'] = [self._item(1_700_000_000_000,
                                           owner_role='chief-of-staff')]
        with mock.patch.object(serve, 'log_action'), \
             mock.patch.object(sim, 'queue_work', return_value=1) as qw:
            sim._scheduled_items_pass(st, 1_700_000_000_001)
        self.assertEqual(qw.call_count, 1)
        self.assertEqual(qw.call_args[0][1][0]['assignedTo'], 'theo')
        self.assertFalse(st['scheduledItems'][0]['enabled'])


class EnginePassTests(unittest.TestCase):
    def _item(self, **over):
        item = dict(sim._SCHEDULED_SEED_ITEM)
        item.update(over)
        return item

    def test_seed_created_once_on_first_pass(self):
        st = _state()
        with mock.patch.object(sim, 'queue_work', return_value=1) as qw, \
                mock.patch.object(sim, 'serve', create=True):
            sim._scheduled_items_pass(st, now_ms=10_000_000_000)
        self.assertEqual([i['id'] for i in st['scheduledItems']],
                         ['sch-rvp-telegram-digest'])
        self.assertTrue(qw.called, 'a due seeded item mints its card')
        # Second pass inside the cadence does NOT re-mint.
        with mock.patch.object(sim, 'queue_work', return_value=1) as qw2:
            sim._scheduled_items_pass(st, now_ms=10_000_000_000 + 60_000)
        self.assertFalse(qw2.called)

    def test_present_but_empty_list_never_reseeds(self):
        st = _state()
        st['scheduledItems'] = []
        with mock.patch.object(sim, 'queue_work', return_value=1):
            sim._scheduled_items_pass(st, now_ms=10_000_000_000)
        self.assertEqual(st['scheduledItems'], [],
                         'the operator deleted the seed; it stays deleted')

    def test_card_is_directed_to_the_owner_with_digest_source(self):
        st = _state()
        with mock.patch.object(sim, 'queue_work', return_value=1) as qw, \
                mock.patch.object(serve, 'write_telegram_alert_digest',
                                  return_value='analysis/digest.md'):
            sim._scheduled_items_pass(st, now_ms=10_000_000_000)
        item = qw.call_args[0][1][0]
        self.assertEqual(item['assignedTo'], 'nora')
        self.assertTrue(item['directedTo'])
        self.assertEqual(item['deliverable'], 'report')
        self.assertEqual(item['sourcePaths'], ['analysis/digest.md'])
        self.assertIn('library/analysis/digest.md', item['instructions'])

    def test_no_live_owner_skips_the_period_and_logs(self):
        st = _state()
        st['agentRoster'] = [r for r in st['agentRoster']
                             if not r.get('villageChief')]
        with mock.patch.object(sim, 'queue_work', return_value=1) as qw, \
                mock.patch.object(sim, '_log_governance') as logged:
            sim._scheduled_items_pass(st, now_ms=10_000_000_000)
        self.assertFalse(qw.called)
        self.assertEqual(st['scheduledItems'][0].get('lastRunSkipped'),
                         'no-live-owner')
        self.assertTrue(logged.called)

    def test_open_previous_instance_dedups_instead_of_duplicating(self):
        st = _state()
        with mock.patch.object(serve, 'write_telegram_alert_digest',
                               return_value=None), \
                mock.patch.object(sim, 'queue_work',
                                  side_effect=sim.queue_work):
            st['workQueue'] = [{'title': sim._SCHEDULED_SEED_ITEM['title'],
                                'notBefore': None}]
            sim._scheduled_items_pass(st, now_ms=10_000_000_000)
        self.assertEqual(len(st['workQueue']), 1,
                         'an open same-title card suppresses the mint')


class DigestTests(unittest.TestCase):
    def test_digest_writes_grouped_alerts_to_the_library(self):
        now = datetime.datetime(2026, 11, 7, 19, 0,
                                tzinfo=datetime.timezone.utc).timestamp()
        rows = [
            (now - 3600, json.dumps({'preview':
                '[AI Think Tank] Failure: A content run was abandoned\n\nAgent: marcus'})),
            (now - 1800, json.dumps({'preview':
                '[AI Think Tank] Failure: A content run was abandoned\n\nAgent: tucker'})),
            (now - 60, json.dumps({'preview':
                '[AI Think Tank] OpenRouter credits restored\n\nRemaining $14.02.'})),
        ]
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
            path = serve.write_telegram_alert_digest(now=now)
        self.assertEqual(path, serve.telegram_digest_scan_path(now))
        text = written['text']
        self.assertIn('Total alerts: 3', text)
        self.assertIn('A content run was abandoned (x2)', text)
        self.assertIn('marcus', text)
        self.assertIn('OpenRouter credits restored (x1)', text)

    def test_digest_path_is_stable_per_day(self):
        self.assertEqual(serve.telegram_digest_path('2026-11-07'),
                         'analysis/telegram-alert-digest-2026-11-07.md')

    def test_digest_writes_a_unique_file_per_scan(self):
        # A 30-min scan must NOT overwrite the day's earlier digests (the
        # RVP's second scan once read a 0-alert rolling window where the
        # 24h triage source had been).
        import datetime
        t1 = datetime.datetime(2026, 11, 7, 21, 47,
                               tzinfo=datetime.timezone.utc).timestamp()
        t2 = datetime.datetime(2026, 11, 7, 22, 17,
                               tzinfo=datetime.timezone.utc).timestamp()
        p1 = serve.telegram_digest_scan_path(t1)
        p2 = serve.telegram_digest_scan_path(t2)
        self.assertEqual(p1, 'analysis/telegram-alert-digest-2026-11-07-2147.md')
        self.assertNotEqual(p1, p2)


class ScheduleApiTests(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        return TestClient(serve.app)

    def test_create_requires_owner_role_and_cadence(self):
        c = self._client()
        with mock.patch.object(serve, 'verify_session', return_value=True):
            r = c.post('/api/intent/schedule-recurring', json={'title': 'x'})
            self.assertEqual(r.status_code, 400)
            r = c.post('/api/intent/schedule-recurring',
                       json={'title': 'x', 'ownerRole': 'wizard',
                             'cadence': 'daily'})
            self.assertEqual(r.status_code, 400)
            self.assertIn('ownerRole', r.json()['error'])

    def test_create_list_edit_delete_round_trip(self):
        st = _state()
        c = self._client()
        with mock.patch.object(serve, 'verify_session', return_value=True), \
                mock.patch.object(serve, 'get_state_from_db',
                                  return_value=st), \
                mock.patch.object(serve, 'save_state_to_db',
                                  return_value=True):
            r = c.post('/api/intent/schedule-recurring', json={
                'title': 'Quarterly review of the technical agreements',
                'ownerRole': 'senior-director', 'cadence': 'quarterly',
                'deliverable': 'report'})
            self.assertEqual(r.status_code, 200, r.text)
            sid = r.json()['item']['id']
            r = c.get('/api/schedules')
            self.assertIn(sid, [i['id'] for i in r.json()['items']])
            r = c.patch(f'/api/schedules/item/{sid}', json={'enabled': False})
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json()['item']['enabled'])
            r = c.delete(f'/api/schedules/item/{sid}')
            self.assertEqual(r.status_code, 200)
            r = c.get('/api/schedules')
            self.assertNotIn(sid, [i['id'] for i in r.json()['items']])


if __name__ == '__main__':
    unittest.main()


class DigestOnlyAlerts(unittest.TestCase):
    """Digest-only alerts land in the digest scan but never ping Telegram
    immediately (operator direction 2026-11-09): a card-pattern suppression
    is the loop stopper working, not a live failure."""

    def test_digest_scan_includes_digest_only_rows(self):
        # serve.py execs server/serve_parts/*.py at import; assert against
        # the parts source, not the shim.
        parts = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))),
            'server', 'serve_parts')
        src = open(os.path.join(parts, '425_scheduled_items.py'),
                   encoding='utf-8').read()
        self.assertIn("'telegram_sent', 'telegram_digest_only'", src,
                      'the digest scan reads telegram_digest_only rows')

    def test_stopper_uses_digest_only_channel(self):
        parts = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))),
            'village', 'sim_parts')
        src = open(os.path.join(parts, '070__fire_decision.py'),
                   encoding='utf-8').read()
        block = src[src.index('def _stop_card_pattern'):]
        block = block[:block.index('\ndef ', 10)]
        self.assertNotIn('send_player_telegram_sync', block,
                         'suppression must not ping Telegram directly')
        self.assertIn('telegram_digest_only', block,
                      'suppression logs a digest-only row instead')
