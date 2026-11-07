"""The benchmark renderer reports only recorded runs.

render_benchmark is the publish-path for eval numbers: a wrong number here
is a public misstatement. These tests pin the honesty contract: latest run
per model wins, missing inputs render as explicit gaps, and nothing is
inferred for an unrecorded baseline."""
import importlib.util
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]


def _renderer():
    spec = importlib.util.spec_from_file_location(
        'render_benchmark', _ROOT / 'evals' / 'render_benchmark.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_run(tmp_path, name, model, run_at, passed, total, cost=None):
    record = {
        'runAt': run_at, 'model': model, 'endpoint': 'local-tank',
        'agentId': 'admin', 'gitSha': 'abc1234',
        'cases': [{'name': 'c1', 'ok': True, 'costUsd': cost}],
        'passed': passed, 'total': total, 'durationMs': 100,
        'costUsd': cost, 'pricedCalls': 1 if cost is not None else 0,
    }
    path = tmp_path / name
    path.write_text(json.dumps(record), encoding='utf-8')
    return path


def test_latest_run_per_model_wins(tmp_path):
    rb = _renderer()
    _write_run(tmp_path, 'a-old.json', 'openrouter/auto',
               '2026-01-01T00:00:00+00:00', 1, 2, 0.01)
    _write_run(tmp_path, 'b-new.json', 'openrouter/auto',
               '2026-02-01T00:00:00+00:00', 2, 2, 0.02)
    latest = rb.latest_per_model(rb.load_runs(tmp_path))
    assert latest['openrouter/auto']['passed'] == 2


def test_empty_results_dir_renders_explicit_gaps(tmp_path):
    rb = _renderer()
    html = rb.render(rb.load_runs(tmp_path), '2026-01-01T00:00:00+00:00')
    assert 'No tank run recorded yet' in html
    assert 'Baseline missing' in html
    assert 'Comparison' not in html  # nothing to compare, nothing inferred


def test_unpriced_run_never_invents_a_cost(tmp_path):
    rb = _renderer()
    _write_run(tmp_path, 'a.json', 'openrouter/auto',
               '2026-01-01T00:00:00+00:00', 2, 2, None)
    latest = rb.latest_per_model(rb.load_runs(tmp_path))
    assert rb.fmt_cost(latest['openrouter/auto']) == 'unpriced'
    html = rb.render(latest and rb.load_runs(tmp_path), 'x')
    assert '$' not in html.split('Comparison')[0].split('<h2>')[1]


def test_comparison_requires_both_sides(tmp_path):
    rb = _renderer()
    _write_run(tmp_path, 'a.json', 'openrouter/auto',
               '2026-01-01T00:00:00+00:00', 2, 2, 0.01)
    html = rb.render(rb.load_runs(tmp_path), '2026-01-01T00:00:00+00:00')
    assert 'Baseline missing' in html
    assert 'Comparison' not in html
    _write_run(tmp_path, 'b.json', rb.BASELINE_MODEL,
               '2026-01-02T00:00:00+00:00', 1, 2, 0.005)
    html = rb.render(rb.load_runs(tmp_path), '2026-01-02T00:00:00+00:00')
    assert 'Comparison' in html
    assert '1/2' in html and '2/2' in html


def test_cache_hit_rate_only_when_recorded(tmp_path):
    rb = _renderer()
    # No token breakdown -> the line stays silent, nothing inferred.
    _write_run(tmp_path, 'a.json', 'openrouter/auto',
               '2026-01-01T00:00:00+00:00', 2, 2, 0.01)
    latest = rb.latest_per_model(rb.load_runs(tmp_path))
    assert rb.fmt_cache(latest['openrouter/auto']) == ''
    # Recorded breakdown -> a real percentage from the provider's numbers.
    (tmp_path / 'a.json').unlink()
    record = json.loads(json.dumps({
        'runAt': '2026-01-01T00:00:00+00:00', 'model': 'openrouter/auto',
        'endpoint': 'local-tank', 'agentId': 'admin', 'gitSha': 'abc1234',
        'cases': [{'name': 'c1', 'ok': True}],
        'passed': 2, 'total': 2, 'durationMs': 100, 'costUsd': 0.01,
        'tokens': {'prompt_tokens': 1000, 'cached_tokens': 800},
    }))
    (tmp_path / 'b.json').write_text(json.dumps(record), encoding='utf-8')
    latest = rb.latest_per_model(rb.load_runs(tmp_path))
    assert rb.fmt_cache(latest['openrouter/auto']) == 'cache hit 80%'
    html = rb.render(rb.load_runs(tmp_path), 'x')
    assert 'cache hit 80%' in html


def test_cache_line_absent_prompt_tokens_but_no_cached(tmp_path):
    rb = _renderer()
    record = {
        'runAt': '2026-01-01T00:00:00+00:00', 'model': 'openrouter/auto',
        'endpoint': 'local-tank', 'agentId': 'admin', 'gitSha': 'abc1234',
        'cases': [{'name': 'c1', 'ok': True}],
        'passed': 2, 'total': 2, 'durationMs': 100, 'costUsd': 0.01,
        'tokens': {'prompt_tokens': 1000},
    }
    (tmp_path / 'a.json').write_text(json.dumps(record), encoding='utf-8')
    latest = rb.latest_per_model(rb.load_runs(tmp_path))
    assert rb.fmt_cache(latest['openrouter/auto']) == 'cache n/a'
    assert rb.fmt_cache(None) == ''


def test_corrupt_record_is_skipped_not_fatal(tmp_path):
    rb = _renderer()
    (tmp_path / 'broken.json').write_text('{not json', encoding='utf-8')
    assert rb.load_runs(tmp_path) == []
