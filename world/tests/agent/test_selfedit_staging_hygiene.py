"""Selfedit staging hygiene: stale q-*/pre-* quarantine copies are reaped;
candidates (*.cand) and anything else are never touched."""
import os
import time

import selfedit


def test_sweep_stale_quarantines(monkeypatch, tmp_path):
    staging = tmp_path / '.selfedit_staging'
    staging.mkdir()
    old = staging / 'pre-oldcopy'
    old.mkdir()
    (old / 'big.bin').write_text('x')
    young = staging / 'q-young'
    young.mkdir()
    cand = staging / 'ed-abc123.cand'
    cand.write_text('candidate bytes')
    unrelated = staging / 'unrelated-dir'
    unrelated.mkdir()
    past = time.time() - selfedit.STALE_QUARANTINE_MAX_AGE_S - 60
    os.utime(old, (past, past))
    monkeypatch.setattr(selfedit, '_world', lambda: str(tmp_path))
    freed = selfedit._sweep_stale_quarantines()
    assert freed == ['pre-oldcopy']
    assert not old.exists()
    assert young.exists(), 'a young quarantine may be a live run -- keep it'
    assert cand.exists(), 'armed edits still read candidates -- never touch'
    assert unrelated.exists()


def test_sweep_survives_missing_staging_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(selfedit, '_world', lambda: str(tmp_path))
    assert selfedit._sweep_stale_quarantines() == []
