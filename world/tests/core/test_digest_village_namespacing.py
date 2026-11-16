"""Ceremony digests follow the scrum master's village.

The refinement and escalation digests are village-scoped ceremonies: they
belong to the scrum master's village, so a non-main scrum master's digest
lands under villages/<vid>/... and never surfaces in another village's
library search or RAG retrieval. Main and unknown owners keep the flat
legacy path. The weekly social digest is deliberately exempt: it is a
cross-team commons event.
"""
import glob
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402


class DigestVillageNamespacing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think-tank-digests-')
        self._patch = unittest.mock.patch.object(
            serve, 'LIBRARY_DIR', os.path.join(self._tmp, 'library'))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _state(self):
        return {'agents': {'sm-w': {'id': 'sm-w', 'villageId': 'winter'},
                           'sm-m': {'id': 'sm-m', 'villageId': 'main'}}}

    def _glob(self, rel):
        return glob.glob(os.path.join(serve.LIBRARY_DIR, rel))

    def test_winter_ceremony_digests_land_in_their_village(self):
        state = self._state()
        serve.log_refinement_digest(state, {}, {
            'scrumMasterId': 'sm-w', 'accepted': []})
        serve.log_escalation_digest(state, {'scrumMasterId': 'sm-w'}, 'story')
        self.assertTrue(self._glob('villages/winter/refinement/refinement-*.md'))
        self.assertTrue(self._glob('villages/winter/escalations/escalation-*.md'))
        # Nothing leaked into main's flat trees.
        self.assertFalse(self._glob('refinement/refinement-*.md'))
        self.assertFalse(self._glob('escalations/escalation-*.md'))

    def test_main_and_unknown_owners_keep_the_flat_legacy_path(self):
        state = self._state()
        serve.log_refinement_digest(state, {}, {
            'scrumMasterId': 'sm-m', 'accepted': []})
        serve.log_escalation_digest(state, {}, 'story')
        self.assertTrue(self._glob('refinement/refinement-*.md'))
        self.assertTrue(self._glob('escalations/escalation-*.md'))
        self.assertFalse(glob.glob(
            os.path.join(serve.LIBRARY_DIR, 'villages', '**', '*.md'),
            recursive=True))

    def test_social_digest_stays_flat(self):
        state = self._state()
        people = {'sm-w': {'id': 'sm-w', 'name': 'Winter SM',
                           'weekApprovals': 1}}
        serve.log_social_digest(state, people,
                                [{'agentId': 'sm-w', 'choice': 'adopt',
                                  'confidence': 0.9}])
        self.assertTrue(self._glob('social/social-*.md'))


if __name__ == '__main__':
    unittest.main()
