"""Coverage-gap tests for the selfedit header helpers."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import selfedit  # noqa: E402
import serve  # noqa: E402

from _selfedit_support import fake_repo  # noqa: E402


def test_cov_physics_refusal_edges(tmp_path):
    path = str(tmp_path / 'sim.py')
    assert selfedit._physics_refusal('/tmp/other.py', 'abc', 'a') is None
    assert selfedit._physics_refusal(path, 'no markers', 'a') is not None
    guarded = (f'{selfedit.PHYSICS_BEGIN}\nPHYS = 1\n'
               f'{selfedit.PHYSICS_END}\n')
    assert selfedit._physics_refusal(path, guarded, 'PHYS = 1') is not None
    # find text outside the guarded span and absent find text both pass.
    assert selfedit._physics_refusal(path, guarded, 'NOT THERE') is None
    outside = (f'X = 1\n{selfedit.PHYSICS_BEGIN}\nY = 2\n'
               f'{selfedit.PHYSICS_END}\n')
    assert selfedit._physics_refusal(path, outside, 'X = 1') is None

    part = os.path.join(selfedit._world(), 'village', 'sim_parts',
                        '00_header.py')
    assert selfedit._physics_refusal(part, 'no markers', 'a') is not None
    assert selfedit._physics_refusal(part, guarded, 'PHYS = 1') is not None
    assert selfedit._physics_refusal(part, outside, 'X = 1') is None


def test_cov_facade_parts_and_source_stitching(fake_repo):
    _, world = fake_repo
    (world / 'pkg').mkdir()
    (world / 'pkg' / 'mod.py').write_text('X = 1\n', encoding='utf-8')
    stub = world / 'stub.py'
    stub.write_text('# facade: pkg.mod\n', encoding='utf-8')
    assert selfedit._facade_redirect(str(stub)) == str(world / 'pkg' / 'mod.py')
    bad = world / 'bad.py'
    bad.write_text('# facade: ../escape\n', encoding='utf-8')
    assert selfedit._facade_redirect(str(bad)) is None
    missing = world / 'missing.py'
    missing.write_text('# facade: pkg.none\n', encoding='utf-8')
    assert selfedit._facade_redirect(str(missing)) is None

    assert selfedit._parts_dir('notes.txt') is None
    parts = world / 'parts'
    parts.mkdir()
    (parts / '10_a.py').write_text('A = 1\n', encoding='utf-8')
    (parts / '20_b.py').write_text('B = 2\n', encoding='utf-8')
    split = world / 'split.py'
    split.write_text('# parts: parts\n', encoding='utf-8')
    assert selfedit._read_source(str(split)) == 'A = 1\nB = 2\n'
    bad_parts = world / 'bad_parts.py'
    bad_parts.write_text('# parts: ../escape\n', encoding='utf-8')
    assert selfedit._parts_dir(str(bad_parts)) is None


def test_cov_propose_governance_refusal(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'gear.py').write_text('SPEED = 2\n', encoding='utf-8')
    monkeypatch.setattr(serve, 'get_state_from_db',
                        lambda: {'agents': {'worker': {}}})
    monkeypatch.setattr(selfedit.governance, 'may_self_edit',
                        lambda state, agent: False)
    ok, payload = selfedit.propose('worker', 'gear.py', 'SPEED = 2',
                                   'SPEED = 3', 'why')
    assert ok is False
    assert 'may not edit' in payload['error']


def test_cov_read_module_query_window_and_miss(fake_repo):
    _, world = fake_repo
    text = '\n'.join(f'line{i}' for i in range(1, 101)) + '\n'
    (world / 'q.py').write_text(text, encoding='utf-8')
    ok, payload = selfedit.read_module('q.py', query='line50',
                                       context='not-a-number')
    assert ok is True, payload
    assert payload['matchLine'] == 50
    assert payload['startLine'] == 10
    assert payload['endLine'] == 90
    assert 'line50' in payload['text']
    ok, payload = selfedit.read_module('q.py', query='absent-anchor')
    assert ok is False
    assert 'query was not found' in payload['error']


def test_cov_validate_kernel_skips_flake_and_smoke(fake_repo, monkeypatch):
    _, world = fake_repo
    (world / 'serve.py').write_text('X = 1\n', encoding='utf-8')
    calls = []

    class Result:
        returncode = 0
        stdout = b''
        stderr = b''

    def fake_run(args, **kwargs):
        calls.append(args)
        return Result()

    monkeypatch.setattr(selfedit.subprocess, 'run', fake_run)
    ok, payload = selfedit._validate('ed-kernel', str(world / 'serve.py'),
                                     'X = 2\n')
    assert ok is True, payload
    assert 'flake8 skipped for the kernel' in payload['validator']
    assert 'import smoke' not in payload['validator']
    assert calls
