"""RFC 6238 TOTP vectors and otpauth parsing."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import totp  # noqa: E402

RFC_SECRET = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'


class RFCVectors(unittest.TestCase):
    def test_sha1_vectors(self):
        vectors = {
            59: '94287082',
            1111111109: '07081804',
            1111111111: '14050471',
            1234567890: '89005924',
            2000000000: '69279037',
            20000000000: '65353130',
        }
        for at, expected in vectors.items():
            self.assertEqual(totp.code(RFC_SECRET, at=at, digits=8), expected)


class Secrets(unittest.TestCase):
    def test_generate_and_normalize(self):
        secret = totp.generate_secret()
        self.assertEqual(totp.normalize_secret(secret.lower()), secret)
        self.assertEqual(totp.normalize_secret(secret + '=='), secret)

    def test_rejects_bad_secret(self):
        with self.assertRaises(ValueError):
            totp.normalize_secret('not base32!')

    def test_verify_window(self):
        now = 1234567890
        secret = 'JBSWY3DPEHPK3PXP'
        current = totp.code(secret, at=now)
        self.assertTrue(totp.verify(secret, current, at=now))
        self.assertTrue(totp.verify(secret, totp.code(secret, at=now - 30), at=now))
        self.assertFalse(totp.verify(secret, current, at=now + 120))


class Otpauth(unittest.TestCase):
    def test_parse(self):
        uri = (f'otpauth://totp/Example:alice@example.com?secret={RFC_SECRET}'
               '&issuer=Example&algorithm=SHA1&digits=6&period=30')
        parsed = totp.parse_otpauth(uri)
        self.assertEqual(parsed['secret'], RFC_SECRET)
        self.assertEqual(parsed['issuer'], 'Example')
        self.assertEqual(parsed['account'], 'alice@example.com')
        self.assertEqual(parsed['digits'], 6)
        self.assertEqual(parsed['period'], 30)

    def test_roundtrip(self):
        uri = totp.provisioning_uri(RFC_SECRET, 'alice@example.com', issuer='Example')
        parsed = totp.parse_otpauth(uri)
        self.assertEqual(parsed['secret'], RFC_SECRET)
        self.assertEqual(parsed['account'], 'alice@example.com')

    def test_otpauth_rejects_unbounded_digits_and_period(self):
        for query in ('digits=999999999', 'period=0', 'period=999999'):
            uri = f'otpauth://totp/Example:alice?secret={RFC_SECRET}&{query}'
            with self.assertRaises(ValueError):
                totp.parse_otpauth(uri)


if __name__ == '__main__':
    unittest.main()
