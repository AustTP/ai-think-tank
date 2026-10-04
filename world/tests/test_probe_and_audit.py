"""Tests for the small/standalone world modules that had zero coverage:
probe_model (weekly model probe), audit_reachability (find_path reachability
audit), tinyprobe/probe_bare/probe_world (tiny dev probes), and the root-level
health.py health-check script. All hermetic: serve's network paths are faked,
the reachability audit runs against the real collision/door JSON (pure
geometry), and health.py reads a fabricated in-memory kv_state blob.
"""
import ast
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audit_reachability  # noqa: E402
import probe_model  # noqa: E402
import tinyprobe  # noqa: E402

WORLD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(WORLD_DIR)


# ---------------------------------------------------------------------------
# probe_model -- weekly model probe scoreboard
# ---------------------------------------------------------------------------

class LoadPrompts(unittest.TestCase):
    def test_parses_heading_delimited_prompts(self):
        p = os.path.join(WORLD_DIR, 'probe_prompts.md')
        prompts = probe_model._load_prompts(p)
        self.assertIsInstance(prompts, list)
        self.assertTrue(len(prompts) >= 3)
        name, text = prompts[0]
        self.assertTrue(name)
        self.assertTrue(text.strip())

    def test_empty_file_returns_empty(self):
        with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False) as f:
            f.write('no headings here\njust text\n')
            path = f.name
        try:
            self.assertEqual(probe_model._load_prompts(path), [])
        finally:
            os.unlink(path)


class ExtractReply(unittest.TestCase):
    def test_returns_choice_content(self):
        data = {'choices': [{'message': {'content': 'hello'}}]}
        self.assertEqual(probe_model._extract_reply(data), 'hello')

    def test_missing_choice_or_content_returns_none(self):
        self.assertIsNone(probe_model._extract_reply({}))
        self.assertIsNone(probe_model._extract_reply(None))
        self.assertIsNone(probe_model._extract_reply({'choices': []}))
        self.assertIsNone(probe_model._extract_reply({'choices': [{}]}))


class ProbeOne(unittest.TestCase):
    def test_circuit_broken_short_circuits(self):
        with unittest.mock.patch.object(probe_model._serve, 'is_model_circuit_broken',
                                        return_value=True):
            result = probe_model._probe_one('m', 'name', 'text')
        self.assertEqual(result[0], None)
        self.assertEqual(result[1], 'circuit-broken')

    def test_exception_is_reported_not_raised(self):
        with unittest.mock.patch.object(probe_model._serve, 'is_model_circuit_broken',
                                        return_value=False), \
             unittest.mock.patch.object(probe_model._serve, '_post_openrouter_raw',
                                        side_effect=RuntimeError('boom')):
            result = probe_model._probe_one('m', 'name', 'text')
        self.assertIsNone(result[0])
        self.assertEqual(result[1], 'error: boom')

    def test_ok_reply_extracts_text_and_cost(self):
        data = {'choices': [{'message': {'content': 'the answer'}}],
                'usage': {'cost': 0.00123}}
        with unittest.mock.patch.object(probe_model._serve, 'is_model_circuit_broken',
                                        return_value=False), \
             unittest.mock.patch.object(probe_model._serve, '_post_openrouter_raw',
                                        return_value=data):
            result = probe_model._probe_one('m', 'name', 'text')
        self.assertEqual(result[0], 'the answer')
        self.assertEqual(result[1], 'ok')
        self.assertAlmostEqual(result[2], 0.00123)

    def test_empty_reply_yields_none(self):
        with unittest.mock.patch.object(probe_model._serve, 'is_model_circuit_broken',
                                        return_value=False), \
             unittest.mock.patch.object(probe_model._serve, '_post_openrouter_raw',
                                        return_value={'choices': [{'message': {'content': ''}}]}):
            result = probe_model._probe_one('m', 'name', 'text')
        self.assertIsNone(result[0])
        self.assertEqual(result[1], 'ok')


class ProbeMain(unittest.TestCase):
    def _run(self, argv):
        with tempfile.TemporaryDirectory() as td:
            prompts = os.path.join(td, 'prompts.md')
            with open(prompts, 'w') as f:
                f.write('## first\nhello world\n\n## second\nprobe body\n')
            argv = list(argv) + ['--prompts', prompts]
            with unittest.mock.patch.object(sys, 'argv', argv), \
                 unittest.mock.patch.object(probe_model, 'SCORES_DIR', os.path.join(td, 'probe')), \
                 unittest.mock.patch.object(probe_model, 'SCORES_FILE',
                                            os.path.join(td, 'probe', 'scores.md')), \
                 unittest.mock.patch.object(probe_model._serve, 'is_model_circuit_broken',
                                            return_value=False), \
                 unittest.mock.patch.object(probe_model._serve, '_post_openrouter_raw',
                                            return_value={'choices': [{'message': {'content': 'probe reply'}}],
                                                          'usage': {'cost': 0.01}}), \
                 unittest.mock.patch('sys.stdout', io.StringIO()) as out:
                code = probe_model.main()
            scores = os.path.join(td, 'probe', 'scores.md')
            self.assertEqual(code, 0)
            with open(scores) as f:
                content = f.read()
            self.assertIn('# Model probe scoreboard', content)
            self.assertIn('first', content)
            self.assertIn('| ok |', content)
            return content

    def test_main_logs_scoreboard_rows(self):
        content = self._run(['probe_model.py', 'deepseek/deepseek-v4-flash', '--limit', '1'])
        self.assertIn('deepseek/deepseek-v4-flash', content)

    def test_no_prompts_returns_one(self):
        with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False) as f:
            f.write('no headings\n')
            path = f.name
        try:
            with unittest.mock.patch.object(sys, 'argv', ['probe_model.py', 'm', '--prompts', path]), \
                 unittest.mock.patch('sys.stdout', io.StringIO()):
                self.assertEqual(probe_model.main(), 1)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# audit_reachability -- find_path reachability audit
# ---------------------------------------------------------------------------

class SpawnABatch(unittest.TestCase):
    def test_returns_n_points_on_floor(self):
        import random
        grid = json.load(open(os.path.join(WORLD_DIR, 'collision_grid.json')))
        rng = random.Random(42)
        pts = audit_reachability.spawn_a_batch(grid, 5, rng)
        self.assertEqual(len(pts), 5)
        for p in pts:
            self.assertIn('x', p)
            self.assertIn('y', p)


class AuditMain(unittest.TestCase):
    def test_main_runs_and_reports_summary(self):
        with unittest.mock.patch('sys.stdout', io.StringIO()) as out:
            self.assertEqual(audit_reachability.main(), 0)
        text = out.getvalue()
        self.assertIn('per-room reachability', text)
        self.assertIn('SUMMARY:', text)

    def test_unreachable_doors_are_counted(self):
        with unittest.mock.patch.object(audit_reachability.sim, 'find_path',
                                        return_value=None), \
             unittest.mock.patch('sys.stdout', io.StringIO()) as out:
            self.assertEqual(audit_reachability.main(), 0)
        text = out.getvalue()
        self.assertIn('SUMMARY: ', text)
        self.assertIn('reachable (100% fail)', text)


class ModuleMain(unittest.TestCase):
    def test_audit_reachability_main_guard(self):
        import runpy
        with unittest.mock.patch.object(sys, 'argv', ['audit_reachability.py']), \
             unittest.mock.patch('sys.stdout', io.StringIO()):
            runpy.run_module('audit_reachability', run_name='__main__')

    def test_probe_model_main_guard(self):
        import runpy
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False) as f:
            f.write('## first\nhello world\n')
            path = f.name
        try:
            with unittest.mock.patch.object(sys, 'argv',
                                            ['probe_model.py', 'deepseek/deepseek-v4-flash',
                                             '--prompts', path, '--limit', '1']), \
                 unittest.mock.patch.object(probe_model, 'SCORES_DIR', '/tmp/probe-scores'), \
                 unittest.mock.patch.object(probe_model._serve, 'is_model_circuit_broken',
                                            return_value=False), \
                 unittest.mock.patch.object(probe_model._serve, '_post_openrouter_raw',
                                            return_value={'choices': [{'message': {'content': 'hi'}}],
                                                          'usage': {'cost': 0.0}}), \
                 unittest.mock.patch('sys.stdout', io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    runpy.run_module('probe_model', run_name='__main__')
                self.assertEqual(cm.exception.code, 0)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# tinyprobe / probe_bare / probe_world -- tiny dev probes
# ---------------------------------------------------------------------------

class TinyProbe(unittest.TestCase):
    def test_z_returns_eleven(self):
        self.assertEqual(tinyprobe.z(), 11)

    def test_probe_bare_imports_cleanly(self):
        import importlib
        mod = importlib.import_module('probe_bare')
        self.assertEqual(mod.x, 1)

    def test_probe_world_imports_cleanly(self):
        import importlib
        with unittest.mock.patch('sys.stdout', io.StringIO()):
            mod = importlib.import_module('probe_world')
        self.assertIsNotNone(mod)


# ---------------------------------------------------------------------------
# health.py -- root-level health-check script
# ---------------------------------------------------------------------------

class HealthScript(unittest.TestCase):
    def _fake_db(self, td):
        path = os.path.join(td, 'think_tank.db')
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE kv_state (id INTEGER PRIMARY KEY, blob TEXT)')
        blob = json.dumps({
            'sim': {'lastTickEpochS': 1700000000, 'tick': 42},
            'tasks': {
                't1': {'status': 'working', 'title': 'Alpha'},
                't2': {'status': 'done', 'title': 'Beta'},
            },
            'workQueue': ['q1'],
            'agents': {'ada': {'role': 'engineer'}},
            '_pendingOnboard': {'ada': 1},
        })
        conn.execute('INSERT INTO kv_state VALUES (1, ?)', (blob,))
        conn.commit()
        conn.close()
        return path

    def test_health_reports_state(self):
        import collections
        import time as _time
        import types
        with tempfile.TemporaryDirectory() as td:
            db = self._fake_db(td)
            fake_sqlite = types.ModuleType('sqlite3')
            fake_sqlite.connect = lambda p, **k: sqlite3.connect(db)
            real_sqlite = sys.modules.get('sqlite3')
            sys.modules['sqlite3'] = fake_sqlite
            g = {
                '__name__': '__main__',
                'json': json,
                'time': _time,
                'collections': collections,
            }
            src = open(os.path.join(REPO_ROOT, 'health.py')).read()
            try:
                with unittest.mock.patch('sys.stdout', io.StringIO()) as out:
                    exec(compile(src, os.path.join(REPO_ROOT, 'health.py'), 'exec'), g)
                text = out.getvalue()
            finally:
                if real_sqlite is not None:
                    sys.modules['sqlite3'] = real_sqlite
        self.assertIn('last tick', text)
        self.assertIn('Alpha', text)


# ---------------------------------------------------------------------------
# _village_check.py -- operational server/DB health check script
# ---------------------------------------------------------------------------

class VillageCheck(unittest.TestCase):
    def test_script_reports_state_with_health_alerts(self):
        import types

        class FakeCursor:
            def __init__(self, rows): self._rows = rows
            def fetchall(self): return self._rows
            def __iter__(self): return iter(self._rows)

        class FakeConn:
            def __init__(self):
                self._alerts = [('', 'id', 'INTEGER'), ('', 'ts', 'REAL'),
                                ('', 'kind', 'TEXT'), ('', 'detail', 'TEXT')]
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=()):
                if 'sqlite_master' in sql:
                    return FakeCursor([('action_log',), ('decision_tape',), ('health_alerts',)])
                if 'health_alerts' in sql and 'PRAGMA' in sql:
                    return FakeCursor(self._alerts)
                if 'health_alerts' in sql and 'SELECT *' in sql:
                    return FakeCursor([('h1', 123, 'critical', 'disk full')])
                if 'action_log' in sql:
                    return FakeCursor([(1700000000.0, 'ada', 'walk', 'to room')])
                return FakeCursor([(1700000000.0, True, 'm', 'yes', 'choice')])

        fake_subprocess = types.SimpleNamespace(
            run=lambda *a, **k: types.SimpleNamespace(returncode=0, stdout='12345'))

        src = open(os.path.join(WORLD_DIR, '_village_check.py')).read()
        g = {
            '__name__': '__main__',
            'time': __import__('time'),
            'json': json,
            'sys': sys,
            'subprocess': fake_subprocess,
        }
        fake_serve = types.ModuleType('serve')
        fake_serve._db = lambda: FakeConn()
        g['serve'] = fake_serve
        real_serve = sys.modules.get('serve')
        sys.modules['serve'] = fake_serve
        try:
            with unittest.mock.patch('sys.stdout', io.StringIO()) as out:
                exec(compile(src, '_village_check.py', 'exec'), g)
            text = out.getvalue()
        finally:
            if real_serve is not None:
                sys.modules['serve'] = real_serve
            else:
                sys.modules.pop('serve', None)
        self.assertIn('=== server process ===', text)
        self.assertIn('alive:', text)
        self.assertIn('=== tables ===', text)
        self.assertIn('=== health alert rows ===', text)
        self.assertIn('critical', text)

    def test_script_handles_missing_health_alerts_table(self):
        import types

        class FakeCursor:
            def __init__(self, rows=()): self._rows = rows
            def fetchall(self): return self._rows
            def __iter__(self): return iter(self._rows)

        class FakeConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=()):
                if 'PRAGMA table_info(health_alerts)' in sql:
                    raise Exception('no such table: health_alerts')
                if 'sqlite_master' in sql:
                    return FakeCursor()
                if 'health_alerts' in sql:
                    raise Exception('no such table: health_alerts')
                return FakeCursor()

        src = open(os.path.join(WORLD_DIR, '_village_check.py')).read()
        fake_serve = types.ModuleType('serve')
        fake_serve._db = lambda: FakeConn()
        g = {
            '__name__': '__main__',
            'time': __import__('time'),
            'json': json,
            'sys': sys,
            'subprocess': types.SimpleNamespace(run=lambda *a, **k: types.SimpleNamespace(returncode=1, stdout='')),
            'serve': fake_serve,
        }
        real_serve = sys.modules.get('serve')
        sys.modules['serve'] = fake_serve
        try:
            with unittest.mock.patch('sys.stdout', io.StringIO()) as out:
                exec(compile(src, '_village_check.py', 'exec'), g)
            text = out.getvalue()
        finally:
            if real_serve is not None:
                sys.modules['serve'] = real_serve
            else:
                sys.modules.pop('serve', None)
        self.assertIn('err', text)


# ---------------------------------------------------------------------------
# _gapmap.py -- coverage-gap-by-function mapping tool
# ---------------------------------------------------------------------------

class GapMap(unittest.TestCase):
    def _run(self, cov_files, sim_src):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, 'coverage.json'), 'w') as f:
                json.dump({'files': cov_files}, f)
            with open(os.path.join(td, 'sim.py'), 'w') as f:
                f.write(sim_src)
            src = open(os.path.join(WORLD_DIR, '_gapmap.py')).read()
            old = os.getcwd()
            os.chdir(td)
            try:
                with unittest.mock.patch('sys.stdout', io.StringIO()) as out:
                    exec(compile(src, os.path.join(WORLD_DIR, '_gapmap.py'), 'exec'),
                         {'__name__': '__main__', 'ast': ast, 'json': json})
                return out.getvalue()
            finally:
                os.chdir(old)

    def test_maps_missing_lines_to_functions(self):
        sim_src = 'def foo():\n    pass\n\n\ndef bar():\n    return 1\n'
        # Absolute sim.py key path -> the `.get` branch is taken directly.
        cov = {'/Users/poole86/ai-village-template/world/sim.py': {'missing_lines': [2]}}
        text = self._run(cov, sim_src)
        self.assertIn('total missing: 1', text)
        self.assertIn('foo (1-2): 1 -> [2]', text)
        self.assertNotIn('bar', text)

    def test_falls_back_to_suffix_match_when_key_absent(self):
        sim_src = 'def foo():\n    pass\n\n\ndef bar():\n    return 1\n'
        # Keyed by a different path: `.get` returns {} (falsy), so the
        # endswith('sim.py') fallback loop finds and breaks on it.
        cov = {'/elsewhere/mirror/sim.py': {'missing_lines': [5]}}
        text = self._run(cov, sim_src)
        self.assertIn('total missing: 1', text)
        self.assertIn('bar (5-6): 1 -> [5]', text)

    def test_no_matching_sim_key_raises(self):
        sim_src = 'def foo():\n    pass\n\n\ndef bar():\n    return 1\n'
        # No key ends with sim.py: the fallback loop exhausts without a break
        # and the tool fails hard on the missing key (its real behavior).
        cov = {'/elsewhere/other.py': {'missing_lines': [2]}}
        with self.assertRaises(KeyError):
            self._run(cov, sim_src)


if __name__ == '__main__':
    unittest.main()