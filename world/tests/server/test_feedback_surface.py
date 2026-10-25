import os

from server.feedback_store import FeedbackStore
import server.feedback_api as feedback_api


def test_feedback_store_round_trip(tmp_path):
    store = FeedbackStore(str(tmp_path / 'feedback.db'))
    fid = store.add('task-1', 'player', 'like', 'works', 'keep it',
                    {'quality': 8, 'aspects': {'layout': 9}},
                    ['more-like-this'])
    assert fid > 0
    rows = store.list(work_item_id='task-1', player='player')
    assert len(rows) == 1
    assert rows[0]['sentiment'] == 'like'
    assert rows[0]['ratings']['aspects']['layout'] == 9
    assert rows[0]['quick_actions'] == ['more-like-this']


def test_taste_round_trip(tmp_path):
    store = FeedbackStore(str(tmp_path / 'feedback.db'))
    saved = store.set_taste('player', {'bold_calm': 90, 'dense_sparse': 20}, 'sharper')
    assert saved['weights']['bold_calm'] == 90
    assert store.get_taste('player')['notes'] == 'sharper'


def test_work_items_projection_prefers_active_and_handles_lists():
    state = {'tasks': [
        {'id': 'done', 'title': 'Old', 'status': 'done', 'updatedAt': 1},
        {'id': 'live', 'title': 'Live', 'status': 'working',
         'assignedTo': 'nora', 'room': 'pressoffice', 'teamId': 'nora',
         'projectLabel': 'feedback UI', 'goal': 'Build it',
         'updatedAt': 9999999999999},
    ]}
    items = feedback_api._work_items(state)
    assert items[0]['id'] == 'live'
    assert items[0]['active'] is True


def test_feedback_page_markers():
    path = os.path.join(os.path.dirname(os.path.dirname(feedback_api.__file__)), 'feedback.html')
    text = open(path, encoding='utf-8').read()
    assert '/api/work-items' in text
    assert '/api/feedback' in text
    assert '/api/taste' in text
    assert 'setInterval' in text and 'countdown' in text
    assert 'data-sentiment' in text and 'data-works' in text
    assert 'data-taste' in text
    assert '@media(max-width:760px)' in text
    assert 'overflow-x:auto' in text


def test_supervision_defaults_to_three(tmp_path):
    from core.governance import supervision_level
    assert supervision_level({}, 'nora', 'feedback') == 3


def test_structured_fields_round_trip_and_filters(tmp_path):
    store = FeedbackStore(str(tmp_path / 'feedback.db'))
    first = store.add('task-1', 'player', category='bug', severity='high',
                      target='feedback.html', notes='broken select')
    second = store.add('task-2', 'player', category='idea', notes='more stats')
    rows = store.list(player='player')
    assert len(rows) == 2
    row = next(r for r in rows if r['id'] == first)
    assert row['category'] == 'bug' and row['severity'] == 'high'
    assert row['target'] == 'feedback.html' and row['status'] == 'new'
    assert [r['id'] for r in store.list(category='idea')] == [second]
    assert [r['id'] for r in store.list(status='new')] == [second, first]


def test_structured_fields_validate(tmp_path):
    store = FeedbackStore(str(tmp_path / 'feedback.db'))
    for bad in ({'category': 'rant'}, {'severity': 'urgent'}):
        try:
            store.add('task-1', 'player', **bad)
        except ValueError:
            pass
        else:
            raise AssertionError('expected ValueError for %r' % (bad,))


def test_set_status_and_ownership(tmp_path):
    store = FeedbackStore(str(tmp_path / 'feedback.db'))
    fid = store.add('task-1', 'player', category='confusion')
    triaged = store.set_status(fid, 'player', 'triaged')
    assert triaged['status'] == 'triaged'
    assert store.set_status(fid, 'someone-else', 'resolved') is None
    assert store.set_status(fid, 'player', 'resolved')['status'] == 'resolved'
    try:
        store.set_status(fid, 'player', 'ignored')
    except ValueError:
        pass
    else:
        raise AssertionError('expected ValueError for unknown status')


def test_old_database_gains_structured_columns(tmp_path):
    import sqlite3
    path = str(tmp_path / 'old.db')
    conn = sqlite3.connect(path)
    conn.execute('''CREATE TABLE player_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT, work_item_id TEXT NOT NULL,
        player TEXT NOT NULL, sentiment TEXT, works TEXT,
        notes TEXT NOT NULL DEFAULT '', ratings TEXT NOT NULL DEFAULT '{}',
        quick_actions TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL)''')
    conn.execute("INSERT INTO player_feedback (work_item_id, player, created_at) VALUES ('task-1','player',1)")
    conn.commit(); conn.close()
    store = FeedbackStore(path)
    rows = store.list(player='player')
    assert rows[0]['status'] == 'new'
    assert rows[0]['category'] is None
    assert store.set_status(rows[0]['id'], 'player', 'triaged')['status'] == 'triaged'


def test_feedback_page_has_review_surface():
    path = os.path.join(os.path.dirname(os.path.dirname(feedback_api.__file__)), 'feedback.html')
    text = open(path, encoding='utf-8').read()
    for marker in ('data-tab="review"', 'id="fbCategory"', 'id="fbSeverity"',
                   'id="fbTarget"', 'id="reviewList"', 'Mark triaged'):
        assert marker in text
