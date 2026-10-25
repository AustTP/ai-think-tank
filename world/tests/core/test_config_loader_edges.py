"""Residual coverage for world/config.py loaders: bad top-level shapes, empty
band keys, and a missing credential_access.json (mtime OSError path)."""
import json
import os

import config


def _p(tmp_path, data, name='x.json'):
    p = tmp_path / name
    p.write_text(data if isinstance(data, str) else json.dumps(data))
    return str(p)


def test_model_bands_skips_blank_band_and_non_list_chain(tmp_path):
    p = _p(tmp_path, {
        'price_bands': {'   ': [0, 1], 'ok': [0, 1]},
        'band_benchmark': {'   ': ['m'], 'not-a-list': 'metric',
                           'ok': ['metric']},
    })
    out = config._load_model_bands(p)
    assert set(out['price_bands']) == {'ok'}
    assert out['band_benchmark']['ok'] == ('metric',)


def test_loaders_reject_non_dict_json(tmp_path):
    p = _p(tmp_path, [])
    assert config._load_endpoint_policy(p) == {'reads': set(), 'writes': set()}
    assert config._load_room_overrides(p) == {'definitions': {}, 'delegatable': []}
    assert config._load_seed_profiles(p) == {'profiles': {}, 'default': None}
    assert config._load_action_taxonomy(p)['ceremony']
    assert config._load_credential_access(p)['autoGrant'] == {}


def test_credential_access_missing_file_resets_cache(tmp_path, monkeypatch):
    missing = str(tmp_path / 'nope.json')
    monkeypatch.setattr(config, 'CREDENTIAL_ACCESS_PATH', missing)
    monkeypatch.setitem(config._CREDENTIAL_ACCESS_CACHE, 'mtime', 'stale')
    monkeypatch.setitem(config._CREDENTIAL_ACCESS_CACHE, 'data', {'old': True})
    out = config.get_credential_access_config()
    assert out['autoGrant'] == {}
    assert config._CREDENTIAL_ACCESS_CACHE['mtime'] == -1
    assert os.path.exists(missing) is False
