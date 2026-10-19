import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import training_review as review


def test_review_and_export(monkeypatch, tmp_path):
    monkeypatch.setattr(review.capture, 'config', lambda: {'TRAINING_CAPTURE_PATH': str(tmp_path / 'capture.db')})
    monkeypatch.setattr(review.capture, 'ROOT', tmp_path)
    # Exporter source remains in the repository; output stays isolated.
    source = Path(__file__).resolve().parents[2] / 'scripts/export_training_interactions.py'
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts/export_training_interactions.py').write_text(source.read_text(), encoding='utf-8')
    payload = json.dumps({'context': {'agent_id': 'ada', 'role': 'Researcher'}, 'request': {'messages': [{'role': 'user', 'content': 'hello'}]}, 'response': {'choices': [{'message': {'content': 'hello'}, 'finish_reason': 'stop'}]}})
    with review.capture.database() as conn:
        for key, ok, flags in [('good', 1, '[]'), ('bad', 0, '[]'), ('flagged', 1, '["omitted"]')]:
            conn.execute('INSERT INTO interactions(id,ts,trace_id,group_id,ok,payload,flags) VALUES (?,1,?,?,?, ?,?)', (key, key, key, ok, payload, flags))
    app = FastAPI()
    app.include_router(review.router)
    client = TestClient(app)
    listing = client.get('/api/training/interactions').json()
    assert listing['total'] == 1
    assert [item['id'] for item in listing['items']] == ['good']
    assert listing['counts'] == {'pending': 1, 'approved': 0, 'rejected': 0}
    assert client.get('/api/training/interactions?status=all').json()['total'] == 1
    assert client.get('/api/training/interactions/missing').status_code == 404
    assert client.get('/api/training/interactions/good').json()['payload']['request']['messages']
    good = client.get('/api/training/interactions/good').json()
    assert good['can_approve'] is True and good['approval_blockers'] == []
    bad = client.get('/api/training/interactions/bad').json()
    assert bad['can_approve'] is False and 'failed' in bad['approval_blockers'][0]
    flagged = client.get('/api/training/interactions/flagged').json()
    assert flagged['can_approve'] is False and 'omitted' in ' '.join(flagged['approval_blockers'])
    assert client.post('/api/training/interactions/good/review', json={'status': 'approved', 'note': ''}).status_code == 400
    for key in ('bad', 'flagged'):
        assert client.post(f'/api/training/interactions/{key}/review', json={'status': 'approved', 'note': 'Checked'}).status_code == 400
    assert client.post('/api/training/interactions/good/review', json={'status': 'approved', 'note': 'Checked'}).status_code == 200
    assert client.get('/api/training/interactions?status=approved').json()['total'] == 1
    exported = client.post('/api/training/export', json={}).json()
    assert exported['summary']['train'] == 1
    with review.capture.database() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='training_review'").fetchone()[0] == 1


@pytest.mark.parametrize('context,allowed', [
    ({'agent_id': 'ada', 'role': 'Researcher'}, True),
    ({'agent_id': 'new-worker', 'role': 'Developer'}, True),
    ({'agent_id': 'reviewer-tessa', 'role': 'Researcher'}, False),
    ({'agent_id': 'new-admin', 'role': 'Administrator'}, False),
    ({'agent_id': 'ada', 'role': 'Researcher', 'isDirector': True}, False),
    ({'agent_id': 'ada', 'role': 'Researcher', 'review_of': 'story'}, False),
    ({'agent_id': 'owen', 'role': 'Scrum Master'}, False),
    ({'agent_id': 'unknown'}, False),
    ({'agent_id': 'ada'}, True),
])
def test_worker_roles(context, allowed):
    assert review.worker_interaction({'context': context}) is allowed
