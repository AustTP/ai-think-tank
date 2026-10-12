"""Authoritative context for reviews of code and its saved deliverables."""
import datetime
import hashlib
import json
import os

import review_quality


def packet(parent, source, pipeline, resolve):
    contract = review_quality.contract_for(parent)
    path = parent.get('libraryPath')
    target = resolve(path) if path else None
    report = ''
    if target and os.path.isfile(target):
        with open(target, encoding='utf-8') as stream:
            report = stream.read(40000)
    inspection = review_quality.artifact_checks(report, contract, resolve)
    metadata = []
    for check in inspection.get('checks', []):
        if check.get('kind') != 'csv':
            continue
        data_path = resolve(check['path'])
        meta_path = resolve(os.path.splitext(check['path'])[0] + '.json')
        item = {'inspection': check}
        if meta_path and os.path.isfile(meta_path):
            with open(meta_path, encoding='utf-8') as stream:
                item['provenance'] = json.load(stream)
            with open(data_path, 'rb') as stream:
                item['actualSha256'] = hashlib.sha256(stream.read()).hexdigest()
            item['hashMatches'] = item['actualSha256'] == item['provenance'].get('sha256')
        metadata.append(item)
    date = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    evidence = ('Current UTC date: ' + date + '\nSaved report path: ' + str(path) +
                '\nSaved report contents:\n' + report + '\nCode:\n' + source +
                '\nActual artifact inspection/provenance:\n' + json.dumps(metadata) +
                '\nActual quality pipeline:\n' + json.dumps(pipeline))
    references = {'criteria': {'C1': contract['instructions'].split('Story:', 1)[-1],
                               'C2': contract['acceptanceCriteria']},
                  'evidence': {'E' + str(i + 1): line for i, line in
                               enumerate(evidence.splitlines()) if line.strip()}}
    return {'text': evidence, 'references': references, 'contract': contract,
            'artifactChecks': inspection, 'currentUtcDate': date}
