import os

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def test_ops_page_has_per_agent_trace_panel():
    with open(os.path.join(_ROOT, 'world/agents/pulse_ui.py'), encoding='utf-8') as fh:
        html = fh.read()
    assert 'Agent traces' in html
    assert '/api/ops/agents' in html
    assert '/trace?limit=150' in html
    assert 'loadAgents' in html and 'loadTrace' in html
    assert 'traceAuto' in html
