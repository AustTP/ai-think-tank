import os

from feedback_store import FeedbackStore
import feedback_api


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
    path = os.path.join(os.path.dirname(feedback_api.__file__), 'feedback.html')
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
    from governance import supervision_level
    assert supervision_level({}, 'nora', 'feedback') == 3
