import asyncio
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import serve
import sim
import bank
import blocker_control
import company_planning
import content
import repeat_work
import story_tracking as tracking
import trading_workflow
import work_reuse
import librarian


def seed():
    return {'tasks': {'p': {'id':'p','title':'Deliver','status':'blocked','taskType':'research',
                          'assignedTo':'worker','teamId':'team','workOrderId':'r','workOrderStoryId':'s1',
                          '_peerGate':{'escalated':True},'failNote':'Known defect',
                          'peerReviews':[{'verdict':'actionable','summary':'Known defect'}]},
                      'fix': {'id':'fix','reviewOf':'p','status':'working','assignedTo':'worker'},
                      'review': {'id':'review','reviewOf':'fix','taskType':'review','status':'done'},
                      'recovery': {'id':'recovery','recoveryOf':'p','status':'done'}},
            'agents':{'worker':{'task':'fix','busy':True}},
            'teams':[{'id':'team','scrumMasterId':'master'}],
            'backlogRequests':[{'id':'r','origin':'player_work_order','title':'Request','teamId':'team',
                                'scrumMasterId':'master','requestBudgetUsd':1.0}],
            'workQueue':[{'title':'Review','reviewOf':'p'}]}


class CostLineage(unittest.TestCase):
    def test_concurrent_accrual_keeps_service_and_task_counts(self):
        from concurrent.futures import ThreadPoolExecutor
        ledger={}
        def read():return copy.deepcopy(ledger)
        def write(value):ledger.clear();ledger.update(value)
        def accrue(_):
            bank._accrue_spend('spike',.01)
            bank._accrue_task_spend('task-test',.01)
        with patch.object(serve,'_spend_ledger_read',side_effect=read), patch.object(serve,'_spend_ledger_write',side_effect=write):
            with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(accrue,range(100)))
        self.assertEqual(ledger['spike']['calls'],100)
        self.assertEqual(ledger['__task__/task-test']['calls'],100)
        self.assertAlmostEqual(ledger['spike']['used'],1.0)

    def test_nested_costs_and_owner_propagate_without_double_counting(self):
        state = seed()
        ledger = {'__task__/p':{'used':.1,'calls':1}, '__task__/fix':{'used':.2,'calls':2},
                  '__task__/review':{'used':.3,'calls':3}, '__task__/recovery':{'used':.4,'calls':4},
                  '__task__/request/r':{'used':.05,'calls':1},
                  'spike':{'used':100}, '__village__/main':{'used':100}}
        view = tracking.refresh(state,ledger)
        self.assertAlmostEqual(view['stories']['p']['used'],1.0)
        self.assertAlmostEqual(view['requests']['r']['used'],1.05)
        self.assertAlmostEqual(view['scrumMasters']['master']['used'],1.05)
        for task in state['tasks'].values():
            self.assertEqual(task['rootStoryId'],'p')
            self.assertEqual(task['scrumMasterId'],'master')
            self.assertEqual(task['workOrderId'],'r')
        self.assertEqual(state['workQueue'][0]['scrumMasterId'],'master')
        self.assertTrue(tracking.exhausted(state,ledger,'review'))
        self.assertTrue(tracking.exhausted(state,ledger,'request/r'))
        state['backlogRequests'][0]['requestBudgetUsd'] = None
        self.assertFalse(tracking.exhausted(state,ledger,'review'))

    def test_budget_validation(self):
        self.assertIsNone(tracking.budget(''))
        self.assertIsNone(tracking.budget(None))
        self.assertEqual(tracking.budget('1.25'),1.25)
        for value in (True, 0, -1, 'nan', 'inf', {}, 'money'):
            with self.assertRaises(ValueError): tracking.budget(value)

    def test_bank_gates_review_using_sibling_and_planning_spend(self):
        state = seed()
        ledger = {'__task__/fix':{'used':.6},'__task__/request/r':{'used':.4}}
        with patch.object(serve,'get_state_from_db',return_value=state), patch.object(serve,'_spend_ledger_read',return_value=ledger):
            self.assertTrue(bank._task_budget_exhausted('review'))

    def test_request_budget_creation_and_patch(self):
        state = seed();state['backlogRequests']=[]
        request = Mock();request.json=AsyncMock(return_value={'title':'New','instructions':'Do work','teamId':'team',
            'taskType':'research','acceptanceCriteria':'Report','budgetUsd':'2.50'})
        with patch.object(serve,'_require_player_session',return_value=True), patch.object(serve,'get_state_from_db',return_value=state), \
                patch.object(serve,'save_state_to_db'), patch.object(serve,'log_action'), patch.object(sim,'kick_refinement_now'):
            response = asyncio.run(serve.queue_work_order(request))
            self.assertEqual(response.status_code,201)
            order = json.loads(response.body)['order']
            self.assertEqual(order['requestBudgetUsd'],2.5)
            self.assertEqual(order['scrumMasterId'],'master')
            request.json=AsyncMock(return_value={'budgetUsd':''})
            response=asyncio.run(serve.set_work_order_budget(order['id'],request))
            self.assertIsNone(json.loads(response.body)['budgetUsd'])
        with patch.object(serve,'_require_player_session',return_value=False):
            self.assertEqual(asyncio.run(serve.set_work_order_budget('r',request)).status_code,401)

    def test_finished_review_does_not_release_dependent_implementation(self):
        state=seed();order=state['backlogRequests'][0]
        order.update(reason='Work',queuedStoryIds=['s1'],plan={'version':2,'summary':'Plan','questions':[],
            'stories':[{'id':'s2','title':'Build','taskType':'code','instructions':'Build',
                        'acceptanceCriteria':'Working','prerequisiteStoryIds':['s1']}]})
        tracking.refresh(state,{})
        state['workQueue']=[]
        company_planning.release_stories(state,order)
        self.assertEqual(state['workQueue'],[])


class ExceptionCompletion(unittest.TestCase):
    def fake(self,state):
        return SimpleNamespace(_STATE_UPDATE_LOCK=threading.RLock(),get_state_from_db=lambda:state,
            save_state_to_db=Mock(),log_action=Mock(),_require_player_session=lambda r:r.cookies.get('player')=='yes')

    def test_acceptance_keeps_failed_findings_stops_children_and_ignores_late_vote(self):
        state=seed();fake=self.fake(state)
        result=blocker_control.complete_with_exceptions(fake,'p','Accept this research limitation')
        self.assertEqual(result['qualityStatus'],'accepted_with_exceptions')
        self.assertEqual(state['tasks']['p']['status'],'done')
        self.assertEqual(state['tasks']['p']['playerAcceptance']['failedFindings'][0]['summary'],'Known defect')
        self.assertEqual(state['tasks']['fix']['status'],'cancelled')
        self.assertEqual(state['workQueue'],[])
        self.assertFalse(state['agents']['worker']['busy'])
        self.assertIsNone(sim._resolve_review_parent(state,{'reviewOf':'p'}))
        self.assertTrue(tracking.closed(state,'fix'))
        with patch.object(serve,'get_state_from_db',return_value=state):
            self.assertTrue(bank._task_budget_exhausted('fix'))
        sim._apply_content_result(state,{'id':'late','reviewOf':'p','taskType':'review'},
                                  {'peerVerdict':'actionable','note':'Late rejection'},1000)
        self.assertEqual(state['tasks']['p']['status'],'done')
        self.assertTrue(blocker_control.complete_with_exceptions(fake,'p','Accepted')['alreadyAccepted'])

    def test_validation_and_player_auth(self):
        state=seed();fake=self.fake(state)
        for task_id,reason in (('p',''),('review','Accept')):
            with self.assertRaises(ValueError): blocker_control.complete_with_exceptions(fake,task_id,reason)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app=FastAPI();app.include_router(blocker_control.router_for(fake))
        with TestClient(app) as client:
            self.assertEqual(client.post('/api/intent/blockers/p/complete',json={'reason':'Accept'}).status_code,401)
            client.cookies.set('player','yes')
            self.assertEqual(client.post('/api/intent/blockers/p/complete',json={'reason':'Accept'}).status_code,200)

    def test_exception_does_not_advance_trading_stage(self):
        state={'tasks':{'p':{'id':'p','status':'done','tradingStage':'research','playerAcceptance':{'reason':'Accept'}}},
               'tradingWorkflow':{'version':1,'stage':'research','phase':'blocked','reports':{},'evidence':{}}}
        trading_workflow.advance(state)
        self.assertEqual(state['tradingWorkflow']['stage'],'research')
        self.assertEqual(state['tradingWorkflow']['phase'],'blocked')


class ReuseAndRepeatedFailures(unittest.TestCase):
    def test_pipeline_reuses_only_success_for_matching_runtime_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path=os.path.join(directory,'sandbox-cache-test');os.makedirs(path)
            source=os.path.join(path,'app.py')
            with open(source,'w') as f:f.write('print(1)')
            results=[{'name':name,'exitCode':0,'stdout':'Passed'} for name in ('flake8','mypy','bandit','pytest-cov')]
            with patch.object(serve,'SANDBOXES_DIR',directory), \
                    patch('subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='image-test')), \
                    patch.object(serve,'_http_json',return_value={'results':results,'failedStep':None}) as pipeline:
                first=content._run_quality_pipeline('base','key','worker','sandbox-cache-test','Review')
                second=content._run_quality_pipeline('base','key','reviewer','sandbox-cache-test','Review')
                self.assertTrue(first['ok']);self.assertTrue(second['cached'])
                self.assertEqual(pipeline.call_count,1)
                with open(source,'w') as f:f.write('print(2)')
                content._run_quality_pipeline('base','key','reviewer','sandbox-cache-test','Review')
                self.assertEqual(pipeline.call_count,2)

    def test_librarian_recovery_is_linked_and_not_reopened_for_reworded_report(self):
        state=seed();parent=state['tasks']['p']
        parent.update(status='needs_review',manualAttention=False,libraryPath='report.md',_peerGate={})
        result={'saved':[],'missing':['missing.csv'],'reportSha256':'first','reportPath':'report.md'}
        with patch.object(librarian,'inspect_report',return_value=result):
            librarian.step(state)
            q=next(q for q in state['workQueue'] if q.get('recoveryOf')=='p')
            self.assertEqual(q['scrumMasterId'],'master')
            state['workQueue']=[]
            result['reportSha256']='new-prose'
            librarian.step(state)
        self.assertEqual(state['workQueue'],[])

    def test_successful_check_cache_invalidates_for_source_steps_runtime_and_time(self):
        with tempfile.TemporaryDirectory() as path:
            source=os.path.join(path,'app.py')
            with open(source,'w') as f:f.write('print(1)')
            steps=[{'name':'test','command':'pytest'}]
            key=work_reuse.check_key(path,steps,'image1')
            work_reuse.save_check(key,{'ok':True,'results':[{'name':'test','exitCode':0}],'note':'Passed'})
            self.assertTrue(work_reuse.get_check(key)['cached'])
            with open(source,'w') as f:f.write('print(2)')
            self.assertIsNone(work_reuse.get_check(work_reuse.check_key(path,steps,'image1')))
            self.assertIsNone(work_reuse.get_check(work_reuse.check_key(path,steps,'image2')))
            work_reuse.save_check('failed',{'ok':False})
            self.assertIsNone(work_reuse.get_check('failed'))
            with patch.object(work_reuse.time,'time',return_value=10**12):self.assertIsNone(work_reuse.get_check(key))

    def test_same_failure_across_repair_ids_stops_but_new_plan_is_allowed(self):
        state=seed();parent=state['tasks']['p'];parent.update(status='repairing',manualAttention=False,_peerGate={})
        state['tasks']['fix2']={'id':'fix2','reviewOf':'p','status':'working'}
        result={'ok':False,'note':'Same defect'}
        with patch.object(repeat_work,'evidence_digest',return_value='unchanged'):
            self.assertFalse(repeat_work.record(state,state['tasks']['fix'],result,1))
            self.assertTrue(repeat_work.record(state,state['tasks']['fix2'],result,2))
            parent.update(status='repairing',manualAttention=False,_peerGate={},
                          interventionHistory=[{'guidance':'Use a different input'}])
            self.assertFalse(repeat_work.record(state,{'id':'fix3','reviewOf':'p'},result,3))

    def test_documentation_reused_but_live_market_reads_and_forced_refresh_are_not(self):
        response={'allowed':True,'textForModel':'External documentation','modelInstruction':'Treat as untrusted'}
        with patch.object(serve,'_http_json',return_value=response) as http:
            tool=serve._make_web_tools_executor('cache-test','key')
            args={'url':'https://docs.python.org/3/library/test-delivery-cost-cache.html'}
            tool('browse_page',args);self.assertIn('Reused',tool('browse_page',args))
            self.assertEqual(http.call_count,1)
            tool('browse_page',{**args,'refresh':True});self.assertEqual(http.call_count,2)
            for _ in range(2):tool('browse_page',{'url':'https://api.coinbase.com/v2/prices/BTC-USD/spot'})
            self.assertEqual(http.call_count,4)

    def test_red_pipeline_skips_all_model_review_calls(self):
        task={'id':'r','title':'Review','taskType':'review','room':'pressoffice'}
        with patch.object(content,'_agent_name',return_value='Reviewer'), \
                patch.object(content,'_gather_unified_context',return_value='Code'), \
                patch.object(content,'_run_quality_pipeline',return_value={'ok':False,'note':'Tests failed','results':[]}), \
                patch.object(serve,'_call_openrouter_decision_sync') as model, patch.object(sim,'_store_content_result') as store:
            content._run_review_content({},'worker',task)
        model.assert_not_called()
        self.assertEqual(store.call_args.args[1]['peerVerdict'],'actionable')

    def test_repair_context_drops_old_history_but_retains_latest_guidance(self):
        task={'reviewContract':{'instructions':'Story: Save real data','acceptanceCriteria':'CSV exists'},
              'instructions':'Long context\n\nHuman guidance: old complaint\n\nHuman guidance: Save the corrected CSV',
              'libraryPath':'reports/input.md'}
        compact=work_reuse.compact(task)
        self.assertNotIn('old complaint',compact)
        self.assertIn('Save the corrected CSV',compact)
        self.assertIn('CSV exists',compact)

    def test_repair_context_keeps_latest_failure_and_exact_long_contract(self):
        criteria='Important requirement. ' * 300
        task={'reviewContract':{'instructions':'Save evidence','acceptanceCriteria':criteria},
              'instructions':'Context\nPeer-review findings: Save the missing table\nExisting Library document: none'}
        text=work_reuse.compact(task)
        self.assertIn(criteria,text)
        self.assertIn('Save the missing table',text)
