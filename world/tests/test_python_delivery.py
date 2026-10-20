import python_delivery as delivery
from unittest.mock import patch
import content


def test_html_only_and_placeholder_do_not_satisfy_python_task(tmp_path):
    (tmp_path / 'index.html').write_text('<h1>Trading</h1>')
    assert len(delivery.check(tmp_path)) == 2
    (tmp_path / 'trader.py').write_text('"""Placeholder."""\npass\n')
    assert any('implementation' in e for e in delivery.check(tmp_path))


def test_root_level_test_file_satisfies_the_contract(tmp_path):
    (tmp_path / 'trader.py').write_text('def trade():\n    return 1\n')
    (tmp_path / 'test_trader.py').write_text('def test_trade():\n    assert True\n')
    assert delivery.check(tmp_path) == []


def test_tests_alone_do_not_replace_implementation(tmp_path):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_trader.py').write_text('def test_trade():\n    assert True\n')
    assert len(delivery.check(tmp_path)) == 1
    (tmp_path / 'trader.py').write_text('def trade():\n    return 1\n')
    assert delivery.check(tmp_path) == []


def test_story_language_is_separate_from_overarching_context():
    assert delivery.required({'instructions': 'Implement a Python API client'})
    assert not delivery.required({'instructions': 'Parent context: Python app\nStory: Build a static HTML page'})


def test_source_context_reads_modules_and_tests(tmp_path):
    (tmp_path / 'client.py').write_text('def existing():\n    return 1\n')
    assert 'def existing' in delivery.context(tmp_path)


def test_inspection_only_command_fails_before_python_pipeline(tmp_path):
    captured = []
    def http(method, base, path, *args, **kwargs):
        if path == '/api/chat':
            return {'reply': 'echo inspected'}
        if path == '/api/execute':
            return {'allowed': True, 'exitCode': 0, 'timedOut': False}
        raise AssertionError('Unexpected request: ' + path)
    with patch.object(content._serve, 'SANDBOXES_DIR', str(tmp_path)), \
            patch.object(content._serve, 'get_or_create_agent_key', return_value='test'), \
            patch.object(content._serve, '_coding_tier_slug', return_value='model'), \
            patch.object(content._serve, '_http_json', side_effect=http), \
            patch.object(content, '_run_quality_pipeline') as pipeline:
        ok = content._run_coding_content_inner({}, 'ada', {'id': 't', 'title': 'Implement Python client'},
            sandbox_override='stage-test', result_sink=lambda tid, result: captured.append(result))
        assert not ok
        assert 'No substantive Python implementation' in captured[-1]['note']
        pipeline.assert_not_called()
