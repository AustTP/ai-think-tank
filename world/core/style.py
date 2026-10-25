"""Shared writing rules for agent output.

The no AI slop rules live in the prompt directive and in plain_writing.json.
This module only enforces the two characters we never want in outbound text:
the em dash and the en dash. Replace them with a hyphen or a plain separator;
do not strip the sentence around them.
"""

_EM_DASHES = ('\u2014', '\u2013', '\u2212')


def sanitize_text(text):
    """Return text with em/en dashes replaced by an ordinary hyphen."""
    if not isinstance(text, str) or not text:
        return text
    text = text.replace('\u2014', ' - ')
    for mark in _EM_DASHES[1:]:
        text = text.replace(mark, '-')
    return text
