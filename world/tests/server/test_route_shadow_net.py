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

    def test_agreement_is_logged_with_both_lanes_and_margin(self):
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
            # Margin context (increment 1): top-2 with probabilities and
            # the gap between them.
            self.assertEqual(len(details['netTop']), 2)
            top = details['netTop'][0]
            self.assertEqual(top['lane'], details['netLane'])
            self.assertTrue(0.0 <= top['p'] <= 1.0)
            self.assertGreaterEqual(details['netMargin'], 0.0)

    def test_scores_rank_all_lanes_and_sum_to_one(self):
        import serve
        _fresh_net_state()
        serve._route_net_cache = json.load(open(_NET_PATH))
        ranked = serve._route_net_scores(
            'We researched PostHog, Sentry, LangSmith. Compare each '
            'against OUR think tank.')
        self.assertEqual(len(ranked), len(serve._route_net_cache['lanes']))
        self.assertAlmostEqual(sum(p for _l, p in ranked), 1.0, places=5)
        probs = [p for _l, p in ranked]
        self.assertEqual(probs, sorted(probs, reverse=True))
        self.assertEqual(ranked[0][0], serve._route_net_predict(
            'We researched PostHog, Sentry, LangSmith. Compare each '
            'against OUR think tank.'))

    def test_second_opinion_read_is_compact_top2(self):
        import serve
        _fresh_net_state()
        serve._route_net_cache = json.load(open(_NET_PATH))
        read = serve._route_net_read('build the overnight survey now')
        self.assertTrue(1 <= len(read) <= 2)
        for item in read:
            self.assertIn('lane', item)
            self.assertIn('p', item)
            self.assertIsInstance(item['p'], float)
        # Shadow off -> no read at all (no fields injected downstream).
        serve._route_net_cache = None
        self.assertIsNone(serve._route_net_read('build the overnight '
                                                'survey now'))

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


class RejectionContext(unittest.TestCase):
    """Increment 3: when Jev lands on 'unclear' (its soft rejection) or a
    Theo policy denial fires, the net's top-2 read is logged beside the
    decision. Context only -- never injected into conversations, never
    decisive, and absent entirely when the shadow is off."""

    def setUp(self):
        _fresh_net_state()
        import serve
        serve._route_net_cache = json.load(open(_NET_PATH))

    def tearDown(self):
        _fresh_net_state()

    def _route_once(self, lane, denied=False, text=None):
        import serve
        text = text or 'Fix the outage, set up a nightly check, and how ' \
                       'tall is Everest?'
        state = {'agentRoster': [{'id': 'maya', 'isAdmin': True}],
                 'agents': {'maya': {'id': 'maya', 'offDuty': False,
                                     'busy': False, 'task': None,
                                     'pairWith': None}},
                 'teams': [], 'researchTopics': [], 'workQueue': []}

        async def fake_unclear(s, t, admin_id, session_id=None):
            return {'reply': 'on it'}

        decision = mock.Mock(allowed=not denied, requires_approval=False)
        if denied:
            decision.allowed = False
            decision.reason = 'role worker may not request start_run'
        with mock.patch.object(serve, '_lane_decider', return_value=lane), \
             mock.patch.object(serve, '_admin_agent_id', return_value='maya'), \
             mock.patch.dict(serve._ROUTING_HANDLERS, {lane: fake_unclear}), \
             mock.patch.object(serve._theo_policy, 'evaluate',
                               return_value=decision), \
             mock.patch.object(serve, 'log_action') as log:
            import asyncio
            asyncio.run(serve._route_player_request(state, text))
        return log

    def _classified_details(self, log):
        call = next(c for c in log.call_args_list
                    if c[0][1] == 'route_classified')
        return call[0][2]

    def test_unclear_lane_carries_net_second_opinion(self):
        log = self._route_once('unclear')
        details = self._classified_details(log)
        self.assertEqual(details['lane'], 'unclear')
        self.assertIn('netSecondOpinion', details)
        self.assertTrue(1 <= len(details['netSecondOpinion']) <= 2)
        self.assertIn('p', details['netSecondOpinion'][0])

    def test_clear_lane_does_not_carry_second_opinion(self):
        # Jev answered confidently; the context field would be noise there.
        log = self._route_once('story', text='build the overnight survey')
        details = self._classified_details(log)
        self.assertEqual(details['lane'], 'story')
        self.assertNotIn('netSecondOpinion', details)

    def test_policy_denial_carries_net_second_opinion(self):
        log = self._route_once('spike', denied=True,
                               text='Dig into the sweep one time')
        denied = next(c for c in log.call_args_list
                      if c[0][1] == 'theo_denied')
        details = denied[0][2]
        self.assertEqual(details['lane'], 'spike')
        self.assertIn('netSecondOpinion', details)

    def test_shadow_off_means_no_context_fields(self):
        import serve
        serve._route_net_cache = None
        log = self._route_once('unclear')
        details = self._classified_details(log)
        self.assertNotIn('netSecondOpinion', details)


class ReviewPairing(unittest.TestCase):
    """Increment 2: the pure pairing behind GET /api/route-shadow. A shadow
    row pairs with the nearest route_classified row of the same text inside
    the 10s window; disagreements and unpaired rows come back separately."""

    def _row(self, action, ts, **details):
        return (ts, json.dumps(details) if isinstance(details, dict)
                else details) if action == 'raw' else \
            (action, ts, json.dumps(details))

    def test_pairs_disagreements_and_totals(self):
        import serve
        t0 = 1795020000.0
        rows = [
            (t0 + 2.0, json.dumps({'lane': 'spike', 'text': 'Dig into it '
                                                   'one time'})),
            (t0 + 2.1, json.dumps({'liveLane': 'spike', 'netLane': 'story',
                                   'agree': False, 'netMargin': 0.12,
                                   'netTop': [{'lane': 'story', 'p': 0.44},
                                              {'lane': 'spike', 'p': 0.32}],
                                   'text': 'Dig into it one time'})),
            (t0 + 40.0, json.dumps({'lane': 'story', 'text': 'build it'})),
            (t0 + 40.1, json.dumps({'liveLane': 'story', 'netLane': 'story',
                                    'agree': True, 'netMargin': 0.5,
                                    'netTop': [{'lane': 'story', 'p': 0.7}],
                                    'text': 'build it'})),
        ]
        out = serve._route_shadow_pair(rows, now_ts=t0 + 100.0)
        self.assertEqual(out['totals']['shadow'], 2)
        self.assertEqual(out['totals']['paired'], 2)
        self.assertEqual(out['totals']['agree'], 1)
        self.assertEqual(out['totals']['disagree'], 1)
        self.assertEqual(out['totals']['agreeRate'], 0.5)
        self.assertEqual(len(out['disagreements']), 1)
        d = out['disagreements'][0]
        self.assertEqual(d['liveLane'], 'spike')
        self.assertEqual(d['netLane'], 'story')
        self.assertEqual(d['netTop'][0]['lane'], 'story')
        self.assertEqual(out['unpaired'], [])

    def test_out_of_window_or_absent_twin_is_unpaired_not_miscounted(self):
        import serve
        t0 = 1795020000.0
        rows = [
            # live row from BEFORE the shadow existed (old decision)
            (t0 - 3600.0, json.dumps({'lane': 'story', 'text': 'old ask'})),
            # shadow row far from any twin
            (t0 + 5.0, json.dumps({'liveLane': 'story', 'netLane': 'ask',
                                   'agree': False, 'text': 'old ask'})),
            # shadow row whose live twin was never logged
            (t0 + 6.0, json.dumps({'liveLane': 'ask', 'netLane': 'ask',
                                   'agree': True, 'text': 'ghost ask'})),
        ]
        out = serve._route_shadow_pair(rows, now_ts=t0 + 10.0)
        self.assertEqual(out['totals']['paired'], 0)
        self.assertEqual(out['totals']['agreeRate'], None)
        self.assertEqual(out['totals']['unpaired'], 2)

    def test_duplicate_live_rows_pair_to_nearest(self):
        import serve
        t0 = 1795020000.0
        rows = [
            (t0 + 1.0, json.dumps({'lane': 'story', 'text': 'same text'})),
            (t0 + 3.9, json.dumps({'lane': 'story', 'text': 'same text'})),
            (t0 + 4.0, json.dumps({'liveLane': 'story', 'netLane': 'story',
                                   'agree': True, 'text': 'same text'})),
        ]
        out = serve._route_shadow_pair(rows, now_ts=t0 + 10.0)
        self.assertEqual(out['totals']['paired'], 1)

    def test_corrupt_or_nonshadow_rows_are_skipped(self):
        import serve
        rows = [
            (1795020000.0, '{ truncated'),
            (1795020001.0, json.dumps({'some': 'other action'})),
            (1795020002.0, None),
        ]
        out = serve._route_shadow_pair(rows, now_ts=1795020010.0)
        self.assertEqual(out['totals']['shadow'], 0)
        self.assertTrue(out['windowSeconds'] == 0.0)
