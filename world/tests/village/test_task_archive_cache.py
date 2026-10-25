import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import serve


class TaskArchiveCacheIsolation(unittest.TestCase):
    def test_state_mutation_does_not_mutate_the_archive_cache(self):
        archived = {'t': {'id': 't', 'status': 'done', 'title': 'old'}}
        cache = {'db': serve.DB_PATH, 'loaded': True, 'tasks': archived}
        with patch.object(serve, '_TASK_ARCHIVE_CACHE', cache):
            data = {'tasks': {}}
            serve._merge_archived_tasks(data)
            data['tasks']['t']['rootStoryId'] = 'root'
            data['tasks']['t']['storySpendUsd'] = 1.0
            self.assertNotIn('rootStoryId', archived['t'])
            self.assertNotIn('storySpendUsd', archived['t'])
