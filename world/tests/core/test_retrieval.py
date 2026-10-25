import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.retrieval as retrieval  # noqa: E402


class RetrievalTest(unittest.TestCase):
    def test_chunks_cover_text(self):
        text = 'intro\n\n' + ('middle line\n' * 200) + '\n\npricing conclusion'
        chunks = retrieval.chunk_text(text, max_chars=400, overlap=40)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 400 for c in chunks))
        self.assertIn('pricing', '\n'.join(chunks))

    def test_rank_finds_relevant_chunk(self):
        text = ('alpha ' * 300) + '\n\nPricing and revenue details.\n\n' + ('beta ' * 300)
        out = retrieval.excerpts(text, 'pricing revenue', top_k=1, max_chars=300)
        self.assertIn('Pricing and revenue', out)

    def test_empty_query_returns_nothing(self):
        self.assertEqual(retrieval.excerpts('abc', ''), '')
