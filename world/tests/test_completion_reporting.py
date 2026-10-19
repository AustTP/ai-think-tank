from unittest.mock import patch
import copy

import completion_reporting
import sim


def task(tid='a', **changes):
    return {'id': tid, 'title': 'Generate portfolio report', 'status': 'done',
            'note': 'Created report formatter and an output report.',
            'acceptanceCriteria': 'Report contains verified holdings.',
            'libraryPath': 'reports/portfolio.md', **changes}


def test_two_completions_each_get_writeup_without_duplicate_delivery():
    state = {'tasks': {'a': task(), 'b': task('b')}}
    for t in state['tasks'].values():
        completion_reporting.queue(state, t)
        completion_reporting.queue(state, t)
    assert len(state['emailOutbox']) == 2
    assert len({e['kind'] for e in state['emailOutbox']}) == 2
    body = state['tasks']['a']['completionWriteup']['body']
    assert 'Created report formatter' in body and 'reports/portfolio.md' in body
    assert 'not proof of acceptance' in body
    assert 'visme.co/blog/report-writing-format/' in body


def test_reviews_drafts_and_incomplete_tasks_do_not_send_completion_writeups():
    for changes in ({'status':'needs_review'}, {'reviewOf':'parent'}, {'shadowDoneAt':1}, {'distill':True},
                    {'recoveryOf':'parent'}, {'title':'Fix: orphaned repair'},
                    {'title':'Repair: report'}, {'taskType':'code_preflight'}):
        state = {'tasks': {'a':task(**changes)}}
        assert completion_reporting.queue(state, state['tasks']['a']) is None
        assert not state.get('emailOutbox')


def test_existing_queued_repair_email_is_suppressed_before_sending():
    state = {'tasks': {'a':task(title='Fix: portfolio report')}, 'emailOutbox': [
        {'kind':'story_done:a','completionTaskId':'a','subject':'Repair completed','body':'Repair details'}]}
    with patch('serve.send_player_email_sync') as email:
        sim._drain_email_outbox_sync(state)
    email.assert_not_called()
    assert not state['emailOutbox']
    assert state['tasks']['a']['completionWriteup']['emailStatus'] == 'suppressed'


def test_failed_delivery_is_retained_and_success_does_not_send_twice():
    state = {'tasks': {'a':task()}}
    completion_reporting.queue(state, state['tasks']['a'])
    with patch('serve.send_player_email_sync', return_value=False), patch('serve.send_player_telegram_sync') as telegram:
        sim._drain_email_outbox_sync(state)
    telegram.assert_not_called()
    assert state['tasks']['a']['completionWriteup']['emailStatus'] == 'retrying'
    assert len(state['emailOutbox']) == 1
    with patch('serve.send_player_email_sync', return_value=True) as email:
        sim._drain_email_outbox_sync(state)
        email.assert_not_called()  # scheduled backoff is respected
        state['emailOutbox'][0]['nextAttemptAt'] = 0
        sim._drain_email_outbox_sync(state)
        sim._drain_email_outbox_sync(state)
        email.assert_called_once()
    assert state['tasks']['a']['completionWriteup']['emailStatus'] == 'sent'
    assert state['emailOutbox'] == []


def test_background_sender_merges_delivery_receipt_without_overwriting_new_work():
    state = {'tasks': {'a':task()}}
    completion_reporting.queue(state, state['tasks']['a'])
    fresh = copy.deepcopy(state)
    fresh['tasks']['a']['note'] = 'New information arrived during SMTP send.'
    with patch('serve.get_state_from_db', side_effect=[state, fresh]), \
            patch('serve.save_state_to_db') as save, \
            patch('serve.send_player_email_sync', return_value=True):
        sim._drain_emails_from_db()
    saved = save.call_args.args[0]
    assert saved['tasks']['a']['completionWriteup']['emailStatus'] == 'sent'
    assert saved['tasks']['a']['note'] == 'New information arrived during SMTP send.'
    assert not saved['emailOutbox']
