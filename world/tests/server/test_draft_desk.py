"""Draft desk (Tier-2, 2026-10-30): the tank prepares short local-area
posts; the operator decides each from Telegram with "post <id>" /
"skip <id>". "post" is a LEDGER entry only -- the tank never posts
anywhere (Facebook hard rule). The default when nothing clears the bar
is NO draft. Covers command parsing, the ledger table, generation
parsing, and the pre-lane owner-command hook."""
import asyncio
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-draft-desk-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )


def tearDownModule():
    _PATCHER.stop()


def _run(coro):
    return asyncio.run(coro)


class TestParse(unittest.TestCase):
    def test_recognizes_all_four_commands(self):
        self.assertEqual(serve._parse_draft_command('draft'), ('draft', None))
        self.assertEqual(serve._parse_draft_command('drafts'), ('drafts', None))
        self.assertEqual(serve._parse_draft_command('post dft_3'),
                         ('post', 'dft_3'))
        self.assertEqual(serve._parse_draft_command('SKIP dft_17'),
                         ('skip', 'dft_17'))

    def test_prose_is_never_a_command(self):
        for text in ('post the thing now', 'what happened today',
                     'post', 'skip', '', 'draft extra', 'I will post dft_3'):
            self.assertIsNone(serve._parse_draft_command(text), text)


class TestLedger(unittest.TestCase):
    def test_create_and_decide_lifecycle(self):
        created = serve._draft_desk_create(
            ['Cafe counts are up on W 6th: 12 new spots since spring (Overpass).'],
            'test')
        self.assertEqual(len(created), 1)
        draft_id = created[0]['draftId']
        self.assertTrue(any(p['draftId'] == draft_id
                            for p in serve._draft_desk_pending()))
        ok, msg = serve._draft_desk_decide(draft_id, 'post', 'player')
        self.assertTrue(ok)
        self.assertIn('ledger only', msg)
        ok2, msg2 = serve._draft_desk_decide(draft_id, 'skip', 'player')
        self.assertFalse(ok2)
        self.assertIn('already', msg2)
        self.assertFalse(any(p['draftId'] == draft_id
                             for p in serve._draft_desk_pending()))

    def test_unknown_draft_id_is_refused(self):
        ok, msg = serve._draft_desk_decide('dft_99999', 'post', 'player')
        self.assertFalse(ok)
        self.assertIn('No draft', msg)

    def test_overlong_and_empty_candidates_are_refused(self):
        created = serve._draft_desk_create(['', 'x' * 281, 'ok one'], 'test')
        self.assertEqual([c['text'] for c in created], ['ok one'])

    def test_max_two_per_run(self):
        created = serve._draft_desk_create(
            [f'draft number {i} with some words' for i in range(5)], 'test')
        self.assertEqual(len(created), serve._DRAFT_MAX_PER_RUN)


class TestGeneration(unittest.TestCase):
    def test_draft_lines_become_pending_rows(self):
        before = len(serve._draft_desk_pending())

        async def fake_ask(state, prompt, **kwargs):
            return {'reply': 'Two grounded drafts:\n\n'
                             'DRAFT: Milk prices at the co-op rose 40c since June (feed: county market report).\n'
                             'DRAFT: Hornets home tickets from 19usd on Mar 13 (ticketmaster-discovery).\n'
                             'DRAFT: this one is way over the limit ' + 'x' * 300}

        with unittest.mock.patch.object(serve, '_ask_core', side_effect=fake_ask):
            result = _run(serve._draft_desk_generate({}, session_id=None))
        pending = serve._draft_desk_pending()
        self.assertEqual(len(pending), before + 2,
                         'two in-limit drafts must land, the overlong refused')
        joined = ' | '.join(p['text'] for p in pending)
        self.assertNotIn('x' * 300, joined)
        self.assertIn('post <id>', result['reply'])

    def test_no_draft_lines_means_nothing_created(self):
        before = len(serve._draft_desk_pending())

        async def fake_ask(state, prompt, **kwargs):
            return {'reply': 'Nothing clears the bar today: the local feeds '
                             'carry only national syndicated copy.'}

        with unittest.mock.patch.object(serve, '_ask_core', side_effect=fake_ask):
            result = _run(serve._draft_desk_generate({}, session_id=None))
        self.assertIn('No draft cleared the bar', result['reply'])
        self.assertEqual(len(serve._draft_desk_pending()), before)


class TestOwnerHook(unittest.TestCase):
    def test_non_command_text_returns_none(self):
        self.assertIsNone(_run(
            serve._draft_desk_handle_owner_command({}, 'what happened today')))

    def test_drafts_command_lists_pending(self):
        serve._draft_desk_create(['A short grounded draft.'], 'test')
        got = _run(serve._draft_desk_handle_owner_command({}, 'drafts'))
        self.assertIn('dft_', got['reply'])
        self.assertIn("post <id>", got['reply'])

    def test_post_decision_ledger_language_only(self):
        created = serve._draft_desk_create(['Another short draft.'], 'test')
        got = _run(serve._draft_desk_handle_owner_command(
            {}, f"post {created[0]['draftId']}"))
        self.assertIn('POSTED', got['reply'])
        self.assertIn('nothing was posted anywhere', got['reply'])
