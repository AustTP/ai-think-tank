import json
import shlex
import subprocess
import sys
from unittest.mock import patch

import approval_automation
import code_workspaces
import coding_handoff
import story_tracking
import structured_edits
import sim
import completion_watchdog


def test_same_request_stories_get_separate_workspaces_and_repairs_reuse_their_parent(tmp_path):
    a={'id':'a','workOrderId':'request','codeWorkspaceVersion':2}
    b={'id':'b','workOrderId':'request','codeWorkspaceVersion':2}
    state={'tasks':{'a':a,'b':b}}
    first=code_workspaces.prepare(state,a,tmp_path)
    second=code_workspaces.prepare(state,b,tmp_path)
    assert first != second
    assert code_workspaces.sandbox_for(state,{'reviewOf':'a'}) == first
    assert (tmp_path/first/'tests').is_dir()


def test_edits_validate_every_file_before_writing_and_reject_stale_context(tmp_path):
    (tmp_path/'a.py').write_text('value = 1\n')
    edits={'edits':[{'path':'a.py','old':'value = 1','new':'value = 2'},
                    {'path':'b.py','old':'missing','new':'changed'}]}
    args=shlex.split(structured_edits.command(json.dumps(edits)))
    result=subprocess.run([sys.executable,*args[1:]],cwd=tmp_path,capture_output=True)
    assert result.returncode != 0
    assert (tmp_path/'a.py').read_text() == 'value = 1\n'
    edits['edits']=edits['edits'][:1]
    args=shlex.split(structured_edits.command(json.dumps(edits)))
    assert subprocess.run([sys.executable,*args[1:]],cwd=tmp_path,capture_output=True).returncode == 0
    assert (tmp_path/'a.py').read_text() == 'value = 2\n'


def test_story_budget_includes_repairs_and_review_and_uses_tightest_remaining_cap():
    state={'tasks':{'root':{'id':'root','budgetUsd':0.5,'workOrderId':'r'},
                    'fix':{'id':'fix','reviewOf':'root','budgetUsd':0.5}},
           'backlogRequests':[{'id':'r','requestBudgetUsd':5}]}
    ledger={'__task__/root':{'used':0.2},'__task__/fix':{'used':0.31}}
    assert story_tracking.exhausted(state,ledger,'fix')
    assert story_tracking.budget_status(state,ledger,'fix')['scope'] == 'story'
    assert story_tracking.budget_status(state,ledger,'fix')['used'] == 0.51


def test_resolved_decisions_survive_repair_context_compaction():
    parent={'id':'root','workOrderId':'r','workOrderStoryId':'s4'}
    state={'tasks':{'root':parent},'backlogRequests':[{'id':'r','plan':{
        'questions':[{'id':'q1','question':'Report format?','status':'answered','answer':'human-readable text'}],
        'stories':[{'id':'s4','prerequisiteStoryIds':[]}]}}]}
    assert 'human-readable text' in coding_handoff.context(state,{'id':'fix','reviewOf':'root'})


def test_repair_steps_and_latest_guidance_survive_verbose_failure_history():
    import work_reuse
    parent = {'id':'root', 'repairStories':[
        {'id':'r1','status':'queued','instructions':'Implement the missing agent integration',
         'acceptanceCriteria':'P&L stop conditions pass'},
        {'id':'r2','status':'done','instructions':'Old completed work'}]}
    state = {'tasks':{'root':parent}}
    context = coding_handoff.context(state, {'id':'fix','reviewOf':'root'})
    assert 'Implement the missing agent integration' in context
    assert 'P&L stop conditions pass' in context
    assert 'Old completed work' not in context
    compact = work_reuse.compact({'currentFailure':'old failure ' * 1000,
        'instructions':'Original contract\n\nHuman guidance: Read the verified prerequisite first'})
    assert 'Read the verified prerequisite first' in compact


def test_recovery_evidence_ids_resolve_only_to_supplied_findings():
    card={'evidence':'Module has a syntax error.', 'findings':[]}
    proposal={'action':'investigate','reason':'Inspect the error.','steps':['Read the affected file.'],
              'evidenceIds':['E1'],'verification':'Import succeeds.'}
    assert approval_automation.validate_recommendation(proposal,card)['evidence'] == [card['evidence']]
    proposal['evidenceIds']=['E999']
    import pytest
    with pytest.raises(ValueError,match='Evidence IDs'):
        approval_automation.validate_recommendation(proposal,card)


def test_independent_stories_can_be_staffed_while_same_workspace_is_serialized():
    state={'agents':{}, 'tasks':{'a':{'id':'a','room':'pressoffice','status':'working','codeWorkspaceVersion':2},
                               'b':{'id':'b','room':'pressoffice','status':'repairing','codeWorkspaceVersion':2}}}
    with patch.object(sim,'_eligible_candidates',return_value=[]) as candidates:
        sim._assign_due_item(state,{'room':'pressoffice','reviewOf':'b'},False,None,None,1000)
        candidates.assert_called_once()
    with patch.object(sim,'_eligible_candidates',return_value=[]) as candidates:
        sim._assign_due_item(state,{'room':'pressoffice','reviewOf':'a'},False,None,None,1000)
        candidates.assert_not_called()


def test_exhausted_story_does_not_keep_dispatching_repair_tasks():
    state={'tasks':{'p':{'id':'p','budgetUsd':0.5,'storySpendUsd':0.51}}}
    assert not completion_watchdog.can_start(state,{'reviewOf':'p'})


def test_nested_reviews_and_repairs_stay_in_the_original_story_workspace():
    state={'tasks':{'p':{'id':'p','codeWorkspaceVersion':2},
                    'fix':{'id':'fix','reviewOf':'p','codeWorkspaceVersion':2}}}
    assert code_workspaces.sandbox_for(state,{'reviewOf':'fix'}) == code_workspaces.sandbox_for(state,state['tasks']['p'])


def test_private_execution_rejects_other_story_even_in_always_approve_mode():
    import serve
    from types import SimpleNamespace
    state={'agents':{'ada':{'task':'fix'}},'tasks':{'p':{'id':'p','codeWorkspaceVersion':2},
        'other':{'id':'other','codeWorkspaceVersion':2},'fix':{'id':'fix','reviewOf':'p'}}}
    request=SimpleNamespace(cookies={},headers={'X-Agent-Key':'valid'})
    own=code_workspaces.sandbox_for(state,state['tasks']['p'])
    other=code_workspaces.sandbox_for(state,state['tasks']['other'])
    with patch.object(serve,'get_state_from_db',return_value=state), patch.object(serve,'verify_session',return_value=False), \
            patch.object(serve,'verify_agent_key',return_value=True):
        assert serve._private_story_access(request,'ada',own)
        assert not serve._private_story_access(request,'ada',other)


def test_workspace_migration_keeps_valid_failed_edits_and_preserves_original(tmp_path):
    import staged_work,shutil
    (tmp_path/'legacy').mkdir()
    (tmp_path/'legacy'/'app.py').write_text('value = 1\n')
    stage=staged_work.Stage(tmp_path,'legacy')
    (stage.directory/'app.py').write_text('value = 2\n')
    stage.checkpoint()
    shutil.copytree(tmp_path/'legacy',tmp_path/'story-new')
    copied=code_workspaces.fork_checkpoint(tmp_path,'legacy','story-new',stage.id)
    resumed=staged_work.Stage(tmp_path,'story-new',resume=copied)
    assert (resumed.directory/'app.py').read_text()=='value = 2\n'
    assert (tmp_path/'legacy'/'app.py').read_text()=='value = 1\n'
    assert stage.directory.is_dir()


def test_explicit_unlimited_story_removes_child_caps_without_erasing_costs():
    state={'tasks':{'p':{'id':'p','budgetUnlimited':True,'budgetUsd':0},
                    'fix':{'id':'fix','reviewOf':'p','budgetUsd':0.5}}}
    ledger={'__task__/p':{'used':1.0},'__task__/fix':{'used':2.0}}
    assert not story_tracking.exhausted(state,ledger,'fix')
    status=story_tracking.budget_status(state,ledger,'fix')
    assert status['scope']=='unlimited' and status['used']==3.0
    assert state['tasks']['fix']['budgetUsd']==0
