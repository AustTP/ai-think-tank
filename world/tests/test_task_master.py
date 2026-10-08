import os
import sys
import unittest
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve
import task_master as master


def seed(busy=0):
    roster=[{'id':str(i),'name':str(i)} for i in range(4)] + [{'id':'q','dedicatedReviewer':True}]
    return {'agentRoster':roster,'agents':{d['id']:{'id':d['id'],'offDuty':True,'busy':int(d['id'])<busy if d['id'].isdigit() else False} for d in roster},
            'tasks':{},'teams':[],'workQueue':[],'backlogRequests':[]}

class TaskMaster(unittest.TestCase):
    def test_below_half_idle_does_not_create_work(self):
        state=seed(3)
        self.assertEqual(master.step(state,10000000),[])
        self.assertEqual(state['workQueue'],[])
        self.assertEqual(state['taskMasterControl']['totalWorkers'],4)

    def test_exactly_half_idle_creates_a_bounded_owned_story(self):
        state=seed(2)
        owners=master.step(state,10000000)
        self.assertEqual(len(owners),1)
        self.assertEqual(state['workQueue'][0]['assignedTo'],owners[0])
        self.assertTrue(state['workQueue'][0]['taskMasterWork'])
        self.assertEqual(state['workQueue'][0]['budgetMs'],120000)
        self.assertEqual(master.step(state,10000001),[])
        self.assertEqual(len(state['workQueue']),1)

    def test_existing_work_is_staffed_before_new_work(self):
        state=seed()
        state['workQueue']=[{'title':'Player work','room':'observatory'},{'title':'Another story','room':'observatory'}]
        owners=master.step(state,10000000)
        self.assertEqual(len(owners),2)
        self.assertEqual(len(set(owners)),2)
        self.assertEqual(len(state['workQueue']),2)
        self.assertEqual(state['workQueue'][0]['title'],'Player work')

    def test_completed_discovery_is_not_recreated_on_every_cycle(self):
        state=seed()
        master.step(state,10000000)
        state['workQueue']=[]
        self.assertEqual(master.step(state,10000000+master.COOLDOWN_MS),[])
        self.assertEqual(state['workQueue'],[])

    def test_role_is_created_once_and_reserved(self):
        state=seed()
        master.create_role(state);master.create_role(state)
        self.assertEqual(sum(d['id']==master.AGENT_ID for d in state['agentRoster']),1)
        self.assertNotIn(master.AGENT_ID,master.workers(state))

    def test_unroutable_stale_requests_do_not_block_work_discovery(self):
        state=seed()
        state['backlogRequests']=[{'id':'old','status':'pending','filedBy':'missing-reviewer','title':'Stale follow-up'}]
        self.assertEqual(len(master.step(state,10000000)),1)
        self.assertEqual(len(state['workQueue']),1)
