import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from url_evidence import DiscoveredURLs


def test_urls_require_real_discovery_and_keep_exact_path():
    seen = DiscoveredURLs('Task source https://example.org/article')
    assert seen.allows('https://example.org/article')
    assert not seen.allows('https://example.org/invented')
    seen.observe({'links':[{'url':'https://example.org/next'}]})
    assert seen.allows('https://example.org/next')
    assert not seen.allows('https://example.org/ri[REDACTED]')
    assert not seen.allows('https://user:password@example.org/next')


def test_search_result_citation_punctuation_does_not_change_url():
    seen = DiscoveredURLs('- Official docs (https://example.org/api/overview): Summary')
    assert seen.allows('https://example.org/api/overview')
    assert not seen.allows('https://example.org/api/overview):')


def test_balanced_parentheses_in_actual_path_are_preserved():
    seen = DiscoveredURLs('- Article (https://example.org/Function_(mathematics)): Summary')
    assert seen.allows('https://example.org/Function_(mathematics)')


def test_structured_search_urls_are_not_json_escaped():
    seen = DiscoveredURLs()
    seen.observe({'results': [{'url': 'https://example.org/search?q=a&format=json'}]})
    assert seen.allows('https://example.org/search?q=a&format=json')


def test_search_then_browse_uses_exact_discovered_url():
    from unittest.mock import patch
    import serve
    search = {'ok': True, 'modelInstruction': 'search data',
              'textForModel': '- Docs (https://example.org/overview): Text'}
    page = {'allowed': True, 'textForModel': 'Actual page',
            'modelInstruction': 'page data'}
    with patch.object(serve, '_api_execute', return_value=search), \
            patch.object(serve, 'log_action'), \
            patch.object(serve, '_http_json', return_value=page) as fetch:
        tool = serve._make_web_tools_executor('oliver', 'test')
        tool('search_web', {'query': 'official docs'})
        assert 'Actual page' in tool('browse_page', {'url': 'https://example.org/overview'})
        assert fetch.call_args.args[3]['url'] == 'https://example.org/overview'
        fetch.reset_mock()
        rejected = tool('browse_page', {'url': 'https://example.org/invented'})
        assert 'valid syntax' in rejected and 'No page fetch was attempted' in rejected
        assert 'https://example.org/overview' in rejected
        fetch.assert_not_called()
