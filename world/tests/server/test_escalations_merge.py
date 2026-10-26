"""Escalation file merge: replace_missing must tolerate a removed record."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
from integrations import notify  # noqa: E402


def test_replace_missing_removes_a_deleted_approved_record(tmp_path, monkeypatch):
    path = tmp_path / 'escalations.json'
    path.write_text(json.dumps({
        'keep': {'status': 'pending'},
        'gone': {'status': 'approved'},
    }))
    monkeypatch.setattr(serve, 'ESCALATIONS_PATH', str(path))
    notify._save_escalations({'keep': {'status': 'pending'}},
                             replace_missing=True)
    saved = json.loads(path.read_text())
    assert set(saved) == {'keep'}
