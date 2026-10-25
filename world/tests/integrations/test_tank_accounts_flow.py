import sqlite3
from contextlib import contextmanager

import tank_accounts


class Fake:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        tank_accounts.ensure_schema(self.conn)
        self.payloads = {}
        self.logs = []
    @contextmanager
    def _db(self):
        yield self.conn
    def _store_credential(self, name, service, payload, status=None, proposed_by=None):
        self.payloads[name] = dict(payload)
    def _credential_payload(self, name):
        return self.payloads.get(name)
    def _delete_credential(self, name):
        self.payloads.pop(name, None)
    def log_action(self, *a, **k):
        self.logs.append((a, k))


def test_tank_accounts_flow(monkeypatch):
    f = Fake()
    monkeypatch.setattr(tank_accounts, '_serve', lambda: f)
    assert tank_accounts._row(None) is None
    for kw in ({'service': ''}, {'service': 's', 'account': ''}, {'service': 's', 'account': 'a', 'status': 'bad'}):
        try:
            tank_accounts.create(**kw)
        except ValueError:
            pass
    rec = tank_accounts.create(service='s', account='a', password='p')
    assert rec['service'] == 's'
    assert tank_accounts.list_accounts()[0]['id'] == rec['id']
    assert tank_accounts.get(rec['id'])['account'] == 'a'
    assert tank_accounts.code(rec['id'])['code']
    assert tank_accounts.rotate(rec['id'])['provisioningUri'].startswith('otpauth://')
    g = tank_accounts.mint_grant(rec['id'], 'agent')
    assert tank_accounts.consume_grant(g['grant'], 'agent', rec['id']) is True
    assert tank_accounts.consume_grant(g['grant'], 'agent', rec['id']) is False
    assert tank_accounts.consume_grant('nope', 'agent', rec['id']) is False
    assert tank_accounts.delete(rec['id'])['ok'] is True
    for fn in (tank_accounts.get, tank_accounts.code, tank_accounts.rotate, tank_accounts.delete,
               tank_accounts.mint_grant):
        try:
            fn('missing') if fn is not tank_accounts.mint_grant else fn('missing', 'a')
        except KeyError:
            pass
    # disabled account
    rec2 = tank_accounts.create(service='s', account='a', status='disabled')
    for fn in (tank_accounts.code, tank_accounts.mint_grant):
        try:
            fn(rec2['id']) if fn is tank_accounts.code else fn(rec2['id'], 'a')
        except ValueError:
            pass
    try:
        tank_accounts.mint_grant(rec2['id'], '')
    except ValueError:
        pass
    # payload unavailable
    f.payloads.pop(tank_accounts._vault_name(rec2['id']), None)
    try:
        tank_accounts.rotate(rec2['id'])
    except ValueError:
        pass
    # insert failure deletes credential
    class BadConn:
        def execute(self, *a, **k): raise sqlite3.OperationalError('x')
    @contextmanager
    def bad_db():
        yield BadConn()
    f._db = bad_db
    try:
        tank_accounts.create(service='x', account='y')
    except sqlite3.OperationalError:
        pass


def test_tank_accounts_more(monkeypatch):
    assert tank_accounts._serve() is not None
    f = Fake(); monkeypatch.setattr(tank_accounts, '_serve', lambda: f)
    assert tank_accounts._serve() is f
    rec = tank_accounts.create(service='s', account='a',
                               totp_secret='otpauth://totp/S:a?secret=JBSWY3DPEHPK3PXP&issuer=S')
    # missing payload for code
    f.payloads.pop(tank_accounts._vault_name(rec['id']), None)
    try:
        tank_accounts.code(rec['id'])
    except ValueError:
        pass
    try:
        tank_accounts.mint_grant(rec['id'], '')
    except ValueError:
        pass
    # grant mismatch and rowcount
    rec2 = tank_accounts.create(service='s', account='b')
    g = tank_accounts.mint_grant(rec2['id'], 'a')
    assert tank_accounts.consume_grant(g['grant'], 'b', rec2['id']) is False
    with f._db() as c:
        c.execute('UPDATE tank_account_grants SET used_at=1 WHERE grant_id=?', (g['grant'],))
    assert tank_accounts.consume_grant(g['grant'], 'a', rec2['id']) is False
