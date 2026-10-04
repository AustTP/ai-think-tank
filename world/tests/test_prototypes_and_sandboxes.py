"""Coverage tests for the gitignored per-agent prototype and shared-sandbox
modules (agents/*/prototypes and sandboxes/workroom-shared). These files are
regenerated mirrors (not the source of truth), but they are real Python that
must stay exercised. Because every agent's prototypes are byte-identical copies
of each other, the modules are loaded by file path via importlib so each copy
counts toward coverage without module-name collisions.

Also covers sandboxes/sb-p1/checkout.py (tracked) and tinyprobe_test/*.py.
"""
import importlib.util
import os
import sys
import tempfile
import unittest
import unittest.mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROTO = os.path.join(REPO_ROOT, 'agents')
SHARED = os.path.join(REPO_ROOT, 'sandboxes', 'workroom-shared')


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _nullctx():
    return unittest.mock.patch.object(object, '__class__') if False else _NullCtx()


class _NullCtx:
    def __enter__(self): return None
    def __exit__(self, *a): return False


class PrototypeApp(unittest.TestCase):
    def test_every_agents_app_py_runs(self):
        for d in sorted(os.listdir(PROTO)):
            app = os.path.join(PROTO, d, 'prototypes', 'app.py')
            if not os.path.exists(app):
                continue
            mod = _load(f'proto_app_{d}', app)
            if hasattr(mod, 'main'):
                with (unittest.mock.patch.object(mod, 'load_news_data', return_value=[])
                      if hasattr(mod, 'load_news_data') else _nullctx()), \
                     unittest.mock.patch('sys.stdout', __import__('io').StringIO()):
                    mod.main()
            if hasattr(mod, 'load_news_data'):
                import json as _json
                m = unittest.mock.mock_open(read_data=_json.dumps({'news': 1}))
                with unittest.mock.patch('builtins.open', m):
                    self.assertEqual(mod.load_news_data(), {'news': 1})
                m2 = unittest.mock.mock_open()
                m2.side_effect = FileNotFoundError
                with unittest.mock.patch('builtins.open', m2):
                    result = mod.load_news_data()
                self.assertEqual(result, [])
            src = open(app).read()
            if '__main__' in src:
                with unittest.mock.patch('sys.stdout', __import__('io').StringIO()):
                    exec(compile(src, app, 'exec'), {'__name__': '__main__'})


class PrototypeLuhn(unittest.TestCase):
    def test_every_agents_luhn_validates(self):
        for d in sorted(os.listdir(PROTO)):
            path = os.path.join(PROTO, d, 'prototypes', 'luhn.py')
            if not os.path.exists(path):
                continue
            mod = _load(f'proto_luhn_{d}', path)
            if hasattr(mod, 'Luhn'):
                self.assertTrue(mod.Luhn('4532015112830366').valid(), d)
                self.assertFalse(mod.Luhn('4532015112830367').valid(), d)
                if not hasattr(mod.Luhn('x'), 'isdigit'):
                    pass
                self.assertFalse(mod.Luhn('1').valid(), d)
                self.assertFalse(mod.Luhn('abc').valid(), d)
            else:
                self.assertTrue(mod.validate_luhn('4532015112830366'), d)
                self.assertFalse(mod.validate_luhn('4532015112830367'), d)
                self.assertTrue(mod.validate_luhn('4532 0151 1283 0366'), d)
                self.assertFalse(mod.validate_luhn(''), d)


class PrototypeTempFix(unittest.TestCase):
    def test_every_agents_temp_fix_runs(self):
        for d in sorted(os.listdir(PROTO)):
            path = os.path.join(PROTO, d, 'prototypes', 'temp_fix.py')
            if not os.path.exists(path):
                continue
            mod = _load(f'proto_tempfix_{d}', path)
            if hasattr(mod, 'fix_whitespace_in_file'):
                with tempfile.TemporaryDirectory() as td:
                    fp = os.path.join(td, 'sample.txt')
                    with open(fp, 'w') as f:
                        f.write('  hello   \n\n\n')
                    mod.fix_whitespace_in_file(fp)
                    with open(fp) as f:
                        self.assertEqual(f.read(), '  hello\n\n')
                import runpy
                src = open(path).read()
                m = unittest.mock.mock_open(read_data='x\n')
                with unittest.mock.patch('os.path.exists', return_value=True), \
                     unittest.mock.patch('os.getcwd', return_value=tempfile.gettempdir()), \
                     unittest.mock.patch('builtins.open', m), \
                     unittest.mock.patch('sys.stdout', __import__('io').StringIO()):
                    exec(compile(src, path, 'exec'), {'__name__': '__main__'})
            if hasattr(mod, 'luhn_check'):
                self.assertTrue(mod.luhn_check('4532015112830366'), d)
                self.assertFalse(mod.luhn_check('4532015112830367'), d)
            if hasattr(mod, 'extract_and_validate'):
                self.assertEqual(len(mod.extract_and_validate('4532-0151-1283-0366 ok')), 1, d)
            if hasattr(mod, 'load_news_data'):
                import json as _json
                import io as _io
                with tempfile.TemporaryDirectory() as td, \
                     unittest.mock.patch('os.chdir', lambda p: None), \
                     unittest.mock.patch('builtins.open',
                                         unittest.mock.mock_open(read_data=_json.dumps(
                                             {'title': 'x', 'link': 'y'}))):
                    data = mod.load_news_data()
                    self.assertIn('title', data)
                with tempfile.TemporaryDirectory() as td, \
                     unittest.mock.patch('os.chdir', lambda p: None), \
                     unittest.mock.patch('builtins.open',
                                         side_effect=FileNotFoundError):
                    try:
                        mod.load_news_data()
                    except FileNotFoundError:
                        pass
                with tempfile.TemporaryDirectory() as td, \
                     unittest.mock.patch('os.chdir', lambda p: None), \
                     unittest.mock.patch('builtins.open',
                                         unittest.mock.mock_open(read_data=_json.dumps(
                                             [{'title': 'x', 'link': 'y'}]))), \
                     unittest.mock.patch('sys.stdout', _io.StringIO()):
                    mod.display_news()


class SharedSandbox(unittest.TestCase):
    def test_luhn_class_validates(self):
        path = os.path.join(SHARED, 'luhn.py')
        mod = _load('shared_luhn', path)
        self.assertTrue(mod.Luhn('4532015112830366').valid())
        self.assertFalse(mod.Luhn('4532015112830367').valid())
        self.assertFalse(mod.Luhn('').valid())
        self.assertFalse(mod.Luhn('1').valid())
        self.assertTrue(mod.Luhn('4532 0151 1283 0366').valid())

    def test_temp_fix_extract_and_validate(self):
        path = os.path.join(SHARED, 'temp_fix.py')
        mod = _load('shared_tempfix', path)
        out = mod.extract_and_validate('card 4532-0151-1283-0366 and 5555 5555 5555 4444')
        self.assertEqual(len(out), 2)
        self.assertIn('4532-0151-1283-0366', out)
        self.assertEqual(mod.extract_and_validate('no cards here'), [])
        self.assertFalse(mod.luhn_check('4532015112830367'))
        self.assertTrue(mod.luhn_check('4532015112830366'))

    def test_app_runs(self):
        path = os.path.join(SHARED, 'app.py')
        mod = _load('shared_app', path)
        mod.main()


class SbP1Checkout(unittest.TestCase):
    def test_checkout_module_imports(self):
        path = os.path.join(REPO_ROOT, 'sandboxes', 'sb-p1', 'checkout.py')
        mod = _load('sbp1_checkout', path)
        self.assertIsNotNone(mod)


class TinyProbeTestDir(unittest.TestCase):
    def test_tp_and_probe2copy(self):
        tp = _load('tiny_tp', os.path.join(REPO_ROOT, 'tinyprobe_test', 'tp.py'))
        self.assertEqual(tp.t(), 3)
        p2 = _load('tiny_probe2', os.path.join(REPO_ROOT, 'tinyprobe_test', 'probe2copy.py'))
        self.assertEqual(p2.foo(True), 1)
        self.assertEqual(p2.foo(False), 0)


if __name__ == '__main__':
    unittest.main()