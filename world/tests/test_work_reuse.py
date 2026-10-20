import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import work_reuse


class WorkReuse(unittest.TestCase):
    def test_tree_digest_changes_with_content_and_cache_reuses_only_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'module.py')
            with open(path, 'w') as stream:
                stream.write('x = 1\n')
            steps = [{'name': 'flake8', 'command': 'flake8'}]
            key = work_reuse.check_key(directory, steps, 'v1')
            result = {'ok': True, 'failedStep': None, 'results': [], 'note': 'green'}
            work_reuse.save_check(key, result)
            cached = work_reuse.get_check(key)
            self.assertEqual(cached['note'], 'green')
            self.assertTrue(cached['cached'])
            work_reuse.save_check(key, {'ok': False, 'note': 'red'})
            with open(path, 'w') as stream:
                stream.write('x = 2\n')
            new_key = work_reuse.check_key(directory, steps, 'v1')
            self.assertNotEqual(new_key, key)
            self.assertIsNone(work_reuse.get_check(new_key))
            self.assertEqual(work_reuse.get_check(key)['note'], 'green')
