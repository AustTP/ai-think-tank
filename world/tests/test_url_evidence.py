import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from url_evidence import DiscoveredURLs


def test_urls_require_real_discovery_and_keep_exact_path():
    seen = DiscoveredURLs('Task source https://example.org/article')
    assert seen.allows('https://example.org/article')
    assert not seen.allows('https://example.org/invented')
    seen.observe({'links':[{'url':'https://example.org/next'}]})
    assert seen.allows('https://example.org/next')
    assert not seen.allows('https://example.org/ri[REDACTED]')
    assert not seen.allows('https://user:password@example.org/next')
