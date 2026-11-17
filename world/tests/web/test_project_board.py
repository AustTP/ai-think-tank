"""Tests for the read-only project board."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import content
import serve
import agents.pulse_ui as pulse_ui


class ProjectBoard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='board-test-')
        self.library = os.path.join(self.tmp, 'library')
        os.makedirs(self.library)
        self.state = {'tasks': {
            't1': {'title': 'Open research', 'status': 'working', 'assignedTo': 'cora',
                   'teamId': 'nora', 'projectLabel': 'Atlas', 'priority': 'high',
                   'budgetUsd': 1.0, 'spentUsd': 0.2, 'reportPath': 'archive/a.md',
                   'acceptanceCriteria': 'A source', 'parentTaskId': 'sup1', 'updatedAt': 3},
            't2': {'title': 'Done report', 'status': 'done', 'assignedTo': 'leo',
                   'teamId': 'nora', 'projectLabel': 'Atlas', 'reportPath': 'archive/b.md',
                   'updatedAt': 2},
            't3': {'title': 'Other team', 'status': 'queued', 'teamId': 'ruth',
                   'projectLabel': 'Other', 'updatedAt': 1},
        }}
        open(os.path.join(self.library, 'archive'), 'w').close() if False else None
        os.makedirs(os.path.join(self.library, 'archive'), exist_ok=True)
        with open(os.path.join(self.library, 'archive', 'a.md'), 'w') as f:
            f.write('x')
        self.patcher = unittest.mock.patch.multiple(
            serve, LIBRARY_DIR=self.library,
            get_state_from_db=lambda *a, **k: self.state,
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_open_filter_and_artifact(self):
        out = json.loads(content.project_board({'status': 'open'}))
        self.assertEqual(out['counts']['open'], 2)
        self.assertEqual([t['taskId'] for t in out['tasks']], ['t1', 't3'])
        t1 = out['tasks'][0]
        self.assertTrue(t1['artifactExists'])
        self.assertEqual(t1['parentTaskId'], 'sup1')
        self.assertEqual(t1['spentUsd'], 0.2)

    def test_team_and_project_filters(self):
        out = json.loads(content.project_board({'team': 'nora', 'project': 'atlas', 'status': 'all'}))
        self.assertEqual({t['taskId'] for t in out['tasks']}, {'t1', 't2'})
        done = json.loads(content.project_board({'status': 'done'}))
        self.assertEqual([t['taskId'] for t in done['tasks']], ['t2'])


NODE = shutil.which('node')

BOARD_TASKS = [
    {'taskId': 't1', 'title': 'Open research', 'status': 'working', 'assignedTo': 'cora',
     'teamId': 'nora', 'projectLabel': 'Atlas', 'updatedAt': 30},
    {'taskId': 't2', 'title': 'Done report', 'status': 'done', 'assignedTo': 'leo',
     'teamId': 'nora', 'projectLabel': 'Atlas', 'updatedAt': 20},
    {'taskId': 't3', 'title': 'Other team', 'status': 'queued', 'teamId': 'ruth',
     'projectLabel': 'Other', 'updatedAt': 10},
    {'taskId': 't4', 'title': 'Loose ends', 'status': 'working', 'assignedTo': 'cora',
     'teamId': '', 'projectLabel': '', 'updatedAt': 5},
    {'taskId': 't5', 'title': 'Cancelled extra', 'status': 'cancelled', 'teamId': 'nora',
     'projectLabel': 'Atlas', 'updatedAt': 1},
]


def extract_board_groups_js():
    html = pulse_ui.PULSE_HTML
    start = html.index('function boardGroups(')
    end = html.index('function renderBoardSections(')
    return html[start:end]


def run_board_groups(tasks, mode='team', team_filter='', project_filter=''):
    script = (
        'const tasks = ' + json.dumps(tasks) + ';\n'
        'const mode = ' + json.dumps(mode) + ';\n'
        'const teamFilter = ' + json.dumps(team_filter) + ';\n'
        'const projectFilter = ' + json.dumps(project_filter) + ';\n'
        + extract_board_groups_js() + '\n'
        'console.log(JSON.stringify(boardGroups(tasks, mode, teamFilter, projectFilter)));'
    )
    out = subprocess.run([NODE, '-e', script], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise AssertionError('boardGroups failed: ' + out.stderr)
    return json.loads(out.stdout.strip())


@unittest.skipIf(NODE is None, 'node not available')
class PulseBoardTeamGroups(unittest.TestCase):
    def test_team_sections_render_with_their_titles(self):
        groups = run_board_groups(BOARD_TASKS, mode='team')
        self.assertEqual([g['key'] for g in groups], ['nora', 'ruth', ''])
        by_team = {g['key']: [t['title'] for t in g['tasks']] for g in groups}
        self.assertEqual(by_team['nora'], ['Open research', 'Done report', 'Cancelled extra'])
        self.assertEqual(by_team['ruth'], ['Other team'])
        self.assertEqual(by_team[''], ['Loose ends'])

    def test_empty_team_id_lands_in_unassigned_section_last(self):
        groups = run_board_groups(BOARD_TASKS, mode='team')
        unassigned = groups[-1]
        self.assertEqual(unassigned['key'], '')
        self.assertEqual(unassigned['label'], 'Unassigned')
        self.assertEqual([t['taskId'] for t in unassigned['tasks']], ['t4'])
        self.assertEqual(unassigned['open'], 1)
        self.assertEqual(unassigned['done'], 0)

    def test_default_mode_is_team_and_project_mode_is_reachable(self):
        html = pulse_ui.PULSE_HTML
        self.assertIn("boardMode = 'team'", html)
        self.assertIn('id="groupTeam" class="on" data-group="team"', html)
        self.assertIn('id="groupProject" data-group="project"', html)
        self.assertIn("setBoardMode('project')", html)
        self.assertIn("setBoardMode('team')", html)
        proj_groups = run_board_groups(BOARD_TASKS, mode='project')
        self.assertEqual([g['label'] for g in proj_groups], ['Atlas', 'Other', '(no project)'])

    def test_per_team_open_done_counts_render(self):
        groups = run_board_groups(BOARD_TASKS, mode='team')
        counts = {g['key']: (g['open'], g['done']) for g in groups}
        self.assertEqual(counts['nora'], (1, 1))
        self.assertEqual(counts['ruth'], (1, 0))
        self.assertEqual(counts[''], (1, 0))
        html = pulse_ui.PULSE_HTML
        self.assertIn("' open / '", html)
        self.assertIn("' done'", html)

    def test_sections_sort_by_recent_activity(self):
        tasks = [
            {'taskId': 'a', 'title': 'older team', 'status': 'working', 'teamId': 'old',
             'projectLabel': 'P', 'updatedAt': 1},
            {'taskId': 'b', 'title': 'newer team', 'status': 'working', 'teamId': 'new',
             'projectLabel': 'P', 'updatedAt': 9},
        ]
        groups = run_board_groups(tasks, mode='team')
        self.assertEqual([g['key'] for g in groups], ['new', 'old'])

    def test_team_and_project_filters_narrow_sections(self):
        groups = run_board_groups(BOARD_TASKS, mode='team', team_filter='nora')
        self.assertEqual(len(groups), 1)
        self.assertEqual([t['taskId'] for t in groups[0]['tasks']], ['t1', 't2', 't5'])
        for mode in ('team', 'project'):
            groups = run_board_groups(BOARD_TASKS, mode=mode, project_filter='atlas')
            self.assertTrue(all(t['projectLabel'] == 'Atlas' for g in groups for t in g['tasks']))
            self.assertTrue(groups)

    def test_page_markup_has_group_markers_and_filters(self):
        html = pulse_ui.PULSE_HTML
        self.assertIn("sec.setAttribute('data-group', boardMode)", html)
        self.assertIn("sec.setAttribute('data-key'", html)
        self.assertIn("'Unassigned'", html)
        self.assertIn("'(no project)'", html)
        self.assertIn("'All teams'", html)
        self.assertIn('boardTeam', html)
        self.assertIn('boardProject', html)
        self.assertIn('renderBoardSections()', html)
        self.assertIn("'project '+t.projectLabel", html)


if __name__ == '__main__':
    unittest.main()
