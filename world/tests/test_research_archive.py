from unittest.mock import patch

import content


def test_incomplete_research_is_archived_without_becoming_successful():
    with patch.object(content._serve, '_http_json') as save, \
            patch.object(content._serve, 'get_or_create_agent_key', return_value='test'), \
            patch.object(content, '_library_report_saved', return_value=True):
        result = content.archive_research_result({'id': 'attempt', 'title': 'Inspect API'}, 'oliver',
            {'ok': False, 'note': 'Source retrieval failed', 'researchEvidence': [{'url': 'source', 'text': 'partial evidence'}]})
        assert result['ok'] is False
        assert result['libraryPath'].endswith('.txt')
        assert 'partial evidence' in save.call_args.args[3]['content']
        assert 'incomplete' in save.call_args.args[3]['content']
        assert 'researchEvidence' not in result


def test_archive_write_failure_prevents_success():
    with patch.object(content._serve, '_http_json'), \
            patch.object(content._serve, 'get_or_create_agent_key', return_value='test'), \
            patch.object(content, '_library_report_saved', return_value=False):
        result = content.archive_research_result({'id': 'attempt'}, 'oliver', {'ok': True, 'note': 'Findings'})
        assert result['ok'] is False
        assert result['reviewFailureKind'] == 'service_unavailable'


def test_existing_saved_report_is_reused():
    with patch.object(content, '_library_report_saved', return_value=True), \
            patch.object(content._serve, '_http_json') as save:
        result = content.archive_research_result({'id': 'attempt'}, 'oliver',
                                                {'ok': True, 'libraryPath': 'report.txt'})
        assert result['libraryPath'] == 'report.txt'
        save.assert_not_called()
