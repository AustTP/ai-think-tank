"""VPN egress-location intelligence (world/vpn_location.py).

Hermetic: every network call is mocked, so no exit proxy, no
ip-api.com, and no real DB writes (temp dir) are touched.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import vpn_location  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-vpn-location-test-')
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


class FetchEgressIp(unittest.TestCase):
    def _opener(self, payload):
        response = unittest.mock.MagicMock()
        response.read.return_value = payload
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        opener = unittest.mock.Mock()
        opener.open.return_value = response
        return opener

    def test_direct_fetch_returns_valid_ip(self):
        opener = self._opener(b'203.0.113.7\n')
        with unittest.mock.patch.object(
                urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(vpn_location.fetch_egress_ip(), '203.0.113.7')

    def test_proxied_fetch_builds_proxy_handler(self):
        opener = self._opener(b'198.51.100.9')
        with unittest.mock.patch.object(
                urllib.request, 'build_opener', return_value=opener) as build:
            ip = vpn_location.fetch_egress_ip('http://127.0.0.1:18801')
        self.assertEqual(ip, '198.51.100.9')
        handler = build.call_args[0][0]
        self.assertIsInstance(handler, urllib.request.ProxyHandler)
        self.assertEqual(handler.proxies['https'], 'http://127.0.0.1:18801')

    def test_garbage_response_rejected(self):
        opener = self._opener(b'not-an-ip')
        with unittest.mock.patch.object(
                urllib.request, 'build_opener', return_value=opener):
            self.assertIsNone(vpn_location.fetch_egress_ip())

    def test_network_error_returns_none(self):
        opener = unittest.mock.Mock()
        opener.open.side_effect = urllib.request.URLError('boom')
        with unittest.mock.patch.object(
                urllib.request, 'build_opener', return_value=opener):
            self.assertIsNone(vpn_location.fetch_egress_ip())


class GeolocateIp(unittest.TestCase):
    def _urlopen(self, payload):
        response = unittest.mock.MagicMock()
        response.read.return_value = payload
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return unittest.mock.patch.object(
            urllib.request, 'urlopen', return_value=response)

    def test_success_returns_location_with_zip_and_null_area_code(self):
        payload = json.dumps({
            'status': 'success', 'country': 'United States',
            'regionName': 'New York', 'city': 'New York',
            'zip': '10001', 'isp': 'Mullvad VPN',
        }).encode()
        with self._urlopen(payload):
            location = vpn_location.geolocate_ip('203.0.113.7')
        self.assertEqual(location['country'], 'United States')
        self.assertEqual(location['region'], 'New York')
        self.assertEqual(location['zip'], '10001')
        self.assertEqual(location['isp'], 'Mullvad VPN')
        self.assertIsNone(location['areaCode'])

    def test_fail_status_returns_none(self):
        payload = json.dumps({'status': 'fail', 'message': 'invalid ip'}).encode()
        with self._urlopen(payload):
            self.assertIsNone(vpn_location.geolocate_ip('203.0.113.7'))

    def test_bad_json_returns_none(self):
        with self._urlopen(b'not json'):
            self.assertIsNone(vpn_location.geolocate_ip('203.0.113.7'))

    def test_non_http_scheme_is_rejected(self):
        with unittest.mock.patch.object(
                vpn_location, 'GEOLOCATION_URL',
                'ftp://ip-api.com/json/{ip}?fields=status'):
            self.assertIsNone(vpn_location.geolocate_ip('203.0.113.7'))

    def test_network_error_returns_none(self):
        with unittest.mock.patch.object(
                urllib.request, 'urlopen',
                side_effect=urllib.request.URLError('down')):
            self.assertIsNone(vpn_location.geolocate_ip('203.0.113.7'))


class FreshnessAndSnapshot(unittest.TestCase):
    def test_fresh_within_ttl(self):
        record = {'egressIp': '1.2.3.4', 'location': None, 'fetchedAt': 940.0}
        self.assertTrue(vpn_location.is_fresh(record, now=1000.0))

    def test_stale_after_ttl(self):
        record = {'egressIp': '1.2.3.4', 'location': None, 'fetchedAt': -2601.0}
        self.assertFalse(vpn_location.is_fresh(record, now=1000.0))

    def test_non_dict_or_missing_timestamp_is_not_fresh(self):
        self.assertFalse(vpn_location.is_fresh(None))
        self.assertFalse(vpn_location.is_fresh({'egressIp': '1.2.3.4'}))

    def test_record_location_upserts_without_clobbering_others(self):
        state = {}
        vpn_location.record_location(
            state, 'us-nyc', {'egressIp': '1.1.1.1', 'location': None, 'fetchedAt': 1.0})
        vpn_location.record_location(
            state, 'us-atl', {'egressIp': '2.2.2.2', 'location': None, 'fetchedAt': 2.0})
        snapshot = vpn_location.location_snapshot(state)
        self.assertEqual(set(snapshot), {'us-nyc', 'us-atl'})
        self.assertEqual(snapshot['us-atl']['egressIp'], '2.2.2.2')

    def test_location_snapshot_tolerates_bad_state(self):
        self.assertEqual(vpn_location.location_snapshot(None), {})
        self.assertEqual(vpn_location.location_snapshot({'vpnExitLocations': 'nope'}), {})


class DescribeExit(unittest.TestCase):
    EXITS = {'us-nyc': {
        'proxy': 'http://127.0.0.1:18801', 'country': 'United States',
        'city': 'New York', 'relay': 'se-us-nyc-wg-001',
        'useFor': 'US East storefront look'}}

    def test_combines_registry_meta_with_observed_location(self):
        snapshot = {'us-nyc': {
            'egressIp': '203.0.113.7',
            'location': {'country': 'United States', 'region': 'NY',
                         'city': 'New York', 'zip': '10001',
                         'areaCode': None, 'isp': 'Mullvad VPN'},
            'fetchedAt': time.time()}}
        described = vpn_location.describe_exit('us-nyc', self.EXITS, snapshot)
        self.assertEqual(described['proxy'], 'http://127.0.0.1:18801')
        self.assertEqual(described['relay'], 'se-us-nyc-wg-001')
        self.assertEqual(described['useFor'], 'US East storefront look')
        self.assertEqual(described['egressIp'], '203.0.113.7')
        self.assertEqual(described['observedLocation']['zip'], '10001')
        self.assertIsNone(described['observedLocation']['areaCode'])
        self.assertTrue(described['locationFresh'])

    def test_without_snapshot_reports_no_observation(self):
        described = vpn_location.describe_exit('us-nyc', self.EXITS)
        self.assertIsNone(described['observedLocation'])
        self.assertIsNone(described['egressIp'])
        self.assertFalse(described['locationFresh'])

    def test_stale_record_marked_not_fresh(self):
        snapshot = {'us-nyc': {'egressIp': '1.1.1.1', 'location': {},
                               'fetchedAt': time.time() - 7200}}
        described = vpn_location.describe_exit('us-nyc', self.EXITS, snapshot)
        self.assertFalse(described['locationFresh'])


class Refresh(unittest.TestCase):
    EXITS = {'us-nyc': {'proxy': 'http://127.0.0.1:18801'}}
    LOCATION = {'country': 'United States', 'region': 'NY',
                'city': 'New York', 'zip': '10001',
                'areaCode': None, 'isp': 'Mullvad VPN'}

    def test_refresh_exit_records_ip_and_location(self):
        with unittest.mock.patch.object(
                vpn_location, 'fetch_egress_ip', return_value='203.0.113.7'), \
             unittest.mock.patch.object(
                 vpn_location, 'geolocate_ip', return_value=self.LOCATION):
            record, error = vpn_location.refresh_exit('us-nyc', self.EXITS)
        self.assertIsNone(error)
        self.assertEqual(record['egressIp'], '203.0.113.7')
        self.assertEqual(record['location']['zip'], '10001')
        self.assertIsInstance(record['fetchedAt'], float)

    def test_refresh_exit_without_egress_ip(self):
        with unittest.mock.patch.object(
                vpn_location, 'fetch_egress_ip', return_value=None):
            record, error = vpn_location.refresh_exit('us-nyc', self.EXITS)
        self.assertIsNone(record)
        self.assertIn('no egress IP', error)

    def test_refresh_exit_geolocation_unavailable_keeps_ip(self):
        with unittest.mock.patch.object(
                vpn_location, 'fetch_egress_ip', return_value='203.0.113.7'), \
             unittest.mock.patch.object(
                 vpn_location, 'geolocate_ip', return_value=None):
            record, error = vpn_location.refresh_exit('us-nyc', self.EXITS)
        self.assertEqual(record['egressIp'], '203.0.113.7')
        self.assertIsNone(record['location'])
        self.assertEqual(error, 'geolocation unavailable')

    def test_refresh_exits_filters_unknown_names(self):
        with unittest.mock.patch.object(
                vpn_location, 'fetch_egress_ip', return_value='203.0.113.7'), \
             unittest.mock.patch.object(
                 vpn_location, 'geolocate_ip', return_value=self.LOCATION):
            results = vpn_location.refresh_exits(
                self.EXITS, names=['us-nyc', 'bogus'])
        self.assertEqual(set(results), {'us-nyc'})
        self.assertTrue(results['us-nyc']['ok'])
        self.assertIsNone(results['us-nyc']['error'])

    def test_refresh_exits_all_names_by_default(self):
        exits = {'us-nyc': {'proxy': 'http://127.0.0.1:18801'},
                 'us-atl': {'proxy': 'http://127.0.0.1:18802'}}
        with unittest.mock.patch.object(
                vpn_location, 'fetch_egress_ip', return_value='203.0.113.7'), \
             unittest.mock.patch.object(
                 vpn_location, 'geolocate_ip', return_value=self.LOCATION):
            results = vpn_location.refresh_exits(exits)
        self.assertEqual(set(results), {'us-nyc', 'us-atl'})


class VpnExitsView(unittest.TestCase):
    def test_get_vpn_exits_includes_location_fields(self):
        response = asyncio.run(serve.vpn_exits_view())
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertEqual([e['name'] for e in body['exits']],
                         sorted(serve.VPN_EXITS))
        self.assertIn('locationTtlS', body)
        self.assertEqual(body['locationTtlS'], vpn_location.LOCATION_TTL_S)
        for entry in body['exits']:
            for key in ('egressIp', 'observedLocation', 'locationFresh', 'fetchedAt'):
                self.assertIn(key, entry)


if __name__ == '__main__':
    unittest.main()
