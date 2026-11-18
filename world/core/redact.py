"""Edge scrubbing (the Relay pattern, 2026-11-17): strip secrets from text
BEFORE it lands in any ledger.

Sentry's Relay proxies every event through a scrubbing layer at the edge so
secrets never reach the control plane. The tank's equivalent edge is ledger
ingestion: evidence claims, epistemic propositions, validation procedures,
and decision rationales are all freeform text an agent (or an external
page's content) produced, and a leaked credential in a stored blob is a
credential forever. Scrubbing at QUERY time is too late -- the write already
replicated into backups, digests, and dumps.

Deliberately conservative: only high-confidence secret shapes are scrubbed
(API keys, bearer tokens, JWTs, cloud credentials, password assignments,
URLs with embedded credentials). Emails, names, and IPs are NOT scrubbed --
they are usually the substance of a research claim, and mangling them makes
ledgers useless. When in doubt, leave the text alone: a false negative
leaves one secret in one row, a false positive corrupts every row.

Every caller reports the redaction count so the audit trail can show that
scrubbing happened (the fact of a redaction is not itself a secret).
"""

import re

# High-confidence secret shapes. Each pattern is intentionally narrow:
# matching it in prose almost always means a real credential.
_PATTERNS = (
    # OpenAI-style keys and other sk- service keys.
    ('api_key', re.compile(r'\bsk-[A-Za-z0-9_-]{16,}\b')),
    # Google API keys (AIza...).
    ('google_api_key', re.compile(r'\bAIza[0-9A-Za-z_-]{30,}\b')),
    # AWS access key ids.
    ('aws_key', re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    # JSON web tokens: three base64url segments.
    ('jwt', re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b')),
    # Bearer/Authorization headers.
    ('bearer', re.compile(r'\b(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}')),
    # Password assignments in prose, config, or query strings.
    ('password', re.compile(
        r'\b(?i:password|passwd|pwd|secret)\s*[=:]\s*\S{6,}')),
    # URLs with embedded credentials (https://user:pass@host/...).
    ('url_credentials', re.compile(
        r'\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s:/@]+@[^\s]')),
)

# Replacement keeps the KIND visible (auditable) but not the value.
_REPLACEMENT = '[REDACTED:{kind}]'


def scrub(text):
    """Redact high-confidence secrets from freeform ledger text. Returns
    (clean_text, redaction_kinds) -- kinds is the ordered list of secret
    classes found (possibly empty). Idempotent: already-redacted text
    scrubs to itself, so replaying a stored blob is safe."""
    text = str(text or '')
    found = []
    for kind, pattern in _PATTERNS:
        def _sub(match, kind=kind):
            found.append(kind)
            return _REPLACEMENT.format(kind=kind)
        text = pattern.sub(_sub, text)
    return text, found


def scrub_result(text):
    """Scrub + a compact report for callers that log the fact of a
    redaction: {'text': clean, 'redacted': n, 'kinds': [...]}."""
    clean, kinds = scrub(text)
    return {'text': clean, 'redacted': len(kinds), 'kinds': kinds}
