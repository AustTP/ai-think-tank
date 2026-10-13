import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import context_budget as budget
import company_planning as planning
import pytest


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
