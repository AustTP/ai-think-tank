"""Tests for the phone check-in feature: POST /api/device/checkin
lets a device (an iOS Shortcut, to start) report location/battery/Focus/Wi-Fi.
See sim.record_device_checkin and serve.device_checkin.

DB-isolated (setUpModule below) from the start -- this file was written after
finding test_composite_trust.py and test_email.py both lacked it, so it never
gets a chance to repeat that class of bug.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim  # noqa: E402
import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None
_NOW_MS = 1_725_000_000_000


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-device-checkin-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        DEVICE_API_KEY='test-device-key-abc123',
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class RecordDeviceCheckin(unittest.TestCase):
    """sim.record_device_checkin -- pure, no serve.py import needed."""

    def test_stores_last_and_appends_history(self):
        st = {}
        entry = sim.record_device_checkin(st, {'battery': 82, 'wifi': None}, now_ms=_NOW_MS)
        self.assertEqual(entry['battery'], 82)
        self.assertNotIn('wifi', entry)  # None values dropped, not stored as noise
        self.assertEqual(entry['receivedAt'], _NOW_MS)
        self.assertEqual(st['deviceCheckins']['last'], entry)
        self.assertEqual(st['deviceCheckins']['history'], [entry])

    def test_history_capped_at_max(self):
        st = {}
        for i in range(sim._DEVICE_CHECKIN_HISTORY_MAX + 10):
            sim.record_device_checkin(st, {'battery': i}, now_ms=_NOW_MS + i)
        history = st['deviceCheckins']['history']
        self.assertEqual(len(history), sim._DEVICE_CHECKIN_HISTORY_MAX)
        # Trimmed from the FRONT -- oldest dropped, newest kept, in order.
        self.assertEqual(history[0]['battery'], 10)
        self.assertEqual(history[-1]['battery'], sim._DEVICE_CHECKIN_HISTORY_MAX + 9)
        self.assertEqual(st['deviceCheckins']['last']['battery'], sim._DEVICE_CHECKIN_HISTORY_MAX + 9)


class DeviceCheckinEndpoint(unittest.TestCase):
    """POST /api/device/checkin via TestClient -- real auth, real validation,
    no live network (record_device_checkin itself never touches the network)."""

    def _client(self):
        from starlette.testclient import TestClient
        return TestClient(serve.app)

    def test_valid_checkin_stores_and_returns_ok(self):
        c = self._client()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value={'agents': {}}), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save:
            r = c.post('/api/device/checkin', headers={'X-Device-Key': 'test-device-key-abc123'},
                      json={'location': {'lat': 1.0, 'lon': 2.0}, 'battery': 50})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['ok'])
        self.assertIn('storedAt', r.json())
        save.assert_called_once()

    def test_missing_key_rejected(self):
        c = self._client()
        r = c.post('/api/device/checkin', json={'battery': 50})
        self.assertEqual(r.status_code, 401)

    def test_wrong_key_rejected(self):
        c = self._client()
        r = c.post('/api/device/checkin', headers={'X-Device-Key': 'not-the-real-key'},
                   json={'battery': 50})
        self.assertEqual(r.status_code, 401)

    def test_empty_body_rejected(self):
        c = self._client()
        r = c.post('/api/device/checkin', headers={'X-Device-Key': 'test-device-key-abc123'}, json={})
        self.assertEqual(r.status_code, 400)

    def test_malformed_location_rejected(self):
        c = self._client()
        r = c.post('/api/device/checkin', headers={'X-Device-Key': 'test-device-key-abc123'},
                   json={'location': 'not an object'})
        self.assertEqual(r.status_code, 400)

    def test_missing_state_returns_503(self):
        c = self._client()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=None):
            r = c.post('/api/device/checkin', headers={'X-Device-Key': 'test-device-key-abc123'},
                      json={'battery': 50})
        self.assertEqual(r.status_code, 503)

    def test_only_battery_no_location_is_valid(self):
        # Every field is optional -- a Shortcut might only send some of them.
        c = self._client()
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value={'agents': {}}), \
             unittest.mock.patch.object(serve, 'save_state_to_db'):
            r = c.post('/api/device/checkin', headers={'X-Device-Key': 'test-device-key-abc123'},
                      json={'focus': 'Sleep'})
        self.assertEqual(r.status_code, 200)


if __name__ == '__main__':
    unittest.main()
