import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import x_ingest  # noqa: E402


class XIngestTest(unittest.TestCase):
    def test_status_id(self):
        self.assertEqual(x_ingest.status_id('https://x.com/a/status/123?s=46'), '123')
        self.assertIsNone(x_ingest.status_id('https://x.com/a'))

    def test_category(self):
        self.assertEqual(x_ingest._category('AI agents', 'An LLM agent workflow'), 'AI & Agents')
        self.assertEqual(x_ingest._category('Bitcoin trading', 'on-chain memecoin'), 'Crypto & Trading')
        self.assertEqual(x_ingest._category('Recipe', 'bread and butter'), 'Other')

    def test_render_article_blocks(self):
        tab = {'url': 'https://x.com/a/status/1', 'title': 'fallback'}
        payload = {'tweet': {
            'author': {'name': 'Author'},
            'created_at': '2026-01-01T00:00:00Z',
            'article': {
                'id': '99', 'title': 'A title',
                'content': {'blocks': [
                    {'type': 'unstyled', 'text': 'First paragraph.'},
                    {'type': 'header-one', 'text': 'Heading'},
                ]},
            },
        }}
        title, preview, md = x_ingest._render(tab, payload, 'AI & Agents')
        self.assertEqual(title, 'A title')
        self.assertIn('## Heading', md)
        self.assertIn('First paragraph.', md)
        self.assertIn('Category: AI & Agents', md)
        self.assertIn('untrusted data', md)
