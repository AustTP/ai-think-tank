"""Line-coverage tests for serve.py's failure ledger, taxonomy sort, rule
mining, and the two-line escalation handoff.

The draft-review rule loop (DESIGN.md "Failure taxonomy and rule mining"):
compare -> sort (four buckets) -> write a rule -> add a test. These tests pin
the serve.py half: the deterministic + Jev classifier (`_classify_failure`),
the durable ledger (`_record_failure`), the weekly aggregation into PROPOSED
rules (`_mine_rule_proposals`), and the "what I checked / look here first"
lines on escalation emails.

Same isolation contract as tests/test_serve.py: every real file path is
redirected into a throwaway temp dir, including the module-level DERIVED
paths (FAILURES_PATH, RULE_PROPOSALS_PATH, ESCALATIONS_PATH) computed at
import time from the real THINK_TANK_DIR -- without that, _record_failure /
create_escalation would touch the real ~/ai-village-template tree. Network /
Jev seams are patched per test.

Run (isolated coverage file):
  cd /Users/poole86/ai-village-template/world
  COVERAGE_FILE=/tmp/cov_failure_ledger.coverage python3 -m coverage run --source=serve tests/test_failure_ledger.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

# Same bridge every other test file uses: the coverage script runs each
# test_*.py from the repo root, so without this `import serve` resolves to
# a missing module and the whole file silently covers nothing.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve

_FAILURE_TYPES = serve.FAILURE_TYPES
_MODULE_TMP_DIR = None
_MODULE_PATCHER = None
_EXTRA_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER, _EXTRA_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='think-tank-failure-ledger-')
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
    )
    _MODULE_PATCHER.start()
    # Derived paths computed at import time from the REAL THINK_TANK_DIR --
    # redirect them too (see the module docstring).
    _EXTRA_PATCHER = unittest.mock.patch.multiple(
        serve,
        FAILURES_PATH=os.path.join(_MODULE_TMP_DIR, 'failures.json'),
        RULE_PROPOSALS_PATH=os.path.join(_MODULE_TMP_DIR, 'rule_proposals.json'),
        ESCALATIONS_PATH=os.path.join(_MODULE_TMP_DIR, 'escalations.json'),
    )
    _EXTRA_PATCHER.start()


def tearDownModule():
    _EXTRA_PATCHER.stop()
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


def _read(path):
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return None


class _LedgerTestCase(unittest.TestCase):
    """Per-test isolation: the ledger + proposal files are shared temp paths,
    so each test starts clean (they accumulate across a test otherwise)."""

    def setUp(self):
        for p in (serve.FAILURES_PATH, serve.RULE_PROPOSALS_PATH):
            if os.path.exists(p):
                os.remove(p)


class ClassifyFailureRule(unittest.TestCase):
    def test_money_hints_map_to_factual_error(self):
        for text in ('the budget is $50', 'price 30', 'cost of 10 dollars', 'under budget'):
            self.assertEqual(serve._classify_failure_rule(text), 'factual_error', text)

    def test_style_hints_map_to_style(self):
        for text in ('please rephrase the opening', 'the tone is off', 'awkward wording', 'too verbose'):
            self.assertEqual(serve._classify_failure_rule(text), 'style', text)

    def test_no_signal_returns_none(self):
        self.assertIsNone(serve._classify_failure_rule('the section misses the point entirely'))


class FailureTaxonomyDecider(unittest.TestCase):
    def _decide(self, choice, confidence, raise_call=False):
        def call(*a, **k):
            if raise_call:
                raise RuntimeError('model down')
            return {'answers': {'q': {'choice': choice, 'confidence': confidence}},
                    'usage': {'cost': 0.01}}
        with unittest.mock.patch.object(serve, '_jev_model', return_value='jev-1'), \
             unittest.mock.patch.object(serve, '_effective_review_grade_confidence', return_value=0.6), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync', side_effect=call):
            return serve._failure_taxonomy_decider_default('some draft text')

    def test_high_confidence_choice_passes_through(self):
        for ftype in _FAILURE_TYPES:
            choice, confidence = self._decide(ftype, 0.9)
            self.assertEqual((choice, confidence), (ftype, 0.9))

    def test_unknown_choice_fails_closed_to_style(self):
        self.assertEqual(self._decide('shiny', 0.9)[0], 'style')

    def test_low_confidence_fails_closed_to_style(self):
        choice, confidence = self._decide('factual_error', 0.4)
        self.assertEqual(choice, 'style')
        self.assertEqual(confidence, 0.4)

    def test_failed_call_fails_closed_to_style(self):
        self.assertEqual(self._decide(None, None, raise_call=True), ('style', 0.0))


class ClassifyFailure(unittest.TestCase):
    def test_deterministic_short_circuit_never_spends_jev(self):
        with unittest.mock.patch.object(serve, '_failure_taxonomy_decider',
                                        side_effect=AssertionError('must not be called')) as decider:
            self.assertEqual(serve._classify_failure('the price is $40'), 'factual_error')
        decider.assert_not_called()

    def test_jev_fallback_when_no_deterministic_signal(self):
        with unittest.mock.patch.object(serve, '_failure_taxonomy_decider',
                                        return_value=('missing_information', 0.9)):
            self.assertEqual(serve._classify_failure('the section misses the point entirely'),
                             'missing_information')


class RecordFailure(_LedgerTestCase):
    def test_appends_record_with_normalized_type_and_truncation(self):
        failure_id = serve._record_failure('factual_error', 'x' * 900, rule_hint='h' * 300,
                                           section='s' * 200, input_text='in', agent_id='ada')
        failures = serve._load_failures()
        self.assertEqual(len(failures), 1)
        record = failures[0]
        self.assertEqual(record['id'], failure_id)
        self.assertEqual(record['type'], 'factual_error')
        self.assertEqual(len(record['summary']), 500)
        self.assertEqual(len(record['ruleHint']), 200)
        self.assertEqual(len(record['section']), 120)
        self.assertEqual(record['input'], 'in')
        self.assertEqual(record['agentId'], 'ada')

    def test_invalid_type_normalized_to_style(self):
        serve._record_failure('nonsense_type', 'summary')
        self.assertEqual(serve._load_failures()[0]['type'], 'style')

    def test_ledger_bounded_to_tail(self):
        for i in range(serve.FAILURE_MAX_RECORDS + 10):
            serve._record_failure('style', f'summary {i}')
        failures = serve._load_failures()
        self.assertEqual(len(failures), serve.FAILURE_MAX_RECORDS)
        self.assertEqual(failures[-1]['summary'], f'summary {serve.FAILURE_MAX_RECORDS + 9}')
        self.assertEqual(failures[0]['summary'], 'summary 10')


class RuleTextFor(unittest.TestCase):
    def test_builds_instruction_from_type_and_hint(self):
        text = serve._rule_text_for('factual_error', 'quote the price')
        self.assertIn('Never ship work', text)
        self.assertIn('has no source', text)
        self.assertIn('quote the price', text)

    def test_unknown_type_uses_generic_label(self):
        self.assertIn('recurring issue', serve._rule_text_for('bogus', 'x'))


class MineRuleProposals(_LedgerTestCase):
    def _seed(self, pairs):
        for ftype, hint in pairs:
            serve._record_failure(ftype, hint, rule_hint=hint)

    def test_recurring_pattern_becomes_proposal_with_rule_and_fixture(self):
        self._seed([('factual_error', 'quote the price'),
                    ('factual_error', 'quote the price')])
        created = serve._mine_rule_proposals(now_ts=1000.0)
        self.assertEqual(len(created), 1)
        proposal = created[0]
        self.assertEqual(proposal['type'], 'factual_error')
        self.assertEqual(proposal['ruleHint'], 'quote the price')
        self.assertEqual(proposal['count'], 2)
        self.assertEqual(proposal['status'], 'pending')
        self.assertIn('quote the price', proposal['rule'])
        self.assertEqual(proposal['fixture']['issue'], 'quote the price')
        self.assertIn('rule-', proposal['id'])

    def test_below_recurrence_no_proposal(self):
        self._seed([('style', 'rephrase the opening')])
        self.assertEqual(serve._mine_rule_proposals(), [])

    def test_already_proposed_pattern_deduped(self):
        self._seed([('factual_error', 'quote the price'),
                    ('factual_error', 'quote the price')])
        serve._mine_rule_proposals()
        self._seed([('factual_error', 'quote the price'),
                    ('factual_error', 'quote the price')])
        self.assertEqual(serve._mine_rule_proposals(), [])

    def test_records_without_rule_hint_ignored(self):
        serve._record_failure('style', 'no hint here', rule_hint='')
        serve._record_failure('style', 'still no hint', rule_hint='  ')
        self.assertEqual(serve._mine_rule_proposals(), [])

    def test_custom_recurrence_threshold(self):
        self._seed([('style', 'verbose copy'), ('style', 'verbose copy')])
        self.assertEqual(serve._mine_rule_proposals(recurrence=3), [])

    def test_proposals_bounded(self):
        for i in range(serve.RULE_PROPOSALS_MAX + 5):
            hint = f'recurring hint {i}'
            serve._record_failure('style', hint, rule_hint=hint)
            serve._record_failure('style', hint, rule_hint=hint)
        serve._mine_rule_proposals(now_ts=2000.0)
        self.assertEqual(len(serve._load_rule_proposals()), serve.RULE_PROPOSALS_MAX)


class CreateEscalationTwoLineHandoff(unittest.TestCase):
    def test_email_carries_two_line_handoff(self):
        sent = {}
        with unittest.mock.patch.object(serve, '_load_escalations', return_value={}), \
             unittest.mock.patch.object(serve, '_save_escalations', lambda d: None), \
             unittest.mock.patch.object(serve, 'ESCALATION_BASE_URL', 'http://x'), \
             unittest.mock.patch.object(serve, '_send_escalation_email_sync',
                                        side_effect=lambda s, b: sent.update(subject=s, body=b)):
            serve.create_escalation('unresolved review requirement', 'Q?',
                                    what_checked='Graded 3 requirements', look_first='Q?')
        self.assertIn('What I checked: Graded 3 requirements', sent['body'])
        self.assertIn('Look here first: Q?', sent['body'])

    def test_defaults_when_lines_empty(self):
        sent = {}
        with unittest.mock.patch.object(serve, '_load_escalations', return_value={}), \
             unittest.mock.patch.object(serve, '_save_escalations', lambda d: None), \
             unittest.mock.patch.object(serve, 'ESCALATION_BASE_URL', 'http://x'), \
             unittest.mock.patch.object(serve, '_send_escalation_email_sync',
                                        side_effect=lambda s, b: sent.update(subject=s, body=b)):
            serve.create_escalation('kind', 'the question')
        self.assertIn('What I checked: (see question)', sent['body'])
        self.assertIn('Look here first: the question above', sent['body'])


class RuleProposalsEndpoint(_LedgerTestCase):
    def _request(self):
        return unittest.mock.MagicMock(cookies={})

    def test_player_reads_proposals(self):
        serve._record_failure('style', 'rephrase it', rule_hint='rephrase it')
        serve._record_failure('style', 'rephrase it', rule_hint='rephrase it')
        serve._mine_rule_proposals()
        with unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            resp = asyncio.run(serve.rule_proposals_get(self._request()))
        import json as _json
        data = _json.loads(resp.body)
        self.assertEqual(len(data['proposals']), 1)

    def test_unauthenticated_denied(self):
        with unittest.mock.patch.object(serve, 'verify_session', return_value=False):
            resp = asyncio.run(serve.rule_proposals_get(self._request()))
        self.assertEqual(resp.status_code, 403)


if __name__ == '__main__':
    unittest.main()
