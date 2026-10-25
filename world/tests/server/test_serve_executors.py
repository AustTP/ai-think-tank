"""Split part of test_serve.py (serve.py behavior tests)."""
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse

import asyncio
import openpyxl

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
import sim  # noqa: E402
from test_serve_helpers import setUpModule, tearDownModule  # noqa: E402


class RemainingExecutors(unittest.TestCase):
    # Phase 3 slice 2 follow-ups: the weather / media / skill-review / research
    # branch-3 (projectLabel) and branch-4 (bare) content executors, plus the
    # _server_content_dispatcher router. Hermetic: patch serve._http_json (and
    # the Jev decision sync) to return canned endpoint responses and assert the
    # store call. No live network, no real Jev, no real rate-limiter.

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-exec-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()
        self.stored = {}
        self.http_log = []
        with serve._db() as conn:
            # Any executor that models a finding or digests needs a mid-tier.
            conn.execute("INSERT OR REPLACE INTO model_tiers (band, slug, name, price_per_m, chosen_at) "
                         "VALUES ('mid', 'test-mid-model', 'Test Mid', 0.0, 0)")

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _record(self, tid, r):
        self.stored.update({tid: r})

    def _fake_http(self, **kw):
        # kw-driveable fake for serve._http_json (route-by-route overrides).
        browse = kw.get('browse')  # list of {'allowed':..., 'text':...}
        chatreply = kw.get('chat')
        library_file = kw.get('library_file')  # dict path->content for GET
        feeds_content = kw.get('feeds_content')
        pipeline_ok = kw.get('pipeline_ok', True)

        def fake(method, base, path, body=None, header=None, timeout=None):
            self.http_log.append((method, path))
            # library file reads carry ?path= in the query string (urllib puts it
            # in the URL, not the body); parse it for GETs and strip the query so
            # the simple string matches below work.
            if path.startswith('/api/library/file') and '?' in path:
                raw, _, query = path.partition('?')
                if method == 'GET' and body is None:
                    from urllib.parse import parse_qs
                    qs = parse_qs(query)
                    body = {'path': qs.get('path', [''])[0]}
                path = raw
            if path == '/api/browse':
                p = browse.pop(0) if browse else None
                if p is None:
                    return {'error': 'exhausted'}
                return {'allowed': p.get('allowed', True), 'text': p.get('text', ''), 'url': p.get('url', '')}
            if path == '/api/chat':
                return {'reply': chatreply} if chatreply else {'error': 'model failed'}
            if path == '/api/library/file' and method == 'GET':
                q = (body if isinstance(body, dict) else {})
                name = q.get('path', '').split('/')[-1]
                content = (library_file or {}).get(name)
                if content is None:
                    return {'error': 'not found'}
                return {'path': q.get('path'), 'content': content}
            if path == '/api/library/file' and method == 'POST':
                return '_raw = saved'
            if path == '/api/library/promote' or path == '/api/library/reject':
                return {'ok': True, 'path': (body or {}).get('path')}
            if path.split('?')[0] == '/api/library':
                return {'files': kw.get('library_files', [])}
            if path == '/api/pipeline':
                return {'ok': pipeline_ok, 'failedStep': kw.get('pipeline_failed_step')}
            return {'error': f'unexpected {method} {path}'}
        return fake

    def _restore(self):
        import sim as _sim
        _sim._store_content_result = self._real_store

    def _patch_store(self):
        import sim as _sim
        self._real_store = _sim._store_content_result
        _sim._store_content_result = self._record

    def test_media_digest_no_feeds(self):
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(library_file={'feeds.md': '# feeds\n'}), create=False):
                serve._run_media_content({}, 'ada', {'id': 'm1', 'room': 'media'}, {})
        finally:
            self._restore()
        self.assertEqual([], [p for p in self.http_log if p[1] == '/api/chat'])
        self.assertIn('No feeds configured', self.stored['m1']['note'])

    def test_media_digest_fetches_summarizes_files(self):
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(
                                                library_file={'feeds.md': '# feeds\nhttps://news.example'},
                                                browse=[{'allowed': True, 'text': 'Long enough body text about the news.'}],
                                                chat='Here is a summary of the news item.'), create=False):
                serve._run_media_content({'agents': {'ada': {'name': 'Ada'}}}, 'ada', {'id': 'm2', 'room': 'media'}, {})
        finally:
            self._restore()
        posts = [p for p in self.http_log if p[1] == '/api/library/file' and p[0] == 'POST']
        self.assertEqual(1, len(posts), 'a digest must be filed to the Library')
        self.assertIn('digest', self.stored['m2']['note'].lower())

    def test_skill_review_promotes_and_rejects(self):
        # Three pending files with a fake Jev (one reject, two keep).
        self._patch_store()
        pending = [{'path': f'pending_review/skills/{n}.md'} for n in ('a', 'b', 'c')]
        libfiles = {
            'a.md': 'skill a body',
            'b.md': 'skill b body',
            'c.md': 'skill c body',
        }
        real_jev = serve._call_openrouter_decision_sync
        choices = iter(['reject', 'keep', 'keep'])
        # Shape responses to match what _jev_choice reads: data['answers'].
        serve._call_openrouter_decision_sync = lambda *a, **k: {
            'answers': {'q': {'choice': next(choices), 'confidence': 0.9}},
            'usage': {'cost': 0.005},
        }
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(library_file=libfiles, library_files=pending), create=False):
                serve._run_skill_review_content({'agents': {'ada': {'name': 'Ada'}}}, 'ada', {'id': 's1', 'room': 'observatory', 'skillReview': True}, {})
        finally:
            serve._call_openrouter_decision_sync = real_jev
            self._restore()
        promotes = [p for p in self.http_log if p[1] == '/api/library/promote']
        rejects = [p for p in self.http_log if p[1] == '/api/library/reject']
        self.assertEqual(2, len(promotes))
        self.assertEqual(1, len(rejects))
        self.assertIn('2 promoted, 1 rejected', self.stored['s1']['note'])

    def test_skill_review_none_pending(self):
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(library_files=[]), create=False):
                serve._run_skill_review_content({}, 'ada', {'id': 's2', 'room': 'observatory', 'skillReview': True}, {})
        finally:
            self._restore()
        self.assertIn('nothing waiting', self.stored['s2']['note'])

    def test_research_project_branch_logs_and_writes(self):
        # branch-3 (projectLabel): a real model finding -> pipeline log -> library write.
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(chat='A concrete finding about inference latency.'), create=False):
                serve._run_research_project_content(
                    {'agents': {'ada': {'name': 'Ada'}}, 'researchTopics': []},
                    'ada',
                    {'id': 'p1', 'room': 'observatory', 'projectLabel': 'Inference Lab',
                     'title': 'optimize speculative decoding', 'instructions': 'find the top bottleneck'}, {})
        finally:
            self._restore()
        posts = [p for p in self.http_log if p[1] == '/api/library/file' and p[0] == 'POST']
        self.assertEqual(1, len(posts), 'a real finding must be written to the Library')
        self.assertIn('optimize speculative decoding', self.stored['p1']['note'])

    def test_research_bare_branch_failure_note(self):
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(pipeline_ok=False, pipeline_failed_step='tally findings so far'),
                                            create=False):
                serve._run_research_bare_content({}, 'ada', {'id': 'b1', 'room': 'observatory'}, {})
        finally:
            self._restore()
        self.assertIn('hit a failure at "tally findings so far"', self.stored['b1']['note'])

    def test_spike_lands_findings_and_marks_ok(self):
        # A spike (Phase E2b) is room-agnostic and files a findings artifact --
        # never a product release. Route it straight through the dispatcher to
        # prove the room-agnostic dispatch happens. Hermetic against the
        # tool-loop spike executor: the executor calls the model
        # DIRECTLY (_call_openrouter_sync for plan/synthesize, _post_openrouter_raw
        # for the tool loop), not through _http_json -- so both are mocked here.
        self._patch_store()
        fake_model = {'choices': [{'message': {'content': 'WebAssembly works headless via a patched GLIBC stub.'}}],
                      'usage': {'cost': 0.0}}

        def fake_raw(model, messages, tools=None, max_tokens=None, tool_choice=None):
            # Round 1: forced tool call -- have the model browse a page so a
            # real (mocked) tool runs and the spike counts as "investigated".
            # The browse_page tool is served by _http_json (mocked) below.
            if tool_choice is not None:
                return {'choices': [{'message': {'tool_calls': [
                    {'id': 'call_1', 'function': {'name': 'browse_page',
                     'arguments': '{"url": "https://example.com", "purpose": "verify WASM"}'}},
                ]}}], 'usage': {'cost': 0.0}}
            # Round 2: model settles with its investigation summary.
            return {'choices': [{'message': {'content': 'Investigated the WASM question.'}}],
                    'usage': {'cost': 0.0}}

        with unittest.mock.patch.object(serve, '_http_json',
                                        self._fake_http(chat='WebAssembly works headless via a patched GLIBC stub.'),
                                        create=False), \
             unittest.mock.patch.object(serve, '_call_openrouter_sync', return_value=fake_model), \
             unittest.mock.patch.object(serve, '_post_openrouter_raw', side_effect=fake_raw):
            serve._server_content_dispatcher(
                {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                {'id': 'sp1', 'room': 'pressoffice', 'taskType': 'spike',
                 'title': 'Does WASM work headless?', 'instructions': 'spike it',
                 'budgetMs': 30_000}, {})
        self._restore()
        posts = [p for p in self.http_log if p[1] == '/api/library/file' and p[0] == 'POST']
        self.assertEqual(1, len(posts), 'a spike must file a findings artifact')
        self.assertTrue(self.stored['sp1'].get('ok'), 'a successful spike commits a real finding')
        # The full finding lives in the library file (data-minimization,
        # not the short note -- the note points at the artifact instead.
        self.assertIsNotNone(self.stored['sp1'].get('libraryPath'))

    def test_spike_model_failure_reports_not_ok(self):
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(), create=False):
                serve._server_content_dispatcher(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'sp2', 'room': 'observatory', 'taskType': 'spike',
                     'title': 'failed spike', 'budgetMs': 30_000}, {})
        finally:
            self._restore()
        self.assertFalse(self.stored['sp2'].get('ok'),
                         'a spike whose model call fails must not claim fabricated findings')


class PressOfficeContentExecutors(unittest.TestCase):
    # Phase 3: the Work Room / Press Office content executors -- _run_workroom_content
    # (the dispatcher), _run_coding_content (probe-before-writing + heredoc
    # continuation + the four mechanical integrity checks), and _run_review_content
    # (unified context + critique + screenshot visual + Jev actionable/clean that
    # queues a follow-up fix via 'queueFix'). Hermetic: patch serve._http_json with
    # a command-routing fake and the Jev decision sync, then assert the store call.
    # No live network, no real Jev/rate-limit/execute.

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-pressoffice-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            SANDBOXES_DIR=os.path.join(self.tmp, 'sandboxes'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()
        self.stored = {}
        self.http_log = []
        with serve._db() as conn:
            conn.execute("INSERT OR REPLACE INTO model_tiers (band, slug, name, price_per_m, chosen_at) "
                         "VALUES ('mid', 'test-mid-model', 'Test Mid', 0.0, 0)")
            conn.execute("INSERT OR REPLACE INTO model_tiers (band, slug, name, price_per_m, chosen_at) "
                         "VALUES ('coding', 'test-coding-model', 'Test Coding', 0.0, 0)")

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _record(self, tid, r):
        self.stored.update({tid: r})

    def _patch_store(self):
        import sim as _sim
        self._real_store = _sim._store_content_result
        _sim._store_content_result = self._record

    def _restore(self):
        import sim as _sim
        _sim._store_content_result = self._real_store

    def _fake_http(self, *, chat_chain=None, write_exit=0, write_allowed=True,
                   probe=None, screenshot=None, search_matches=None, ls_stdout=None):
        """Command-routing fake for serve._http_json. /api/chat pops the next
        entry from chat_chain (a list of reply strings, or {'error': ...}); any other
        route is routed by the request body's command/path."""
        chat_chain = list(chat_chain) if chat_chain else []

        def fake(method, base, path, body=None, header=None, timeout=None):
            self.http_log.append((method, path))
            # library file reads carry ?path= in the query string.
            if path.startswith('/api/library/file') and '?' in path:
                raw, _, query = path.partition('?')
                if method == 'GET' and body is None:
                    from urllib.parse import parse_qs
                    qs = parse_qs(query)
                    body = {'path': qs.get('path', [''])[0]}
                path = raw
            if path == '/api/chat':
                if not chat_chain:
                    return {'error': 'model failed'}
                nxt = chat_chain.pop(0)
                # A bare string is a reply; a dict is passed through verbatim
                # (so a test can force a failure with {'error': ...}).
                return {'reply': nxt} if isinstance(nxt, str) else nxt
            if path == '/api/library/search':
                return {'matches': search_matches or []}
            if path == '/api/library/file' and method == 'GET':
                return {'error': 'not found'}
            if path == '/api/library/file' and method == 'POST':
                return '_raw = saved'
            if path == '/api/page-probe':
                return probe if probe is not None else {'error': 'probe failed'}
            if path == '/api/screenshot':
                return screenshot if screenshot is not None else {'error': 'no headless browser'}
            if path == '/api/pipeline':
                return {'ok': write_allowed}  # workroom bare path
            if path == '/api/execute':
                body = body or {}
                command = (body.get('command') or '')
                # getSandboxContext budget-cat read.
                if command.startswith('budget=15000'):
                    return {'allowed': True, 'stdout': '--- index.html ---\n<html><body>hi</body></html>\n', 'exitCode': 0}
                # runWorkroomTask's ls -la context listing.
                if command == 'ls -la':
                    return {'allowed': True, 'stdout': ls_stdout or 'index.html\napp.js\n', 'exitCode': 0}
                # unlinked-check grep (all return LINKED so nothing is auto-linked).
                if command.startswith('grep -q'):
                    return {'allowed': True, 'stdout': 'LINKED:app.js', 'exitCode': 0}
                # phantom-ref check loop.
                if 'PHANTOM:' in command and 'EXISTS:' in command:
                    return {'allowed': True, 'stdout': '', 'exitCode': 0}
                # dangling-selector read loop.
                if command.startswith('for f in *.html *.js'):
                    return {'allowed': True, 'stdout': '', 'exitCode': 0}
                # everything else is the actual coding write command.
                return {'allowed': write_allowed, 'exitCode': write_exit, 'timedOut': False,
                        'stdout': '' if write_allowed else 'blocked by classifier', 'reason': None}
            return {'error': f'unexpected {method} {path}'}
        return fake

    # ---- workroom bare pipeline ----

    def test_workroom_bare_runs_shared_tooling(self):
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(), create=False):
                serve._run_workroom_content({}, 'ada',
                                            {'id': 'wr1', 'room': 'pressoffice', 'title': 'ambient check'}, {})
        finally:
            self._restore()
        pipes = [p for p in self.http_log if p[1] == '/api/pipeline']
        self.assertEqual(1, len(pipes), 'bare workroom runs the shared-tooling pipeline')
        self.assertIn('shared tooling in the Work Room', self.stored['wr1']['note'])

    def test_workroom_bare_reports_failed_step(self):
        # The bare shared-tooling pipeline can stop at a failed step; the note
        # must surface which step, not fail the executor.
        def failing_http(method, base, path, body=None, header=None):
            if path == '/api/pipeline':
                return {'ok': False, 'failedStep': 'run it'}
            return self._fake_http()(method, base, path, body, header)
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json', failing_http, create=False):
                serve._run_workroom_content({}, 'ada',
                                            {'id': 'wr1b', 'room': 'pressoffice', 'title': 'ambient check'}, {})
        finally:
            self._restore()
        self.assertIn('hit a failure at "run it"', self.stored['wr1b']['note'])

    # ---- workroom projectLabel -> coding ----

    def test_workroom_project_codes_and_runs(self):
        # A real project task calls the coding pipeline; a write heredoc command is
        # produced and executed, plus integrity checks run, then a success note.
        self._patch_store()
        chat = ['cat > app.js << \'EOF\'\nwindow.answer = 42;\nEOF']
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(chat_chain=chat), create=False):
                serve._run_workroom_content(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'wr2', 'room': 'pressoffice', 'title': 'implement scoring',
                     'instructions': 'add a score field', 'projectLabel': 'Snake'},
                    {})
        finally:
            self._restore()
        result = self.stored['wr2']
        # The coding executor stores its own note on success (env: the shared
        # sandbox write ran and no integrity check surfaced an issue).
        self.assertTrue(result.get('ok'))
        self.assertIn('shared Work Room sandbox', result['note'])
        # The write command must have been executed through /api/execute.
        self.assertIn('cat > app.js', result.get('command', ''))
        execs = [p for p in self.http_log if p[1] == '/api/execute']
        self.assertGreaterEqual(len(execs), 1, 'the coding write must go through /api/execute')

    # ---- coding probe loop + heredoc continuation ----

    def test_coding_probe_then_heredoc_continuation(self):
        # reply 1 = a probe request (real page check), reply 2 = truncated heredoc,
        # reply 3 = the EOF close line. The probe doesn't count against the
        # continuation budget; the final command is balanced and executed.
        probe_json = '{"probeRequest": {"path": "index.html", "actions": [{"type":"click","selector":"text=Go"}], "probes": ["typeof window.answer"]}}'
        truncated = "cat > app.js << 'EOF'\nwindow.answer = 1;"
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(
                                                chat_chain=[probe_json, truncated, '\nEOF'],
                                                probe={'actionLog': ['click Go'], 'console': [],
                                                       'pageErrors': [], 'customGlobals': [],
                                                       'results': {'typeof window.answer': 'undefined'}}),
                                            create=False):
                serve._run_coding_content(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'c1', 'room': 'pressoffice', 'title': 'add score', 'projectLabel': 'Snake'},
                    'Current files:\nindex.html\n')
        finally:
            self._restore()
        result = self.stored['c1']
        self.assertTrue(result.get('ok'), f"coding should succeed, got: {result}")
        # The probe round must not count against continuation attempts -- the
        # final command includes both the heredoc open and the EOF close.
        self.assertIn('EOF', result.get('command', ''))
        probes = [p for p in self.http_log if p[1] == '/api/page-probe']
        self.assertEqual(1, len(probes), 'one real page probe must have been run')

    def test_coding_blocked_by_execute(self):
        # The write command is produced but the /api/execute gate blocks it.
        self._patch_store()
        chat = ["cat > app.js << 'EOF'\nwindow.answer = 42;\nEOF"]
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(chat_chain=chat, write_allowed=False),
                                            create=False):
                serve._run_coding_content(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'c2', 'room': 'pressoffice', 'title': 'add score', 'projectLabel': 'Snake'},
                    {})
        finally:
            self._restore()
        result = self.stored['c2']
        self.assertFalse(result.get('ok'))
        self.assertIn('blocked', result['note'])

    # ---- review actionable / clean / model failure ----

    def _patched_jev(self, choice):
        real_jev = serve._call_openrouter_decision_sync
        serve._call_openrouter_decision_sync = lambda *a, **k: {
            'answers': {'q': {'choice': choice, 'confidence': 0.9}},
            'usage': {'cost': 0.005},
        }
        return real_jev

    def test_review_actionable_queues_fix(self):
        self._patch_store()
        real_jev = self._patched_jev('actionable')
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(
                                                chat_chain=['This genuinely has a real bug: the reset button calls an undefined function.'],
                                                screenshot={'imageBase64': 'c29h', 'width': 10, 'height': 10}),
                                            create=False):
                serve._run_review_content(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'r1', 'room': 'pressoffice', 'title': 'review the game',
                     'instructions': 'check it works', 'projectLabel': 'Snake', 'taskType': 'review'},
                    {})
        finally:
            serve._call_openrouter_decision_sync = real_jev
            self._restore()
        result = self.stored['r1']
        self.assertTrue(result.get('ok'))
        self.assertIn('queued a fix', result['note'])
        self.assertIsNotNone(result.get('queueFix'), 'an actionable review must enqueue a fix task')
        self.assertEqual('pressoffice', result['queueFix']['room'])
        # The review is filed to the Library.
        posts = [p for p in self.http_log if p[1] == '/api/library/file' and p[0] == 'POST']
        self.assertEqual(1, len(posts))

    def test_review_actionable_fix_returns_to_author_not_reviewer(self):
        # A gate review that rejects must hand the fix back to the ORIGINAL
        # author (the worker who built the story), not to the reviewer who ran
        # the review. The reviewer's own id travels in `assignedTo` on the
        # review subtask; the author's id travels in `reviewAuthorId`.
        self._patch_store()
        real_jev = self._patched_jev('actionable')
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(
                                                chat_chain=['Clear regression: the logout flow crashes.'],
                                                screenshot={'imageBase64': 'c29h', 'width': 10, 'height': 10}),
                                            create=False):
                serve._run_review_content(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'r3', 'room': 'pressoffice', 'title': 'review the game',
                     'instructions': 'check it works', 'projectLabel': 'Snake',
                     'taskType': 'review', 'assignedTo': 'ada',   # the reviewer
                     'reviewOf': 'parent-1',                       # a gate review
                     'reviewAuthorId': 'ben'},                    # the author
                    {})
        finally:
            serve._call_openrouter_decision_sync = real_jev
            self._restore()
        result = self.stored['r3']
        self.assertIsNotNone(result.get('queueFix'))
        self.assertEqual(result['queueFix']['assignedTo'], 'ben',  # pinned to author
                         'the fix for a rejected story must go to its author, not the reviewer')
        self.assertEqual(result['queueFix']['reviewOf'], 'parent-1')

    def test_review_clean_no_fix(self):
        self._patch_store()
        real_jev = self._patched_jev('clean')
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(
                                                chat_chain=['This genuinely looks solid -- nothing actionable.'],
                                                screenshot={'error': 'no browser'}),
                                            create=False):
                serve._run_review_content(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'r2', 'room': 'pressoffice', 'title': 'review the game',
                     'projectLabel': 'Snake', 'taskType': 'review'},
                    {})
        finally:
            serve._call_openrouter_decision_sync = real_jev
            self._restore()
        result = self.stored['r2']
        self.assertIn('nothing actionable found', result['note'])
        self.assertNotIn('queueFix', result, 'a clean review must not queue a fix')

    def test_review_model_failure_falls_back_to_note(self):
        # A chat call that returns nothing usable -> a soft "didn't produce
        # anything usable" note, no crash, no queueFix.
        self._patch_store()
        try:
            with unittest.mock.patch.object(serve, '_http_json',
                                            self._fake_http(chat_chain=[{'error': 'model failed'}]),
                                            create=False):
                serve._run_review_content(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'r3', 'room': 'pressoffice', 'title': 'review the game',
                     'projectLabel': 'Snake', 'taskType': 'review'},
                    {})
        finally:
            self._restore()
        result = self.stored['r3']
        self.assertFalse(result.get('ok'))
        self.assertIn('didn\'t produce anything usable', result['note'])

    def test_dispatcher_routes_observatory_review_to_review_executor(self):
        # A peer-gate review/QA subtask inherits its PARENT story's room, so a
        # review of an observatory research deliverable arrives with room
        # 'observatory' + taskType 'review'. The dispatcher must honour taskType
        # BEFORE room: routing to the observatory research executors would
        # produce a "Researched..." note with NO peerVerdict, silently dropping
        # the vote and wedging the story in 'needs_review' forever (the infinite
        # re-review fork -- tasks 7-12 of the same story). The review executor
        # must run instead; only it emits the verdict the gate folds.
        self._patch_store()
        real_jev = self._patched_jev('clean')
        try:
            with unittest.mock.patch.object(
                    serve, '_http_json',
                    self._fake_http(
                        chat_chain=['The research artifact looks solid -- the bulletin is complete and accurate.'],
                        screenshot={'error': 'no headless browser'}),
                    create=False):
                serve._server_content_dispatcher(
                    {'agents': {'ada': {'name': 'Ada'}}}, 'ada',
                    {'id': 'obsrv1', 'room': 'observatory', 'title': 'Draft weather bulletin report',
                     'instructions': 'write the bulletin', 'projectLabel': 'weather',
                     'taskType': 'review', 'reviewOf': 'task-4', 'assignedTo': 'ada',
                     'reviewAuthorId': 'ben'}, {})
        finally:
            serve._call_openrouter_decision_sync = real_jev
            self._restore()
        result = self.stored['obsrv1']
        self.assertEqual('clean', result.get('peerVerdict'),
                         'an observatory-roomed gate review must emit a peerVerdict the parent gate can fold')
        self.assertIn('Filed a review', result['note'],
                      'the review executor, not a research executor, must run for a review task in any room')
