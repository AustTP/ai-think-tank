"""Durable usage receipts and unpriced-usage budget holds."""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import integrations.bank as bank  # noqa: E402


@pytest.fixture
def accounting_db(tmp_path, monkeypatch):
    db = tmp_path / 'receipts.db'
    monkeypatch.setattr(serve, 'DB_PATH', str(db))
    serve.init_db()
    bank._RECEIPTS_REPLAYED['done'] = True
    yield db
    bank._RECEIPTS_REPLAYED['done'] = False


def test_priced_receipt_applies_exactly_once(accounting_db):
    receipt = bank._record_usage_receipt('svc', 1.25, task_id='task-1')
    assert bank._apply_usage_receipt(receipt) is True
    ledger = serve._spend_ledger_read()
    assert ledger['svc']['used'] == pytest.approx(1.25)
    assert receipt in ledger[bank._RECEIPTS_LEDGER_KEY]
    assert bank._apply_usage_receipt(receipt) is False
    assert serve._spend_ledger_read()['svc']['used'] == pytest.approx(1.25)


def test_receipt_idempotency_key_is_not_double_recorded(accounting_db):
    first = bank._record_usage_receipt('svc', 1.0, idempotency_key='call-1')
    second = bank._record_usage_receipt('svc', 1.0, idempotency_key='call-1')
    assert first == second
    with sqlite3.connect(accounting_db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM usage_receipts').fetchone()[0] == 1


def test_replay_applies_a_receipt_left_before_projection(accounting_db):
    receipt = bank._record_usage_receipt('svc', 0.75)
    assert bank._replay_usage_receipts() == 1
    assert serve._spend_ledger_read()['svc']['used'] == pytest.approx(0.75)
    assert bank._replay_usage_receipts() == 0
    assert receipt


def test_ledger_marker_prevents_double_apply_after_crash(accounting_db):
    receipt = bank._record_usage_receipt('svc', 2.0)
    assert bank._apply_usage_receipt(receipt) is True
    with sqlite3.connect(accounting_db) as conn:
        conn.execute('UPDATE usage_receipts SET applied = 0 WHERE id = ?', (receipt,))
    assert bank._replay_usage_receipts() == 0
    assert serve._spend_ledger_read()['svc']['used'] == pytest.approx(2.0)


def test_unpriced_usage_holds_the_task_until_resolved(accounting_db, monkeypatch):
    bank._record_unpriced_usage('svc', task_id='task-7')
    assert bank._task_unpriced_blocked('task-7') is True
    assert bank._task_unpriced_blocked('task-8') is False
    assert bank._task_unpriced_blocked('') is False
    monkeypatch.setenv('UNPRICED_USAGE_POLICY', 'allow')
    assert bank._task_unpriced_blocked('task-7') is False
    monkeypatch.setenv('UNPRICED_USAGE_POLICY', 'block')
    assert bank._resolve_unpriced_usage(task_id='task-7') == 1
    assert bank._task_unpriced_blocked('task-7') is False
    assert bank._resolve_unpriced_usage() == 0


def test_invalid_status_falls_back_to_reported(accounting_db):
    receipt = bank._record_usage_receipt('svc', 0.5, status='bogus')
    with sqlite3.connect(accounting_db) as conn:
        status = conn.execute('SELECT status FROM usage_receipts WHERE id = ?', (receipt,)).fetchone()[0]
    assert status == 'reported'


def test_policy_defaults_to_block_when_env_read_fails(accounting_db, monkeypatch):
    monkeypatch.delenv('UNPRICED_USAGE_POLICY', raising=False)
    monkeypatch.setattr(serve, '_load_env', lambda: (_ for _ in ()).throw(RuntimeError('x')))
    assert bank._unpriced_policy() == 'block'


def test_unpriced_check_fails_open_on_db_error(accounting_db, monkeypatch):
    monkeypatch.setenv('UNPRICED_USAGE_POLICY', 'block')
    monkeypatch.setattr(serve, '_db', lambda: (_ for _ in ()).throw(RuntimeError('x')))
    assert bank._task_unpriced_blocked('task-1') is False


def test_resolve_by_receipt_id(accounting_db):
    receipt = bank._record_unpriced_usage('svc', task_id='task-9')
    assert bank._resolve_unpriced_usage(receipt_id=receipt) == 1
    assert bank._task_unpriced_blocked('task-9') is False


def test_task_budget_exhausted_honors_unpriced_hold(accounting_db, monkeypatch):
    monkeypatch.setattr(serve, '_task_budget_spent', lambda task_id: (0.0, 0))
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: {'tasks': {}})
    bank._record_unpriced_usage('svc', task_id='task-3')
    assert serve._task_budget_exhausted('task-3') is True
    monkeypatch.setenv('UNPRICED_USAGE_POLICY', 'allow')
    assert serve._task_budget_exhausted('task-3') is False


def test_accrual_survives_receipt_and_projection_failures(accounting_db, monkeypatch):
    monkeypatch.setattr(bank, '_record_usage_receipt',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    bank._accrue_spend('svc', 0.25)
    assert serve._spend_ledger_read()['svc']['used'] == pytest.approx(0.25)

    monkeypatch.setattr(bank, '_apply_spend_buckets',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    bank._accrue_spend('svc', 0.5)  # swallowed


def test_replay_failure_does_not_break_ledger_read(accounting_db, monkeypatch):
    bank._RECEIPTS_REPLAYED['done'] = False
    monkeypatch.setattr(bank, '_replay_usage_receipts',
                        lambda: (_ for _ in ()).throw(RuntimeError('x')))
    assert serve._spend_ledger_read() == {}


def test_task_budget_unpriced_error_fails_open(accounting_db, monkeypatch):
    monkeypatch.setattr(bank, '_task_unpriced_blocked',
                        lambda task_id: (_ for _ in ()).throw(RuntimeError('x')))
    monkeypatch.setattr(serve, 'get_state_from_db',
                        lambda: {'tasks': {'task-1': {'budgetUsd': 1.0}}})
    monkeypatch.setattr(serve, '_task_budget_spent', lambda task_id: (0.0, 0))
    assert serve._task_budget_exhausted('task-1') is False
