import json
import os
import sys
import unittest
import tempfile
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve
import content
import sim

class RepairEvidence(unittest.TestCase):
    def test_only_anchored_in_scope_repairs_survive(self):
        story = {'title':'Save dataset', 'criterion':'Save historical data in Library',
                 'evidenceQuote':'Data was downloaded.', 'instructions':'Save CSV and validation results.',
                 'acceptanceCriteria':'CSV path, row count and date coverage are available.'}
        output = json.dumps({'verdict':'send_back','repairStories':[story]})
        self.assertEqual(len(content._document_review_repairs(output,'Data was downloaded.','Save historical data in Library')),1)
        self.assertEqual(content._document_review_repairs(output,'A different report.','Save historical data in Library'),[])
        self.assertEqual(content._document_review_repairs(output,'Data was downloaded.','Research weather'),[])

    def test_malformed_stories_and_prose_cannot_become_repairs(self):
        for text in ['reject this', json.dumps({'verdict':'send_back','repairStories':{}}),
                     json.dumps({'verdict':'send_back','repairStories':[{'title':'Vague fix'}]})]:
            self.assertEqual(content._document_review_repairs(text,'report','requirements'),[])

    def test_repair_stories_become_one_owned_executable_correction(self):
        story = {'title':'Save CSV', 'criterion':'Save data', 'evidenceQuote':'Downloaded data.',
                 'instructions':'Write the fetched rows to Library CSV.', 'acceptanceCriteria':'CSV has dated rows and source URL.'}
        parent = {'id':'p','title':'Historical data','assignedTo':'ada','room':'observatory','taskType':'research',
                  '_peerGate':{},'peerReviews':[{'verdict':'actionable','repairStories':[story]}]}
        state = {'agents':{'ada':{}},'tasks':{'p':parent},'workQueue':[]}
        sim._queue_peer_repair(state,parent,'Missing saved data')
        sim._queue_peer_repair(state,parent,'Missing saved data')
        self.assertEqual(len(state['workQueue']),1)
        repair=state['workQueue'][0]
        self.assertEqual(repair['assignedTo'],'ada')
        self.assertEqual(repair['reviewOf'],'p')
        self.assertIn('CSV has dated rows',repair['instructions'])
        self.assertEqual(parent['repairStories'][0]['title'],story['title'])
        self.assertEqual(parent['repairStories'][0]['owner'],'ada')

    def test_review_transport_error_is_preserved_not_called_bad_json(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root,'report.md')
            with open(path,'w') as f: f.write('Report evidence.')
            with patch.object(serve,'_safe_library_path',return_value=path), \
                 patch.object(serve,'get_or_create_agent_key',return_value='test'), \
                 patch.object(serve,'_http_json',return_value={'error':'request failed: timed out'}) as request, \
                 patch.object(sim,'_store_content_result') as store:
                content._run_document_review({},'reviewer',{'id':'r'},{'libraryPath':'report.md'})
            result = store.call_args.args[1]
            self.assertIn('timed out',result['note'])
            self.assertTrue(result['reviewFormatError'])
            self.assertEqual(request.call_count,1)
            self.assertEqual(request.call_args.kwargs['timeout'],120)
            self.assertFalse(request.call_args.args[3]['deliberate'])
