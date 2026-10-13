import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serve


def test_public_risk_management_urls_survive_secret_redaction():
    url = 'https://algosone.ai/the-science-of-risk-management-in-crypto-trading/'
    assert serve._redact_secrets(url) == url


def test_actual_api_keys_still_get_redacted():
    assert serve._redact_secrets('key: sk-' + 'a'*32) == 'key: [REDACTED]'
