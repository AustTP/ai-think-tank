"""Local model tiers (operator, 2026-11-26): repos/ML runtimes as additional
tiers routed to instead of an LLM when the job fits. Registry is
world/local_models.json; lanes are health-checked, cooled down on failure,
and always leave the LLM band underneath as the fallback. The OCR lane
answers a YES/NO visual check ONLY with positive evidence.
"""
import base64
import os
import shutil
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402

# Captured BEFORE any test patches shutil.which (serve.shutil IS shutil; a
# lambda calling shutil.which would recurse into its own patch).
_REAL_WHICH = shutil.which


def _fake_which(name):
    return _REAL_WHICH(name) or f'/usr/bin/{name}'


def _reset_state():
    serve._LOCAL_MODEL_STATE.update({'cache': None, 'cacheAt': 0.0,
                                     'ready': {}, 'cooldownUntil': {},
                                     'cacheMtime': None})


class LocalModelRegistry(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.addCleanup(_reset_state)

    def test_registry_ships_a_live_ocr_entry(self):
        cfg = serve._local_model_cfg('ocr')
        self.assertTrue(cfg)
        self.assertEqual(cfg.get('engine'), 'tesseract')
        self.assertTrue(cfg.get('enabled'))

    def test_missing_binary_lane_off(self):
        cfg = serve._local_model_cfg('ocr')
        with unittest.mock.patch.object(serve.shutil, 'which', return_value=None):
            self.assertFalse(serve._local_model_ready('ocr'))
        # enabled=False in the registry also keeps the lane off.
        with unittest.mock.patch.object(serve, '_local_model_cfg',
                                        return_value={**cfg, 'enabled': False}):
            self.assertFalse(serve._local_model_ready('ocr'))
        with unittest.mock.patch.object(serve, '_local_model_cfg',
                                        return_value=None):
            self.assertFalse(serve._local_model_ready('ocr'))

    def test_unknown_kind_lane_off(self):
        self.assertFalse(serve._local_model_ready('nosuchkind'))


class LocalOcrEngine(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.addCleanup(_reset_state)
        # Prove the lane is live: pretend tesseract is on PATH.
        self._which = unittest.mock.patch.object(
            serve.shutil, 'which', _fake_which)
        self._which.start()
        self.addCleanup(self._which.stop)

    def test_ocr_runs_engine_and_returns_text(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return unittest.mock.Mock(returncode=0, stdout='  page text  ',
                                      stderr='')

        with unittest.mock.patch.object(serve.subprocess, 'run', fake_run), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            text = serve._local_ocr_text(b'png-bytes')
        self.assertEqual(text, 'page text')
        self.assertTrue(calls and calls[0][0] == 'tesseract')
        # $0 spend lands in the ledger; no row means the tier-value math
        # silently gains a free win.
        log.assert_called_once()
        self.assertEqual(log.call_args[0][1], 'local_model_used')

    def test_engine_failure_returns_none_and_cooldowns(self):
        with unittest.mock.patch.object(
                serve.subprocess, 'run',
                unittest.mock.Mock(side_effect=RuntimeError('boom'))):
            self.assertIsNone(serve._local_ocr_text(b'png-bytes'))
        # Cooldown: readiness flips off even with the binary present.
        self.assertFalse(serve._local_model_ready('ocr'))

    def test_nonzero_exit_returns_none(self):
        with unittest.mock.patch.object(
                serve.subprocess, 'run',
                lambda *a, **k: unittest.mock.Mock(returncode=1, stdout='',
                                                   stderr='err')):
            self.assertIsNone(serve._local_ocr_text(b'png-bytes'))

    def test_empty_output_returns_none(self):
        with unittest.mock.patch.object(
                serve.subprocess, 'run',
                lambda *a, **k: unittest.mock.Mock(returncode=0, stdout='   ',
                                                   stderr='')):
            self.assertIsNone(serve._local_ocr_text(b'png-bytes'))

    def test_no_bytes_short_circuits(self):
        self.assertIsNone(serve._local_ocr_text(None))
        self.assertIsNone(serve._local_ocr_text(b''))


class LocalOcrVisualAnswer(unittest.TestCase):
    QUESTION = ('Does this screenshot show: total $12.50? '
                'Answer exactly YES or NO on the first line, then one sentence '
                'of visual evidence.')

    def setUp(self):
        _reset_state()
        self.addCleanup(_reset_state)
        self._which = unittest.mock.patch.object(
            serve.shutil, 'which', _fake_which)
        self._which.start()
        self.addCleanup(self._which.stop)

    def _ocr(self, text):
        return unittest.mock.patch.object(serve, '_local_ocr_text',
                                          lambda _b: text)

    def test_positive_evidence_answers_yes(self):
        b64 = base64.b64encode(b'png').decode()
        with self._ocr('Invoice\nTOTAL $12.50\nthanks'), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            out = serve._local_ocr_visual_answer(b64, 'total $12.50')
        self.assertEqual(out['ok'], True)
        self.assertTrue(out['reply'].strip().lower().startswith('yes'))
        # The ledger entry comes from the engine (_local_ocr_text), not the
        # answerer; one entry per local answer, never two.
        log.assert_not_called()

    def test_absent_phrase_returns_none_for_vision_fallback(self):
        b64 = base64.b64encode(b'png').decode()
        with self._ocr('some other page'):
            self.assertIsNone(serve._local_ocr_visual_answer(b64, 'total $12.50'))

    def test_empty_ocr_returns_none(self):
        b64 = base64.b64encode(b'png').decode()
        with self._ocr(''):
            self.assertIsNone(serve._local_ocr_visual_answer(b64, 'total $12.50'))

    def test_lane_off_returns_none(self):
        b64 = base64.b64encode(b'png').decode()
        with unittest.mock.patch.object(serve.shutil, 'which', return_value=None):
            self.assertIsNone(serve._local_ocr_visual_answer(b64, 'total $12.50'))

    def test_bad_base64_returns_none(self):
        with self._ocr('x'):
            self.assertIsNone(serve._local_ocr_visual_answer('!!!not-b64', 'x'))

    def test_no_expect_returns_none(self):
        b64 = base64.b64encode(b'png').decode()
        with self._ocr('x'):
            self.assertIsNone(serve._local_ocr_visual_answer(b64, ''))


class VisionReviewLocalLane(unittest.TestCase):
    """_vision_review_base64: the local OCR lane answers only the fixed
    expect_visual question shape and only with positive evidence; every
    other path keeps the LLM vision tier as the arbiter."""

    def setUp(self):
        _reset_state()
        self.addCleanup(_reset_state)

    def _run(self, question, ocr_text=None, local=None):
        import content
        vision_calls = []

        def fake_http(method, base, path, body, key, timeout=None):
            vision_calls.append(body.get('model'))
            return {'reply': 'NO\nvision'}

        patches = [
            unittest.mock.patch.object(serve, '_vision_tier_slug',
                                       return_value='vision-slug'),
            unittest.mock.patch.object(serve, 'VISION_ANTI_INSTRUCTION_GUARD', 'g'),
            unittest.mock.patch.object(serve, '_http_json', side_effect=fake_http),
            unittest.mock.patch.object(serve, '_local_ocr_visual_answer',
                                       local),
        ]
        for p in patches:
            p.start()
        try:
            out = content._vision_review_base64('a', 'k', 't', 'b64', question)
        finally:
            mock_stop = unittest.mock.patch.stopall
            mock_stop()
        return out, vision_calls

    Q = ('Does this screenshot show: invoice ready? '
         'Answer exactly YES or NO on the first line, then one sentence '
         'of visual evidence.')

    def test_local_yes_short_circuits_vision(self):
        out, calls = self._run(self.Q, local=lambda b64, expect: {
            'ok': True, 'reply': 'YES\nlocal'})
        self.assertEqual(out['reply'], 'YES\nlocal')
        self.assertEqual(calls, [])

    def test_local_none_falls_through_to_vision(self):
        out, calls = self._run(self.Q, local=lambda b64, expect: None)
        self.assertEqual(out['ok'], True)
        self.assertEqual(calls, ['vision-slug'])

    def test_open_question_skips_local_lane(self):
        out, calls = self._run('Describe this page.', local=unittest.mock.Mock())
        self.assertEqual(out['ok'], True)
        self.assertEqual(calls, ['vision-slug'])
        self.assertFalse(getattr(
            self._run.__wrapped__ if hasattr(self._run, '__wrapped__') else
            unittest.mock.Mock(), 'called', False))


if __name__ == '__main__':
    unittest.main()
