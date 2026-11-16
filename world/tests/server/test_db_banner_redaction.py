"""The boot banner never prints a live credential.

2026-11-16: the [db] banner printed the raw DSN, so the Postgres password
landed in journald on every boot (and in every boot log anyone pastes).
The banner's job is saying WHICH database a process uses; the redactor
keeps user and host, masks the secret.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import serve  # noqa: E402


class DsnRedaction(unittest.TestCase):
    def test_password_masked_user_and_host_kept(self):
        self.assertEqual(
            serve._redact_dsn(
                'postgres://avnadmin:cbc39753c78d8b867f31f3dcb6df0172'
                '@pg-1f751108-think-tank.l.aivencloud.com:17684/defaultdb'
                '?sslmode=require'),
            'postgres://avnadmin:***'
            '@pg-1f751108-think-tank.l.aivencloud.com:17684/defaultdb'
            '?sslmode=require')

    def test_password_containing_at_sign_still_masks_everything(self):
        # rpartition('@'): an '@' inside the password must not split it.
        self.assertEqual(
            serve._redact_dsn('postgres://bob:p@ss@db.example:5432/x'),
            'postgres://bob:***@db.example:5432/x')

    def test_no_credentials_passes_through(self):
        self.assertEqual(serve._redact_dsn('postgres://db.example:5432/x'),
                         'postgres://db.example:5432/x')
        self.assertEqual(serve._redact_dsn('not a url'), 'not a url')

    def test_userless_dsn_masks_host_side_only(self):
        # ':pass@host' with an empty user still loses the secret.
        self.assertEqual(serve._redact_dsn('postgres://:secret@db/x'),
                         'postgres://:***@db/x')


if __name__ == '__main__':
    unittest.main()
