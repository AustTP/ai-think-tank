import sqlite3
import sys

import feedback_store
import optimization_advisor
import rss
import totp


def test_totp_branches():
    for bad in (None, '', '!!!!'):
        try:
            totp.normalize_secret(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    try:
        totp._algorithm('md5')
    except ValueError:
        pass
    assert totp.verify('JBSWY3DPEHPK3PXP', 123) is False
    assert totp.verify('JBSWY3DPEHPK3PXP', '1234567') is False
    for uri in (None, '', 'http://x', 'otpauth://totp/x'):
        try:
            totp.parse_otpauth(uri)
        except ValueError:
            pass
        else:
            raise AssertionError(uri)
    try:
        totp.provisioning_uri('JBSWY3DPEHPK3PXP', '')
    except ValueError:
        pass
    info = totp.code_info('JBSWY3DPEHPK3PXP', at=0)
    assert info['code'] and info['period'] == 30


def test_feedback_store_errors_and_corruption(tmp_path):
    store = feedback_store.FeedbackStore(str(tmp_path / 'f.db'))
    for kwargs in ({'work_item_id': '', 'player': 'p'}, {'work_item_id': 'x', 'player': 'p', 'sentiment': 'meh'},
                   {'work_item_id': 'x', 'player': 'p', 'works': 'maybe'},
                   {'work_item_id': 'x', 'player': 'p', 'category': 'bad'},
                   {'work_item_id': 'x', 'player': 'p', 'severity': 'bad'}):
        try:
            store.add(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(kwargs)
    fid = store.add('x', 'p')
    with sqlite3.connect(str(tmp_path / 'f.db')) as con:
        con.execute('UPDATE player_feedback SET ratings=?, quick_actions=? WHERE id=?', ('bad', 'bad', fid))
        con.commit()
    item = store.list('x')[0]
    assert item['ratings'] == {} and item['quick_actions'] == []
    assert store.set_status(999, 'p', 'resolved') is None
    with sqlite3.connect(str(tmp_path / 'f.db')) as con:
        con.execute('UPDATE player_feedback SET ratings=?, quick_actions=? WHERE id=?', ('bad', 'bad', fid))
        con.commit()
    assert store.set_status(fid, 'p', 'resolved')['ratings'] == {}
    assert store.get_taste('p')['weights'] == {}
    with sqlite3.connect(str(tmp_path / 'f.db')) as con:
        con.execute('INSERT INTO player_taste VALUES (?,?,?,?)', ('p', 'bad', '', 1))
        con.commit()
    assert store.get_taste('p')['weights'] == {}
    assert store.set_taste('p', {'a': 'bad', 'b': 200})['weights'] == {'b': 100}


def test_optimization_advisor_branches():
    snap = {
        'db_ok': False, 'task_assigned_last_24h': 10, 'task_fast_completion_rate': 0.2,
        'task_in_flight_last_24h': 6, 'aging_in_flight_work': 6,
        'work_queue_due_size': 20, 'agents_count': 2,
        'review_grader_kappa': 0.2, 'review_grader_samples': 20,
        'missing_model_tier_bands': ['low'], 'ceremony_imbalance_score': 2,
        'open_escalations': 3, 'bank_over_cap': ['x'],
    }
    out = optimization_advisor.build_optimization_advice(snap, now=0)
    assert out['recommendations'] and out['bottleneck']
    assert optimization_advisor.build_optimization_advice({'db_ok': True, 'task_in_flight_last_24h': 6})['recommendations']
    assert optimization_advisor.build_optimization_advice({})['recommendations']


def test_rss_ingest_pulse_failure(monkeypatch):
    import serve
    monkeypatch.setattr(serve, '_RSS_BUFFER_LOCK', __import__('threading').Lock())
    monkeypatch.setattr(serve, '_RSS_BUFFER', {})
    monkeypatch.setattr(serve, '_RSS_BUFFER_MAX_PER_FEED', 2)
    monkeypatch.setitem(sys.modules, 'pulse', type('P', (), {'observe': staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))}))
    assert rss._rss_ingest('f', [{'title': 'a', 'link': 'u'}, {'title': 'b', 'link': 'u'}]) == 1
