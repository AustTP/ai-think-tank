import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import context_budget as budget
import company_planning as planning
import pytest


def test_coding_reference_can_be_summarized_without_removing_requirements():
    import content
    requirements = 'Implement the Coinbase client. Add tests importing the implementation.'
    reference = 'Existing client.py source and mypy error evidence. ' * 4000
    messages = content._coding_messages(requirements, reference)
    captured = []
    def summarize(history):
        captured.extend(history)
        return 'client.py exists; repair the reported mypy error and add missing tests.'
    count = lambda history, tools: budget.size(history) // 4
    result = budget.summarize_history(messages, 1500, None, 8192, count, summarize)
    assert captured and reference in captured[0]['content']
    assert result[0] == {'role': 'system', 'content': requirements}
    assert result[-1] == messages[-1]
    assert count(result, None) <= 8192 - 1500 - 512
    assert reference not in result[0]['content']


def test_python_review_keeps_criteria_pinned_and_evidence_summarizable():
    import content
    references = {'criteria': {'C1': 'Build an API client', 'C2': 'Working Python'},
                  'evidence': {'E1': 'Actual source and observed output. ' * 5000}}
    messages = content._python_review_messages('Reviewer', references, 'all green')
    captured = []
    def summarize(history):
        captured.extend(history)
        return 'E1 contains API source and actual output; no live account test occurred.'
    result = budget.summarize_history(messages, 1500, None, 8192,
        lambda history, tools: budget.size(history) // 4, summarize)
    assert captured[0]['role'] == 'assistant'
    assert 'Build an API client' in result[0]['content']
    assert references['evidence']['E1'] not in result[0]['content']
    assert 'criterionId' in result[0]['content']
    assert result[-1] == messages[-1]


def test_long_tool_history_is_bounded_without_orphaned_results():
    messages = [{'role':'system', 'content':'Required policy'}, {'role':'user', 'content':'Original task'}]
    for i in range(20):
        messages += [{'role':'assistant','tool_calls':[{'id':str(i)}]},
                     {'role':'tool','tool_call_id':str(i),'content':'evidence '*4000}]
    messages.append({'role':'user','content':'Finish the original task'})
    result = budget.compact(messages, 900)
    assert budget.size(result) < int((8192-900-768)*2.5)
    assert result[1]['content'] == 'Original task'
    assert result[-1]['content'] == 'Finish the original task'
    calls = {c['id'] for m in result for c in m.get('tool_calls', [])}
    assert all(m['tool_call_id'] in calls for m in result if m['role']=='tool')
    assert messages[3]['content'] == 'evidence '*4000


def test_requirements_are_never_silently_cut():
    with pytest.raises(ValueError, match='Task instructions'):
        budget.compact([{'role':'system','content':'x'*30000}], 900)


def test_only_scrum_masters_and_reviewers_receive_direction():
    state={'agents':{'worker':{'role':'Engineer'}, 'reviewer':{'role':'Reviewer'}, 'master':{'role':'Engineer'}},
           'teams':[{'scrumMasterId':'master'}], 'northStar':{'objective':'Company objective'}}
    text=planning.north_star_context(state)+'\nStory: implement the collector'
    assert 'Company objective' not in planning.task_context(state,'worker',text)
    assert 'Story: implement the collector' in planning.task_context(state,'worker',text)
    for agent in ['master','reviewer']:
        assert planning.task_context(state,agent,text)==text


def test_task_tools_are_selected_and_schema_constraints_survive():
    tools=[{'type':'function','function':{'name':name,'description':'long '*200,'parameters':{'type':'object','required':['url'],'properties':{'url':{'type':'string'}}}}} for name in ['browse_page','search_web','create_calendar_event']]
    selected=budget.select_tools([{'role':'user','content':'Research API documentation'}],tools)
    assert [t['function']['name'] for t in selected]==['browse_page','search_web']
    assert selected[0]['function']['parameters']['required']==['url']
    assert len(selected[0]['function']['description'])==96


def test_history_is_summarized_with_recent_tool_pairs_and_instructions_preserved():
    import copy
    messages = [{'role': 'system', 'content': 'Never invent evidence'},
                {'role': 'user', 'content': 'Inspect real Coinbase integration'}]
    for i in range(12):
        messages += [{'role': 'assistant', 'tool_calls': [{'id': str(i)}]},
                     {'role': 'tool', 'tool_call_id': str(i),
                      'content': 'Evidence from source.py; permission denied. ' * 120}]
    messages.append({'role': 'user', 'content': 'Report the missing access precisely'})
    original = copy.deepcopy(messages)
    captured = []
    def summarize(history):
        captured.extend(history)
        return 'Earlier source.py inspection was denied; real holdings remain unverified.'
    count = lambda history, tools: budget.size(history) // 4 + budget.size(tools or []) // 4
    result = budget.summarize_history(messages, 900, None, 8192, count, summarize)
    assert captured and any(m.get('role') == 'tool' for m in captured)
    assert any('real holdings remain unverified' in m.get('content', '') for m in result)
    assert result[0] == original[0] and result[1] == original[1] and result[-1] == original[-1]
    assert messages == original
    calls = {c['id'] for m in result for c in m.get('tool_calls', [])}
    assert all(m['tool_call_id'] in calls for m in result if m['role'] == 'tool')
    assert any(m.get('tool_call_id') == '11' for m in result)
    assert count(result, None) <= 8192 - 900 - 512


def test_summary_failure_does_not_fall_back_to_full_history():
    messages = [{'role': 'system', 'content': 'Policy'}, {'role': 'user', 'content': 'Task'},
                {'role': 'assistant', 'content': 'old evidence ' * 10000}]
    count = lambda history, tools: budget.size(history) // 4
    with pytest.raises(ValueError, match='no summary'):
        budget.summarize_history(messages, 900, None, 8192, count, lambda history: '')


def test_short_history_does_not_need_a_summary():
    messages = [{'role': 'user', 'content': 'Task'}]
    def unexpected(history):
        raise AssertionError('Unnecessary summary call')
    assert budget.summarize_history(messages, 900, None, 8192,
                                   lambda history, tools: 10, unexpected) == messages
