import json
from pathlib import Path

import review_quality as rq


def test_clean_vote_errors_all():
    result = {'acceptanceAssessment': {'verified': False, 'checks': [{'status': 'unmet'}]},
              'ok': False, 'pipelineOk': False, 'peerRepairStories': [1],
              'peerChecks': ['[MISSING_ARTIFACT] x'],
              'reviewDiagnostics': {'artifactChecks': {'errors': ['x']}, 'citationErrors': ['u']},
              'checklistEscalated': True, 'checklistGrades': [{'verdict': 'fails_requirement'}]}
    errs = rq.clean_vote_errors(result)
    assert len(errs) >= 8
    assert rq.clean_vote_errors({'acceptanceAssessment': {'verified': True, 'checks': [{'status': 'met', 'evidence': 'x'}]},
                                 'ok': True, 'pipelineOk': True}) == []


def test_known_failure_and_contract():
    assert rq.known_failure({'ok': False})
    assert rq.known_failure({'acceptanceAssessment': {'checks': [{'status': 'unmet'}]}})
    assert not rq.known_failure({'ok': True, 'pipelineOk': True})
    c = rq.contract_for({'instructions': 'x\nYour deliverable \'a\' failed the quality pipeline.\nmore\nAddress this on your next pass.\nPeer review of y'})
    assert 'failed the quality pipeline' not in c['instructions']
    c2 = rq.contract_for({'reviewContract': {'instructions': 'Story: z', 'acceptanceCriteria': 'a', 'version': 1}})
    assert c2['instructions'] == 'Story: z'
    assert len(rq.digest('x')) == 64


def test_artifact_references_and_date():
    doc = 'made a.csv; b.csv is missing'
    contract = {'instructions': 'Story: need c.csv', 'acceptanceCriteria': ''}
    refs = rq.artifact_references(doc, contract)
    assert 'a.csv' in refs and 'b.csv' not in refs and 'c.csv' in refs
    assert rq._date('1700000000').year == 2023
    assert rq._date('1700000000000').year == 2023
    assert rq._date('2024-01-01').year == 2024
    assert rq._date('bad') is None


def test_artifact_checks_paths(tmp_path, monkeypatch):
    contract = {'instructions': 'Story: save historical dataset in data.csv', 'acceptanceCriteria': ''}
    doc = 'data.csv'
    assert 'missing' in ' '.join(rq.artifact_checks(doc, contract, lambda p: None)['errors'])
    f = tmp_path / 'data.csv'; f.write_text('timestamp,open,high,low,close\n2024-01-01,1,2,0.5,1.5\n')
    out = rq.artifact_checks(doc, contract, lambda p: str(f))
    assert out['passed'] is False or out['checks']
    # too large
    monkeypatch.setattr(rq.os.path, 'getsize', lambda p: 21 * 1024 * 1024)
    out = rq.artifact_checks(doc, contract, lambda p: str(f))
    assert '20 MB' in ' '.join(out['errors'])
    monkeypatch.undo()
    # duplicate/missing
    f.write_text('timestamp,open,high,low,close\n2024-01-01,1,2,0.5,1.5\n2024-01-01,1,2,0.5,1.5\n,1,2,0.5,1.5\n')
    out = rq.artifact_checks(doc, contract, lambda p: str(f))
    assert 'duplicate' in ' '.join(out['errors']).lower()
    # notebook
    nb = {'cells': [{'cell_type': 'code', 'source': 'x', 'execution_count': None, 'outputs': [{'output_type': 'error'}]}]}
    n = tmp_path / 'n.ipynb'; n.write_text(json.dumps(nb))
    out = rq.artifact_checks('n.ipynb', {'instructions': 'Story: notebook', 'acceptanceCriteria': ''}, lambda p: str(n))
    assert 'Notebook' in ' '.join(out['errors'])
    # unreadable / corrupt JSON
    bad = tmp_path / 'bad.csv'; bad.write_text('\xff')
    out = rq.artifact_checks('bad.csv', {'instructions': 'Story: x', 'acceptanceCriteria': ''}, lambda p: str(bad))
    assert out['errors']


def test_artifact_csv_extra_and_exception(tmp_path, monkeypatch):
    contract = {'instructions': 'Story: x', 'acceptanceCriteria': ''}
    f = tmp_path / 'x.csv'
    f.write_text('timestamp,open\n2024,1,extra\n')
    out = rq.artifact_checks('x.csv', contract, lambda p: str(f))
    assert 'more values' in ' '.join(out['errors'])
    monkeypatch.setattr(rq, 'open', lambda *a, **k: (_ for _ in ()).throw(OSError('x')), raising=False)
    out = rq.artifact_checks('x.csv', contract, lambda p: str(f))
    assert 'Cannot inspect' in ' '.join(out['errors'])
