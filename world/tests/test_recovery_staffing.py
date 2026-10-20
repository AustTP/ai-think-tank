import os
import sys
import unittest
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)))
import recovery_staffing


class RecoveryStaffing(unittest.TestCase):
    def test_researchers_share_work_and_scrum_master_coordinates(self):
        state={'agents':{'iris':{},'ada':{},'ben':{}},
               'agentRoster':[{'id':'iris'},{'id':'ada'},{'id':'ben'}],
               'teams':[{'id':'nora','scrumMasterId':'iris','members':['ada','ben']}],
               'tasks':{str(i):{'id':str(i),'title':'Research','status':'investigating','assignedTo':'iris'} for i in range(2)},'workQueue':[]}
        recovery_staffing.step(state)
        self.assertEqual({q['assignedTo'] for q in state['workQueue']},{'ada','ben'})
        self.assertTrue(all(t['recoveryOwner']=='iris' for t in state['tasks'].values()))
        recovery_staffing.step(state)
        self.assertEqual(len(state['workQueue']),2)

    def test_active_investigation_is_not_reassigned(self):
        state={'agents':{'iris':{},'ada':{}},'agentRoster':[{'id':'ada'}],
            'teams':[{'id':'nora','scrumMasterId':'iris','members':['ada']}],
            'tasks':{'p':{'id':'p','status':'investigating','assignedTo':'iris'},
                'c':{'id':'c','investigationOf':'p','status':'working','assignedTo':'iris'}},'workQueue':[]}
        recovery_staffing.step(state)
        self.assertEqual(state['workQueue'],[])
