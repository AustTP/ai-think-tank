"""Deterministic lane routing plus the completion-gate contract.

These are the cases that caused the 2026-10-25 loops: a code card queued into
media, a flagless Distill card, and a published code card with no Library
record. None of these tests spend a model call or touch live state.
"""
from unittest import mock

import sim
import content


def _dispatch(task):
    with mock.patch.object(content, '_run_coding_content') as code, \
         mock.patch.object(content, '_run_media_content') as media, \
         mock.patch.object(content, '_run_observatory_content') as obs, \
         mock.patch.object(content, '_run_report_content') as report, \
         mock.patch.object(content, '_run_review_content') as review, \
         mock.patch.object(content, '_run_pipeline_step_content') as pipeline, \
         mock.patch.object(content, '_run_workroom_content') as workroom:
        content._server_content_dispatcher({}, 'cora', task)
    return {name: fn.call_count for name, fn in (
        ('code', code), ('media', media), ('observatory', obs),
        ('report', report), ('review', review), ('pipeline', pipeline),
        ('workroom', workroom))}


def test_ui_code_card_in_media_still_uses_coding_lane():
    lanes = _dispatch({'id': 't1', 'taskType': 'code', 'room': 'media',
                       'title': 'Add the south outlet selector'})
    assert lanes['code'] == 1
    assert lanes['media'] == 0


def test_media_card_uses_media_lane():
    lanes = _dispatch({'id': 't2', 'taskType': 'media', 'room': 'media',
                       'title': 'Digest the feeds'})
    assert lanes['media'] == 1
    assert lanes['code'] == 0


def test_research_flag_and_distill_title_use_observatory():
    lanes = _dispatch({'id': 't3', 'taskType': 'code', 'room': 'media',
                       'title': 'Research the market', 'research': True})
    assert lanes['observatory'] == 1
    lanes = _dispatch({'id': 't4', 'taskType': 'code', 'room': 'observatory',
                       'title': 'Fix: Distill recent think tank knowledge'})
    assert lanes['observatory'] == 1


def test_report_and_review_are_room_agnostic():
    assert _dispatch({'id': 't5', 'taskType': 'report', 'room': 'media'})['report'] == 1
    assert _dispatch({'id': 't6', 'taskType': 'review', 'room': 'media'})['review'] == 1


def test_pipeline_step_overrides_everything():
    assert _dispatch({'id': 't7', 'taskType': 'code', 'room': 'media',
                      'pipelineStep': {'pipelineId': 'p', 'stepIndex': 0}})['pipeline'] == 1


def test_code_card_in_pressoffice_uses_workroom():
    assert _dispatch({'id': 't8', 'taskType': 'code', 'room': 'pressoffice'})['workroom'] == 1


def test_distill_gate_accepts_a_recorded_wiki_note():
    task = {'id': 'd1', 'title': 'Distill recent think tank knowledge',
            'taskType': 'code', 'distill': True, 'distillSince': 0,
            'note': 'Distilled 3 findings into the think tank wiki (v2).'}
    assert sim._completion_has_evidence({}, task) is True


def test_distill_gate_refuses_an_empty_note():
    task = {'id': 'd2', 'title': 'Distill recent think tank knowledge',
            'taskType': 'code', 'distill': True, 'distillSince': 0, 'note': ''}
    assert sim._completion_has_evidence({}, task) is False


def test_code_gate_requires_a_publish_or_library_record():
    base = {'id': 'c1', 'title': 'Fix the thing', 'taskType': 'code'}
    assert sim._completion_has_evidence({}, dict(base, libraryPath='archive/c1.md')) is True
    assert sim._completion_has_evidence({}, dict(base, codePublished=True)) is True
    assert sim._completion_has_evidence({}, dict(base)) is False


def test_bug_gate_accepts_written_python_evidence():
    task = {'id': 'b1', 'title': 'Fix the bug', 'taskType': 'bug', 'writtenPy': 'fix.py'}
    assert sim._completion_has_evidence({}, task) is True
