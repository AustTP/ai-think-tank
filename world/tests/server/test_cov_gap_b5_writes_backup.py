"""Coverage-gap tests for serve_parts 010, 020, 030 and 040."""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import asyncio
import builtins
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

import serve


class _CM:
    def __init__(self, conn=None, exc=None):
        self.conn = conn
        self.exc = exc

    def __enter__(self):
        if self.exc:
            raise self.exc
        return self.conn

    def __exit__(self, *exc):
        return False


class _TempMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cov-b5-writes-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.patcher = mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'db.sqlite'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
            ESCALATIONS_PATH=os.path.join(self.tmp, 'escalations.json'),
            BROWSER_PROFILE_DIR=os.path.join(self.tmp, 'profiles'),
            HTTP_CACHE_DIR=os.path.join(self.tmp, '.http_cache'),
            BROWSE_BLOCK_LOG=os.path.join(self.tmp, 'blocks.jsonl'),
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        serve.init_db()


class ExternalWriteOnceTest(_TempMixin, unittest.TestCase):
    def test_ledger_unavailable(self):
        with mock.patch.object(serve, '_db', side_effect=RuntimeError('down')):
            result, error, replayed = serve.external_write_once(
                'k', 'key', lambda: ('ok', None))
        self.assertIsNone(result)
        self.assertIn('idempotency ledger unavailable', error)
        self.assertFalse(replayed)

    def test_failed_fn_delete_also_fails(self):
        conn = mock.MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with mock.patch.object(
                serve, '_db',
                side_effect=[_CM(conn), _CM(conn), _CM(exc=RuntimeError('delete'))]):
            result, error, replayed = serve.external_write_once(
                'k', 'key', lambda: (None, 'boom'))
        self.assertIsNone(result)
        self.assertEqual(error, 'boom')
        self.assertFalse(replayed)

    def test_update_ledger_fails_after_write(self):
        conn = mock.MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with mock.patch.object(
                serve, '_db',
                side_effect=[_CM(conn), _CM(conn), _CM(exc=RuntimeError('update'))]):
            result, error, replayed = serve.external_write_once(
                'k', 'key', lambda: ({'ok': True}, None))
        self.assertIsNone(result)
        self.assertIn('idempotency ledger failed', error)
        self.assertFalse(replayed)


class CheckpointTest(_TempMixin, unittest.TestCase):
    def test_collector_checkpoint_guards_and_errors(self):
        self.assertFalse(serve.set_collector_checkpoint('', 'x'))
        with mock.patch.object(serve, '_db', side_effect=RuntimeError('down')):
            self.assertFalse(serve.set_collector_checkpoint('c', 'x'))
        self.assertIsNone(serve.get_collector_checkpoint(''))
        with mock.patch.object(serve, '_db', side_effect=RuntimeError('down')):
            self.assertIsNone(serve.get_collector_checkpoint('c'))

    def test_task_checkpoint_guards_and_errors(self):
        self.assertFalse(serve.save_task_checkpoint('', 'phase', {}))
        with mock.patch.object(serve, '_db', side_effect=RuntimeError('down')):
            self.assertFalse(serve.save_task_checkpoint('t', 'phase', {}))
        self.assertIsNone(serve.load_task_checkpoint(''))
        self.assertTrue(serve.save_task_checkpoint('t-list', 'phase', [1, 2]))
        self.assertIsNone(serve.load_task_checkpoint('t-list'))
        with mock.patch.object(serve, '_db', side_effect=RuntimeError('down')):
            self.assertIsNone(serve.load_task_checkpoint('t'))
        self.assertFalse(serve.clear_task_checkpoint(''))
        with mock.patch.object(serve, '_db', side_effect=RuntimeError('down')):
            self.assertFalse(serve.clear_task_checkpoint('t'))


class ModelTierBootBackfillTest(unittest.TestCase):
    def test_refreshes_when_table_empty(self):
        conn = mock.MagicMock()
        conn.execute.return_value.fetchone.return_value = (0,)
        refresh = mock.AsyncMock()
        with mock.patch('asyncio.sleep', new=mock.AsyncMock()), \
             mock.patch.object(serve, '_db', return_value=_CM(conn)), \
             mock.patch.object(serve, '_ensure_tier_fallbacks_table'), \
             mock.patch.object(serve, '_tier_refresh_logged', refresh):
            asyncio.run(serve._model_tier_boot_backfill())
        refresh.assert_awaited_once()

    def test_skips_when_table_populated(self):
        conn = mock.MagicMock()
        conn.execute.return_value.fetchone.return_value = (3,)
        refresh = mock.AsyncMock()
        with mock.patch('asyncio.sleep', new=mock.AsyncMock()), \
             mock.patch.object(serve, '_db', return_value=_CM(conn)), \
             mock.patch.object(serve, '_ensure_tier_fallbacks_table'), \
             mock.patch.object(serve, '_tier_refresh_logged', refresh):
            asyncio.run(serve._model_tier_boot_backfill())
        refresh.assert_not_awaited()

    def test_failure_is_logged(self):
        with mock.patch('asyncio.sleep', new=mock.AsyncMock()), \
             mock.patch.object(serve, '_db', side_effect=RuntimeError('down')):
            asyncio.run(serve._model_tier_boot_backfill())


class DormancyWindowLoopTest(unittest.TestCase):
    def _run(self, iterations, selfedit, dormant=True):
        calls = {'n': 0}

        async def fake_sleep(_):
            calls['n'] += 1
            if calls['n'] > iterations:
                raise asyncio.CancelledError()

        with mock.patch('asyncio.sleep', side_effect=fake_sleep), \
             mock.patch.object(serve, '_selfedit', selfedit), \
             mock.patch.object(serve, '_dormant', return_value=dormant):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._dormancy_window_loop(poll_s=0))

    def test_window_tick_fired_and_error(self):
        selfedit = mock.MagicMock()
        selfedit.window_tick.side_effect = [['w1'], RuntimeError('tick')]
        self._run(2, selfedit, dormant=False)

    def test_apply_pass_success_and_error(self):
        selfedit = mock.MagicMock()
        selfedit.window_tick.return_value = []
        selfedit.apply_pass.side_effect = [['edit'], RuntimeError('apply')]
        selfedit.pending_validator_windows.return_value = []
        self._run(2, selfedit, dormant=True)

    def test_validator_queue_read_fails(self):
        selfedit = mock.MagicMock()
        selfedit.window_tick.return_value = []
        selfedit.apply_pass.return_value = []
        selfedit.pending_validator_windows.side_effect = RuntimeError('queue')
        self._run(1, selfedit, dormant=True)

    def test_validator_turns_decide_and_fail(self):
        selfedit = mock.MagicMock()
        selfedit.window_tick.return_value = []
        selfedit.apply_pass.return_value = []
        selfedit.pending_validator_windows.return_value = ['w1', 'w2']
        selfedit.run_validator_review.side_effect = [
            (True, {'status': 'approved'}), RuntimeError('review')]
        self._run(1, selfedit, dormant=True)


class FailureNoticeTest(_TempMixin, unittest.TestCase):
    def test_repeated_failure_body(self):
        subject, body = serve._failure_notice_message(
            'a1', 'act', {}, 'Label', 'high', count=2)
        self.assertIn('repeated 2 times', body)

    def test_send_failure_notice_swallows_error(self):
        with mock.patch.object(serve, 'send_player_telegram_sync',
                               side_effect=RuntimeError('down')):
            serve._send_failure_notice('s', 'b')

    def test_log_action_survives_notification_failure(self):
        with mock.patch.object(serve, '_maybe_notify_failure',
                               side_effect=RuntimeError('down')):
            serve.log_action('a1', 'act', {'x': 1})


class CachedYoutubeTranscriptTest(_TempMixin, unittest.TestCase):
    def test_no_video_id(self):
        self.assertIsNone(serve._cached_youtube_transcript('https://example.com/x'))

    def test_missing_file(self):
        self.assertIsNone(
            serve._cached_youtube_transcript('https://youtu.be/abcdefghijk'))

    def test_read_oserror(self):
        with mock.patch.object(serve.os.path, 'isfile', return_value=True), \
             mock.patch.object(builtins, 'open', side_effect=OSError('denied')):
            self.assertIsNone(
                serve._cached_youtube_transcript('https://youtu.be/abcdefghijk'))

    def test_empty_body(self):
        directory = os.path.join(serve.LIBRARY_DIR, 'media', 'transcripts')
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, 'abcdefghijk.txt'), 'w') as fh:
            fh.write('# YouTube transcript\n\n\n\n')
        self.assertIsNone(
            serve._cached_youtube_transcript('https://youtu.be/abcdefghijk'))
