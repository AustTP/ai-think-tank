"""The agent phone lane (world/phone.py): chat
between agents filed into the recipient's ordinary
mailbox as phone-channel entries, gated by Theo's
roster policy. A phone message never wakes an
off-duty agent -- unlike _deliver_mail, which
wakes her, chat is not work assignment.

Uses the real TheoPolicy over a roster dict, so
the gate tested here is the one serve runs. The
policy's clock is injectable for the rate limit.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import phone  # noqa: E402
import sim  # noqa: E402
import theo_policy  # noqa: E402


def _state():
    # Plain roster agents resolve as workers, and
    # workers may send_message and read_message --
    # the phone gate. Mailboxes start empty.
    return {
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True},
            {'id': 'nora', 'isDirector': True},
            {'id': 'ada'},
            {'id': 'bee'},
        ],
        'agents': {
            'theo': {'mailbox': []},
            'nora': {'mailbox': []},
            'ada': {'mailbox': []},
            'bee': {'mailbox': []},
        },
    }


class _Clock(object):
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class PhoneSend(unittest.TestCase):
    def setUp(self):
        self.state = _state()
        self.policy = theo_policy.TheoPolicy()
        self.phone = phone.Phone(self.policy)

    def test_send_files_a_phone_entry(self):
        sent = self.phone.send(self.state, 'ada',
                                  'bee', 'blocked on the build')
        self.assertTrue(sent.allowed)
        self.assertEqual(sent.agent_id, 'ada')
        self.assertRegex(sent.message_id, r'^m-[0-9a-f]{8}$')
        entry = self.state['agents']['bee']['mailbox'][0]
        self.assertEqual(entry['kind'], 'phone')
        self.assertEqual(entry['channel'], 'phone')
        self.assertEqual(entry['mid'], sent.message_id)
        self.assertEqual(entry['from'], 'ada')
        self.assertEqual(entry['to'], 'bee')
        self.assertEqual(entry['text'],
                         'blocked on the build')
        self.assertFalse(entry['read'])
        self.assertIn('ts', entry)

    def test_send_to_unknown_recipient_denied(self):
        sent = self.phone.send(self.state, 'ada',
                                  'ghost', 'hello')
        self.assertFalse(sent.allowed)
        self.assertEqual(sent.reason, 'unknown recipient')
        self.assertEqual(self.state['agents']['ada']
                         ['mailbox'], [])

    def test_send_is_rate_limited(self):
        clock = _Clock()
        policy = theo_policy.TheoPolicy(
            rate_limit=1, clock=clock)
        phone_lane = phone.Phone(policy)
        first = phone_lane.send(self.state, 'ada',
                                   'bee', 'one')
        second = phone_lane.send(self.state, 'ada',
                                    'bee', 'two')
        self.assertTrue(first.allowed)
        self.assertFalse(second.allowed)
        self.assertEqual(second.reason,
                         'rate limit exceeded')
        self.assertEqual(
            len(self.state['agents']['bee']['mailbox']),
            1)

    def test_send_never_wakes_off_duty_recipient(self):
        self.state['agents']['bee']['offDuty'] = True
        self.state['agents']['bee']['_mailAwake'] = (
            False)
        sent = self.phone.send(self.state, 'ada',
                                  'bee', 'hello')
        self.assertTrue(sent.allowed)
        self.assertTrue(
            self.state['agents']['bee']['offDuty'])
        self.assertFalse(
            self.state['agents']['bee'].get(
                '_mailAwake', False))

    def test_send_retention_trims_to_keep_count(self):
        # A rate limit above the keep count, so the
        # retention trim is what bounds the mailbox,
        # not the phone gate.
        policy = theo_policy.TheoPolicy(
            rate_limit=sim.MAILBOX_KEEP_COUNT + 10)
        phone_lane = phone.Phone(policy)
        bee = self.state['agents']['bee']
        for i in range(sim.MAILBOX_KEEP_COUNT + 5):
            phone_lane.send(self.state, 'ada', 'bee',
                              'msg %d' % i)
        mailbox = bee['mailbox']
        self.assertEqual(len(mailbox),
                         sim.MAILBOX_KEEP_COUNT)
        self.assertEqual(
            mailbox[-1]['text'],
            'msg %d' % (sim.MAILBOX_KEEP_COUNT + 4))


class PhoneInbox(unittest.TestCase):
    def setUp(self):
        self.state = _state()
        self.policy = theo_policy.TheoPolicy()
        self.phone = phone.Phone(self.policy)

    def test_inbox_lists_unread_phone_oldest_first(self):
        self.phone.send(self.state, 'ada', 'bee', 'a')
        self.phone.send(self.state, 'nora', 'bee', 'b')
        # A non-phone mailbox entry stays invisible to
        # the phone lane.
        sim._append_mailbox(
            self.state['agents']['bee'],
            {'kind': 'review', 'channel': 'mail',
             'text': 'not phone'})
        box = self.phone.inbox(self.state, 'bee')
        self.assertTrue(box.allowed)
        self.assertEqual(box.agent_id, 'bee')
        self.assertEqual([m['text'] for m in box.messages],
                         ['a', 'b'])

    def test_read_messages_drop_out_of_the_inbox(self):
        sent = self.phone.send(self.state, 'ada',
                                  'bee', 'a')
        self.phone.read(self.state, 'bee',
                           sent.message_id)
        box = self.phone.inbox(self.state, 'bee')
        self.assertEqual(box.messages, [])

    def test_inbox_of_unknown_agent_denied(self):
        box = self.phone.inbox(self.state, 'ghost')
        self.assertFalse(box.allowed)
        self.assertEqual(box.reason, 'unknown agent')

    def test_worker_may_read(self):
        # Workers hold the read_message capability, so
        # any agent can read her own phone mail.
        box = self.phone.inbox(self.state, 'bee')
        self.assertTrue(box.allowed)


class PhoneRead(unittest.TestCase):
    def setUp(self):
        self.state = _state()
        self.policy = theo_policy.TheoPolicy()
        self.phone = phone.Phone(self.policy)

    def test_read_marks_one_message(self):
        first = self.phone.send(self.state, 'ada',
                                   'bee', 'a')
        self.phone.send(self.state, 'ada',
                             'bee', 'b')
        box = self.phone.read(self.state, 'bee',
                                 first.message_id)
        self.assertTrue(box.allowed)
        self.assertEqual([m['text'] for m in box.messages],
                         ['b'])
        bee = self.state['agents']['bee']
        self.assertTrue(bee['mailbox'][0]['read'])
        self.assertFalse(bee['mailbox'][1]['read'])

    def test_read_unknown_message_denied(self):
        self.phone.send(self.state, 'ada', 'bee', 'a')
        box = self.phone.read(self.state, 'bee',
                                 'm-deadbeef')
        self.assertFalse(box.allowed)
        self.assertEqual(box.reason, 'unknown message')

    def test_read_is_rate_limited(self):
        clock = _Clock()
        policy = theo_policy.TheoPolicy(
            rate_limit=1, clock=clock)
        phone_lane = phone.Phone(policy)
        sent = phone_lane.send(self.state, 'ada',
                                  'bee', 'a')
        first = phone_lane.read(self.state, 'bee',
                                   sent.message_id)
        second = phone_lane.read(self.state, 'bee',
                                    sent.message_id)
        self.assertTrue(first.allowed)
        self.assertFalse(second.allowed)
        self.assertEqual(second.reason,
                         'rate limit exceeded')


class PhoneMarkAllRead(unittest.TestCase):
    def setUp(self):
        self.state = _state()
        self.policy = theo_policy.TheoPolicy()
        self.phone = phone.Phone(self.policy)

    def test_mark_all_read_clears_the_inbox(self):
        self.phone.send(self.state, 'ada', 'bee', 'a')
        self.phone.send(self.state, 'nora', 'bee', 'b')
        box = self.phone.mark_all_read(self.state,
                                          'bee')
        self.assertTrue(box.allowed)
        self.assertEqual(box.messages, [])
        bee_mailbox = self.state['agents']['bee']['mailbox']
        for entry in bee_mailbox:
            self.assertTrue(entry['read'])

    def test_mark_all_read_keeps_other_channels(self):
        sim._append_mailbox(
            self.state['agents']['bee'],
            {'kind': 'review', 'channel': 'mail',
             'text': 'not phone'})
        self.phone.send(self.state, 'ada', 'bee', 'a')
        self.phone.mark_all_read(self.state, 'bee')
        self.assertFalse(
            self.state['agents']['bee']['mailbox'][0]
            ['read'])


if __name__ == '__main__':
    unittest.main()
