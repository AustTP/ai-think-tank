"""Cross-agent escalation dedupe + the delegable owner-safety floor.

2026-11-04: ten agents running the SAME shared-prompt shell command produced
ten pending 'owner safety decision' records (the question embeds the agent
name, so the exact-match dedupe never hit) and ten pings to the owner. The
operator asked for these to route to the internal chain instead of the
phone. Two changes under test here: (1) create_escalation dedupes on a
normalized key that strips the requester's name, recording extra requesters
on the winner's alsoWaiting list; (2) the 'owner safety decision' floor is
the highest DELEGABLE bar (0.95), so the director loop -- supervisor rungs,
recursive directors, chief of staff, judge cross-check -- resolves them and
the human is only notified on the loop's give-up.
"""
import json
import os
import tempfile
import unittest
import unittest.mock

import serve


class EscalationDedupe(unittest.TestCase):
    def _esc_env(self):
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        os.unlink(path)
        return unittest.mock.patch.object(serve, 'ESCALATIONS_PATH', path), path

    def test_same_command_two_agents_one_record(self):
        q = ('Agent marcus needs a decision on this This command: '
             'budget=15000; total=0; for f in *.html; do wc -c < "$f"; done')
        patcher, path = self._esc_env()
        with patcher as _:
            first = serve.create_escalation('owner safety decision', q,
                                            agent_id='marcus')
            second = serve.create_escalation(
                'owner safety decision',
                q.replace('Agent marcus', 'Agent giles'), agent_id='giles')
        self.assertEqual(first, second)
        data = json.load(open(path))
        winner = data[first]
        self.assertEqual(winner.get('alsoWaiting'), ['giles'])
        # The winner's own record keeps the original question (audit trail).
        self.assertIn('marcus', winner['question'])
        self.assertEqual(len(data), 1)

    def test_different_commands_stay_separate(self):
        patcher, path = self._esc_env()
        with patcher as _:
            a = serve.create_escalation(
                'owner safety decision',
                'Agent marcus needs a decision on this This command: rm -rf /tmp/x',
                agent_id='marcus')
            b = serve.create_escalation(
                'owner safety decision',
                'Agent ruth needs a decision on this This command: curl http://x.test',
                agent_id='ruth')
        self.assertNotEqual(a, b)

    def test_resolved_escalation_does_not_dedup(self):
        q = 'Agent marcus needs a decision on this This command: ls'
        patcher, path = self._esc_env()
        with patcher as _:
            first = serve.create_escalation('owner safety decision', q,
                                            agent_id='marcus')
            data = json.load(open(serve.ESCALATIONS_PATH))
            data[first]['status'] = 'denied'
            json.dump(data, open(serve.ESCALATIONS_PATH, 'w'))
            second = serve.create_escalation('owner safety decision', q,
                                             agent_id='ruth')
        self.assertNotEqual(first, second)

    def test_owner_safety_floor_is_delegable_high(self):
        # 1.0 barred the kind from the director loop entirely (human-only).
        # The operator wants the internal chain to own it; 0.95 is the highest
        # delegable floor and sits above the 0.85 'unsure' bar.
        self.assertEqual(serve._escalation_floor('owner safety decision'), 0.95)
        self.assertGreater(
            serve._escalation_floor('owner safety decision'),
            serve._escalation_floor('unsure safety decision'))
        # Genuine blocks stay human-only.
        self.assertEqual(serve._escalation_floor('blocked command'), 1.0)


if __name__ == '__main__':
    unittest.main()
