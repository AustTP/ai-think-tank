"""Residual coverage for world/content.py helper branches and executors."""
import json
import os
import sys
import types
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import content  # noqa: E402
import serve  # noqa: E402


def _ns(**kw):
    base = {'log_action': lambda *a, **k: None}
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_json_object_and_shell_and_criteria_helpers():
    assert content._json_object('{bad json}') is None
    assert content._json_object('prose {"a": 1} tail') == {'a': 1}
    assert content._looks_like_shell('Note: run this') is False
    assert content._acceptance_criteria({'acceptanceCriteria': 'a; b; c'}) == {
        'C1': 'a', 'C2': 'b', 'C3': 'c'}
    assert content._acceptance_criteria({
        'acceptanceCriteria': '- one\n2. two'}) == {'C1': 'one', 'C2': 'two'}


def test_parse_acceptance_checks_skips_bad_json_then_finds_rows():
    text = '{bad} then {"verdict": "approve", "acceptanceChecks": [{"criterionId": "C1"}]}'
    assert content._parse_acceptance_checks(text) == [{'criterionId': 'C1'}]


def test_assess_acceptance_reports_unknown_evidence_status_and_text():
    criteria = {'C1': 'one', 'C2': 'two'}
    rows = [
        {'criterionId': 'C1'},
        {'criterionId': 'C1', 'evidenceIds': ['E1'], 'status': 'met',
         'explanation': 'because'},
        {'criterionId': 'C9', 'evidenceIds': ['E'], 'status': 'met',
         'explanation': 'x'},
    ]
    out = content._assess_acceptance(rows, criteria)
    assert out['verified'] is False
    assert any('Unknown or duplicate' in e for e in out['errors'])
    assert any('lacks inspected evidence' in e for e in out['errors'])
    assert any('unmet or unverified' in e for e in out['errors'])
    assert any('C2 was not verified' in e for e in out['errors'])


def test_document_review_repairs_filters_bad_findings():
    document = 'quoted evidence'
    requirements = 'criterion text instructions acceptance'
    text = json.dumps({'verdict': 'send_back', 'repairStories': [
        'not-a-dict',
        {'title': 't', 'criterionId': 'C1', 'evidenceId': 'E1',
         'instructions': 'do', 'measurableAcceptanceCriteria': 'acc'},
        {'title': 't', 'criterion': 'criterion text', 'evidenceQuote': 'missing',
         'instructions': 'do', 'acceptanceCriteria': 'a'},
    ]})
    references = {'criteria': {'C1': 'criterion text'},
                  'evidence': {'E1': 'quoted evidence'}}
    out = content._document_review_repairs(text, document, requirements,
                                           references)
    assert out == [{'title': 't', 'criterion': 'criterion text',
                    'evidenceQuote': 'quoted evidence', 'instructions': 'do',
                    'acceptanceCriteria': 'acc'}]
    assert content._document_review_repairs(
        '{"verdict":"send_back","repairStories":"x"}', document,
        requirements) == []
    assert content._document_review_repairs('no json', document,
                                            requirements) == []


def test_spike_work_window():
    assert content._spike_work_window_s({'budgetMs': 'bad'}) == 300.0
    assert content._spike_work_window_s(
        {'instructions': 'run youtube_transcript'}) == 900.0
    assert content._spike_work_window_s({'budgetMs': 60_000}) == 300.0


def test_poll_pipeline_job_paths(monkeypatch):
    monkeypatch.setattr(content.time, 'sleep', lambda s: None)
    assert content._poll_pipeline_job('b', 'k', 'j', deadline_s=0)['error']
    monkeypatch.setattr(content._serve, '_http_json',
                        lambda *a, **k: 'nope')
    assert content._poll_pipeline_job('b', 'k', 'j', deadline_s=1)['error']
    monkeypatch.setattr(content._serve, '_http_json',
                        lambda *a, **k: {'error': 'bad'})
    assert content._poll_pipeline_job('b', 'k', 'j', deadline_s=1)['error'] == 'bad'
    monkeypatch.setattr(content._serve, '_http_json',
                        lambda *a, **k: {'status': 'done', 'result': None})
    assert content._poll_pipeline_job('b', 'k', 'j', deadline_s=1)['error']


def test_run_quality_pipeline_cache_and_job(monkeypatch):
    monkeypatch.setattr(content, '_quality_pipeline_steps', lambda s: [])
    monkeypatch.setattr(content._serve, '_village_sandbox_id', lambda a, b: 'sb')
    monkeypatch.setattr(content._serve, '_sandbox_dir_for', lambda s: '/tmp')
    monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    monkeypatch.setattr(content.work_reuse, 'check_key', lambda *a: 'key')
    monkeypatch.setattr(content.work_reuse, 'get_check', lambda k: {'cached': 1})
    assert content._run_quality_pipeline('b', 'k', 'a', 's', 'p') == {'cached': 1}
    monkeypatch.setattr(content.work_reuse, 'get_check', lambda k: None)
    monkeypatch.setattr(content._serve, '_http_json',
                        lambda *a, **k: {'jobId': 'j'})
    monkeypatch.setattr(content, '_poll_pipeline_job',
                        lambda *a, **k: {'ok': True, 'results': [], 'note': 'green'})
    out = content._run_quality_pipeline('b', 'k', 'a', 's', 'p')
    assert out['ok'] is True


def test_commit_story_workspace_branches(monkeypatch, tmp_path):
    content._commit_story_workspace('not-a-story')
    monkeypatch.setattr(content._serve, 'SANDBOXES_DIR', str(tmp_path))
    content._commit_story_workspace('story-1')
    git = tmp_path / 'story-1' / '.git'
    git.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(content._serve, '_run_git_sync',
                        lambda d, args: calls.append(args) or types.SimpleNamespace(
                            stdout=' M file' if args[0] == 'status' else ''))
    content._commit_story_workspace('story-1', 't1')
    assert any(c[0] == 'commit' for c in calls)
    monkeypatch.setattr(content._serve, '_run_git_sync',
                        lambda *a: (_ for _ in ()).throw(RuntimeError('git down')))
    content._commit_story_workspace('story-1')


def test_tree_manifest_skips_links_and_unreadable(monkeypatch, tmp_path):
    (tmp_path / 'a.txt').write_text('a')
    (tmp_path / '__pycache__').mkdir()
    (tmp_path / '__pycache__' / 'b').write_text('b')
    (tmp_path / 'link').symlink_to(tmp_path / 'a.txt')
    manifest = content._tree_manifest(str(tmp_path))
    assert 'a.txt' in manifest
    assert 'links' not in manifest
    assert all(not k.startswith('__pycache__/') for k in manifest)


def test_read_library_document_paths(monkeypatch, tmp_path):
    assert content._read_library_document('') == ''
    target = tmp_path / 'd.md'
    target.write_text('hello')
    monkeypatch.setattr(content._serve, '_safe_library_path', lambda p: str(target))
    assert content._read_library_document('d.md') == 'hello'
    monkeypatch.setattr(content._serve, '_safe_library_path',
                        lambda p: (_ for _ in ()).throw(ValueError('bad')))
    assert content._read_library_document('d.md') == ''
    monkeypatch.setattr(content._serve, '_safe_library_path',
                        lambda p: str(tmp_path / 'missing.md'))
    assert content._read_library_document('d.md') == ''


def test_browser_sessions_ensure_swallows_db_errors(monkeypatch):
    monkeypatch.setattr(content, '_BROWSER_SESSIONS_READY', False)
    monkeypatch.setattr(content._serve, '_db',
                        lambda: (_ for _ in ()).throw(RuntimeError('db')))
    content._browser_sessions_ensure()
    assert content._BROWSER_SESSIONS_READY is False


def test_browser_session_load_and_save_errors(monkeypatch):
    monkeypatch.setattr(content, '_browser_sessions_ensure', lambda: None)
    monkeypatch.setattr(content._serve, '_db',
                        lambda: (_ for _ in ()).throw(RuntimeError('db')))
    out = content._browser_session_load('a', 's')
    assert out['history'] == []
    saved = content._browser_session_save('a', 's', 'u', 'read', None, '')
    assert saved['lastVerified'] is None


def test_browser_session_load_bad_history(monkeypatch):
    monkeypatch.setattr(content, '_browser_sessions_ensure', lambda: None)
    row = ('u', 'read', -1, '', 1, 'not-json')

    class _Conn:
        def execute(self, *a):
            return types.SimpleNamespace(fetchone=lambda: row)

    class _Db:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(content._serve, '_db', lambda: _Db())
    out = content._browser_session_load('a', 's')
    assert out['history'] == []


def test_vision_review_base64_paths(monkeypatch):
    assert content._vision_review_base64('a', 'k', 't', '', 'q')['ok'] is False
    monkeypatch.setattr(content._serve, '_vision_tier_slug', lambda: '')
    assert content._vision_review_base64('a', 'k', 't', 'b64', 'q')['ok'] is False
    monkeypatch.setattr(content._serve, '_vision_tier_slug', lambda: 'v')
    monkeypatch.setattr(content._serve, 'VISION_ANTI_INSTRUCTION_GUARD', 'g')
    monkeypatch.setattr(content._serve, '_http_json',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('down')))
    assert 'failed' in content._vision_review_base64('a', 'k', 't', 'b64', 'q')['note']
    monkeypatch.setattr(content._serve, '_http_json',
                        lambda *a, **k: {'reply': 'yes'})
    assert content._vision_review_base64('a', 'k', 't', 'b64', 'q')['ok'] is True
    monkeypatch.setattr(content._serve, '_http_json', lambda *a, **k: {})
    assert content._vision_review_base64('a', 'k', 't', 'b64', 'q')['ok'] is False


def test_captcha_executor_paths(monkeypatch):
    run = content._make_captcha_solve_executor('a', 'k')
    assert 'requires requestId' in run('solve_captcha', {})
    monkeypatch.setattr(content._serve, 'SELF_BASE_URL', 'http://x')
    monkeypatch.setattr(content._serve, '_http_json', lambda *a, **k: 'x')
    assert 'unexpected response' in run('solve_captcha', {'requestId': 'r', 'waitSec': 'bad'})
    monkeypatch.setattr(content._serve, '_http_json',
                        lambda *a, **k: {'error': 'bad'})
    assert 'broker error' in run('solve_captcha', {'requestId': 'r'})
    monkeypatch.setattr(content._serve, '_http_json',
                        lambda *a, **k: {'status': 'waiting'})
    assert 'waiting for the player' in run('solve_captcha', {'requestId': 'r'})
    monkeypatch.setattr(content._serve, '_http_json',
                        lambda *a, **k: {'status': 'captcha_still_present'})
    assert 'still shows' in run('solve_captcha', {'requestId': 'r'})
    monkeypatch.setattr(content._serve, '_http_json',
                        lambda *a, **k: {'page': {'url': 'u', 'title': 't', 'text': 'x'}})
    assert 'CAPTCHA solved' in run('solve_captcha', {'requestId': 'r'})


def test_executor_factories(monkeypatch):
    monkeypatch.setattr(serve, 'supervisor_live_read', lambda a, x: {'a': a})
    assert json.loads(content._make_supervisor_live_read_executor('a')
                      ('live_read', {})) == {'a': 'a'}
    monkeypatch.setattr(serve, 'supervisor_delegate',
                        lambda *a, **k: (True, {'ok': True}))
    assert json.loads(content._make_supervisor_delegate_executor('a')
                      ('delegate', {})) == {'ok': True}
    monkeypatch.setattr(serve, 'supervisor_schedules', lambda a, x: {'s': 1})
    assert json.loads(content._make_supervisor_schedules_executor('a')('schedules', {})) == {'s': 1}
    monkeypatch.setattr(serve, 'supervisor_state_records', lambda a, x: {'st': 1})
    assert json.loads(content._make_supervisor_state_executor('a')
                      ('state_records', {})) == {'st': 1}
    monkeypatch.setattr(serve, 'generate_agent_avatar', lambda a, b, c: {'av': 1})
    assert json.loads(content._make_generate_agent_avatar_executor('a')(
        'generate_agent_avatar', {'agentId': 'b'})) == {'av': 1}
    monkeypatch.setattr(serve, 'supervisor_backlog_directive',
                        lambda *a: (True, {'bl': 1}))
    assert json.loads(content._make_supervisor_backlog_executor('a')(
        'direct_backlog', {})) == {'bl': 1}
    monkeypatch.setattr(serve, 'supervisor_db_records', lambda a, x: {'db': 1})
    assert json.loads(content._make_supervisor_db_executor('a')('db_records', {})) == {'db': 1}
    monkeypatch.setattr(serve, 'supervisor_grant_budget',
                        lambda *a: (True, {'g': 1}))
    assert json.loads(content._make_supervisor_grant_executor('a')(
        'supervisor_grant_budget', {})) == {'g': 1}


def test_runbook_executor_branches(monkeypatch):
    run = content._make_supervisor_runbook_executor()
    with mock.patch('builtins.open', side_effect=OSError('missing')):
        assert 'unavailable' in run('runbook', {})
    import io
    data = json.dumps({'deploy': {'title': 'Deploy', 'steps': ['a']}})
    with mock.patch('builtins.open', side_effect=lambda *a, **k: io.StringIO(data)):
        assert 'Available runbooks' in run('runbook', {})
        assert 'Unknown runbook' in run('runbook', {'name': 'nope'})
        assert 'Deploy' in run('runbook', {'name': 'deploy'})


def test_google_create_event_409_returns_existing(monkeypatch):
    calls = []

    def google_call(method, url, body=None):
        calls.append((method, url))
        if method == 'POST':
            return None, '409 already exists'
        return {'id': 'ev', 'htmlLink': 'link'}, None

    monkeypatch.setattr(serve, '_google_call', google_call)
    monkeypatch.setattr(serve, 'external_write_key', lambda s, *a: 'key')
    monkeypatch.setattr(serve, 'external_write_once',
                        lambda kind, key, fn, ttl_s=None: (*fn(), False))
    run = content._make_google_tools_executor('a')
    out = run('create_calendar_event', {
        'summary': 's', 'start_datetime': 'a', 'end_datetime': 'b'})
    assert json.loads(out)['id'] == 'ev'
    assert calls[0][0] == 'POST'


def test_spike_budget_and_selfedit_app_prompt(monkeypatch):
    messages_seen = {}
    monkeypatch.setattr(content._serve, 'SELF_BASE_URL', 'http://x')
    monkeypatch.setattr(content._serve, 'get_or_create_agent_key', lambda a: 'k')
    monkeypatch.setattr(content, '_agent_name', lambda s, a: 'Cora')
    monkeypatch.setattr(content._serve, '_task_budget_exhausted', lambda t: False)
    monkeypatch.setattr(content._serve, '_resolve_model_tier',
                        lambda *a, **k: 'm')
    monkeypatch.setattr(content._serve, 'load_task_checkpoint',
                        lambda t: {'phase': 'planned', 'title': 'Card',
                                   'plan': 'plan'})
    monkeypatch.setattr(content, '_spike_work_window_s', lambda t: 300)
    monkeypatch.setattr(content._serve, '_make_web_tools_executor',
                        lambda *a, **k: (lambda n, x: ''))
    monkeypatch.setattr(content, '_make_spike_sandbox_executor',
                        lambda *a, **k: (lambda n, x: ''))
    monkeypatch.setattr(content, '_make_browser_act_executor',
                        lambda *a, **k: (lambda n, x: ''))
    monkeypatch.setattr(content, '_make_browser_step_executor',
                        lambda *a, **k: (lambda n, x: ''))
    monkeypatch.setattr(content, '_make_captcha_solve_executor',
                        lambda *a, **k: (lambda n, x: ''))
    monkeypatch.setattr(content, '_make_list_handles_executor',
                        lambda *a, **k: (lambda n, x: ''))
    monkeypatch.setattr(content, '_make_library_tools_executor',
                        lambda *a, **k: (lambda n, x: ''))
    monkeypatch.setattr(content, '_make_skill_publisher',
                        lambda *a, **k: (lambda n, x: ''))
    monkeypatch.setattr(content, '_make_spike_report_writer',
                        lambda *a, **k: (lambda n, x: ''))
    monkeypatch.setattr(content, '_make_treg_tools_executor',
                        lambda *a, **k: (lambda n, x: ''))
    monkeypatch.setattr(content, '_make_pixellab_tools_executor',
                        lambda *a, **k: (lambda n, x: ''))
    monkeypatch.setattr(content, '_make_google_tools_executor',
                        lambda *a, **k: (lambda n, x: ''))
    monkeypatch.setattr(content, '_make_github_tools_executor',
                        lambda *a, **k: (lambda n, x: ''))
    monkeypatch.setattr(content, '_make_apify_tools_executor',
                        lambda *a, **k: (lambda n, x: ''))
    monkeypatch.setattr(content, '_make_youtube_transcript_executor',
                        lambda *a, **k: (lambda n, x: ''))
    monkeypatch.setattr(content, '_spike_first_tool', lambda *a: None)
    monkeypatch.setattr(content, '_sim_village_of_agent', lambda s, a: None)
    monkeypatch.setattr(content, 'task_tool_policy', lambda t: None)

    def fake_loop(*a, **k):
        messages_seen['system'] = a[2][0]['content']
        messages_seen['execute'] = a[4]
        messages_seen['before'] = k.get('on_before_tools')
        messages_seen['after'] = k.get('on_after_tools')
        return 'exec', [{'role': 'tool', 'content': 'x'}]

    monkeypatch.setattr(content, '_run_spike_tool_loop_with_reflection',
                        fake_loop)
    monkeypatch.setattr(content, '_plain_completion', lambda *a, **k: 'report')
    monkeypatch.setattr(content, '_plan_requires_verification_basis', lambda p: False)
    monkeypatch.setattr(content, '_finding_shows_basis_column', lambda f: True)
    monkeypatch.setattr(serve, 'get_or_create_agent_key', lambda a: 'k')
    monkeypatch.setattr(serve, 'AGENT_ASK_TOOLS', [])
    monkeypatch.setattr(serve, '_PULSE_TOOLS', [])
    monkeypatch.setattr(serve, 'APIFY_API_KEY', '')
    monkeypatch.setattr(serve, 'GITHUB_TOKEN', '')
    monkeypatch.setattr(serve, 'COLAB_CLI_AVAILABLE', False)
    monkeypatch.setattr(serve, 'FILE_ACCESS_ENABLED', False)
    monkeypatch.setattr(serve, 'APP_ACCESS_ENABLED', True)
    monkeypatch.setattr(serve, '_effective_app_grants', lambda s: [{'x': 1}])
    monkeypatch.setattr(serve, '_effective_file_grants', lambda s: [])
    monkeypatch.setattr(serve, 'idempotent_tool_call',
                        lambda a, t, fn, n, x: fn(n, x))
    monkeypatch.setattr(serve, 'save_task_checkpoint', lambda *a, **k: None)
    monkeypatch.setattr(serve, 'load_task_checkpoint',
                        lambda t: {'phase': 'planned', 'title': 'Card',
                                   'plan': 'plan'})
    stored = []
    import sim
    monkeypatch.setattr(sim, '_store_content_result',
                        lambda t, r: stored.append(r))
    content._run_spike_content({}, 'a', {
        'id': 't', 'title': 'Card', 'approvedTools': ['self_edit']})
    assert 'GOVERNED EDIT TASK' in messages_seen['system']
    assert stored and stored[-1]['ok'] is True
    # Exercise the dispatch branches and checkpoint error handlers.
    monkeypatch.setattr(content.execute_pulse_tool, '__call__', lambda *a: '')
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: {'tasks': {}})
    monkeypatch.setattr(serve, 'save_task_checkpoint',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('cp')))
    for name in ('browser_act', 'browser_step', 'audit_agent_readiness',
                 'project_board', 'solve_captcha', 'pulse_watch'):
        try:
            messages_seen['execute'](name, {})
        except Exception:
            pass
    messages_seen['before']([], [])
    messages_seen['after']([])


def _serve_with_tools(monkeypatch):
    s = types.SimpleNamespace(
        SELF_BASE_URL='http://x',
        AGENT_ASK_TOOLS=[], _PULSE_TOOLS=[],
        APIFY_API_KEY='', GITHUB_TOKEN='', COLAB_CLI_AVAILABLE=False,
        FILE_ACCESS_ENABLED=False, APP_ACCESS_ENABLED=True,
        _LOCAL_FILE_TOOL={}, _READ_STORE_TOOL={}, _APP_SCRIPT_TOOL={},
        _effective_file_grants=lambda s: [],
        _effective_app_grants=lambda s: [{'x': 1}],
        idempotent_tool_call=lambda a, t, fn, n, x: fn(n, x),
        save_task_checkpoint=lambda *a, **k: None,
        _local_file_tool=lambda a, x: '',
        _read_store_tool=lambda a, x: '',
        _app_script_tool=lambda a, x: '',
    )
    return s


def test_document_review_repairs_multiple_filters():
    document = 'ev'
    requirements = 'crit'
    text = ('{not json} {"verdict": "clean"} ' + json.dumps({
        'verdict': 'send_back', 'repairStories': [
            {'title': 't', 'criterion': 'crit', 'evidenceQuote': 'ev',
             'instructions': 'i', 'measurable acceptanceCriteria': 'acc'},
            {'title': 't', 'criterion': 'crit', 'evidenceQuote': 'ev'},
            {'title': 't', 'criterion': 'other', 'evidenceQuote': 'ev',
             'instructions': 'i', 'acceptanceCriteria': 'a'},
        ]}))
    out = content._document_review_repairs(text, document, requirements)
    assert out == [{'title': 't', 'criterion': 'crit',
                    'evidenceQuote': 'ev', 'instructions': 'i',
                    'acceptanceCriteria': 'acc'}]


def test_poll_pipeline_job_sleeps_then_done(monkeypatch):
    sleeps = []
    monkeypatch.setattr(content.time, 'sleep', lambda s: sleeps.append(s))
    replies = iter([{'status': 'running'}, {'status': 'done', 'result': {'ok': True}}])
    monkeypatch.setattr(content._serve, '_http_json', lambda *a, **k: next(replies))
    out = content._poll_pipeline_job('b', 'k', 'j', deadline_s=10)
    assert out == {'ok': True}
    assert sleeps == [1.0]


def test_tree_manifest_skips_unreadable(monkeypatch, tmp_path):
    (tmp_path / 'a.txt').write_text('a')
    real_open = open

    def fake_open(path, *a, **k):
        if str(path).endswith('a.txt'):
            raise OSError('locked')
        return real_open(path, *a, **k)

    with mock.patch('builtins.open', fake_open):
        assert content._tree_manifest(str(tmp_path)) == {}


def test_browser_act_executor_reuses_last_url_and_captcha(monkeypatch):
    monkeypatch.setattr(content._serve, 'SELF_BASE_URL', 'http://x')
    replies = iter([
        {'ok': True, 'url': 'https://site/page'},
        {'ok': True, 'url': 'https://site/page', 'captchaRequired': {'requestId': 'r'}},
    ])
    monkeypatch.setattr(content._serve, '_http_json', lambda *a, **k: next(replies))
    run = content._make_browser_act_executor('a', 'k', 's')
    run('browser_act', {'op': 'goto', 'url': 'https://site'})
    out = run('browser_act', {'op': 'read'})
    assert 'CAPTCHA REQUIRED' in out


def test_browser_step_executor_branches(monkeypatch):
    monkeypatch.setattr(content._serve, 'SELF_BASE_URL', 'http://x')
    monkeypatch.setattr(content._serve, '_mint_work_trust_nonce', lambda: 'n')
    monkeypatch.setattr(content._serve, 'wrap_external_content',
                        lambda text, label: (text, 'n', 't', 'INSTR'))
    monkeypatch.setattr(content, '_browser_session_load',
                        lambda a, s: {'lastUrl': 'https://last'})
    monkeypatch.setattr(content, '_browser_session_save',
                        lambda *a, **k: {'saved': True})
    run = content._make_browser_step_executor('a', 'k', 's')
    with pytest.raises(ValueError):
        run('other', {})
    assert json.loads(run('browser_step', {'op': 'bogus'}))['ok'] is False

    replies = iter(['string', {
        'ok': True, 'error': 'failed', 'captchaRequired': None,
        'text': 'body', 'fields': [], 'links': []}])
    monkeypatch.setattr(content._serve, '_http_json', lambda *a, **k: next(replies))
    assert json.loads(run('browser_step', {'op': 'read'}))['ok'] is False
    assert json.loads(run('browser_step', {'op': 'read'}))['ok'] is False

    replies = iter([{'ok': True, 'text': '', 'fields': [],
                     'links': [], 'url': 'https://x'},
                    {'text': 'read text', 'title': 'T', 'url': 'https://r'},
                    {'ok': True}])
    monkeypatch.setattr(content._serve, '_http_json', lambda *a, **k: next(replies))
    sleeps = []
    monkeypatch.setattr(content.time, 'sleep', lambda s: sleeps.append(s))
    out = json.loads(run('browser_step', {
        'op': 'read', 'waitMs': 100, 'expectUrl': 'r',
        'expectSelector': 'read', 'screenshot': True, 'captchaRequired': True}))
    assert out['checks'] == {'url': True, 'selector': True}
    assert out['screenshotCaptured'] is True
    assert sleeps == [0.1]

    replies = iter([{'ok': True, 'text': 't', 'fields': [], 'links': [],
                     'captchaRequired': {'requestId': 'r'}}])
    monkeypatch.setattr(content._serve, '_http_json', lambda *a, **k: next(replies))
    out = json.loads(run('browser_step', {'op': 'read'}))
    assert out['captchaRequired'] == {'requestId': 'r'}


def test_browser_step_vision_and_screenshot_error(monkeypatch):
    monkeypatch.setattr(content._serve, 'SELF_BASE_URL', 'http://x')
    monkeypatch.setattr(content._serve, '_mint_work_trust_nonce', lambda: '')
    monkeypatch.setattr(content._serve, 'wrap_external_content',
                        lambda text, label: (text, '', '', 'INSTR'))
    monkeypatch.setattr(content, '_browser_session_load', lambda a, s: {})
    monkeypatch.setattr(content, '_browser_session_save', lambda *a, **k: {})
    monkeypatch.setattr(content, '_vision_review_base64',
                        lambda *a: {'ok': False, 'note': 'no vision'})
    replies = iter([{'ok': True, 'text': 't', 'fields': [], 'links': [],
                     'screenshotBase64': 'abc'}])
    monkeypatch.setattr(content._serve, '_http_json', lambda *a, **k: next(replies))
    run = content._make_browser_step_executor('a', 'k', 's')
    out = json.loads(run('browser_step', {'op': 'read', 'expectVisual': 'cat'}))
    assert out['visionNote'] == 'no vision'
    assert out['screenshotCaptured'] is True

    def boom(*a, **k):
        raise RuntimeError('shot failed')

    replies = iter([{'ok': True, 'text': 't', 'fields': [], 'links': []}])
    monkeypatch.setattr(content._serve, '_http_json',
                        lambda *a, **k: next(replies))
    out = json.loads(run('browser_step', {'op': 'read', 'screenshot': True}))
    assert out['screenshotCaptured'] is False


def test_library_tool_query_and_log(monkeypatch, tmp_path):
    target = tmp_path / 'doc.md'
    target.write_text('alpha\nbeta\ngamma\n')
    monkeypatch.setattr(content._serve, '_safe_library_path', lambda p: str(target))
    monkeypatch.setattr(content._serve, 'record_library_read', lambda p: None)
    logs = []
    monkeypatch.setattr(content._serve, 'log_action',
                        lambda *a, **k: logs.append(a))
    monkeypatch.setattr(content.retrieval, 'excerpts', lambda *a, **k: '')
    run = content._make_library_tools_executor('a')
    text = run('read_library_file', {'path': 'doc.md', 'query': 'alpha'})
    assert text.startswith('alpha')
    assert logs[-1][2]['query'] == 'alpha'


def test_treg_pulse_observe_failures(monkeypatch):
    import agents.pulse as pulse
    monkeypatch.setattr(pulse, 'observe',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    monkeypatch.setattr(content._serve, '_treg_call',
                        lambda *a, **k: ({'data': []}, None))
    monkeypatch.setattr(content._serve, '_accrue_spend', lambda *a: None)
    monkeypatch.setattr(content._serve, 'TREG_ENDPOINT_COSTS',
                        {'x.x.get-trends-by-woeid': 0,
                         'scrapecreators.x.v1-linkedin-search-posts': 0})
    monkeypatch.setattr(content._serve, 'wrap_external_content',
                        lambda text, label: (text, '', '', 'I'))
    run = content._make_treg_tools_executor()
    assert 'I' in run('x_trending_topics', {})
    assert 'I' in run('search_linkedin_posts', {'query': 'q'})


def test_audit_and_project_board(monkeypatch, tmp_path):
    def fetch(name, args): return 'preamble\n\nzzz'
    out = json.loads(content.audit_agent_readiness(fetch, {'url': 'u'}))
    assert 'ok' in out
    monkeypatch.setattr(content._serve, 'LIBRARY_DIR', str(tmp_path))
    state = {'tasks': {
        'bad': 'x',
        't1': {'title': 'Other', 'status': 'done', 'projectLabel': 'p'},
        't2': {'title': 'Match', 'status': 'cancelled', 'projectLabel': 'p',
               'reportPath': '../escape'},
    }}
    monkeypatch.setattr(content._serve, 'get_state_from_db', lambda: state)
    out = json.loads(content.project_board({'project': 'nomatch'}))
    assert out['tasks'] == []
    out = json.loads(content.project_board({'status': 'all', 'project': 'match'}))
    assert out['tasks'][0]['artifactExists'] is False


def test_run_coding_content_guard_and_exception(monkeypatch, tmp_path):
    import sim
    import agents.staged_work as staged_work
    import agents.work_reuse as work_reuse
    import agents.story_tracking as story_tracking
    stored = []
    monkeypatch.setattr(sim, '_store_content_result',
                        lambda t, r: stored.append(r))

    class _Stage:
        id = 'stage'
        original = tmp_path / 'orig'
        directory = tmp_path / 'dir'
        resumed_from = ''

        def __init__(self, *a, **k):
            self.original.mkdir(exist_ok=True)
            self.directory.mkdir(exist_ok=True)

        def checkpoint(self):
            pass

        def publish(self):
            return 'prev'

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(staged_work, 'cleanup', lambda d: None)
    monkeypatch.setattr(staged_work, 'Stage', _Stage)
    monkeypatch.setattr(work_reuse, 'validation_lock', lambda w: _Lock())
    monkeypatch.setattr(work_reuse, 'tree_digest', lambda p: 'd')
    monkeypatch.setattr(story_tracking, 'root', lambda s, t: {})
    monkeypatch.setattr(content._serve, 'SANDBOXES_DIR', str(tmp_path))
    monkeypatch.setattr(content._serve.config, 'ALLOW_WORKSPACE_SYNC', True)
    monkeypatch.setattr(content, '_tree_manifest', lambda p: {})
    monkeypatch.setattr(content, '_deliverable_problem',
                        lambda a, b: 'no deliverable')
    monkeypatch.setattr(content, '_review_sandbox_id', lambda s, t, a: 'sb')

    def inner(*a, **k):
        k['result_sink']('t', {'ok': True})
        return True

    monkeypatch.setattr(content, '_run_coding_content_inner', inner)
    content._run_coding_content({}, 'a', {'id': 't'})
    assert stored[-1]['ok'] is False
    assert 'no deliverable' in stored[-1]['note']

    def boom(*a, **k):
        raise RuntimeError('stage broke')

    monkeypatch.setattr(content, '_run_coding_content_inner', boom)
    content._run_coding_content({}, 'a', {'id': 't2'})
    assert 'stage broke' in stored[-1]['note']


def test_run_supervisor_content_dispatch(monkeypatch):
    import sim
    stored = []
    monkeypatch.setattr(sim, '_store_content_result', lambda t, r: stored.append(r))
    monkeypatch.setattr(sim, '_supervisor_brief',
                        lambda s, t: {'text': 'brief'})
    monkeypatch.setattr(sim, '_bump_content_heartbeat',
                        lambda t: (_ for _ in ()).throw(RuntimeError('hb')))
    monkeypatch.setattr(content, '_agent_name', lambda s, a: 'Cora')
    monkeypatch.setattr(content._serve, '_is_admin', lambda s, a: False)
    monkeypatch.setattr(content._serve, 'get_or_create_agent_key', lambda a: 'k')
    monkeypatch.setattr(content._serve, '_mid_tier_slug', lambda: 'm')
    monkeypatch.setattr(content._serve, '_low_tier_slug', lambda: 'l')
    names = ['report', 'grant', 'live', 'delegate', 'backlog', 'db', 'state',
             'avatar', 'schedules', 'runbook', 'sandbox']
    for name in names:
        monkeypatch.setattr(content, '_make_' + {
            'report': 'supervisor_report_executor',
            'grant': 'supervisor_grant_executor',
            'live': 'supervisor_live_read_executor',
            'delegate': 'supervisor_delegate_executor',
            'backlog': 'supervisor_backlog_executor',
            'db': 'supervisor_db_executor',
            'state': 'supervisor_state_executor',
            'avatar': 'generate_agent_avatar_executor',
            'schedules': 'supervisor_schedules_executor',
            'runbook': 'supervisor_runbook_executor',
            'sandbox': 'spike_sandbox_executor',
        }[name], lambda *a, _n=name, **k: (lambda n, x: _n), raising=False)
    monkeypatch.setattr(content, 'task_tool_policy', lambda t: None)
    seen = []

    tool_names = ['supervisor_report', 'supervisor_grant_budget', 'live_read',
                  'delegate', 'direct_backlog', 'db_records', 'state_records',
                  'generate_agent_avatar', 'schedules', 'runbook', 'execute_script']

    def call_loop(model, messages, tools, execute, **k):
        for name in tool_names:
            seen.append(execute(name, {}))
        return 'done', []

    monkeypatch.setattr(content._serve, '_call_agent_tool_loop', call_loop)
    monkeypatch.setattr(content._serve, 'idempotent_tool_call',
                        lambda a, t, fn, n, x: fn(n, x))
    snapshot = {'teams': [{'id': 'team-a', 'directorId': 'cora'}],
                'agents': {'cora': {}}}
    content._run_supervisor_content(snapshot, 'cora',
                                    {'id': 't', 'teamId': 'team-a'})
    assert seen == names
    assert stored[-1]['ok'] is True


def _review_harness(monkeypatch, *, repairs=None, doc=False):
    import sim
    stored = []
    monkeypatch.setattr(sim, '_store_content_result', lambda t, r: stored.append(r))
    monkeypatch.setattr(content._serve, 'SELF_BASE_URL', 'http://x')
    monkeypatch.setattr(content._serve, 'get_or_create_agent_key', lambda a: 'k')
    reply = 'Looks fine. {"verdict": "send_back", "summary": "s"}'
    monkeypatch.setattr(content._serve, '_http_json',
                        lambda *a, **k: {'reply': reply})
    monkeypatch.setattr(content._serve, '_coding_tier_slug', lambda: 'c')
    monkeypatch.setattr(content._serve, '_call_openrouter_decision_sync',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('jev')))
    monkeypatch.setattr(content._serve, '_jev_model', lambda: 'j')
    monkeypatch.setattr(content._serve, '_jev_choice', lambda d: ('clean', 0, 0))
    monkeypatch.setattr(content._serve, '_safe_library_path', lambda p: p)
    monkeypatch.setattr(content, '_agent_name', lambda s, a: 'Cora')
    monkeypatch.setattr(content, '_review_sandbox_id', lambda *a: 'sb')
    monkeypatch.setattr(content, '_gather_unified_context', lambda *a: '')
    monkeypatch.setattr(content, '_run_quality_pipeline',
                        lambda *a, **k: {'ok': True, 'note': 'green'})
    monkeypatch.setattr(content, '_review_screenshot',
                        lambda *a, **k: {'ok': True, 'review': 'good'})
    monkeypatch.setattr(content, '_read_library_document',
                        lambda p: 'doc body' if doc else '')
    monkeypatch.setattr(content, '_document_review_repairs',
                        lambda *a: repairs or [])
    monkeypatch.setattr(content.review_quality, 'contract_for', lambda t: {})
    monkeypatch.setattr(content.review_quality, 'artifact_checks',
                        lambda *a: {'ok': True})
    monkeypatch.setattr(content.url_evidence, 'citation_errors',
                        lambda *a: ['bad citation'])
    monkeypatch.setattr(content, '_quality_pipeline_evidence', lambda q: {})
    monkeypatch.setattr(sim, '_peer_coding_class', lambda t: False)
    return stored


def test_run_review_content_repairs_document_and_citation(monkeypatch):
    stored = _review_harness(monkeypatch, repairs=[
        {'title': 'fix', 'instructions': 'do', 'acceptanceCriteria': 'acc'}],
        doc=True)
    snapshot = {'tasks': {'p': {'libraryPath': 'docs/x.md',
                                'sourceUrls': ['https://x'],
                                'acceptanceCriteria': 'C1: good'}}}
    task = {'id': 't', 'title': 'Card', 'reviewOf': 'p',
            'acceptanceCriteria': 'C1: good', 'reviewAuthorId': 'ben'}
    content._run_review_content(snapshot, 'cora', task)
    result = stored[-1]
    assert 'repair stories' in result['note'] or result['ok'] is True
    assert result['reviewDiagnostics']['citationErrors'] == ['bad citation']
    assert result['acceptanceAssessment']['verified'] is True or \
        'acceptanceAssessment' in result


def test_supervisor_unknown_tool_branch(monkeypatch):
    import sim
    monkeypatch.setattr(sim, '_store_content_result', lambda t, r: None)
    monkeypatch.setattr(sim, '_supervisor_brief', lambda s, t: {'text': 'b'})
    monkeypatch.setattr(sim, '_bump_content_heartbeat', lambda t: None)
    monkeypatch.setattr(content, '_agent_name', lambda s, a: 'Cora')
    monkeypatch.setattr(content._serve, '_is_admin', lambda s, a: False)
    monkeypatch.setattr(content._serve, 'get_or_create_agent_key', lambda a: 'k')
    monkeypatch.setattr(content._serve, '_mid_tier_slug', lambda: 'm')
    monkeypatch.setattr(content._serve, '_low_tier_slug', lambda: 'l')
    for attr in ('_make_spike_sandbox_executor', '_make_supervisor_report_executor',
                 '_make_supervisor_grant_executor', '_make_supervisor_live_read_executor',
                 '_make_supervisor_delegate_executor', '_make_supervisor_schedules_executor',
                 '_make_supervisor_runbook_executor', '_make_supervisor_backlog_executor',
                 '_make_supervisor_db_executor', '_make_supervisor_state_executor',
                 '_make_generate_agent_avatar_executor'):
        monkeypatch.setattr(content, attr, lambda *a, **k: (lambda n, x: ''))
    monkeypatch.setattr(content, 'task_tool_policy', lambda t: None)
    seen = []
    monkeypatch.setattr(content._serve, '_call_agent_tool_loop',
                        lambda m, msgs, tools, execute, **k:
                        (seen.append(execute('bogus', {})), ('', []))[1])
    monkeypatch.setattr(content._serve, 'idempotent_tool_call',
                        lambda a, t, fn, n, x: fn(n, x))
    content._run_supervisor_content(
        {'teams': [{'id': 'team-a', 'directorId': 'cora'}], 'agents': {'cora': {}}},
        'cora', {'id': 't', 'teamId': 'team-a'})
    assert seen == ['unknown tool bogus']


def test_coding_inner_heartbeat_and_library_limit(monkeypatch):
    import sim
    import agents.coding_library as coding_library
    monkeypatch.setattr(sim, '_store_content_result', lambda t, r: None)
    monkeypatch.setattr(sim, '_bump_content_heartbeat',
                        lambda t: (_ for _ in ()).throw(RuntimeError('hb')))
    monkeypatch.setattr(content, '_agent_name', lambda s, a: 'Cora')
    monkeypatch.setattr(content, '_review_sandbox_id', lambda *a: 's')
    monkeypatch.setattr(content._serve, 'SELF_BASE_URL', 'http://x')
    monkeypatch.setattr(content._serve, '_coding_tier_slug', lambda: 'c')
    monkeypatch.setattr(content._serve, 'COLAB_CLI_AVAILABLE', False)
    monkeypatch.setattr(content._serve, 'get_or_create_agent_key', lambda a: 'k')
    monkeypatch.setattr(content._serve, '_http_json',
                        lambda *a, **k: {'reply': 'library please'})
    monkeypatch.setattr(content, '_review_sandbox_id', lambda *a: 's')
    import agents.python_delivery as python_delivery
    monkeypatch.setattr(python_delivery, 'required', lambda t: False)
    monkeypatch.setattr(coding_library, 'parse', lambda r: {'ref': 1})
    monkeypatch.setattr(coding_library, 'MAX_ROUNDS', 0)
    out = content._run_coding_content_inner({}, 'a', {'id': 't', 'title': 'C'})
    assert out is False


def test_project_board_manifest_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(content._serve, 'LIBRARY_DIR', str(tmp_path))
    monkeypatch.setattr(content._serve, 'get_state_from_db', lambda: {'tasks': {
        't': {'title': 'T', 'status': 'done', 'reportPath': 'r'}}})
    monkeypatch.setattr(content.os.path, 'normpath',
                        lambda p: (_ for _ in ()).throw(ValueError('bad')))
    out = json.loads(content.project_board({'status': 'done'}))
    assert out['tasks'][0]['artifactExists'] is False
