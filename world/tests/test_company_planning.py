import os
import sys
import unittest
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve
import company_planning as planning

class Planning(unittest.TestCase):
    def test_implementation_request_retains_visible_action_after_discovery_fallback(self):
        state,order=self.seed()
        order.update(title='Implement portfolio reporting', taskType='code')
        planning.apply_plan(state,order,{'stories':[]},'owen')
        build=next(s for s in order['plan']['stories'] if s['taskType']=='code')
        self.assertEqual(build['planningStatus'],'waiting_for_prerequisites')
        self.assertEqual(build['prerequisiteStoryIds'],['discover'])

    def test_explicit_order_and_independent_work_and_potential_stories(self):
        state,order=self.seed()
        def story(sid,kind,deps,**extra):
            return dict(id=sid,title=sid,taskType=kind,instructions='Perform '+sid,
                        acceptanceCriteria='Saved evidence for '+sid,prerequisiteStoryIds=deps,**extra)
        planning.apply_plan(state,order,{'stories':[
            story('apis','research',[]),story('call','code',['apis']),
            story('independent','code',[]),story('optional','code',['call'],potential=True,activationCondition='Call shows pagination is required')]},'owen')
        self.assertEqual(set(order['queuedStoryIds']),{'apis','independent'})
        state['tasks']['api-task']={'id':'api-task','status':'done','workOrderId':order['id'],
                                   'workOrderStoryId':'apis','libraryPath':'apis.csv'}
        planning.release_stories(state,order)
        self.assertIn('call',order['queuedStoryIds'])
        self.assertIn('apis.csv',next(q for q in state['workQueue'] if q.get('workOrderStoryId')=='call')['instructions'])
        self.assertNotIn('optional',order['queuedStoryIds'])

    def test_invalid_and_cyclic_dependencies_are_not_dispatched(self):
        state,order=self.seed()
        stories=[dict(id=sid,title=sid,taskType='research',instructions='Inspect',acceptanceCriteria='Evidence',
                      prerequisiteStoryIds=deps) for sid,deps in [('a',['b']),('b',['a']),('c',['absent'])]]
        planning.apply_plan(state,order,{'stories':stories},'owen')
        self.assertFalse(state['workQueue'])
        self.assertTrue(all(s['planningStatus']=='blocked_invalid_dependencies' for s in order['plan']['stories']))

    def seed(self):
        order={'id':'wrq-1','title':'tools','origin':'player_work_order','status':'pending',
            'reason':'Prepare Coinbase trading tools','teamId':'theo','acceptanceCriteria':'Tools created.'}
        state={'tasks':{},'backlogRequests':[order],'workQueue':[],'playerInbox':[],
            'northStar':{'objective':'Evidence-based Coinbase spot workflow','constraints':'$50 loss limit'}}
        return state,order

    def test_questions_allow_discovery_and_hold_dependent_implementation(self):
        state,order=self.seed()
        plan={'summary':'Research before implementation','questions':[{'id':'mode','question':'What outcome defines tool readiness?'}],
            'stories':[{'id':'facts','title':'Inspect tools','taskType':'spike','instructions':'Inspect existing tools.',
                'acceptanceCriteria':'Save capability inventory.','dependsOnQuestions':[]},
                {'id':'build','title':'Implement tools','taskType':'code','instructions':'Build missing tools.',
                 'acceptanceCriteria':'Tests demonstrate readiness.','dependsOnQuestions':['mode']}]}
        planning.apply_plan(state,order,plan,'owen')
        self.assertEqual(order['status'],'needs_input')
        self.assertEqual(len(state['workQueue']),1)
        self.assertEqual(state['workQueue'][0]['workOrderId'],'wrq-1')
        self.assertNotIn('Company North Star',state['workQueue'][0]['instructions'])
        self.assertEqual(state['workQueue'][0]['reviewContract']['acceptanceCriteria'],'Save capability inventory.')
        self.assertNotIn('Tools created.',state['workQueue'][0]['reviewContract']['instructions'])
        self.assertEqual(state['playerInbox'][0]['agentId'],'owen')
        state['tasks']['facts-task']={'id':'facts-task','workOrderId':'wrq-1','workOrderStoryId':'facts','status':'done'}
        planning.answer(state,'wrq-1','mode','Paper execution tools with tested order previews.')
        self.assertEqual(order['status'],'accepted')
        self.assertEqual(len(state['workQueue']),2)
        self.assertIn('Paper execution',state['workQueue'][1]['instructions'])
        planning.release_stories(state,order)
        self.assertEqual(len(state['workQueue']),2)

    def test_code_waits_for_all_human_answers_even_if_model_omits_dependency(self):
        state,order=self.seed()
        planning.apply_plan(state,order,{'questions':[{'id':'q','question':'Which constraints apply?'}],
            'stories':[{'id':'build','title':'Build','taskType':'code','instructions':'Implement.',
                        'acceptanceCriteria':'Pass checks.'}]},'owen')
        self.assertEqual(state['workQueue'],[])
        self.assertEqual(order['status'],'needs_input')

    def test_old_rejected_player_orders_are_merged_not_rejected_again(self):
        state,order=self.seed();order['status']='rejected';order['filedAt']=1
        newer={**order,'id':'wrq-2','filedAt':2};state['backlogRequests'].append(newer)
        planning.step(state)
        self.assertEqual(order['status'],'superseded')
        self.assertEqual(order['supersededBy'],'wrq-2')
        self.assertEqual(newer['status'],'pending')

    def test_invalid_plan_becomes_discovery_instead_of_rejection(self):
        state,order=self.seed();planning.apply_plan(state,order,{'stories':[]},'owen')
        self.assertEqual(order['status'],'accepted')
        self.assertEqual(state['workQueue'][0]['taskType'],'spike')

    def test_north_star_is_player_owned_persistent_and_versioned(self):
        import threading
        from types import SimpleNamespace
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        state={'tasks':{}}
        fake=SimpleNamespace(_STATE_UPDATE_LOCK=threading.RLock(),get_state_from_db=lambda:state,
            save_state_to_db=lambda value:None,
            _require_player_session=lambda request:request.cookies.get('player')=='yes')
        app=FastAPI();app.include_router(planning.router_for(fake))
        with TestClient(app) as client:
            self.assertEqual(client.post('/api/intent/north-star',json={'objective':'Goal'}).status_code,401)
            client.cookies.set('player','yes')
            result=client.post('/api/intent/north-star',json={'objective':'Research, test and deliver reliable tools',
                'successMeasures':'Verified artifacts and completed milestones','constraints':'Spot trading only'})
            self.assertEqual(result.status_code,200)
            self.assertEqual(state['northStar']['version'],1)
            self.assertIn('Spot trading only',planning.north_star_context(state))
            self.assertEqual(client.get('/api/intent/north-star').json()['northStar']['objective'],state['northStar']['objective'])

    def test_technical_questions_are_researched_and_code_milestone_is_created(self):
        state,order=self.seed();order['taskType']='code'
        planning.apply_plan(state,order,{'questions':[{'id':'language','question':'What programming language should we use?'}],
            'stories':[{'id':'facts','title':'Inspect app','taskType':'spike','instructions':'Inspect current code.',
                        'acceptanceCriteria':'Save stack and API inventory.'}]},'owen')
        self.assertEqual(order['status'],'accepted')
        self.assertEqual(order['plan']['questions'][0]['status'],'delegated_to_research')
        implementation=next(story for story in order['plan']['stories'] if story['taskType']=='code')
        self.assertNotIn(implementation['id'],order['queuedStoryIds'])
        state['tasks']['facts-task']={'id':'facts-task','workOrderId':order['id'],'workOrderStoryId':'facts','status':'done'}
        planning.release_stories(state,order)
        self.assertIn(implementation['id'],order['queuedStoryIds'])

    def test_restart_resumes_existing_ids_once_and_keeps_budget(self):
        import sim
        from unittest.mock import patch
        task={'id':'running','status':'working','_contentInFlight':True,'assignedTo':'ada','budgetUsd':0.15}
        state={'tasks':{'running':task},'agents':{'ada':{'task':'running','busy':True}}}
        with patch.object(sim,'_restart_recovery_done',False), patch.object(sim,'_content_executor',lambda *a:None), \
             patch.object(sim,'_dispatch_content_work',return_value=True) as dispatch:
            self.assertEqual(sim._recover_content_after_restart(state),['running'])
            self.assertEqual(sim._recover_content_after_restart(state),[])
            self.assertEqual(dispatch.call_count,1)
        self.assertEqual(task['id'],'running')
        self.assertEqual(task['budgetUsd'],0.15)
        self.assertEqual(task['status'],'working')

    def test_autosave_cannot_overwrite_the_player_north_star(self):
        state={'sim':{'owner':'server'},'northStar':{'objective':'Player goal'},'backlogRequests':[{'id':'real'}]}
        merged=serve._merge_server_owned(state,{'northStar':{'objective':'Stale browser goal'},'backlogRequests':[]})
        self.assertEqual(merged['northStar'],state['northStar'])
        self.assertEqual(merged['backlogRequests'],state['backlogRequests'])
