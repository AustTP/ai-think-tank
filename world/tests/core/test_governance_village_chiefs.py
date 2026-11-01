"""Village chiefs of staff: appointment invariant (operator direction).

Every village gets a chief of staff AT CREATION (the per-village deputy
between the admin and the village's senior directors), and the first
village's chief is appointed through the same path. The roster tier is
explicit -- `villageChief: True` + `chiefOfVillage: <vid>` -- never
emergent; exactly one chief per village; the village's other senior
directors re-parent under the chief.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.governance as g  # noqa: E402
import sim as h  # noqa: E402


def _state():
    return {
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True, 'director': None},
            {'id': 'd1', 'director': 'theo'},
            {'id': 'd2', 'director': 'theo'},
            {'id': 'd3', 'director': 'theo'},
            {'id': 'w1', 'director': 'd1'},
        ],
        'agents': {
            'theo': {'name': 'Theo', 'villageId': 'main'},
            'd1': {'name': 'D One', 'villageId': 'main'},
            'd2': {'name': 'D Two', 'villageId': 'main'},
            'd3': {'name': 'D Three', 'villageId': 'main'},
            'w1': {'name': 'W One', 'villageId': 'main'},
        },
        'teams': [
            {'id': 't1', 'directorId': 'd1', 'scrumMasterId': 'w1'},
            {'id': 't2', 'directorId': 'd2', 'scrumMasterId': 'w1'},
            {'id': 't3', 'directorId': 'd3', 'scrumMasterId': 'w1'},
        ],
        'villages': [{'id': 'main', 'name': 'Main'}],
    }


def test_auto_pick_prefers_a_senior_director_of_the_village():
    s = _state()
    chief = h.appoint_village_chief(s, 'main')
    assert chief == 'd1'  # first teams-row director, teams order
    entry = next(r for r in s['agentRoster'] if r['id'] == 'd1')
    assert entry['villageChief'] is True
    assert entry['chiefOfVillage'] == 'main'
    assert entry['role'] == 'chief_of_staff'
    assert entry['director'] == 'theo'  # reports to the admin


def test_senior_directors_reparent_under_the_chief():
    s = _state()
    h.appoint_village_chief(s, 'main')
    by_id = {r['id']: r for r in s['agentRoster']}
    assert by_id['d2']['director'] == 'd1'
    assert by_id['d3']['director'] == 'd1'
    # A deeper tree keeps its own shape: w1 still reports to d1.
    assert by_id['w1']['director'] == 'd1'


def test_explicit_appointment_validates_village_and_membership():
    s = _state()
    assert h.appoint_village_chief(s, 'nope', 'd1') is None
    assert h.appoint_village_chief(s, 'main', 'ghost') is None
    # A worker (not a director) is eligible when named explicitly.
    assert h.appoint_village_chief(s, 'main', 'w1') == 'w1'


def test_reappointment_replaces_the_previous_chief():
    s = _state()
    assert h.appoint_village_chief(s, 'main', 'd1') == 'd1'
    assert h.appoint_village_chief(s, 'main', 'd2') == 'd2'
    by_id = {r['id']: r for r in s['agentRoster']}
    assert not by_id['d1'].get('villageChief')
    assert not by_id['d1'].get('chiefOfVillage')
    assert by_id['d2']['villageChief'] is True
    assert g.chief_of_village(s, 'main') == 'd2'


def test_one_chief_per_agent_across_villages():
    s = _state()
    s['villages'].append({'id': 'vlg-2', 'name': 'Second'})
    s['agents']['d1']['villageId'] = 'vlg-2'
    assert h.appoint_village_chief(s, 'main') == 'd2'  # d1 is another village's
    assert h.appoint_village_chief(s, 'main', 'd1') is None


def test_no_eligible_candidate_returns_none():
    s = _state()
    s['teams'] = []  # no senior directors at all
    assert h.appoint_village_chief(s, 'main') is None


def test_villages_without_chief():
    s = _state()
    s['villages'].append({'id': 'vlg-2', 'name': 'Second'})
    assert g.villages_without_chief(s) == ['main', 'vlg-2']
    h.appoint_village_chief(s, 'main')
    assert g.villages_without_chief(s) == ['vlg-2']
    # An explicit appointment still requires village membership.
    s['agents']['d2']['villageId'] = 'vlg-2'
    assert h.appoint_village_chief(s, 'vlg-2', 'd2') == 'd2'
    assert g.villages_without_chief(s) == []


def test_create_village_appoints_a_chief():
    s = _state()
    h.appoint_village_chief(s, 'main')  # the first village's chief, up front
    assert g.villages_without_chief(s) == []
    record = h.create_village(s, 'Coastal')
    assert record is not None
    # No directors belong to the new village yet -> chief-less until one
    # moves in; the invariant is flagged, not faked.
    assert g.villages_without_chief(s) == [record['id']]
    s['agents']['d3']['villageId'] = record['id']
    h.appoint_village_chief(s, record['id'])
    assert g.chief_of_village(s, record['id']) == 'd3'
    assert g.villages_without_chief(s) == []


def test_chiefs_never_report_to_themselves_and_admin_is_stable():
    s = _state()
    s['agents']['theo']['villageId'] = 'main'
    chief = h.appoint_village_chief(s, 'main', 'theo')
    assert chief == 'theo'
    entry = next(r for r in s['agentRoster'] if r['id'] == 'theo')
    assert entry.get('director') is None  # the admin reports to nobody
