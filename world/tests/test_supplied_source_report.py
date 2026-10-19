from unittest.mock import patch

import content
import sim
import review_quality


def test_source_report_reads_evidence_and_saves_before_success(tmp_path):
    source = tmp_path / 'source.md'
    source.write_text('Verified source code')
    task = {'id': 'source-report', 'taskType': 'research', 'room': 'observatory',
            'title': 'Inspect integration', 'sourceEvidencePaths': ['source.md'],
            'instructions': 'Document the integration', 'acceptanceCriteria': 'Saved report'}
    with patch.object(content._serve, '_safe_library_path', return_value=str(source)), \
            patch.object(content._serve, 'log_action'), \
            patch.object(content._serve, 'record_library_read'), \
            patch.object(content._serve, '_mid_tier_slug', return_value='model'), \
            patch.object(content._serve, 'get_or_create_agent_key', return_value='test'), \
            patch.object(content._serve, '_http_json') as save, \
            patch.object(content, '_plain_completion', return_value='Evidence-based report') as complete, \
            patch.object(content, '_library_report_saved', return_value=True), \
            patch.object(content, '_run_spike_content') as spike, \
            patch.object(sim, '_store_content_result') as result:
        content._run_observatory_content({}, 'ada', task)
        assert 'Verified source code' in str(complete.call_args)
        assert complete.call_count == 1
        spike.assert_not_called()
        assert 'Evidence-based report' in save.call_args.args[3]['content']
        assert save.call_args.args[3]['path'].endswith('.txt')
        assert result.call_args.args[1]['ok']
        assert result.call_args.args[1]['libraryPath']


def test_missing_supplied_source_fails_without_model_call():
    task = {'id': 'missing-source', 'sourceEvidencePaths': ['missing.md']}
    with patch.object(content._serve, '_safe_library_path', return_value=None), \
            patch.object(content, '_plain_completion') as complete, \
            patch.object(sim, '_store_content_result') as result:
        content._run_supplied_source_report({}, 'ada', task)
        complete.assert_not_called()
        assert not result.call_args.args[1]['ok']


def test_failed_research_repair_preserves_type_and_source():
    task = {'id': 'research', 'assignedTo': 'ada', 'room': 'observatory',
            'taskType': 'research', 'title': 'Inspect source',
            'sourceEvidencePaths': ['source.md'], 'instructions': 'Write a report'}
    state = {'tasks': {'research': task}, 'agents': {'ada': {'id': 'ada'}}}
    with patch.object(sim, '_sim_notify_author_failed'), \
            patch.object(sim, '_maybe_escalate_stuck_gate', return_value=False), \
            patch.object(sim, '_log_quality_gate_reject'), \
            patch.object(sim, 'queue_work') as queue:
        sim._send_back_after_failure(state, task, 'No report')
        repair = queue.call_args.args[1][0]
        assert repair['taskType'] == 'research'
        assert repair['sourceEvidencePaths'] == ['source.md']


def test_legacy_pipeline_coaching_is_not_a_review_requirement():
    contract = {'instructions': "Inspect this app.\nYour deliverable 'Unrelated strategy' failed the quality pipeline. What failed: lint. Address this on your next pass.",
                'acceptanceCriteria': 'Save an inspection report', 'version': 8}
    clean = review_quality.contract_for({'reviewContract': contract})
    assert clean['instructions'] == 'Inspect this app.'
    assert clean['acceptanceCriteria'] == contract['acceptanceCriteria']
    assert clean['version'] == 8
    assert 'Unrelated strategy' in contract['instructions']


def test_document_review_receives_the_supplied_source(tmp_path):
    (tmp_path / 'report.md').write_text('The app uses public data.')
    (tmp_path / 'source.md').write_text('Numbered implementation evidence')
    parent = {'id': 'parent', 'instructions': 'Inspect app', 'acceptanceCriteria': 'Save report',
              'libraryPath': 'report.md', 'sourceEvidencePaths': ['source.md']}
    with patch.object(content._serve, '_safe_library_path', side_effect=lambda p: str(tmp_path / p)), \
            patch.object(content._serve, '_mid_tier_slug', return_value='model'), \
            patch.object(content._serve, 'get_or_create_agent_key', return_value='test'), \
            patch.object(content._serve, '_http_json', return_value={
                'reply': '{"verdict":"approve","summary":"Verified","checks":[],"risks":[],"repairStories":[]}'}) as call, \
            patch.object(sim, '_store_content_result') as result:
        content._run_document_review({}, 'reviewer', {'id': 'review'}, parent)
        assert 'Numbered implementation evidence' in str(call.call_args)
        assert result.call_args.args[1]['peerVerdict'] == 'clean'
