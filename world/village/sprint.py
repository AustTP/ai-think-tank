"""Sprint planning primitives (asks 8 and 9).

WorkItems are the unit of planned work. run_discovery turns a
list of items into a DiscoveryBrief that records what is known
(transcript availability, durations, page count) and what is
not, and flags scopes large enough to need explicit
confirmation before backlog refinement. refine_backlog batches
items into Stories of at most `batch_size` items each.
plan_sprint composes both and picks the supervising role from
the agent count: a director runs small sprints, a scrum master
runs sprints of four or more agents. youtube_items builds
items from video ids, checking captions through an injected
callable.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# Single source of truth: schedule.py's role_for_sprint (driven by its
# SCRUM_MASTER_MIN_AGENTS constant). Re-exported here because the sprint
# planner and tests import it from this module.
from schedule import role_for_sprint  # noqa: F401

DIRECTOR_MAX_AGENTS = 3
DEFAULT_BATCH_SIZE = 25
CONFIRM_SCOPE_THRESHOLD = 100

CaptionCheck = Callable[[str], Optional[bool]]


@dataclass
class WorkItem:
    """One unit of planned work."""

    item_id: str
    title: str
    duration_minutes: float = 0.0
    transcript_available: Optional[bool] = None


@dataclass
class DiscoveryBrief:
    """What discovery learned about a batch of work."""

    role: str
    total_items: int
    with_transcript: int
    missing_transcript: int
    unknown_transcript: int
    total_minutes: float
    pages: Optional[int]
    unknowns: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    scope_confirmation_required: bool = False
    confirmation_question: Optional[str] = None


@dataclass
class Story:
    """A batch of work items, sized for one sprint slot."""

    story_id: int
    title: str
    item_ids: List[str]
    estimate_minutes: float


@dataclass
class SprintPlan:
    """A complete sprint plan: role, brief, and stories."""

    role: str
    brief: DiscoveryBrief
    stories: List[Story]


def run_discovery(
    items: List[WorkItem], role: str, pages: Optional[int] = None
) -> DiscoveryBrief:
    """Summarize counts, transcripts, durations, and unknowns."""
    with_transcript = sum(1 for i in items if i.transcript_available is True)
    missing = sum(1 for i in items if i.transcript_available is False)
    unknown = sum(1 for i in items if i.transcript_available is None)
    total_minutes = sum(i.duration_minutes for i in items)
    unknowns: List[str] = []
    if unknown:
        unknowns.append(
            f"transcript availability unknown for {unknown} item(s)"
        )
    if pages is None:
        unknowns.append("page count unknown; verify pagination is exhausted")
    confirmation_required = len(items) > CONFIRM_SCOPE_THRESHOLD
    question: Optional[str] = None
    if confirmation_required:
        question = (
            f"Scope is {len(items)} items. Confirm the batch size and total "
            "before backlog refinement."
        )
    return DiscoveryBrief(
        role=role,
        total_items=len(items),
        with_transcript=with_transcript,
        missing_transcript=missing,
        unknown_transcript=unknown,
        total_minutes=total_minutes,
        pages=pages,
        unknowns=unknowns,
        assumptions=["durations are caller supplied and may be rounded"],
        scope_confirmation_required=confirmation_required,
        confirmation_question=question,
    )


def refine_backlog(
    items: List[WorkItem],
    brief: DiscoveryBrief,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> List[Story]:
    """Chunk items into stories of at most batch_size items."""
    stories: List[Story] = []
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        stories.append(
            Story(
                story_id=len(stories) + 1,
                title=f"Batch {len(stories) + 1} ({len(batch)} items)",
                item_ids=[i.item_id for i in batch],
                estimate_minutes=sum(i.duration_minutes for i in batch),
            )
        )
    return stories


def plan_sprint(
    items: List[WorkItem],
    agent_count: int,
    pages: Optional[int] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> SprintPlan:
    """Run discovery and refinement, picking the role by headcount."""
    role = role_for_sprint(agent_count)
    brief = run_discovery(items, role, pages)
    stories = refine_backlog(items, brief, batch_size)
    return SprintPlan(role=role, brief=brief, stories=stories)


def youtube_items(
    video_ids: List[str],
    caption_check: CaptionCheck,
    durations: Optional[Dict[str, float]] = None,
) -> List[WorkItem]:
    """Build work items from video ids, checking each caption."""
    durations = durations or {}
    return [
        WorkItem(
            item_id=video_id,
            title=f"youtube:{video_id}",
            duration_minutes=durations.get(video_id, 0.0),
            transcript_available=caption_check(video_id),
        )
        for video_id in video_ids
    ]
