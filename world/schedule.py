"""Standing-task scheduler (the cron scheduling ask).

A standing task declares when it wants to run -- always, a bounded
number of hours, a daily time window, specific weekdays, a day of
the month, or a day of the year -- plus optional budgets: a cap on
total occurrences, on consecutive hours, and on total hours, with an
optional gap that resets the consecutive-hour count. The scheduler
keeps per-task runtime state and, on each tick, decides the next
move:

- start: the active window just opened, with the agent count and the
  lead role (a director leads a small crew, a scrum master leads a
  crew at or above the scrum-master team size);
- continue: still inside the window, same crew;
- stop: the window closed or a budget cap was hit;
- idle: outside the window, the period already completed, or the
  task is finished.

hours mode is one-shot by design: the first tick anchors the clock
and the task finishes when its duration elapses. A task that
completes its work early can call complete() to close the period
(or finish outright on the occurrence cap); abort() cancels a
running period without counting it.

The scheduler is pure -- no sim import, no DB, no clock of its own;
the caller supplies now on every tick and owns what a
start/continue/stop means in terms of queueing work. role_for_sprint
mirrors sim.SCRUM_MASTER_MIN_TEAM_SIZE (4): below it the director
facilitates, at or above it a scrum master is required.
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Callable, Dict, List, Optional, Set, Tuple

ALWAYS = "always"
HOURS = "hours"
WINDOW = "window"
WEEKLY = "weekly"
MONTHLY = "monthly"
YEARLY = "yearly"

MODES = (ALWAYS, HOURS, WINDOW, WEEKLY, MONTHLY, YEARLY)

SUPERVISOR_DEFAULT_AGENTS = 3

# Mirrors sim.SCRUM_MASTER_MIN_TEAM_SIZE: a crew smaller than this
# is led by the director, a crew at or above it needs a scrum master.
SCRUM_MASTER_MIN_AGENTS = 4

AgentCountPolicy = Callable[["StandingTask"], int]


def parse_time(text: str) -> time:
    """Parse an HH:MM wall-clock time."""
    return datetime.strptime(text, "%H:%M").time()


def supervisor_agent_count(task: "StandingTask") -> int:
    """Default crew size for a standing task unless one is set."""
    if task.agent_count is not None:
        return task.agent_count
    return SUPERVISOR_DEFAULT_AGENTS


def role_for_sprint(agent_count: int) -> str:
    """Lead role for a crew: director below the scrum-master size."""
    if agent_count < SCRUM_MASTER_MIN_AGENTS:
        return "director"
    return "scrum master"


@dataclass
class StandingTask:
    """One standing (cron) task and its schedule contract."""

    task_id: str
    name: str
    mode: str = ALWAYS
    agent_count: Optional[int] = None
    duration_hours: Optional[float] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    days_of_week: Set[int] = field(default_factory=set)
    day_of_month: Optional[int] = None
    yearly_date: Optional[Tuple[int, int]] = None
    max_occurrences: Optional[int] = None
    max_consecutive_hours: Optional[float] = None
    max_total_hours: Optional[float] = None
    consecutive_reset_after: Optional[timedelta] = None
    team: Optional[str] = None

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"unknown mode: {self.mode}")
        if self.mode == HOURS and self.duration_hours is None:
            raise ValueError("hours mode requires duration_hours")
        if self.mode in (WINDOW, WEEKLY, MONTHLY, YEARLY):
            if self.start_time is None or self.end_time is None:
                raise ValueError(
                    f"{self.mode} mode requires start_time and end_time"
                )
            parse_time(self.start_time)
            parse_time(self.end_time)
        if self.mode == WEEKLY:
            if not self.days_of_week:
                raise ValueError("weekly mode requires days_of_week")
            for day in self.days_of_week:
                if not 0 <= day <= 6:
                    raise ValueError(f"day of week out of range: {day}")
        if self.mode == MONTHLY and self.day_of_month is None:
            raise ValueError("monthly mode requires day_of_month")
        if self.mode == YEARLY and self.yearly_date is None:
            raise ValueError("yearly mode requires yearly_date")


@dataclass
class TaskState:
    """Per-task runtime state the scheduler owns between ticks."""

    running: bool = False
    mode_anchor: Optional[datetime] = None
    run_started_at: Optional[datetime] = None
    last_active_at: Optional[datetime] = None
    occurrences: int = 0
    total_hours: float = 0.0
    consecutive_hours: float = 0.0
    finished: bool = False
    finish_reason: Optional[str] = None
    was_active: Optional[bool] = None


@dataclass
class TickResult:
    """What the scheduler decided at one tick."""

    task_id: str
    action: str
    agent_count: Optional[int] = None
    lead_role: Optional[str] = None
    reason: str = ""
    finished: bool = False


def in_window(now_time: time, start: time, end: time) -> bool:
    """True when now_time falls in [start, end], overnight-aware."""
    if start <= end:
        return start <= now_time <= end
    return now_time >= start or now_time <= end


class StandingScheduler:
    """Ticks standing tasks and reports start/continue/stop/idle."""

    def __init__(
        self,
        agent_count_policy: AgentCountPolicy = supervisor_agent_count,
    ):
        self.agent_count_policy = agent_count_policy
        self.states: Dict[str, TaskState] = {}

    def is_active(
        self, task: StandingTask, state: TaskState, now: datetime
    ) -> bool:
        """Whether the task's schedule is open right now."""
        if task.mode == ALWAYS:
            return True
        if task.mode == HOURS:
            if state.mode_anchor is None:
                return True
            return (
                now - state.mode_anchor
                < timedelta(hours=task.duration_hours or 0.0)
            )
        if task.start_time is not None and task.end_time is not None:
            start = parse_time(task.start_time)
            end = parse_time(task.end_time)
            if not in_window(now.time(), start, end):
                return False
        if task.mode == WINDOW:
            return True
        if task.mode == WEEKLY:
            return now.weekday() in task.days_of_week
        if task.mode == MONTHLY:
            return now.day == task.day_of_month
        if task.mode == YEARLY:
            return (now.month, now.day) == task.yearly_date
        return False

    def tick(
        self, task: StandingTask, now: datetime
    ) -> TickResult:
        """Decide the task's next move at now."""
        state = self._state(task)
        if state.finished:
            return TickResult(
                task_id=task.task_id,
                action="idle",
                reason=f"finished: {state.finish_reason}",
            )
        active = self.is_active(task, state, now)
        if state.running:
            started = state.run_started_at
            if started is None:
                started = now
            elapsed = (now - started).total_seconds() / 3600.0
            if not active:
                self._end_period(task, state, now)
                state.was_active = False
                return TickResult(
                    task_id=task.task_id,
                    action="stop",
                    finished=state.finished,
                    reason="active window closed",
                )
            if self._hours_cap_reached(task, state, elapsed, now):
                return TickResult(
                    task_id=task.task_id,
                    action="stop",
                    finished=True,
                    reason=state.finish_reason or "hour cap reached",
                )
            return TickResult(
                task_id=task.task_id,
                action="continue",
                agent_count=self.agent_count_policy(task),
                reason="within active window",
            )
        self._maybe_reset_consecutive(task, state, now)
        if not active:
            state.was_active = False
            return TickResult(
                task_id=task.task_id,
                action="idle",
                reason="outside active window",
            )
        if state.was_active is True and task.mode != ALWAYS:
            return TickResult(
                task_id=task.task_id,
                action="idle",
                reason="period complete",
            )
        if task.max_occurrences is not None:
            if state.occurrences >= task.max_occurrences:
                self._finish(state, "occurrence cap reached")
                return TickResult(
                    task_id=task.task_id,
                    action="idle",
                    reason=f"finished: {state.finish_reason}",
                )
        state.running = True
        state.run_started_at = now
        state.was_active = True
        if task.mode == HOURS and state.mode_anchor is None:
            state.mode_anchor = now
        agent_count = self.agent_count_policy(task)
        return TickResult(
            task_id=task.task_id,
            action="start",
            agent_count=agent_count,
            lead_role=role_for_sprint(agent_count),
            reason="active window opened",
        )

    def tick_all(
        self, tasks: List[StandingTask], now: datetime
    ) -> Dict[str, TickResult]:
        """Tick every task, keyed by task id."""
        return {task.task_id: self.tick(task, now) for task in tasks}

    def complete(
        self, task: StandingTask, now: datetime
    ) -> TickResult:
        """Close the running period early as work complete."""
        state = self._state(task)
        if state.finished:
            return TickResult(
                task_id=task.task_id,
                action="idle",
                finished=True,
                reason=f"finished: {state.finish_reason}",
            )
        if not state.running:
            return TickResult(
                task_id=task.task_id,
                action="idle",
                reason="not running",
            )
        self._end_period(
            task, state, now, finish_reason="work complete"
        )
        state.was_active = True
        return TickResult(
            task_id=task.task_id,
            action="stop",
            finished=state.finished,
            reason="work complete",
        )

    def abort(self, task: StandingTask) -> bool:
        """Cancel a running period without counting it."""
        state = self._state(task)
        if not state.running:
            return False
        state.running = False
        state.run_started_at = None
        state.was_active = None
        if task.mode == HOURS:
            state.mode_anchor = None
        return True

    def _hours_cap_reached(
        self,
        task: StandingTask,
        state: TaskState,
        elapsed: float,
        now: datetime,
    ) -> bool:
        projected = state.consecutive_hours + elapsed
        if (
            task.max_consecutive_hours is not None
            and projected >= task.max_consecutive_hours
        ):
            self._end_period(task, state, now)
            state.was_active = True
            self._finish(state, "consecutive hours reached")
            return True
        projected_total = state.total_hours + elapsed
        if (
            task.max_total_hours is not None
            and projected_total >= task.max_total_hours
        ):
            self._end_period(task, state, now)
            state.was_active = True
            self._finish(state, "total hours reached")
            return True
        return False

    def _end_period(
        self,
        task: StandingTask,
        state: TaskState,
        ended_at: datetime,
        finish_reason: Optional[str] = None,
    ) -> None:
        started = state.run_started_at
        if started is None:
            started = ended_at
        elapsed = (ended_at - started).total_seconds() / 3600.0
        state.total_hours += elapsed
        state.consecutive_hours += elapsed
        state.occurrences += 1
        state.running = False
        state.run_started_at = None
        state.last_active_at = ended_at
        if task.mode == HOURS:
            self._finish(
                state, finish_reason or "duration elapsed"
            )
        elif (
            task.max_occurrences is not None
            and state.occurrences >= task.max_occurrences
        ):
            self._finish(state, "occurrence cap reached")
        elif (
            task.max_total_hours is not None
            and state.total_hours >= task.max_total_hours
        ):
            self._finish(state, "total hours reached")
        elif (
            task.max_consecutive_hours is not None
            and state.consecutive_hours >= task.max_consecutive_hours
        ):
            self._finish(state, "consecutive hours reached")

    def _maybe_reset_consecutive(
        self, task: StandingTask, state: TaskState, now: datetime
    ) -> None:
        if task.consecutive_reset_after is None:
            return
        if state.last_active_at is None:
            return
        if now - state.last_active_at > task.consecutive_reset_after:
            state.consecutive_hours = 0.0

    def _finish(self, state: TaskState, reason: str) -> None:
        if not state.finished:
            state.finished = True
            state.finish_reason = reason

    def _state(self, task: StandingTask) -> TaskState:
        if task.task_id not in self.states:
            self.states[task.task_id] = TaskState()
        return self.states[task.task_id]
