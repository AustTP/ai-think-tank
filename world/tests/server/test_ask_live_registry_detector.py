"""2026-10-30 Tier-1 probe finding: a live-data ask got a fabricated
"restricted by policy approvals" answer with ZERO registry tool calls. The
fix forces list_services on turn 1 for live-data questions. These tests pin
the detector so the force cannot silently regress."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve  # noqa: E402


def test_probe2_question_matches():
    q = ('Tier-1 probe 2: Pull live data right now and give me three numbers with sources: '
         '(1) how many cafes/nodes tagged amenity=cafe exist in the bbox -80.95,35.15,-80.70,35.40 '
         '(Overpass), (2) how many current traffic incidents are in that same bbox '
         '(TomTom incidentDetails), (3) the next 3 Ticketmaster Discovery events near '
         'latlong 35.2271,-80.8431 radius 25 miles.')
    assert serve._ask_wants_live_registry_data(q)


def test_live_phrasings_match():
    for q in ("What's the current traffic in Charlotte?",
              "How many cafes are open right now near uptown?",
              "Give me today's event count near the arena",
              "Use the overpass service to count parks",
              "as of now, what is the incident number on I-77?"):
        assert serve._ask_wants_live_registry_data(q), q


def test_plain_and_briefing_questions_do_not_match():
    for q in ("What happened in my world overnight? Summarize the so-what.",
              "What is the team status?",
              "Write a short story about a lighthouse.",
              "What did the CEO of ideabrowser.com announce last week?",
              "read my mailbox"):
        assert not serve._ask_wants_live_registry_data(q), q


def test_ask_tools_seed_grants_api_call_when_caller_passes_nothing():
    assert serve._seed_ask_approved_tools(None) == ['api_call']


def test_ask_tools_explicit_lists_keep_their_narrowing():
    assert serve._seed_ask_approved_tools([]) == []
    assert serve._seed_ask_approved_tools(['local_file']) == ['local_file']
    assert serve._seed_ask_approved_tools('search_web') == ['search_web']
    assert serve._seed_ask_approved_tools(['api_call', 'local_file', '']) == \
        ['api_call', 'local_file']


def test_ask_task_policy_admits_api_call_with_the_seed():
    # The exact gate the ask loop applies (task_tool_policy on the seeded
    # task): the registry gateway must pass, an unrelated commit tool must not.
    policy = serve.task_tool_policy(
        {'approvedTools': serve._seed_ask_approved_tools(None),
         'taskType': 'ask', 'allowCommit': False})
    assert policy('api_call') is True
    assert policy('send_mail') is False
    assert policy('search_web') is True
