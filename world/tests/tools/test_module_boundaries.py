"""The package-boundary ratchet catches new cross-package imports."""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_SCRIPT = os.path.join(_REPO, 'scripts', 'check_module_boundaries.py')
_spec = importlib.util.spec_from_file_location('check_module_boundaries', _SCRIPT)
boundaries = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(boundaries)


def test_new_core_import_is_rejected(tmp_path):
    (tmp_path / 'core').mkdir()
    (tmp_path / 'server').mkdir()
    (tmp_path / 'core' / 'new.py').write_text('from server import app\n', encoding='utf-8')
    (tmp_path / 'server' / 'app.py').write_text('X = 1\n', encoding='utf-8')
    violations = boundaries.scan(tmp_path)
    assert violations == ['core/new.py: core may not import server']


def test_allowlisted_edge_and_lower_layer_import_pass(tmp_path):
    (tmp_path / 'core').mkdir()
    (tmp_path / 'server').mkdir()
    (tmp_path / 'core' / 'planner.py').write_text('from village import roster\n', encoding='utf-8')
    (tmp_path / 'core' / 'evidence.py').write_text('from integrations import web_helpers\n', encoding='utf-8')
    (tmp_path / 'village').mkdir()
    (tmp_path / 'village' / 'roster.py').write_text('X = 1\n', encoding='utf-8')
    (tmp_path / 'integrations').mkdir()
    (tmp_path / 'integrations' / 'web_helpers.py').write_text('X = 1\n', encoding='utf-8')
    assert boundaries.scan(tmp_path) == []


def test_parser_skips_unreadable_and_test_trees(tmp_path):
    (tmp_path / 'core').mkdir()
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'core' / 'bad.py').write_text('def (', encoding='utf-8')
    (tmp_path / 'core' / 'same.py').write_text('import core.other\nimport os\n', encoding='utf-8')
    (tmp_path / 'tests' / 'test_x.py').write_text('import server\n', encoding='utf-8')
    (tmp_path / 'root.py').write_text('import server\n', encoding='utf-8')
    assert boundaries._imports(tmp_path / 'core' / 'bad.py') == set()
    assert boundaries.scan(tmp_path) == []


def test_main_reports_pass_and_failure(monkeypatch):
    monkeypatch.setattr(boundaries, 'scan', lambda: [])
    assert boundaries.main() == 0
    monkeypatch.setattr(boundaries, 'scan', lambda: ['core/x.py: bad'])
    assert boundaries.main() == 1


def test_cli_entrypoint_passes():
    import subprocess
    result = subprocess.run([sys.executable, _SCRIPT], capture_output=True, text=True)
    assert result.returncode == 0
    assert 'passed' in result.stdout
