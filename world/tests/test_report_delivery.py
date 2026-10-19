import hashlib
import json
from types import SimpleNamespace

import pytest

import report_delivery


def make_storage(tmp_path):
    workspace = tmp_path / 'workspace'
    library = tmp_path / 'library'
    workspace.mkdir()
    library.mkdir()
    (workspace / 'report.txt').write_text('Portfolio report\nHoldings\nFixture BTC: 1', encoding='utf-8')
    (workspace / 'report-deliverables.json').write_text(json.dumps({'reports':[
        {'source':'report.txt','basis':'Mocked test fixture, not live account data'}]}))
    return workspace, SimpleNamespace(LIBRARY_DIR=library)


def test_report_saved_with_provenance_and_stable_path(tmp_path):
    workspace, serve = make_storage(tmp_path)
    paths = report_delivery.publish({'id':'task-1'},workspace,serve)
    assert paths == report_delivery.publish({'id':'task-1'},workspace,serve)
    target = serve.LIBRARY_DIR / paths[0]
    text = target.read_text()
    assert 'Mocked test fixture' in text
    assert 'independent acceptance review required' in text
    provenance = json.loads(target.with_name(target.name+'.provenance.json').read_text())
    assert provenance['sha256'] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert provenance['taskId'] == 'task-1'
    target.write_text('existing local edits')
    with pytest.raises(ValueError, match='overwrite'):
        report_delivery.publish({'id':'task-1'},workspace,serve)


@pytest.mark.parametrize('entry', [
    {'source':'../report.txt','basis':'fixture'},
    {'source':'missing.txt','basis':'fixture'},
    {'source':'report.txt'},
    {'source':'report.py','basis':'fixture'},
])
def test_invalid_artifacts_not_published(tmp_path,entry):
    workspace,serve=make_storage(tmp_path)
    (workspace/'report-deliverables.json').write_text(json.dumps({'reports':[entry]}))
    with pytest.raises(ValueError):
        report_delivery.publish({'id':'task-1'},workspace,serve)
    assert not list(serve.LIBRARY_DIR.iterdir())


def test_no_manifest_means_no_report(tmp_path):
    assert report_delivery.publish({},tmp_path,SimpleNamespace()) == []
