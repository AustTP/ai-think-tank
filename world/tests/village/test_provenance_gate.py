"""Provenance gate for plain research cards (task-2208 class).

A research card completed 112 seconds after assignment with a 12-source
"Verified" report written from model memory -- zero fetches before the write,
the file only ever living in the agent's sandbox. The old gate accepted it on
a non-empty note alone. The gate now requires a trusted-Library artifact that
cites a URL actually fetched in-task, with an explicit escape for honest
inability (task-2193's credential-refusal report must keep completing).

Hermetic: DB + library redirected to a throwaway temp dir (the pattern every
other sim test uses) so log_action writes and file checks are harmless.
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve
import sim


def _research_task(**over):
    task = {
        'id': 'task-9001',
        'title': 'research card',
        'taskType': 'research',
        'assignedTo': 'tester',
        'createdAt': int(time.time() * 1000) - 120000,
        'note': 'Spiked "research card": see the Library entry.',
        'status': 'working',
    }
    task.update(over)
    return task


class ProvenanceGateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think-tank-provenance-test-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()
        os.makedirs(os.path.join(self.tmp, 'library', 'reports'), exist_ok=True)

    def tearDown(self):
        self._cm.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def log_fetch(self, action='browse', url='https://example.test/source'):
        conn = sqlite3.connect(serve.DB_PATH)
        conn.execute(
            'INSERT INTO action_log (ts, agent_id, action, details) VALUES (?,?,?,?)',
            (int(time.time()), 'tester', action,
             f'{{"url": "{url}", "purpose": "research"}}'))
        conn.commit()
        conn.close()

    def write_artifact(self, rel, text):
        path = os.path.join(serve.LIBRARY_DIR, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)
        return rel


class TestProvenanceGate(ProvenanceGateBase):
    def test_note_only_research_card_refused(self):
        """The task-2208 hole: note-only completion with no artifact at all."""
        task = _research_task()
        self.assertFalse(sim._completion_has_evidence({}, task))
        gap = sim._completion_evidence_gap(task)
        self.assertIn('no Library artifact', gap)

    def test_artifact_citing_fetched_url_passes(self):
        rel = self.write_artifact(
            'reports/survey.md',
            '# Survey\nSource: https://example.test/source says the plan is funded.\n')
        self.log_fetch('browse', 'https://example.test/source')
        task = _research_task(libraryPath=rel)
        self.assertTrue(sim._completion_has_evidence({}, task))

    def test_artifact_with_unfetched_citations_refused(self):
        """Fetches happened, but the report cites none of them: the report is
        not anchored to the work that produced it."""
        rel = self.write_artifact(
            'reports/survey.md',
            '# Survey\n1. Source: Transit Weekly -- Basis: Verified. '
            '2. Source: City planning blog -- Basis: Verified.\n')
        self.log_fetch('browse', 'https://example.test/source')
        task = _research_task(libraryPath=rel)
        self.assertFalse(sim._completion_has_evidence({}, task))
        gap = sim._completion_evidence_gap(task)
        self.assertIn('cites no source', gap)

    def test_zero_fetch_research_refused(self):
        """No artifact AND no fetch events at all."""
        rel = self.write_artifact('reports/empty.md', 'Nothing to report.\n')
        task = _research_task(libraryPath=rel)
        self.assertFalse(sim._completion_has_evidence({}, task))
        gap = sim._completion_evidence_gap(task)
        self.assertIn('no fetch evidence', gap)

    def test_honest_refusal_artifact_passes_without_fetches(self):
        """The task-2193 escape: the artifact records WHY nothing was fetched."""
        rel = self.write_artifact(
            'reports/refusal.md',
            '# Sweep result\nAnswer: unknown. api_call was refused: '
            'credential tomtom_api_key is not active (status: removed). '
            'Needs player approval.\n')
        task = _research_task(libraryPath=rel)
        self.assertTrue(sim._completion_has_evidence({}, task))

    def test_search_web_counts_as_fetch(self):
        rel = self.write_artifact(
            'reports/survey.md',
            '# Survey\nPer https://example.test/source the corridor opens in 2027.\n')
        self.log_fetch('search_web', 'https://example.test/source')
        task = _research_task(libraryPath=rel)
        self.assertTrue(sim._completion_has_evidence({}, task))

    def test_gate_env_disable(self):
        rel = self.write_artifact('reports/whatever.md', 'claims without sources')
        task = _research_task()
        with unittest.mock.patch.object(serve, '_load_env',
                                        return_value={'COMPLETION_PROVENANCE_GATE': '0'}):
            self.assertTrue(sim._completion_has_evidence({}, task))

    def test_distill_and_standing_research_keep_note_contract(self):
        """Distill (wiki) and standing research-topic cards keep the old
        note-only contract -- the gate narrows to plain research cards."""
        distill = _research_task(distill={'topic': 'x'})
        self.assertTrue(sim._completion_has_evidence({}, distill))
        standing = _research_task(research={'topicId': 't1'})
        self.assertTrue(sim._completion_has_evidence({}, standing))

    def test_wiki_page_still_counts(self):
        task = _research_task(wikiPage='wiki/some-page')
        self.assertTrue(sim._completion_has_evidence({}, task))


if __name__ == '__main__':
    unittest.main()
