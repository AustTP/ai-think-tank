import sqlite3

import integrations.apis as apis
import server.migrations as migrations
import core.model_config as model_config
import core.structured_edits as structured_edits
import core.url_evidence as url_evidence


def test_migrations_files(tmp_path, monkeypatch):
    (tmp_path / '0002_b.sql').write_text('select 2;')
    (tmp_path / '0001_a.sql').write_text('select 1;')
    (tmp_path / 'ignore.txt').write_text('x')
    monkeypatch.setattr(migrations, 'MIGRATIONS_DIR', tmp_path)
    assert [p.name for p in migrations._files()] == ['0001_a.sql', '0002_b.sql']


def test_api_unknown_credential_type():
    assert apis._api_credential_value({'credential': {'type': 'mystery'}}) == (
        None, None, 'registry entry has no credential configured')


def test_url_evidence_valid_and_allows():
    assert url_evidence.DiscoveredURLs.valid('javascript:alert(1)') is False
    assert url_evidence.DiscoveredURLs.valid('https://user:pass@example.com') is False
    assert url_evidence.DiscoveredURLs.valid('https://example.com/[REDACTED]') is False
    assert url_evidence.DiscoveredURLs.valid('https://example.com/a b') is False
    assert url_evidence.DiscoveredURLs.valid('http://[bad') is False
    d = url_evidence.DiscoveredURLs('see https://example.com/a).')
    assert d.allows('https://example.com/a/') is True
    assert d.allows('https://other.example/a') is False


def test_structured_edits_rejections():
    for bad in ('', 'no json', 'x {bad} y'):
        try:
            structured_edits._load(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    assert structured_edits._load('prefix {"edits": []} suffix') == {'edits': []}
    for bad in (
        {'edits': []},
        {'edits': [{'path': '/abs', 'old': 'a', 'new': 'b'}]},
        {'edits': [{'path': 'a.py', 'old': '', 'new': 'b'}]},
        {'edits': [{'path': 'a.py', 'old': 'a', 'new': 3}]},
        {'edits': [{'path': 'a.py', 'old': 'a', 'new': 'b'}], 'commands': 'x'},
    ):
        try:
            structured_edits.command(__import__('json').dumps(bad))
        except ValueError:
            pass
        else:
            raise AssertionError(bad)


def test_model_config_empty_gaps_and_non_numeric_price(tmp_path):
    cfg = {
        'price_bands': {'low': [0, 1], 'mid': [1, 2], 'high': [2, 3]},
        'band_purpose': {b: 'x' for b in ('low', 'sublow', 'mid', 'coding', 'high')},
        'band_benchmark': {b: 'x' for b in model_config.BANDS},
        'quality_floor_gap': {},
        'tier_gate_criteria': {b: 'x' for b in ('low', 'mid', 'high')},
    }
    db = tmp_path / 'm.db'
    con = sqlite3.connect(db)
    con.executescript('''
        CREATE TABLE model_tiers (band TEXT, slug TEXT, price_per_m REAL);
        CREATE TABLE model_tier_fallbacks (band TEXT, rank INTEGER, slug TEXT);
        INSERT INTO model_tiers VALUES ('low','a','x');
        INSERT INTO model_tier_fallbacks VALUES ('low',1,'a');
    ''')
    con.commit(); con.close()
    problems, _ = model_config.validate(cfg, db)
    assert any('quality_floor_gap is empty' in p for p in problems) or any('missing' in p for p in problems)
    assert any('non-numeric price' in p for p in problems)
