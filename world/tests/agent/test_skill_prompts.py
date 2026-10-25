"""Deterministic loading for skill files that are also executed prompts."""
from __future__ import annotations

import os
import time

import agents.skill_prompts as skill_prompts


def test_shipped_report_skill_loads():
    text = skill_prompts.load_skill('report-writing')
    assert 'ADOPT / REJECT' in text
    assert 'Cite the source path' in text


def test_shipped_meeting_skill_loads():
    text = skill_prompts.load_skill('meeting-minutes')
    assert '**Action items**' in text
    assert 'Under 200 words' in text


def test_prompt_view_drops_citations_and_lessons():
    text = skill_prompts.load_skill_prompt('report-writing')
    assert '## Key facts' in text
    assert 'Cite the source path' in text
    assert 'world/content.py' not in text
    assert 'Lessons learned' not in text


def test_missing_skill_uses_fallback(tmp_path):
    assert skill_prompts.load_skill(
        'missing', fallback='fallback', skills_dir=tmp_path) == 'fallback'


def test_path_traversal_is_rejected(tmp_path):
    (tmp_path / 'secret.md').write_text('secret', encoding='utf-8')
    assert skill_prompts.load_skill(
        '../secret', fallback='fallback', skills_dir=tmp_path) == 'fallback'
    assert skill_prompts.load_skill(
        '.hidden', fallback='fallback', skills_dir=tmp_path) == 'fallback'


def test_empty_skill_uses_fallback(tmp_path):
    (tmp_path / 'empty.md').write_text('   ', encoding='utf-8')
    assert skill_prompts.load_skill(
        'empty', fallback='fallback', skills_dir=tmp_path) == 'fallback'


def test_changed_skill_is_reloaded(tmp_path):
    path = tmp_path / 'demo.md'
    path.write_text('one', encoding='utf-8')
    assert skill_prompts.load_skill('demo', skills_dir=tmp_path) == 'one'
    path.write_text('two', encoding='utf-8')
    stamp = time.time_ns() + 1_000_000
    os.utime(path, ns=(stamp, stamp))
    assert skill_prompts.load_skill('demo', skills_dir=tmp_path) == 'two'
