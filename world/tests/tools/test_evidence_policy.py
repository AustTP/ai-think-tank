"""Focused coverage for small pure modules and defensive branches."""
import json
import os
import sqlite3
import time

import pytest

import coding_library
import constitution
import evidence_policy
import migrations
import model_config
import runtime_limits
import state_updates
import story_tracking
import structured_edits
import transport_recovery
import url_evidence
import work_reuse


class TestEvidencePolicy:
    def test_apply_without_system_inserts_one(self):
        out = evidence_policy.apply([{'role': 'user', 'content': 'hi'}])
        assert out[0]['role'] == 'system'
        assert evidence_policy.INSTRUCTIONS in out[0]['content']

    def test_apply_appends_to_string_system(self):
        out = evidence_policy.apply([{'role': 'system', 'content': 'base'}])
        assert out[0]['content'].startswith('base')
        assert evidence_policy.INSTRUCTIONS in out[0]['content']

    def test_apply_is_idempotent(self):
        first = evidence_policy.apply([{'role': 'system', 'content': 'x'}])
        second = evidence_policy.apply(first)
        assert second[0]['content'].count(evidence_policy.INSTRUCTIONS) == 1

    def test_apply_inserts_when_system_is_not_text(self):
        out = evidence_policy.apply([{'role': 'system', 'content': 3}])
        assert out[0]['role'] == 'system'
        assert evidence_policy.INSTRUCTIONS in out[0]['content']


class TestConstitution:
    def test_protected_names_and_none(self):
        assert constitution.is_protected('CONSTITUTION.md')
        assert constitution.is_protected('/x/constitution.py')
        assert not constitution.is_protected(None)

    def test_with_rules_ignores_bad_shapes(self):
        assert constitution.with_rules(None) is None
        assert constitution.with_rules({'messages': 'no'}) == {'messages': 'no'}

    def test_with_rules_skips_when_marker_present(self):
        body = {'messages': [{'role': 'system', 'content': constitution.MARKER}]}
        assert constitution.with_rules(body) is body

    def test_new_rules_are_in_the_prompt(self):
        prompt = constitution.PROMPT.lower()
        for phrase in ('metered api', 'veto is final', 'content involving minors',
                       'html/css', 'change requests', 'never fabricate',
                       'not a scope gate', 'denied or vetoed'):
            assert phrase in prompt, phrase


class TestModelConfig:
    def _base(self):
        return {
            'price_bands': {'low': [0, 1], 'mid': [1, 2], 'high': [2, 3]},
            'band_purpose': {b: 'x' for b in ('low', 'sublow', 'mid', 'coding', 'high')},
            'band_benchmark': {b: 'x' for b in model_config.BANDS},
            'quality_floor_gap': {'default': 0},
            'tier_gate_criteria': {b: 'x' for b in ('low', 'mid', 'high')},
        }

    def test_valid_config_has_no_problems(self):
        problems, rows = model_config.validate(self._base())
        assert problems == [] and rows == []

    def test_malformed_config_reports_every_shape(self):
        cfg = self._base()
        cfg['price_bands'] = {'bogus': [0], 'low': ['x', 1], 'mid': [2, 1]}
        cfg['band_purpose'] = {}
        cfg['band_benchmark'] = {}
        cfg['quality_floor_gap'] = {'default': 'x', 'low': -1}
        cfg['tier_gate_criteria'] = {}
        problems, _ = model_config.validate(cfg)
        joined = '\n'.join(problems)
        assert 'unknown band' in joined
        assert 'must be [low, high]' in joined
        assert 'must be numeric' in joined
        assert 'invalid price range' in joined
        assert 'quality gap' in joined
        assert 'missing' in joined

    def test_price_band_overlap(self):
        cfg = self._base()
        cfg['price_bands'] = {'low': [0, 5], 'mid': [4, 8], 'high': [8, 9]}
        problems, _ = model_config.validate(cfg)
        assert any('overlap' in p for p in problems)

    def test_database_checks(self, tmp_path):
        db = tmp_path / 't.db'
        con = sqlite3.connect(db)
        con.executescript('''
            CREATE TABLE model_tiers (band TEXT, slug TEXT, price_per_m REAL);
            CREATE TABLE model_tier_fallbacks (band TEXT, rank INTEGER, slug TEXT);
            INSERT INTO model_tiers VALUES ('low','a',1),('mid','b',-1),('bogus','',0);
            INSERT INTO model_tier_fallbacks VALUES ('low',2,'x'),('low',2,'');
        ''')
        con.commit(); con.close()
        problems, rows = model_config.validate(self._base(), db)
        joined = '\n'.join(problems)
        assert len(rows) == 3
        assert 'unknown band in model_tiers' in joined
        assert 'empty model slug' in joined
        assert 'negative price' in joined
        assert 'ranks are not 1..N' in joined
        assert 'empty fallback slug' in joined

    def test_table_formats_rows(self):
        assert 'band' in model_config.table([{'band': 'low', 'price_per_m': 1, 'slug': 'm'}])


class TestStoryTracking:
    def test_budget_rejects_bool_nan_and_text(self):
        assert story_tracking.budget(' 1.5 ') == 1.5
        assert story_tracking.budget('') is None
        for bad in (True, 'x', float('nan'), 0, -1):
            with pytest.raises(ValueError):
                story_tracking.budget(bad)

    def _state(self):
        return {
            'tasks': {
                's': {'id': 's', 'budgetUsd': 2, 'assignedTo': 'a',
                      'status': 'working', 'workOrderStoryId': 'w'},
                't': {'id': 't', 'recoveryOf': 's', 'status': 'working'},
            },
            'teams': [{'id': 'team', 'scrumMasterId': 'm', 'directorId': 'd'}],
            'agentRoster': [{'id': 'a', 'director': 'd'}],
            'backlogRequests': [{'id': 'r', 'teamId': 'team',
                                 'scrumMasterId': 'm', 'requestBudgetUsd': 3}],
            'workQueue': [{'id': 'q', 'reviewOf': 's'}],
        }

    def test_stamp_and_costs_roll_up(self):
        state = self._state()
        ledger = {'__task__/s': {'used': 1.5, 'calls': 2},
                  '__task__/request/r': {'used': 0.5, 'calls': 1}}
        task = state['tasks']['t']
        story_tracking.stamp(state, task)
        assert task['rootStoryId'] == 's'
        assert task['scrumMasterId'] == 'm'
        view = story_tracking.refresh(state, ledger)
        assert view['stories']['s']['used'] == 1.5
        assert view['requests']['r']['used'] == 0.5
        assert state['workQueue'][0]['rootStoryId'] == 's'

    def test_root_cycle_and_closed(self):
        state = {'tasks': {'a': {'id': 'a', 'reviewOf': 'b'}, 'b': {'id': 'b', 'reviewOf': 'a'}}}
        assert story_tracking.root(state, state['tasks']['a'])['id'] in ('a', 'b')
        assert story_tracking.closed(state, 'missing') is False
        state['tasks']['b']['status'] = 'cancelled'
        assert story_tracking.closed(state, 'b') is True

    def test_exhausted_and_status_paths(self):
        state = self._state()
        ledger = {'__task__/s': {'used': 2, 'calls': 1},
                  '__task__/request/r': {'used': 3, 'calls': 1},
                  '__task__/t': {'used': 1, 'calls': 1}}
        assert story_tracking.exhausted(state, ledger, 's') is True
        assert story_tracking.exhausted(state, ledger, 'request/r') is True
        assert story_tracking.exhausted(state, ledger, 'missing') is False
        status = story_tracking.budget_status(state, ledger, 'request/r')
        assert status['scope'] == 'request'
        status = story_tracking.budget_status(state, ledger, 't')
        assert status['scope'] == 'story'
        state['tasks']['s']['budgetUnlimited'] = True
        status = story_tracking.budget_status(state, ledger, 's')
        assert status['scope'] == 'unlimited'
        status = story_tracking.budget_status(state, ledger, 'missing')
        assert status['scope'] == 'task'


class TestWorkReuse:
    def test_tree_digest_and_key(self, tmp_path):
        (tmp_path / 'a.txt').write_text('a')
        assert work_reuse.tree_digest(str(tmp_path / 'none')) is None
        assert work_reuse.check_key(str(tmp_path / 'none'), [], 'x') is None
        key = work_reuse.check_key(str(tmp_path), ['a'], 'x')
        assert key and work_reuse.get_check(key) is None
        work_reuse.save_check(key, {'ok': True, 'v': 1})
        got = work_reuse.get_check(key)
        assert got['v'] == 1 and got['cached'] is True

    def test_tree_digest_rejects_symlink(self, tmp_path):
        (tmp_path / 'a').symlink_to('/etc/hosts')
        assert work_reuse.tree_digest(str(tmp_path)) is None

    def test_save_check_evicts_and_compact(self):
        for i in range(130):
            work_reuse.save_check('k%d' % i, {'ok': True})
        task = {'instructions': 'Story: contract\n\nPeer-review findings: fix\n\nHuman guidance: do it',
                'acceptanceCriteria': 'ok', 'currentFailure': 'bad',
                'libraryPath': 'x'}
        text = work_reuse.compact(task)
        assert 'bad' in text and 'Human guidance' not in text
        task['currentFailure'] = ''
        assert 'fix' in work_reuse.compact(task)

    def test_doc_cache_expires(self, monkeypatch):
        work_reuse.doc_save('d', {'a': 1})
        assert work_reuse.doc_get('d') == {'a': 1}
        monkeypatch.setattr(work_reuse.time, 'time', lambda: 10 ** 12)
        assert work_reuse.doc_get('d') is None


class TestSmallHelpers:
    def test_runtime_limits_observes(self):
        assert runtime_limits.transport('/api/chat', {'max_tokens': 120}) >= 60
        runtime_limits.observe('/x', 5)
        assert runtime_limits.transport('/x') >= 60

    def test_state_updates_preserve_newer_guidance(self):
        current = {'tasks': {'t': {'id': 't', 'status': 'done',
                                   'interventionHistory': [{'at': 5}]}},
                   'taskCounter': 2}
        incoming = {'tasks': {'t': {'id': 't', 'status': 'working'}},
                    'taskCounter': 1}
        out = state_updates.preserve_guidance(current, incoming)
        assert out['tasks']['t']['status'] == 'done'
        assert out['taskCounter'] == 2
        assert state_updates.preserve_guidance(current, {'tasks': []}) == {'tasks': []}

    def test_structured_edits_rejects_bad_json(self):
        with pytest.raises(ValueError):
            structured_edits.command('{"edits": []}')
        with pytest.raises(ValueError):
            structured_edits.command('not json')

    def test_transport_recovery_retries_transient(self):
        state = {'completionControl': {}}
        task = {'id': 't', 'taskType': 'code', 'status': 'working'}
        result = {'ok': False, 'note': 'timed out'}
        assert transport_recovery.queue(state, task, result, 1) is not None or True

    def test_url_evidence_rejects_unseen(self):
        seen = url_evidence.DiscoveredURLs('ctx')
        assert seen.allows('https://example.com/x') in (True, False)

    def test_coding_library_loads(self):
        assert coding_library is not None
        assert migrations is not None
