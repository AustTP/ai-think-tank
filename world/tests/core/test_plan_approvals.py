"""Durable plan revisions and the approval gate."""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import plan_approvals as plans  # noqa: E402


@pytest.fixture
def plan_db(tmp_path, monkeypatch):
    monkeypatch.setenv('THINK_TANK_DB', str(tmp_path / 'plans.db'))
    monkeypatch.delenv('PLAN_APPROVALS_DB', raising=False)
    monkeypatch.delenv('CONFIRMATIONS_DB', raising=False)
    plans.expire_stale()
    return tmp_path / 'plans.db'


def _propose(**over):
    values = {'goal': 'build the thing', 'route': 'staffed',
              'payload': {'admin': 'faye', 'team': 'nora'},
              'constraints': {'budgetUsd': 2}, 'actor': 'player'}
    values.update(over)
    return plans.propose(values['goal'], values['route'], values['payload'],
                         values['actor'], constraints=values['constraints'])


def test_propose_get_and_hash(plan_db):
    plan = _propose()
    assert plan['status'] == 'pending'
    assert plan['version'] == 1
    assert plans.get(plan['plan_id'])['plan_id'] == plan['plan_id']
    assert plans.plan_hash('g', 'staffed', {}, {}) != plans.plan_hash('g', 'backlog', {}, {})
    assert plans.get('missing') is None


def test_approve_claim_apply_and_idempotency(plan_db):
    plan = _propose()
    approved = plans.approve(plan['plan_id'], 1, 'player')
    assert approved['status'] == 'approved'
    assert plans.approve(plan['plan_id'], 1, 'player')['status'] == 'approved'
    assert plans.claim_apply(plan['plan_id'], 1, 'player')['status'] == 'applying'
    assert plans.claim_apply(plan['plan_id'], 1, 'player')['status'] == 'applying'
    applied = plans.mark_applied(plan['plan_id'], 1, 'player')
    assert applied['status'] == 'applied'
    assert plans.mark_applied(plan['plan_id'], 1, 'player')['status'] == 'applied'


def test_reject_requires_reason_and_allows_revision(plan_db):
    plan = _propose()
    with pytest.raises(plans.PlanError):
        plans.reject(plan['plan_id'], 1, 'player', '')
    rejected = plans.reject(plan['plan_id'], 1, 'player', 'too broad')
    assert rejected['status'] == 'rejected'
    assert plans.reject(plan['plan_id'], 1, 'player', 'again')['status'] == 'rejected'
    revised = plans.revise(plan['plan_id'], 'narrower goal', 'staffed',
                           {'admin': 'faye', 'team': 'nora'}, 'player',
                           constraints={})
    assert revised['version'] == 2
    diff = plans.diff(plan['plan_id'], 1, 2)
    assert diff['changed'] is True
    assert 'goal' in diff['fields']


def test_revise_supersedes_pending_and_lists_history(plan_db):
    plan = _propose()
    revised = plans.revise(plan['plan_id'], 'new goal', 'backlog',
                           {'admin': 'faye'}, 'player', constraints={})
    assert revised['version'] == 2
    history = plans.history(plan['plan_id'])
    assert [p['status'] for p in history] == ['superseded', 'pending']
    assert len(plans.list_revisions('pending')) == 1
    assert plans.list_revisions('superseded')[0]['version'] == 1
    assert plans.list_revisions(None, limit=1)


def test_expiry_and_errors(plan_db):
    plan = _propose()
    with sqlite3.connect(plan_db) as conn:
        conn.execute('UPDATE plan_revisions SET expires_at = 0 WHERE id = ?',
                     (plan['plan_id'] + ':v1',))
    assert plans.expire_stale() == 1
    with pytest.raises(plans.PlanError):
        plans.approve(plan['plan_id'], 1, 'player')
    with pytest.raises(plans.PlanError):
        plans.claim_apply(plan['plan_id'], 9, 'player')
    with pytest.raises(plans.PlanError):
        plans.mark_applied(plan['plan_id'], 9, 'player')
    with pytest.raises(plans.PlanError):
        plans.mark_failed(plan['plan_id'], 9, 'x')
    with pytest.raises(plans.PlanError):
        plans.revise('missing', 'g', 'staffed', {}, 'player')
    with pytest.raises(plans.PlanError):
        plans.diff('missing', 1, 2)


def test_mark_failed_and_validation(plan_db):
    plan = _propose()
    plans.approve(plan['plan_id'], 1, 'player')
    plans.claim_apply(plan['plan_id'], 1, 'player')
    failed = plans.mark_failed(plan['plan_id'], 1, 'model down')
    assert failed['status'] == 'failed'
    assert plans.mark_failed(plan['plan_id'], 1, 'again')['status'] == 'failed'
    revised = plans.revise(plan['plan_id'], 'goal', 'backlog', {'admin': 'faye'}, 'player')
    assert revised['version'] == 2
    with pytest.raises(plans.PlanError):
        plans.propose('', 'staffed', {}, 'player')
    with pytest.raises(plans.PlanError):
        plans.propose('g', '', {}, 'player')
    with pytest.raises(plans.PlanError):
        plans.propose('g', 'staffed', {}, '')


def test_corrupt_row_ttl_and_decision_errors(plan_db):
    plan = _propose(ttl_s='not-a-number')
    with sqlite3.connect(plan_db) as conn:
        conn.execute("UPDATE plan_revisions SET constraints_json='not-json', "
                     "payload_json='not-json' WHERE id=?", (plan['plan_id'] + ':v1',))
    corrupt = plans.get(plan['plan_id'])
    assert corrupt['constraints'] is None and corrupt['payload'] is None
    with pytest.raises(plans.PlanError):
        plans.approve('missing', 1, 'player')
    expired = _propose()
    with sqlite3.connect(plan_db) as conn:
        conn.execute('UPDATE plan_revisions SET expires_at = 0 WHERE id = ?',
                     (expired['plan_id'] + ':v1',))
    with pytest.raises(plans.PlanError):
        plans.approve(expired['plan_id'], 1, 'player')


def test_clean_json_rejects_circular_payload(plan_db):
    circular = []
    circular.append(circular)
    with pytest.raises(plans.PlanError):
        plans.propose('g', 'staffed', circular, 'player')


def test_revise_limits_and_claim_status_errors(plan_db):
    plan = _propose()
    plans.approve(plan['plan_id'], 1, 'player')
    with pytest.raises(plans.PlanError):
        plans.revise(plan['plan_id'], 'g', 'staffed', {}, 'player')
    with pytest.raises(plans.PlanError):
        plans.mark_failed(plan['plan_id'], 1, 'x')
    plans.claim_apply(plan['plan_id'], 1, 'player')
    assert plans.mark_failed(plan['plan_id'], 1, '')['status'] == 'failed'
    other = _propose(goal='other')
    with sqlite3.connect(plan_db) as conn:
        conn.execute('UPDATE plan_revisions SET version=20 WHERE id=?',
                     (other['plan_id'] + ':v1',))
    with pytest.raises(plans.PlanError):
        plans.revise(other['plan_id'], 'g', 'staffed', {}, 'player')


def test_claim_and_apply_wrong_status(plan_db):
    plan = _propose()
    with pytest.raises(plans.PlanError):
        plans.claim_apply(plan['plan_id'], 1, 'player')
    with pytest.raises(plans.PlanError):
        plans.mark_applied(plan['plan_id'], 1, 'player')


def test_ttl_fallback(plan_db):
    plan = plans.propose('g', 'staffed', {}, 'player', ttl_s='bad')
    assert plan['status'] == 'pending'
