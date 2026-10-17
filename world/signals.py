"""Signal registry for event-driven wake-ups (ask 7).

A WakePlan subscribes one agent to one subject (for example a
market symbol). Incoming data is evaluated against the plan's armed
scenarios; each match fires a WakeEvent naming the agent, team,
scenario, action, and exit strategy. Scenarios disarm after firing
unless marked for rearm, so each fires at most once per arming.
Plans can be gated by an `active_when` expression and by a
`wakeable` callback that checks whether the agent is available for
the planned work (used once roster support lands).
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class Scenario:
    """A condition/action pair that can fire a wake event."""

    scenario_id: str
    name: str
    condition: str
    action: str
    exit_strategy: str = ""
    armed: bool = True
    rearm: bool = False


@dataclass
class WakePlan:
    """A subscription that ties one agent to one subject."""

    plan_id: str
    agent_id: str
    team: str
    subject: str
    task: Optional[str] = None
    active_when: Optional[str] = None
    scenarios: List[Scenario] = field(default_factory=list)


@dataclass
class WakeEvent:
    """The result of one scenario firing."""

    plan_id: str
    agent_id: str
    team: str
    subject: str
    task: Optional[str]
    scenario_id: str
    action: str
    exit_strategy: str = ""


Evaluator = Callable[[str, str], bool]
Wakeable = Callable[[str, Optional[str]], bool]


class SignalWaker:
    """Evaluates data against armed plans and emits wake events."""

    def __init__(
        self,
        evaluator: Evaluator,
        wakeable: Optional[Wakeable] = None,
    ) -> None:
        """Store the evaluator and the optional wakeability gate."""
        self.evaluator = evaluator
        self.wakeable = wakeable
        self._plans: Dict[str, WakePlan] = {}

    def arm(self, plan: WakePlan) -> None:
        """Register a plan under its id."""
        self._plans[plan.plan_id] = plan

    def disarm(self, plan_id: str) -> bool:
        """Disarm every scenario in a plan; False when unknown."""
        plan = self._plans.get(plan_id)
        if plan is None:
            return False
        for scenario in plan.scenarios:
            scenario.armed = False
        return True

    def evaluate(self, subject: str, data: str) -> List[WakeEvent]:
        """Return wake events for every armed scenario matching data."""
        events: List[WakeEvent] = []
        for plan in self._plans.values():
            if plan.subject != subject:
                continue
            if self.wakeable is not None and not self.wakeable(
                plan.agent_id, plan.task
            ):
                continue
            if (
                plan.active_when is not None
                and not self.evaluator(data, plan.active_when)
            ):
                continue
            for scenario in plan.scenarios:
                if not scenario.armed:
                    continue
                if not self.evaluator(data, scenario.condition):
                    continue
                events.append(
                    WakeEvent(
                        plan_id=plan.plan_id,
                        agent_id=plan.agent_id,
                        team=plan.team,
                        subject=subject,
                        task=plan.task,
                        scenario_id=scenario.scenario_id,
                        action=scenario.action,
                        exit_strategy=scenario.exit_strategy,
                    )
                )
                if not scenario.rearm:
                    scenario.armed = False
        return events
