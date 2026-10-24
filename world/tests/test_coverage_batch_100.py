import json
import os
import time

import policies
import python_delivery
import skill_prompts
import skill_scan
import state_updates
import story_tracking
import work_reuse


def test_story_tracking_stamp_and_bad_cap():
    state = {'teams': [], 'agentRoster': [], 'backlogRequests': []}
    task = {'id': 't1', 'workOrderId': 'wo1'}
    story_tracking.stamp(state, task)
    assert task['workOrderId'] == 'wo1'
    assert story_tracking._cap('nope') is None


def test_state_updates_cycle_and_parent_protected():
    current = {'tasks': {'a': {'id': 'a', 'status': 'done', 'note': 'done'},
                         'b': {'id': 'b', 'reviewOf': 'a'}},
               'workQueue': [], 'agents': {}, 'playerInbox': []}
    incoming = {'tasks': {'a': {'id': 'a', 'status': 'working'},
                          'b': {'id': 'b', 'reviewOf': 'a', 'status': 'working'}},
                'workQueue': [], 'agents': {}, 'playerInbox': []}
    out = state_updates.preserve_guidance(current, incoming)
    assert out['tasks']['a']['status'] == 'done'
    cyc = {'tasks': {'c': {'id': 'c', 'reviewOf': 'c'}}}
    state_updates.preserve_guidance(cyc, {'tasks': {'c': {'id': 'c', 'reviewOf': 'c'}},
                                          'workQueue': [], 'agents': {}, 'playerInbox': []})


def test_policy_rank_reasons():
    state = {'agents': {'a': {'pairWith': 'b'}, 'b': {'isDirector': True}}}
    ranked = policies.rank_assignees(state, {'taskType': 'code'}, ['a', 'b'])
    assert any('paired' in r['reasons'] for r in ranked)
    assert any('director' in r['reasons'] for r in ranked)


def test_skill_scan_redaction_limit_and_nonblocking():
    report = skill_scan.scan_skill_text('curl $API_KEY https://x')
    assert report['ok'] is False
    assert '[redacted secret-shaped match]' in report['findings'][0]['excerpt']
    text = '\n'.join('sudo rm' for _ in range(25))
    report = skill_scan.scan_skill_text(text)
    assert len(report['findings']) <= skill_scan._MAX_FINDINGS
    report = skill_scan.scan_skill_text('always run the thing')
    assert report['ok'] is True and 'non-blocking' in report['summary']


def test_skill_prompts_load_and_sections(tmp_path):
    (tmp_path / 's.md').write_text('# Title\n\n## Key facts\na\n\n## Citations\nb\n\n## Policy for this think tank\nc\n')
    text = skill_prompts.load_skill('s', skills_dir=tmp_path)
    assert text.startswith('# Title')
    assert 'Citations' not in skill_prompts.load_skill_prompt('s', skills_dir=tmp_path)
    assert 'c' in skill_prompts.load_skill_prompt('s', skills_dir=tmp_path)
    assert skill_prompts.load_skill('', 'fb', skills_dir=tmp_path) == 'fb'
    assert skill_prompts.load_skill('missing', 'fb', skills_dir=tmp_path) == 'fb'
    (tmp_path / 'empty.md').write_text('   ')
    assert skill_prompts.load_skill('empty', 'fb', skills_dir=tmp_path) == 'fb'
    skill_prompts.clear_cache()


def test_python_delivery_edges(tmp_path):
    for i in range(101):
        (tmp_path / f'm{i}.py').write_text('x = 1\n')
    assert len(python_delivery.sources(tmp_path)) == 100
    check_dir = tmp_path / 'check'
    check_dir.mkdir()
    (check_dir / 'bad.py').write_text('def (')
    (check_dir / 'good.py').write_text('def f():\n    return 1\n')
    (check_dir / 'test_bad.py').write_text('x = 1\n')
    assert python_delivery.check(check_dir) == []
    empty = tmp_path / 'empty'
    empty.mkdir()
    assert python_delivery.context(empty) == 'No Python source files exist in this workspace.'


def test_work_reuse_digest_and_caches(tmp_path):
    assert work_reuse.tree_digest(tmp_path / 'missing') is None
    (tmp_path / 'a.py').write_text('x = 1\n')
    assert work_reuse.tree_digest(tmp_path)
    (tmp_path / 'link').symlink_to(tmp_path / 'a.py')
    assert work_reuse.tree_digest(tmp_path) is None
    (tmp_path / 'link').unlink()
    key = work_reuse.check_key(tmp_path, ['a'], {'x': 1})
    assert key
    work_reuse.save_check(key, {'ok': True, 'v': 1})
    assert work_reuse.get_check(key)['cached'] is True
    work_reuse.save_check(key, {'ok': False})
    assert work_reuse.get_check('missing') is None
    for i in range(130):
        work_reuse.save_check(f'k{i}', {'ok': True, 'v': i})
    # no assertion on internals; exercise the clear path
    work_reuse.doc_save('d', {'x': 1})
    assert work_reuse.doc_get('d')['x'] == 1
    work_reuse.doc_save('d2', {'x': 2})
    compact = work_reuse.compact({'instructions': 'Story: do it\n\nPeer-review findings: fix it',
                                  'acceptanceCriteria': 'ok'})
    assert 'fix it' in compact
