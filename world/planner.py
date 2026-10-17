"""Supervisor meeting: break down and assign work (asks 8 and 9).

A SupervisorMeeting turns a PlannerSubmission into work
items, runs discovery over them, and refines the backlog
into stories. assign() then routes each story to a team
by keyword match against the team's specialties, falling
back to the team with the most remaining capacity. Work
no team can take is dropped. When a budget ledger is
attached, each assignment opens a budget line scaled by
the ledger's overrun history.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from roster import Roster
from sprint import (
    DEFAULT_BATCH_SIZE,
    DiscoveryBrief,
    Story,
    WorkItem,
    refine_backlog,
    run_discovery,
)
from story_budget import BudgetLedger


@dataclass
class PlannerSubmission:
    """A supervisor's proposed batch of work."""

    title: str
    goal: str
    items: List[str] = field(default_factory=list)
    durations: Dict[str, float] = field(default_factory=dict)


@dataclass
class TeamAssignment:
    """The stories one team is assigned."""

    team: str
    story_ids: List[int] = field(default_factory=list)


class SupervisorMeeting:
    """Breaks submissions into stories and assigns them."""

    def __init__(
        self,
        roster: Roster,
        specialties: Dict[str, List[str]],
        stories_per_agent: int = 1,
        batch_size: int = DEFAULT_BATCH_SIZE,
        role: str = "supervisor",
        ledger: Optional[BudgetLedger] = None,
    ):
        """Store the roster, specialties, and optional ledger."""
        self.roster = roster
        self.specialties = specialties
        self.stories_per_agent = stories_per_agent
        self.batch_size = batch_size
        self.role = role
        self.ledger = ledger
        self._items: Dict[str, WorkItem] = {}

    def breakdown(
        self, submission: PlannerSubmission
    ) -> Tuple[DiscoveryBrief, List[Story]]:
        """Turn a submission into a discovery brief and stories."""
        items = [
            WorkItem(
                item_id=f"w-{number:04d}",
                title=text,
                duration_minutes=submission.durations.get(
                    text, 0.0
                ),
            )
            for number, text in enumerate(submission.items, start=1)
        ]
        self._items = {item.item_id: item for item in items}
        brief = run_discovery(items, self.role)
        stories = refine_backlog(items, brief, self.batch_size)
        return brief, stories

    def assign(
        self, stories: List[Story]
    ) -> List[TeamAssignment]:
        """Assign stories to teams by specialty, then capacity."""
        remaining = {
            team: len(self.roster.team_available(team))
            * self.stories_per_agent
            for team in self.specialties
        }
        assignments: Dict[str, TeamAssignment] = {}
        for story in stories:
            text = " ".join(
                self._items[item_id].title
                for item_id in story.item_ids
                if item_id in self._items
            ).lower()
            claimed = ""
            for team, keywords in self.specialties.items():
                if remaining[team] <= 0:
                    continue
                if any(keyword.lower() in text for keyword in keywords):
                    claimed = team
                    break
            if not claimed:
                candidates = [
                    team
                    for team, count in remaining.items()
                    if count > 0
                ]
                if not candidates:
                    continue
                claimed = max(
                    candidates, key=lambda team: remaining[team]
                )
            remaining[claimed] -= 1
            if claimed not in assignments:
                assignments[claimed] = TeamAssignment(team=claimed)
            assignments[claimed].story_ids.append(story.story_id)
            if self.ledger is not None:
                scale = 1.0 + self.ledger.overrun_factor()
                self.ledger.open_line(
                    story.story_id,
                    story.estimate_minutes * scale,
                    len(story.item_ids),
                )
        return list(assignments.values())
