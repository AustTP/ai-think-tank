"""url_trust: the composite external URL reputation score.

Operator direction (2026-11-01): stop relying FULLY on the Jev classify
gate for URLs -- Jev fails closed and needs high confidence, so ordinary
legitimate URLs stall. These tests pin the pure scorer with every network
signal INJECTED (rdap age, urlhaus hit), so no test touches the network,
plus the band composition rules the gates depend on: blocklist hits are
'bad' regardless of age, young or structurally odd domains are 'suspect',
old + clean + corroborated is 'trusted', missing evidence is 'unknown'
(the gate's cue to ask Jev as before). Degraded signals (None) can only
LOWER confidence, never fabricate a verdict.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import url_trust  # noqa: E402


class Bands(unittest.TestCase):
    def test_blocklist_hit_is_bad_regardless_of_age(self):
        v = url_trust.score_url('https://old-but-evil.example/x',
                                rdap_age_days=3000, urlhaus_hit=True)
        self.assertEqual(v['band'], 'bad')
        self.assertEqual(v['score'], 0)

    def test_virustotal_two_engines_is_bad(self):
        v = url_trust.score_url('https://a.example',
                                rdap_age_days=2000,
                                urlhaus_hit=False)
        # inject vt by monkeypatching the module function
        import unittest.mock as mock
        with mock.patch.object(url_trust, '_virustotal_stats',
                               return_value={'malicious': 2, 'suspicious': 0,
                                             'harmless': 40}):
            v = url_trust.score_url('https://a.example', rdap_age_days=2000,
                                    urlhaus_hit=False)
        self.assertEqual(v['band'], 'bad')

    def test_young_domain_is_suspect(self):
        v = url_trust.score_url('https://fresh.example/login',
                                rdap_age_days=9, urlhaus_hit=False)
        self.assertEqual(v['band'], 'suspect')
        self.assertIn('ageDays', v['signals'])

    def test_old_clean_domain_is_trusted(self):
        v = url_trust.score_url('https://docs.example.org/guide',
                                rdap_age_days=2800, urlhaus_hit=False)
        self.assertEqual(v['band'], 'trusted')

    def test_old_clean_with_virustotal_support_scores_higher(self):
        import unittest.mock as mock
        with mock.patch.object(url_trust, '_virustotal_stats',
                               return_value={'malicious': 0, 'suspicious': 0,
                                             'harmless': 60}):
            v = url_trust.score_url('https://docs.example.org/guide',
                                    rdap_age_days=2800, urlhaus_hit=False)
        self.assertEqual(v['band'], 'trusted')
        self.assertEqual(v['score'], 90)

    def test_no_evidence_at_all_is_unknown(self):
        v = url_trust.score_url('https://mystery.example',
                                rdap_age_days=None, urlhaus_hit=None)
        self.assertEqual(v['band'], 'unknown')

    def test_bad_beats_everything_even_with_flags(self):
        v = url_trust.score_url('https://xn--paypa-2ve.example.login.zip/pay',
                                rdap_age_days=4, urlhaus_hit=True)
        self.assertEqual(v['band'], 'bad')


class Heuristics(unittest.TestCase):
    def test_punycode_host_is_flagged(self):
        flags = url_trust._heuristics('https://xn--paypa-2ve.example/a', 'xn--paypa-2ve.example')
        self.assertTrue(any('punycode' in f for f in flags))

    def test_ip_literal_host_is_flagged(self):
        flags = url_trust._heuristics('http://203.0.113.9/x', '203.0.113.9')
        self.assertTrue(any('ip-literal' in f for f in flags))

    def test_suspicious_tld_is_flagged(self):
        flags = url_trust._heuristics('https://shop.example.zip/x', 'shop.example.zip')
        self.assertTrue(any('.zip' in f for f in flags))

    def test_deep_subdomain_chain_is_flagged(self):
        host = 'a.b.c.d.example.com'
        flags = url_trust._heuristics('https://' + host + '/', host)
        self.assertTrue(any('subdomain chain' in f for f in flags))

    def test_brand_token_on_unrelated_domain_is_flagged(self):
        flags = url_trust._heuristics('https://paypal-verify.example.com/x',
                                      'paypal-verify.example.com')
        self.assertTrue(any('paypal' in f for f in flags))

    def test_real_brand_domain_is_not_flagged(self):
        flags = url_trust._heuristics('https://www.paypal.com/signin', 'www.paypal.com')
        self.assertFalse(any('paypal' in f for f in flags))

    def test_credential_path_word_alone_is_not_flagged(self):
        # A real bank's /login on a structurally clean domain: no flags.
        flags = url_trust._heuristics('https://bank.example/login', 'bank.example')
        self.assertEqual(flags, [])


class Registration(unittest.TestCase):
    def test_registrable_domain_takes_last_two_labels(self):
        self.assertEqual(url_trust.registrable_domain('a.b.c.example.com'),
                         'example.com')
        self.assertEqual(url_trust.registrable_domain('example.com'), 'example.com')
        self.assertEqual(url_trust.registrable_domain('localhost'), '')


if __name__ == '__main__':
    unittest.main()
