"""Tests for the JIRA-like issue register (PREFIX-0128) (2026-09-25).

The village gained a first-class, per-team-issued ticket store fed by
POST /api/intent/issues. This covers the pure sim.py helpers:

1. Required-field validation: `file_issue` refuses to file when any of
   team_id / type / summary / feature / reporter_id is missing, unknown type,
   or the team is unknown.
2. Per-team counter independence, honest keys (DEV-1, DEV-12 -- no leading
   zeros; padding reads as an artificial sequence and is harder to scan), and a
   distinct one-line `title` that defaults to `summary`.
3. Counter survives a "restart" -- a fresh state that re-seeds the same team
   appends the next number, never a collision.
4. Filing also writes into the backlogRequests pipe (tagged issueKey + teamId)
   so the scrum-master refinement ceremony can groom the card into a sprint.
5. `_refinement_team` routes a teamId-tagged request to that team even though
   the filer is the player (no director to derive a team from) -- and still
   falls back to the filer's reporting tree for ordinary work-requests.
6. Prefix set/get: explicit prefix wins over the derived default; clearing
   falls back; invalid prefixes are rejected.
7. Status transition mirrors onto the linked pending backlog request.
8. Description normalization: the two canonical Jira description blocks (user
   story "As a/I want to/so that" and acceptance criteria "Given/When/Then")
   are normalized; a malformed block is dropped rather than stored.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim  # noqa: E402

_NOW_MS = 1_725_000_000_000  # 2026-09


def _state(**over):
    base = {
        'sim': {'owner': 'server'},
        'agentRoster': [
            {'id': 'dev', 'name': 'Dev', 'isDirector': True, 'director': None},
            {'id': 'nadia', 'name': 'Nadia', 'isDirector': False, 'director': 'dev'},
            {'id': 'priya', 'name': 'Priya', 'isDirector': False, 'director': 'dev'},
        ],
        'agents': {
            'dev': {'id': 'dev', 'name': 'Dev'},
            'nadia': {'id': 'nadia', 'name': 'Nadia'},
            'priya': {'id': 'priya', 'name': 'Priya'},
        },
        'teams': [
            {'id': 'dev', 'name': "Dev's Crew", 'directorId': 'dev',
             'purpose': 'X', 'members': ['nadia', 'priya'], 'createdAt': 1},
        ],
        'researchTopics': [],
        'tasks': {},
        'workQueue': [],
    }
    base.update(over)
    return base


class IssueValidation(unittest.TestCase):
    def test_requires_team_id(self):
        state = _state()
        self.assertIsNone(sim.file_issue(state, '', 'story', 'do a thing',
                                         'checkout', 'player', now_ms=_NOW_MS))

    def test_requires_type(self):
        state = _state()
        self.assertIsNone(sim.file_issue(state, 'dev', '', 'do a thing',
                                         'checkout', 'player', now_ms=_NOW_MS))

    def test_requires_summary(self):
        state = _state()
        self.assertIsNone(sim.file_issue(state, 'dev', 'story', '   ',
                                         'checkout', 'player', now_ms=_NOW_MS))

    def test_requires_feature(self):
        state = _state()
        self.assertIsNone(sim.file_issue(state, 'dev', 'story', 'do a thing',
                                         '   ', 'player', now_ms=_NOW_MS))

    def test_requires_reporter(self):
        state = _state()
        self.assertIsNone(sim.file_issue(state, 'dev', 'story', 'do a thing',
                                         'checkout', '', now_ms=_NOW_MS))

    def test_rejects_unknown_type(self):
        state = _state()
        self.assertIsNone(sim.file_issue(state, 'dev', 'saga', 'do a thing',
                                         'checkout', 'player', now_ms=_NOW_MS))

    def test_rejects_unknown_team(self):
        state = _state()
        self.assertIsNone(sim.file_issue(state, 'ghost-team', 'story',
                                         'do a thing', 'checkout', 'player',
                                         now_ms=_NOW_MS))


class IssueSequencing(unittest.TestCase):
    def test_per_team_counters_independent(self):
        state = _state()
        # Add a second team to prove counters don't collide.
        state['teams'].append({'id': 'nora', 'name': "Nora's Team",
                               'directorId': 'nora', 'purpose': 'Y',
                               'members': [], 'createdAt': 1})
        state['agentRoster'].append({'id': 'nora', 'name': 'Nora',
                                     'isDirector': True, 'director': None})
        dev1 = sim.file_issue(state, 'dev', 'story', 'a', 'checkout',
                              'player', now_ms=_NOW_MS)
        nora1 = sim.file_issue(state, 'nora', 'spike', 'b', 'search',
                               'player', now_ms=_NOW_MS)
        dev2 = sim.file_issue(state, 'dev', 'bug', 'c', 'checkout',
                              'player', now_ms=_NOW_MS)
        self.assertEqual(dev1['key'], 'DEV-1')
        self.assertEqual(nora1['key'], 'NORA-1')
        self.assertEqual(dev2['key'], 'DEV-2')
        self.assertEqual(dev1['teamKey'], 'DEV')
        self.assertEqual(nora1['teamKey'], 'NORA')

    def test_collision_free_across_restart(self):
        # First process files up to 12, then a "restarted" state with the
        # same counter continues at 13 (a stored issue scan never reissues).
        state = _state()
        state.setdefault('issueCounters', {})['DEV'] = 12
        state.setdefault('issues', {})['DEV-12'] = {'key': 'DEV-12',
                                                    'teamId': 'dev'}
        issued = sim.file_issue(state, 'dev', 'story', 'z', 'checkout',
                                'player', now_ms=_NOW_MS)
        self.assertEqual(issued['key'], 'DEV-13')

    def test_no_leading_zeros(self):
        # Story keys have no zero padding -- DEV-1, DEV-2, ..., DEV-12 (not
        # DEV-0001). Leading zeros are dropped per product decision: padding
        # makes keys harder to scan and reads as an artificial sequence.
        state = _state()
        state.setdefault('issueCounters', {})['DEV'] = 11
        issued = sim.file_issue(state, 'dev', 'story', 'z', 'checkout',
                                'player', now_ms=_NOW_MS)
        self.assertEqual(issued['key'], 'DEV-12')

    def test_title_field(self):
        # A player-supplied title is kept distinct from the summary -- it is the
        # one-line scan headline. When omitted it falls back to the summary.
        state = _state()
        explicit = sim.file_issue(state, 'dev', 'story', 'do the thing',
                                  'checkout', 'player', title='Card headline',
                                  now_ms=_NOW_MS)
        self.assertEqual(explicit['title'], 'Card headline')
        self.assertEqual(explicit['summary'], 'do the thing')
        self.assertNotEqual(explicit['title'], explicit['summary'])
        fallback = sim.file_issue(state, 'dev', 'bug', 'just a summary',
                                  'checkout', 'player', now_ms=_NOW_MS)
        self.assertEqual(fallback['title'], 'just a summary')
        # Whitespace-only titles fall back to the summary rather than blank.
        blank = sim.file_issue(state, 'dev', 'spike', 'w title', 'checkout',
                               'player', title='   ', now_ms=_NOW_MS)
        self.assertEqual(blank['title'], 'w title')


class IssueFeedsBacklog(unittest.TestCase):
    def test_filing_writes_backlog_request(self):
        state = _state()
        issue = sim.file_issue(state, 'dev', 'story', 'Ship the thing', 'checkout',
                               'player', now_ms=_NOW_MS)
        reqs = state.get('backlogRequests') or []
        self.assertEqual(len(reqs), 1)
        req = reqs[0]
        self.assertEqual(req['issueKey'], issue['key'])
        self.assertEqual(req['teamId'], 'dev')
        self.assertEqual(req['origin'], 'jira_issue')
        self.assertEqual(req['status'], 'pending')
        self.assertIn(issue['key'], req['id'])
        # Title is decorated with the issue type.
        self.assertEqual(req['title'], '[STORY] Ship the thing')

    def test_refinement_team_routes_player_filed_issue(self):
        # The player files an issue for the dev team: no director graph to
        # derive a team from, so the explicit teamId must route it home.
        state = _state()
        sim.file_issue(state, 'dev', 'story', 'Ship the thing', 'checkout',
                       'player', now_ms=_NOW_MS)
        req = state['backlogRequests'][0]
        team = sim._refinement_team(state, req)
        self.assertIsNotNone(team)
        self.assertEqual(team['id'], 'dev')


class IssuePrefix(unittest.TestCase):
    def test_derived_default_prefix(self):
        state = _state()
        self.assertEqual(sim.team_prefix(state, 'dev'), 'DEV')

    def test_explicit_prefix_wins(self):
        state = _state()
        applied = sim.set_team_prefix(state, 'dev', 'DSPPZ')
        self.assertEqual(applied, 'DSPPZ')
        issued = sim.file_issue(state, 'dev', 'story', 'a', 'checkout',
                                'player', now_ms=_NOW_MS)
        self.assertEqual(issued['key'], 'DSPPZ-1')

    def test_clear_prefix_falls_back_to_default(self):
        state = _state()
        state['teams'][0]['prefix'] = 'DSPPZ'
        applied = sim.set_team_prefix(state, 'dev', '')
        self.assertEqual(applied, 'DEV')

    def test_rejects_too_long_prefix(self):
        state = _state()
        self.assertIsNone(sim.set_team_prefix(state, 'dev', 'TOOLONG'))
        self.assertEqual(sim.team_prefix(state, 'dev'), 'DEV')

    def test_unknown_team(self):
        state = _state()
        self.assertIsNone(sim.set_team_prefix(state, 'ghost', 'ABC'))


class IssueStatus(unittest.TestCase):
    def test_status_transition_mirrors_to_backlog(self):
        state = _state()
        issue = sim.file_issue(state, 'dev', 'story', 'Ship it', 'checkout',
                               'player', now_ms=_NOW_MS)
        updated = sim.set_issue_status(state, issue['key'], 'in_progress')
        self.assertEqual(updated['status'], 'in_progress')
        req = state['backlogRequests'][0]
        self.assertEqual(req['status'], 'accepted')
        updated = sim.set_issue_status(state, issue['key'], 'done')
        req = state['backlogRequests'][0]
        self.assertEqual(req['status'], 'resolved')

    def test_unknown_issue_or_invalid_status(self):
        state = _state()
        self.assertIsNone(sim.set_issue_status(state, 'DEV-0001', 'bogus'))
        self.assertIsNone(sim.set_issue_status(state, 'ghost', 'done'))


class IssueDescription(unittest.TestCase):
    def test_user_story_block_normalized(self):
        state = _state()
        issue = sim.file_issue(state, 'dev', 'story', 'Ship checkout',
                               'checkout', 'player',
                               description="As a shopper, I want to pay once, "
                                           "so that I can skip the queue",
                               now_ms=_NOW_MS)
        self.assertEqual(
            issue['userStory'],
            'As a shopper, I want to pay once, so that I can skip the queue')
        self.assertIsNone(issue['acceptanceCriteria'])

    def test_acceptance_criteria_block_normalized(self):
        state = _state()
        issue = sim.file_issue(state, 'dev', 'story', 'Ship checkout',
                               'checkout', 'player',
                               description={'acceptanceCriteria':
                                            'Given a cart, When I check out, '
                                            'Then I see a receipt'},
                               now_ms=_NOW_MS)
        self.assertIsNone(issue['userStory'])
        self.assertEqual(
            issue['acceptanceCriteria'],
            'Given a cart, When I check out, Then I see a receipt')

    def test_both_blocks_dict(self):
        state = _state()
        issue = sim.file_issue(state, 'dev', 'story', 'Ship checkout',
                               'checkout', 'player',
                               description={
                                   'userStory': 'As a shopper, I want to pay once, '
                                                'so that I can skip the queue',
                                   'acceptanceCriteria':
                                       'Given a cart, When I check out, '
                                       'Then I see a receipt'},
                               now_ms=_NOW_MS)
        self.assertIsNotNone(issue['userStory'])
        self.assertIsNotNone(issue['acceptanceCriteria'])

    def test_malformed_block_dropped(self):
        state = _state()
        issue = sim.file_issue(state, 'dev', 'story', 'Ship checkout',
                               'checkout', 'player',
                               description="this is not a real story",
                               now_ms=_NOW_MS)
        self.assertIsNone(issue['userStory'])
        self.assertIsNone(issue['acceptanceCriteria'])


class IssueListing(unittest.TestCase):
    def test_list_issues_newest_first_and_team_filter(self):
        state = _state()
        state['teams'].append({'id': 'nora', 'name': "Nora's Team",
                               'directorId': 'nora', 'purpose': 'Y',
                               'members': [], 'createdAt': 1})
        state['agentRoster'].append({'id': 'nora', 'name': 'Nora',
                                     'isDirector': True, 'director': None})
        sim.file_issue(state, 'dev', 'story', 'old', 'checkout', 'player', now_ms=_NOW_MS)
        sim.file_issue(state, 'nora', 'spike', 'new', 'search', 'player', now_ms=_NOW_MS + 1)
        all_ = sim.list_issues(state)
        self.assertEqual([i['summary'] for i in all_], ['new', 'old'])
        dev_only = sim.list_issues(state, 'dev')
        self.assertEqual([i['summary'] for i in dev_only], ['old'])


if __name__ == '__main__':
    unittest.main()