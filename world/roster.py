"""Team and agent roster for routing and wake-ups (ask 11).

Teams carry a focused task and a do-not-disturb flag. Agents
carry a team, role, duty status, and whether their sprint is
complete. The roster routes one-off work to off-duty agents:
same-team agents who finished their sprint come first, then
agents on other teams (which needs explicit confirmation).
Confirmations lapse when the chosen agent stops being
available. Focused teams accept only work matching their
focus, do-not-disturb teams accept nothing, and `wakeable`
reports whether a signal may wake an agent for given work.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

ON_DUTY = "on_duty"
OFF_DUTY = "off_duty"


@dataclass
class TeamRecord:
    """A team's focus and disturbance policy."""

    name: str
    focused_task: Optional[str] = None
    do_not_disturb: bool = False

    @property
    def focused(self) -> bool:
        return self.focused_task is not None

    def disturbable_for(self, work: Optional[str]) -> bool:
        """True when this team may be disturbed for `work`."""
        if self.do_not_disturb:
            return False
        if self.focused_task is None:
            return True
        return work == self.focused_task


@dataclass
class AgentRecord:
    """One agent's team, role, and availability."""

    agent_id: str
    team: str
    role: str = "worker"
    status: str = ON_DUTY
    sprint_complete: bool = False


@dataclass
class RoutingDecision:
    """The outcome of routing one-off work."""

    task_id: str
    agent_id: Optional[str] = None
    team: Optional[str] = None
    same_team: bool = False
    requires_confirmation: bool = False
    reason: str = ""


class Roster:
    """Tracks teams and agents, and routes one-off work."""

    def __init__(self) -> None:
        self._agents: Dict[str, AgentRecord] = {}
        self._teams: Dict[str, TeamRecord] = {}
        self._confirmed: Dict[str, RoutingDecision] = {}

    def add_team(
        self,
        name: str,
        focused_task: Optional[str] = None,
        do_not_disturb: bool = False,
    ) -> TeamRecord:
        """Register a team and return its record."""
        record = TeamRecord(
            name=name,
            focused_task=focused_task,
            do_not_disturb=do_not_disturb,
        )
        self._teams[name] = record
        return record

    def team(self, name: str) -> Optional[TeamRecord]:
        """A team's record, or None."""
        return self._teams.get(name)

    def set_focus(self, name: str, focused_task: str) -> bool:
        """Point a team at one focused task."""
        record = self._teams.get(name)
        if record is None:
            return False
        record.focused_task = focused_task
        return True

    def release_team(self, name: str) -> bool:
        """Clear a team's focus so it takes any work."""
        record = self._teams.get(name)
        if record is None:
            return False
        record.focused_task = None
        return True

    def set_do_not_disturb(self, name: str, flag: bool) -> bool:
        """Set or clear a team's do-not-disturb flag."""
        record = self._teams.get(name)
        if record is None:
            return False
        record.do_not_disturb = flag
        return True

    def add_agent(
        self, agent_id: str, team: str, role: str = "worker"
    ) -> AgentRecord:
        """Register an agent, creating the team if needed."""
        if team not in self._teams:
            self._teams[team] = TeamRecord(name=team)
        record = AgentRecord(agent_id=agent_id, team=team, role=role)
        self._agents[agent_id] = record
        return record

    def agent(self, agent_id: str) -> Optional[AgentRecord]:
        """An agent's record, or None."""
        return self._agents.get(agent_id)

    def set_status(self, agent_id: str, status: str) -> bool:
        """Set an agent's duty status."""
        record = self._agents.get(agent_id)
        if record is None:
            return False
        record.status = status
        return True

    def mark_sprint_complete(self, agent_id: str) -> bool:
        """Mark an agent's sprint as complete."""
        record = self._agents.get(agent_id)
        if record is None:
            return False
        record.sprint_complete = True
        return True

    def available_in_team(self, team: str) -> List[AgentRecord]:
        """Off-duty agents in a team whose sprint is complete."""
        team_record = self._teams.get(team)
        if team_record is not None and not team_record.disturbable_for(None):
            return []
        return [
            record
            for record in self._agents.values()
            if record.team == team
            and record.status == OFF_DUTY
            and record.sprint_complete
        ]

    def off_duty_elsewhere(self, exclude_team: str) -> List[AgentRecord]:
        """Off-duty agents on other disturbable teams."""
        candidates = []
        for record in self._agents.values():
            if record.team == exclude_team or record.status != OFF_DUTY:
                continue
            team_record = self._teams.get(record.team)
            if team_record is not None and not team_record.disturbable_for(
                None
            ):
                continue
            candidates.append(record)
        return candidates

    def route_one_off(self, task_id: str, team: str) -> RoutingDecision:
        """Route one-off work to an off-duty agent.

        A still-valid confirmation is honored first. Then the
        first same-team agent whose sprint is complete. Then the
        first off-duty agent on another team, which requires
        confirmation. Otherwise the decision carries no agent.
        """
        pending = self._confirmed.get(task_id)
        if pending is not None:
            record = self._agents.get(pending.agent_id or "")
            team_record = self._teams.get(pending.team or "")
            valid = (
                record is not None
                and record.status == OFF_DUTY
                and (
                    team_record is None
                    or team_record.disturbable_for(None)
                )
            )
            if valid:
                return RoutingDecision(
                    task_id=task_id,
                    agent_id=pending.agent_id,
                    team=pending.team,
                    same_team=pending.same_team,
                    reason=pending.reason,
                )
            self._confirmed.pop(task_id, None)
        finished = self.available_in_team(team)
        if finished:
            chosen = finished[0]
            return RoutingDecision(
                task_id=task_id,
                agent_id=chosen.agent_id,
                team=chosen.team,
                same_team=True,
                reason="sprint complete and off duty",
            )
        others = self.off_duty_elsewhere(team)
        if not others:
            return RoutingDecision(
                task_id=task_id,
                reason="no off duty agent available",
            )
        chosen = others[0]
        return RoutingDecision(
            task_id=task_id,
            agent_id=chosen.agent_id,
            team=chosen.team,
            requires_confirmation=True,
            reason="off duty on another team, confirm assignment",
        )

    def confirm(self, decision: RoutingDecision) -> Optional[RoutingDecision]:
        """Confirm a cross-team proposal while the agent stays off duty."""
        record = self._agents.get(decision.agent_id or "")
        if record is None or record.status != OFF_DUTY:
            return None
        confirmed = RoutingDecision(
            task_id=decision.task_id,
            agent_id=decision.agent_id,
            team=decision.team,
            same_team=decision.same_team,
            reason=decision.reason,
        )
        self._confirmed[decision.task_id] = confirmed
        return confirmed

    def consume_confirmation(
        self, task_id: str
    ) -> Optional[RoutingDecision]:
        """Remove and return a stored confirmation."""
        return self._confirmed.pop(task_id, None)

    def team_available(self, team: str) -> List[AgentRecord]:
        """Agents in a team who are on duty or sprint-complete."""
        team_record = self._teams.get(team)
        if team_record is not None and not team_record.disturbable_for(None):
            return []
        return [
            record
            for record in self._agents.values()
            if record.team == team
            and (record.status == ON_DUTY or record.sprint_complete)
        ]

    def wakeable(self, agent_id: str, work: Optional[str]) -> bool:
        """True when a signal may wake this agent for `work`."""
        record = self._agents.get(agent_id)
        if record is None or record.status != OFF_DUTY:
            return False
        team_record = self._teams.get(record.team)
        if team_record is None:
            return True
        return team_record.disturbable_for(work)
