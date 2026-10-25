"""Split part of test_cov_gap_serve.py (hermetic serve.py coverage tests)."""
import asyncio
import collections
import datetime
import shutil
import tempfile
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import core.config as config  # noqa: E402
import serve  # noqa: E402
from test_cov_gap_serve_helpers import (  # noqa: E402
    _NO_GRANT, _SENTINEL, req_with, decision_cache_report_defaults)


class TasteExpiredTest(unittest.TestCase):
    def test_bad_date(self):
        self.assertFalse(serve._taste_is_expired(
            'Review after: 2020-13-01'))


class SignalPlanBodyTest(unittest.TestCase):
    def test_paths(self):
        base = {'planId': 'p', 'agentId': 'a', 'subject': 's'}
        self.assertIn('object', serve._signal_plan_from_body(
            dict(base, scenarios=[1]))[1])
        self.assertIn('scenarioId', serve._signal_plan_from_body(
            dict(base, scenarios=[{'scenarioId': ''}]))[1])


class CoverageWriteTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'agentId': 'a'})
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, '_require_player_session',
                               return_value=False), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=False):
            self.assertEqual(asyncio.run(serve.coverage_write(
                req)).status_code, 401)
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'reviewed': True})
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True):
            self.assertEqual(asyncio.run(serve.coverage_write(
                req)).status_code, 400)


class CoverageListTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.query_params = {'status': 'bad'}
        with mock.patch.object(serve, '_coverage_read', return_value={}):
            self.assertEqual(asyncio.run(serve.coverage_list(
                req)).status_code, 400)
        req = mock.Mock()
        req.query_params = {'status': 'covered'}
        with mock.patch.object(serve, '_coverage_read',
                               return_value={'i': {'status': 'covered'}}):
            body = json.loads(asyncio.run(serve.coverage_list(req)).body)
        self.assertEqual(len(body['coverage']), 1)


class JevQuoteTest(unittest.TestCase):
    def test_bad_calls(self):
        req = mock.Mock()
        req.query_params = {'estimatedCalls': 'bad'}
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, 'quote_jev_grant',
                               return_value={}):
            self.assertEqual(json.loads(asyncio.run(
                serve.jev_quote(req)).body)['ok'], True)


class ReworkMetricsTest(unittest.TestCase):
    def test_bad_details(self):
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = [('bad',)]
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_db', return_value=cm):
            out = serve._rework_metrics(100)
        self.assertEqual(out['loops'], 1)


class LibraryDownloadTest(unittest.TestCase):
    def test_redirect_denied(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'url': 'https://x', 'filename': 'f', 'agentId': 'a'})
        req.headers = {'X-Agent-Key': 'k'}

        async def fake_to_thread(f, *a):
            return f(*a)
        with mock.patch.object(serve, 'BROWSING_ENABLED', True), \
             mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             mock.patch.object(serve, 'check_rate_limit',
                               return_value=True), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_sanitize_download_filename',
                               return_value='f'), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True), \
             mock.patch.object(serve, '_jev_quorum_decision',
                               new=mock.AsyncMock(
                                   return_value=('allow', .9, 0, 't'))), \
             mock.patch.object(serve, '_jev_safety_gate',
                               return_value=True), \
             mock.patch.object(serve, '_download_file_sync',
                               return_value=('https://y', 'text/plain',
                                             b'x', False)), \
             mock.patch.object(serve, '_jev_re_gate_redirect_final',
                               new=mock.AsyncMock(
                                   return_value=(False, 'no'))), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch('asyncio.to_thread', side_effect=fake_to_thread):
            body = json.loads(asyncio.run(serve.library_download(
                req)).body)
        self.assertFalse(body['allowed'])


class PostReportTest(unittest.TestCase):
    def test_notify_error(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'aboutId': 'b', 'fromId': 'a', 'quote': 'q', 'note': 'n'})
        req.headers = {'X-Agent-Key': 'k'}
        state = {'agentRoster': [{'id': 'b'}], 'reports': []}
        with mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_state_begin',
                               return_value=state), \
             mock.patch.object(serve, '_classify_report_severity',
                               return_value='minor'), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch.object(serve, '_append_passport_decision'), \
             mock.patch('sim._report_strikes',
                        side_effect=RuntimeError('boom')):
            body = json.loads(asyncio.run(serve.post_report(req)).body)
        self.assertTrue(body['ok'])


class BackfillTapeHashesTest(unittest.TestCase):
    def test_hashed_row_skipped(self):
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = [
            (1, 'prev', 0, 'k', 'm', 'p', 'c', 'ch', .5, 0, 'r', 1, 't')]
        with mock.patch.object(serve, '_tape_row_hash', return_value='h'):
            self.assertEqual(serve._backfill_decision_tape_hashes(conn), 0)


class AllowlistWaitEstimateTest(unittest.TestCase):
    def test_hours(self):
        with mock.patch.object(serve, '_allowlist_wait_samples',
                               return_value=[7200]):
            self.assertIn('hours', serve._allowlist_wait_estimate())


class ApproveServiceProposalTest(unittest.TestCase):
    def test_bad_note(self):
        self.assertFalse(serve._approve_api_service_proposal(
            {'note': 'bad'})[0])


class ApiCredentialValueTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.dict(serve.__dict__, {'ENVK': ''}), \
             mock.patch.object(serve, '_load_env',
                               return_value={'ENVK2': ''}):
            self.assertIn('not set', serve._api_credential_value(
                {'credential': {'type': 'env', 'key': 'ENVK',
                                'key2': 'ENVK2'}})[2])
        with mock.patch.object(serve, '_credential_token',
                               return_value=None), \
             mock.patch.object(serve, '_open_secret', return_value=None):
            self.assertIn('vault', serve._api_credential_value(
                {'credential': {'type': 'vault', 'name': 'n'}})[2])


class DecisionTelemetryTest(unittest.TestCase):
    def test_db_errors(self):
        with mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')):
            serve._decision_cache_put('m', {}, [], {})
            serve._record_decision_keys('m', {}, [])
            serve._prune_decision_key_stats()
            self.assertEqual(decision_cache_report_defaults(),
                             serve.decision_cache_report())
            self.assertEqual(serve.decision_key_report('')['calls'], 0)


class ParseHfTest(unittest.TestCase):
    def test_bad_bytes(self):
        self.assertIsNone(serve._parse_hf_contents(b'bad'))


class ApiMailReadTest(unittest.TestCase):
    def test_malformed(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(side_effect=ValueError('x'))
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, '_agent_id_for_key',
                               return_value='a'):
            self.assertEqual(asyncio.run(serve.api_mail_read(
                req)).status_code, 400)


class FeedbackInboxTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.cookies = {}
        with mock.patch.object(serve, 'verify_session',
                               return_value=False):
            self.assertEqual(asyncio.run(serve.feedback_player_inbox(
                'm', req)).status_code, 401)
        req = mock.Mock()
        req.cookies = {}
        req.json = mock.AsyncMock(return_value={'rating': True})
        with mock.patch.object(serve, 'verify_session',
                               return_value=True), \
             mock.patch.object(serve, '_state_begin',
                               return_value={'playerInbox': [
                                   {'id': 'm', 'kind': 'card_report'}]}):
            self.assertEqual(asyncio.run(serve.feedback_player_inbox(
                'm', req)).status_code, 400)


class ReadAgentFileTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        with mock.patch.object(serve, '_resolve_requester',
                               return_value='a'), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={'x': 1}), \
             mock.patch.object(serve, '_cross_village_agent_denied',
                               return_value=True):
            self.assertEqual(asyncio.run(serve.read_agent_file(
                'b', 'f', req)).status_code, 403)
        with mock.patch.object(serve, '_resolve_requester',
                               return_value='a'), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={'x': 1}), \
             mock.patch.object(serve, '_cross_village_agent_denied',
                               return_value=False), \
             mock.patch.object(serve, '_agent_reports_off_limits',
                               return_value=True), \
             mock.patch.object(serve, 'is_under_reports',
                               return_value=True):
            self.assertEqual(asyncio.run(serve.read_agent_file(
                'b', 'reports/x', req)).status_code, 403)


class LibraryPromoteTest(unittest.TestCase):
    def test_cross_village(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'path': 'pending_review/f', 'agentId': 'a'})
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, '_safe_library_path',
                               return_value='/p'), \
             mock.patch('os.path.isfile', return_value=True), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_scope_allows',
                               return_value=False), \
             mock.patch.object(serve, '_requester_villages',
                               return_value=[]), \
             mock.patch.object(serve, '_library_path_village',
                               return_value='v2'), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={'x': 1}), \
             mock.patch.object(serve, 'log_action'):
            self.assertEqual(asyncio.run(serve.library_promote(
                req)).status_code, 403)


class ProbeChannelsTest(unittest.TestCase):
    def test_no_probe_url(self):
        with mock.patch.object(serve, '_API_CHANNELS',
                               {'c': {'name': 'C', 'probe_url': ''}}):
            out = serve._probe_all_channels_sync()
        self.assertIn('no probe_url', out['c']['error'])


class ChannelProbeTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, 'check_rate_limit',
                               return_value=False):
            self.assertEqual(asyncio.run(serve.channel_probe(
                req)).status_code, 429)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, 'check_rate_limit',
                               return_value=True), \
             mock.patch.object(serve, '_API_CHANNELS_LOAD_ERROR', 'bad'):
            self.assertEqual(asyncio.run(serve.channel_probe(
                req)).status_code, 500)


class SmsInboxTest(unittest.TestCase):
    def test_bad_limit(self):
        req = mock.Mock()
        req.query_params = {'limit': 'bad'}
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = []
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, '_db', return_value=cm):
            self.assertEqual(asyncio.run(serve.sms_inbox(
                req)).status_code, 200)


class LoginScrubScalarTest(unittest.TestCase):
    def test_scalar(self):
        self.assertEqual(serve._login_scrub_result((1,)), (1,))


class TierRefreshLoggedTest(unittest.TestCase):
    def test_no_fresh(self):
        with mock.patch.object(serve, 'get_cached_model_tiers',
                               return_value={}), \
             mock.patch.object(serve, 'refresh_model_tiers',
                               new=mock.AsyncMock(return_value=None)), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={'x': 1}), \
             mock.patch.object(serve, '_admin_agent_id',
                               return_value=None), \
             mock.patch.object(serve, '_senior_most_director_id',
                               return_value=None), \
             mock.patch.object(serve, '_auto_failover_jev_if_gone',
                               new=mock.AsyncMock()):
            self.assertEqual(asyncio.run(serve._tier_refresh_logged('r')),
                             (None, False))


class PixellabPollTest(unittest.TestCase):
    def test_timeout_env(self):
        with mock.patch.object(serve, '_load_env',
                               return_value={'PIXELLAB_POLL_TIMEOUT_S':
                                             '0'}):
            self.assertIn('timed out', serve._pixellab_poll_job('j')[1])


class CreditsProbeLoopTest(unittest.TestCase):
    def test_error(self):
        async def stop(_s):
            raise asyncio.CancelledError()
        with mock.patch.object(serve, '_openrouter_account_credits',
                               side_effect=RuntimeError('x')), \
             mock.patch('asyncio.sleep', side_effect=stop):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._openrouter_credits_probe_loop())


class PeerReviewTickTest(unittest.TestCase):
    def test_serious_kept(self):
        state = {'lastPeerReviewAt': 0,
                 'reports': [{'ts': 10, 'severity': 'serious'}]}
        with mock.patch('time.time', return_value=10000.0), \
             mock.patch.object(serve, 'PEER_REVIEW_INTERVAL_S', 0), \
             mock.patch.object(serve, 'PEER_REVIEW_SIGNAL_STALE_S', 1), \
             mock.patch('sim.REPORT_STRIKE_WINDOW_MS', 10**12):
            serve._peer_review_tick(state)
        self.assertEqual(len(state['reports']), 1)


class HiggsfieldPollTest(unittest.TestCase):
    def test_error(self):
        with mock.patch.object(serve, '_higgsfield_call',
                               return_value=(None, 'err')):
            self.assertEqual(serve._higgsfield_poll('j'), (None, 'err'))


class SeedServicesTest(unittest.TestCase):
    def test_missing_seed(self):
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = (0,)
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch('os.path.exists', return_value=False):
            serve._seed_api_services_if_empty()


class ApiApplyAuthTest(unittest.TestCase):
    def test_mullvad_no_url(self):
        with self.assertRaises(ValueError):
            serve._api_apply_auth({'auth': {'type': 'mullvad'}}, 't', None,
                                  'GET', 'u', {}, None)


class BandPoolsTest(unittest.TestCase):
    def test_mandatory_reasoning_skipped(self):
        pools = serve._band_candidate_pools([
            {'id': 'm', 'reasoning': {'mandatory': True},
             'architecture': {'input_modalities': ['image'],
                              'output_modalities': ['text']},
             'pricing': {'prompt': '1', 'completion': '1'}}])
        self.assertTrue(all('m' not in [x['id'] for x in v]
                            for v in pools.values()))


class MintNonceTest(unittest.TestCase):
    def test_gc(self):
        with mock.patch.dict(serve.__dict__, {'_work_trust_nonces':
                                              {'old': 0}}), \
             mock.patch.object(serve, '_WORK_TRUST_TTL_MS', 1000), \
             mock.patch('time.time', return_value=0.0):
            serve._mint_work_trust_nonce()
        self.assertNotIn('old', serve._work_trust_nonces)


class RssIngestTest(unittest.TestCase):
    def test_trim(self):
        buf = {'f': [{'link': f'l{i}'} for i in range(5)]}
        with mock.patch.dict(serve.__dict__, {'_RSS_BUFFER': buf}), \
             mock.patch.object(serve, '_RSS_BUFFER_LOCK',
                               mock.MagicMock()), \
             mock.patch.object(serve, '_RSS_BUFFER_MAX_PER_FEED', 3), \
             mock.patch.object(serve, '_rss_match_topics',
                               return_value=[]):
            serve._rss_ingest('f', [{'link': 'n'}])
        self.assertEqual(len(buf['f']), 3)


class RoomKeywordTest(unittest.TestCase):
    def test_undelegatable_skipped(self):
        with mock.patch('sim._ROOM_KEYWORDS', {'x': ['y']}), \
             mock.patch.object(serve, '_DELEGATABLE_ROOMS', set()):
            self.assertIsNone(serve._room_keyword_pick('y'))


class IntentScheduleOnceTest(unittest.TestCase):
    def test_assigned_to(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'title': 't', 'at': '9999999999', 'assignedTo': 'other'})
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, '_state_begin',
                               return_value={'x': 1}), \
             mock.patch.object(serve, '_parse_schedule_once_at',
                               return_value=1000), \
             mock.patch('time.time', return_value=0.0):
            self.assertEqual(asyncio.run(serve.intent_schedule_once(
                req)).status_code, 400)


class WriteWikiServerTest(unittest.TestCase):
    def test_invalid_target(self):
        with mock.patch.object(serve, '_state_begin',
                               return_value={'x': 1}), \
             mock.patch('sim.wiki_write_page',
                        return_value=({}, True)), \
             mock.patch('sim.record_memory_provenance'), \
             mock.patch.object(serve, '_wiki_page_target',
                               return_value=None):
            self.assertIsNone(serve._write_wiki_server(
                'p', 't', 'think_tank', 'body'))


class LibraryListTest(unittest.TestCase):
    def test_scope_skip(self):
        req = mock.Mock()
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, 'f.txt'), 'w') as f:
            f.write('x')
        try:
            with mock.patch.object(serve, 'LIBRARY_DIR', tmp), \
                 mock.patch.object(serve, 'LIBRARY_ARCHIVE_DIR',
                                   os.path.join(tmp, 'archive')), \
                 mock.patch.object(serve, 'get_state_from_db',
                                   return_value={'x': 1}), \
                 mock.patch.object(serve, '_resolve_requester',
                                   return_value='a'), \
                 mock.patch.object(serve, '_requester_villages',
                                   return_value=set()), \
                 mock.patch.object(serve, '_scope_allows',
                                   return_value=False):
                body = json.loads(asyncio.run(serve.list_library(
                    req)).body)
            self.assertEqual(body['files'], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class LibrarySearchTest(unittest.TestCase):
    def test_scope_skip(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, 'f.txt'), 'w') as f:
            f.write('needle')
        try:
            with mock.patch.object(serve, 'LIBRARY_DIR', tmp), \
                 mock.patch.object(serve, 'get_state_from_db',
                                   return_value={'x': 1}), \
                 mock.patch.object(serve, '_scope_allows',
                                   return_value=False):
                self.assertEqual(serve._library_search_matches(
                    'needle'), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class IngestDocxTest(unittest.TestCase):
    def test_empty(self):
        doc = mock.Mock()
        doc.paragraphs = []
        doc.tables = []
        with mock.patch.object(serve, '_docx') as docx:
            docx.Document.return_value = doc
            self.assertEqual(serve._ingest_docx_to_text('d'),
                             ('', None))


class AdversarialHelpersTest(unittest.TestCase):
    def test_reset_and_launch(self):
        state = {'agents': {'a': {'villageId': serve._ADV_VILLAGE_ID}},
                 'villages': [{'id': serve._ADV_VILLAGE_ID}]}
        serve._disable_adversarial_village(state)
        self.assertEqual(state['agents']['a']['villageId'], 'main')
        with mock.patch.object(serve, '_adversarial_state',
                               return_value={}), \
             mock.patch.object(serve, '_spawn_winter_team',
                               return_value=None):
            self.assertFalse(serve._launch_adversarial_village(
                {}, 'admin', 'task')[0])
        with mock.patch.object(serve, '_adversarial_state',
                               return_value={'enabled': True,
                                             'status': 'active',
                                             'taskId': None}):
            self.assertIsNone(serve._adversarial_village_tick({}))


class NormalizeTargetTest(unittest.TestCase):
    def test_no_host(self):
        self.assertIsNotNone(serve._normalize_allowlist_target(
            'http://')[1])


class ScreenshotProxyTest(unittest.TestCase):
    def test_proxy_cmd(self):
        with mock.patch.object(serve, '_find_chrome',
                               return_value='/c'), \
             mock.patch.object(serve, 'THINK_TANK_DIR', '/t'), \
             mock.patch('subprocess.run') as run, \
             mock.patch('os.path.isfile', return_value=False):
            self.assertIsNone(serve._screenshot_url_sync(
                'https://x', proxy='http://p'))
        self.assertIn('--proxy-server=http://p', run.call_args[0][0])


class TierGettersTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.object(serve, 'get_tier_daily_log',
                               return_value=[]), \
             mock.patch.object(serve, 'get_catalog_price_history',
                               return_value=[]), \
             mock.patch.object(serve, 'get_tier_value_windows',
                               return_value={'x': 1}):
            self.assertEqual(json.loads(asyncio.run(
                serve.tier_daily_log_get('b', 0)).body)['log'], [])
            self.assertEqual(json.loads(asyncio.run(
                serve.catalog_price_history_get('m', 0)).body)['history'],
                [])
            self.assertEqual(json.loads(asyncio.run(
                serve.tier_value_windows_get()).body), {'x': 1})


class BrowseVpnConflictTest(unittest.TestCase):
    def test_both_vpn(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'url': 'https://x', 'agentId': 'a',
            'viaVpnCountry': 'us', 'viaVpnExit': 'e'})
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, 'BROWSING_ENABLED', True), \
             mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             mock.patch.object(serve, '_page_budget_exhausted',
                               return_value=False), \
             mock.patch.object(serve, 'check_rate_limit',
                               return_value=True), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True), \
             mock.patch.object(serve, '_is_allowlisted_host',
                               return_value=True), \
             mock.patch.object(serve, '_work_trust_from_body',
                               return_value=False), \
             mock.patch.object(serve, 'log_action'):
            body = json.loads(asyncio.run(serve.browse(req)).body)
        self.assertFalse(body['allowed'])


class PageProbeSyncTest(unittest.TestCase):
    def test_bad_wait_ms(self):
        pw = mock.MagicMock()
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = pw
        browser = mock.Mock()
        pw.chromium.launch.return_value = browser
        baseline = mock.Mock()
        baseline.evaluate.return_value = []
        page = mock.Mock()
        page.evaluate.return_value = []
        browser.new_page.side_effect = [baseline, page]
        with mock.patch.object(serve, 'sync_playwright',
                               return_value=ctx):
            serve._page_probe_sync('/f', [{'type': 'wait',
                                          'ms': 'bad'}], ['1'])
        page.wait_for_timeout.assert_called_once()


class FetchRenderedProxyTest(unittest.TestCase):
    def test_proxy(self):
        pw = mock.MagicMock()
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = pw
        browser = mock.Mock()
        pw.chromium.launch.return_value = browser
        page = mock.Mock()
        page.url = 'https://x'
        page.evaluate.side_effect = ['', []]
        browser.new_page.return_value = page
        with mock.patch.object(serve, 'sync_playwright',
                               return_value=ctx), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True):
            out = serve._fetch_rendered_page_sync('https://x',
                                                  proxy='http://p')
        self.assertEqual(out[0], 'https://x')
        pw.chromium.launch.assert_called_once_with(
            headless=True, proxy={'server': 'http://p'})


class PeerReviewPassTest(unittest.TestCase):
    def test_mail_failure(self):
        state = {
            'agentRoster': [
                {'id': 'w1', 'name': 'W1', 'role': 'Worker',
                 'director': 'd'},
                {'id': 'w2', 'name': 'W2', 'role': 'Worker',
                 'director': 'd'}],
            'agents': {'d': {}, 'w1': {}, 'w2': {}},
            'reports': [],
            'helpSignals': [],
        }

        def db():
            cm = mock.MagicMock()

            def execute(_sql, params):
                cur = mock.Mock()
                n = 5 if params[0] == 'w2' else 0
                cur.fetchall.return_value = [('task_completed', None, 1)] * n
                return cur
            cm.__enter__.return_value.execute.side_effect = execute
            return cm
        with mock.patch.object(serve, '_db', side_effect=db), \
             mock.patch.object(serve, '_direct_reports',
                               return_value=False), \
             mock.patch.object(serve, 'PEER_REVIEW_MIN_LOOKBACK_S', 0), \
             mock.patch.object(serve, 'PEER_REVIEW_SIGNAL_STALE_S', 0), \
             mock.patch.object(serve, 'PEER_REVIEW_FILE_PROBABILITY', 1), \
             mock.patch('random.random', return_value=0), \
             mock.patch('random.choices',
                        side_effect=lambda pop, weights, k: [pop[0]]), \
             mock.patch('sim._deliver_mail',
                        side_effect=RuntimeError('mail')), \
             mock.patch.object(serve, 'log_action'):
            self.assertEqual(serve._peer_review_pass(state, 100.0), 1)


class RefreshVisionFallbackTest(unittest.TestCase):
    def test_jev_choice_error(self):
        models = [{'id': 'v', 'name': 'V', 'price': 1.0}]
        conn = mock.Mock()
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn

        async def to_thread(f, *a):
            return f(*a)
        with mock.patch.object(serve, 'MODEL_BAND_PURPOSE', {}), \
             mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                               return_value=models), \
             mock.patch.object(serve, '_sync_model_catalog'), \
             mock.patch.object(serve, '_bucket_models_by_price',
                               return_value={}), \
             mock.patch.object(serve, 'get_model_benchmark_scores',
                               return_value=[]), \
             mock.patch.object(serve, '_band_candidate_pools',
                               return_value={'vision': models}), \
             mock.patch.object(serve, '_best_value_pick',
                               new=mock.AsyncMock(return_value=None)), \
             mock.patch.object(serve, '_call_openrouter_decision_sync',
                               side_effect=RuntimeError('jev')), \
             mock.patch.object(serve, '_jev_model', return_value='m'), \
             mock.patch.object(serve, '_verify_model_works_sync',
                               return_value=False), \
             mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch('asyncio.to_thread', side_effect=to_thread):
            self.assertEqual(asyncio.run(serve.refresh_model_tiers()), {})


class AssignBigTaskConstraintsTest(unittest.TestCase):
    def test_constraints(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'goal': 'g', 'constraints': {'team': 'x'}})

        async def to_thread(f, *a, **k):
            return f(*a, **k)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, '_state_begin',
                               return_value={'x': 1}), \
             mock.patch.object(serve, '_all_teams_busy_in_sprint',
                               return_value=True), \
             mock.patch.object(serve, '_breakdown_into_shared_backlog',
                               return_value={'feature': {'id': 'f',
                                                         'name': 'F'},
                                             'items': [{'id': 'i'}]}), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch('asyncio.to_thread', side_effect=to_thread):
            body = json.loads(asyncio.run(serve.intent_assign_big_task(
                req)).body)
        self.assertTrue(body['backlogged'])


class RoutePlayerApprovalTest(unittest.TestCase):
    def test_requires_approval(self):
        decision = mock.Mock(allowed=False, requires_approval=True,
                             approval_id='apr1')

        async def to_thread(f, *a):
            return f(*a)
        with mock.patch.object(serve, '_parse_approval_command',
                               return_value=None), \
             mock.patch.object(serve, '_admin_agent_id',
                               return_value=None), \
             mock.patch.object(serve, '_lane_decider',
                               return_value='ask'), \
             mock.patch.object(serve, '_LANE_ACTIONS',
                               {'ask': 'converse'}), \
             mock.patch.object(serve, '_theo_policy') as tp, \
             mock.patch.object(serve, 'log_action'), \
             mock.patch('asyncio.to_thread', side_effect=to_thread):
            tp.evaluate.return_value = decision
            out = asyncio.run(serve._route_player_request({}, 'hi'))
        self.assertIn('approval', out['reply'])


class DecideCaptureTest(unittest.TestCase):
    def test_training_capture(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'state': {'s': 1}, 'questions': [{'q': 1}], 'agentId': 'a'})
        data = {'choices': []}
        with mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             mock.patch.object(serve, '_jev_model',
                               return_value='m'), \
             mock.patch.object(serve, '_decide_allowed',
                               return_value=(True, '')), \
             mock.patch.object(serve, 'training_capture') as tc, \
             mock.patch.object(serve, '_call_openrouter_decision_sync',
                               return_value=data), \
             mock.patch.object(serve, '_jev_choice',
                               return_value=('c', .9, 0)), \
             mock.patch.object(serve, '_accrue_spend'), \
             mock.patch.object(serve, '_sim_village_for_agent',
                               return_value='v'), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch('asyncio.to_thread',
                        side_effect=lambda f, *a: f(*a)):
            tc.enabled.return_value = True
            tc.metadata.return_value = {}
            self.assertEqual(json.loads(asyncio.run(serve.decide(
                req)).body), data)


class CreditsWatchdogTest(unittest.TestCase):
    def test_low_then_restored_without_reminders(self):
        state = {'low': False}
        low = {'remaining': 0.10, 'totalCredits': 10.0, 'totalUsage': 9.9}
        with mock.patch.object(serve, 'OPENROUTER_CREDITS_WATCHDOG_FLOOR_USD', 0.50):
            self.assertEqual(serve._credits_watchdog_step(low, 100.0, state), 'low')
            self.assertIsNone(serve._credits_watchdog_step(low, 200.0, state))
            self.assertIsNone(serve._credits_watchdog_step(low, 3800.0, state),
                              'the watchdog must not nag hourly')
            self.assertEqual(serve._credits_watchdog_step(
                {'remaining': 2.0, 'totalCredits': 10.0, 'totalUsage': 8.0},
                4000.0, state), 'restored')
            self.assertIsNone(serve._credits_watchdog_step(
                {'remaining': 2.0, 'totalCredits': 10.0, 'totalUsage': 8.0},
                5000.0, state))

    def test_unknown_credits_do_not_alert(self):
        self.assertIsNone(serve._credits_watchdog_step(None, 1.0, {}))


class CreditsPauseTest(unittest.TestCase):
    def setUp(self):
        self._old = dict(serve._CREDITS_PAUSE)
        self._old_watch = dict(serve._CREDITS_WATCHDOG_STATE)
        serve._CREDITS_PAUSE.update(active=False, since=0.0)
        serve._CREDITS_WATCHDOG_STATE.update(low=False)

    def tearDown(self):
        serve._CREDITS_PAUSE.update(self._old)
        serve._CREDITS_WATCHDOG_STATE.update(self._old_watch)

    def test_pause_notifies_once_and_restore_clears_it(self):
        with mock.patch.object(serve, 'send_player_telegram_sync') as send, \
             mock.patch.object(serve, 'log_action'):
            self.assertEqual(serve._set_credits_pause(
                True, {'remaining': 0.10}, source='test'), 'paused')
            self.assertTrue(serve.credits_pause_active())
            self.assertIsNone(serve._set_credits_pause(
                True, {'remaining': 0.05}, source='test'))
            self.assertEqual(serve._set_credits_pause(
                False, {'remaining': 2.0}, source='test'), 'resumed')
            self.assertFalse(serve.credits_pause_active())
            self.assertEqual(send.call_count, 2)

    def test_model_call_is_refused_while_paused(self):
        serve._CREDITS_PAUSE.update(active=True)
        with self.assertRaises(serve.OpenRouterCreditsPaused):
            serve._call_openrouter_once_sync('test/model', [], 10)
        with self.assertRaises(serve.OpenRouterCreditsPaused):
            serve._post_openrouter_raw('test/model', [])
