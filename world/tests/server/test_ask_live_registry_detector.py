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
