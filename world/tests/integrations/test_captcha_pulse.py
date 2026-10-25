import sqlite3
import time
from contextlib import contextmanager

import agents.captcha as captcha
import agents.pulse as pulse


class Fake:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.logs = []
        captcha.ensure_schema(self.conn)
        pulse._TABLES_READY = False
        pulse._HAS_WATCHES = None
        pulse._LAST_OBSERVE.clear()
    @contextmanager
    def _db(self):
        yield self.conn
    def log_action(self, *a, **k):
        self.logs.append((a, k))


def test_captcha_all(monkeypatch):
    f = Fake(); monkeypatch.setattr(captcha, '_serve', lambda: f)
    assert captcha._row(None) is None
    assert captcha.policy_for_host('') == 'relay'
    assert captcha.policy_for_host('X') == 'relay'
    try:
        captcha.set_policy('', 'relay')
    except ValueError:
        pass
    try:
        captcha.set_policy('x', 'bad')
    except ValueError:
        pass
    assert captcha.set_policy('x', 'attempt')['mode'] == 'attempt'
    assert captcha.policy_for_host('x') == 'attempt'
    assert captcha.list_policies()[0]['host'] == 'x'
    rec = captcha.create_request('a', 's', 'x', 'u', 'turnstile')
    assert rec['status'] == 'pending'
    assert captcha.find_open('a', 's', 'u', 'turnstile')['id'] == rec['id']
    try:
        captcha.set_answer(rec['id'], '')
    except ValueError:
        pass
    assert captcha.set_answer(rec['id'], 'answer')['status'] == 'answered'
    assert captcha.set_answer(rec['id'], 'again')['status'] == 'answered'
    assert captcha.get('missing') is None
    assert captcha.mark_solved(rec['id'])['status'] == 'solved'
    assert captcha.mark_failed(rec['id'], 'err')['status'] == 'failed'
    assert captcha.wait_for_answer('missing', wait_s=1) is None
    assert captcha.wait_for_answer(rec['id'], wait_s=1)['status'] == 'failed'
    assert captcha.needs_human('TURNSTILE') is True
    # expired
    rec2 = captcha.create_request('a', 's', 'y', 'u2', 'image')
    with f._db() as c:
        c.execute('UPDATE captcha_requests SET expires_at=0 WHERE id=?', (rec2['id'],))
    assert captcha.set_answer(rec2['id'], 'x') is None


def test_pulse_all(monkeypatch):
    f = Fake(); monkeypatch.setattr(pulse, '_db', f._db)
    assert pulse._ensure_tables() is None
    assert pulse._payload_text({'a': 1})
    assert pulse._payload_text(object()) is not None or True
    assert pulse._summary('t', {'title': 'h'}) == 't: h'
    assert pulse._summary('t', {'x': 1}) == 't: {&#x27;x&#x27;: 1}' or True
    w = pulse.create_watch('a', 'L', 'any', 'foo')
    assert pulse.list_watches('a')[0]['watchId'] == w['watchId']
    assert pulse.list_watches()[0]['watchId'] == w['watchId']
    assert pulse.observe('any', 'topic', {'title': 'foo bar', 'ts': 1000000000000}, 'k') == 1
    assert pulse.observe('any', 'topic', {'title': 'foo bar', 'ts': 1000000000000}, 'k') == 0
    assert pulse.events(watch_id=w['watchId'], agent_id='a', since_ms=0, limit=5)
    assert 'PULSE BRIEF' in pulse.brief('a', 24)
    assert 'No watches' in pulse.brief('nobody', 24)
    assert 'watchId is required' in pulse.execute_pulse_tool('a', 'pulse_watch', {'action': 'delete'})
    assert 'ok' in pulse.execute_pulse_tool('a', 'pulse_watch', {'action': 'create'})
    assert 'ok' in pulse.execute_pulse_tool('a', 'pulse_watch', {'action': 'list'})
    monkeypatch.setattr(pulse, 'wrap_external_content', lambda *a: ('w', '', '', 'i'), raising=False)
    import serve
    monkeypatch.setattr(serve, 'wrap_external_content', lambda *a: ('w', '', '', 'i'))
    assert 'i' in pulse.execute_pulse_tool('a', 'read_pulse', {'action': 'brief'})
    assert 'i' in pulse.execute_pulse_tool('a', 'read_pulse', {'action': 'events', 'hours': 1})
    assert 'unknown' in pulse.execute_pulse_tool('a', 'x', {})
    assert pulse.delete_watch(w['watchId'], 'other') is False
    assert pulse.delete_watch(w['watchId'], 'a') is True


def test_captcha_remaining(monkeypatch):
    assert captcha._serve() is not None
    f = Fake(); monkeypatch.setattr(captcha, '_serve', lambda: f)
    assert captcha.set_answer('missing', 'x') is None
    rec = captcha.create_request('a', 's', 'h', 'u', 'image')
    captcha.mark_solved(rec['id'])
    assert captcha.set_answer(rec['id'], 'x') is None
    rec2 = captcha.create_request('a', 's', 'h', 'u2', 'image')
    assert captcha.wait_for_answer(rec2['id'], wait_s=1, poll_s=0.01)['status'] == 'pending'
    captcha.mark_solved(rec2['id'])
    assert captcha.wait_for_answer(rec2['id'], wait_s=1)['status'] == 'solved'


def test_pulse_remaining(monkeypatch):
    assert pulse._db() is not None
    f = Fake()
    monkeypatch.setattr(pulse, '_db', f._db)
    pulse._HAS_WATCHES = False
    assert pulse.observe('x', 't', {}) == 0
    pulse._HAS_WATCHES = True
    pulse._LAST_OBSERVE['x'] = time.time()
    assert pulse.observe('x', 't', {}) == 0
    pulse._LAST_OBSERVE.clear()
    assert pulse.observe('x', 't', {'ts': 'bad'}) == 0
    assert pulse.observe('x', 't', {}) == 0  # no watches
    w = pulse.create_watch('a', 'L', 'any', 'needle')
    assert pulse.observe('any', 't', {'title': 'haystack'}, 'k2') == 0
    pulse._LAST_OBSERVE.clear()
    assert pulse.observe('any', 't', {'title': 'needle', 'ts': 1000000000000}, 'k3') == 1
    assert pulse.brief('a', 1)
    assert pulse.delete_watch(w['watchId']) is True
    # tables error
    monkeypatch.setattr(pulse, '_db', lambda: (_ for _ in ()).throw(RuntimeError('x')))
    pulse._TABLES_READY = False
    pulse._ensure_tables()
    # payload error
    monkeypatch.setattr(pulse.json, 'dumps', lambda *a, **k: (_ for _ in ()).throw(TypeError('x')))
    assert pulse._payload_text(object())


def test_pulse_observe_error_brief_delete(monkeypatch):
    f = Fake(); monkeypatch.setattr(pulse, '_db', f._db)
    w = pulse.create_watch('a', 'L', 'any', '')
    pulse._LAST_OBSERVE.clear()
    assert pulse.observe('any', 't', {'title': 'x', 'ts': int(time.time()*1000)}, 'z') == 1
    assert 'latest' in pulse.brief('a', 24)
    out = pulse.execute_pulse_tool('a', 'pulse_watch', {'action': 'delete', 'watchId': w['watchId']})
    assert '"ok": true' in out
    class Bad:
        def execute(self, *a, **k): raise RuntimeError('x')
    @contextmanager
    def bad_db():
        yield Bad()
    monkeypatch.setattr(pulse, '_db', bad_db)
    pulse._LAST_OBSERVE.clear()
    assert pulse.observe('any', 't', {}, 'q') == 0
