"""Base64 code wrappers are decoded for review, never executed."""
import base64
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve  # noqa: E402
from core.review_decode import review_target


def _wrap(code):
    blob = base64.b64encode(code.encode()).decode()
    return f"python -c 'import base64;exec(base64.b64decode(\"{blob}\"))'"


def test_decodes_a_python_wrapper():
    r = review_target(_wrap("import json\nprint('hi')\n"))
    assert r['decoded'] is True
    assert "import json" in r['text']
    assert r['original'].startswith('python -c')


def test_ordinary_command_is_unchanged():
    r = review_target('pytest -q world/tests')
    assert r['decoded'] is False
    assert r['text'] == 'pytest -q world/tests'


def test_binary_blob_is_not_decoded():
    blob = base64.b64encode(bytes(range(256))).decode()
    r = review_target(f'echo {blob}')
    assert r['decoded'] is False


def test_decoded_text_is_capped():
    r = review_target(_wrap('print("x")\n' * 5000), max_chars=100)
    assert r['decoded'] is True
    assert len(r['text']) == 100


def test_dormancy_windows_are_delegable():
    assert serve._escalation_floor('dormancy window request') == 0.85
    assert serve._escalation_floor('blocked command') == 1.0
