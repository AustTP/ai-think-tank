# Knowledge curator (operator, 2026-11-22): the background organ that closes
# flagged knowledge gaps. Deterministic-first house doctrine: verdict-
# carrying flags (action+target+verdict) become Jev lessons via 392's record
# with NO LLM call -- deny generalizes by host/command-family, allow stays
# exact. Every other open flag gets ONE free-pool LLM triage per flag
# lifetime (attempt-capped, budget-gated by the model-call gate). Wiki- and
# skill-shaped gaps file a proposal quarantined at pending_review/wiki/ and
# ride the existing operator approve/reject flow (280) -- the curator
# creates no new operator surface and never touches the live wiki directly.
# Idempotent per flag via a persisted attempt tally; parent-only; env-gated;
# every pass fails open per flag.
import asyncio
import json
import os
import time

_KC_ENABLED = str(_load_env().get('KNOWLEDGE_CURATOR_ENABLED') or '1') \
    .lower() not in ('0', 'false', 'no')
_KC_CADENCE_S = int(_load_env().get('KNOWLEDGE_CURATOR_CADENCE_S') or 3600)
_KC_MAX_PER_PASS = 8
_KC_TRIAGE_ATTEMPT_CAP = 2


def _kc_attempt_tally(flag_id, increment=True):
    """Read-and-bump the flag's triageAttempts inside the KG lock. Returns
    the pre-increment tally. Fail-open: a store error reads as cap-reached
    (flag stays open, skipped this pass)."""
    try:
        with _KG_LOCK:
            store = _kg_read()
            for flag in store['flags']:
                if flag.get('id') != flag_id:
                    continue
                attempts = int(flag.get('triageAttempts') or 0)
                if increment and attempts < _KC_TRIAGE_ATTEMPT_CAP:
                    flag['triageAttempts'] = attempts + 1
                    _kg_write(store)
                return attempts
            return _KC_TRIAGE_ATTEMPT_CAP  # unknown flag: treat as capped
    except Exception:
        return _KC_TRIAGE_ATTEMPT_CAP  # fail-open to capped: skip this pass


def _kc_record_lesson(flag):
    """Verdict-carrying flag -> Jev lesson via 392. Deterministic path, no
    LLM. Returns the lesson key or None."""
    try:
        key = _jev_lesson_record(
            flag.get('verdictAction') or '',
            flag.get('verdictTarget') or '',
            flag.get('verdict'),
            source=f"curator:{flag.get('agent') or 'agent'}",
            confidence=0.9)
        return key
    except Exception:
        return None


def _kc_file_wiki_proposal(flag):
    """Write a quarantined proposal file with the 280-listing/approve schema
    {id,title,category,summary,body,proposedBy,proposedAt}. Curator-proposed
    bodies are drafts for human review -- never live wiki writes."""
    try:
        page_id = f"curator-{str(flag.get('id') or 'unknown').replace('kg-', '')}"
        category = str(flag.get('kind') or 'operations').strip().lower()
        if category not in ('missing', 'wrong', 'data_access', 'insight'):
            category = 'operations'
        title = f"[curator draft] {str(flag.get('about') or '')[:120]}"
        body = (
            f"## Why this page\nAn agent flagged a knowledge gap while working:\n\n"
            f"- Kind: {flag.get('kind')}\n"
            f"- About: {flag.get('about')}\n"
            f"- Note: {flag.get('note')}\n\n"
            "## Draft\nTODO: curator drafted outline goes here.\n")
        proposal = {
            'id': page_id,
            'title': title,
            'category': category,
            'summary': str(flag.get('note') or '')[:240],
            'body': body,
            'proposedBy': 'knowledge-curator',
            'proposedAt': int(time.time() * 1000),
        }
        rel = f"pending_review/wiki/{category}/{page_id}.json"
        target = _safe_library_path(rel)
        if not target or os.path.isfile(target):
            return None
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(proposal, fh, indent=1, sort_keys=True)
            fh.write('\n')
        os.replace(tmp, target)
        return rel
    except Exception:
        return None


def _kc_triage_prompt(flag):
    about = str(flag.get('about') or '')[:240]
    note = str(flag.get('note') or '')[:400]
    return (
        'A work-agent flagged a knowledge gap while doing its job.\n'
        f"About: {about}\n"
        f"Note: {note}\n"
        'APPROVE means: this is procedural or organizational knowledge '
        'worth persisting as a wiki-page proposal for operator review.\n'
        'DENY means: transient or duplicate noise, nothing to persist.\n'
        'Judge only from the text above.')

def _kc_triage_once():
    """One curator pass: close verdict-carrying flags into lessons, triage
    the rest. Returns the processed count. Per-flag fail-open."""
    processed = 0
    try:
        for flag in _kg_list(status='open'):
            if processed >= _KC_MAX_PER_PASS:
                break
            attempts = _kc_attempt_tally(flag.get('id') or '')
            if attempts >= _KC_TRIAGE_ATTEMPT_CAP:
                continue
            processed += 1
            verdict = flag.get('verdict')
            if verdict in ('approve', 'deny') \
                    and flag.get('verdictAction') and flag.get('verdictTarget'):
                key = _kc_record_lesson(flag)
                if key:
                    _kg_resolve(flag.get('id'), by='curator',
                                note=f'recorded as jev lesson {key}')
                    log_action('system', 'knowledge_curator_lesson',
                               {'flagId': flag.get('id'),
                                'lesson': str(key)[:160]}, authorized=True)
                    continue
                # Lesson write failed: leave open; next pass may retry.
                continue
            prompt = _kc_triage_prompt(flag)
            criteria = {
                'approve': 'Persistence-worthy procedural or organizational '
                           'knowledge: file a wiki proposal.',
                'deny': 'Transient or duplicate noise: do not persist.',
            }
            try:
                label, _conf, _usage = _jev_quorum_choice_sync(
                    prompt, criteria)
            except Exception:
                label = None
            label = (label or '').strip().lower()
            if label == 'approve':
                rel = _kc_file_wiki_proposal(flag)
                if rel:
                    _kg_resolve(flag.get('id'), by='curator',
                                note=f'filed wiki proposal {rel}')
                    log_action('system', 'knowledge_curator_wiki',
                               {'flagId': flag.get('id'), 'path': rel},
                               authorized=True)
                continue
            if label == 'deny':
                _kg_resolve(flag.get('id'), by='curator', note='triaged: none')
                log_action('system', 'knowledge_curator_discard',
                           {'flagId': flag.get('id')}, authorized=True)
                continue
            # label == 'lesson' without a verdict, or malformed/None: leave
            # open for the next pass (attempt already tallied).
    except Exception as e:  # noqa: BLE001
        try:
            log_action('system', 'knowledge_curator_error',
                       {'reason': str(e)[:200]}, authorized=True)
        except Exception:
            pass
    return processed


async def _knowledge_curator_loop():
    while True:
        try:
            if _KC_ENABLED:
                await asyncio.to_thread(_kg_alert_pass)
                await asyncio.to_thread(_kc_triage_once)
        except Exception as e:  # noqa: BLE001
            print(f'[kg-curator] loop error: {e}', flush=True)
        await asyncio.sleep(_KC_CADENCE_S)
