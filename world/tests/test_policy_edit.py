# Hermetic tests for the Phase 3 policy externalization: the 24 serve
# statics that became operator-policy keys (world/operator_policy.json +
# config.OPERATOR_POLICY_DEFAULT), the AGENT_EDITABLE_POLICY_KEYS allowlist,
# and serve._policy_edit_apply -- the governed agent lane that reviews one
# key/value change through the JEV quorum, writes the JSON atomically, and
# rebinds every bound module without a restart.
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import serve  # noqa: E402

# The Phase 3 key set: everywhere it appears, it is complete and consistent.
PHASE3_KEYS = frozenset({
    'dbBackupKeep', 'dbBackupIntervalS', 'logRetentionDaysDefault',
    'logPruneIntervalS', 'logPruneMaxRows',
    'modelTierRefreshIntervalS', 'tierPriceWatchIntervalS',
    'tierRecheckCooldownS', 'tierGateMinConfidence',
    'failureMaxRecords', 'successMaxRecords', 'ruleProposalsMax',
    'successProposalsMax', 'ruleMinRecurrence', 'successMinRecurrence',
    'memoryMaxAgeDays', 'memoryDecayFactor', 'memoryKeepCount',
    'maxDownloadBytes', 'youtubeTranscriptMaxChars',
    'youtubeTranscriptMaxSegments', 'agentDirSyncIntervalS',
    'peerReviewMinLookbackS', 'peerReviewSignalStaleS',
})
HYGIENE_KEYS = frozenset({
    'dbBackupKeep', 'dbBackupIntervalS', 'logRetentionDaysDefault',
    'logPruneIntervalS', 'logPruneMaxRows',
})


class Phase3Iso(unittest.TestCase):
    """Data wiring: defaults, schema, shipped JSON, agent allowlist."""

    def test_defaults_schema_and_shipped_json_agree(self):
        for key in PHASE3_KEYS:
            self.assertIn(key, config.OPERATOR_POLICY_DEFAULT, key)
            self.assertIn(key, config._OPERATOR_SCHEMA, key)
            kind, lo, hi = config._OPERATOR_SCHEMA[key]
            self.assertIn(kind, ('int', 'float'), key)
            self.assertGreater(hi, lo, key)
            # The shipped file self-documents: its value equals the code
            # default, so a fresh clone boots identical either way.
            self.assertEqual(
                config.get_operator_policy()[key],
                config.OPERATOR_POLICY_DEFAULT[key],
                f'shipped operator_policy.json drifted from the code '
                f'default for {key}')

    def test_agent_allowlist_shape(self):
        # Allowlist keys must exist, must be validated, and the DB/log
        # hygiene keys stay operator-only on purpose (audit-trail shape).
        self.assertLessEqual(config.AGENT_EDITABLE_POLICY_KEYS,
                             set(config._OPERATOR_SCHEMA))
        self.assertEqual(
            config.AGENT_EDITABLE_POLICY_KEYS,
            PHASE3_KEYS - HYGIENE_KEYS - config.PLAYER_ONLY_POLICY_KEYS)


class _TempPolicyEdit(unittest.TestCase):
    """A governed apply against a temp copy of the shipped policy file."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think-tank-policy-edit-test-')
        self._policy = os.path.join(self._tmp, 'operator_policy.json')
        shutil.copyfile(config.OPERATOR_POLICY_PATH, self._policy)
        patchers = [
            unittest.mock.patch.object(config, 'OPERATOR_POLICY_PATH',
                                       self._policy),
            unittest.mock.patch.object(serve, 'log_action'),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        config._OPERATOR_CACHE['mtime'] = None
        self.addCleanup(config.reload_policy_config)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _review(self, decision='approve', confidence=0.9):
        calls = []

        def fake_review(instructions, criteria):
            calls.append((instructions, criteria))
            return decision, confidence, 0.0

        patcher = unittest.mock.patch.object(
            serve, '_jev_quorum_choice_sync', side_effect=fake_review)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def _file_value(self, key):
        with open(self._policy, encoding='utf-8') as fh:
            return json.load(fh).get(key)


class GovernedFlow(_TempPolicyEdit, unittest.TestCase):
    def test_unknown_and_operator_only_keys_fail_closed(self):
        self._review()
        ok, payload = serve._policy_edit_apply(
            'ag-x', 'notARealKey', 1, 'because')
        self.assertFalse(ok)
        self.assertIn('not an operator-policy key', payload['error'])
        ok, payload = serve._policy_edit_apply(
            'ag-x', 'maxTotalAgents', 10, 'because')
        self.assertFalse(ok)
        self.assertIn('operator-only', payload['error'])
        ok, payload = serve._policy_edit_apply(
            'ag-x', 'tierGateMinConfidence', 0.9, 'lower the gate')
        self.assertFalse(ok)
        self.assertIn('operator-only', payload['error'])

    def test_out_of_range_value_fails_closed(self):
        self._review()
        ok, payload = serve._policy_edit_apply(
            'ag-x', 'memoryDecayFactor', 2.0, 'looser floor')
        self.assertFalse(ok)
        self.assertIn('must be a float between', payload['error'])
        ok, payload = serve._policy_edit_apply(
            'ag-x', 'peerReviewMinLookbackS', 'nope', 'because')
        self.assertFalse(ok)
        self.assertIn('must be a int between', payload['error'])

    def test_review_reject_changes_nothing(self):
        self._review(decision='reject', confidence=0.8)
        before = serve.MEMORY_KEEP_COUNT
        ok, payload = serve._policy_edit_apply(
            'ag-x', 'memoryKeepCount', 99, 'keep more memories')
        self.assertFalse(ok)
        self.assertIn('reject', payload['error'])
        self.assertEqual(serve.MEMORY_KEEP_COUNT, before)
        self.assertEqual(self._file_value('memoryKeepCount'), 20)

    def test_approve_applies_reloads_and_preserves_the_file(self):
        calls = self._review()
        ok, payload = serve._policy_edit_apply(
            'ag-x', 'memoryKeepCount', 7, 'longer memory for the tank')
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._file_value('memoryKeepCount'), 7)
        # _notes survived the rewrite (read-modify-write, not a reset).
        with open(self._policy, encoding='utf-8') as fh:
            data = json.load(fh)
        self.assertIn('_notes', data)
        self.assertEqual(len(data), 108)  # 108 shipped keys + none lost
        self.assertEqual(payload['value'], 7)
        self.assertEqual(payload['previous'], 20)
        # Every bound module rebinds without a restart.
        self.assertEqual(serve.MEMORY_KEEP_COUNT, 7)
        self.assertEqual(config.OPERATOR_POLICY['memoryKeepCount'], 7)

    def test_review_transport_failure_fails_closed(self):
        patcher = unittest.mock.patch.object(
            serve, '_jev_quorum_choice_sync',
            side_effect=RuntimeError('backend down'))
        patcher.start()
        self.addCleanup(patcher.stop)
        ok, payload = serve._policy_edit_apply(
            'ag-x', 'memoryKeepCount', 7, 'because')
        self.assertFalse(ok)
        self.assertIn('review transport failed', payload['error'])
        self.assertEqual(self._file_value('memoryKeepCount'), 20)

    def test_corrupt_policy_file_fails_closed(self):
        self._review()
        with open(self._policy, 'w', encoding='utf-8') as fh:
            fh.write('this is not json')
        ok, payload = serve._policy_edit_apply(
            'ag-x', 'memoryKeepCount', 7, 'because')
        self.assertFalse(ok)
        self.assertIn('cannot read the policy file', payload['error'])


if __name__ == '__main__':
    unittest.main()
