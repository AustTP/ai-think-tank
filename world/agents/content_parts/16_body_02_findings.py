
# Shared findings buffer (2026-11-27), from arXiv 2610.31355 ("Muffin - A
# Shared Memory Buffer for Asynchronous AI Agents", UTN Nuremberg).
#
# What the paper builds (an orchestrator-owned shared memory buffer:
# subagents emit SUMMARY/SHARE_FINDING into an append-only store, every step
# reads the k most recent findings from OTHER agents, a summarizer writes
# the final answer from summaries alone) needs no model training and no
# new tool surface -- the tank's equivalent is exactly this hook pair:
#
#   write -- _apply_content_result (sim) appends one finding per completed
#            run into library/findings/<root-request>.jsonl
#            (agents/findings_buffer.py).
#   read  -- _findings_prepare here: at EVERY dispatch on the same request
#            the executor's perception string gains the k most recent
#            findings from OTHER agents, oldest-last. Early findings are
#            the paper's highest-value shares, which is why this reads
#            every dispatch instead of once at the end.
#
# Executor-invisible by design, like the 15_body_02_hybrid pre-pass:
# base_ctx is a perception STRING, and folding the digest here means zero
# changes in the executors themselves. Unlike hybrid this carries no model
# call and no cost: it only surfaces what peer runs already learned, so
# review N can see review N-1's finding and a fix pass can see the
# reviewer's verdict without re-reading the archive. Kill switch:
# FINDINGS_BUFFER=off (agents/findings_buffer.enabled()).

import agents.findings_buffer as findings_buffer


def _findings_prepare(snapshot, agent_id, task, base_ctx):
    """Dispatcher hook: return the compiled peer-findings digest (or '').

    Called once per content dispatch, right before the executor runs, next
    to the hybrid pre-pass. Best-effort by contract -- a buffer failure
    must never break the dispatch that follows it. A non-empty fold logs a
    findings_digest row (hybrid_mode precedent) so a live deploy can show
    the read side actually fires instead of silently never mattering."""
    try:
        if not findings_buffer.enabled():
            return ''
        entries = findings_buffer.recent(snapshot, agent_id, task,
                                         k=findings_buffer._DEFAULT_K)
        if not entries:
            return ''
        text = findings_buffer.digest(entries)
        _serve.log_action(agent_id, 'findings_digest',
                          {'task': task.get('id'), 'peers': len(entries),
                           'chars': len(text)}, authorized=True)
        return text
    except Exception:
        return ''
