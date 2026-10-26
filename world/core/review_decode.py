"""Decode code wrappers for review, without executing them.

Agents often wrap a long Python edit script in
`python -c 'import base64;exec(base64.b64decode("..."))'` to avoid shell
quoting. The safety classifier and the director chain then see an opaque blob
and cannot judge it. This unwraps the blob for REVIEW ONLY; the original
command is still what runs.
"""
from __future__ import annotations

import base64
import binascii
import re

_B64_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
_CODE_HINTS = ('import ', 'def ', 'print(', 'echo ', 'cd ', 'cat ', 'mkdir',
               'pathlib', 'json', 'subprocess', 'open(', 'write_text', 'shutil',
               'os.', 'rm ', 'curl ', 'pip ', 'pytest')


def _printable_ratio(text):
    if not text:
        return 0.0
    good = sum(1 for ch in text if ch.isprintable() or ch in '\n\r\t')
    return good / len(text)


def _decode_candidate(token):
    raw = text = None
    token = token.strip().strip('\'"')
    padded = token + '=' * (-len(token) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(padded)
            text = raw.decode('utf-8')
        except (binascii.Error, ValueError, UnicodeDecodeError):
            continue
        if '\x00' in text or _printable_ratio(text) < 0.85:
            text = None
            continue
        return text
    return None


def review_target(target, max_chars=20000):
    """Return {text, decoded, original}. `text` is the decoded code when a
    long base64 blob decodes to readable code; otherwise it is the original.
    Never executes anything."""
    original = target or ''
    best = None
    for match in _B64_TOKEN.finditer(original):
        decoded = _decode_candidate(match.group(0))
        if not decoded:
            continue
        if not any(hint in decoded or hint in decoded.lower() for hint in _CODE_HINTS):
            continue
        if best is None or len(decoded) > len(best):
            best = decoded
    if not best:
        return {'text': original, 'decoded': False, 'original': original}
    preview = best[:max_chars]
    return {'text': preview, 'decoded': True, 'original': original}
