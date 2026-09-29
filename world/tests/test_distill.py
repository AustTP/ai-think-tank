"""Tests for the hive-mind distillation loop (2026-09-25).

The think tank shares storage (library/archive/) and injects wiki context before a
task acts, but had no step that *merges* many past findings into living,
synthesized knowledge. Distillation is that step: a cadence-driven task reads
the archive files written since the last run, LLM-synthesizes them (plus the
current think tank wiki) into an updated wiki page, and persists it as server
authority so inject_wiki_context feeds the merged knowledge back to every
subsequent agent.

Covered here:
1. `_check_schedules` queues a distill task when the cadence is due and NOT
   when it just ran; the due run stamps `lastDistillAt` and passes the previous
   run's stamp as `distillSince`.
2. The `queue_work` whitelist keeps `distill`/`distillSince` (unknown fields
   would otherwise be silently dropped).
3. `_run_distill_content` reads only archive files NEWER than `distillSince`
   and caps at `_DISTILL_MAX_ARCHIVES`.
4. On synthesis it writes a think tank wiki page via serve._write_wiki_server,
   records the distilled count, and the writer is called with category
   'think_tank' (so inject_wiki_context injects it think tank-wide).
5. noop guards: no new archives -> no wiki write; model returns nothing -> no
   wiki write.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim  # noqa: E402

_NOW_MS = 1_725_000_000_000  # 2026-09


def _state(**over):
    base = {
        'sim': {'owner': 'server'},
        'agentRoster': [{'id': 'ada'}, {'id': 'ben'}],
        'agents': {'ada': {'id': 'ada'}, 'ben': {'id': 'ben'}},
        'researchTopics': [],
        'tasks': {},
        'workQueue': [],
        'wiki': {'pages': {}, 'categories': {'think_tank': {}, 'workroom': {},
                                             'observatory': {}}},
    }
    base.update(over)
    return base


def _distill_task(**over):
    task = {'id': 'task-d1', 'title': 'Distill recent think tank knowledge',
            'room': 'observatory', 'instructions': 'merge findings',
            'distill': True, 'distillSince': 0}
    task.update(over)
    return task


def _seed_archive_file(archive_dir, name='finding.md', mtime_s=int(_NOW_MS / 1000) - 100):
    os.makedirs(archive_dir, exist_ok=True)
    p = os.path.join(archive_dir, name)
    with open(p, 'w') as f:
        f.write(f'{name} finding')
    os.utime(p, (mtime_s, mtime_s))
    return p


class DistillCadenceSweep(unittest.TestCase):
    # The distill sweep is content-gated: it fires only when archive files
    # newer than the last distillation are actually waiting, so these cadence
    # tests patch LIBRARY_DIR/ARCHIVE_DIR to a temp tree with one recent
    # finding (hermetic -- no dependence on the live library on disk).
    def setUp(self):
        import serve as serve_mod
        self._tmp = tempfile.mkdtemp()
        self._archive = os.path.join(self._tmp, 'archive')
        _seed_archive_file(self._archive)
        self._patcher = mock.patch.multiple(
            serve_mod,
            LIBRARY_DIR=self._tmp,
            LIBRARY_ARCHIVE_DIR=self._archive)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_queues_distill_when_due(self):
        state = _state()
        previous = _NOW_MS - sim.DISTILL_CADENCE_MS  # last run, now due
        state['lastDistillAt'] = previous
        sim._check_schedules(state, now=_NOW_MS / 1000, now_ms=_NOW_MS)
        distill = [q for q in state['workQueue'] if q.get('distill')]
        self.assertEqual(len(distill), 1)
        item = distill[0]
        self.assertEqual(item['room'], 'observatory')
        # The sweep stamps the marker BEFORE assignment and passes the PREVIOUS
        # stamp, so the executor folds in only what is new since the last pass.
        self.assertEqual(item['distillSince'], previous)
        self.assertEqual(state['lastDistillAt'], _NOW_MS,
                         'marker stamped so the ceremony is not re-picked')

    def test_recent_distill_is_not_re_run(self):
        state = _state()
        state['lastDistillAt'] = _NOW_MS  # just ran
        sim._check_schedules(state, now=_NOW_MS / 1000, now_ms=_NOW_MS)
        self.assertEqual([q for q in state['workQueue'] if q.get('distill')], [])

    def test_distill_and_skill_review_can_coexist(self):
        state = _state()
        state['lastSkillReviewAt'] = _NOW_MS
        state['lastDistillAt'] = _NOW_MS - sim.DISTILL_CADENCE_MS
        sim._check_schedules(state, now=_NOW_MS / 1000, now_ms=_NOW_MS)
        kinds = {q.get('distill') for q in state['workQueue']}
        self.assertIn(True, kinds)
        self.assertEqual([q for q in state['workQueue'] if q.get('skillReview')], [])

    def test_due_with_no_new_archives_stays_quiet_and_unstamped(self):
        # Cadence due but no archive file newer than the last stamp -> the
        # ceremony must not queue a task or advance the marker.
        import serve as serve_mod
        empty = os.path.join(self._tmp, 'empty-archive')
        with mock.patch.object(serve_mod, 'LIBRARY_ARCHIVE_DIR', empty):
            state = _state()
            state['lastDistillAt'] = _NOW_MS - sim.DISTILL_CADENCE_MS
            sim._check_schedules(state, now=_NOW_MS / 1000, now_ms=_NOW_MS)
        self.assertEqual([q for q in state['workQueue'] if q.get('distill')], [],
                         'an empty archive must not spawn a distillation task')
        self.assertEqual(state['lastDistillAt'],
                         _NOW_MS - sim.DISTILL_CADENCE_MS,
                         'marker must NOT advance when there is nothing to merge')

    def test_findings_appearing_later_fire_the_very_next_pass(self):
        # No new findings on the first due pass -> quiet, unstamped. A finding
        # appears; the next _check_schedules pass fires without a full cadence.
        import serve as serve_mod
        empty = os.path.join(self._tmp, 'empty-archive')
        with mock.patch.object(serve_mod, 'LIBRARY_ARCHIVE_DIR', empty):
            state = _state()
            state['lastDistillAt'] = _NOW_MS - sim.DISTILL_CADENCE_MS
            sim._check_schedules(state, now=_NOW_MS / 1000, now_ms=_NOW_MS)
            self.assertEqual([q for q in state['workQueue'] if q.get('distill')], [])
        _seed_archive_file(self._archive)
        sim._check_schedules(state, now=_NOW_MS / 1000, now_ms=_NOW_MS)
        self.assertEqual(len([q for q in state['workQueue'] if q.get('distill')]), 1,
                         'findings arriving later must trigger the sweep promptly')

    def test_leaked_distill_sentinel_still_fires_with_seeded_content(self):
        # Self-heal parity with skill review: a leaked 1e18 distill stamp is
        # normalized to unset, so the content gate compares against 0 and the
        # loaded finding fires the first distillation.
        state = _state()
        state['lastDistillAt'] = 1_000_000_000_000_000_000
        sim._check_schedules(state, now=_NOW_MS / 1000, now_ms=_NOW_MS)
        distill = [q for q in state['workQueue'] if q.get('distill')]
        self.assertEqual(len(distill), 1,
                         'a leaked DISTILL sentinel must not gate the ceremony out')
        self.assertEqual(distill[0]['distillSince'], 0,
                         'sentinel normalized so the gate compares since the beginning')


class DistillQueueWhitelist(unittest.TestCase):
    def test_distill_fields_survive_queue_work(self):
        state = _state()
        sim.queue_work(state, [{
            'title': 'Distill recent think tank knowledge', 'room': 'observatory',
            'instructions': 'merge findings', 'distill': True,
            'distillSince': 123456,
        }])
        item = state['workQueue'][0]
        self.assertIs(item['distill'], True)
        self.assertEqual(item['distillSince'], 123456)


class DistillExecutor(unittest.TestCase):
    def _run(self, archive_files, think_tank_body='',
             model_reply='# Think Tank knowledge\n\nMerged.', since=0):
        """Temp LIBRARY_DIR, mocked model call + server wiki-write, run executor.

        The /api/chat mock ECHOES the sources it was handed (call_args[0][2][-1])
        as the reply, so the wiki body that gets written reflects exactly what
        the executor read -- which lets tests assert on the folded-in inputs.
        Returns (write_wiki_mock, captured_content_result)."""
        tmp = tempfile.mkdtemp()
        archive_dir = os.path.join(tmp, 'archive')
        wiki_dir = os.path.join(tmp, 'wiki', 'think_tank')
        os.makedirs(archive_dir, exist_ok=True)
        os.makedirs(wiki_dir, exist_ok=True)
        for fn, body, mtime_s in archive_files:
            p = os.path.join(archive_dir, fn)
            with open(p, 'w') as f:
                f.write(body)
            os.utime(p, (mtime_s, mtime_s))
        if think_tank_body:
            with open(os.path.join(wiki_dir, 'state-of-knowledge.md'), 'w') as f:
                f.write(think_tank_body)

        import serve as serve_mod

        def _chat_echo(_method, _base, _path, payload=None, _key=None):
            # payload = {'model':..., 'messages': [system, {...content: sources}]}
            msgs = (payload or {}).get('messages') or []
            if msgs:
                system_messages['system'] = msgs[0].get('content', '')
            if model_reply is None:
                return {'reply': None}  # the model produced nothing
            return {'reply': msgs[-1].get('content') if msgs else model_reply}

        system_messages = {}
        captured = {}
        with mock.patch.object(serve_mod, 'LIBRARY_DIR', tmp), \
             mock.patch.object(serve_mod, 'LIBRARY_ARCHIVE_DIR', archive_dir), \
             mock.patch.object(serve_mod, '_mid_tier_slug',
                               return_value='mid-tier'), \
             mock.patch.object(serve_mod, '_http_json', side_effect=_chat_echo), \
             mock.patch.object(serve_mod, 'get_or_create_agent_key',
                               return_value='k'), \
             mock.patch.object(serve_mod, '_write_wiki_server',
                               return_value={'id': 'state-of-knowledge',
                                             'title': 'Think Tank state of knowledge',
                                             'category': 'think_tank',
                                             'version': 3}) as wk, \
             mock.patch.object(sim, '_store_content_result',
                               side_effect=lambda _tid, res: captured.update(res)):
            from content import _run_distill_content
            snapshot = {'agents': {}}
            _run_distill_content(snapshot, 'ada', _distill_task(distillSince=since))
        return wk, captured, system_messages.get('system', '')

    def test_reads_newest_archives_and_writes_think_tank_wiki(self):
        recent = ('new.md', 'NEW: butterflies migrate', int(_NOW_MS / 1000) - 100)
        wk, captured, _sys = self._run([recent])
        self.assertEqual(wk.call_count, 1)
        (page_id, title, category, _body), _k = wk.call_args
        self.assertEqual(page_id, 'state-of-knowledge')
        self.assertEqual(category, 'think_tank',
                         'think tank category -> injected think tank-wide by affinity')
        self.assertEqual(captured.get('distilled'), 1)
        self.assertNotIn('noop', captured)

    def test_ignores_archives_older_than_since(self):
        # The distillation's `since` is the previous run's stamp; only files
        # modified AFTER it may be folded in. here `since` = 30s ago (ms).
        since_ms = int(_NOW_MS) - 30_000
        old_mtime = _NOW_MS / 1000 - 5000        # 5000s ago -> pre-since
        recent_mtime = _NOW_MS / 1000 - 10       # 10s ago -> post-since
        old = ('old.md', 'OLD: water is wet', old_mtime)
        recent = ('new.md', 'NEW: server owns state', recent_mtime)
        wk, _, _s = self._run([old, recent], since=since_ms)
        self.assertEqual(wk.call_count, 1)
        (_pid, _title, _cat, body), _k = wk.call_args
        self.assertIn('NEW: server owns state', body)
        self.assertNotIn('OLD: water is wet', body)

    def test_noop_when_no_new_archives(self):
        # Only the (old) file exists and it predates `since` -> nothing new -> the
        # executor must not churn the wiki.
        since_ms = int(_NOW_MS) - 30_000
        old = ('old.md', 'OLD FINDING', _NOW_MS / 1000 - 5000)
        wk, captured, _s = self._run([old], since=since_ms)
        self.assertEqual(wk.call_count, 0, 'no churn when nothing is new')
        self.assertNotIn('distilled', captured)
        self.assertTrue(captured.get('noop'))

    def test_noop_when_model_returns_nothing(self):
        recent = ('new.md', 'NEW FINDING', int(_NOW_MS / 1000) - 100)
        wk, captured, _s = self._run([recent], model_reply=None)
        self.assertEqual(wk.call_count, 0)
        self.assertTrue(captured.get('noop'))

    def test_cap_at_max_archives(self):
        files = [(f'find-{100 + i}.md', f'F{i}', int(_NOW_MS / 1000) - i)
                 for i in range(40)]
        wk, _, _s = self._run(files)
        self.assertEqual(wk.call_count, 1)
        (_pid, _title, _cat, body), _k = wk.call_args
        # The echo mock returns exactly the sources the executor read in, so the
        # distinct content markers tell us how many files were actually merged.
        self.assertEqual(sum(f'F{i}' in body for i in range(40)), 25,
                         'input capped at _DISTILL_MAX_ARCHIVES (25)')

    def test_merges_current_think_tank_wiki_into_the_llm_call(self):
        # The current think tank page is passed to the synthesis model (via the
        # system prompt) so the merge is INCREMENTAL, not a from-scratch rewrite.
        recent = ('new.md', 'NEW finding', int(_NOW_MS / 1000) - 100)
        _wk, _c, system_content = self._run(
            [recent], think_tank_body='# Think Tank state of knowledge\n\nKNOWN baseline.')
        self.assertIn('KNOWN baseline', system_content,
                      'current wiki is fed to the synthesis model for an incremental merge')


class CsvLikeBlockExtraction(unittest.TestCase):
    """content._extract_csv_like_blocks: the pure detector behind the
    distill CSV-preservation safety net (2026-09-26). Lazily imports content
    (matching this file's own established convention, see DistillExecutor
    and DistillCsvPreservation's local imports below) -- a top-level `import
    content` here, ahead of any `import serve`, hits a pre-existing circular
    import (content imports serve; serve's own bottom re-exports from
    content) that's otherwise never triggered because every OTHER test file
    that imports content at module level does `import serve` first."""

    @classmethod
    def setUpClass(cls):
        import serve  # noqa: F401 -- must load BEFORE content (see class docstring)
        import content as content_mod
        cls.content = content_mod

    def test_fenced_csv_block_is_detected(self):
        text = ('some prose\n\n```csv\nname,url\nAda,https://a.com\n'
               'Ben,https://b.com\n```\n\nmore prose')
        blocks = self.content._extract_csv_like_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertIn('name,url', blocks[0])

    def test_unfenced_csv_like_lines_are_detected(self):
        text = 'Findings:\nname,url\nAda,https://a.com\nBen,https://b.com\n\nDone.'
        blocks = self.content._extract_csv_like_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertIn('Ada,https://a.com', blocks[0])

    def test_short_comma_bearing_prose_is_not_flagged(self):
        # Two consecutive comma-bearing sentences (ordinary prose, not
        # tabular data) -- below _DISTILL_CSV_MIN_LINES, must not false-
        # positive as a CSV.
        text = 'Ada, Ben, and Cora attended.\nIt went well, overall.'
        self.assertEqual(self.content._extract_csv_like_blocks(text), [])

    def test_three_comma_bearing_sentences_with_varying_comma_counts_not_flagged(self):
        # Real gap fixed (2026-09-26): the ORIGINAL loose scan only checked
        # "any comma present" -- three ordinary prose sentences in a row,
        # each with at least one comma, would have false-positived as a
        # CSV. Real CSV rows share their header's column count; these three
        # sentences have 1, 2, and 1 commas respectively -- inconsistent,
        # so the tightened (matching comma count) scan correctly rejects it.
        text = ('The meeting ran long, but it was productive.\n'
               'Ada, Ben, and Cora all contributed real ideas.\n'
               'We should follow up, probably next week.')
        self.assertEqual(self.content._extract_csv_like_blocks(text), [])

    def test_no_csv_returns_empty_list(self):
        self.assertEqual(self.content._extract_csv_like_blocks('Just plain prose, no data here.'), [])

    def test_fenced_and_loose_scan_do_not_duplicate_the_same_block(self):
        text = '```csv\nname,url\nAda,https://a.com\nBen,https://b.com\n```'
        self.assertEqual(len(self.content._extract_csv_like_blocks(text)), 1)

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(self.content._extract_csv_like_blocks(''), [])
        self.assertEqual(self.content._extract_csv_like_blocks(None), [])


class DistillCsvPreservation(unittest.TestCase):
    """Deterministic safety net (2026-09-26): the synthesis prompt tells the
    model to summarize, not re-print, the archives it merges -- the same
    instruction that already flattened a spike's real CSV into prose during
    its OWN synthesis. This is the same failure one step downstream: a
    spike's real CSV, archived verbatim, getting lost when distilled into
    the wiki. Uses its own harness (not DistillExecutor._run) because that
    one's mock always echoes the model's OWN input back as the reply, which
    can never exercise "the model's reply dropped real source data.\""""

    def _run(self, archive_body, wiki_reply, since=0):
        tmp = tempfile.mkdtemp()
        archive_dir = os.path.join(tmp, 'archive')
        os.makedirs(archive_dir, exist_ok=True)
        mtime = int(_NOW_MS / 1000) - 100
        p = os.path.join(archive_dir, 'spike.md')
        with open(p, 'w') as f:
            f.write(archive_body)
        os.utime(p, (mtime, mtime))

        import serve as serve_mod

        with mock.patch.object(serve_mod, 'LIBRARY_DIR', tmp), \
             mock.patch.object(serve_mod, 'LIBRARY_ARCHIVE_DIR', archive_dir), \
             mock.patch.object(serve_mod, '_mid_tier_slug', return_value='mid-tier'), \
             mock.patch.object(serve_mod, '_http_json', return_value={'reply': wiki_reply}), \
             mock.patch.object(serve_mod, 'get_or_create_agent_key', return_value='k'), \
             mock.patch.object(serve_mod, '_write_wiki_server',
                               return_value={'id': 'state-of-knowledge', 'title': 't',
                                             'category': 'think_tank', 'version': 1}) as wk, \
             mock.patch.object(sim, '_store_content_result'):
            from content import _run_distill_content
            _run_distill_content({'agents': {}}, 'ada', _distill_task(distillSince=since))
        return wk

    def test_csv_dropped_by_the_model_is_appended_back_verbatim(self):
        archive_body = ('# Spike findings\n\n```csv\nsource,url,method\n'
                        'OpenAI,https://openai.com,api\nGoogle,https://google.com,scrape\n```\n')
        wk = self._run(archive_body, wiki_reply='## Merged\n\nSources were investigated.')
        (_pid, _title, _cat, body), _k = wk.call_args
        self.assertIn('OpenAI,https://openai.com,api', body)
        self.assertIn('added automatically', body)
        self.assertIn('spike.md', body)

    def test_csv_already_preserved_by_the_model_is_not_duplicated(self):
        archive_body = ('```csv\nsource,url,method\n'
                        'OpenAI,https://openai.com,api\nGoogle,https://google.com,scrape\n```\n')
        wiki_reply = ('## Merged\n\n```csv\nsource,url,method\n'
                     'OpenAI,https://openai.com,api\nGoogle,https://google.com,scrape\n```\n')
        wk = self._run(archive_body, wiki_reply=wiki_reply)
        (_pid, _title, _cat, body), _k = wk.call_args
        self.assertEqual(body.count('OpenAI,https://openai.com,api'), 1)
        self.assertNotIn('added automatically', body)

    def test_csv_beyond_the_truncated_excerpt_is_still_recovered(self):
        # The CSV sits past _DISTILL_ARCHIVE_EXCERPT_CHARS -- the synthesis
        # model never even saw it (a separate failure from paraphrasing it
        # away), but the safety net reads the archive's FULL body, not the
        # truncated excerpt, so it's still recovered.
        import content as content_mod
        padding = 'x' * (content_mod._DISTILL_ARCHIVE_EXCERPT_CHARS + 500)
        archive_body = (padding + '\n\n```csv\nsource,url\n'
                        'OpenAI,https://openai.com\nGoogle,https://google.com\n```\n')
        wk = self._run(archive_body, wiki_reply='## Merged\n\nNothing further to add.')
        (_pid, _title, _cat, body), _k = wk.call_args
        self.assertIn('OpenAI,https://openai.com', body)

    def test_no_csv_in_archive_leaves_the_reply_untouched(self):
        wk = self._run('Just a prose finding, nothing tabular.', wiki_reply='## Merged\n\nProse.')
        (_pid, _title, _cat, body), _k = wk.call_args
        self.assertEqual(body, '## Merged\n\nProse.')


if __name__ == '__main__':
    unittest.main()