"""The hourly village scan's tool authority (2026-11-24).

The scan template's instructions direct api_call (GET /api/ops/failures,
GET /api/ops/cards, POST /api/intent/issues) but api_call is commit-tier --
without an approvedTools grant on the minted card, every scan burned two
tool_policy_denied refusals plus a MEDIUM telegram alert before finishing
via library tools alone (task-35908 tool_policy_denied ×2 per scan). Tests
assert the grant rides the seed item and that the mint path forwards it."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import sim  # noqa: E402  (sim wires the world/village path)
from core.tool_policy import task_tool_policy  # noqa: E402


def _clean_state():
    return {
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True},
            {'id': 'nora', 'villageChief': True, 'chiefOfVillage': 'north'},
        ],
        'teams': [{'id': 'nora', 'directorId': 'nora'}],
        'workQueue': [],
    }


def _minted_items(qw):
    return [c for call in qw.call_args_list
            for c in (call.args[1] if len(call.args) > 1 else
                      (call.kwargs.get('items') or []))]


class ScanSeedCarriesGrant(unittest.TestCase):
    def test_seed_item_lists_api_call(self):
        self.assertIn('api_call',
                      sim._SCHEDULED_VILLAGE_SCAN_ITEM.get('approvedTools') or [])

    def test_ensure_by_id_seed_carries_grant(self):
        st = _clean_state()
        st['scheduledItems'] = []
        with mock.patch.object(sim, 'queue_work', return_value=1) as qw, \
                mock.patch.object(sim, 'serve', create=True):
            sim._scheduled_items_pass(st)
        self.assertTrue(st.get('_villageScanSeeded'))
        scans = [c for c in _minted_items(qw)
                 if 'village scan' in str(c.get('title') or '').lower()]
        self.assertTrue(scans, 'the seeded scan mints its card')
        for c in scans:
            self.assertIn('api_call', c.get('approvedTools') or [])

    def test_existing_item_not_clobbered(self):
        st = _clean_state()
        st['scheduledItems'] = [
            {'id': 'sch-rvp-village-scan', 'title': 'x', 'enabled': True,
             'ownerRole': 'regional-vp', 'cadence': 'hourly',
             'approvedTools': []}]
        with mock.patch.object(sim, 'queue_work', return_value=1), \
                mock.patch.object(sim, 'serve', create=True):
            sim._scheduled_items_pass(st)
        scan = next(i for i in st['scheduledItems']
                    if i.get('id') == 'sch-rvp-village-scan')
        self.assertEqual(scan.get('approvedTools'), [],
                         'ensure-by-id must keep an operator-edited grant list')


class MintForwardsGrant(unittest.TestCase):
    def test_queue_work_and_policy_allow_api_call(self):
        st = _clean_state()
        st['scheduledItems'] = [dict(sim._SCHEDULED_VILLAGE_SCAN_ITEM)]
        with mock.patch.object(sim, 'queue_work', return_value=1) as qw, \
                mock.patch.object(sim, 'serve', create=True):
            sim._scheduled_items_pass(st)
        scans = [c for c in _minted_items(qw)
                 if 'village scan' in str(c.get('title') or '').lower()]
        self.assertTrue(scans, 'the scan minted a card')
        for c in scans:
            self.assertIn('api_call', c.get('approvedTools') or [])
            # task_tool_policy must admit the granted tool and stay closed
            # to unrelated commit tools.
            policy = task_tool_policy(c)
            self.assertTrue(policy('api_call'))
            self.assertFalse(policy('send_email'))


if __name__ == '__main__':
    unittest.main()
