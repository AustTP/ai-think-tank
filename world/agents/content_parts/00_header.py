"""Server-owned content executors: the per-room real-work runners.

Extracted from serve.py (phase-consolidation): this module holds every `_run_*`
content executor (research/media/skill-review/research-project/research-
bare/bank/coding/review/product-build/spike/workroom), the dispatcher
(_server_content_dispatcher), and the private helpers only those executors use
(_gather_unified_context, _parse_probe_request, _format_page_probe_result,
_review_screenshot, _wiki_context_for_task, _run_quality_pipeline,
_release_product_from_build, _agent_name).

The executors are a *consumer* of serve.py's services: they call through to
serve's HTTP/IAM/tier/config layer as `_serve.<name>` (module-qualified, so the
hermetic tests that patch `serve._coding_tier_slug` etc. keep working), and they
consume sim's `_store_content_result` exactly as before. serve.py registers
`_server_content_dispatcher` as sim._content_executor at startup (lifespan) and
imports THIS module lazily to avoid an import-time cycle.

Each `_run_*` signature is (snapshot, agent_id, task, base_ctx=None) and stores
its result via the sim module's _store_content_result -- the same seam sim.py's
task-cycle merges via _apply_content_result.
"""

import datetime
import hashlib
import json
import os
import random
import re
import time
import urllib.parse

import serve as _serve
import core.evidence_policy as evidence_policy
import agents.review_quality as review_quality
import core.retrieval as retrieval
import agents.skill_prompts as skill_prompts
import agents.skill_scan as skill_scan
import agents.training_capture as training_capture
import core.url_evidence as url_evidence
import agents.work_reuse as work_reuse

from core.config import RESEARCH_DESK_PASSES, bind_policy

from core.tool_policy import (task_tool_policy, tool_policy, tool_replay_safety,
                            tool_trust_tier)
from agents.pulse import execute_pulse_tool


def _chat_error_result(r, fallback_note):
    """Item 4: when /api/chat refuses a call carrying a taskId because that
    task's model-spend budget is exhausted (429 {'budgetExhausted': True}),
    return a fail-closed result the executor can store directly. Returns None
    for every OTHER failure shape so existing failure handling is untouched."""
    if isinstance(r, dict) and r.get('budgetExhausted'):
        return {'ok': False,
                'budgetExhausted': True,
                'taskSpendUsd': r.get('taskSpendUsd'),
                'taskSpendAttempts': r.get('taskSpendAttempts'),
                'note': (fallback_note or
                         'This task used its full model-spend budget and was paused before any further model call.')}
    return None


# Bell-style first-principles pass (2026-10-06): folded into research synthesis
# and spike planning so the model questions the FRAME of a question before
# answering it -- Bell Labs did fundamental science because people asked why
# before how. Pure prompt guidance (token cost near zero at the cheap tier).
PREMISE_QUESTIONING_GUIDANCE = (
    'QUESTION THE PREMISE before you answer: state the assumption buried in the '
    'question itself and ask whether it is even the right question to be asking. '
    'Challenge the frame -- the most valuable finding is often that the frame was '
    'wrong. Distinguish what you actually verified from what you are assuming.'
)

# Deterministic fallbacks keep the two task playbooks usable even if the
# operator-editable skill files are missing. Normal runs load the instruction
# sections from library/skills through skill_prompts.load_skill_prompt().
_REPORT_WRITING_FALLBACK = (
    'Write the report requested by the card below, using ONLY the source text '
    'supplied here as evidence. Cite the source path for each claim. If a source '
    'has no timestamps, write "timestamps unavailable" and do not invent any. If a '
    'source is missing, say so explicitly and make no claims from it. Do not mention '
    'tools, tool failures, or any content that is not in the sources. Keep the '
    'addendum under 900 words. End with one revised final recommendation line: '
    'ADOPT / REJECT / ADOPT (limited), with conditions.'
)
_MEETING_MINUTES_FALLBACK = (
    'Write plain Markdown with exactly these sections: Topic; Attendees; Discussion '
    '(2-4 bullets in the participants\' voices); Decisions; Action items. Stay inside '
    'the agenda. Invent no external facts or decisions the agenda cannot support. '
    'Respect any NEVER section. Under 200 words.'
)


# The Research Desk's multi-pass search structure (article section 4): give
# the desk several small questions rather than one giant query, one pass per
# question. Each pass records its question, window, and links so the run is
# auditable. These are folded into the research synthesis prompt; they are
# research instructions, not a promise of exhaustive X coverage.
# Loaded from world/research_desk.json by world/config.py (operator-edited).


def _record_source_evidence(base, key, agent_id, source_url, resolved_url, text, lastmod,
                            topic_id, task_id, claim_type='unclassified'):
    """POST one candidate source to the server's evidence ledger (/api/evidence).
    The server applies source identity (resolved landing URL -- a short link's
    raw string is never the identity), the coverage gate, and the repost rule.
    `agent_id` is the real research agent (its key is in `key`), so the POST
    passes the endpoint's agent-key auth. Returns the decision dict:
    - {'recorded': True, ...} -> keep the page
    - {'recorded': False, 'reason': 'covered'} -> skip (already covered, no
      material update)
    - {'recorded': False, 'reason': 'invalid'} -> malformed; keep the page but
      don't record (a candidate page is still useful for the crawl).
    Best-effort: an evidence endpoint failure must never abort a crawl, so a
    failed POST degrades to 'recorded' (the page proceeds)."""
    record = {
        'source_url': source_url,
        'resolved_url': resolved_url or None,
        'claim': (text or '')[:200] or 'Unclassified candidate from scheduled research',
        'claim_type': claim_type,
        'scope': 'Candidate from scheduled research crawl; not independently verified',
        'lastModified': lastmod,
        'topicId': topic_id,
        'taskId': task_id,
    }
    try:
        r = _serve._http_json('POST', base, '/api/evidence',
                              {'agentId': agent_id, 'record': record}, key)
        if isinstance(r, dict) and r.get('recorded') is False:
            return r
        return r if isinstance(r, dict) else {'recorded': True}
    except Exception:
        return {'recorded': True}


def _note_research_coverage(base, key, agent_id, identity, artifact, reviewed=False,
                            source_url=None, resolved_url=None, topic_id=None,
                            run_id=None):
    """POST a coverage entry (reviewed=False from the crawl -> pending_review;
    the operator/Verifier later marks reviewed=True -> covered)."""
    try:
        _serve._http_json('POST', base, '/api/coverage', {
            'agentId': agent_id,
            'identity': identity,
            'artifact': artifact,
            'reviewed': reviewed,
            'eventId': topic_id,
            'sourceUrl': source_url,
            'resolvedUrl': resolved_url,
            'runId': run_id,
        }, key)
    except Exception:
        pass


def _note_research_failure(base, key, agent_id, identity, reason, source_url=None,
                           resolved_url=None, topic_id=None):
    """POST a failed run against a source identity. A failure never suppresses
    the next attempt (only 'covered' does)."""
    try:
        _serve._http_json('POST', base, '/api/coverage', {
            'agentId': agent_id,
            'identity': identity,
            'failure': reason,
            'eventId': topic_id,
            'sourceUrl': source_url,
            'resolvedUrl': resolved_url,
        }, key)
    except Exception:
        pass



def _run_research_content(snapshot, agent_id, task, base_ctx=None):
    """Port of runResearchTask's scheduled-topic branch (tasks.js:2201-2259) +
    crawlAndCollect (world.js:529+). Runs the real crawl > save > synthesize >
    writeSkillFile pipeline, then stores the result for the next task_cycle pass.
    Executed on a background thread by sim._dispatch_content_work (room-agnostic
    4-arg signature); resolves the topic from the snapshot's researchTopics."""
    import sim as _sim_module
    research = task.get('research') or {}
    topic = None
    for t in (snapshot.get('researchTopics') or []):
        if t.get('id') == research.get('topicId'):
            topic = t
            break
    if topic is None:
        # Topic vanished since scheduling: fall back to the placeholder budget.
        _sim_module._store_content_result(task.get('id'),
                                          {'note': 'Scheduled research arrived with no matching topic record.', 'seenUrls': []})
        return
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    since = research.get('since') or 0
    seen = set(topic.get('seenUrls') or [])
    purpose = f"scheduled research: {topic.get('topic')}"
    link_kw = (topic.get('linkKeyword') or '').lower()
    page_kw = (topic.get('pageKeyword') or '').lower()
    sandbox_id = _serve._SANDBOX_RESEARCH_ID
    in_room = 'observatory'

    # crawlAndCollect: BFS over the frontier, /api/browse each, save kept pages.
    # Each kept page is ALSO routed through the Research Desk evidence ledger:
    # the browse response carries the RESOLVED landing URL (a short link's raw
    # string is never the source identity), and the /api/evidence decision
    # tells us whether to skip a page whose source is already covered with no
    # material update.
    visited = []
    frontier = [topic.get('startUrl')]
    kept = []
    while frontier and len(visited) < _serve.RESEARCH_CRAWL_MAX_PAGES:
        url = frontier.pop(0)
        if url in visited:
            continue  # pragma: no cover -- frontier is deduped at enqueue, a URL already visited can never be re-popped
        visited.append(url)
        data = _serve._http_json('POST', base, '/api/browse',
                          {'url': url, 'purpose': purpose, 'agentId': agent_id}, key)
        if data.get('error'):
            continue
        if not data.get('allowed'):
            continue
        text = data.get('text') or ''
        lastmod = data.get('lastModified') or 0
        resolved_url = (data.get('url') or url)
        changed = bool(lastmod) and lastmod > since
        already = url in seen
        if already and not changed:
            pass  # dedup: already collected and unchanged since -- skip save
        else:
            should_keep = (not page_kw) or (page_kw in text.lower())
            if should_keep:
                # Evidence gate first: skip a source that is already COVERED
                # (reviewed brief) without a material update. The server's
                # /api/evidence decides on the RESOLVED identity, so two short
                # links that LOOK the same but land on different sites are
                # never conflated, and a repost is never saved as a new find.
                ev = _record_source_evidence(base, key, agent_id, url, resolved_url, text,
                                             lastmod, topic.get('id'), task.get('id'))
                if ev.get('recorded') is False and ev.get('reason') == 'covered':
                    continue  # covered identity, no material update -- skip the duplicate page
                idx = len(kept) + 1
                saved = _serve._http_json('POST', base, '/api/sandbox-save-page',
                                   {'agentId': agent_id, 'sandboxId': sandbox_id,
                                    'url': url, 'purpose': purpose,
                                    'filename': f'crawl-{idx}.txt', 'content': text,
                                    'inRoom': in_room}, key)
                if saved.get('allowed') and saved.get('ok'):
                    kept.append({'url': url, 'resolved_url': resolved_url,
                                 'text': text, 'lastModified': lastmod})
        # Extend the frontier with matching links (dedup against visited/queued).
        for link in (data.get('links') or []):
            lu = link.get('url')
            if not lu or lu in visited or lu in frontier:
                continue
            hay = ((link.get('text') or '') + ' ' + lu).lower()
            if (not link_kw) or (link_kw in hay):
                frontier.append(lu)

    # (manifest.json write is omitted -- the browser saves it, but nothing reads
    # it back server-side; skip the extra call. Kept pages are the durable out.)

    # The pass log makes the multi-pass search audit-visible: which question
    # was asked, over what window, and what the run actually saw. The article's
    # rule -- "a missing result is not proof that nobody reported a problem" --
    # is the point of writing this down instead of asserting coverage.
    pass_log = [{
        'key': p['key'],
        'question': p['question'],
        'window_since_ms': since,
        'pages_checked': len(visited),
        'candidates_found': sum(1 for k in kept if k.get('url')),
    } for p in RESEARCH_DESK_PASSES]

    # Synthesize the updated skill file from freshly collected sources.
    note = None
    artifact = f"skills/{_skill_slug(topic.get('topic'))}.md"
    run_id = task.get('id')
    if kept:
        existing = _serve._http_json('GET', base, '/api/library/file?path=' +
                              urllib.parse.quote(artifact) + '&requesterId=' + urllib.parse.quote(agent_id), None, key)
        existing_content = existing.get('content') if isinstance(existing, dict) and 'content' in existing else None
        tier_slug = _serve._resolve_model_tier(f'Synthesize an updated skill-reference file for research topic: {topic.get("topic")}')
        if not tier_slug:
            note = f'Collected {len(kept)} new page(s) for "{topic.get("topic")}", but synthesis failed (no model tier).'
            for k in kept:
                _note_research_failure(base, key, agent_id, _serve.source_identity(k['url'], k['resolved_url'])[0],
                                       'synthesis failed (no model tier)', source_url=k['url'],
                                       resolved_url=k['resolved_url'], topic_id=topic.get('id'))
        else:
            sources_text = '\n\n'.join(
                f'### {p["url"]}\n{(p.get("text") or "")[:3000]}' for p in kept)
            passes_text = '\n'.join(
                f'- Pass {i + 1} ({p["key"]}): {p["question"]}'
                for i, p in enumerate(RESEARCH_DESK_PASSES))
            sys_msg = (f'You are updating a real skill-reference file for "{topic.get("topic")}". '
                       f'{_skill_file_format_guide()} '
                       f'Answer the desk passes explicitly (one section each):\n{passes_text}\n'
                       'Separate what a source actually supports from our inference. '
                       'Do not treat a repost as a second independent source. '
                       'Note limits, uncertainty, and anything you could not check. '
                       + PREMISE_QUESTIONING_GUIDANCE + ' '
                       + (f'Here is the EXISTING skill file -- preserve what still holds, update what changed, add what is genuinely new:\n\n{existing_content[:6000]}'
                          if existing_content else 'No existing skill file yet -- write one from scratch.'))
            reply = None
            r = None
            try:
                r = _serve._http_json('POST', base, '/api/chat',
                               {'model': tier_slug,
                                'messages': [{'role': 'system', 'content': sys_msg},
                                             {'role': 'user', 'content': f'Freshly collected sources:\n\n{sources_text}'}],
                                'max_tokens': _serve.RESEARCH_SKILL_SYNTHESIS_TOKENS,
                                'agentId': agent_id,
                                'taskId': task.get('id')}, key)
                if isinstance(r, dict) and not r.get('error') and r.get('reply'):
                    reply = r['reply'].strip()
            except Exception:
                reply = None
            if reply:
                _serve._http_json('POST', base, '/api/library/file',
                           {'agentId': agent_id,
                            'path': artifact,
                            'content': reply, 'source': 'external'}, key)
                note = (f'Ran scheduled research for "{topic.get("topic")}" -- collected '
                        f'{len(kept)} new page(s) and wrote an updated skill file (pending review).')
                # Coverage entries land PENDING_REVIEW (reviewed=False): visible,
                # but they do NOT suppress a re-report until the Verifier clears
                # them. Only a reviewed brief counts as covered.
                for k in kept:
                    _note_research_coverage(base, key, agent_id, _serve.source_identity(k['url'], k['resolved_url'])[0],
                                            artifact, reviewed=False, source_url=k['url'],
                                            resolved_url=k['resolved_url'],
                                            topic_id=topic.get('id'), run_id=run_id)
            else:
                budget_hit = _chat_error_result(r, f'Collected {len(kept)} new page(s) for "{topic.get("topic")}", but the skill-file synthesis call was paused because this task used its full model-spend budget.')
                if budget_hit:
                    _sim_module._store_content_result(task.get('id'), budget_hit)
                    return
                note = (f'Collected {len(kept)} new page(s) for "{topic.get("topic")}", '
                        'but the skill-file synthesis call didn\'t produce anything usable.')
                for k in kept:
                    _note_research_failure(base, key, agent_id, _serve.source_identity(k['url'], k['resolved_url'])[0],
                                           'skill-file synthesis produced nothing usable',
                                           source_url=k['url'], resolved_url=k['resolved_url'],
                                           topic_id=topic.get('id'))
    else:
        note = (f'Ran scheduled research for "{topic.get("topic")}" -- nothing new since '
                f'last time (checked {len(visited)} page(s)).')

    # Record every URL we actually fetched this run so a future run dedups
    # against it (mirrors runResearchTask pushing crawl.pages into seenUrls;
    # the frontier's hub pages count too).
    for u in visited:
        seen.add(u)
    source_urls = sorted({k.get('resolved_url') or k.get('url') for k in kept
                          if k.get('resolved_url') or k.get('url')} | set(visited))
    _sim_module._store_content_result(task.get('id'),
                                      {'note': note, 'seenUrls': sorted(seen),
                                       'sourceUrls': source_urls,
                                       'passLog': pass_log})


def _skill_slug(name):
    return re.sub(r'[^a-z0-9_-]', '-', (name or '').lower())


def _skill_file_format_guide():
    return ('Write it as a real, distilled reference file, not a raw dump. '
            'Headings roughly like: ## Purpose (when to reach for this, one or two sentences), '
            '## Key facts (the concrete, actionable content), '
            '## Sources (real URLs/files this was actually built from), '
            '## Lessons learned (add to this section over time as the skill gets used for real work).')


# --- Remaining per-room content executors (Phase 3, slice 2 follow-ups) -------
# Each mirrors its browser counterpart (tasks.js/world.js) and stores its
# result via sim._store_content_result; the router dispatches by room. The
# heavy pressoffice/coding/review executors (runCodingTask/runReviewTask) are a
# separate larger slice and are NOT dispatched here -- those rooms keep the
# slice-1 workUntil placeholder for now.
_MEDIA_FEEDS_PATH = 'media/feeds.md'
_MEDIA_DIGEST_TOKENS = 200            # runMediaDigestTask /api/chat max_tokens
_SKILL_REVIEW_MAX_PER_SWEEP = 5       # tasks.js SKILL_REVIEW_MAX_PER_SWEEP
RENDER_FALLBACK_THRESHOLD_CHARS = 300  # world.js RENDER_FALLBACK_THRESHOLD_CHARS

# Hive-mind distillation limits. Bounded reads keep the synthesis call from
# being flooded: at most the newest archives since the last run, each excerpt
# capped, plus the current think tank wiki so the merge is incremental.
_DISTILL_MAX_ARCHIVES = 25
_DISTILL_ARCHIVE_EXCERPT_CHARS = 4000
_DISTILL_WIKI_EXCERPT_CHARS = 6000
_DISTILL_THINK_TANK_PAGE_ID = 'state-of-knowledge'
_DISTILL_CSV_MIN_LINES = 3


def _extract_csv_like_blocks(text):
    """Best-effort detection of real CSV/tabular data inside an archive
    file's FULL body (not the truncated excerpt the synthesis prompt sees --
    this must catch data the model never even saw, not just data it saw and
    paraphrased away). Pure heuristic -- a fenced code block, or a run of
    3+ consecutive comma-bearing lines -- same 'don't reach for a heavier
    tool than the problem needs' reasoning as the Library's own plain
    substring search.

    Deterministic safety net: the distill synthesis prompt
    explicitly tells the model to 'extract what the think tank NOW knows...
    do not just re-print the raw archive files' -- exactly the summarize-
    don't-preserve instruction that already, twice, caused a
    spike's real CSV to get flattened into prose instead of kept verbatim
    during ITS OWN synthesis step. This is the same failure one step
    downstream, when a spike's archived findings get folded into the wiki."""
    blocks = []
    for block in re.findall(r'```(?:csv)?\n(.*?)```', text or '', re.DOTALL):
        lines = [l for l in block.strip().split('\n') if l.strip()]
        if len(lines) >= _DISTILL_CSV_MIN_LINES and all(',' in l for l in lines[:_DISTILL_CSV_MIN_LINES]):
            blocks.append(block.strip())
    # Loose scan for CSV-like data that was never fenced -- a run of 3+
    # consecutive non-empty lines with a CONSISTENT comma count (matching
    # their header's column count), not just "any comma present."
    #
    # Gap flagged: the original check (>=1 comma, no
    # consistency requirement) could false-positive on ordinary prose -- 3
    # consecutive comma-bearing sentences is rare but not impossible. A
    # blanket higher minimum (e.g. >=2 commas) was considered and rejected:
    # it would have broken real, legitimate 2-column CSVs (a simple
    # name,url list has exactly 1 comma per row) -- caught by this file's
    # OWN existing test for exactly that shape. Consistency is the sharper
    # signal either way: real CSV rows share their header's column count;
    # ordinary prose sentences essentially never land on the same comma
    # count 3+ times running, regardless of how many commas each has.
    lines = (text or '').split('\n')
    i = 0
    while i < len(lines):
        commas = lines[i].count(',') if lines[i].strip() else 0
        if commas >= 1:
            j = i + 1
            while j < len(lines) and lines[j].strip() and lines[j].count(',') == commas:
                j += 1
            if j - i >= _DISTILL_CSV_MIN_LINES:
                candidate = '\n'.join(lines[i:j])
                if not any(candidate in b or b in candidate for b in blocks):
                    blocks.append(candidate)
            i = j
        else:
            i += 1
    return blocks


def _parse_feed_urls_after(text):
    return [l for l in (text or '').split('\n') if l.strip() and not l.strip().startswith('#')]


def _run_media_content(snapshot, agent_id, task, base_ctx=None):
    """Port of tasks.js runMediaDigestTask: read media/feeds.md, fetch ONE
    subscribed feed (via /api/browse with the thin-page render fallback),
    summarize via /api/chat, and file the digest into the Library."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    out = _serve._http_json('GET', base, '/api/library/file?path=' + urllib.parse.quote(_MEDIA_FEEDS_PATH) + '&requesterId=' + urllib.parse.quote(agent_id), None, key)
    feeds = _parse_feed_urls_after(out.get('content') if isinstance(out, dict) else None)
    if not feeds:
        note = 'No feeds configured yet -- waiting on media/feeds.md in the Library.'
        # A legitimate no-op, not a failed deliverable: there is nothing to
        # digest until feeds.md exists, so this must not loop as a refused
        # completion.
        _sim_module._store_content_result(task.get('id'), {'note': note, 'noop': True})
        return
    url = random.choice(feeds)
    purpose = 'Fetching a subscribed feed source to digest for the player.'
    # fetchPageSmart port: plain browse, then a render:true retry only if thin.
    data = _serve._http_json('POST', base, '/api/browse', {'url': url, 'agentId': agent_id, 'purpose': purpose}, key)
    if data.get('error') or not data.get('allowed'):
        note = f"Tried to check a subscribed feed, but it wasn't approved: {data.get('reason') or 'no reason given'}"
        _sim_module._store_content_result(task.get('id'), {'note': note})
        return
    text = (data.get('text') or '').strip()
    if len(text) < RENDER_FALLBACK_THRESHOLD_CHARS:
        r = _serve._http_json('POST', base, '/api/browse', {'url': url, 'agentId': agent_id, 'purpose': purpose, 'render': True}, key)
        if (r.get('allowed') and not r.get('error')) and (r.get('text') or '').strip().count(' ') > 0 \
                and len((r.get('text') or '').strip()) > len(text):
            text = (r.get('text') or '').strip()
    if not text:
        note = f'Checked {url}, but the page came back empty.'
        _sim_module._store_content_result(task.get('id'), {'note': note})
        return
    # Summarize.
    py_notes = _agent_name(snapshot, agent_id)
    model_slug = _serve._resolve_model_tier('Summarize a fetched web page in a few honest sentences')
    summary = None
    if model_slug:
        r = _serve._http_json('POST', base, '/api/chat',
                       {'model': model_slug,
                        'messages': [
                            {'role': 'system', 'content': 'Summarize the following page in 2-3 short, honest sentences for someone who has not read it. Only report what is actually in the text -- do not invent detail.'},
                            {'role': 'user', 'content': text[:6000]}],
                        'max_tokens': _MEDIA_DIGEST_TOKENS, 'agentId': agent_id,
                        'taskId': task.get('id')}, key)
        if isinstance(r, dict) and not r.get('error') and r.get('reply'):
            summary = r['reply'].strip()
    if not summary:
        budget_hit = _chat_error_result(r,
                                        f'Fetched {url}, but this task has used its full model-spend budget and the digest call was paused.')
        if budget_hit:
            _sim_module._store_content_result(task.get('id'), budget_hit)
            return
        note = f"Fetched {url}, but couldn't summarize it this time."
        _sim_module._store_content_result(task.get('id'), {'note': note})
        return
    slug = re.sub(r'[^a-z0-9]+', '-', url.replace('https://', '').replace('http://', '')).strip('-')[:40].lower()
    path = f"media/digests/{int(time.time() * 1000)}-{slug}.md"
    content = f"# Digest -- {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n\nSource: {url}\nBy: {py_notes or agent_id}\n\n{summary}\n"
    _serve._http_json('POST', base, '/api/library/file',
               {'agentId': agent_id, 'path': path, 'content': content, 'source': 'firsthand'}, key)
    _serve.log_action(agent_id, 'media_digest_filed', {'url': url, 'path': path}, authorized=True)
    note = f'Filed a digest on {url} for the player.'
    # The digest IS the deliverable -- record its real path so completion's
    # Library-evidence check can see it (it was written but never linked).
    _sim_module._store_content_result(task.get('id'), {'note': note, 'libraryPath': path})


def _run_skill_review_content(snapshot, agent_id, task, base_ctx=None):
    """Port of tasks.js runSkillReviewTask: list pending_review/skills/, and for
    each, a real Jev keep/reject judgment (deterministic keep on a failed Jev
    call), promoting the kept and annotating+rejecting the rejected."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    listing = _serve._http_json('GET', base, '/api/library?requesterId=' + urllib.parse.quote(agent_id), None, key)
    files = listing.get('files') or []
    pending = [f for f in files if f.get('path', '').startswith('pending_review/skills/') or 'pending_review/skills/' in f.get('path', '')][:_SKILL_REVIEW_MAX_PER_SWEEP]
    if not pending:
        note = 'Checked for pending skill files -- nothing waiting right now.'
        _sim_module._store_content_result(task.get('id'), {'note': note})
        return
    kept = rejected = 0
    for f in pending:
        path = f['path']
        content = _serve._http_json('GET', base, '/api/library/file?path=' + urllib.parse.quote(path) + '&requesterId=' + urllib.parse.quote(agent_id), None, key)
        if not isinstance(content, dict) or 'content' not in content:
            continue
        body = content['content']
        verdict = 'keep'
        try:
            decision = _serve._call_openrouter_decision_sync(
                _serve._jev_model(), {'messages': [], 'signals': {}},
                {'choice': {'type': 'choice',
                            'instructions': f'A candidate skill-reference file is waiting for review. Its content: "{body[:1500]}" Is this accurate and genuinely useful as real reference material for future work, or should it be discarded?',
                            'criteria': {'keep': 'Yes -- accurate and specific enough to be worth keeping as real reference material.',
                                         'reject': 'No -- inaccurate, too vague/generic to be useful, or not actually relevant to real work here.'}}})
            verdict, _, _ = _serve._jev_choice(decision)
        except Exception:
            verdict = 'keep'  # deterministic safe fallback on an unreachable judge
        if verdict == 'reject':
            _serve._http_json('POST', base, '/api/library/file',
                       {'agentId': agent_id, 'path': path,
                        'content': body + f"\n\n## Rejected\n\nReviewed and rejected on {datetime.datetime.now(datetime.timezone.utc).isoformat()} -- judged not accurate/relevant enough to keep as trusted reference material.\n",
                        'source': 'firsthand'}, key)
            _serve._http_json('POST', base, '/api/library/reject', {'agentId': agent_id, 'path': path}, key)
            rejected += 1
        else:
            _serve._http_json('POST', base, '/api/library/promote', {'agentId': agent_id, 'path': path}, key)
            kept += 1
    note = f'Reviewed {len(pending)} pending skill file(s): {kept} promoted, {rejected} rejected.'
    _sim_module._store_content_result(task.get('id'), {'note': note})


def _run_distill_content(snapshot, agent_id, task, base_ctx=None):
    """The hive mind's merge step: read the archive findings written since the
    last distillation, LLM-synthesize them into an updated think tank wiki page,
    and write it back as server authority. This is what makes the think tank a
    *body* that learns -- shared storage (archive/), merged knowledge (wiki),
    and read-before-act injection (inject_wiki_context) form the loop.

    Pure-enough for hermetic tests: reads archive/wiki bodies from disk (path
    monkeypatched), one /api/chat synthesis call (mocked), and persists via
    serve._write_wiki_server (mocked)."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    since = task.get('distillSince') or 0

    # 1. Collect recent archive findings (newest first, bounded).
    archive_dir = _serve.LIBRARY_ARCHIVE_DIR
    archives = []
    try:
        if os.path.isdir(archive_dir):
            for fn in os.listdir(archive_dir):
                full = os.path.join(archive_dir, fn)
                if not os.path.isfile(full):
                    continue
                mtime_ms = int(os.path.getmtime(full) * 1000)
                if mtime_ms <= since:
                    continue  # already folded into a previous distillation
                try:
                    with open(full, 'r', errors='replace') as f:
                        archives.append((mtime_ms, fn, f.read()))
                except OSError:
                    continue
    except OSError:
        archives = []
    archives.sort(key=lambda t: -t[0])
    archives = archives[:_DISTILL_MAX_ARCHIVES]

    # 2. Pull the current think tank wiki page so the merge is incremental, not a
    # from-scratch rewrite (what the think tank already "knows" is preserved).
    current_wiki = ''
    think_tank_wiki_path = os.path.join(_serve.LIBRARY_DIR, 'wiki', 'think_tank',
                                        f'{_DISTILL_THINK_TANK_PAGE_ID}.md')
    try:
        with open(think_tank_wiki_path, 'r', errors='replace') as f:
            current_wiki = f.read()
    except OSError:
        current_wiki = ''

    # Nothing new since the last distillation -> don't churn the wiki.
    if not archives:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': 'Distillation ran -- no new archive findings since the last pass, so the think tank knowledge is unchanged.', 'noop': True})
        return

    tier_slug = _serve._resolve_model_tier('Distill recent archived think tank findings into the wiki, merging and de-duplicating what the think tank now knows')
    if not tier_slug:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Found {len(archives)} new finding(s) to distill, but the synthesis call failed (no model tier).',
                                           'noop': True})
        return

    # 3. Synthesis call -- same shape as the research executor's /api/chat.
    sources_text = '\n\n'.join(
        f'### {fn}\n{body[:_DISTILL_ARCHIVE_EXCERPT_CHARS]}' for _t, fn, body in archives)
    sys_msg = (
        'You are the distillation step of a think tank-wide knowledge base. Merge the '
        'findings below -- and the current think tank knowledge, when provided -- into one '
        f'updated page ({_DISTILL_THINK_TANK_PAGE_ID}). Rules:\n'
        '- Remove redundancy and contradiction; where findings disagree, keep the view '
        'with more supporting evidence and mark residual disagreement honestly.\n'
        '- Extract what the think tank NOW knows as a body; do not just re-print the raw '
        'archive files.\n'
        '- Preserve citations to the source files you folded in.\n'
        '- If a current page is given, keep what still holds and fold in what is new.\n'
        'Write concrete, actionable markdown, not filler.' +
        (f'\n\nCURRENT THINK TANK KNOWLEDGE (keep/merge):\n\n{current_wiki[:_DISTILL_WIKI_EXCERPT_CHARS]}'
         if current_wiki else '\n\nNo current think tank page yet -- write one from scratch.'))
    reply = None
    r = None
    try:
        r = _serve._http_json('POST', base, '/api/chat',
                              {'model': tier_slug,
                               'messages': [{'role': 'system', 'content': sys_msg},
                                            {'role': 'user', 'content': f'Recent think tank findings:\n\n{sources_text}'}],
                               'max_tokens': _serve.RESEARCH_SKILL_SYNTHESIS_TOKENS,
                               'agentId': agent_id,
                               'taskId': task.get('id')}, key)
        if isinstance(r, dict) and not r.get('error') and r.get('reply'):
            reply = r['reply'].strip()
    except Exception:
        reply = None
    if not reply:
        budget_hit = _chat_error_result(r, f'Distilled {len(archives)} finding(s), but this task has used its full model-spend budget and the synthesis call was paused.')
        if budget_hit:
            _sim_module._store_content_result(task.get('id'), budget_hit)
            return
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Distilled {len(archives)} finding(s), but the synthesis call returned nothing usable.', 'noop': True})
        return

    # 4. Deterministic safety net: the synthesis prompt above
    # explicitly tells the model to summarize, not re-print, the archives --
    # the same instruction that already flattened a spike's real CSV into
    # prose during ITS OWN synthesis, one step upstream of here. Any real
    # CSV-like block in a folded-in archive's FULL body (not just the
    # truncated excerpt the model saw) that isn't substantially still
    # present in the merged page gets appended back verbatim, cited to its
    # source file, so real structured data is never silently lost to a
    # lossy wiki merge.
    for _t, fn, body in archives:
        for block in _extract_csv_like_blocks(body):
            if len(block) > 40 and block[:80] not in reply:
                reply += (f'\n\n---\n\nRaw source data preserved from {fn} (added automatically -- '
                         f'the merge above may have summarized rather than kept this verbatim):\n\n'
                         f'```\n{block[:4000]}\n```')

    # 5. Persist as server authority (chains the passport; survives autosave).
    record = _serve._write_wiki_server(
        _DISTILL_THINK_TANK_PAGE_ID, 'Think Tank state of knowledge', 'think_tank', reply)
    if record is None:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Distilled {len(archives)} finding(s), but the wiki write failed.', 'noop': True})
        return
    _sim_module._store_content_result(task.get('id'),
                                      {'note': f'Distilled {len(archives)} recent finding(s) into the think tank wiki ({record.get("title")}, v{record.get("version")}).',
                                       'distilled': len(archives), 'wikiPage': record.get('id')})


def _run_observatory_content(snapshot, agent_id, task, base_ctx=None):
    """Observatory's own executor choice branches on which flag the task
    carries, not a fixed 1:1 key like every other room -- pulled out of the
    dispatcher (registry conversion) so it reads and tests the
    same way every other _run_*_content executor does."""
    if task.get('distill'):
        _run_distill_content(snapshot, agent_id, task, base_ctx)
    elif task.get('skillReview'):
        _run_skill_review_content(snapshot, agent_id, task, base_ctx)
    elif task.get('research'):
        _run_research_content(snapshot, agent_id, task, base_ctx)
    elif task.get('projectLabel'):
        # Project research needs the web tools (browse_page, search_web,
        # youtube_transcript, GitHub read). The old project branch was a single
        # model call with no tools, so source-based cards came back as honest
        # refusals ("I cannot open the URL") and still completed as done. Reuse
        # the tested spike executor, which plans, gathers with real tools, and
        # writes a cited report.
        _run_spike_content(snapshot, agent_id, task, base_ctx)
    else:
        # Old cards, and repair cards minted before the lane markers were
        # carried over, can arrive with no flag at all. The title is the last
        # stable signal: without this, a "Distill ..." card fell into the bare
        # Research Center pass, which records only a note -- the completion
        # gate then refused it, and every repair repeated the same mismatch.
        title = str(task.get('title') or '').lower()
        if 'distill' in title:
            task['distill'] = True
            task.setdefault('distillSince', 0)
            _run_distill_content(snapshot, agent_id, task, base_ctx)
        elif 'skill review' in title or 'skill-review' in title:
            task['skillReview'] = True
            _run_skill_review_content(snapshot, agent_id, task, base_ctx)
        elif 'research' in title:
            task['research'] = True
            _run_research_content(snapshot, agent_id, task, base_ctx)
        else:
            _run_research_bare_content(snapshot, agent_id, task, base_ctx)


def _run_supervisor_content(snapshot, agent_id, task, base_ctx=None):
    """A supervisor's monitoring turn: scoped to their own team, read-only
    except for one budget top-up. Uses supervisor_report for live state,
    execute_script only to inspect files/sandbox paths, and finishes with a
    short facilitation report. Never edits code and never touches the DB."""
    import sim as _sim_module
    name = _agent_name(snapshot, agent_id)
    team = next((t for t in (snapshot.get('teams') or [])
                 if t.get('directorId') == agent_id or t.get('id') == task.get('teamId')), None)
    is_admin = bool(_serve._is_admin(snapshot, agent_id))
    if not team and not is_admin:
        _sim_module._store_content_result(task.get('id'), {
            'ok': False,
            'note': 'No team is scoped to this supervisor card; nothing to inspect.'})
        return
    if team:
        team_id = team.get('id')
        brief = _sim_module._supervisor_brief(snapshot, team_id)
    else:
        team_id = None
        parts = []
        for row in (snapshot.get('teams') or []):
            b = _sim_module._supervisor_brief(snapshot, row.get('id'))
            if b:
                parts.append(b['text'])
        brief = {'text': '\n\n'.join(parts)} if parts else None
    if not brief:
        _sim_module._store_content_result(task.get('id'), {
            'ok': False, 'note': 'The scoped supervisor brief could not be built.'})
        return
    avatar_paused = bool(snapshot.get('_avatarPaused'))
    key = _serve.get_or_create_agent_key(agent_id)
    model = _serve._mid_tier_slug() or _serve._low_tier_slug()
    task['workUntil'] = time.time() + 180

    def _hb():
        try:
            task['_contentHeartbeat'] = time.time()
            _sim_module._bump_content_heartbeat(task.get('id'))
        except Exception:
            pass

    _hb()
    sandbox_id = f"supervisor-{task.get('id') or 'adhoc'}"
    sandbox_tool = _make_spike_sandbox_executor(agent_id, key, sandbox_id)
    report_tool = _make_supervisor_report_executor(agent_id)
    grant_tool = _make_supervisor_grant_executor(agent_id)
    live_read_tool = _make_supervisor_live_read_executor(agent_id)
    delegate_tool = _make_supervisor_delegate_executor(agent_id, task.get('id'))
    schedules_tool = _make_supervisor_schedules_executor(agent_id)
    runbook_tool = _make_supervisor_runbook_executor()
    backlog_tool = _make_supervisor_backlog_executor(agent_id)
    db_tool = _make_supervisor_db_executor(agent_id)
    state_tool = _make_supervisor_state_executor(agent_id)
    avatar_tool = _make_generate_agent_avatar_executor(agent_id)
    village_tool = _make_generate_village_map_executor(agent_id)
    prices_tool = _make_service_prices_executor(agent_id)
    review_exec_tool = _make_review_execute_executor(agent_id)

    def _execute_tool_raw(tool_name, args):
        _hb()
        if tool_name == 'supervisor_report':
            return report_tool(tool_name, args)
        if tool_name == 'supervisor_grant_budget':
            return grant_tool(tool_name, args)
        if tool_name == 'live_read':
            return live_read_tool(tool_name, args)
        if tool_name == 'delegate':
            return delegate_tool(tool_name, args)
        if tool_name == 'direct_backlog':
            return backlog_tool(tool_name, args)
        if tool_name == 'db_records':
            return db_tool(tool_name, args)
        if tool_name == 'state_records':
            return state_tool(tool_name, args)
        if tool_name == 'generate_agent_avatar':
            return avatar_tool(tool_name, args)
        if tool_name == 'generate_village_map':
            return village_tool(tool_name, args)
        if tool_name == 'service_prices':
            return prices_tool(tool_name, args)
        if tool_name == 'review_execute':
            return review_exec_tool(tool_name, args)
        if tool_name == 'schedules':
            return schedules_tool(tool_name, args)
        if tool_name == 'runbook':
            return runbook_tool(tool_name, args)
        if tool_name == 'execute_script':
            return sandbox_tool(tool_name, args)
        return f'unknown tool {tool_name}'

    def execute_tool(tool_name, args):
        return _serve.idempotent_tool_call(agent_id, task.get('id'), _execute_tool_raw,
                                           tool_name, args)

    tools = [_SPIKE_SANDBOX_TOOL, _SUPERVISOR_REPORT_TOOL, _SUPERVISOR_GRANT_TOOL,
             _SUPERVISOR_LIVE_READ_TOOL, _SUPERVISOR_DELEGATE_TOOL,
             _SUPERVISOR_SCHEDULES_TOOL, _SUPERVISOR_RUNBOOK_TOOL,
             _GENERATE_AGENT_AVATAR_TOOL, _GENERATE_VILLAGE_MAP_TOOL,
             _serve._SERVICE_PRICES_TOOL, _REVIEW_EXECUTE_TOOL]
    if is_admin:
        tools += [_SUPERVISOR_BACKLOG_TOOL, _SUPERVISOR_DB_TOOL,
                  _SUPERVISOR_STATE_TOOL]
        card_note = (task.get('instructions') or '').strip()
        system = (
            f'You are {name}, chief of staff for the whole think tank. '
            'Your job is cross-team oversight and coordination, not the work itself.\n'
            'Call supervisor_report first. Then use live_read to inspect the LIVE project '
            '(ls/read/grep/team_log, git_status/git_diff/git_log, task_trace, find_artifact) '
            'and execute_script only for sandbox paths the report names. '
            'Do not edit project files or write code.\n'
            'Your tools: supervisor_report (every team), live_read (whole live project), '
            'delegate (queue a scoped check for a worker; name the team), schedules '
            '(list/create/edit/delete any), runbook (standard procedures), '
            'supervisor_grant_budget (any team), direct_backlog (instruct a team to put an '
            'immediate high-priority task in its backlog), generate_agent_avatar (generate '
            'the four directional images for any non-admin agent), generate_village_map '
            '(quote then generate a village background plus collision and door maps; '
            'metered), service_prices (list or quote every external API price and '
            'its remaining budget), review_execute (run a pending escalation\'s own '
            'candidate command in the sandbox for a decision; never edit files), '
            'db_records (read or modify '
            'non-secret SQL tables; every write is audited), state_records (read or '
            'modify live business records in the state blob by path). '
            'When reviewing a pending escalation, you may use review_execute to run '
            'that escalation\'s own candidate command in the isolated sandbox. That is '
            'the only code execution allowed; never edit project files or write new code.\n'
            'Look for anything unusual across the tank: a failure, a card stuck in review, '
            'budget pressure, a missing deliverable, a missed schedule, or work that is not '
            'resolving. If something needs action, instruct the owning team with '
            'direct_backlog (concrete title + reason/acceptance), or delegate a scoped check. '
            'Use db_records (SQL tables) or state_records (live business records by path) only '
            'for a concrete record fix you cannot make through the normal tools; state the '
            'scope, filter/path, and why. If a team is missing avatar images, generate them '
            'with generate_agent_avatar. A research or report card whose note says it could '
            'not fetch its source is a failure, not a done card; direct the owning team to '
            're-card it as a spike. Never do a team\'s work for it.\n'
            'Finish with a short report: what you checked, anything unusual, directives filed, '
            'records changed, and next actions.\n\n'
            'Current cross-team brief (supervisor_report returns a fresh copy):\n' + brief['text']
            + ('\n\nAvatar generation is paused during the expansion track. Do not call generate_agent_avatar.' if avatar_paused else '')
            + (f'\n\nSpecific card instruction:\n{card_note}' if card_note else '')
        )
        user_prompt = 'Sweep the whole think tank now.'
    else:
        card_note = (task.get('instructions') or '').strip()
        system = (
            f'You are {name}, team "{team.get("name") or team_id}". '
            'Your job is facilitation, not the work. Your scope is this team only.\n'
            'Call supervisor_report first. Then use live_read to inspect the LIVE project '
            '(ls/read/grep/team_log) and execute_script only for the sandbox paths the report names. '
            'Do not edit project files.\n'
            'Your tools: supervisor_report (scoped state), live_read (read-only live project and team '
            'log), delegate (queue a scoped check for a worker with a return contract), schedules '
            '(list/create/edit/delete), runbook (standard procedures), supervisor_grant_budget, '
            'generate_agent_avatar (generate the four directional images for a team member or '
            'yourself; checks PixelLab availability and budget), generate_village_map '
            '(quote then generate a village background plus exact collision and door '
            'maps; metered, so check the quote against your budget first), '
            'service_prices (quote any registered external API before delegating a call), '
            'review_execute (run a pending escalation\'s own candidate command in the '
            'sandbox for a decision; never edit files or write code). '
            'Outside an escalation review, execute_script is for inspecting what the report '
            'names only; do not write or edit project code.\n'
            'Act on four things:\n'
            '1. Budget. If a card spent its ceiling before close, say what the spend bought and either '
            'grant a top-up with supervisor_grant_budget (with a clear reason) or recommend stopping.\n'
            '2. Process. If a card failed, is stuck in review, or its deliverable is missing, say so '
            'plainly, delegate the check when it needs evidence, and name the next action.\n'
            '3. Research gaps. If a research or report card is done but its note says the source was '
            'not fetched or the finding is empty, treat it as unfinished and delegate a spike to do '
            'the real source read.\n'
            '4. Avatars. If the brief lists workers missing avatar images and PixelLab is available, '
            'call generate_agent_avatar for each one before you finish.\n'
            'Never inspect or comment on another team, never touch the database, never write code. '
            'Finish with a short report: cards reviewed, budget decisions, process issues, delegations, next actions.\n\n'
            'Current scoped brief (supervisor_report returns a fresh copy):\n' + brief['text']
            + ('\n\nAvatar generation is paused during the expansion track. Do not call generate_agent_avatar.' if avatar_paused else '')
            + (f'\n\nSpecific card instruction:\n{card_note}' if card_note else '')
        )
        user_prompt = 'Check your team now.'
    messages = [{'role': 'system', 'content': system},
                {'role': 'user', 'content': user_prompt}]
    try:
        text, _transcript = _serve._call_agent_tool_loop(
            model, messages, tools,
            execute_tool, max_iterations=10, max_tokens=1600,
            service='supervisor', force_first_tool='supervisor_report',
            return_transcript=True, task_id=task.get('id'),
            conversation_policy=task_tool_policy({
                **task,
                'approvedTools': ['delegate', 'schedules', 'supervisor_grant_budget',
                                  'direct_backlog', 'generate_agent_avatar', 'generate_village_map',
                                  'service_prices', 'review_execute', 'execute_script'],
            }))
    except Exception as error:
        _sim_module._store_content_result(task.get('id'), {
            'ok': False, 'note': f'Supervisor check could not run: {error}'})
        return
    note = (text or '').strip() or 'Supervisor check completed with no findings.'
    _sim_module._store_content_result(task.get('id'), {
        'ok': True, 'note': note, 'supervisorCheck': True, 'teamId': team_id})
