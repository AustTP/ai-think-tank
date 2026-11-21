"""Cross-village mail queue and shard helpers (118_village_shards) plus the
tick relay wiring (010): enqueue/claim/complete lifecycle, stale recovery,
TTL expiry, mailbox delivery, and the global relay pass."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import sim  # noqa: E402


def _state_with_villages():
    return {
        'villages': [{'id': 'main', 'name': 'Main Village'},
                     {'id': 'vlg-1', 'name': 'North'}],
        'agents': {'ada': {'name': 'Ada', 'villageId': 'main'},
                   'ben': {'name': 'Ben', 'villageId': 'vlg-1'}},
    }


def test_enqueue_rejects_unknown_village():
    state = _state_with_villages()
    assert sim._village_mail_enqueue(state, 'main', 'nope', {}, 1000) is None
    mail_id = sim._village_mail_enqueue(state, 'main', 'vlg-1',
                                        {'toAgent': 'ben', 'kind': 'hello'}, 1000)
    assert mail_id == 'vmail-1'
    queue = sim._village_mail_queue(state)
    assert len(queue) == 1
    assert queue[0]['toVillage'] == 'vlg-1'
    assert queue[0]['status'] == 'queued'


def test_deliver_files_mailbox_and_completes():
    state = _state_with_villages()
    sim._village_mail_enqueue(state, 'main', 'vlg-1',
                              {'toAgent': 'ben', 'kind': 'issue_note',
                               'mail': {'kind': 'issue_note', 'text': 'hi'}}, 1000)
    assert sim._village_mail_deliver(state, 'vlg-1', 2000) == 1
    assert state['agents']['ben']['mailbox'][0]['text'] == 'hi'
    assert state['agents']['ben']['mailbox'][0]['read'] is False
    assert sim._village_mail_queue(state)[0]['status'] == 'done'
    assert sim._village_mail_queue(state)[0]['outcome'] == 'delivered'


def test_mail_to_wrong_village_agent_is_dropped():
    state = _state_with_villages()
    sim._village_mail_enqueue(state, 'vlg-1', 'vlg-1',
                              {'toAgent': 'ada'}, 1000)  # ada lives in main
    assert sim._village_mail_deliver(state, 'vlg-1', 2000) == 0
    assert sim._village_mail_queue(state)[0]['outcome'] == 'dropped'


def test_relay_pass_delivers_every_village_and_counts():
    state = _state_with_villages()
    sim._village_mail_enqueue(state, 'main', 'vlg-1', {'toAgent': 'ben'}, 1000)
    sim._village_mail_enqueue(state, 'vlg-1', 'main', {'toAgent': 'ada'}, 1000)
    counts = sim._village_mail_relay_pass(state, 2000)
    assert counts == {'main': 1, 'vlg-1': 1}
    assert state['agents']['ada']['mailbox'][0]['fromVillage'] == 'vlg-1'
    assert state['agents']['ben']['mailbox'][0]['fromVillage'] == 'main'


def test_stale_claim_recovers():
    state = _state_with_villages()
    sim._village_mail_enqueue(state, 'main', 'vlg-1', {'toAgent': 'ben'}, 1000)
    sim._village_mail_claim(state, 'vlg-1', 1000)
    assert sim._village_mail_recover_stale(state, 1000 + 1000) == 0  # inside window
    assert sim._village_mail_recover_stale(
        state, 1000 + sim.VILLAGE_CLAIM_TIMEOUT_MS + 1000) == 1
    queue = sim._village_mail_queue(state)
    assert queue[0]['status'] == 'queued'
    assert queue[0]['attempts'] == 1


def test_expired_mail_is_dropped():
    state = _state_with_villages()
    sim._village_mail_enqueue(state, 'main', 'vlg-1',
                              {'toAgent': 'gone-agent'}, 1000)
    assert sim._village_mail_deliver(state, 'vlg-1', 2000) == 0
    assert sim._village_mail_queue(state)[0]['outcome'] == 'dropped'
    # TTL expiry on a mail that was never claimed.
    sim._village_mail_enqueue(state, 'main', 'vlg-1', {'toAgent': 'nobody2'}, 3000)
    assert sim._village_mail_expire(state, 3000 + sim.VILLAGE_MAIL_TTL_MS + 1) == 1
    assert sim._village_mail_queue(state)[1]['outcome'] == 'expired'


def test_trim_drops_done_first_never_live():
    state = _state_with_villages()
    # Fill past the cap with done entries, then one live entry at the end.
    for i in range(sim.VILLAGE_MAIL_MAX):
        sim._village_mail_enqueue(state, 'main', 'vlg-1', {'toAgent': 'ben'}, i)
        sim._village_mail_claim(state, 'vlg-1', i)
        sim._village_mail_complete(state, f'vmail-{i + 1}', 'delivered', i)
    sim._village_mail_enqueue(state, 'main', 'vlg-1', {'toAgent': 'ben'}, 99_000)
    queue = sim._village_mail_queue(state)
    assert len(queue) == sim.VILLAGE_MAIL_MAX
    assert queue[-1]['status'] == 'queued'


def test_agents_in_village_slice():
    state = _state_with_villages()
    assert sim._agents_in_village(state, 'main') == ['ada']
    assert sim._agents_in_village(state, 'vlg-1') == ['ben']
    assert sim._agents_in_village(state, 'ghost') == []


def test_village_of_defaults_to_main():
    state = _state_with_villages()
    assert sim._village_of(state, 'ada') == 'main'
    assert sim._village_of(state, 'unknown') == 'main'


def _api_client(monkeypatch, state, authed=True):
    import serve
    from starlette.testclient import TestClient
    c = TestClient(serve.app)
    if authed:
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
    monkeypatch.setattr(serve, 'get_state_from_db', lambda: state)
    monkeypatch.setattr(serve, 'save_state_to_db', lambda s: None)
    monkeypatch.setattr(serve, 'log_action',
                        lambda *a, **k: None)
    return c


def test_mail_api_post_queues_and_returns_id(monkeypatch):
    state = _state_with_villages()
    r = _api_client(monkeypatch, state).post(
        '/api/villages/mail', json={'toVillage': 'vlg-1', 'toAgent': 'ben',
                                    'text': 'handoff ready'})
    assert r.status_code == 200
    body = r.json()
    assert body['status'] == 'queued'
    assert body['id'] == 'vmail-1'
    queue = sim._village_mail_queue(state)
    assert len(queue) == 1
    assert queue[0]['entry']['mail']['fromVillage'] == 'main'


def test_mail_api_post_unknown_village_is_404(monkeypatch):
    r = _api_client(monkeypatch, _state_with_villages()).post(
        '/api/villages/mail', json={'toVillage': 'nope', 'text': 'x'})
    assert r.status_code == 404


def test_mail_api_get_lists_queue(monkeypatch):
    state = _state_with_villages()
    sim._village_mail_enqueue(state, 'main', 'vlg-1',
                              {'toAgent': 'ben', 'kind': 'village_mail'}, 1000)
    r = _api_client(monkeypatch, state).get('/api/villages/mail')
    assert r.status_code == 200
    body = r.json()
    assert body['counts']['queued'] == 1
    assert body['total'] == 1
    assert body['mail'][0]['to'] == 'vlg-1'


def test_mail_api_get_requires_operator_auth(monkeypatch):
    r = _api_client(monkeypatch, _state_with_villages(),
                    authed=False).get('/api/villages/mail')
    assert r.status_code == 401
