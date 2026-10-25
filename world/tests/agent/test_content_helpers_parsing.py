"""Content probe, sandbox-context, review-verdict, and JS-integrity helpers."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402
import content  # noqa: E402


class ProbeParsing(unittest.TestCase):
    def test_parse_probe_request_rejects_empty_and_invalid(self):
        self.assertIsNone(content._parse_probe_request(''))
        self.assertIsNone(content._parse_probe_request('not json'))
        self.assertIsNone(content._parse_probe_request('{"other": 1}'))
        self.assertIsNone(content._parse_probe_request('{"probeRequest": {"path": "x"}}'))
        self.assertIsNone(content._parse_probe_request('{"probeRequest": {"actions": "nope"}}'))

    def test_parse_probe_request_accepts_actions_and_probes(self):
        req = content._parse_probe_request(
            '```json\n{"probeRequest": {"path": "index.html", '
            '"actions": [{"type": "click"}], "probes": ["document.title"]}}\n```')
        self.assertEqual(req['path'], 'index.html')
        self.assertEqual(req['actions'], [{'type': 'click'}])
        self.assertEqual(req['probes'], ['document.title'])

    def test_parse_probe_request_defaults(self):
        req = content._parse_probe_request('{"probeRequest": {"actions": []}}')
        self.assertEqual(req['path'], 'index.html')

    def test_parse_colab_run_request_rejects_empty_and_invalid(self):
        self.assertIsNone(content._parse_colab_run_request(''))
        self.assertIsNone(content._parse_colab_run_request('not json'))
        self.assertIsNone(content._parse_colab_run_request('{"colabRun": {"code": "  "}}'))
        self.assertIsNone(content._parse_colab_run_request('{"other": 1}'))

    def test_parse_colab_run_request_defaults_and_filters(self):
        req = content._parse_colab_run_request(
            '{"colabRun": {"code": "print(1)", "purpose": "why", '
            '"packages": ["numpy", "", 3, "torch"], "timeout_seconds": "abc", '
            '"runtimes": "x", "runtime": "quantum"}}')
        self.assertEqual(req['code'], 'print(1)')
        self.assertEqual(req['purpose'], 'why')
        self.assertEqual(req['packages'], ['numpy', 'torch'])
        self.assertEqual(req['timeout_seconds'], 300)
        self.assertEqual(req['runtimes'], 1)
        self.assertEqual(req['runtime'], 'cpu')

    def test_parse_colab_run_request_keeps_valid_ints(self):
        req = content._parse_colab_run_request(
            '{"colabRun": {"code": "print(1)", "timeout_seconds": 900, "runtimes": 3, "runtime": "gpu"}}')
        self.assertEqual(req['timeout_seconds'], 900)
        self.assertEqual(req['runtimes'], 3)
        self.assertEqual(req['runtime'], 'gpu')

    def test_format_colab_run_result_non_dict(self):
        self.assertEqual(content._format_colab_run_result(None),
                         '(Colab run returned no usable response)')

    def test_format_colab_run_result_error(self):
        self.assertEqual(content._format_colab_run_result({'error': 'no slot'}),
                         '(Colab run refused: no slot)')

    def test_format_colab_run_result_success(self):
        out = content._format_colab_run_result({'session': 's', 'elapsed_s': 5,
                                                'units': 2, 'stdout': 'real out'})
        self.assertIn('Colab run OK', out)
        self.assertIn('real out', out)

    def test_format_colab_run_result_sharded_and_no_output(self):
        out = content._format_colab_run_result({'runtimes': 3, 'elapsed_s': 5,
                                                'units': 6, 'stdout': ''})
        self.assertIn('Colab sharded run OK across 3 runtime(s)', out)
        self.assertIn('(no output', out)

    def test_format_page_probe_result_error(self):
        self.assertIn('probe could not run', content._format_page_probe_result({'error': 'boom'}))
        self.assertIn('no response', content._format_page_probe_result(None))

    def test_format_page_probe_result_full(self):
        data = {
            'actionLog': ['clicked x', 'typed y'],
            'console': ['log line'],
            'pageErrors': ['TypeError'],
            'customGlobals': [
                {'name': 'obj', 'type': 'object', 'keys': ['a', 'b']},
                {'name': 'objEmpty', 'type': 'object', 'keys': []},
                {'name': 'arr', 'type': 'array', 'length': 3},
                {'name': 'num', 'type': 'number', 'value': 42},
                {'name': 'plain', 'type': 'string'},
            ],
            'results': {'document.title': 'hi', 'bad': object()},
        }
        out = content._format_page_probe_result(data)
        self.assertIn('window.obj (object) -- real keys: a, b', out)
        self.assertIn('window.objEmpty (object) -- real keys: (none)', out)
        self.assertIn('window.arr (array, length 3)', out)
        self.assertIn('window.num (number) = 42', out)
        self.assertIn('window.plain (string)', out)
        self.assertIn('document.title => "hi"', out)

    def test_format_page_probe_result_no_globals(self):
        out = content._format_page_probe_result({'actionLog': [], 'console': [],
                                                 'pageErrors': [], 'customGlobals': [],
                                                 'results': {}})
        self.assertIn('(none found)', out)
        self.assertIn('(none)', out)

    def test_format_page_probe_result_skips_non_dict_globals(self):
        # A stray non-dict entry in customGlobals is defensively skipped, not
        # a crash.
        out = content._format_page_probe_result({'actionLog': [], 'console': [],
                                                 'pageErrors': [], 'customGlobals': ['stray', 7],
                                                 'results': {}})
        self.assertNotIn('window.stray', out)
        self.assertNotIn('window.7', out)

class SelfBaseUrl(unittest.TestCase):
    def test_matches_loopback_bind(self):
        # env_url passed explicitly: on a real host SELF_BASE_URL may point at a
        # Tailscale bind, and this test is about the match contract, not the
        # machine's .env.
        url, matches = serve._derive_self_base_url('127.0.0.1', 8937, env_url='http://127.0.0.1:8937')
        self.assertEqual(url, 'http://127.0.0.1:8937')
        self.assertTrue(matches)

    def test_zero_bind_maps_to_loopback(self):
        # A client cannot connect to 0.0.0.0; self-calls must use 127.0.0.1.
        url, _ = serve._derive_self_base_url('0.0.0.0', 8937)
        self.assertEqual(url, 'http://127.0.0.1:8937')

    def test_explicit_tailscale_host_is_used_as_is(self):
        url, _ = serve._derive_self_base_url('100.64.0.5', 8937)
        self.assertEqual(url, 'http://100.64.0.5:8937')

    def test_drift_is_detected(self):
        # The wiring bug: .env said 8936 while the process binds 8937, so every
        # self-loopback (browse/evidence/save) hit a dead port and the crawl
        # silently recorded nothing.
        url, matches = serve._derive_self_base_url('127.0.0.1', 8937, env_url='http://127.0.0.1:8936')
        self.assertEqual(url, 'http://127.0.0.1:8937')
        self.assertFalse(matches)

class SandboxContextHelpers(unittest.TestCase):
    def test_get_sandbox_context_no_output(self):
        with mock.patch.object(serve, '_http_json', return_value={'allowed': False}):
            self.assertEqual(content._get_sandbox_context('b', 'k', 'a', 'sb'),
                             '(nothing written yet)')
        with mock.patch.object(serve, '_http_json', return_value={'allowed': True, 'stdout': ''}):
            self.assertEqual(content._get_sandbox_context('b', 'k', 'a', 'sb'),
                             '(nothing written yet)')
        with mock.patch.object(serve, '_http_json', return_value=None):
            self.assertEqual(content._get_sandbox_context('b', 'k', 'a', 'sb'),
                             '(nothing written yet)')

    def test_get_sandbox_context_returns_stdout(self):
        with mock.patch.object(serve, '_http_json', return_value={'allowed': True,
                                                                  'stdout': 'file contents'}):
            self.assertEqual(content._get_sandbox_context('b', 'k', 'a', 'sb'), 'file contents')

    def test_review_sandbox_id_shared_fallback_for_non_code_lane(self):
        self.assertEqual(content._review_sandbox_id({'tasks': {}},
                                                    {'id': 'k1', 'room': 'observatory'},
                                                    'cora'),
                         content.WORKROOM_SANDBOX_ID)
        # A code-class task that somehow carries no id falls back too.
        self.assertEqual(content._review_sandbox_id({'tasks': {}},
                                                    {'room': 'pressoffice'},
                                                    'cora'),
                         content.WORKROOM_SANDBOX_ID)

    def test_search_library_files_variants(self):
        with mock.patch.object(serve, '_http_json', return_value={'matches': [{'path': 'p'}]}):
            self.assertEqual(content._search_library_files('b', 'k', 'a', 'q'), [{'path': 'p'}])
        with mock.patch.object(serve, '_http_json', return_value={'matches': []}):
            self.assertEqual(content._search_library_files('b', 'k', 'a', 'q'), [])
        with mock.patch.object(serve, '_http_json', return_value=None):
            self.assertEqual(content._search_library_files('b', 'k', 'a', 'q'), [])

    def test_gather_unified_context_library_and_mail_variants(self):
        snapshot = {'agents': {'a': {'mailbox': [
            {'text': 'old mail', 'read': True},
            {'text': 'new mail', 'read': False},
        ]}}}
        with mock.patch.object(serve, '_http_json',
                               side_effect=[{'allowed': True, 'stdout': 'sb files'},
                                            {'matches': [{'path': 'p', 'snippet': 'sn'}]}]):
            out = content._gather_unified_context(snapshot, 'b', 'k', 'a', 'sb', 'topic')
        self.assertIn('sb files', out)
        self.assertIn('- p: sn', out)
        self.assertIn('old mail', out)
        self.assertIn('[NEW] new mail', out)

    def test_gather_unified_context_no_matches_no_mail(self):
        with mock.patch.object(serve, '_http_json',
                               side_effect=[{'allowed': True, 'stdout': 'sb'}, {}]):
            out = content._gather_unified_context({}, 'b', 'k', 'a', 'sb', None)
        self.assertIn('(no relevant Library files', out)
        self.assertIn('(nothing in your mailbox)', out)

    def test_gather_unified_context_non_list_mailbox(self):
        snapshot = {'agents': {'a': {'mailbox': 'not a list'}}}
        with mock.patch.object(serve, '_http_json',
                               side_effect=[{'allowed': True, 'stdout': 'sb'}, {}]):
            out = content._gather_unified_context(snapshot, 'b', 'k', 'a', 'sb', 't')
        self.assertIn('(nothing in your mailbox)', out)

    def test_review_screenshot_failures(self):
        with mock.patch.object(serve, '_http_json', return_value={'error': 'no shot'}):
            self.assertEqual(content._review_screenshot('b', 'k', 'a', 'sb', 'i.html', 'q')['ok'], False)
        with mock.patch.object(serve, '_http_json', return_value=None):
            self.assertEqual(content._review_screenshot('b', 'k', 'a', 'sb', 'i.html', 'q')['ok'], False)
        with mock.patch.object(serve, '_http_json',
                               return_value={'imageBase64': 'abc'}), \
             mock.patch.object(serve, '_vision_tier_slug', return_value=None):
            res = content._review_screenshot('b', 'k', 'a', 'sb', 'i.html', 'q')
            self.assertEqual(res['ok'], False)
            self.assertIn('vision model tier not available', res['note'])

    def test_review_screenshot_vision_failure_and_success(self):
        with mock.patch.object(serve, '_http_json',
                               side_effect=[{'imageBase64': 'abc'},
                                            {'reply': 'real review'}]), \
             mock.patch.object(serve, '_vision_tier_slug', return_value='vis'):
            res = content._review_screenshot('b', 'k', 'a', 'sb', 'i.html', 'q')
            self.assertEqual(res, {'ok': True, 'review': 'real review'})
        with mock.patch.object(serve, '_http_json',
                               side_effect=[{'imageBase64': 'abc'}, {'reply': ''}]), \
             mock.patch.object(serve, '_vision_tier_slug', return_value='vis'):
            res = content._review_screenshot('b', 'k', 'a', 'sb', 'i.html', 'q')
            self.assertEqual(res['ok'], False)
            self.assertIn('vision call failed', res['note'])

class ReviewVerdictParsing(unittest.TestCase):
    def test_parse_review_verdict_accepts_bare_json(self):
        v, summary, checks, risks = content._parse_review_verdict(
            '{"verdict": "send_back", "summary": "reset is broken", '
            '"checks": ["clicked reset"], "risks": ["no error handling"]}')
        self.assertEqual(v, 'send_back')
        self.assertEqual(summary, 'reset is broken')
        self.assertEqual(checks, ['clicked reset'])
        self.assertEqual(risks, ['no error handling'])

    def test_parse_review_verdict_tolerates_embedded_prose_and_fences(self):
        v, summary, checks, risks = content._parse_review_verdict(
            'The code looks mostly fine but the reset path crashes.\n'
            '```json\n{"verdict": "approve", "summary": "solid enough", '
            '"checks": ["pipeline green"], "risks": []}\n```')
        self.assertEqual(v, 'approve')
        self.assertEqual(summary, 'solid enough')
        self.assertEqual(checks, ['pipeline green'])

    def test_parse_review_verdict_rejects_unknown_verdict(self):
        v, *_ = content._parse_review_verdict('{"verdict": "maybe", "summary": "x"}')
        self.assertIsNone(v)

    def test_parse_review_verdict_falls_back_on_non_json(self):
        v, summary, checks, risks = content._parse_review_verdict(
            'This genuinely looks solid -- nothing actionable.')
        self.assertIsNone(v)
        self.assertEqual(summary, '')
        self.assertEqual(checks, [])
        self.assertEqual(risks, [])

    def test_parse_review_verdict_filters_non_string_entries(self):
        v, _, checks, risks = content._parse_review_verdict(
            '{"verdict": "approve", "checks": ["ok", 42], "risks": ["r", null]}')
        self.assertEqual(checks, ['ok'])
        self.assertEqual(risks, ['r'])

    def test_parse_review_verdict_malformed_json_falls_back(self):
        v, summary, checks, risks = content._parse_review_verdict('{not json at all}')
        self.assertIsNone(v)
        self.assertEqual((summary, checks, risks), ('', [], []))

class JsFileIntegrity(unittest.TestCase):
    def test_extract_written_js_files(self):
        cmd = 'cat > app.js << EOF\nx\nEOF\ncat >> util.js << EOF\ny\nEOF\ncat > not.py << EOF\nEOF'
        self.assertEqual(content._extract_written_js_files(cmd), ['app.js', 'util.js'])
        self.assertEqual(content._extract_written_js_files('echo hi'), [])

    def test_find_unlinked_js_files(self):
        self.assertEqual(content._find_unlinked_js_files('b', 'k', 'a', 'sb', []), [])
        with mock.patch.object(serve, '_http_json', return_value=None):
            self.assertEqual(content._find_unlinked_js_files('b', 'k', 'a', 'sb', ['a.js']), ['a.js'])
        with mock.patch.object(serve, '_http_json',
                               return_value={'stdout': 'LINKED:a.js\nUNLINKED:b.js\n'}):
            self.assertEqual(content._find_unlinked_js_files('b', 'k', 'a', 'sb',
                                                             ['a.js', 'b.js']), ['b.js'])

    def test_guess_link_target_html(self):
        self.assertEqual(content._guess_link_target_html('b', 'k', 'a', 'sb', 'index.js'),
                         'index.html')
        with mock.patch.object(serve, '_http_json',
                               return_value={'allowed': True, 'stdout': 'yes'}):
            self.assertEqual(content._guess_link_target_html('b', 'k', 'a', 'sb', 'settings.js'),
                             'settings.html')
        with mock.patch.object(serve, '_http_json',
                               return_value={'allowed': True, 'stdout': 'no'}):
            self.assertEqual(content._guess_link_target_html('b', 'k', 'a', 'sb', 'settings.js'),
                             'index.html')
        with mock.patch.object(serve, '_http_json', return_value=None):
            self.assertEqual(content._guess_link_target_html('b', 'k', 'a', 'sb', 'settings.js'),
                             'index.html')

    def test_auto_link_js_files_budget_exhausted(self):
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/execute':
                return {'allowed': False}
            if path == '/api/chat':
                return {'budgetExhausted': True, 'taskSpendUsd': 1.0,
                        'taskSpendAttempts': 2}
            return {}
        with mock.patch.object(serve, '_http_json', side_effect=side_effect):
            res = content._auto_link_js_files('b', 'k', 'a', 'sb', ['a.js'], [],
                                              'tier', task_id='c9')
        self.assertTrue(res.get('budgetExhausted'))
        self.assertEqual(res.get('taskSpendUsd'), 1.0)

    def test_auto_link_js_files_success_and_failure_paths(self):
        calls = {'n': 0}
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            calls['n'] += 1
            if path == '/api/chat':
                return {'reply': 'cat >> index.html << EOF\n<script src="a.js"></script>\nEOF'}
            if path == '/api/execute':
                if calls['n'] == 3:
                    return {'allowed': True, 'exitCode': 1, 'timedOut': False}  # non-zero
                return {'allowed': True, 'exitCode': 0, 'timedOut': False}
            return {'ok': True}
        messages = [{'role': 'user', 'content': 'x'}]
        with mock.patch.object(serve, '_http_json', side_effect=side_effect):
            res = content._auto_link_js_files('b', 'k', 'a', 'sb', ['a.js'], messages, 'tier')
        self.assertFalse(res['ok'])
        self.assertIn('exit code 1', res['note'])

    def test_auto_link_js_files_blocked(self):
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/chat':
                return {'reply': 'cat >> index.html << EOF\n<script src="a.js"></script>\nEOF'}
            if path == '/api/execute':
                return {'allowed': False, 'reason': 'denied'}
            return {'ok': True}
        with mock.patch.object(serve, '_http_json', side_effect=side_effect):
            res = content._auto_link_js_files('b', 'k', 'a', 'sb', ['a.js'], [], 'tier')
        self.assertIn('blocked', res['note'])

    def test_auto_link_js_files_chat_failure_and_success(self):
        with mock.patch.object(serve, '_http_json', return_value={'reply': ''}):
            res = content._auto_link_js_files('b', 'k', 'a', 'sb', ['a.js'], [], 'tier')
            self.assertIn('follow-up model call failed', res['note'])
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/chat':
                return {'reply': 'cat >> index.html << EOF\n<script src="a.js"></script>\nEOF'}
            return {'allowed': True, 'exitCode': 0, 'timedOut': False}
        with mock.patch.object(serve, '_http_json', side_effect=side_effect):
            res = content._auto_link_js_files('b', 'k', 'a', 'sb', ['a.js', 'b.js'], [], 'tier')
            self.assertTrue(res['ok'])

    def test_find_phantom_script_refs(self):
        with mock.patch.object(serve, '_http_json', return_value=None):
            self.assertEqual(content._find_phantom_script_refs('b', 'k', 'a', 'sb'), [])
        with mock.patch.object(serve, '_http_json',
                               return_value={'stdout': 'EXISTS:index.html:real.js\nPHANTOM:index.html:ghost.js\n'}):
            refs = content._find_phantom_script_refs('b', 'k', 'a', 'sb')
            self.assertEqual(refs, [{'html': 'index.html', 'file': 'ghost.js'}])

    def test_remove_phantom_script_refs_paths(self):
        refs = [{'html': 'index.html', 'file': 'ghost.js'}]
        with mock.patch.object(serve, '_http_json',
                               return_value={'allowed': False, 'reason': 'denied'}):
            res = content._remove_phantom_script_refs('b', 'k', 'a', 'sb', refs)
            self.assertIn('blocked', res['note'])
        with mock.patch.object(serve, '_http_json',
                               return_value={'allowed': True, 'exitCode': 1, 'timedOut': False}):
            res = content._remove_phantom_script_refs('b', 'k', 'a', 'sb', refs)
            self.assertIn('exit code', res['note'])
        with mock.patch.object(serve, '_http_json',
                               return_value={'allowed': True, 'exitCode': 0, 'timedOut': False}):
            res = content._remove_phantom_script_refs('b', 'k', 'a', 'sb', refs)
            self.assertEqual(res['ok'], True)

    def _dangling(self, blob):
        with mock.patch.object(serve, '_http_json', return_value={'allowed': True,
                                                                  'stdout': blob}):
            return content._find_dangling_selector_refs('b', 'k', 'a', 'sb')

    def test_find_dangling_selector_refs_no_output(self):
        with mock.patch.object(serve, '_http_json', return_value={'allowed': False}):
            self.assertEqual(content._find_dangling_selector_refs('b', 'k', 'a', 'sb'), [])
        with mock.patch.object(serve, '_http_json', return_value=None):
            self.assertEqual(content._find_dangling_selector_refs('b', 'k', 'a', 'sb'), [])

    def test_find_dangling_selector_refs_matches_markup(self):
        blob = ('--- index.html ---\n<div class="hero"></div>\n'
                '--- app.js ---\ndocument.querySelector(".hero")\n')
        self.assertEqual(self._dangling(blob), [])

    def test_find_dangling_selector_refs_dangling_class_and_id(self):
        blob = ('--- index.html ---\n<div class="other"></div>\n'
                '--- app.js ---\ndocument.querySelector(".ghost")\ngetElementById("spirit")\n'
                '--- util.js ---\ndocument.querySelectorAll("#part")\n')
        dangling = self._dangling(blob)
        names = {(d['selector'], tuple(d['files'])) for d in dangling}
        self.assertIn(('.ghost', ('app.js',)), names)
        self.assertIn(('#spirit', ('app.js',)), names)
        self.assertIn(('#part', ('util.js',)), names)

    def test_find_dangling_selector_refs_dynamic_creation_exempts(self):
        blob = ('--- index.html ---\n<div></div>\n--- app.js ---\n'
                'document.querySelector(".dyn")\ndocument.getElementById("setid")\n'
                'classList.add("dyn")\nel.id = "setid"\n'
                'setAttribute("id", "attr")\n')
        self.assertEqual(self._dangling(blob), [])

    def test_find_dangling_selector_refs_preamble_and_other_file_types(self):
        # Content before the first `--- file ---` marker (current is None) and
        # files that are neither .html nor .js are both ignored, never a crash.
        blob = ('browser banner line\n'
                '--- index.html ---\n<div class="ok"></div>\n'
                '--- app.js ---\ndocument.querySelector(".ok")\n'
                '--- notes.md ---\n# just notes\n')
        self.assertEqual(self._dangling(blob), [])
