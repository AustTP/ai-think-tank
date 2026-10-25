"""Signal-plan wake wiring (ask 7): event-driven wake-ups.

signals.py is pure and already tested on its own. These tests
cover the WIRING in sim.emit_signals: durable
state['signalPlans'] declarations are rebuilt as WakePlan
objects, evaluated against an emission's (subject, data) with
token containment (every condition token must appear in
the data, in any order), matching agents are woken only
when fully idle, every wake is logged as governance, and scenario
arming is written BACK into the durable declarations (a fired
scenario stays disarmed across emissions unless marked rearm).
The waker is constructed from the very plan objects the
write-back persists, so disarming a fired scenario is real, not
a second rebuild's no-op. emit_signals never raises: a signal
failure must not break the emission point (a task completion).
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import sim  # noqa: E402


class SignalWiring(unittest.TestCase):
    def setUp(self):
        self.governance = []
        self._gov = unittest.mock.patch.object(
            sim, '_log_governance',
            side_effect=lambda state, agent_id, action, details:
                self.governance.append((agent_id, action, details)))
        self._gov.start()
        self.addCleanup(self._gov.stop)

    def _idle_agent(self):
        return {'id': 'ada', 'offDuty': False, 'busy': False,
                'task': None, 'x': 0, 'y': 0}

    def _plan(self, armed=True, rearm=False,
              condition='status=done room=observatory',
              agent_id='ada', active_when=None):
        return {
            'planId': 'p1', 'agentId': agent_id, 'team': 'dev',
            'subject': 'task.completed', 'task': 'task-9',
            'activeWhen': active_when,
            'scenarios': [{
                'scenarioId': 's1', 'name': 'Observatory done',
                'condition': condition, 'action': 'investigate',
                'exitStrategy': 'report to faye',
                'armed': armed, 'rearm': rearm,
            }],
        }

    def _state(self, plans=None, agents=None):
        return {
            'agents': agents if agents is not None
            else {'ada': self._idle_agent()},
            'signalPlans': plans if plans is not None
            else [self._plan()],
        }

    def _data(self, **extra):
        task = {'title': 'T', 'room': 'observatory'}
        task.update(extra)
        return sim._task_completed_data(task)

    def test_a_matching_scenario_fires_a_wake_event(self):
        state = self._state()
        events = sim.emit_signals(state, 'task.completed', self._data())
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.plan_id, 'p1')
        self.assertEqual(event.agent_id, 'ada')
        self.assertEqual(event.team, 'dev')
        self.assertEqual(event.subject, 'task.completed')
        self.assertEqual(event.task, 'task-9')
        self.assertEqual(event.scenario_id, 's1')
        self.assertEqual(event.action, 'investigate')
        self.assertEqual(event.exit_strategy, 'report to faye')
        self.assertEqual(self.governance, [(
            'ada', 'signal_wake', {
                'planId': 'p1', 'agentId': 'ada', 'team': 'dev',
                'subject': 'task.completed', 'task': 'task-9',
                'scenarioId': 's1', 'action': 'investigate',
                'exitStrategy': 'report to faye'})])

    def test_task_completion_data_is_a_flat_key_value_line(self):
        self.assertEqual(
            self._data(teamId='t1', taskType='code'),
            'status=done title=T room=observatory team=t1 taskType=code')
        # Optional bits only appear when the task carries them.
        self.assertEqual(
            self._data(), 'status=done title=T room=observatory')

    def test_a_fired_scenario_disarms_and_persists(self):
        state = self._state()
        data = self._data()
        self.assertEqual(len(sim.emit_signals(
            state, 'task.completed', data)), 1)
        self.assertFalse(state['signalPlans'][0]['scenarios'][0]['armed'])
        # Disarmed stays disarmed: the next match is silent.
        self.assertEqual(sim.emit_signals(
            state, 'task.completed', data), [])

    def test_a_rearm_scenario_fires_again(self):
        state = self._state(plans=[self._plan(rearm=True)])
        data = self._data()
        self.assertEqual(len(sim.emit_signals(
            state, 'task.completed', data)), 1)
        self.assertTrue(state['signalPlans'][0]['scenarios'][0]['armed'])
        self.assertEqual(len(sim.emit_signals(
            state, 'task.completed', data)), 1)

    def test_a_condition_the_data_does_not_contain_never_fires(self):
        state = self._state(plans=[self._plan(
            condition='status=done room=kitchen')])
        self.assertEqual(sim.emit_signals(
            state, 'task.completed', self._data()), [])
        self.assertEqual(self.governance, [])

    def test_a_different_subject_never_fires(self):
        state = self._state()
        self.assertEqual(sim.emit_signals(
            state, 'browse.done', self._data()), [])
        self.assertEqual(self.governance, [])

    def test_a_busy_agent_is_not_woken(self):
        state = self._state(agents={'ada': dict(
            self._idle_agent(), busy=True)})
        self.assertEqual(sim.emit_signals(
            state, 'task.completed', self._data()), [])
        self.assertEqual(self.governance, [])

    def test_an_agent_with_a_task_is_not_woken(self):
        state = self._state(agents={'ada': dict(
            self._idle_agent(), task='t1')})
        self.assertEqual(sim.emit_signals(
            state, 'task.completed', self._data()), [])
        self.assertEqual(self.governance, [])

    def test_an_unknown_agent_is_not_woken(self):
        state = self._state(plans=[self._plan(agent_id='ghost')])
        self.assertEqual(sim.emit_signals(
            state, 'task.completed', self._data()), [])
        self.assertEqual(self.governance, [])

    def test_an_active_when_gate_can_hold_a_plan(self):
        state = self._state(plans=[self._plan(active_when='team=t1')])
        gated = self._data(teamId='t1')
        held = self._data(teamId='t2')
        self.assertEqual(len(sim.emit_signals(
            state, 'task.completed', gated)), 1)
        self.assertEqual(sim.emit_signals(
            state, 'task.completed', held), [])

    def test_no_plans_means_no_events(self):
        state = self._state(plans=[])
        self.assertEqual(sim.emit_signals(
            state, 'task.completed', self._data()), [])

    def test_corrupted_plan_records_never_raise(self):
        state = self._state(plans=[
            None, 'garbage', {'noPlanId': 1},
            {'planId': 'p', 'scenarios': [None, {}]},
        ])
        self.assertEqual(sim.emit_signals(
            state, 'task.completed', self._data()), [])
        self.assertEqual(self.governance, [])

    def test_garbage_state_never_raises(self):
        for state in (None, {}, {'signalPlans': 'oops'}):
            self.assertEqual(sim.emit_signals(
                state, 'task.completed', self._data()), [])


class RosterGating(unittest.TestCase):
    """Roster-aware wake gating: when state carries an
    agentRoster, signals wake OFF-DUTY agents only, and
    focused or do-not-disturb teams are not disturbed for
    work they did not focus on. The plan's task field is
    the work tag a focused team matches on. A state with
    no agentRoster keeps the weaker idle-only gate (the
    tests above)."""

    def setUp(self):
        self.governance = []
        self._gov = unittest.mock.patch.object(
            sim, '_log_governance',
            side_effect=lambda state, agent_id, action, details:
                self.governance.append((agent_id, action, details)))
        self._gov.start()
        self.addCleanup(self._gov.stop)

    def _agent(self, off_duty=True):
        return {'id': 'ada', 'offDuty': off_duty, 'busy': False,
                'task': None, 'x': 0, 'y': 0}

    def _plan(self, plan_id='p1', task='task-9'):
        return {
            'planId': plan_id, 'agentId': 'ada', 'team': 'dev',
            'subject': 'task.completed', 'task': task,
            'scenarios': [{
                'scenarioId': 's1', 'name': 'Observatory done',
                'condition': 'status=done room=observatory',
                'action': 'investigate', 'armed': True,
            }],
        }

    def _state(self, teams, roster, plans=None, agents=None):
        return {
            'agents': agents if agents is not None
            else {'ada': self._agent()},
            'agentRoster': roster,
            'teams': teams,
            'signalPlans': plans if plans is not None
            else [self._plan()],
        }

    def _data(self):
        return sim._task_completed_data(
            {'title': 'T', 'room': 'observatory'})

    def test_an_off_duty_agent_is_woken(self):
        state = self._state(
            teams=[{'id': 'dev'}],
            roster=[{'id': 'ada', 'director': 'dev'}])
        self.assertEqual(len(sim.emit_signals(
            state, 'task.completed', self._data())), 1)
        self.assertEqual(len(self.governance), 1)

    def test_an_on_duty_agent_is_not_woken(self):
        state = self._state(
            teams=[{'id': 'dev'}],
            roster=[{'id': 'ada', 'director': 'dev'}],
            agents={'ada': self._agent(off_duty=False)})
        self.assertEqual(sim.emit_signals(
            state, 'task.completed', self._data()), [])
        self.assertEqual(self.governance, [])

    def test_a_do_not_disturb_team_is_not_woken(self):
        state = self._state(
            teams=[{'id': 'dev', 'doNotDisturb': True}],
            roster=[{'id': 'ada', 'director': 'dev'}])
        self.assertEqual(sim.emit_signals(
            state, 'task.completed', self._data()), [])
        self.assertEqual(self.governance, [])

    def test_a_focused_team_is_woken_only_for_its_focus(self):
        state = self._state(
            teams=[{'id': 'dev', 'focus': 'task-9'}],
            roster=[{'id': 'ada', 'director': 'dev'}])
        self.assertEqual(len(sim.emit_signals(
            state, 'task.completed', self._data())), 1)
        # The same agent, subscribed to a different work tag:
        # the focused team is not disturbed for it.
        other = self._state(
            teams=[{'id': 'dev', 'focus': 'task-9'}],
            roster=[{'id': 'ada', 'director': 'dev'}],
            plans=[self._plan(plan_id='p2', task='task-8')])
        self.assertEqual(sim.emit_signals(
            other, 'task.completed', self._data()), [])

    def test_an_agent_whose_team_has_no_record_is_woken(self):
        # The roster entry's team carries no team record:
        # the permissive branch applies to that agent.
        state = self._state(
            teams=[],
            roster=[{'id': 'ada', 'director': 'ghost-team'}])
        self.assertEqual(len(sim.emit_signals(
            state, 'task.completed', self._data())), 1)

    def test_an_agent_missing_from_the_roster_is_not_woken(self):
        state = self._state(
            teams=[{'id': 'dev'}],
            roster=[{'id': 'bee', 'director': 'dev'}])
        self.assertEqual(sim.emit_signals(
            state, 'task.completed', self._data()), [])
        self.assertEqual(self.governance, [])

    def test_a_state_without_a_roster_keeps_the_idle_gate(self):
        # No agentRoster: an on-duty but idle agent is
        # woken, exactly as before the roster wiring.
        state = {
            'agents': {'ada': self._agent(off_duty=False)},
            'signalPlans': [self._plan()],
        }
        self.assertEqual(len(sim.emit_signals(
            state, 'task.completed', self._data())), 1)


if __name__ == '__main__':
    unittest.main()
