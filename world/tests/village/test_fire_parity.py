"""The fire/keep two-provider parity decider (060): agreement decides,
disagreement escalates without acting, a failed second call degrades to the
single verdict with a logged marker."""
import unittest
import unittest.mock

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve
import sim
from test_sim_governance import SimIsolation


class FireParityTests(SimIsolation):
    _CANDIDATES = [{'id': 'fire', 'description': 'end it'},
                   {'id': 'keep', 'description': 'keep them'}]

    def test_agreement_decides(self):
        with unittest.mock.patch.object(sim, '_governance_decider', return_value='keep'), \
             unittest.mock.patch.object(serve, '_parity_second_model', return_value='z-ai/glm-5.3-flash'), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync', return_value={}), \
             unittest.mock.patch.object(serve, '_jev_choice', return_value=('keep', None, None)), \
             unittest.mock.patch.object(serve, 'create_escalation') as esc:
            self.assertEqual(sim._fire_parity_decider({}, 'instructions', self._CANDIDATES), 'keep')
        esc.assert_not_called()

    def test_disagreement_escalates_and_blocks(self):
        with unittest.mock.patch.object(sim, '_governance_decider', return_value='fire'), \
             unittest.mock.patch.object(serve, '_parity_second_model', return_value='z-ai/glm-5.3-flash'), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync', return_value={}), \
             unittest.mock.patch.object(serve, '_jev_choice', return_value=('keep', None, None)), \
             unittest.mock.patch.object(serve, 'create_escalation') as esc:
            self.assertIsNone(sim._fire_parity_decider({}, 'instructions', self._CANDIDATES))
        self.assertEqual(esc.call_args[0][0], 'owner safety decision')
        self.assertIn('DISAGREED', esc.call_args[0][1])

    def test_second_unavailable_degrades_to_single(self):
        with unittest.mock.patch.object(sim, '_governance_decider', return_value='keep'), \
             unittest.mock.patch.object(serve, '_parity_second_model',
                                        return_value='z-ai/glm-5.3-flash'), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        side_effect=RuntimeError('down')), \
             unittest.mock.patch.object(serve, 'create_escalation') as esc:
            self.assertEqual(sim._fire_parity_decider({}, 'instructions', self._CANDIDATES), 'keep')
        esc.assert_not_called()

    def test_no_second_model_degrades_to_single(self):
        with unittest.mock.patch.object(sim, '_governance_decider', return_value='fire'), \
             unittest.mock.patch.object(serve, '_parity_second_model', return_value=None):
            self.assertEqual(sim._fire_parity_decider({}, 'instructions', self._CANDIDATES), 'fire')

    def test_second_out_of_domain_degrades(self):
        with unittest.mock.patch.object(sim, '_governance_decider', return_value='keep'), \
             unittest.mock.patch.object(serve, '_parity_second_model', return_value='x/y'), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync', return_value={}), \
             unittest.mock.patch.object(serve, '_jev_choice', return_value=('nonsense', None, None)):
            self.assertEqual(sim._fire_parity_decider({}, 'instructions', self._CANDIDATES), 'keep')


if __name__ == '__main__':
    unittest.main()
