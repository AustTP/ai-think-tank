"""escalation_kinds.json registry: floors + operator-facing guidance for
escalation kinds are DATA (config-not-code, same pattern as api_services.json),
with the shipped Python tables as the fail-safe. The invariants that matter:

  * the file OVERRIDES the built-in per kind and per field;
  * a corrupt/unreadable file falls back to the built-in -- a broken edit can
    only restore today's behavior, never silently weaken a gate;
  * floors outside 0..1 are ignored loudly (built-in kept);
  * a kind whose bar should track the LIVE default keeps no pinned floor in
    the merged view -- the consumer still routes it through the
    calibration-adjusted threshold;
  * a kind only present in the file is a legitimate new kind and starts at
    the delegable default, never at 1.0."""
import core.jev
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402  (repo test convention)

_TMP_DIR = None
_PATCHER = None
_PATH_PERSIST = None


def setUpModule():
    global _TMP_DIR, _PATCHER, _PATH_PERSIST
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-kinds-test-')
    # The loader reads core.jev's OWN module global (serve's re-export is an
    # alias of the function, whose globals live in core.jev), so the test
    # redirect targets core.jev, exactly what a self-edit reload refreshes.
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
    )
    _PATCHER.start()
    _PATH_PERSIST = unittest.mock.patch.object(
        core.jev, 'ESCALATION_KINDS_PATH',
        os.path.join(_TMP_DIR, 'escalation_kinds.json'))
    _PATH_PERSIST.start()
    globals()['_PATH_PERSIST'] = _PATH_PERSIST  # stopped in tearDownModule
    serve.init_db()


def tearDownModule():
    _PATH_PERSIST.stop()
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _write_kinds(payload):
    import json
    with open(core.jev.ESCALATION_KINDS_PATH, 'w', encoding='utf-8') as fh:
        if isinstance(payload, str):
            fh.write(payload)
        else:
            json.dump(payload, fh)
    core.jev._KINDS_TABLE_CACHE = {'mtime': None, 'table': None}  # force reload


def _clear_kinds():
    try:
        os.remove(core.jev.ESCALATION_KINDS_PATH)
    except FileNotFoundError:
        pass
    core.jev._KINDS_TABLE_CACHE = {'mtime': None, 'table': None}


class EscalationKindsRegistry(unittest.TestCase):
    def tearDown(self):
        _clear_kinds()

    def test_shipped_file_matches_the_builtin_floors(self):
        # With the repo's real escalation_kinds.json, every floor the
        # composite-trust tests assert still holds: the file is the same
        # policy as the built-in, now editable without a code change.
        table = serve.escalation_kinds_effective()
        self.assertEqual(1.0, table['blocked command']['floor'])
        self.assertEqual(1.0, table['blocked pipeline step']['floor'])
        self.assertEqual(1.0, table['blocked browser action']['floor'])
        self.assertEqual(0.85, table['unsure safety decision']['floor'])
        self.assertEqual(0.95, table['owner safety decision']['floor'])
        self.assertEqual(0.85, table['dormancy window request']['floor'])
        # a kind that tracks the live default resolves through calibration
        self.assertEqual(serve._effective_safety_confidence(),
                         serve._escalation_floor('unresolved review requirement'))

    def test_file_overrides_builtin_floor(self):
        _write_kinds({'kinds': {'unsure safety decision': {'floor': 0.99}}})
        self.assertEqual(0.99, serve.escalation_kinds_effective()
                         ['unsure safety decision']['floor'])

    def test_corrupt_file_falls_back_to_builtin(self):
        _write_kinds('{"kinds": { oops')
        table = serve.escalation_kinds_effective()
        self.assertEqual(1.0, table['blocked command']['floor'])
        self.assertEqual(0.85, table['unsure safety decision']['floor'])

    def test_missing_file_falls_back_to_builtin(self):
        _clear_kinds()
        table = serve.escalation_kinds_effective()
        self.assertEqual(1.0, table['blocked command']['floor'])
        self.assertEqual(0.85, table['unsure safety decision']['floor'])

    def test_invalid_floor_is_ignored_loudly(self):
        # A non-numeric (or out-of-range) floor cannot weaken a gate: the
        # built-in applies. An IN-range floor is a legitimate operator
        # override and does apply (the file lives in the repo, code-reviewed).
        _write_kinds({'kinds': {'blocked command': {'floor': 'high'},
                                'unsure safety decision': {'floor': 0.5}}})
        table = serve.escalation_kinds_effective()
        self.assertEqual(1.0, table['blocked command']['floor'])  # builtin kept
        self.assertEqual(0.5, table['unsure safety decision']['floor'])  # override applied

    def test_floor_must_be_in_0_1(self):
        _write_kinds({'kinds': {'blocked command': {'floor': 1.5}}})
        self.assertEqual(1.0, serve.escalation_kinds_effective()
                         ['blocked command']['floor'])

    def test_new_file_only_kind_defaults_to_delegable_bar(self):
        _write_kinds({'kinds': {'vendor outage': {'impact': 'stalled'}}})
        entry = serve.escalation_kinds_effective()['vendor outage']
        self.assertEqual(serve.ESCALATION_DEFAULT_FLOOR, entry['floor'])
        self.assertEqual('stalled', entry['impact'])

    def test_guidance_overrides_per_field(self):
        _write_kinds({'kinds': {'personnel_strike': {
            'impact': 'Custom impact wording.'}}})
        impact, action = serve._escalation_guidance('personnel_strike')
        self.assertEqual('Custom impact wording.', impact)
        self.assertIn('Read the cited reports', action)  # shipped copy kept
        impact, action = serve._escalation_guidance('totally unknown kind')
        self.assertTrue(impact and action)  # generic default still returned


class EscalationFloorConsumer(unittest.TestCase):
    def test_blocked_command_floor_is_live_from_the_registry(self):
        # The consumer reads the MERGED table: a file override reaches the
        # auto-approval gate without a code change.
        _write_kinds({'kinds': {'blocked command': {'floor': 0.75}}})
        try:
            self.assertEqual(0.75, serve._escalation_floor('blocked command'))
        finally:
            _clear_kinds()
        self.assertEqual(1.0, serve._escalation_floor('blocked command'))
