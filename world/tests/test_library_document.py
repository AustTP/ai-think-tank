import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve


class FullLibraryDocument(unittest.TestCase):
    def test_full_reader_preserves_document_past_agent_context_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, 'long.md')
            content = '# Research\n' + 'x' * 210_000 + '\nEND OF DOCUMENT'
            with open(target, 'w') as f:
                f.write(content)
            with patch.object(serve, '_require_player_session', return_value=True), \
                    patch.object(serve, '_safe_library_path', return_value=target), \
                    patch.object(serve, 'record_library_read'):
                response = asyncio.run(serve.read_library_document(Mock(), 'long.md'))
                self.assertEqual(json.loads(response.body)['content'], content)
                bounded = asyncio.run(serve.read_library_file('long.md'))
                self.assertEqual(len(json.loads(bounded.body)['content']), 200_000)

    def test_authentication_and_missing_document(self):
        with patch.object(serve, '_require_player_session', return_value=False):
            self.assertEqual(asyncio.run(serve.read_library_document(Mock(), 'secret.md')).status_code, 401)
        with patch.object(serve, '_require_player_session', return_value=True), \
                patch.object(serve, '_safe_library_path', return_value=None):
            self.assertEqual(asyncio.run(serve.read_library_document(Mock(), '../escape')).status_code, 404)
