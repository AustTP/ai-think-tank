# Hermetic tests for the YOLO Colab bridge (2026-11-21). Everything that
# touches the network or the colab CLI is mocked; what is tested is the
# batch packing (PNG re-encode), the index-aligned row mapping, the
# budget/enable gates, and the soc integration points (pass-through from
# the youtube-frames endpoint).
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402


def _png_bytes(width=8, height=8):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (width, height), (200, 40, 40)).save(buf, 'PNG')
    return buf.getvalue()


class BatchZipPacking(unittest.TestCase):
    def _build(self, images):
        # The builder returns a plain path (the sync caller unlinks it in
        # its finally); tests clean up the same way.
        path = serve._yolo_build_batch_zip(images)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_pngs_are_reencoded_as_jpeg_members(self):
        with zipfile.ZipFile(self._build([_png_bytes(), _png_bytes()])) as zf:
            names = sorted(zf.namelist())
            self.assertEqual(names, ['frame-0000.jpg', 'frame-0001.jpg'])
            self.assertTrue(zf.read('frame-0000.jpg').startswith(b'\xff\xd8'))

    def test_bad_bytes_ride_along_as_png(self):
        raw = b'not-an-image'
        with zipfile.ZipFile(self._build([_png_bytes(), raw])) as zf:
            self.assertEqual(sorted(zf.namelist()),
                             ['frame-0000.jpg', 'frame-0001.png'])

    def test_index_names_preserve_order(self):
        with zipfile.ZipFile(self._build([_png_bytes()] * 3)) as zf:
            self.assertEqual(zf.namelist(),
                             ['frame-0000.jpg', 'frame-0001.jpg',
                              'frame-0002.jpg'])


class DetectSyncGates(unittest.TestCase):
    def test_disabled_by_flag(self):
        with unittest.mock.patch.object(serve, '_YOLO_DETECT_ENABLED', False):
            ok, payload = serve._yolo_detect_images_sync([_png_bytes()])
        self.assertFalse(ok)
        self.assertIn('disabled', payload['error'])

    def test_cli_missing(self):
        with unittest.mock.patch.object(serve, '_YOLO_DETECT_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', False):
            ok, payload = serve._yolo_detect_images_sync([_png_bytes()])
        self.assertFalse(ok)
        self.assertIn('colab CLI', payload['error'])

    def test_empty_batch_refused(self):
        with unittest.mock.patch.object(serve, '_YOLO_DETECT_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True):
            ok, payload = serve._yolo_detect_images_sync([])
        self.assertFalse(ok)
        self.assertIn('no frames', payload['error'])

    def test_budget_cap_blocks(self):
        with unittest.mock.patch.object(serve, '_YOLO_DETECT_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_budget_exceeded',
                                        return_value=True):
            ok, payload = serve._yolo_detect_images_sync([_png_bytes()])
        self.assertFalse(ok)
        self.assertIn('cap', payload['error'])


class DetectSyncRun(unittest.TestCase):
    def _run(self, cli_side_effect, images=None):
        images = images if images is not None else [_png_bytes(), _png_bytes()]

        def noop_provision(session=None, kind='gpu', account='primary'):
            return True, 'ok'

        with unittest.mock.patch.object(serve, '_YOLO_DETECT_ENABLED', True), \
                unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
                unittest.mock.patch.object(serve, '_colab_budget_exceeded',
                                           return_value=False), \
                unittest.mock.patch.object(serve, '_colab_compute_provision',
                                           side_effect=noop_provision), \
                unittest.mock.patch.object(serve, '_colab_cli',
                                           side_effect=cli_side_effect), \
                unittest.mock.patch.object(serve, '_accrue_colab_units'), \
                unittest.mock.patch.object(serve, 'log_action') as log:
            ok, payload = serve._yolo_detect_images_sync(
                images, model_slug='yolov8n.pt', conf=0.3)
        return ok, payload, log

    @staticmethod
    def _cli(rows, uploads_ok=True):
        def handler(*args, timeout=120, input=None, account=None):
            op = args[0]
            if op == 'upload':
                return (0, 'ok') if uploads_ok else (1, 'ftp dead')
            if op == 'exec':
                assert '__COLAB_DONE__' in input, 'template must signal done'
                assert 'YOLO_MODEL' in ' '.join(args), 'model env must be set'
                out = []
                for row in rows:
                    out.append(json.dumps({'i': row[0], 'f': f'frame-{row[0]:04d}.jpg',
                                           'd': row[1]}))
                out.append('__COLAB_DONE__')
                return 0, '\n'.join(out)
            return 0, ''
        return handler

    def test_happy_path_rows_align_by_index(self):
        # 'f' keys decoded from the runtime rows must map to the right slots.
        rows = [(1, [{'name': 'person'}])]
        ok, payload, log = self._run(self._cli(rows))
        self.assertTrue(ok)
        self.assertEqual([r['detections'] for r in payload['rows']],
                         [[], [{'name': 'person'}]])

    def test_missing_rows_pad_explicitly(self):
        ok, payload, log = self._run(self._cli([]))
        self.assertTrue(ok)
        self.assertEqual(payload['rows'], [{'detections': [], 'file': None}] * 2)

    def test_upload_failure_fails_closed_with_reason(self):
        ok, payload, log = self._run(self._cli([], uploads_ok=False))
        self.assertFalse(ok)
        self.assertIn('upload failed', payload['error'])

    def test_exec_failure_fails_closed_with_reason(self):
        def handler(*args, timeout=120, input=None, account=None):
            if args[0] == 'upload':
                return 0, 'ok'
            if args[0] == 'exec':
                return 1, 'boom'
            return 0, ''
        ok, payload, log = self._run(handler)
        self.assertFalse(ok)
        self.assertIn('YOLO exec failed', payload['error'])

    def test_max_frames_cap_enforced(self):
        seen = {}

        def handler(*args, timeout=120, input=None, account=None):
            if args[0] == 'upload':
                seen['zip'] = args[1]
                return 0, 'ok'
            if args[0] == 'exec':
                return 0, '__COLAB_DONE__'
            return 0, ''

        with unittest.mock.patch.object(serve, '_YOLO_DETECT_MAX_FRAMES', 3):
            ok, payload, log = self._run(handler, images=[_png_bytes()] * 7)
        self.assertTrue(ok)
        self.assertEqual(len(payload['rows']), 3)

    def test_log_action_records_pipeline_shape(self):
        ok, payload, log = self._run(
            self._cli([(0, [{'name': 'car'}])]))
        self.assertTrue(ok)
        self.assertEqual(log.call_args.args[0], 'system')
        self.assertEqual(log.call_args.args[1], 'yolo_detect_run')
        self.assertEqual(log.call_args.kwargs['authorized'], True)


if __name__ == '__main__':
    unittest.main()
