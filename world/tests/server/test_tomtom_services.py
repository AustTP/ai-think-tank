"""Tests for the TomTom registry entries and the generic query_param auth.

The tank-native integration: four api_services.json entries (search,
geocoding, routing, traffic incidents) with per-product daily caps sized
from the documented free-plan MONTHLY allowances, and one generic auth
extension (query_param) in the api_call machinery. No TomTom-specific
Python adapter exists -- that is the point.
"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402


def _services_json():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), 'api_services.json')
    with open(path) as fh:
        return json.load(fh)


class EntriesValidate(unittest.TestCase):
    def test_all_four_tomtom_entries_validate(self):
        services = _services_json()['services']
        for sid in ('tomtom-search', 'tomtom-geocoding', 'tomtom-routing',
                    'tomtom-traffic'):
            spec = dict(services[sid])
            spec['id'] = sid
            resolved, normalized, error = serve._validate_api_service_spec(spec)
            self.assertIsNone(error, f'{sid}: {error}')
            self.assertEqual(resolved, sid)
            self.assertEqual(normalized['auth']['type'], 'query_param')
            self.assertEqual(normalized['credential']['name'], 'tomtom_api_key')
            self.assertEqual(normalized['base_url'], 'https://api.tomtom.com')

    def test_daily_caps_never_exceed_monthly_free_tier(self):
        # floor(monthly / 31) * 31 <= monthly, always, even in a 31-day month.
        documented = {'tomtom-search': 2500, 'tomtom-geocoding': 20000,
                      'tomtom-routing': 20000, 'tomtom-traffic': 2500}
        services = _services_json()['services']
        for sid, monthly in documented.items():
            cap = services[sid]['spend']['cap']
            self.assertLessEqual(cap * 31, monthly,
                                 f'{sid}: {cap}/day can exceed {monthly}/month')
            self.assertGreater(cap, 0)


class QueryParamAuth(unittest.TestCase):
    def test_query_param_injects_key_into_url(self):
        spec = {'auth': {'type': 'query_param', 'field': 'key'}}
        url, headers, body = serve._api_apply_auth(
            spec, 'sekrit-token', None, 'GET',
            'https://api.tomtom.com/search/2/poiSearch/coffee.json?lat=1&lon=2',
            {}, None)
        self.assertIn('key=sekrit-token', url)
        self.assertNotIn('Authorization', headers)
        self.assertIsNone(body)

    def test_query_param_appends_without_existing_query(self):
        spec = {'auth': {'type': 'query_param'}}
        url, _h, _b = serve._api_apply_auth(
            spec, 'k', None, 'GET', 'https://api.tomtom.com/x.json', {}, None)
        self.assertEqual(url, 'https://api.tomtom.com/x.json?key=k')

    def test_validator_rejects_bad_query_param_field(self):
        services = _services_json()['services']
        spec = dict(services['tomtom-search'])
        spec['id'] = 'tomtom-search'
        spec['auth'] = {'type': 'query_param', 'field': 'bad=field'}
        _sid, _norm, error = serve._validate_api_service_spec(spec)
        self.assertIn('query_param', error)


class RegistryShape(unittest.TestCase):
    def test_registry_exposes_tomtom_services_after_seed(self):
        # A fresh install seeds api_services from world/api_services.json the
        # first time the table comes up empty; a seeded temp DB must surface
        # the four TomTom entries to the generic api_call path.
        tmp = tempfile.mkdtemp(prefix='think-tank-tomtom-test-')
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, ignore_errors=True))
        with unittest.mock.patch.multiple(
                serve, DB_PATH=os.path.join(tmp, 'test.db'),
                THINK_TANK_DIR=tmp, LIBRARY_DIR=os.path.join(tmp, 'library'),
                PASSPORT_PATH=os.path.join(tmp, 'library', '.passport.json')):
            serve.init_db()
            serve._seed_api_services_if_empty()
            serve._load_api_services()
            registry = serve._API_SERVICES or {}
        for sid in ('tomtom-search', 'tomtom-geocoding', 'tomtom-routing',
                    'tomtom-traffic'):
            self.assertIn(sid, registry, f'{sid} missing from the seeded registry')
