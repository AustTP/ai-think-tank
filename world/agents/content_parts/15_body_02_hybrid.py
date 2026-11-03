
# Hybrid reasoning protocol (2026-11-02), from arxiv 2610.18861
# ("Hybrid Reasoning Agents", Stanford/Together AI/UIUC/Meta).
#
# What the paper trains (HybridLink adapter R(h)=h+W2*sigma(W1*h),
# continuous-token generation, KV-cache working memory, HybridSFT +
# HybridRL) requires white-box model access the tank does not have: every
# agent call goes through /api/chat to OpenRouter-hosted models. What the
# tank CAN implement is the paper's behavioral contract, at the
# orchestration layer:
#
#   latent mode  -- a private reasoning pass whose output is NEVER shown to
#                   collaborators and never re-sent as conversation text;
#                   it is distilled into the agent's working memory for the
#                   task.
#   text mode    -- the existing model calls: tool use, messages,
#                   deliverables.
#   working memory -- per (agent, task) distilled notes stored as readable
#                   pseudo-text under library/hybrid/<agent>/<task>.md (the
#                   paper's interpretability property: latent thoughts are
#                   inspectable, not discarded) plus an optional rag_index
#                   embedding (source='hybrid') for semantic retrieval.
#   HybridLink   -- the bridge from continuous state to next-call input is
#                   the digest compiler: /api/chat prepends the distilled
#                   memory as a system message on every later call for that
#                   task. The file IS the interface; no request flag and no
#                   state read needed.
#   HybridRL     -- the mode policy is heuristic v0 with every switch
#                   decision logged (hybrid_mode rows in action_log); the
#                   tank's grade/completion outcomes are the reward signal
#                   for tuning it later.
#
# Kill switch: HYBRID_MODE=off disables the pre-pass AND the /api/chat
# injection. Per-task overrides: task['hybrid']=True forces the pre-pass,
# task['hybridOff']=True disables it.

import hashlib
import json
import os
import time

_HYBRID_DIR_NAME = 'hybrid'
_HYBRID_MEMORY_MAX_CHARS = 12000     # file cap: newest sections kept
_HYBRID_BRIDGE_MAX_CHARS = 2000      # digest size injected into /api/chat
_HYBRID_PLAN_MAX_TOKENS = 400
_HYBRID_PLAN_COOLDOWN_S = 300        # one pre-pass per task per 5 minutes
_HYBRID_LAST_PLAN_AT = {}            # (agent_id, task_id) -> epoch


def _hybrid_enabled():
    # .env flags in this codebase are read through _load_env() at call time
    # (it returns a dict; os.environ is NOT populated from .env -- only
    # process-exported vars live there). Default 'off' so a deploy without
    # the flag stays inert.
    return str(_serve._load_env().get('HYBRID_MODE', 'off')).strip().lower() != 'off'


def _hybrid_task_eligible(task):
    """Latent-mode eligibility, mirroring the dispatcher's lane logic.

    The paper's division of labor: internal reasoning (planning,
    synthesizing, verifying) moves to latent mode; tool interaction and
    communication stay in text. In tank terms: research / distill /
    spike / note / report lanes plan latently; coding, bugs, meetings and
    reviews do not -- their reasoning already lives in sandbox work and
    structured verdicts, and a private planning pass would just add a call.
    """
    if not isinstance(task, dict):
        return False, 'no-task'
    if task.get('hybrid'):
        return True, 'forced'
    if task.get('hybridOff'):
        return False, 'opted-out'
    if str(task.get('pipelineStep') or ''):
        return False, 'pipeline-step'
    ttype = str(task.get('taskType') or '')
    title = str(task.get('title') or '').lower()
    if ttype in ('meeting', 'review', 'qa', 'supervise'):
        return False, f'type-{ttype}'
    if ttype in ('code', 'bug'):
        deliverable = str(task.get('deliverable') or '').strip().lower()
        if deliverable in ('note', 'report'):
            return True, f'deliverable-{deliverable}'
        return False, 'type-code'
    lane_marked = bool(task.get('distill') or task.get('skillReview')
                       or task.get('research') or ttype == 'research'
                       or 'distill' in title or 'skill review' in title
                       or 'skill-review' in title)
    if lane_marked:
        return True, 'research-lane'
    if ttype in ('spike', 'report', 'note'):
        return True, f'type-{ttype}'
    deliverable = str(task.get('deliverable') or '').strip().lower()
    if deliverable in ('note', 'report'):
        return True, f'deliverable-{deliverable}'
    return False, 'not-eligible'


def _hybrid_memory_path(agent_id, task_id):
    return os.path.join(_serve.LIBRARY_DIR, _HYBRID_DIR_NAME,
                        agent_id, f'{task_id}.md')


def _hybrid_memory_append(agent_id, task, section_text, step='plan'):
    """Append one distilled section to the task's working memory file.

    The file is the durable artifact and the inspectability surface: it
    stays readable pseudo-text exactly so a human (or a later agent) can
    audit what the agent believed while it worked -- the paper's point
    that latent thoughts must remain examinable, not discarded.
    Bounded: when the file exceeds the cap, the OLDEST half is dropped
    (sections are newest-first, so the tail is history).
    """
    path = _hybrid_memory_path(agent_id, task.get('id') or 'unknown')
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        section = (f'\n## {step} -- {stamp}\n\n'
                   + (section_text or '').strip()[:3000] + '\n')
        existing = ''
        if os.path.isfile(path):
            with open(path, encoding='utf-8', errors='replace') as f:
                existing = f.read()
        body = section + existing
        if len(body) > _HYBRID_MEMORY_MAX_CHARS:
            cut = body.find('\n## ', len(body) // 2)
            if cut > 0:
                body = body[:cut] + '\n[... older sections trimmed ...]\n'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(body)
    except OSError:
        return False
    return True


def _hybrid_memory_digest(agent_id, task_id, max_chars=_HYBRID_BRIDGE_MAX_CHARS):
    """The HybridLink analog: compile working memory into the compact form
    the next call's context can carry. Head of the file (newest sections
    first) up to the cap."""
    try:
        with open(_hybrid_memory_path(agent_id, task_id),
                  encoding='utf-8', errors='replace') as f:
            return f.read(max_chars)
    except OSError:
        return ''


def _hybrid_embed_memory(agent_id, task, text):
    """Best-effort semantic retrieval hook: embed the digest into rag_index
    (source='hybrid', ref=task id). The file is durable; this only makes the
    memory FINDABLE by search. Never blocks the run."""
    try:
        _serve._rag_ensure_table()
        vec = _serve._rag_embed(text)
        if not vec:
            return
        doc_id = f'hybrid:{agent_id}:{task.get("id")}'
        now = time.time()
        digest = hashlib.sha256(
            text.encode('utf-8', errors='replace')).hexdigest()
        with _serve._db() as conn:
            conn.execute(
                'INSERT INTO rag_index (doc_id, source, ref, title, room, '
                'village_id, text, vec, grade, outcome, content_hash, '
                'updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(doc_id) DO UPDATE SET text = excluded.text, '
                'vec = excluded.vec, content_hash = excluded.content_hash, '
                'updated_at = excluded.updated_at',
                (doc_id, 'hybrid', task.get('id'), str(task.get('title') or '')[:80],
                 task.get('room'), None, text[:4000], json.dumps(vec), None,
                 None, digest, now))
    except Exception:
        pass


def _hybrid_latent_plan(snapshot, agent_id, task, base, key):
    """One latent-mode pass: think privately, keep the output.

    Contract with the model: dense working notes (plan, unknowns, approach,
    risks, done-checks), written for the agent's own future self, never
    shown to collaborators. The pass runs on the LOW tier -- the paper's
    efficiency argument, in tank terms: private thinking does not need the
    expensive model, and its output REPLACES verbose in-context reasoning
    on later calls. Bounded by _HYBRID_PLAN_COOLDOWN_S per (agent, task).
    Returns the distilled notes, or '' (no notes; never raises).
    """
    now = time.time()
    k = (agent_id, task.get('id'))
    if now - _HYBRID_LAST_PLAN_AT.get(k, 0.0) < _HYBRID_PLAN_COOLDOWN_S:
        return ''
    _HYBRID_LAST_PLAN_AT[k] = now
    tier = _serve._resolve_model_tier(
        f'Private planning notes for task: {task.get("title")}') or '@freepool:low'
    instructions = str(task.get('instructions') or '')[:1200]
    acceptance = str(task.get('acceptanceCriteria') or '')[:600]
    prior = _hybrid_memory_digest(agent_id, task.get('id'), max_chars=1600)
    sys_msg = (
        'You are the PRIVATE reasoning channel of a think-tank agent. '
        'Nobody sees this output except the agent\'s later turns. Produce '
        'dense working notes for the task below: (1) what is actually being '
        'asked, (2) unknowns and how to check them, (3) a concrete approach '
        'with steps, (4) risks/failure modes, (5) how you will know it is '
        'done. Under 300 words. No preamble, no politeness.')
    user_msg = (f'TASK: {task.get("title")}\n'
                + (f'\nINSTRUCTIONS:\n{instructions}\n' if instructions else '')
                + (f'\nACCEPTANCE:\n{acceptance}\n' if acceptance else '')
                + (f'\nPRIOR WORKING NOTES (yours):\n{prior}\n' if prior else ''))
    try:
        r = _serve._http_json('POST', base, '/api/chat',
                              {'model': tier,
                               'messages': [{'role': 'system', 'content': sys_msg},
                                            {'role': 'user', 'content': user_msg}],
                               'max_tokens': _HYBRID_PLAN_MAX_TOKENS,
                               'agentId': agent_id,
                               'taskId': task.get('id'),
                               'hybridBypass': True}, key)
        notes = (r.get('reply') or '').strip() if isinstance(r, dict) else ''
        if not notes and isinstance(r, dict):
            # No exception but no reply either: the chat endpoint returned an
            # error body (e.g. {'error': ...} from a dead pool). Capture it
            # so the latent-failed row says WHY instead of 'None'.
            _plan_err = {'replyKeys': sorted(r.keys()),
                         'error': str(r.get('error') or '')[:120]}
    except Exception as _exc:
        # NOT 'except ... as _plan_err': Python unbinds the as-name when the
        # except block exits, which would leave _plan_err undefined exactly
        # on the failure path we are trying to report.
        notes = ''
        _plan_err = repr(_exc)
    else:
        _plan_err = None
    if not notes:
        # A failed pre-pass must be visible: without this row a free-pool
        # outage looks exactly like 'the protocol never ran' (the live
        # 2026-11-02 confusion -- zero hybrid rows while the pool's circuits
        # were broken made the silent design indistinguishable from a bug).
        _serve.log_action(agent_id, 'hybrid_mode',
                          {'decision': 'latent-failed', 'task': task.get('id'),
                           'step': 'plan', 'error': str(_plan_err)[:200]},
                          authorized=True)
        return ''
    _hybrid_memory_append(agent_id, task, notes, step='plan')
    _hybrid_embed_memory(agent_id, task, notes)
    _serve.log_action(agent_id, 'hybrid_mode',
                      {'decision': 'latent', 'task': task.get('id'),
                       'step': 'plan', 'chars': len(notes),
                       'policy': _serve._load_env().get('HYBRID_MODE', 'off')},
                      authorized=True)
    return notes


def _hybrid_prepare(snapshot, agent_id, task, base_ctx):
    """Dispatcher hook: decide the mode, run the latent pre-pass when
    eligible, and hand the compiled memory to the executor via base_ctx.

    Called once per content dispatch, right before the executor runs. The
    pre-pass is the paper's 'think latently before acting through text':
    the notes land in working memory, and /api/chat injects them into every
    later call for this task -- so the executors need no changes at all to
    benefit. base_ctx['hybridMemory'] is also set for executors that want
    the digest directly."""
    if not isinstance(base_ctx, dict):
        # Defensive: proceed with a local dict so the mode decision is still
        # made and logged even if a caller forgets to materialize base_ctx.
        # (The dispatcher now materializes it -- this guard remains so a
        # stray None can never again silently skip the whole protocol.)
        base_ctx = {}
    base_ctx['hybridMemory'] = _hybrid_memory_digest(agent_id, task.get('id'))
    if not _hybrid_enabled():
        return
    eligible, reason = _hybrid_task_eligible(task)
    if not eligible:
        _serve.log_action(agent_id, 'hybrid_mode',
                          {'decision': 'text', 'task': task.get('id'),
                           'reason': reason}, authorized=True)
        return
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    notes = _hybrid_latent_plan(snapshot, agent_id, task, base, key)
    if notes:
        base_ctx['hybridMemory'] = _hybrid_memory_digest(agent_id, task.get('id'))

