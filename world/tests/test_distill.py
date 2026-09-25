"""Tests for the hive-mind distillation loop (2026-09-25).

The village shares storage (library/archive/) and injects wiki context before a
task acts, but had no step that *merges* many past findings into living,
synthesized knowledge. Distillation is that step: a cadence-driven task reads
the archive files written since the last run, LLM-synthesizes them (plus the
current village wiki) into an updated wiki page, and persists it as server
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
4. On synthesis it writes a village wiki page via serve._write_wiki_server,
   records the distilled count, and the writer is called with category
   'village' (so inject_wiki_context injects it village-wide).
5. noop guards: no new archives -> no wiki write; model returns nothing -> no
   wiki write.
"""
import os
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
        'wiki': {'pages': {}, 'categories': {'village': {}, 'workroom': {},
                                             'observatory': {}}},
    }
    base.update(over)
    return base


def _distill_task(**over):
    task = {'id': 'task-d1', 'title': 'Distill recent village knowledge',
            'room': 'observatory', 'instructions': 'merge findings',
            'distill': True, 'distillSince': 0}
    task.update(over)
    return task


class DistillCadenceSweep(unittest.TestCase):
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


class DistillQueueWhitelist(unittest.TestCase):
    def test_distill_fields_survive_queue_work(self):
        state = _state()
        sim.queue_work(state, [{
            'title': 'Distill recent village knowledge', 'room': 'observatory',
            'instructions': 'merge findings', 'distill': True,
            'distillSince': 123456,
        }])
        item = state['workQueue'][0]
        self.assertIs(item['distill'], True)
        self.assertEqual(item['distillSince'], 123456)


class DistillExecutor(unittest.TestCase):
    def _run(self, archive_files, village_body='',
             model_reply='# Village knowledge\n\nMerged.', since=0):
        """Temp LIBRARY_DIR, mocked model call + server wiki-write, run executor.

        The /api/chat mock ECHOES the sources it was handed (call_args[0][2][-1])
        as the reply, so the wiki body that gets written reflects exactly what
        the executor read -- which lets tests assert on the folded-in inputs.
        Returns (write_wiki_mock, captured_content_result)."""
        tmp = tempfile.mkdtemp()
        archive_dir = os.path.join(tmp, 'archive')
        wiki_dir = os.path.join(tmp, 'wiki', 'village')
        os.makedirs(archive_dir, exist_ok=True)
        os.makedirs(wiki_dir, exist_ok=True)
        for fn, body, mtime_s in archive_files:
            p = os.path.join(archive_dir, fn)
            with open(p, 'w') as f:
                f.write(body)
            os.utime(p, (mtime_s, mtime_s))
        if village_body:
            with open(os.path.join(wiki_dir, 'state-of-knowledge.md'), 'w') as f:
                f.write(village_body)

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
                                             'title': 'Village state of knowledge',
                                             'category': 'village',
                                             'version': 3}) as wk, \
             mock.patch.object(sim, '_store_content_result',
                               side_effect=lambda _tid, res: captured.update(res)):
            from content import _run_distill_content
            snapshot = {'agents': {}}
            _run_distill_content(snapshot, 'ada', _distill_task(distillSince=since))
        return wk, captured, system_messages.get('system', '')

    def test_reads_newest_archives_and_writes_village_wiki(self):
        recent = ('new.md', 'NEW: butterflies migrate', int(_NOW_MS / 1000) - 100)
        wk, captured, _sys = self._run([recent])
        self.assertEqual(wk.call_count, 1)
        (page_id, title, category, _body), _k = wk.call_args
        self.assertEqual(page_id, 'state-of-knowledge')
        self.assertEqual(category, 'village',
                         'village category -> injected village-wide by affinity')
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

    def test_merges_current_village_wiki_into_the_llm_call(self):
        # The current village page is passed to the synthesis model (via the
        # system prompt) so the merge is INCREMENTAL, not a from-scratch rewrite.
        recent = ('new.md', 'NEW finding', int(_NOW_MS / 1000) - 100)
        _wk, _c, system_content = self._run(
            [recent], village_body='# Village state of knowledge\n\nKNOWN baseline.')
        self.assertIn('KNOWN baseline', system_content,
                      'current wiki is fed to the synthesis model for an incremental merge')


if __name__ == '__main__':
    unittest.main()