import json

import core.config as config


def _p(tmp_path, data, name='x.json'):
    p = tmp_path / name
    p.write_text(data if isinstance(data, str) else json.dumps(data))
    return str(p)


def test_loader_bad_shapes(tmp_path):
    p = _p(tmp_path, [])
    config._load_model_bands(p)
    p = _p(tmp_path, {'price_bands': {'a': 'x', 'b': [0, 0]}, 'band_metric_chains': {'a': 'x', 'b': [1]},
                      'quality_floor_gap': {'a': 'x', 'b': -1}})
    config._load_model_bands(p)
    config._load_endpoint_policy(p)
    config._load_room_overrides(p)
    config._load_seed_profiles(p)


def test_credential_access(tmp_path):
    p = _p(tmp_path, {'readOnlyHosts': 'x', 'autoGrant': {
        '': {}, 'a': 'x', 'b': {'hosts': 'x'}, 'c': {'hosts': ['']},
        'd': {'hosts': ['H'], 'methods': 'x', 'ttlSec': 'bad'},
        'e': {'hosts': ['H'], 'methods': ['', 'post'], 'ttlSec': 99999}}})
    out = config._load_credential_access(p)
    assert out['autoGrant']['d']['ttlSec'] == 900
    assert out['autoGrant']['e']['ttlSec'] == 3600
    p = _p(tmp_path, {'readOnlyHosts': []})
    assert config._load_credential_access(p)['readOnlyHosts']
    p = _p(tmp_path, 'x')
    assert config._load_credential_access(p)['autoGrant'] == {}


def test_watchlist_operations(tmp_path, monkeypatch):
    p = tmp_path / 'w.json'
    monkeypatch.setattr(config, 'WATCHLIST_PATH', str(p))
    monkeypatch.setattr(config, 'ALLOW_WATCHLIST_WRITE', False)
    assert config.note_watchlist_topic({'topic': 't', 'startUrl': 'u'}) is False
    assert config.remove_watchlist_topic('t') is False
    assert config.update_watchlist_topic('t', 'u', {'topic': 'n', 'startUrl': 'v'}) is False
    monkeypatch.setattr(config, 'ALLOW_WATCHLIST_WRITE', True)
    assert config.note_watchlist_topic(None) is False
    assert config.note_watchlist_topic({'topic': ''}) is False
    assert config.remove_watchlist_topic('') is False
    p.write_text('bad')
    assert config.remove_watchlist_topic('t') is False
    p.write_text(json.dumps({'topics': 'x'}))
    assert config.remove_watchlist_topic('t') is False
    p.write_text(json.dumps({'topics': [{'topic': 't', 'startUrl': 'u'}]}))
    assert config.remove_watchlist_topic('t', 'other') is False
    assert config.remove_watchlist_topic('t') is True
    # note: missing file, non-list topics, existing, new fields
    p.unlink()
    p.write_text(json.dumps({'topics': []}))
    assert config.note_watchlist_topic({'topic': 't', 'startUrl': 'u', 'cadenceMs': 1,
                                        'linkKeyword': 'l', 'pageKeyword': 'p', 'enabled': False}) is True
    assert config.note_watchlist_topic({'topic': 't', 'startUrl': 'u'}) is False
    p.write_text(json.dumps({'topics': 'x'}))
    assert config.note_watchlist_topic({'topic': 't2', 'startUrl': 'u2'}) is True
    # replace path
    assert config.update_watchlist_topic('t2', 'u2', {'topic': 't3', 'startUrl': 'u3'}) is True
    # exception path
    monkeypatch.setattr(config.json, 'dump', lambda *a, **k: (_ for _ in ()).throw(TypeError('x')))
    assert config.note_watchlist_topic({'topic': 't4', 'startUrl': 'u4'}) is False
    assert config.remove_watchlist_topic('t3') is False
