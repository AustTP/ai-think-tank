"""The agent phone lane: chat between agents, gated by Theo.

Ask 13. Agents already talk through the mailbox when the
simulation files work for them (_deliver_mail) -- but that
path is for work the engine assigns, and it wakes an
off-duty agent to act. Agents also need a plain chat: a
worker telling her director she is blocked, a director
asking a question of another team. The phone is that lane.

A phone message is filed into the recipient agent's ordinary
mailbox as a phone-channel entry, so every surface that
already reads mailboxes sees phone chat with no second
store. Unlike _deliver_mail, a phone message never wakes an
off-duty agent: chat is not work assignment, and waking the
whole think tank every time two agents talk would drown the
wake-on-mail signal that real work depends on.

The gate is the same Theo policy as every other lane. The
acting agent's roster role must allow send_message or
read_message -- directors and workers both may, so any agent
can chat with any other agent -- and the per-agent rate
limit still applies. The policy decides, the mailbox stores,
and this module only routes between them.
"""

import secrets
import time

from sim import _append_mailbox


class PhoneSent(object):
    """The outcome of one phone send: whether Theo allowed
    it, why not when denied, who sent it, and the message id
    when the message was actually filed."""

    __slots__ = ('allowed', 'reason', 'agent_id', 'message_id')

    def __init__(self, allowed, reason='', agent_id=None,
                 message_id=None):
        self.allowed = allowed
        self.reason = reason
        self.agent_id = agent_id
        self.message_id = message_id


class PhoneInbox(object):
    """The unread phone mail for one agent, or the reason a
    send, read, or check was denied."""

    __slots__ = ('allowed', 'reason', 'agent_id', 'messages')

    def __init__(self, allowed, reason='', agent_id=None,
                 messages=None):
        self.allowed = allowed
        self.reason = reason
        self.agent_id = agent_id
        self.messages = list(messages or [])


class Phone(object):
    """Chat between agents over the shared mailbox. Every
    method re-checks the roster policy for the acting agent,
    so a phone call is allowed exactly when that agent's
    role may send or read messages -- the same decision the
    routing layer would make for a direct ask. The policy
    is injected, so a test can replace it with one whose
    clock and rate limits it controls."""

    def __init__(self, policy):
        self.policy = policy

    def send(self, state, agent_id, recipient_id, text):
        """File a phone message from `agent_id` to
        `recipient_id`, when Theo allows that agent to send
        messages. An unknown recipient is its own denial, so
        a mistyped id surfaces as a refusal instead of a
        chat that quietly went nowhere. Returns the sent
        outcome; the message id names the filed entry."""
        decision = self.policy.evaluate(
            state, agent_id, 'send_message',
            {'recipient': recipient_id, 'text': text})
        if not decision.allowed:
            return PhoneSent(False, decision.reason)
        recipient = (state.get('agents') or {}).get(recipient_id)
        if not isinstance(recipient, dict):
            return PhoneSent(False, 'unknown recipient',
                             agent_id=agent_id)
        mid = 'm-' + secrets.token_hex(4)
        _append_mailbox(recipient, {
            'kind': 'phone',
            'channel': 'phone',
            'mid': mid,
            'from': agent_id,
            'to': recipient_id,
            'text': text,
            'ts': int(time.time() * 1000),
        })
        return PhoneSent(True, agent_id=agent_id, message_id=mid)

    def inbox(self, state, agent_id):
        """The agent's unread phone messages, oldest first,
        when Theo allows her to read messages."""
        decision = self.policy.evaluate(
            state, agent_id, 'read_message')
        if not decision.allowed:
            return PhoneInbox(False, decision.reason)
        agent = (state.get('agents') or {}).get(agent_id)
        if not isinstance(agent, dict):
            return PhoneInbox(False, 'unknown agent',
                              agent_id=agent_id)
        return PhoneInbox(True, agent_id=agent_id,
                          messages=self._unread_phone(agent))

    def read(self, state, agent_id, mid):
        """Mark one phone message read, when Theo allows the
        agent to read messages, and return what is still
        unread. A message id that is not one of her phone
        messages is a denial, not a silent no-op, so a stale
        or guessed id cannot be used to probe mailboxes."""
        decision = self.policy.evaluate(
            state, agent_id, 'read_message',
            {'message_id': mid})
        if not decision.allowed:
            return PhoneInbox(False, decision.reason)
        agent = (state.get('agents') or {}).get(agent_id)
        if not isinstance(agent, dict):
            return PhoneInbox(False, 'unknown agent',
                              agent_id=agent_id)
        for entry in (agent.get('mailbox') or []):
            if (entry.get('channel') == 'phone'
                    and entry.get('mid') == mid):
                entry['read'] = True
                return PhoneInbox(
                    True, agent_id=agent_id,
                    messages=self._unread_phone(agent))
        return PhoneInbox(False, 'unknown message',
                          agent_id=agent_id)

    def mark_all_read(self, state, agent_id):
        """Mark every phone message read at once, when Theo
        allows the agent to read messages."""
        decision = self.policy.evaluate(
            state, agent_id, 'read_message')
        if not decision.allowed:
            return PhoneInbox(False, decision.reason)
        agent = (state.get('agents') or {}).get(agent_id)
        if not isinstance(agent, dict):
            return PhoneInbox(False, 'unknown agent',
                              agent_id=agent_id)
        for entry in (agent.get('mailbox') or []):
            if entry.get('channel') == 'phone':
                entry['read'] = True
        return PhoneInbox(True, agent_id=agent_id,
                          messages=[])

    @staticmethod
    def _unread_phone(agent):
        """The phone-channel entries in `agent`'s mailbox
        that no one has read yet, in mailbox order."""
        return [entry for entry in (agent.get('mailbox') or [])
                if entry.get('channel') == 'phone'
                and not entry.get('read', False)]
