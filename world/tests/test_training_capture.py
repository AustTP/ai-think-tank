import concurrent.futures
import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import training_capture as capture


def configure_capture(monkeypatch, tmp_path):
    path = tmp_path / 'capture.sqlite3'
    monkeypatch.setattr(capture, 'config', lambda: {'TRAINING_CAPTURE_ENABLED': 'true',
        'TRAINING_CAPTURE_PATH': str(path), 'EXAMPLE_API_KEY': 'private-value-12345'})
    return path


def test_secrets_tool_messages_and_seed_are_preserved_safely(monkeypatch, tmp_path):
    path = configure_capture(monkeypatch, tmp_path)
    @capture.record_call('unsloth-local')
    def completion(selection, messages, max_tokens, tools=None):
        return {'choices': [{'message': {'role': 'assistant', 'content': 'Supported report'}}]}
    messages = [{'role': 'system', 'content': 'Write a report'},
                {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'Evidence; private-value-12345'},
                {'role': 'user', 'content': 'Summarize the evidence'},
                {'role': 'assistant', 'tool_calls': [{'function': {'name': 'request', 'arguments': '{"password":"another-private-password"}'}}]}]
    with capture.context({'agent_id': 'a', 'task_id': 't', 'group_id': 'request-1'}):
        completion({'model': 'adapter', 'seed': 45, 'api_key': 'never-record-this'}, messages, 200,
                   tools=[{'type': 'function', 'function': {'name': 'browse'}}])
        capture.event('tool_result', 't', {'password': 'unrelated-secret', 'result': 'real excerpt'})
    with sqlite3.connect(path) as conn:
        row = conn.execute('SELECT payload,seed,flags FROM interactions').fetchone()
        event = conn.execute('SELECT payload FROM events').fetchone()[0]
    assert 'private-value-12345' not in row[0] and 'never-record-this' not in row[0]
    assert 'unrelated-secret' not in event
    assert 'another-private-password' not in row[0]
    assert row[1] == 45 and 'credentials_redacted' in row[2]
    request = json.loads(row[0])['request']
    assert request['messages'][1]['tool_call_id'] == 'call-1'
    assert request['tools'][0]['function']['name'] == 'browse'


def test_concurrent_agents_and_outcomes_are_isolated(monkeypatch, tmp_path):
    path = configure_capture(monkeypatch, tmp_path)
    def worker(agent):
        with capture.context({'agent_id': agent, 'task_id': agent, 'group_id': 'root-'+agent}):
            capture.record('local', {'model': 'm', 'messages': [{'role':'user','content':agent}]},
                {'choices':[{'message':{'content':'answer-'+agent}}]})
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, ['a','b','c','d']))
    capture.sync_outcomes({'tasks': {'a': {'id':'a','status':'done','pipelineOk':True},
                                    'b': {'id':'b','status':'failed','failNote':'Incorrect citation'}}})
    with sqlite3.connect(path) as conn:
        rows = conn.execute('SELECT agent_id,task_id,trace_id FROM interactions').fetchall()
        outcome = json.loads(conn.execute("SELECT payload FROM task_outcomes WHERE task_id='b'").fetchone()[0])
    assert len(rows) == 4 and len({r[2] for r in rows}) == 4
    assert all(a == t for a, t, _ in rows)
    assert outcome['data']['status'] == 'failed'


def test_capture_failure_does_not_interrupt_model(monkeypatch, tmp_path):
    configure_capture(monkeypatch, tmp_path)
    monkeypatch.setattr(capture, 'database', lambda: (_ for _ in ()).throw(OSError('disk unavailable')))
    @capture.record_call('local')
    def completion(model, messages, max_tokens):
        return {'choices':[{'message':{'content':'done'}}]}
    with capture.context({'agent_id':'a','group_id':'g'}):
        assert completion('m', [], 100)['choices'][0]['message']['content'] == 'done'


def test_failed_calls_remain_diagnostic_records(monkeypatch, tmp_path):
    path = configure_capture(monkeypatch, tmp_path)
    @capture.record_call('local')
    def completion(model, messages, max_tokens):
        raise RuntimeError('failure private-value-12345')
    with capture.context({'group_id':'g'}):
        try:
            completion('m', [], 100)
        except RuntimeError:
            pass
    with sqlite3.connect(path) as conn:
        row = conn.execute('SELECT ok,payload FROM interactions').fetchone()
    assert row[0] == 0 and 'private-value-12345' not in row[1]


def test_export_requires_approval_and_keeps_request_groups(monkeypatch, tmp_path):
    path = configure_capture(monkeypatch, tmp_path)
    for identifier, group in [('a','request-1'),('b','request-1'),('c','request-2')]:
        with capture.context({'agent_id':identifier,'task_id':identifier,'group_id':group}):
            capture.record('local', {'model':'m','messages':[{'role':'user','content':'question-'+identifier}]},
                {'choices':[{'message':{'content':'answer-'+identifier},'finish_reason':'stop'}]})
    script = Path(__file__).resolve().parents[2] / 'scripts/export_training_interactions.py'
    spec = importlib.util.spec_from_file_location('export_interactions', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        summary = module.export(conn, tmp_path / 'out')
        assert summary['train'] == summary['eval'] == 0
        conn.execute("UPDATE interactions SET review_status='approved'")
        summary = module.export(conn, tmp_path / 'out', 0.5)
    assert summary['train'] + summary['eval'] == 3
    manifest = [json.loads(x) for x in (tmp_path/'out/manifest.jsonl').read_text().splitlines()]
    groups = {}
    for row in manifest:
        groups.setdefault(row['group_id'], set()).add(row['split'])
    assert all(len(splits)==1 for splits in groups.values())
    assert groups['request-1'] != groups['request-2']


def test_chat_boundary_records_real_request_payload_and_thread_context(monkeypatch, tmp_path):
    path = configure_capture(monkeypatch, tmp_path)
    import serve
    task = {'id':'t','status':'working','rootStoryId':'root','requestId':'request-1'}
    state = {'agents': {'a': {'role':'Researcher','task':task}}, 'tasks':{'t':task}}
    request = MagicMock()
    request.json = AsyncMock(return_value={'model':'remote-model','agentId':'a','plain':False,
        'messages':[{'role':'user','content':'Evidence: observed value is 42. Summarize.'}], 'max_tokens':100})
    request.cookies = {}
    request.headers = {'X-Agent-Key':'test-agent-key'}
    reply = {'choices':[{'message':{'role':'assistant','content':'The observed value is 42.'}}]}
    with patch.multiple(serve, OPENROUTER_API_KEY='test', get_state_from_db=MagicMock(return_value=state),
        verify_session=MagicMock(return_value=False), verify_agent_key=MagicMock(return_value=True),
        _load_env=MagicMock(return_value={}), _ttc_should_deliberate=MagicMock(return_value=False),
        _think_tank_spend_cap_exceeded=MagicMock(return_value=False), is_model_circuit_broken=MagicMock(return_value=False),
        _coding_tier_slug=MagicMock(return_value=None), _high_tier_slug=MagicMock(return_value=None),
        _mid_tier_slug=MagicMock(return_value=None), _reasoning_tier_slug=MagicMock(return_value=None),
        record_model_result=MagicMock(), log_action=MagicMock(),
        _urlopen_with_resilience=MagicMock(return_value=json.dumps(reply))):
        response = asyncio.run(serve._chat_request(request))
    assert response.status_code == 200
    with sqlite3.connect(path) as conn:
        row = conn.execute('SELECT agent_id,group_id,payload FROM interactions').fetchone()
    assert row[0]=='a' and row[1]=='request-1'
    recorded = json.loads(row[2])['request']
    assert recorded['reasoning'] == {'enabled':False}
    assert 'headers' not in recorded and recorded['max_tokens']==100
    assert capture.CURRENT.get() is None
