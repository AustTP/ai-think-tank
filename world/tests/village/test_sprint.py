"""Tests for sprint planning primitives (asks 8 and 9)."""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from sprint import (  # noqa: E402
    CONFIRM_SCOPE_THRESHOLD,
    DEFAULT_BATCH_SIZE,
    DiscoveryBrief,
    WorkItem,
    plan_sprint,
    refine_backlog,
    role_for_sprint,
    run_discovery,
    youtube_items,
)


def make_items(count, captions=None):
    items = []
    for n in range(count):
        available = None if captions is None else (n % 2 == 0)
        items.append(
            WorkItem(
                item_id=f"vid-{n:04d}",
                title=f"video {n}",
                duration_minutes=10.0,
                transcript_available=available,
            )
        )
    return items


class TestRoleForSprint(unittest.TestCase):
    def test_director_for_fewer_than_four_agents(self):
        for count in (1, 2, 3):
            self.assertEqual(role_for_sprint(count), "director")

    def test_scrum_master_for_four_or_more_agents(self):
        for count in (4, 5, 8):
            self.assertEqual(role_for_sprint(count), "scrum master")


class TestRunDiscovery(unittest.TestCase):
    def test_counts_transcript_availability(self):
        items = make_items(6, captions=True)
        brief = run_discovery(items, "director", pages=3)
        self.assertEqual(brief.total_items, 6)
        self.assertEqual(brief.with_transcript, 3)
        self.assertEqual(brief.missing_transcript, 3)
        self.assertEqual(brief.unknown_transcript, 0)
        self.assertEqual(brief.total_minutes, 60.0)
        self.assertEqual(brief.pages, 3)
        self.assertEqual(brief.role, "director")
        self.assertEqual(brief.unknowns, [])

    def test_unknowns_recorded(self):
        items = make_items(2, captions=None)
        brief = run_discovery(items, "director")
        self.assertEqual(brief.unknown_transcript, 2)
        self.assertEqual(len(brief.unknowns), 2)
        self.assertTrue(any("unknown for 2" in u for u in brief.unknowns))
        self.assertTrue(any("pagination" in u for u in brief.unknowns))

    def test_small_scope_does_not_require_confirmation(self):
        items = make_items(5, captions=True)
        brief = run_discovery(items, "director")
        self.assertFalse(brief.scope_confirmation_required)
        self.assertIsNone(brief.confirmation_question)

    def test_large_scope_requires_confirmation(self):
        items = make_items(CONFIRM_SCOPE_THRESHOLD + 1, captions=True)
        brief = run_discovery(items, "director")
        self.assertTrue(brief.scope_confirmation_required)
        self.assertIn("101 items", brief.confirmation_question)


class TestRefineBacklog(unittest.TestCase):
    def test_chunks_by_batch_size(self):
        items = make_items(60, captions=True)
        brief = run_discovery(items, "director", pages=2)
        stories = refine_backlog(items, brief, batch_size=25)
        self.assertEqual(len(stories), 3)
        self.assertEqual([len(s.item_ids) for s in stories], [25, 25, 10])
        self.assertEqual(stories[0].story_id, 1)
        self.assertEqual(stories[-1].estimate_minutes, 100.0)

    def test_default_batch_size(self):
        self.assertEqual(DEFAULT_BATCH_SIZE, 25)


class TestPlanSprint(unittest.TestCase):
    def test_plan_uses_director_role(self):
        items = make_items(4, captions=True)
        plan = plan_sprint(items, agent_count=2, pages=1)
        self.assertEqual(plan.role, "director")
        self.assertEqual(plan.brief.role, "director")
        self.assertEqual(len(plan.stories), 1)

    def test_plan_uses_scrum_master_role(self):
        items = make_items(4, captions=True)
        plan = plan_sprint(items, agent_count=4, pages=1)
        self.assertEqual(plan.role, "scrum master")


class TestYoutubeItems(unittest.TestCase):
    def test_builds_work_items_from_video_ids(self):
        def caption_check(video_id):
            return video_id != "vid-002"

        items = youtube_items(
            ["vid-001", "vid-002", "vid-003"],
            caption_check,
            durations={"vid-001": 12.5},
        )
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].duration_minutes, 12.5)
        self.assertEqual(items[1].duration_minutes, 0.0)
        self.assertEqual(
            [i.transcript_available for i in items], [True, False, True]
        )

    def test_brief_for_youtube_items(self):
        items = youtube_items(
            ["a", "b"], lambda vid: True, durations={"a": 5.0, "b": 7.0}
        )
        brief = run_discovery(items, "director")
        self.assertEqual(brief.total_minutes, 12.0)
        self.assertEqual(brief.with_transcript, 2)
        self.assertIsInstance(brief, DiscoveryBrief)


if __name__ == "__main__":
    unittest.main()
