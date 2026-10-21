"""Schedule control panel: list, edit, and delete recurring topics and
pending one-offs. Player-session only."""
import asyncio
import os
import sys

from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402


def _state():
    return {
        'researchTopics': [
            {'id': 't-1', 'topic': 'AI news', 'startUrl': 'https://example.com/news',
             'cadenceMs': 3600000, 'lastRunAt': 0, 'enabled': True},
            {'id': 't-2', 'topic': 'Off', 'startUrl': 'https://example.com/off',
             'cadenceMs': 3600000, 'lastRunAt': 0, 'enabled': False},
        ],
        'workQueue': [
            {'title': 'later', 'notBefore': 5000, 'room': 'pressoffice'},
            {'title': 'now', 'room': 'pressoffice'},
        ],
        'tasks': {
            'task-1': {'id': 'task-1', 'title': 'assigned one-off', 'notBefore': 9000,
                       'status': 'working'},
        },
    }


def test_list_returns_topics_and_one_offs():
    with mock.patch.object(serve, '_operator_gate', return_value=(True, 'player')), \
         mock.patch.object(serve, 'get_state_from_db', return_value=_state()):
        resp = asyncio.run(serve.list_schedules(mock.Mock()))
        import json
        data = json.loads(resp.body)
    assert [t['id'] for t in data['topics']] == ['t-1', 't-2']
    assert [o['key'] for o in data['oneOffs']] == ['q:0', 't:task-1']


def test_update_topic_mirrors_watchlist():
    state = _state()
    req = mock.Mock()
    req.json = mock.AsyncMock(return_value={
        'topic': 'AI news 2', 'cadenceMs': 7200000, 'enabled': False})
    with mock.patch.object(serve, '_operator_gate', return_value=(True, 'player')), \
         mock.patch.object(serve, '_state_begin', return_value=state), \
         mock.patch.object(serve, 'save_state_to_db'), \
         mock.patch.object(serve, 'log_action'), \
         mock.patch.object(serve.config, 'update_watchlist_topic') as mirror:
        resp = asyncio.run(serve.update_schedule_topic('t-1', req))
    assert resp.status_code == 200
    topic = state['researchTopics'][0]
    assert topic['topic'] == 'AI news 2'
    assert topic['cadenceMs'] == 7200000
    assert topic['enabled'] is False
    mirror.assert_called_once()


def test_update_topic_rejects_a_bad_url():
    state = _state()
    req = mock.Mock()
    req.json = mock.AsyncMock(return_value={'startUrl': 'not-a-url'})
    with mock.patch.object(serve, '_operator_gate', return_value=(True, 'player')), \
         mock.patch.object(serve, '_state_begin', return_value=state), \
         mock.patch.object(serve, 'save_state_to_db'):
        resp = asyncio.run(serve.update_schedule_topic('t-1', req))
    assert resp.status_code == 400


def test_delete_topic_removes_state_and_watchlist_entry():
    state = _state()
    with mock.patch.object(serve, '_operator_gate', return_value=(True, 'player')), \
         mock.patch.object(serve, '_state_begin', return_value=state), \
         mock.patch.object(serve, 'save_state_to_db'), \
         mock.patch.object(serve, 'log_action'), \
         mock.patch.object(serve.config, 'remove_watchlist_topic') as mirror:
        resp = asyncio.run(serve.delete_schedule_topic('t-1', mock.Mock()))
    assert resp.status_code == 200
    assert [t['id'] for t in state['researchTopics']] == ['t-2']
    mirror.assert_called_once_with('AI news', 'https://example.com/news')


def test_edit_one_off_in_the_queue():
    state = _state()
    req = mock.Mock()
    req.json = mock.AsyncMock(return_value={'title': 'renamed', 'at': '9999999999'})
    with mock.patch.object(serve, '_operator_gate', return_value=(True, 'player')), \
         mock.patch.object(serve, '_state_begin', return_value=state), \
         mock.patch.object(serve, 'save_state_to_db'), \
         mock.patch.object(serve, '_parse_schedule_once_at', return_value=12345):
        resp = asyncio.run(serve.update_schedule_once('q:0', req))
    assert resp.status_code == 200
    assert state['workQueue'][0]['title'] == 'renamed'
    assert state['workQueue'][0]['notBefore'] == 12345


def test_delete_assigned_one_off_cancels_it():
    state = _state()
    with mock.patch.object(serve, '_operator_gate', return_value=(True, 'player')), \
         mock.patch.object(serve, '_state_begin', return_value=state), \
         mock.patch.object(serve, 'save_state_to_db'), \
         mock.patch.object(serve, 'log_action'):
        resp = asyncio.run(serve.delete_schedule_once('t:task-1', mock.Mock()))
    assert resp.status_code == 200
    assert state['tasks']['task-1']['status'] == 'cancelled'
    assert state['tasks']['task-1']['notBefore'] is None
