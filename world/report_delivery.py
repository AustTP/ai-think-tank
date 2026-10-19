"""Publish explicitly declared text reports after coding quality validation."""
import hashlib
import json
from pathlib import Path
import re

from coding_library import contained

GUIDANCE = '''When your task requires a saved human-readable report, generate its .txt file
inside the workspace. Also write report-deliverables.json containing:
{"reports":[{"source":"portfolio_report.txt","basis":"mocked fixture data; not live account data"}]}
Use an accurate basis describing the actual input provenance. After all quality checks pass,
the server copies declared text reports into Library reports/ and records their exact paths
for independent review. Never label fixture/sample data as real retrieved account data.
Do not publish prerequisite notes as your completed report. No manifest is needed for code-only tasks.'''


def publish(task, workspace, serve):
    manifest = contained(workspace, 'report-deliverables.json')
    if not manifest.is_file():
        return []
    if manifest.stat().st_size > 16000:
        raise ValueError('Report manifest is too large.')
    entries = json.loads(manifest.read_text(encoding='utf-8')).get('reports')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 5:
        raise ValueError('Declare between one and five reports.')
    prepared = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Report entries must be objects.')
        source = contained(workspace, entry.get('source'))
        basis = entry.get('basis')
        if source.suffix != '.txt' or not source.is_file() or source.stat().st_size > 1000000:
            raise ValueError('Reports must be existing text files of at most 1 MB.')
        if not isinstance(basis, str) or not basis.strip() or len(basis) > 2000:
            raise ValueError('Report input provenance is required.')
        text = source.read_text(encoding='utf-8')
        if not text.strip():
            raise ValueError('An empty report cannot be delivered.')
        body = ('Input provenance: ' + basis + '\n'
                'Status: quality checks passed; independent acceptance review required.\n\n' + text)
        digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
        tid = re.sub(r'[^A-Za-z0-9_-]', '-', str(task.get('id') or 'adhoc'))[:100]
        name = f'reports/{tid}-{digest[:16]}.txt'
        destination = contained(serve.LIBRARY_DIR, name)
        prepared.append((name, destination, body, digest, entry['source'], basis))
    for name, destination, body, digest, source, basis in prepared:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open('x', encoding='utf-8', newline='') as stream:
                stream.write(body)
        except FileExistsError:
            if destination.read_bytes() != body.encode('utf-8'):
                raise ValueError('Existing report differs; refusing to overwrite it.')
        provenance = contained(serve.LIBRARY_DIR, name + '.provenance.json')
        provenance.write_text(json.dumps({'taskId': task.get('id'), 'source': source,
            'basis': basis, 'sha256': digest, 'method': 'Quality-validated workspace text report; acceptance remains unverified.'}), encoding='utf-8')
    return [item[0] for item in prepared]
