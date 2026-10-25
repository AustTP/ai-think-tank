import json

import core.config as config


def _p(tmp_path, data, name='x.json'):
    p = tmp_path / name
    p.write_text(data if isinstance(data, str) else json.dumps(data))
    return str(p)


def test_standing_and_costs(tmp_path):
    p = _p(tmp_path, {'standing_safe_domains': 'x'})
    assert config._load_standing_safe_domains(p)
    p = _p(tmp_path, {'standing_safe_domains': [1, '', 'bad/x', 'a.com', '.b.org', 'gov']})
    assert 'a.com' in config._load_standing_safe_domains(p)
    p = _p(tmp_path, {'treg_endpoint_costs': []})
    assert config._load_treg_endpoint_costs(p)
    p = _p(tmp_path, {'treg_endpoint_costs': {'': 1, 'a': 'x', 'b': -1, 'c': 2}})
    assert config._load_treg_endpoint_costs(p)['c'] == 2


def test_model_bands(tmp_path):
    p = _p(tmp_path, {'price_bands': {'a': [1], 'b': [-1, 1], 'c': [0, 1]},
                      'band_metric_chains': {'a': 'x', 'b': [1], 'c': ['x']},
                      'quality_floor_gap': {'': 1, 'a': 'x', 'b': -1, 'c': 2}})
    out = config._load_model_bands(p)
    assert out['price_bands']['c'] == (0.0, 1.0)
    p = _p(tmp_path, {'price_bands': 'x', 'band_metric_chains': 'x', 'quality_floor_gap': 'x'})
    config._load_model_bands(p)


def test_endpoint_room_seed(tmp_path):
    p = _p(tmp_path, {'extra_non_waking_reads': 'x', 'extra_non_waking_writes': ['a', '/b']})
    assert config._load_endpoint_policy(p)['writes'] == {'/b'}
    p = _p(tmp_path, {'definitions': {'': {}, 'r': 'x', 'r2': {'label': ' L ', 'purpose': ''}}})
    config._load_room_overrides(p)
    p = _p(tmp_path, {'profiles': {'': {'mission': 'm'}, 'r': 'x', 'r2': {'mission': ' M ', 'instructions': [' a ', 1], 'notes': ['n', 1]}},
                      'default': {'mission': ''}})
    out = config._load_seed_profiles(p)
    assert out['profiles']['r2']['mission'] == 'M'


def test_action_desc_secret(tmp_path):
    p = _p(tmp_path, {'actions': 'x', 'api_log_allowed': ['a', 1], 'other': []})
    config._load_action_taxonomy(p)
    p = _p(tmp_path, {'descriptions': []})
    assert config._load_tool_descriptions(p) == {}
    p = _p(tmp_path, {'descriptions': {'': 'x', 'a': 1, 'b': ' t '}})
    assert config._load_tool_descriptions(p) == {'b': 't'}
    p = _p(tmp_path, {'secret_patterns': 'x'})
    assert config._load_secret_patterns(p)
    p = _p(tmp_path, {'secret_patterns': ['', 1, '[', 'x']})
    assert config._load_secret_patterns(p)


def test_research_watch_grants(tmp_path):
    p = _p(tmp_path, {'passes': 'x'})
    assert config._load_research_desk(p)
    p = _p(tmp_path, {'passes': [{}, {'key': 'k', 'question': 'q'}]})
    assert config._load_research_desk(p)[0]['key'] == 'k'
    p = _p(tmp_path, {'topics': 'x'})
    assert config._load_watchlist(p) == []
    p = _p(tmp_path, {'topics': [1, {}, {'topic': 't', 'startUrl': 'ftp://x'},
                                 {'topic': 't', 'startUrl': 'https://x', 'cadenceMs': 1,
                                  'linkKeyword': 'l', 'pageKeyword': 'p'}]})
    assert config._load_watchlist(p)
    p = _p(tmp_path, 'x')
    assert config._load_grants(p) == {'appGrants': [], 'fileGrants': []}
    p = _p(tmp_path, {'appGrants': [1, {'bundleId': 'bad'}, {'bundleId': 'com.x', 'caps': {'use': False}}],
                      'fileGrants': [1, {'scope': ''}, {'scope': '~/x', 'caps': {'read': False}}]})
    config._load_grants(p)
