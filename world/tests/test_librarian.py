import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)))
import librarian


class LibrarianTests(unittest.TestCase):
    def test_prior_work_handoff_distinguishes_help_from_possible_fulfillment(self):
        with tempfile.TemporaryDirectory() as root:
            for name,text in [('risk.csv','crypto risk management source evidence'),('weather.md','rain and sunshine')]:
                with open(os.path.join(root,name),'w',encoding='utf-8') as stream: stream.write(text)
            task={'title':'Research crypto risk management','acceptanceCriteria':'Descriptions and references'}
            state={'tasks':{'old':{'id':'old','status':'done','libraryPath':'risk.csv',
                                  'acceptanceCriteria':'Descriptions and references'}}}
            librarian.attach_reuse(state,task,SimpleNamespace(LIBRARY_DIR=root))
            docs=task['libraryReuse']['documents']
            self.assertEqual([d['path'] for d in docs],['risk.csv'])
            self.assertEqual(docs[0]['assessment'],'potentially_fulfills')
            self.assertTrue(docs[0]['requiresVerification'])
            self.assertNotIn('status',task)
            state['tasks']['old']['status']='needs_review'
            self.assertEqual(librarian.relevant_documents(state,task,SimpleNamespace(LIBRARY_DIR=root))[0]['assessment'],'may_help')

    def test_role_idempotent_and_excluded_from_workers(self):
        import task_master
        state = {}
        librarian.create_role(state); librarian.create_role(state)
        self.assertEqual(len(state['agentRoster']),1)
        self.assertEqual(task_master.workers(state),[])

    def test_exact_labeled_table_saved_with_provenance_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            def resolve(path):
                target = os.path.abspath(os.path.join(root,path))
                return target if os.path.commonpath([root,target]) == root else None
            serve = SimpleNamespace(_safe_library_path=resolve,log_action=lambda *a,**k:None)
            report = '# CSV File: crypto_strategies.csv\n\n|name|basis|\n|---|---|\n|Scalping|Estimated|\n'
            with open(resolve('report.md'),'w',encoding='utf-8') as f: f.write(report)
            task = {'id':'task-1','libraryPath':'report.md'}
            result = librarian.inspect_report(task,serve)
            self.assertEqual(result['saved'],['crypto_strategies.csv'])
            with open(resolve('crypto_strategies.csv'),encoding='utf-8') as f:
                self.assertEqual(f.read(),'name,basis\nScalping,Estimated\n')
            with open(resolve('crypto_strategies.csv.provenance.json')) as f:
                self.assertIn('unverified',json.load(f)['method'])
            self.assertEqual(librarian.inspect_report(task,serve)['saved'],[])
            self.assertNotIn('status',task)

    def test_missing_contents_are_not_invented(self):
        self.assertIsNone(librarian.embedded_csv('Saved missing.csv\n\nNo actual data.','missing.csv'))
        self.assertIsNone(librarian.embedded_csv('Other.csv\n\n|a|b|\n|---|---|\n|1|2|','missing.csv'))

    def test_fenced_csv_and_malformed_table(self):
        self.assertEqual(librarian.embedded_csv('## data.csv\n```csv\na,b\n1,2\n```','data.csv'),'a,b\r\n1,2\r\n')
        self.assertIsNone(librarian.embedded_csv('## data.csv\n|a|b|\n|---|---|\n|1|','data.csv'))

    def test_plain_fence_after_explicit_filename_is_saved(self):
        report = 'Full contents of `project_status.csv`:\n```\ntask,status\n1,Estimated\n```'
        self.assertEqual(librarian.embedded_csv(report, 'project_status.csv'),
                         'task,status\r\n1,Estimated\r\n')
        self.assertIsNone(librarian.embedded_csv(report.replace('```\n', '```python\n', 1),
                                               'project_status.csv'))


if __name__ == '__main__': unittest.main()
