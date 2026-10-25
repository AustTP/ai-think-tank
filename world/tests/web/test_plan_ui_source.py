"""The board exposes the plan approval gate and its three actions."""
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def test_plan_board_uses_the_approval_endpoints():
    html = _read('world/index.html')
    assert 'id="planApprovals"' in html
    assert '/api/intent/plan-big-task' in html
    assert '/api/plans?status=pending' in html
    assert '/approve' in html and '/reject' in html and '/revise' in html


def test_plan_board_renders_route_and_budget():
    html = _read('world/index.html')
    assert 'renderPlanApprovals' in html
    assert 'estimatedBudgetUsd' in html
    assert 'No plans waiting for approval' in html
