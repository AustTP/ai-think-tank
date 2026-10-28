"""External watch loops (195): dead-man switch ping, new free-model
discovery, OSV dependency audit, GitHub CI/issue watcher, owner-calendar ICS
sync + the selfedit calendar-conflict integration. Every test is offline:
network seams are patched, side-car files are pointed at temp dirs."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
import selfedit  # noqa: E402


class TempWatchFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='external-watches-')
        self._patches = [
            mock.patch.object(serve, 'WATCH_FREE_MODELS_PATH',
                              os.path.join(self.tmp, 'watches_free_models.json')),
            mock.patch.object(serve, 'WATCH_OSV_PATH',
                              os.path.join(self.tmp, 'watches_osv.json')),
            mock.patch.object(serve, 'WATCH_GITHUB_PATH',
                              os.path.join(self.tmp, 'watches_github.json')),
            mock.patch.object(serve, 'CALENDAR_BLACKOUTS_PATH',
                              os.path.join(self.tmp, 'calendar_blackouts.json')),
            mock.patch.object(serve, 'WEBHOOKS_PATH',
                              os.path.join(self.tmp, 'webhooks.jsonl')),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class DeadmanTests(TempWatchFiles):
    def test_unconfigured_is_none_not_failure(self):
        self.assertIsNone(serve._deadman_ping_once(''))

    def test_http_error_is_a_failure(self):
        def boom(req, timeout=None):
            raise OSError('unreachable')
        with mock.patch.object(serve.urllib.request, 'urlopen', boom):
            self.assertIs(serve._deadman_ping_once('https://hc-ping.example/x'), False)

    def test_success(self):
        class Resp:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                return b'OK'
        with mock.patch.object(serve.urllib.request, 'urlopen',
                               lambda req, timeout=None: Resp()):
            self.assertIs(serve._deadman_ping_once('https://hc-ping.example/x'), True)


class FreeModelDiscoveryTests(TempWatchFiles):
    def test_diff_reports_only_new_free_models(self):
        catalog = [
            {'id': 'vendor/free-a', 'pricing': {'prompt': '0', 'completion': '0'}},
            {'id': 'vendor/paid', 'pricing': {'prompt': '0.000001', 'completion': '0.000002'}},
            {'id': 'vendor/free-b', 'pricing': {'prompt': '0', 'completion': '0'}},
            {'id': 'vendor/zero-with-junk', 'pricing': {}},
        ]
        # first-ever run baselines the whole catalog silently
        with mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                               lambda: catalog):
            self.assertEqual(serve._free_model_discovery_once(), [])
        data = json.load(open(serve.WATCH_FREE_MODELS_PATH))
        self.assertEqual(sorted(data['seen']),
                         ['vendor/free-a', 'vendor/free-b'])
        # a model that appears AFTER the baseline is the news
        catalog = catalog + [
            {'id': 'vendor/free-c', 'pricing': {'prompt': '0', 'completion': '0'}}]
        with mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                               lambda: catalog):
            fresh = serve._free_model_discovery_once()
        self.assertEqual(fresh, ['vendor/free-c'])
        # and it is not news the second time
        with mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                               lambda: catalog):
            self.assertEqual(serve._free_model_discovery_once(), [])

    def test_paid_alone_reports_nothing(self):
        catalog = [{'id': 'vendor/paid', 'pricing': {'prompt': '1', 'completion': '2'}}]
        with mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                               lambda: catalog):
            self.assertEqual(serve._free_model_discovery_once(), [])


class OsvAuditTests(TempWatchFiles):
    def test_requirements_pins_parse(self):
        req = os.path.join(self.tmp, 'requirements.txt')
        with open(req, 'w') as f:
            f.write('# comment\n'
                    'fastapi==0.115.0\n'
                    'some_pkg==1.2.3 # inline\n'
                    'loose-package>=2.0\n'
                    '-r other.txt\n'
                    'git+https://example/x.git\n'
                    '\n')
        pins = serve._parse_requirements_pins(req)
        self.assertEqual(pins, [{'name': 'fastapi', 'version': '0.115.0'},
                                {'name': 'some-pkg', 'version': '1.2.3'}])

    def test_querybatch_maps_hits_to_pins(self):
        calls = []
        class Resp:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                calls.append(json.loads(self._body) if hasattr(self, '_body') else None)
                return json.dumps({'results': [
                    {'vulns': [{'id': 'GHSA-xxxx', 'summary': 'bad', 'modified': 'm'}]},
                    {}]}).encode()
        def fake_urlopen(req, timeout=None):
            resp = Resp()
            resp._body = req.data
            return resp
        with mock.patch.object(serve.urllib.request, 'urlopen', fake_urlopen):
            hits = serve._osv_query_sync([{'name': 'fastapi', 'version': '0.115.0'},
                                          {'name': 'clean', 'version': '1.0.0'}])
        self.assertEqual(list(hits), ['fastapi==0.115.0'])
        self.assertEqual(hits['fastapi==0.115.0'][0]['id'], 'GHSA-xxxx')

    def test_audit_once_persists_and_logs(self):
        req = os.path.join(self.tmp, 'requirements.txt')
        with open(req, 'w') as f:
            f.write('fastapi==0.115.0\n')
        with mock.patch.object(serve, '_osv_query_sync',
                               lambda pins: {'fastapi==0.115.0': [{'id': 'GHSA-1'}]}):
            hits = serve._osv_dependency_audit_once(requirements_path=req)
        self.assertEqual(len(hits), 1)
        data = json.load(open(serve.WATCH_OSV_PATH))
        self.assertEqual(data['scanned'], 1)
        self.assertEqual(data['hits']['fastapi==0.115.0'][0]['id'], 'GHSA-1')


class GithubWatchTests(TempWatchFiles):
    def _runs_payload(self):
        return {'workflow_runs': [
            {'id': 11, 'status': 'completed', 'conclusion': 'failure',
             'name': 'tests', 'head_branch': 'main', 'head_sha': 'abc123def',
             'html_url': 'https://github.com/x/y/actions/runs/11',
             'actor': {'login': 'developer'}},
            {'id': 12, 'status': 'completed', 'conclusion': 'success',
             'name': 'tests', 'head_branch': 'main', 'head_sha': 'abc',
             'html_url': 'u', 'actor': {'login': 'developer'}},
        ]}

    def test_failures_and_new_issues_become_webhook_entries(self):
        issues = [{'id': 900, 'title': 'flake in suite X',
                   'html_url': 'https://github.com/x/y/issues/1',
                   'user': {'login': 'owner'}},
                  {'id': 901, 'title': 'PR title',
                   'html_url': 'u', 'user': {'login': 'o'},
                   'pull_request': {'url': 'p'}}]
        with mock.patch.object(serve, '_github_call',
                               side_effect=[(self._runs_payload(), None), (issues, None)]):
            serve._github_watch_once('owner/repo')
        lines = [json.loads(l) for l in open(serve.WEBHOOKS_PATH)]
        self.assertEqual(len(lines), 2)
        run = [e for e in lines if e['payload']['id'] == 'run-11'][0]
        self.assertEqual(run['channel'], 'github')
        self.assertIn('failure', run['payload']['text'])
        issue = [e for e in lines if e['payload']['id'] == 'issue-900'][0]
        self.assertEqual(issue['payload']['author'], 'owner')
        # PRs and successes are not relayed
        self.assertFalse(any('901' in e['payload']['id'] or 'run-12' in e['payload']['id']
                             for e in lines))

    def test_second_poll_does_not_relay_the_same_run(self):
        with mock.patch.object(serve, '_github_call',
                               side_effect=[(self._runs_payload(), None), ([], None)]):
            serve._github_watch_once('owner/repo')
        with mock.patch.object(serve, '_github_call',
                               side_effect=[(self._runs_payload(), None), ([], None)]):
            serve._github_watch_once('owner/repo')
        self.assertEqual(len(open(serve.WEBHOOKS_PATH).readlines()), 1)


class IcsParsingTests(TempWatchFiles):
    def test_utc_timed_event(self):
        events = serve._parse_ics_events(
            'BEGIN:VEVENT\r\nUID:a@x\r\nSUMMARY:Owner offline\r\n'
            'DTSTART:20300101T090000Z\r\nDTEND:20300101T100000Z\r\n'
            'END:VEVENT\r\n')
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e['summary'], 'Owner offline')
        self.assertEqual(e['end_ts'] - e['start_ts'], 3600)
        self.assertEqual(time.strftime('%H:%M', time.gmtime(e['start_ts'])), '09:00')

    def test_tzid_and_line_folding(self):
        events = serve._parse_ics_events(
            'BEGIN:VEVENT\nUID:b@x\nSUMMARY:long summary that\n'
            '  continues here\nDTSTART;TZID=America/New_York:20300101T090000\n'
            'DTEND;TZID=America/New_York:20300101T100000\nEND:VEVENT\n')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['summary'], 'long summary that continues here')
        # 09:00 New York (UTC-5 in January) == 14:00 UTC
        self.assertEqual(time.strftime('%H:%M', time.gmtime(events[0]['start_ts'])),
                         '14:00')

    def test_all_day_event(self):
        events = serve._parse_ics_events(
            'BEGIN:VEVENT\nUID:c@x\nSUMMARY:trip\n'
            'DTSTART;VALUE=DATE:20300101\nDTEND;VALUE=DATE:20300103\nEND:VEVENT\n')
        self.assertEqual(events[0]['end_ts'] - events[0]['start_ts'], 2 * 86400)

    def test_missing_end_defaults_to_one_hour(self):
        events = serve._parse_ics_events(
            'BEGIN:VEVENT\nUID:d@x\nSUMMARY:sync\n'
            'DTSTART:20300101T120000Z\nEND:VEVENT\n')
        self.assertEqual(events[0]['end_ts'] - events[0]['start_ts'], 3600)


class CalendarConflictTests(TempWatchFiles):
    def test_overlapping_owner_event_blocks_the_slot(self):
        start = time.time() + 7 * 86400
        with open(serve.CALENDAR_BLACKOUTS_PATH, 'w') as f:
            json.dump({'fetchedAt': time.time(), 'events': [
                {'uid': 'a@x', 'summary': 'Dentist', 'start_ts': start - 600,
                 'end_ts': start + 600},
                {'uid': 'b@x', 'summary': 'Later', 'start_ts': start + 7200,
                 'end_ts': start + 10800}]}, f)
        conflicts = selfedit._calendar_conflicts(start, start + 1800)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['kind'], 'owner calendar')
        self.assertEqual(conflicts[0]['name'], 'Dentist')
        self.assertEqual(selfedit._calendar_conflicts(start + 3600, start + 5400), [])

    def test_stale_events_are_ignored(self):
        old = time.time() - 10 * 86400
        with open(serve.CALENDAR_BLACKOUTS_PATH, 'w') as f:
            json.dump({'events': [{'uid': 'o@x', 'summary': 'past',
                                   'start_ts': old, 'end_ts': old + 600}]}, f)
        self.assertEqual(selfedit._calendar_conflicts(old + 300, old + 900), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
