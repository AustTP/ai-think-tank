from unittest.mock import patch

import content
import sim
import staged_work


def test_checkpoint_retains_failed_files_without_publishing(tmp_path):
    original = tmp_path / 'project'
    original.mkdir()
    stage = staged_work.Stage(tmp_path, 'project')
    (stage.directory / 'client.py').write_text('def fetch():\n    return 1\n')
    stage.checkpoint()
    resumed = staged_work.Stage(tmp_path, 'project', resume=stage.id)
    assert resumed.resumed_from == stage.id
    assert (resumed.directory / 'client.py').exists()
    assert not (original / 'client.py').exists()
    (original / 'new.py').write_text('changed = True')
    stale = staged_work.Stage(tmp_path, 'project', resume=stage.id)
    assert stale.resumed_from is None
    assert not (stale.directory / 'client.py').exists()


def test_missing_tests_are_added_in_same_stage_before_publish(tmp_path):
    calls = []
    implementation = 'def fetch():\n    return 1\n'
    def run(snapshot, agent, task, context, sandbox_override, result_sink):
        stage = tmp_path / sandbox_override
        calls.append(sandbox_override)
        if len(calls) == 1:
            (stage / 'client.py').write_text(implementation)
            result_sink(task['id'], {'ok': False, 'note': 'Python deliverable is incomplete before quality validation: No tests/test_*.py files exist.'})
            return False
        assert (stage / 'client.py').read_text() == implementation
        assert 'Add only missing deliverables' in context
        assert 'No tests/test_*.py' in context
        (stage / 'tests').mkdir(exist_ok=True)
        (stage / 'tests/test_client.py').write_text('from client import fetch\ndef test_fetch():\n    assert fetch() == 1\n')
        result_sink(task['id'], {'ok': True, 'pipelineOk': True, 'note': 'all checks passed'})
        return True
    with patch('code_workspaces.prepare', return_value='project'), \
            patch.object(content._serve, 'SANDBOXES_DIR', str(tmp_path)), \
            patch.object(content._serve, 'get_state_from_db', return_value={}), \
            patch.object(content, '_run_coding_content_inner', side_effect=run), \
            patch('librarian.publish_validated_csv', return_value=[]), \
            patch.object(sim, '_store_content_result') as store:
        assert content._run_coding_content({}, 'ada', {'id': 't'})
        assert calls[0] == calls[1]
        assert (tmp_path / 'project/client.py').read_text() == implementation
        assert (tmp_path / 'project/tests/test_client.py').exists()
        assert store.call_args.args[1]['pipelineOk']


def test_policy_failure_is_not_automatically_retried(tmp_path):
    def run(*args, **kwargs):
        kwargs['result_sink']('t', {'ok': False, 'note': 'blocked: policy denied'})
        return False
    with patch('code_workspaces.prepare', return_value='project'), \
            patch.object(content._serve, 'SANDBOXES_DIR', str(tmp_path)), \
            patch.object(content, '_run_coding_content_inner', side_effect=run) as work, \
            patch.object(sim, '_store_content_result') as store:
        assert not content._run_coding_content({}, 'ada', {'id': 't'})
        assert work.call_count == 1
        checkpoint = store.call_args.args[1]['stagedWorkspace']
        assert (tmp_path / (checkpoint + '.repair.json')).exists()
