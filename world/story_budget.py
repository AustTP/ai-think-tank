"""Story budget ledger for supervisor meetings (ask 9).

Each story gets a BudgetLine with a minute budget and an
item count. record_spend() charges minutes against a line
and raises a BudgetAlert the first time a line is
exceeded (not on every further charge). review() records
a BudgetReview and either grants additional minutes or
closes the line. overrun_factor() averages the
spend-to-budget ratio of reviewed lines, so meetings can
scale future budgets by history.

The reference design also has a BudgetMonitor that pushes
alerts through a keyed policy API. That monitor is not
ported: the main repo's theo_policy uses a different API
(evaluate(state, requester_id, action, payload)), and the
alerts are already covered by the budget overrun
intelligence in budget.py.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class BudgetLine:
    """The minute budget for one story."""

    story_id: int
    budget_minutes: float
    total_items: int
    spent_minutes: float = 0.0
    completed_items: int = 0
    closed: bool = False

    @property
    def remaining(self) -> float:
        return self.budget_minutes - self.spent_minutes

    @property
    def exceeded(self) -> bool:
        return self.spent_minutes > self.budget_minutes


@dataclass
class BudgetAlert:
    """Raised once when a line first goes over budget."""

    story_id: int
    spent_minutes: float
    budget_minutes: float
    completed_items: int
    total_items: int


@dataclass
class BudgetReview:
    """A director's decision on an over-budget story."""

    story_id: int
    reason: str
    decision: str
    completed_items: int
    total_items: int
    spent_minutes: float
    budget_minutes: float
    additional_minutes: float = 0.0


class BudgetLedger:
    """Tracks budget lines, alerts, and reviews per story."""

    def __init__(self) -> None:
        self._lines: Dict[int, BudgetLine] = {}
        self._alerts: List[BudgetAlert] = []
        self._reviews: List[BudgetReview] = []

    def open_line(
        self, story_id: int, budget_minutes: float, total_items: int
    ) -> BudgetLine:
        """Open (or reopen) a budget line for a story."""
        line = BudgetLine(
            story_id=story_id,
            budget_minutes=budget_minutes,
            total_items=total_items,
        )
        self._lines[story_id] = line
        return line

    def line(self, story_id: int) -> Optional[BudgetLine]:
        """A story's budget line, or None."""
        return self._lines.get(story_id)

    def record_spend(
        self, story_id: int, minutes: float, completed_items: int
    ) -> Optional[BudgetAlert]:
        """Charge minutes; alert on the first overrun only."""
        line = self._lines.get(story_id)
        if line is None or line.closed:
            return None
        was_exceeded = line.exceeded
        line.spent_minutes += minutes
        line.completed_items = completed_items
        if line.exceeded and not was_exceeded:
            alert = BudgetAlert(
                story_id=story_id,
                spent_minutes=line.spent_minutes,
                budget_minutes=line.budget_minutes,
                completed_items=line.completed_items,
                total_items=line.total_items,
            )
            self._alerts.append(alert)
            return alert
        return None

    def review(
        self,
        story_id: int,
        reason: str,
        decision: str,
        additional_minutes: float = 0.0,
    ) -> Optional[BudgetReview]:
        """Record a review; grant minutes or close the line."""
        line = self._lines.get(story_id)
        if line is None:
            return None
        review = BudgetReview(
            story_id=story_id,
            reason=reason,
            decision=decision,
            completed_items=line.completed_items,
            total_items=line.total_items,
            spent_minutes=line.spent_minutes,
            budget_minutes=line.budget_minutes,
            additional_minutes=additional_minutes,
        )
        self._reviews.append(review)
        if decision == "grant":
            line.budget_minutes += additional_minutes
        else:
            line.closed = True
        return review

    def alerts(self) -> List[BudgetAlert]:
        """Every alert raised so far."""
        return list(self._alerts)

    def lines(self) -> List[BudgetLine]:
        """Every budget line, open or closed."""
        return list(self._lines.values())

    def reviews(self) -> List[BudgetReview]:
        """Every review recorded so far."""
        return list(self._reviews)

    def overrun_factor(self) -> float:
        """Mean spend-to-budget ratio across reviewed lines."""
        if not self._reviews:
            return 0.0
        ratios = [
            review.spent_minutes / review.budget_minutes
            for review in self._reviews
            if review.budget_minutes > 0
        ]
        if not ratios:
            return 0.0
        return sum(ratios) / len(ratios)

    def to_state(self) -> Dict:
        """A JSON-ready snapshot for durable storage."""
        return {
            'lines': [vars(line) for line in self._lines.values()],
            'alerts': [vars(alert) for alert in self._alerts],
            'reviews': [vars(review) for review in self._reviews],
        }

    @classmethod
    def from_state(cls, snapshot) -> Optional['BudgetLedger']:
        """Rebuild a ledger from a to_state snapshot.

        Returns None for anything that is not a ledger
        snapshot, so a caller can fall back to a fresh
        ledger. Unreadable records are skipped rather
        than raising: a durable ledger must never take
        the meeting down."""
        if not isinstance(snapshot, dict):
            return None
        ledger = cls()
        for line in snapshot.get('lines') or []:
            if isinstance(line, dict) and 'story_id' in line:
                try:
                    ledger._lines[line['story_id']] = BudgetLine(**line)
                except TypeError:
                    continue
        for alert in snapshot.get('alerts') or []:
            if isinstance(alert, dict) and 'story_id' in alert:
                try:
                    ledger._alerts.append(BudgetAlert(**alert))
                except TypeError:
                    continue
        for review in snapshot.get('reviews') or []:
            if isinstance(review, dict) and 'story_id' in review:
                try:
                    ledger._reviews.append(BudgetReview(**review))
                except TypeError:
                    continue
        return ledger
