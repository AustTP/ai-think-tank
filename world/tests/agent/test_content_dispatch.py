import types

import content


def test_observatory_dispatch(monkeypatch):
    calls = []
    for name in ('_run_distill_content', '_run_skill_review_content', '_run_research_content', '_run_spike_content', '_run_research_bare_content'):
        monkeypatch.setattr(content, name, lambda *a, n=name, **k: calls.append(n))
    for key in ('distill', 'skillReview', 'research', 'projectLabel', 'other'):
        content._run_observatory_content({}, 'a', {key: True})
    assert calls == ['_run_distill_content', '_run_skill_review_content', '_run_research_content', '_run_spike_content', '_run_research_bare_content']


def test_supervisor_content(monkeypatch):
    import sim
    stores = []
    monkeypatch.setattr(sim, '_store_content_result', lambda i, r: stores.append(r))
    monkeypatch.setattr(sim, '_supervisor_brief', lambda s, t: {'text': 'brief'})
    monkeypatch.setattr(sim, '_bump_content_heartbeat', lambda i: None)
    monkeypatch.setattr(content, '_agent_name', lambda s, a: 'Name')
    monkeypatch.setattr(content._serve, '_is_admin', lambda s, a: False)
    # no team
    content._run_supervisor_content({'teams': []}, 'a', {'id': 't'})
    assert stores[-1]['ok'] is False
    # team, no brief
    monkeypatch.setattr(sim, '_supervisor_brief', lambda s, t: None)
    content._run_supervisor_content({'teams': [{'id': 'x', 'directorId': 'a'}]}, 'a', {'id': 't'})
    assert stores[-1]['ok'] is False
    # team with brief
    monkeypatch.setattr(sim, '_supervisor_brief', lambda s, t: {'text': 'brief'})
    for name in ('_make_spike_sandbox_executor', '_make_supervisor_report_executor',
                 '_make_supervisor_grant_executor', '_make_supervisor_live_read_executor',
                 '_make_supervisor_delegate_executor', '_make_supervisor_schedules_executor',
                 '_make_supervisor_runbook_executor', '_make_supervisor_backlog_executor',
                 '_make_supervisor_db_executor', '_make_supervisor_state_executor',
                 '_make_generate_agent_avatar_executor'):
        monkeypatch.setattr(content, name, lambda *a, **k: (lambda n, args: f'{n} ok'))
    monkeypatch.setattr(content._serve, 'get_or_create_agent_key', lambda a: 'k')
    monkeypatch.setattr(content._serve, '_mid_tier_slug', lambda: 'm')
    monkeypatch.setattr(content._serve, '_call_agent_tool_loop', lambda *a, **k: ('done', []))
    content._run_supervisor_content({'teams': [{'id': 'x', 'directorId': 'a'}]}, 'a', {'id': 't'})
    assert stores[-1]['ok'] is True
    # admin branch
    monkeypatch.setattr(content._serve, '_is_admin', lambda s, a: True)
    content._run_supervisor_content({'teams': [{'id': 'x'}]}, 'a', {'id': 't'})
    assert stores[-1]['ok'] is True
    # call failure
    monkeypatch.setattr(content._serve, '_call_agent_tool_loop', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    content._run_supervisor_content({'teams': [{'id': 'x', 'directorId': 'a'}]}, 'a', {'id': 't'})
    assert stores[-1]['ok'] is False


def test_report_content(monkeypatch, tmp_path):
    import sim
    stores = []
    monkeypatch.setattr(sim, '_store_content_result', lambda i, r: stores.append(r))
    monkeypatch.setattr(content, '_run_spike_content', lambda *a: stores.append('spike'))
    content._run_report_content({}, 'a', {'id': 't'})
    assert stores[-1] == 'spike'
    monkeypatch.setattr(content, '_read_library_document', lambda p: '')
    content._run_report_content({}, 'a', {'id': 't', 'sourcePaths': ['x']})
    assert stores[-1]['ok'] is False
    monkeypatch.setattr(content, '_read_library_document', lambda p: 'text')
    monkeypatch.setattr(content, '_agent_name', lambda s, a: 'Name')
    monkeypatch.setattr(content._serve, 'get_or_create_agent_key', lambda a: 'k')
    monkeypatch.setattr(content._serve, '_resolve_model_tier', lambda *a, **k: 'm')
    monkeypatch.setattr(content.skill_prompts, 'load_skill_prompt', lambda *a, **k: 'guide')
    monkeypatch.setattr(content, '_plain_completion', lambda *a, **k: 'report')
    target = tmp_path / 'r.md'; target.write_text('x')
    monkeypatch.setattr(content._serve, '_http_json', lambda *a, **k: {})
    monkeypatch.setattr(content._serve, '_safe_library_path', lambda p: str(target))
    monkeypatch.setattr(content.os.path, 'isfile', lambda p: True)
    content._run_report_content({}, 'a', {'id': 't', 'sourcePaths': ['x'], 'title': 'T'})
    assert stores[-1]['ok'] is True
    # empty synthesis
    monkeypatch.setattr(content, '_plain_completion', lambda *a, **k: '')
    content._run_report_content({}, 'a', {'id': 't', 'sourcePaths': ['x']})
    assert stores[-1]['ok'] is False
    # save failure
    monkeypatch.setattr(content, '_plain_completion', lambda *a, **k: 'report')
    monkeypatch.setattr(content._serve, '_http_json', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    content._run_report_content({}, 'a', {'id': 't', 'sourcePaths': ['x']})
    assert stores[-1]['ok'] is False
    # missing target
    monkeypatch.setattr(content._serve, '_http_json', lambda *a, **k: {})
    monkeypatch.setattr(content._serve, '_safe_library_path', lambda p: '')
    content._run_report_content({}, 'a', {'id': 't', 'sourcePaths': ['x']})
    assert stores[-1]['ok'] is False
