#!/usr/bin/env python3
"""Check BUSINESS-PROCESSES.md for review dates and live code paths.

The document is a map, not the source of truth. This check keeps the map
honest in two cheap ways: every BP section carries a last-checked date, and
every code path named in Appendix C still resolves in the repo.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / 'BUSINESS-PROCESSES.md'
DATE = re.compile(r'^\*\*Last checked:\*\* (\d{4}-\d{2}-\d{2})$', re.M)
HEADING = re.compile(r'^### (BP-\d+(?:\.\d+)?)\b.*$', re.M)
TOKEN = re.compile(r'`([^`]+)`')


def _appendix_c(text):
    start = text.index('## 19. Appendix C')
    end = text.index('## 20. Appendix D')
    return text[start:end]


def _resolves(token, root=ROOT):
    candidates = (root / token, root / 'world' / token,
                  root / 'world' / 'server' / token,
                  root / 'world' / 'core' / token,
                  root / 'world' / 'integrations' / token,
                  root / 'world' / 'agents' / token)
    return any(path.exists() for path in candidates)


def check_document(root=None):
    root = Path(root or ROOT)
    text = (root / 'BUSINESS-PROCESSES.md').read_text(encoding='utf-8')
    errors = []
    headings = list(HEADING.finditer(text))
    if not headings:
        return ['no BP sections found']
    for match in headings:
        tail = text[match.end():match.end() + 120]
        if not DATE.search(tail):
            errors.append(f'{match.group(1)} has no last-checked date')
    appendix = _appendix_c(text)
    for token in sorted(set(TOKEN.findall(appendix))):
        looks_like_path = ('/' in token or token.endswith(('.py', '.md', '.json')))
        if not looks_like_path or token.startswith(('http://', 'https://')):
            continue
        if '*' in token:
            continue
        if not _resolves(token, root):
            errors.append(f'Appendix C path does not resolve: {token}')
    return errors


def main():
    errors = check_document()
    if errors:
        print('\n'.join(errors))
        return 1
    print('business processes check passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
