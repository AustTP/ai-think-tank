import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve
import sim
import review_quality as quality

class QualityEvidence(unittest.TestCase):
    def test_reference_ids_resolve_exact_quotes_and_reject_invented_ids(self):
        import content
        refs={'criteria':{'C2':'List pros and cons.'},'evidence':{'E4':'Cons were not evaluated.'}}
        story={'title':'Complete comparison','criterionId':'C2','evidenceId':'E4',
               'instructions':'Document the omitted disadvantages.','acceptanceCriteria':'List pros and cons.'}
        raw=json.dumps({'verdict':'send_back','repairStories':[story]})
        repairs=content._document_review_repairs(raw,'Cons were not evaluated.','List pros and cons.',refs)
        self.assertEqual(repairs[0]['criterion'],'List pros and cons.')
        self.assertEqual(repairs[0]['evidenceQuote'],'Cons were not evaluated.')
        story['evidenceId']='E999'
        raw=json.dumps({'verdict':'send_back','repairStories':[story]})
        self.assertEqual(content._document_review_repairs(raw,'Cons were not evaluated.','List pros and cons.',refs),[])

    def test_api_documentation_is_not_a_historical_dataset(self):
        with tempfile.TemporaryDirectory() as root:
            path=os.path.join(root,'endpoints.csv')
            with open(path,'w') as f: f.write('endpoint,parameter\n/candles,start\n')
            contract={'instructions':'Overarching player request: collect historical data\nStory: Research Coinbase API documentation for collecting historical data',
                      'acceptanceCriteria':'List endpoints and parameters for collecting historical data'}
            result=quality.artifact_checks('endpoints.csv',contract,lambda p:path)
            self.assertTrue(result['passed'],result['errors'])

    def test_documentation_does_not_excuse_explicit_dataset_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            path=os.path.join(root,'endpoints.csv')
            with open(path,'w') as f: f.write('endpoint,parameter\n/candles,start\n')
            result=quality.artifact_checks('endpoints.csv',{'instructions':'Research API endpoints and download historical data',
                'acceptanceCriteria':'Save dataset'},lambda p:path)
            self.assertFalse(result['passed'])
            self.assertFalse(any('every row' in e for e in result['errors']))

    def test_contract_removes_feedback_and_keeps_explicit_criteria(self):
        task={'instructions':'Fetch Coinbase historical data.\nPeer review of other work: research weather',
              'acceptanceCriteria':'Save a CSV.'}
        contract=quality.contract_for(task)
        self.assertNotIn('weather',contract['instructions'])
        task['reviewContract']=contract
        task['instructions']='A later reviewer added unrelated requirements.'
        self.assertEqual(quality.contract_for(task),contract)

    def test_historical_csv_checks_real_rows_years_and_gaps(self):
        with tempfile.TemporaryDirectory() as root:
            path=os.path.join(root,'data.csv')
            with open(path,'w') as f:f.write('date,close\n2024-01-01,10\n2024-01-02,11\n2024-01-03,12\n2024-01-10,13\n2024-01-10,13\n')
            result=quality.artifact_checks('Saved data.csv',{'instructions':'Historical data for 2024 and 2025','acceptanceCriteria':'Save dataset'},lambda p:path)
            self.assertFalse(result['passed'])
            self.assertEqual(result['checks'][0]['rows'],5)
            self.assertEqual(result['checks'][0]['duplicateRows'],1)
            self.assertEqual(result['checks'][0]['intervalGaps'],1)
            self.assertTrue(any('2025' in error for error in result['errors']))

    def test_claims_and_provider_tables_do_not_prove_saved_historical_data(self):
        result=quality.artifact_checks('I downloaded and validated historical data.',
            {'instructions':'Fetch historical data','acceptanceCriteria':'Save data'},lambda p:None)
        self.assertFalse(result['passed'])

    def test_notebook_outputs_are_not_independent_execution_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            path=os.path.join(root,'result.ipynb')
            with open(path,'w') as f:json.dump({'cells':[{'cell_type':'code','source':['print(1)'],
                                                         'execution_count':1,'outputs':[]}]},f)
            result=quality.artifact_checks('result.ipynb',{'instructions':'Execute notebook','acceptanceCriteria':'Run cells'},lambda p:path)
            self.assertFalse(result['passed'])
            self.assertTrue(any('recorded execution' in e for e in result['errors']))

    def test_service_failure_keeps_diagnostics_and_separate_counter(self):
        parent={'id':'p','status':'needs_review','_peerGate':{'formatFailures':1}}
        task={'id':'r','reviewOf':'p','assignedTo':'quinn','taskType':'review'}
        state={'tasks':{'p':parent,'r':task},'agents':{},'workQueue':[]}
        diagnostic={'model':'test','attempts':[{'apiError':'timed out','response':''}]}
        with patch.object(sim,'_queue_player_email'):
            sim._apply_content_result(state,task,{'ok':True,'reviewFormatError':True,
                'reviewFailureKind':'service_unavailable','reviewDiagnostics':diagnostic,'note':'timed out'},123)
        self.assertEqual(task['reviewDiagnostics'],diagnostic)
        self.assertEqual(parent['_peerGate']['formatFailures'],1)
        self.assertEqual(parent['_peerGate']['serviceFailures'],1)
        self.assertEqual(parent['lastReviewDiagnosticsTask'],'r')
