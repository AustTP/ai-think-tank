import json

import pytest

import core.config as config
import serve


def _spec(**extra):
    spec = {
        'provider': 'local',
        'base_url': 'http://127.0.0.1:9999/v1/chat/completions',
        'model': 'SKY-001',
        'api_key_env': '',
        'price_per_m': 2.0,
        'send_reasoning': False,
    }
    spec.update(extra)
    return spec


def test_3d_alias_routes_to_coding(monkeypatch):
    monkeypatch.setattr(serve, 'MODEL_BAND_ALIASES', {'3d': 'coding'})
    monkeypatch.setattr(serve, 'MODEL_PROVIDER_OVERRIDES', {'default': None, 'tiers': {}})
    seen = []

    def raw(band):
        seen.append(band)
        return 'coding/model'

    monkeypatch.setattr(serve, '_raw_model_tier_slug', raw)
    assert serve._model_tier_slug('3d') == 'coding/model'
    assert seen == ['coding']


def test_tier_override_becomes_provider_slug(monkeypatch):
    monkeypatch.setattr(serve, 'MODEL_BAND_ALIASES', {'3d': 'coding'})
    monkeypatch.setattr(serve, 'MODEL_PROVIDER_OVERRIDES',
                        {'default': None, 'tiers': {'3d': _spec()}})
    assert serve._model_tier_slug('3d') == '@provider:3d'
    got = serve._model_provider_for('@provider:3d')
    assert got['model'] == 'SKY-001'
    assert got['is_custom'] is True


def test_default_override_without_model_uses_band_model(monkeypatch):
    monkeypatch.setattr(serve, 'MODEL_BAND_ALIASES', {'3d': 'coding'})
    monkeypatch.setattr(serve, 'MODEL_PROVIDER_OVERRIDES',
                        {'default': _spec(model=''), 'tiers': {}})
    monkeypatch.setattr(serve, '_raw_model_tier_slug', lambda band: 'coding/model')
    assert serve._model_tier_slug('3d') == '@provider:3d'
    assert serve._model_provider_for('@provider:3d')['model'] == 'coding/model'


def test_custom_endpoint_strips_reasoning_and_records_cost(monkeypatch):
    monkeypatch.setattr(serve, 'MODEL_PROVIDER_OVERRIDES',
                        {'default': None, 'tiers': {'3d': _spec()}})
    monkeypatch.setattr(serve, '_record_model_latency', lambda *a, **k: None)
    monkeypatch.setattr(serve.training_capture, 'record', lambda *a, **k: None)
    captured = {}

    def fake(req, timeout):
        captured['url'] = req.full_url
        captured['headers'] = dict(req.headers)
        captured['body'] = json.loads(req.data.decode())
        return json.dumps({'choices': [{'message': {'content': 'ok'}}],
                           'usage': {'total_tokens': 1000}}).encode()

    monkeypatch.setattr(serve, '_urlopen_with_resilience', fake)
    out = serve._openrouter_chat_post({
        'model': '@provider:3d', 'messages': [], 'max_tokens': 10,
        'reasoning': {'enabled': False},
    })
    assert captured['url'] == 'http://127.0.0.1:9999/v1/chat/completions'
    assert captured['body']['model'] == 'SKY-001'
    assert 'reasoning' not in captured['body']
    assert 'Authorization' not in captured['headers']
    assert out['usage']['cost'] == 0.002


def test_model_only_override_stays_on_openrouter(monkeypatch):
    monkeypatch.setattr(serve, 'MODEL_PROVIDER_OVERRIDES',
                        {'default': None, 'tiers': {'3d': _spec(base_url='', model='SKY-001')}})
    monkeypatch.setattr(serve, 'OPENROUTER_API_KEY', 'key-1')
    monkeypatch.setattr(serve, '_record_model_latency', lambda *a, **k: None)
    monkeypatch.setattr(serve.training_capture, 'record', lambda *a, **k: None)
    captured = {}

    def fake(req, timeout):
        captured['url'] = req.full_url
        captured['headers'] = dict(req.headers)
        captured['body'] = json.loads(req.data.decode())
        return b'{"choices": []}'

    monkeypatch.setattr(serve, '_urlopen_with_resilience', fake)
    serve._openrouter_chat_post({'model': '@provider:3d', 'messages': []})
    assert captured['url'] == serve.OPENROUTER_CHAT_COMPLETIONS_URL
    assert captured['body']['model'] == 'SKY-001'
    assert captured['headers']['Authorization'] == 'Bearer key-1'


def test_paid_custom_provider_respects_global_spend_cap(monkeypatch):
    monkeypatch.setattr(serve, 'MODEL_PROVIDER_OVERRIDES',
                        {'default': None, 'tiers': {'3d': _spec(price_per_m=2.0)}})
    monkeypatch.setattr(serve, '_think_tank_spend_cap_exceeded', lambda: True)
    with pytest.raises(RuntimeError, match='spend cap'):
        serve._call_openrouter_once_sync('@provider:3d', [], 10)


def test_free_custom_provider_does_not_respect_openrouter_spend_cap(monkeypatch):
    monkeypatch.setattr(serve, 'MODEL_PROVIDER_OVERRIDES',
                        {'default': None, 'tiers': {'3d': _spec(price_per_m=0.0)}})
    monkeypatch.setattr(serve, '_think_tank_spend_cap_exceeded', lambda: True)
    monkeypatch.setattr(serve, 'is_model_circuit_broken', lambda model: False)
    monkeypatch.setattr(serve, 'record_model_result', lambda *a, **k: None)
    monkeypatch.setattr(serve, '_openrouter_chat_post',
                        lambda body: {'choices': [{}], 'usage': {'cost': 0.0}})
    out = serve._call_openrouter_once_sync('@provider:3d', [], 10)
    assert out['usage']['cost'] == 0.0


def test_provider_billable_only_for_priced_custom_endpoints():
    assert serve._provider_is_billable({**_spec(), 'is_custom': True}) is True
    assert serve._provider_is_billable({**_spec(price_per_m=0), 'is_custom': True}) is False
    assert serve._provider_is_billable(_spec(base_url='', model='SKY-001')) is False
    assert serve._provider_is_billable(None) is False


def test_loader_rejects_bad_base_url_and_keeps_valid_tiers(tmp_path):
    path = tmp_path / 'model_providers.json'
    path.write_text(json.dumps({
        'default': {'base_url': 'file:///etc/passwd', 'model': 'x'},
        'tiers': {
            '3d': {'base_url': 'http://localhost:11434/v1/chat/completions',
                   'model': 'SKY-001'},
            'bad': {'base_url': 'not-a-url', 'model': 'x'},
            'empty': {},
        },
    }), encoding='utf-8')
    out = config._load_model_providers(path)
    assert out['default'] is None
    assert set(out['tiers']) == {'3d'}
    assert out['tiers']['3d']['price_per_m'] == 0.0
