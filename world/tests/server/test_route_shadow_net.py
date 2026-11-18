"""Route shadow net (foundry pilot 1): a tiny trained net re-answers each
routed message beside the live Jev quorum classifier. It NEVER decides
routing -- it only logs `route_shadow_classified` with liveLane/netLane/agree
so agreement can be measured passively as real traffic flows. Tests cover the
net artifact shape, spec parity against the full training set, refresh
loading, hook wiring, and the swallow-all failure rules."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

_TANK_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_NET_PATH = os.path.join(_TANK_ROOT, 'library', 'nets', 'route-lane-v1.json')
_DATASET_PATH = os.path.join(_TANK_ROOT, 'library', 'experiments',
                             'route-lane-net', 'dataset.json')


def _fresh_net_state():
    """Reset the shadow's cache so each test starts from a clean load."""
    import serve
    serve._route_net_cache = None
    serve._route_net_loaded_at = 0.0


class NetArtifact(unittest.TestCase):
    """The committed artifact must stay loadable, well-formed, and faithful
    to the training data it was fitted on."""

    def test_artifact_is_well_formed(self):
        net = json.load(open(_NET_PATH))
        self.assertEqual(net['version'], 2)
        self.assertEqual(net['lanes'],
                         ['ask', 'schedule', 'schedule_once', 'spike',
                          'story', 'incident', 'unclear'])
        self.assertEqual(len(net['weights']), net['buckets'])
        self.assertTrue(all(len(row) == len(net['lanes'])
                            for row in net['weights']))
        self.assertEqual(len(net['bias']), len(net['lanes']))
        self.assertIn('md5', net['spec'])
        self.assertIn('200 chars', net['spec'])
        meta = net['meta']
        self.assertEqual(meta['role'],
                         'shadow only -- never decides routing '
                         '(476_route_shadow_net.py)')
        self.assertEqual(meta['cvMode'],
                         'group-aware (duplicate texts never split across '
                         'folds)')
        self.assertEqual(meta['unseenLanes'],
                         ['schedule', 'schedule_once', 'incident'])
        self.assertEqual(meta['uniqueTexts'], 45)

    def test_inference_truncation_parity(self):
        # Spec: the shadow only ever sees the same 200-char prefix the
        # action log stores. Tokens past the cap must not change the
        # prediction (they would be weightless but still inflate the L2
        # norm on the live path).
        import serve
        _fresh_net_state()
        serve._route_net_cache = json.load(open(_NET_PATH))
        head = 'We researched PostHog, Sentry, LangSmith. Compare each ' \
               'against OUR think tank. PostHog: events via Kafka into ' \
               'ClickHouse queried with HogQL.'
        long_text = head + (' tail detail ' * 60)
        self.assertGreater(len(long_text), 200)
        self.assertEqual(serve._route_net_predict(long_text),
                         serve._route_net_predict(long_text[:200]))
        self.assertEqual(serve._route_net_features(long_text),
                         serve._route_net_features(long_text[:200]))
        # Sanity: the truncation actually binds (a head-only prediction is
        # allowed to differ; only full-vs-prefix must agree).
        self.assertLess(len(head), 200)

    def test_serve_side_predict_reproduces_every_training_label(self):
        # The artifact fit the dataset perfectly (fullFitAccuracy 1.0), so the
        # serve-side implementation of the spec must too: any drift in the
        # hashing, normalization, or argmax logic breaks this immediately.
        import serve
        net = json.load(open(_NET_PATH))
        dataset = json.load(open(_DATASET_PATH))
        _fresh_net_state()
        serve._route_net_cache = net
        self.assertEqual(len(dataset), net['meta']['examples'])
        wrong = []
        for row in dataset:
            got = serve._route_net_predict(row['text'])
            if got != row['lane']:
                wrong.append((row['lane'], got, row['text'][:60]))
        self.assertEqual(wrong, [])

    def test_predict_is_deterministic(self):
        import serve
        _fresh_net_state()
        serve._route_net_cache = json.load(open(_NET_PATH))
        first = serve._route_net_predict('build the overnight survey now')
        for _ in range(5):
            self.assertEqual(serve._route_net_predict(
                'build the overnight survey now'), first)


class RefreshLoading(unittest.TestCase):
    def setUp(self):
        _fresh_net_state()

    def tearDown(self):
        _fresh_net_state()

    def test_missing_net_means_shadow_off_no_log(self):
        import serve
        with mock.patch.object(serve, '_ROUTE_NET_PATH',
                               '/nonexistent/route-lane-v1.json'):
            serve._route_net_refresh()
            self.assertIsNone(serve._route_net_cache)
            with mock.patch.object(serve, 'log_action') as log:
                serve._route_shadow_classified('build the thing', 'story')
                log.assert_not_called()

    def test_missing_net_is_retried_after_throttle(self):
        # A net deployed AFTER boot must be picked up without a restart: the
        # failed load is retried once the 60s throttle window has passed.
        import serve
        with mock.patch.object(serve, '_ROUTE_NET_PATH',
                               '/nonexistent/route-lane-v1.json'):
            serve._route_net_refresh()
            self.assertIsNone(serve._route_net_cache)
            serve._route_net_loaded_at -= 61.0  # age the throttle out
        serve._route_net_refresh()
        self.assertIsNotNone(serve._route_net_cache)

    def test_corrupt_net_stays_off(self):
        import serve
        with tempfile.NamedTemporaryFile('w', suffix='.json',
                                         delete=False) as fh:
            fh.write('{ truncated')
            bad = fh.name
        try:
            with mock.patch.object(serve, '_ROUTE_NET_PATH', bad):
                serve._route_net_refresh()
                self.assertIsNone(serve._route_net_cache)
        finally:
            os.unlink(bad)

    def test_malformed_net_missing_lanes_stays_off(self):
        import serve
        with tempfile.NamedTemporaryFile('w', suffix='.json',
                                         delete=False) as fh:
            json.dump({'lanes': ['ask'], 'weights': [], 'bias': []}, fh)
            bad = fh.name
        try:
            with mock.patch.object(serve, '_ROUTE_NET_PATH', bad):
                serve._route_net_refresh()
                self.assertIsNone(serve._route_net_cache)
        finally:
            os.unlink(bad)


class ShadowLogging(unittest.TestCase):
    def setUp(self):
        _fresh_net_state()
        import serve
        serve._route_net_cache = json.load(open(_NET_PATH))

    def tearDown(self):
        _fresh_net_state()

    def test_agreement_is_logged_with_both_lanes(self):
        import serve
        text = 'We researched PostHog, Sentry, LangSmith. Compare each ' \
               'against OUR think tank.'
        with mock.patch.object(serve, 'log_action') as log:
            serve._route_shadow_classified(text, 'spike')
            log.assert_called_once()
            agent, action, details = log.call_args[0][:3]
            self.assertEqual(agent, 'player')
            self.assertEqual(action, 'route_shadow_classified')
            self.assertEqual(details['liveLane'], 'spike')
            self.assertEqual(details['netLane'], details['liveLane'])
            self.assertTrue(details['agree'])
            self.assertLessEqual(len(details['text']), 200)

    def test_disagreement_is_logged_not_hidden(self):
        import serve
        text = 'We researched PostHog, Sentry, LangSmith. Compare each ' \
               'against OUR think tank.'
        with mock.patch.object(serve, 'log_action') as log:
            serve._route_shadow_classified(text, 'story')  # live says story
            log.assert_called_once()
            details = log.call_args[0][2]
            self.assertEqual(details['liveLane'], 'story')
            self.assertNotEqual(details['netLane'], 'story')
            self.assertFalse(details['agree'])

    def test_none_live_lane_logs_as_unclear(self):
        # The caller converts a classifier None to 'unclear' before logging
        # route_classified; the shadow receives the resolved lane either way.
        import serve
        with mock.patch.object(serve, 'log_action') as log:
            serve._route_shadow_classified(
                'We researched PostHog, Sentry, LangSmith. Compare each '
                'against OUR think tank.', None)
            details = log.call_args[0][2]
            self.assertEqual(details['liveLane'], 'unclear')


class HookWiring(unittest.TestCase):
    """The shadow hook sits inside _route_player_request AFTER the live
    classification log, so every real routed message is shadowed exactly
    once."""

    def setUp(self):
        _fresh_net_state()
        import serve
        serve._route_net_cache = json.load(open(_NET_PATH))

    def tearDown(self):
        _fresh_net_state()

    def _route_once(self, text='build the overnight survey now'):
        import serve
        state = {'agentRoster': [{'id': 'maya', 'isAdmin': True}],
                 'agents': {'maya': {'id': 'maya', 'offDuty': False,
                                     'busy': False, 'task': None,
                                     'pairWith': None}},
                 'teams': [], 'researchTopics': [], 'workQueue': []}

        async def fake_unclear(s, t, admin_id, session_id=None):
            return {'reply': 'on it'}

        async def fake_run(state, text_arg, session_id=None):
            pass

        with mock.patch.object(serve, '_lane_decider', return_value='story'), \
             mock.patch.object(serve, '_admin_agent_id', return_value='maya'), \
             mock.patch.dict(serve._ROUTING_HANDLERS,
                             {'story': fake_unclear}), \
             mock.patch.object(serve._theo_policy, 'evaluate') as ev, \
             mock.patch.object(serve, 'log_action') as log:
            ev.return_value = mock.Mock(allowed=True,
                                        requires_approval=False)
            import asyncio
            asyncio.run(serve._route_player_request(state, text))
        return log

    def test_route_player_request_shadows_the_live_lane(self):
        log = self._route_once()
        actions = [c[0][1] for c in log.call_args_list]
        self.assertIn('route_classified', actions)
        self.assertIn('route_shadow_classified', actions)
        shadow = next(c for c in log.call_args_list
                      if c[0][1] == 'route_shadow_classified')
        self.assertEqual(shadow[0][2]['liveLane'], 'story')

    def test_shadow_failure_never_breaks_routing(self):
        # The swallow lives INSIDE _route_shadow_classified: any error in
        # feature hashing, prediction, or logging is invisible to the live
        # route. Simulate a mid-flight crash and assert silence.
        import serve
        with mock.patch.object(serve, '_route_net_predict',
                               side_effect=RuntimeError('boom')), \
             mock.patch.object(serve, 'log_action') as log:
            serve._route_shadow_classified('build the thing', 'story')
            log.assert_not_called()
