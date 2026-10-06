"""Server-owned content executors: the per-room real-work runners.

Extracted from serve.py (phase-consolidation): this module holds every `_run_*`
content executor (research/weather/media/skill-review/research-project/research-
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
import json
import os
import random
import re
import time
import urllib.parse

import serve as _serve


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
        changed = bool(lastmod) and lastmod > since
        already = url in seen
        if already and not changed:
            pass  # dedup: already collected and unchanged since -- skip save
        else:
            should_keep = (not page_kw) or (page_kw in text.lower())
            if should_keep:
                idx = len(kept) + 1
                saved = _serve._http_json('POST', base, '/api/sandbox-save-page',
                                   {'agentId': agent_id, 'sandboxId': sandbox_id,
                                    'url': url, 'purpose': purpose,
                                    'filename': f'crawl-{idx}.txt', 'content': text,
                                    'inRoom': in_room}, key)
                if saved.get('allowed') and saved.get('ok'):
                    kept.append({'url': url, 'text': text})
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

    # Synthesize the updated skill file from freshly collected sources.
    note = None
    if kept:
        existing = _serve._http_json('GET', base, '/api/library/file?path=' +
                              urllib.parse.quote(f"skills/{_skill_slug(topic.get('topic'))}.md"))
        existing_content = existing.get('content') if isinstance(existing, dict) and 'content' in existing else None
        tier_slug = _serve._resolve_model_tier(f'Synthesize an updated skill-reference file for research topic: {topic.get("topic")}')
        if not tier_slug:
            note = f'Collected {len(kept)} new page(s) for "{topic.get("topic")}", but synthesis failed (no model tier).'
        else:
            sources_text = '\n\n'.join(
                f'### {p["url"]}\n{(p.get("text") or "")[:3000]}' for p in kept)
            sys_msg = (f'You are updating a real skill-reference file for "{topic.get("topic")}". '
                       f'{_skill_file_format_guide()} '
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
                            'path': f"skills/{_skill_slug(topic.get('topic'))}.md",
                            'content': reply, 'source': 'external'}, key)
                note = (f'Ran scheduled research for "{topic.get("topic")}" -- collected '
                        f'{len(kept)} new page(s) and wrote an updated skill file (pending review).')
            else:
                budget_hit = _chat_error_result(r, f'Collected {len(kept)} new page(s) for "{topic.get("topic")}", but the skill-file synthesis call was paused because this task used its full model-spend budget.')
                if budget_hit:
                    _sim_module._store_content_result(task.get('id'), budget_hit)
                    return
                note = (f'Collected {len(kept)} new page(s) for "{topic.get("topic")}", '
                        'but the skill-file synthesis call didn\'t produce anything usable.')
    else:
        note = (f'Ran scheduled research for "{topic.get("topic")}" -- nothing new since '
                f'last time (checked {len(visited)} page(s)).')

    # Record every URL we actually fetched this run so a future run dedups
    # against it (mirrors runResearchTask pushing crawl.pages into seenUrls;
    # the frontier's hub pages count too).
    for u in visited:
        seen.add(u)
    _sim_module._store_content_result(task.get('id'),
                                      {'note': note, 'seenUrls': sorted(seen)})


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


def _run_weather_content(snapshot, agent_id, task, base_ctx=None):
    """The Weather Station logs LIVE readings for the think tank's configured
    location (serve.WEATHER_LOCATION, default Charlotte, NC -- overridable via
    WEATHER_LOCATION env). Real Open-Meteo data through serve's own fetcher,
    never a hard-coded forecast or a fixed reference page."""
    import sim as _sim_module
    reading = _serve._weather_fetch(_serve.WEATHER_LOCATION)
    if reading.startswith('__TOOL_ERROR__'):
        note = f'Could not log live weather readings: {reading}'
    else:
        note = f'Logged live weather for {_serve.WEATHER_LOCATION}: {reading}'
    _sim_module._store_content_result(task.get('id'), {'note': note})


def _parse_feed_urls_after(text):
    return [l for l in (text or '').split('\n') if l.strip() and not l.strip().startswith('#')]


def _run_media_content(snapshot, agent_id, task, base_ctx=None):
    """Port of tasks.js runMediaDigestTask: read media/feeds.md, fetch ONE
    subscribed feed (via /api/browse with the thin-page render fallback),
    summarize via /api/chat, and file the digest into the Library."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    out = _serve._http_json('GET', base, '/api/library/file?path=' + urllib.parse.quote(_MEDIA_FEEDS_PATH))
    feeds = _parse_feed_urls_after(out.get('content') if isinstance(out, dict) else None)
    if not feeds:
        note = 'No feeds configured yet -- waiting on media/feeds.md in the Library.'
        _sim_module._store_content_result(task.get('id'), {'note': note})
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
    _sim_module._store_content_result(task.get('id'), {'note': note})


def _run_skill_review_content(snapshot, agent_id, task, base_ctx=None):
    """Port of tasks.js runSkillReviewTask: list pending_review/skills/, and for
    each, a real Jev keep/reject judgment (deterministic keep on a failed Jev
    call), promoting the kept and annotating+rejecting the rejected."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    listing = _serve._http_json('GET', base, '/api/library')
    files = listing.get('files') or []
    pending = [f for f in files if f.get('path', '').startswith('pending_review/skills/')][:_SKILL_REVIEW_MAX_PER_SWEEP]
    if not pending:
        note = 'Checked for pending skill files -- nothing waiting right now.'
        _sim_module._store_content_result(task.get('id'), {'note': note})
        return
    kept = rejected = 0
    for f in pending:
        path = f['path']
        content = _serve._http_json('GET', base, '/api/library/file?path=' + urllib.parse.quote(path))
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
        _run_research_project_content(snapshot, agent_id, task, base_ctx)
    else:
        _run_research_bare_content(snapshot, agent_id, task, base_ctx)


def _server_content_dispatcher(snapshot, agent_id, task, base_ctx=None):
    """The router registered as sim._content_executor. Decides by task.room +
    task flags which real content executor runs, mirroring arriveAtTask's
    dispatch + runResearchTask's branch split. Rooms with no ported executor
    (bank / library / postoffice / unset) are NOT dispatched -- those fall back
    to the slice-1 workUntil placeholder.

    Registry conversion: was a single if/elif chain: taskType
    checked before room (a review/spike overrides whatever room it's queued
    into -- see each branch's own reasoning below), room itself a flat match
    except observatory's flag-based sub-dispatch (now _run_observatory_content,
    same shape as every other room executor). Built HERE, not as module-level
    dict literals, so this function can stay where it naturally reads first in
    the file without caring that most _run_*_content executors are defined
    further down -- by the time this runs (per-task, at simulation runtime)
    the whole module has already finished loading, so the forward references
    are always valid."""
    task_type_executors = {
        # A spike is room-agnostic: an investigation, not the room's craft.
        'spike': _run_spike_content,
        # A peer-gate review/QA pass inherits its PARENT story's room (the
        # deliverable is where it was built -- e.g. an observatory research
        # bulletin). Checked before room so a review in ANY room emits the
        # peerVerdict the parent gate needs -- the room-specific executors
        # only produce research/craft notes, never a verdict; letting a
        # review fall through to them silently drops the vote and wedges the
        # story open in 'needs_review' forever (the infinite fork).
        'review': _run_review_content,
        'qa': _run_review_content,
    }
    room_executors = {
        'observatory': _run_observatory_content,
        'weatherstation': _run_weather_content,
        'media': _run_media_content,
        'pressoffice': _run_workroom_content,
        'bank': _run_bank_content,
        # library / postoffice / unset -> no entry -> placeholder (unchanged).
    }
    executor = task_type_executors.get(task.get('taskType')) or room_executors.get(task.get('room'))
    if task.get('pipelineStep'):
        executor = _run_pipeline_step_content
    if executor:
        executor(snapshot, agent_id, task, base_ctx)


def _run_pipeline_step_content(snapshot, agent_id, task, base_ctx=None):
    """The executor for an ORDERED-pipeline step (sim.add_pipeline /
    _check_pipelines): a scheduled, single-shot step with an optional tool +
    args. Resolves the step's definition from the snapshot's pipelines (the
    pipelineStep marker on the task carries pipelineId + stepIndex), then:
      - with a `tool`, runs the real upstream tool (Treg LinkedIn search /
        x_trending_topics today) and stores the raw result as the note;
      - without a tool, falls back to the generic research-center pass so the
        step still lands a real content result (the pipeline's strict-order
        gate advances on the task's 'done' status, so a step MUST complete to
        release its successor -- never a placeholder no-op that looks done).
    Executed on a background thread by sim._dispatch_content_work; stores its
    result via sim._store_content_result for the next task_cycle pass."""
    import sim as _sim_module
    marker = task.get('pipelineStep') or {}
    pipeline_id = marker.get('pipelineId')
    step_index = marker.get('stepIndex')
    step = None
    for p in (snapshot.get('pipelines') or []):
        if p.get('id') == pipeline_id:
            steps = p.get('steps') or []
            if 0 <= (step_index or 0) < len(steps):
                step = steps[step_index]
            break
    if step is None:
        # Pipeline vanished since scheduling: report, don't wedge the agent.
        _sim_module._store_content_result(task.get('id'),
                                          {'note': 'Pipeline step arrived with no matching pipeline record.'})
        return
    tool = (step.get('tool') or '').strip()
    args = dict(step.get('args') or {})
    if tool:
        try:
            if tool in ('x_trending_topics', 'search_linkedin_posts'):
                executor = _make_treg_tools_executor()
            elif tool in _APIFY_TOOL_NAMES:
                executor = _make_apify_tools_executor()
            elif tool == 'generate_pixel_character':
                executor = _make_pixellab_tools_executor()
            else:
                executor = None
            if executor is None:
                out = f'unknown tool for pipeline step: {tool}'
            else:
                out = executor(tool, args)
            _sim_module._store_content_result(task.get('id'), {'note': out})
            return
        except Exception as e:
            _sim_module._store_content_result(task.get('id'),
                                              {'note': f'Pipeline step tool failed: {e}'})
            return
    _run_research_bare_content(snapshot, agent_id, task, base_ctx)


def _agent_name(snapshot, agent_id):
    roster = snapshot.get('agentRoster') or []
    for d in roster:
        if d.get('id') == agent_id:
            return d.get('name') or agent_id
    a = (snapshot.get('agents') or {}).get(agent_id)
    return (a or {}).get('name') or agent_id


def _run_bank_content(snapshot, agent_id, task, base_ctx=None):
    """The Bank teller. A director (or the admin) gets the full readable
    readout: per-service used/cap/left, a burn-rate forecast, and reallocation
    authority. A non-director gets a real, READ-ONLY cumulative summary too --
    The think tank should have hive-mind awareness of
    what its own actions cost, not just directors. Only the AUTHORITY to
    reallocate/raise caps stays director-only; visibility into "are we
    healthy" does not. Reports via the standard content-result note so the
    task/board can surface it."""
    import sim as _sim_module
    name = _agent_name(snapshot, agent_id)
    is_director = _serve._is_director(snapshot, agent_id)
    view = _serve._bank_budget_view(snapshot)
    if not is_director:
        if not view:
            note = (f"{name} checked the bank: nothing has been spent yet, so every "
                     f"service is at $0.00 of its cap.")
        else:
            total_used = sum(row['used'] for row in view.values())
            total_cap = sum(row['cap'] for row in view.values())
            over_any = any(row['over'] for row in view.values())
            pct = (100.0 * total_used / total_cap) if total_cap else 0.0
            note = (f"{name} checked the bank: the think tank has used ${total_used:.2f} of "
                     f"${total_cap:.2f} cumulative budget ({pct:.0f}%) across {len(view)} "
                     f"services. " + ("At least one service is OVER its cap -- work in that "
                     "area should slow down; only a director can reallocate or raise it."
                     if over_any else "Everything is within cap right now."))
        _sim_module._store_content_result(task.get('id'), {'note': note})
        return
    if not view:
        note = (f"{name} (director) checked the bank: nothing has been spent yet, so "
                f"every service is at $0.00 of its cap. No forecast until there's a burn signal.")
        _sim_module._store_content_result(task.get('id'), {'note': note})
        return
    lines = []
    total_used = total_cap = 0.0
    for svc, row in sorted(view.items()):
        total_used += row['used']
        total_cap += row['cap']
        status = 'OVER' if row['over'] else 'ok'
        left_s = 'no cap' if row['left'] is None else f"{row['left']:.2f} left"
        forecast = f", {row['daysLeft']:.1f} days until cap at current burn" if row['daysLeft'] is not None else ""
        lines.append(f"- {svc}: ${row['used']:.2f} / ${row['cap']:.2f} cap ({left_s}, {status}, {row['calls']} calls, {row['burnPerDay']:.2f}/day{forecast})")
    body = '; '.join(lines)
    over_any = any(v['over'] for v in view.values())
    note = (f"{name} (director) reviewed the bank. Cumulative: ${total_used:.2f} used of ${total_cap:.2f} "
            f"across {len(view)} services. Per service: {body}. "
            + ("WARNING: at least one service is over its cap - directors should re-allocate or raise a cap."
               if over_any else
               "All services are within cap; directors can coordinate to keep it that way."))
    # Reconcile against the REAL OpenRouter account
    # balance too, not just the think tank's own internal ledger -- the two can
    # legitimately diverge (outside usage on the same key, manual top-ups).
    credits = _serve._openrouter_account_credits()
    if credits:
        note += (f" OpenRouter account (real): ${credits['totalCredits']:.2f} total credits, "
                 f"${credits['totalUsage']:.2f} used lifetime, ${credits['remaining']:.2f} remaining.")
    # Apify FREE-plan reconcile: same live-account pattern -- show
    # the REAL cycle spend/cap the plan is enforcing, not just the think tank's
    # own ledger, so the teller sees the $5/month wall it's actually against.
    apify_usage = _serve._apify_account_usage()
    if apify_usage:
        note += (f" Apify account (real): ${apify_usage['usedUsd']:.4f} of "
                 f"${apify_usage['capUsd']:.2f} monthly cap spent "
                 f"(${apify_usage['remainingUsd']:.4f} remaining).")
    _sim_module._store_content_result(task.get('id'), {'note': note})


def _run_research_project_content(snapshot, agent_id, task, base_ctx=None):
    """Port of runResearchTask's projectLabel branch: a real model research
    finding, logged to the Research Center sandbox's findings.log + written to
    the Library (never shell-interpolated raw)."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    project = task.get('projectLabel') or ''
    backlog = f"{task.get('title')} -- {task.get('instructions')}" if task.get('instructions') else (task.get('title') or '')
    tier_slug = _serve._resolve_model_tier(f'Write research findings for a project: {project or backlog[:120]}')
    finding = None
    r = None
    if tier_slug:
        r = _serve._http_json('POST', base, '/api/chat',
                       {'model': tier_slug,
                        'messages': [
                            {'role': 'system', 'content': f'You are researching for a real project: {project}. Your specific task right now: {backlog}. Write 2-4 honest, concrete sentences of real findings or analysis -- no filler, an actual answer or set of concrete points.'},
                            {'role': 'user', 'content': 'Go ahead.'}],
                        'max_tokens': 400, 'agentId': agent_id,
                        'service': project or task.get('productId') or '__general__',
                        'taskId': task.get('id')}, key)
        if isinstance(r, dict) and not r.get('error') and r.get('reply'):
            finding = r['reply'].strip()
    if finding:
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _serve._http_json('POST', base, '/api/pipeline',
                   {'agentId': agent_id, 'sandboxId': _serve._SANDBOX_RESEARCH_ID,
                    'steps': [{'name': 'log this finding', 'command': f"cat >> findings.log << 'FINDING_EOF'\n{ts}: {task.get('title')}\n{finding}\nFINDING_EOF"}]}, key)
        _serve._http_json('POST', base, '/api/library/file',
                   {'agentId': agent_id, 'path': f"archive/{int(time.time() * 1000)}-research-{task.get('id') or 'adhoc'}.md",
                    'content': f"# {task.get('title')}\n\nProject: {project}\nBy: {_agent_name(snapshot, agent_id)}\n\n{finding}\n",
                    'source': 'firsthand'}, key)
        note = f'Researched "{task.get("title")}" for real and logged the finding.'
    else:
        budget_hit = _chat_error_result(r, f'Tried to research "{task.get("title")}", but this task has used its full model-spend budget and the research call was paused.')
        if budget_hit:
            _sim_module._store_content_result(task.get('id'), budget_hit)
            return
        note = f'Tried to research "{task.get("title")}", but the model call didn\'t produce anything usable.'
    _sim_module._store_content_result(task.get('id'), {'note': note})


def _run_research_bare_content(snapshot, agent_id, task, base_ctx=None):
    """Port of runResearchTask's bare branch: a fixed log-and-tally of the
    Research Center's findings.log."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    r = _serve._http_json('POST', base, '/api/pipeline',
                   {'agentId': agent_id, 'sandboxId': _serve._SANDBOX_RESEARCH_ID,
                    'steps': [
                        {'name': 'log this pass', 'command': "echo \"$(date): reviewed findings\" >> findings.log"},
                        {'name': 'tally findings so far', 'command': 'wc -l findings.log'}]}, key)
    note = f"Worked in the Research Center sandbox -- hit a failure at \"{r.get('failedStep')}\"." \
        if r.get('failedStep') else 'Reviewed and logged this week\'s findings in the Research Center.'
    _sim_module._store_content_result(task.get('id'), {'note': note})


# --- Press Office content executors (Phase 3): runWorkroomTask/coding/review --
# Ports of tasks.js runWorkroomTask + runCodingTask + runReviewTask and their
# helpers, into the (snapshot, agent_id, task, base_ctx) executor shape. The
# tasks.js versions were written specifically for the think tank's own shared
# sandbox (WORKROOM_SANDBOX_ID), so these are a faithful pipeline: real
# /api/execute calls (probe-before-writing, heredoc-continuation, orphaned and
# phantom script detection) and /api/chat for the code itself.
WORKROOM_SANDBOX_ID = 'workroom-shared'      # index.html WORKROOM_SANDBOX_ID
CODE_CONTINUATION_ATTEMPTS = 2               # tasks.js
MAX_CODE_PROBE_ROUNDS = 3                    # tasks.js
MAX_CODE_COLAB_ROUNDS = 3                    # code-lane Colab compute rounds (mirrors probe rounds)
MAX_REVIEW_PROBE_ROUNDS = 2                  # tasks.js
_CODE_MAX_TOKENS = 3500                      # runCodingTask /api/chat
_REVIEW_MAX_TOKENS = 900                     # runReviewTask /api/chat
_VISION_MAX_TOKENS = 500                     # reviewScreenshot /api/chat


def _heredoc_balance(command):
    """Port of tasks.js _heredocBalance: count heredoc-open delimiters and the
    bare 'EOF' close lines; balanced means either no opens or equal opens/closes."""
    opens = len(re.findall(r"<<-?\s*'?EOF'?", command or ''))
    closes = len([ln for ln in (command or '').split('\n') if ln.strip() == 'EOF'])
    return opens, closes, bool(opens == 0 or opens == closes)


def _parse_probe_request(reply):
    """Port of tasks.js _parseProbeRequest: a probe request is a JSON object
    whose ONLY recognized key is probeRequest {path, actions, probes}; anything
    else (an ordinary shell command) returns None and falls through to the
    normal command path. Deliberately narrow so a real command is never
    mistaken for a probe request."""
    if not reply:
        return None
    import json as _json
    try:
        parsed = _json.loads(reply.strip().replace('```json', '').replace('```', '').strip())
    except Exception:
        return None
    req = parsed.get('probeRequest') if isinstance(parsed, dict) else None
    if not isinstance(req, dict):
        return None
    if not isinstance(req.get('actions'), list) and not isinstance(req.get('probes'), list):
        return None
    return {'path': req.get('path') or 'index.html',
            'actions': req.get('actions') or [],
            'probes': req.get('probes') or []}


def _parse_colab_run_request(reply):
    """Mirror of _parse_probe_request for the code lane: a Colab run request
    is a JSON object whose ONLY recognized key is colabRun {code, purpose,
    packages, timeout_seconds, runtimes, runtime}; anything else (an ordinary
    shell command or a probeRequest) returns None and falls through. Narrow on
    purpose so a real command is never mistaken for a Colab request."""
    if not reply:
        return None
    import json as _json
    try:
        parsed = _json.loads(reply.strip().replace('```json', '').replace('```', '').strip())
    except Exception:
        return None
    req = parsed.get('colabRun') if isinstance(parsed, dict) else None
    if not isinstance(req, dict):
        return None
    code = (req.get('code') or '').strip()
    if not code:
        return None
    try:
        timeout = int(req.get('timeout_seconds') or 300)
    except (TypeError, ValueError):
        timeout = 300
    try:
        runtimes = int(req.get('runtimes') or 1)
    except (TypeError, ValueError):
        runtimes = 1
    kind = (req.get('runtime') or 'cpu').strip()
    if kind not in ('gpu', 'cpu'):
        kind = 'cpu'
    return {'code': code,
            'purpose': (req.get('purpose') or 'story computation').strip(),
            'packages': [p for p in (req.get('packages') or [])
                         if isinstance(p, str) and p.strip()][:12],
            'timeout_seconds': timeout,
            'runtimes': runtimes,
            'runtime': kind}


def _format_colab_run_result(result):
    """Format a _serve._colab_compute_run result the way the spike toolchain's
    run_on_colab executor does, so the story agent sees the same exit/units/
    stdout shape (including sharding over multiple runtimes) and an honest
    note when the run was refused or printed nothing."""
    if not isinstance(result, dict):
        return '(Colab run returned no usable response)'
    if result.get('error'):
        return f'(Colab run refused: {result["error"]})'
    parts = [f'Colab run OK (session: {result.get("session") or "colab"}, '
             f'{result.get("elapsed_s", 0)}s, {result.get("units", 0)} compute units):']
    if result.get('runtimes'):
        parts.insert(0, f'Colab sharded run OK across {result["runtimes"]} runtime(s) '
                        f'({result.get("elapsed_s", 0)}s, {result.get("units", 0)} compute units total):')
    stdout = (result.get('stdout') or '').strip()
    if stdout:
        parts.append(f'output:\n{stdout}')
    if not stdout:
        parts.append('(no output -- your code printed nothing)')
    return '\n\n'.join(parts)


def _format_page_probe_result(data):
    """Port of world.js formatPageProbeResult: render a /api/page-probe response
    as the same real fact-sheet the browser's agentic probe loops produce --
    real actions, console/errors, actual custom globals with real shape, actual
    probe results. Honest about not guessing when the probe couldn't run."""
    if not isinstance(data, dict) or data.get('error'):
        return f"(probe could not run: {data.get('error') if isinstance(data, dict) else 'no response'})"
    lines = [
        '### Real runtime probe (headless browser -- actual click/keypress, not a guess)',
        "Actions: " + '; '.join(data.get('actionLog') or []),
        "Console: " + (' | '.join(data.get('console') or []) if data.get('console') else '(none)'),
        "Page errors: " + (' | '.join(data.get('pageErrors') or []) if data.get('pageErrors') else '(none)'),
        'Custom globals this page actually defines (window.*), with real shape:',
    ]
    globals_ = data.get('customGlobals') or []
    if not globals_:
        lines.append('  (none found)')
    for g in globals_:
        if not isinstance(g, dict):
            continue
        name, gtype = g.get('name'), g.get('type')
        if gtype == 'object' and isinstance(g.get('keys'), list):
            keys = g['keys']
            lines.append(f'  window.{name} (object) -- real keys: {", ".join(keys) if keys else "(none)"}')
        elif gtype == 'array':
            lines.append(f'  window.{name} (array, length {g.get("length")})')
        elif 'value' in g:
            lines.append(f'  window.{name} ({gtype}) = {json.dumps(g.get("value"))}')
        else:
            lines.append(f'  window.{name} ({gtype})')
    lines.append('Real results, evaluated against the page AFTER those actions:')
    results = data.get('results') or {}
    for expr, val in results.items():
        try:
            encoded = json.dumps(val)
        except (TypeError, ValueError):
            encoded = json.dumps(str(val))
        lines.append(f'  {expr} => {encoded}')
    return '\n'.join(lines)


def _get_sandbox_context(base, key, agent_id, sandbox_id):
    """Port of tasks.js getSandboxContext: read every real *.html *.js *.css
    *.py *.md in the sandbox, skipping whole oversized files once an accumulate
    budget is spent (never truncate mid-file), so serve.py's own _serve.SANDBOX_MAX_OUTPUT
    cap is never hit."""
    command = ("budget=15000; total=0; for f in *.html *.js *.css *.py *.md; do [ -f \"$f\" ] || continue; "
               "sz=$(wc -c < \"$f\"); if [ $((total + sz)) -gt $budget ]; then echo \"--- $f --- (skipped, over context budget)\"; continue; fi; "
               "echo \"--- $f ---\"; cat \"$f\"; total=$((total + sz)); done")
    data = _serve._http_json('POST', base, '/api/execute',
                      {'agentId': agent_id, 'command': command,
                       'purpose': 'Reading current sandbox files for context before the next step.',
                       'sandboxId': sandbox_id}, key)
    if not isinstance(data, dict) or not data.get('allowed') or not data.get('stdout'):
        return '(nothing written yet)'
    return data['stdout']


def _search_library_files(base, key, agent_id, query):
    """Port of world.js searchLibraryFiles: GET /api/library/search?q=. Returns
    [] on any failure -- a failed search shouldn't block the task it feeds.
    No X-Agent-Key header (client searchLibraryFiles sends none, and GET reads
    are not key-gated)."""
    import urllib.parse as _up
    data = _serve._http_json('GET', base, '/api/library/search?q=' + _up.quote(query or ''))
    if isinstance(data, dict) and data.get('matches'):
        return data['matches']
    return []


def _gather_unified_context(snapshot, base, key, agent_id, sandbox_id, topic):
    """Port of world.js gatherUnifiedContext: sandbox files + Library search +
    the agent's recent mailbox, assembled into one context blob. The mailbox
    block reads the snapshot's real mailbox when available. Deliberately skips
    the markMailRead side effect -- that only clears a live-browser HUD counter;
    the server executor has no such counter to clear."""
    sandbox = _get_sandbox_context(base, key, agent_id, sandbox_id)
    library_matches = _search_library_files(base, key, agent_id, topic) if topic else []
    full_agent = (snapshot.get('agents') or {}).get(agent_id)
    recent_mail = (full_agent or {}).get('mailbox') or [] if isinstance((full_agent or {}).get('mailbox'), list) else []
    recent_mail = recent_mail[-5:]

    if library_matches:
        head = ['- {p}: {s}'.format(p=m.get('path', ''), s=m.get('snippet') or '(filename match, open it directly for content)')
                for m in library_matches[:5]]
        library_block = '\n'.join(head)
    else:
        library_block = '(no relevant Library files found for this)'
    if recent_mail:
        mail_lines = []
        for m in recent_mail:
            text = m.get('text') if isinstance(m, dict) else m
            mail_lines.append(text if (isinstance(m, dict) and m.get('read')) else f'[NEW] {text}')
        mail_block = '\n'.join(mail_lines)
    else:
        mail_block = '(nothing in your mailbox)'

    return (f'## Current sandbox files\n{sandbox}\n\n'
            f'## Relevant Library knowledge (searched: "{topic or "none given"}")\n{library_block}\n\n'
            f'## Your mailbox (most recent)\n{mail_block}')


def _review_screenshot(base, key, agent_id, sandbox_id, path, question, task_id=None):
    """Port of world.js reviewScreenshot: real headless screenshot of a
    sandboxed file handed to the MMMU-scored vision tier. Returns {ok, review}
    or {ok: False, note: ...} -- degrades to a note, never a crash, if the
    screenshot or vision call fails."""
    shot = _serve._http_json('POST', base, '/api/screenshot',
                      {'agentId': agent_id, 'sandboxId': sandbox_id, 'path': path}, key)
    if not isinstance(shot, dict) or shot.get('error'):
        return {'ok': False, 'note': f"screenshot failed: {shot.get('error') if isinstance(shot, dict) else 'no response'}"}
    vision_slug = _serve._vision_tier_slug()
    if not vision_slug:
        return {'ok': False, 'note': 'vision model tier not available'}
    r = _serve._http_json('POST', base, '/api/chat',
                   {'model': vision_slug,
                    'messages': [{'role': 'user',
                                  'content': [{'type': 'text', 'text': question},
                                              {'type': 'image_url',
                                               'image_url': {'url': 'data:image/png;base64,' + (shot.get('imageBase64') or '')}}]}],
                    'max_tokens': _VISION_MAX_TOKENS, 'agentId': agent_id,
                    'taskId': task_id}, key)
    if isinstance(r, dict) and not r.get('error') and r.get('reply'):
        return {'ok': True, 'review': r['reply'].strip()}
    budget_hit = _chat_error_result(r, 'the vision call was paused because this task used its full model-spend budget')
    if budget_hit:
        return {'ok': False, 'budgetExhausted': True, 'taskSpendUsd': budget_hit.get('taskSpendUsd'),
                'taskSpendAttempts': budget_hit.get('taskSpendAttempts'), 'note': budget_hit['note']}
    return {'ok': False, 'note': 'vision call failed or returned nothing'}


def _extract_written_js_files(command):
    """Port of tasks.js _extractWrittenJsFiles: every .js filename a heredoc
    command wrote via `cat > name.js` or `cat >> name.js`."""
    files = set()
    for m in re.finditer(r"cat\s*>>?\s*([A-Za-z0-9_.\-]+\.js)\b", command or ''):
        files.add(m.group(1))
    return sorted(files)


def _find_unlinked_js_files(base, key, agent_id, sandbox_id, js_files):
    """Port of tasks.js _findUnlinkedJsFiles: mechanical grep of every *.html for
    src="<file>" -- a file never referenced by any real page is "unlinked".
    On a read failure we can't confirm one way or the other, so treat the file
    as unlinked so the follow-up at least attempts a fix."""
    if not js_files:
        return []
    check_command = ' ; '.join(
        f"grep -q 'src=\"{f}\"' *.html 2>/dev/null && echo \"LINKED:{f}\" || echo \"UNLINKED:{f}\"" for f in js_files)
    data = _serve._http_json('POST', base, '/api/execute',
                      {'agentId': agent_id, 'command': check_command,
                       'purpose': 'Verifying newly written script files are actually linked into index.html.',
                       'sandboxId': sandbox_id}, key)
    if not isinstance(data, dict):
        return js_files
    stdout = data.get('stdout') or ''
    return [f for f in js_files if f'UNLINKED:{f}' in stdout]


def _guess_link_target_html(base, key, agent_id, sandbox_id, js_file):
    """Port of tasks.js _guessLinkTargetHtml: prefer a same-named HTML page
    (settings.js -> settings.html) when one exists; index.html is the fallback."""
    base_name = js_file[:-3] if js_file.endswith('.js') else js_file
    if base_name == 'index':
        return 'index.html'
    data = _serve._http_json('POST', base, '/api/execute',
                      {'agentId': agent_id,
                       'command': f'[ -f "{base_name}.html" ] && echo yes || echo no',
                       'purpose': f'Checking whether {js_file} has a same-named HTML page to link into.',
                       'sandboxId': sandbox_id}, key)
    if isinstance(data, dict) and data.get('allowed') and 'yes' in (data.get('stdout') or ''):
        return f'{base_name}.html'
    return 'index.html'


def _auto_link_js_files(base, key, agent_id, sandbox_id, unlinked_files, messages, tier_slug, task_id=None):
    """Port of tasks.js _autoLinkJsFiles: ask the SAME model in the SAME
    conversation for just the missing <script src> line(s), then execute it,
    grouped by which HTML page each unlinked file belongs on."""
    by_target = {}
    for f in unlinked_files:
        html = _guess_link_target_html(base, key, agent_id, sandbox_id, f)
        by_target.setdefault(html, []).append(f)
    all_ok = True
    notes = []
    for html, files in by_target.items():
        many = len(files) > 1
        prompt = (f'You just created {", ".join(files)} but never added a <script src="..."> tag for '
                  f'{"them" if many else "it"} in {html}, so {"they are" if many else "it is"} never actually '
                  f'loaded by the page. Respond with ONLY a single shell command, no explanation, that appends '
                  f'the missing <script src> tag(s) to {html} -- e.g.:\ncat >> {html} << \'EOF\'\n<script src="{files[0]}"></script>\nEOF')
        r = _serve._http_json('POST', base, '/api/chat',
                       {'model': tier_slug, 'messages': messages + [{'role': 'user', 'content': prompt}],
                        'max_tokens': 400, 'agentId': agent_id,
                        'taskId': task_id}, key)
        if not isinstance(r, dict) or r.get('error') or not r.get('reply'):
            budget_hit = _chat_error_result(r, f'{html}: the follow-up model call was paused because this task used its full model-spend budget')
            if budget_hit:
                return {'ok': False, 'budgetExhausted': True,
                        'taskSpendUsd': budget_hit.get('taskSpendUsd'),
                        'taskSpendAttempts': budget_hit.get('taskSpendAttempts'),
                        'note': budget_hit['note']}
            all_ok = False
            notes.append(f'{html}: follow-up model call failed or returned nothing')
            continue
        link_command = r['reply'].strip().replace('```bash', '').replace('```sh', '').replace('```', '').strip()
        exec_data = _serve._http_json('POST', base, '/api/execute',
                               {'agentId': agent_id, 'command': link_command,
                                'purpose': f'Linking previously-unlinked file(s) into {html}: {", ".join(files)}',
                                'sandboxId': sandbox_id}, key)
        if not isinstance(exec_data, dict) or not exec_data.get('allowed'):
            all_ok = False
            notes.append(f"{html}: blocked ({exec_data.get('reason', 'no reason given') if isinstance(exec_data, dict) else 'no response'})")
            continue
        if not (exec_data.get('exitCode') == 0 and not exec_data.get('timedOut')):
            all_ok = False
            notes.append(f'{html}: exit code {exec_data.get("exitCode")}')
    return {'ok': all_ok, 'note': (all_ok and ', '.join(by_target.keys()) or '; '.join(notes))}


def _find_phantom_script_refs(base, key, agent_id, sandbox_id):
    """Port of tasks.js _findPhantomScriptRefs: which real <script src="...">
    references in ANY *.html file point at a .js file that doesn't exist.
    Returns [] on a read failure -- don't guess-remove something that might be real."""
    check_command = ('for html in *.html; do [ -f "$html" ] || continue; for f in $(grep -oE \'src="[^"]+\\.js"\' "$html" | '
                     "sed -E 's/src=\"//;s/\"$//' | sort -u); do [ -f \"$f\" ] && echo \"EXISTS:$html:$f\" || echo \"PHANTOM:$html:$f\"; done; done")
    data = _serve._http_json('POST', base, '/api/execute',
                      {'agentId': agent_id, 'command': check_command,
                       'purpose': 'Verifying every <script src> reference in every HTML file actually corresponds to a real file.',
                       'sandboxId': sandbox_id}, key)
    if not isinstance(data, dict):
        return []
    stdout = data.get('stdout') or ''
    refs = []
    for m in re.finditer(r'PHANTOM:([^:\n]+):(\S+)', stdout):
        refs.append({'html': m.group(1), 'file': m.group(2)})
    return refs


def _remove_phantom_script_refs(base, key, agent_id, sandbox_id, phantom_refs):
    """Port of tasks.js _removePhantomScriptRefs: cleanup the dangling script
    refs outright (a silent 404 is strictly worse than no reference), grouped
    by which HTML file each reference actually came from."""
    by_html = {}
    for ref in phantom_refs:
        by_html.setdefault(ref['html'], []).append(ref['file'])
    all_ok = True
    notes = []
    for html_name, files in by_html.items():
        excludes = ' '.join('-e ' + json.dumps('src="' + re.escape(f) + '"') for f in files)
        cleanup_command = f'grep -v {excludes} "{html_name}" > "{html_name}.__cleanup_tmp" && mv "{html_name}.__cleanup_tmp" "{html_name}"'
        data = _serve._http_json('POST', base, '/api/execute',
                          {'agentId': agent_id, 'command': cleanup_command,
                           'purpose': f'Removing phantom <script src> reference(s) in {html_name} to nonexistent file(s): {", ".join(files)}',
                           'sandboxId': sandbox_id}, key)
        if not isinstance(data, dict) or not data.get('allowed'):
            all_ok = False
            notes.append(f"{html_name}: blocked ({data.get('reason', 'no reason given') if isinstance(data, dict) else 'no response'})")
        elif not (data.get('exitCode') == 0 and not data.get('timedOut')):
            all_ok = False
            notes.append(f'{html_name}: exit code {data.get("exitCode")}')
    return {'ok': all_ok, 'note': '; '.join(notes) if notes else 'removed'}


def _find_dangling_selector_refs(base, key, agent_id, sandbox_id):
    """Port of tasks.js _findDanglingSelectorRefs: JS queries a .class/#id that
    neither any markup creates nor any JS creates dynamically. Static and
    heuristic (only simple .class/#id/getElementById forms), advisory only --
    there is no safe auto-fix, just a surface-the-finding result."""
    read_command = 'for f in *.html *.js; do [ -f "$f" ] || continue; echo "--- $f ---"; cat "$f"; done'
    data = _serve._http_json('POST', base, '/api/execute',
                      {'agentId': agent_id, 'command': read_command,
                       'purpose': 'Checking for JS selectors with no matching markup or dynamic creation anywhere in the sandbox.',
                       'sandboxId': sandbox_id}, key)
    if not isinstance(data, dict) or not data.get('allowed') or not data.get('stdout'):
        return []
    blob = data.get('stdout') or ''

    files = {}
    current = None
    for line in blob.split('\n'):
        m = re.match(r'^--- (.+) ---$', line)
        if m:
            current = m.group(1)
            files[current] = []
        elif current:
            files[current].append(line)

    html_lines = []
    js_entries = []
    for fname, lines in files.items():
        if fname.endswith('.html'):
            html_lines.extend(lines)
        elif fname.endswith('.js'):
            js_entries.append((fname, '\n'.join(lines)))
    html_text = '\n'.join(html_lines)
    js_text = '\n'.join(text for _, text in js_entries)

    # kind:name -> {name, kind, files:set}
    queried = {}
    for fname, text in js_entries:
        for m in re.finditer(r"""\.querySelector(?:All)?\(\s*['"]([.#][A-Za-z0-9_-]+)['"]""", text):
            raw = m.group(1)
            key_name = f"{raw[0]}:{raw[1:]}"
            kind = 'class' if raw[0] == '.' else 'id'
            queried.setdefault(key_name, {'name': raw[1:], 'kind': kind, 'files': set()})
            queried[key_name]['files'].add(fname)
        for m in re.finditer(r"""getElementById\(\s*['"]([A-Za-z0-9_-]+)['"]""", text):
            key_name = f"id:{m.group(1)}"
            queried.setdefault(key_name, {'name': m.group(1), 'kind': 'id', 'files': set()})
            queried[key_name]['files'].add(fname)

    dangling = []
    for entry in queried.values():
        name, kind, file_set = entry['name'], entry['kind'], entry['files']
        attr = 'class' if kind == 'class' else 'id'
        pattern = re.compile(attr + r'="[^"]*\b' + re.escape(name) + r'\b[^"]*"')
        if pattern.search(html_text):
            continue
        if kind == 'class':
            dynamic = re.compile(r"""classList\.add\([^)]*['"]""" + re.escape(name) + r"""['"]|className\s*=\s*['"][^'"]*\b""" + re.escape(name) + r"""\b""")
        else:
            dynamic = re.compile(r"""\.id\s*=\s*['"]""" + re.escape(name) + r"""['"]|setAttribute\(\s*['"]id['"]\s*,\s*['"]""" + re.escape(name) + r"""['"]""")
        if dynamic.search(js_text):
            continue
        dangling.append({'selector': ('.' if kind == 'class' else '#') + name, 'files': sorted(file_set)})
    return dangling


# The standard quality pipeline every Python-coding agent must pass before its
# work can clear the peer gate (Cut 4). flake8 = PEP 8 + the substantive F-codes;
# mypy = types; bandit = security (High/Medium); pytest-cov enforces >=90%
# coverage. All pre-installed in the _serve.SANDBOX_IMAGE. Deliberately NOT fenced off
# behind a helper that "%" or f-string formats a shell string -- each step ships
# as a literal argv list, so nothing an agent writes is ever interpolated into a
# shell command here.
# The standard clause appended to every Work Room coding system prompt (Cut 4).
# It tells the agent what the OBJECTIVE quality gate (below) will hold it to --
# instruction, not a weakening of the gate. Tests assert on this constant so the
# agent-facing standard stays in lockstep with the hard gate in sim.py.
CODING_STANDARDS_PROMPT = (
    '\n\nCODING STANDARDS (Cut 4): write clean, standards-compliant Python for any .py file you create or edit -- PEP 8 '
    'style, 4-space indent, short lines, meaningful names, no unused imports or dead variables. Before your work counts '
    'as done, it must also pass the SANDBOX quality pipeline, which runs automatically on the workspace and is '
    'non-negotiable: python -m flake8 ., python -m mypy ., python -m bandit -r ., and python -m pytest --cov=. '
    '--cov-fail-under=90 (all tools are pre-installed). Coverage is measured in TOTAL -- across ALL files in the '
    'workspace combined, not per-file -- so write tests that push the overall project coverage to 90% or higher. '
    'If you wrote a JS/HTML-only change, the Python pipeline is trivially green -- fine. If you introduced real logic, '
    'structure it as an importable module with a real test_*.py your own tests would run, not a wall of untestable '
    'globals. Your task is NOT complete and your work will NOT be approved until that pipeline is green.'
)


def _quality_pipeline_steps():
    return [
        {'name': 'flake8', 'command': 'python -m flake8 .'},
        {'name': 'mypy', 'command': 'python -m mypy .'},
        {'name': 'bandit', 'command': 'python -m bandit -r . -q'},
        {'name': 'pytest-cov', 'command': 'python -m pytest --cov=. --cov-fail-under=90 -q'},
    ]


def _run_quality_pipeline(base, key, agent_id, sandbox_id, purpose):
    """Run the standard quality pipeline in the sandbox via /api/pipeline, which
    stops at the first failing step and returns per-step {name, exitCode, stdout,
    stderr} + failedStep (None on a full pass). Always returns a plain dict; never
    raises. Callers gate approval on `ok`."""
    resp = _serve._http_json('POST', base, '/api/pipeline',
                      {'agentId': agent_id, 'sandboxId': sandbox_id,
                       'purpose': purpose, 'steps': _quality_pipeline_steps()}, key)
    if not isinstance(resp, dict) or resp.get('error'):
        return {'ok': False, 'failedStep': None, 'results': [],
                'note': f'quality pipeline did not run: {resp.get("error") if isinstance(resp, dict) else "no response"}'}
    results = resp.get('results') or []
    failed = resp.get('failedStep')
    if failed:
        step = next((s for s in results if s.get('name') == failed), {})
        code = step.get('exitCode')
        summary = (step.get('stderr') or step.get('stdout') or '').strip().splitlines()
        tail = summary[-4:]
        snippet = '\n'.join(tail) if tail else '(no output)'
        return {'ok': False, 'failedStep': failed, 'results': results,
                'note': f'quality pipeline FAILED at {failed} (exit {code}):\n{snippet}'}
    return {'ok': True, 'failedStep': None, 'results': results, 'note': 'quality pipeline all green'}


def _quality_pipeline_evidence(qp):
    """Item 7: extract OBJECTIVE evidence from a quality-pipeline run -- the
    actual per-step output (coverage line, flake8/mypy/bandit tails) that proves
    the code was really exercised by the standard gate, not just declared ok by a
    reviewer. Empty when the pipeline didn't run or produced no output. The sim's
    evidence-based done gate refuses to count a clean vote for a CODING-CLASS
    parent unless this is non-empty."""
    results = qp.get('results') or []
    lines = []
    for s in results:
        name = (s.get('name') or 'step')
        out = (s.get('stdout') or '').strip()
        err = (s.get('stderr') or '').strip()
        text = out + ('\n' + err if err else '')
        if not text.strip():
            continue
        tail = text.splitlines()[-2:]
        lines.append(f'[{name}]\n' + '\n'.join(tail))
    return '\n'.join(lines)[:2000]


def _run_coding_content(snapshot, agent_id, task, base_ctx=None):
    """Port of tasks.js runCodingTask (the real Work Room coding pipeline):
    a real model call with probe-before-writing + heredoc-continuation handling,
    THEN /api/execute to actually run the command, THEN the four mechanical
    integrity checks on the written files (orphaned/phantom/dangling). Uses the
    SWE-bench coding tier for the code itself (always, regardless of how small
    the task sounds). Returns {ok, note} so runWorkroom can phrase the outcome."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    project_label = task.get('projectLabel') or ''
    backlog = task.get('title') or ''
    instructions = task.get('instructions')
    backlog_item = f'{backlog} -- {instructions}' if instructions else backlog
    context_summary = base_ctx or ''
    name = _agent_name(snapshot, agent_id)
    tier_slug = _serve._coding_tier_slug()
    if not tier_slug:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': 'Tried to work on it, but no coding model tier is configured yet.',
                                           'ok': False})
        return False
    colab_available = bool(_serve.COLAB_CLI_AVAILABLE and _serve.COLAB_ENABLED)

    system_prompt = (f'You are {name}, a developer on a small team building {project_label or "a real, working small web application for the team"}. '
                     f'Current project state:\n{context_summary or "(nothing written yet -- you may be starting the first file.)"}\n\n'
                     f'Your task right now: {backlog_item}\n\n'
                     'Before writing your final answer, you may check real facts about how the ACTUAL running page behaves right now -- '
                     'what globals it defines and their real shape, what a button click or keypress actually does -- instead of guessing a '
                     'plausible-sounding name. To do this, respond with ONLY a JSON object, no shell command, no markdown fences, no explanation, '
                     'in exactly this shape: '
                     '{"probeRequest": {"path": "index.html", "actions": [{"type":"click","selector":"text=Play Challenge"}, '
                     '{"type":"keydown","key":"q"}], "probes": ["document.body.className", "typeof window.SomeGlobal"]}}\n'
                     'Action types are click ({selector}), keydown ({key}), wait ({ms}), eval ({code}). '
                     f'You can do this up to {MAX_CODE_PROBE_ROUNDS} times if you genuinely need to. '
                     + ('You may also offload real computation to a Google Colab runtime when this task genuinely needs compute your '
                        'sandbox cannot do -- heavy data parsing/analysis, numeric or ML work. To do that, respond with ONLY a JSON '
                        'object, no shell command, no markdown fences, no explanation, in exactly this shape: '
                        '{"colabRun": {"code": "print(...)", "purpose": "why you are computing this", "packages": [], '
                        '"timeout_seconds": 300, "runtimes": 1, "runtime": "cpu"}}\n'
                        'Your code MUST print everything you need to see. It runs on the Colab free tier, so keep runs small; a slot '
                        'may be refused (provisioning/availability), in which case you may retry once or fall back to a sandbox '
                        'command. To shard ONE computation across multiple runtimes, pass runtimes>1 (up to 5): the same code then runs '
                        'on each granted runtime with COLAB_SHARD_INDEX and COLAB_SHARD_COUNT env vars so it can split the work and '
                        'print its slice. It runs as the player\'s identity -- ONLY raw compute, never Google Drive/GCS/cloud APIs, '
                        'credentials, mining/bulk media, or any exfiltration site. '
                        f'You can do this up to {MAX_CODE_COLAB_ROUNDS} times if you genuinely need to. ' if colab_available else '')
                     + 'When ready to write the actual fix, respond with ONLY a single shell command, no explanation, no markdown fences, '
                     'that writes or updates the necessary file(s) using one or more heredocs, e.g.:\ncat > index.html << \'EOF\'\n<contents>\nEOF\n'
                     'Write real, complete, working code for this specific piece -- no placeholders, no "TODO," no stubs. '
                     'Keep it focused on just this task, building on what already exists rather than starting over. '
                     'If an existing file has grown large, PREFER adding a new small file (e.g. a separate .js file, linked with its own '
                     '<script src> tag) over rewriting the whole large file -- a full rewrite risks running out of room mid-file and being '
                     'cut off incomplete, which a small new file avoids. '
                     + CODING_STANDARDS_PROMPT)
    messages = [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': 'Go ahead.'}]
    command = ''
    attempts = 0
    probe_rounds = 0
    colab_rounds = 0
    _opens = _closes = 0
    balanced = False
    while attempts <= CODE_CONTINUATION_ATTEMPTS:
        reply = None
        r = _serve._http_json('POST', base, '/api/chat',
                       {'model': tier_slug, 'messages': messages,
                        'max_tokens': _CODE_MAX_TOKENS, 'agentId': agent_id,
                        'service': project_label or task.get('productId') or '__general__',
                        'taskId': task.get('id')}, key)
        if not isinstance(r, dict) or r.get('error') or not r.get('reply'):
            budget_hit = _chat_error_result(r, 'this task used its full model-spend budget and the coding call was paused')
            if budget_hit:
                budget_hit['tier'] = tier_slug
                _sim_module._store_content_result(task.get('id'), budget_hit)
            else:
                _sim_module._store_content_result(task.get('id'),
                                                  {'note': 'the model call failed or returned nothing', 'ok': False, 'tier': tier_slug})
            return False
        reply = r['reply']

        probe_req = _parse_probe_request(reply) if probe_rounds < MAX_CODE_PROBE_ROUNDS else None
        if probe_req:
            probe_rounds += 1
            messages.append({'role': 'assistant', 'content': reply})
            probe_data = _serve._http_json('POST', base, '/api/page-probe',
                                    {'agentId': agent_id, 'sandboxId': WORKROOM_SANDBOX_ID,
                                     'path': probe_req['path'], 'actions': probe_req['actions'],
                                     'probes': probe_req['probes']}, key)
            feedback = _format_page_probe_result(probe_data)
            if probe_rounds >= MAX_CODE_PROBE_ROUNDS:
                feedback += f'\n\nYou have used all {MAX_CODE_PROBE_ROUNDS} probe rounds. Respond now with ONLY your final shell command.'
            messages.append({'role': 'user', 'content': feedback})
            continue  # does not count against continuation attempts -- a different failure mode

        colab_req = (_parse_colab_run_request(reply)
                     if colab_available and colab_rounds < MAX_CODE_COLAB_ROUNDS else None)
        if colab_req:
            colab_rounds += 1
            messages.append({'role': 'assistant', 'content': reply})
            feedback = _format_colab_run_result(_serve._colab_compute_run(
                agent_id, colab_req['code'], colab_req['purpose'], colab_req['packages'],
                colab_req['timeout_seconds'], colab_req['runtimes'], colab_req['runtime']))
            if colab_rounds >= MAX_CODE_COLAB_ROUNDS:
                feedback += f'\n\nYou have used all {MAX_CODE_COLAB_ROUNDS} Colab rounds. Respond now with ONLY your final shell command.'
            messages.append({'role': 'user', 'content': feedback})
            continue  # does not count against continuation attempts -- a different failure mode

        # Only the FIRST real command reply may have a fenced-code wrapper worth
        # stripping; a continuation is raw mid-file text by definition.
        if attempts == 0:
            command = reply.strip().replace('```bash', '').replace('```sh', '').replace('```', '').strip()
        else:
            command += reply
        if not command:
            _sim_module._store_content_result(task.get('id'),
                                              {'note': 'the model returned an empty command', 'ok': False, 'tier': tier_slug})
            return False

        _opens, _closes, balanced = _heredoc_balance(command)
        if balanced:
            break
        attempts += 1
        messages.append({'role': 'assistant', 'content': command})
        messages.append({'role': 'user',
                         'content': 'You were cut off before finishing. Continue EXACTLY where you left off -- do not repeat anything you '
                                    'already wrote, do not restart the heredoc or add a new one, just output the rest of the raw file content '
                                    'and the closing EOF line(s).'})
    # Loop exits via `balanced` (the normal case) or by exhausting the
    # continuation attempts -- the truncated-heredoc failure is handled HERE
    # so the loop's own exhausted-exit arc is a reachable, tested path.
    if not balanced:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'generation still looked truncated after {CODE_CONTINUATION_ATTEMPTS} '
                                                   f'continuation attempt(s) ({_opens} heredoc(s) opened, {_closes} closed) -- not executed',
                                           'ok': False, 'tier': tier_slug})
        return False

    exec_data = _serve._http_json('POST', base, '/api/execute',
                           {'agentId': agent_id, 'command': command,
                            'purpose': f'Coding task: {backlog_item}', 'sandboxId': WORKROOM_SANDBOX_ID}, key)
    if not isinstance(exec_data, dict) or not exec_data.get('allowed'):
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f"blocked: {exec_data.get('reason') if isinstance(exec_data, dict) else 'no response'}",
                                           'command': command, 'ok': False, 'tier': tier_slug,
                                           # Item 2: the block reason rides the result channel
                                           # into sim._apply_content_result, where it lands in the
                                           # author's feedback buffer and shows up on the next
                                           # dispatch's perception block.
                                           'feedback': (f'Your command was blocked before running: '
                                                        f'{exec_data.get("reason") if isinstance(exec_data, dict) else "no response"}.')})
        return False
    ok = bool(exec_data.get('exitCode') == 0 and not exec_data.get('timedOut'))
    result = {'ok': ok, 'exitCode': exec_data.get('exitCode'), 'command': command, 'tier': tier_slug}
    if not ok:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'execution exited non-zero (code {exec_data.get("exitCode")}), '
                                                   f'timed out: {bool(exec_data.get("timedOut"))}',
                                           'command': command, 'ok': False, 'tier': tier_slug})
        return False

    # ---- The four mechanical integrity checks (mirror runCodingTask) ----
    written = _extract_written_js_files(command)
    if written:
        unlinked = _find_unlinked_js_files(base, key, agent_id, WORKROOM_SANDBOX_ID, written)
        if unlinked:
            linked = _auto_link_js_files(base, key, agent_id, WORKROOM_SANDBOX_ID, unlinked, messages, tier_slug,
                                         task_id=task.get('id'))
            if linked['ok']:
                result['note'] = f'auto-linked previously-orphaned file(s) into index.html: {", ".join(unlinked)}'
            else:
                if linked.get('budgetExhausted'):
                    _sim_module._store_content_result(task.get('id'), {
                        'ok': False, 'budgetExhausted': True, 'tier': tier_slug,
                        'taskSpendUsd': linked.get('taskSpendUsd'),
                        'taskSpendAttempts': linked.get('taskSpendAttempts'),
                        'note': f'wrote {", ".join(unlinked)}, but the auto-link follow-up was paused because this task used its full model-spend budget.'})
                    return False
                plural = len(unlinked) > 1
                result['note'] = (f'WARNING: created {", ".join(unlinked)} but {"they are" if plural else "it is"} not referenced '
                                  f'by a <script src> tag in index.html, and the automatic follow-up to link {"them" if plural else "it"} '
                                  f'failed -- {linked["note"]}')
    phantom_refs = _find_phantom_script_refs(base, key, agent_id, WORKROOM_SANDBOX_ID)
    if phantom_refs:
        cleaned = _remove_phantom_script_refs(base, key, agent_id, WORKROOM_SANDBOX_ID, phantom_refs)
        ref_list = ', '.join(f'{p["file"]} (in {p["html"]})' for p in phantom_refs)
        phantom_note = (f'removed <script src> reference(s) to file(s) that don\'t actually exist: {ref_list}' if cleaned['ok']
                        else f'WARNING: HTML file(s) reference file(s) that were never written and don\'t exist: {ref_list} '
                             f'-- automatic cleanup failed ({cleaned["note"]})')
        result['note'] = f"{result.get('note')} | {phantom_note}" if result.get('note') else phantom_note

    dangling = _find_dangling_selector_refs(base, key, agent_id, WORKROOM_SANDBOX_ID)
    if dangling:
        pieces = []
        for d in dangling:
            pieces.append('{} (in {})'.format(d['selector'], ', '.join(d['files'])))
        dangling_note = ('WARNING: code queries selector(s) that don\'t exist anywhere in the sandbox\'s markup and are never created '
                         'dynamically either: {} -- likely a missing element, not real gameplay yet.'.format('; '.join(pieces)))
        result['note'] = f"{result.get('note')} | {dangling_note}" if result.get('note') else dangling_note

    # Cut 4 -- the quality pipeline is the fifth hard gate. The write + the four
    # mechanical checks succeeded, but the work is NOT done until the code meets
    # the standard (PEP 8, types, no bandit High/Medium, >=90% coverage). A red
    # pipeline fails the author's task here, before anything reaches review, so
    # there is nothing left for a reviewer to approve.
    qp = _run_quality_pipeline(base, key, agent_id, WORKROOM_SANDBOX_ID,
                               f'Quality gate for coding task: {backlog_item}')
    if not qp['ok']:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'wrote and ran the code, but {qp["note"]}',
                                           'command': command, 'ok': False, 'tier': tier_slug})
        return False

    _sim_module._store_content_result(task.get('id'),
                                      {'note': ((result.get('note') or ('Wrote and ran the code in the shared Work Room sandbox.'))
                                                + ' | quality pipeline all green'),
                                       'command': command, 'ok': True, 'tier': tier_slug})
    return True


GRADE_MEETS = 'meets_requirement'
GRADE_FAILS = 'fails_requirement'
GRADE_UNSURE = 'insufficient_evidence'

# CS329A takeaway #2: Weaver-style verifier ensemble for grading.
# The JS planning model emits a per-requirement CHECKLIST ({id, question,
# section, type: code|jev|human}) that travels with a project's subtasks, but
# the Python review executor (_run_review_content) only ever produced a single
# holistic Jev actionable/clean verdict -- requirement-level grading existed
# only in the browser (grading.js runGradedReviewLoop). This adds the Python
# side: grade each checklist requirement with its OWN focused check, and let
# the ensemble outvote the holistic verdict the way the article's verifier
# ensemble does -- a mechanical 'code' requirement (ground truth = the quality
# pipeline) or a confident focused 'jev' grade that fails is a REAL defect even
# if the holistic call said clean; 'human' requirements and low-confidence
# grades surface to the player instead of being auto-decided.
#
# A requirement whose question is about code health (pipeline/tests/lint/build)
# is decided directly from the LIVE quality-pipeline result the executor
# already ran -- the most objective evidence the review path has. Any other
# 'code' requirement has no predicate that survives serialization (the JS
# predicates are in-memory functions), so it degrades to insufficient_evidence
# -- the same "absent predicate -> unsure" behavior as gradeCodeRequirement in
# grading.js -- rather than guessing.
_PIPELINE_HINT_WORDS = ('pipeline', 'flake8', 'mypy', 'bandit', 'pytest', 'test', 'lint', 'build', 'compile', 'ci', 'green')

# Cap on per-review spend: each focused 'jev' grade is a real Jev call, and the
# checklist is emitted for every subtask of a project -- grading five of them
# costs real money. Five focused checks (the planner's own "2-5 subtasks" scale)
# is a fair ensemble without letting a checklist turn one review into an
# invoice.
MAX_CHECKLIST_JEV_GRADES = 5
# Cap on escalations per review -- an unsure-heavy checklist can't spam the
# player; the first few real questions are enough to get the call.
MAX_CHECKLIST_ESCALATIONS = 3
# SWiRL-style process trace: when a review VERIFIES a checklist requirement
# failed, the review writes a compact lesson file into pending_review/skills/
# (auto-quarantined via source 'external'), where the existing skill-review
# sweep later judges keep/promote vs reject. Bounded: at most a few per
# review, short, and deduped on the requirement -- no repeat spam on re-review.
MAX_CHECKLIST_TRACE_FILES = 3
_PROCESS_TRACE_MAX_CHARS = 4000
# How many verified requirement FAILS the follow-up fix task names explicitly
# (bounded -- the fix instruction is a pointer to the evidence, not a dump).
MAX_QUEUE_FIX_REQUIREMENTS = 2


def _record_review_process_trace(base, key, agent_id, project_label, backlog, kind,
                                 name, checklist_grades, full_review):
    """Write the failed-requirement lessons from a review into
    pending_review/skills/review-trace/ as compact process-trace files, one per
    VERIFIED (GRADE_FAILS) checklist requirement, deduped on the requirement id
    and bounded in count and size. Returns how many were actually written (0
    when there is nothing new worth keeping -- the file already exists, or no
    requirement failed)."""
    written = 0
    for g in checklist_grades:
        if g.get('verdict') != GRADE_FAILS or written >= MAX_CHECKLIST_TRACE_FILES:
            continue
        req_id = g.get('id') or 'req'
        section = g.get('section') or ''
        question = g.get('question') or ''
        slug = _skill_slug(f'{project_label or "project"}-{section}-{req_id}') or 'req'
        path = f'pending_review/skills/review-trace/{_skill_slug(project_label or "project") or "project"}/{slug}.md'
        existing = None
        try:
            r = _serve._http_json('GET', base, '/api/library/file?path=' + urllib.parse.quote(path))
            existing = r.get('content') if isinstance(r, dict) else None
        except Exception:
            existing = None
        if existing:
            continue  # the requirement's lesson is already queued -- don't spam
        conf = g.get('confidence')
        body = (f'# Review trace: {question or req_id}\n\n'
                f'- Project: {project_label or "(unnamed)"}\n'
                f'- Requirement: "{question or req_id}"'
                + (f' (section: {section})' if section else '')
                + f' [{g.get("type", "")}]\n'
                f'- Found during a {kind} of "{backlog}" by {name}\n'
                f'- Verdict: verified FAILS'
                + (f' (Jev confidence {conf:.2f})' if isinstance(conf, float) else '')
                + '\n\n'
                f'## What the reviewer saw\n\n{full_review[:1200]}\n\n'
                f'## Lesson\n\n'
                f'Future work on "{project_label or backlog}" must satisfy this requirement: "{question or req_id}".\n')
        try:
            _serve._http_json('POST', base, '/api/library/file',
                       {'agentId': agent_id, 'path': path, 'content': body[:_PROCESS_TRACE_MAX_CHARS],
                        'source': 'external'}, key)
            written += 1
        except Exception:
            pass
    return written


def _grade_code_requirement(req, qp):
    """Mechanical grade for a 'code'-type checklist requirement. Returns a
    verdict string (GRADE_MEETS/GRADE_FAILS/GRADE_UNSURE) -- no Jev call, no
    cost. Ground truth is the live quality-pipeline result where the
    requirement is about code health; everything else is unknowable from the
    evidence this executor holds and degrades to GRADE_UNSURE (surface to the
    player), never a guess."""
    text = f"{req.get('question', '')} {req.get('section', '')}".lower()
    if any(word in text for word in _PIPELINE_HINT_WORDS):
        return GRADE_MEETS if qp.get('ok') else GRADE_FAILS
    return GRADE_UNSURE


def _grade_jev_requirement(req, review):
    """Focused Jev grade for a 'jev'-type checklist requirement -- a per-
    requirement verifier, not the holistic verdict. Returns (verdict,
    confidence). The Jev contract applies: a meets/fails grade is only acted on
    at confidence >= JEV_SAFETY_CONFIDENCE; below it (or a failed call) the
    requirement is insufficient_evidence and surfaces to the player instead of
    driving an automatic revision on weak signal."""
    try:
        decision = _serve._call_openrouter_decision_sync(
            _serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice',
                        'instructions': f'You are grading one specific requirement of a deliverable. '
                                        f'Requirement: "{req.get("question", "")}". '
                                        f'The section being checked is "{req.get("section", "")}". '
                                        f'Relevant part of the deliverable: "{review[:1500]}" '
                                        f'Answer whether THIS requirement is met by THIS deliverable.',
                        'criteria': {GRADE_MEETS: 'The deliverable satisfies this specific requirement.',
                                     GRADE_FAILS: 'The deliverable does not satisfy this specific requirement.',
                                     GRADE_UNSURE: 'Not enough evidence in the deliverable to judge, or the question cannot be answered from it.'}}})
        choice, confidence, _cost = _serve._jev_choice(decision)
    except Exception:
        return GRADE_UNSURE, 0.0
    if choice in (GRADE_MEETS, GRADE_FAILS) and confidence >= _serve._effective_review_grade_confidence():
        return choice, confidence
    return GRADE_UNSURE, confidence


def _grade_review_checklist(checklist, review, qp, agent_id):
    """Grade every non-'human' checklist requirement against the review, one
    focused check each ('code' = mechanical/pipeline ground truth, 'jev' = a
    confidence-gated focused Jev call). 'human' requirements are never graded
    here -- they surface to the player. Returns a list of grade dicts shaped
    like grading.js's, plus the checklist entries that need a human: {'grades':
    [...], 'escalate': [(req, reason)]}."""
    grades = []
    escalate = []
    jev_graded = 0
    # Judge-vs-anchor calibration (self-deception check, ported in spirit from
    # self-evolve's selfdeception.py): a 'code'-type requirement's verdict is
    # MECHANICAL ground truth (the live quality pipeline), so a section whose
    # code requirements are UNANIMOUS is a real anchor for judging the
    # subjective 'jev' grades in that same section. The Jev grader never sees
    # the pipeline result, so agreement/disagreement is a genuine measurement
    # of the grader, not the grader echoing ground truth back. A section whose
    # code verdicts disagree is ambiguous and is NOT used as an anchor.
    code_anchors = {}
    code_verdicts = {}
    for req in checklist:
        if req.get('type') != 'code':
            continue
        section = req.get('section') or ''
        verdict = _grade_code_requirement(req, qp)
        if verdict in (GRADE_MEETS, GRADE_FAILS):
            code_verdicts.setdefault(section, set()).add(verdict)
    for section, verdicts in code_verdicts.items():
        if len(verdicts) == 1:  # pragma: no cover -- _grade_code_requirement is qp-only, all verdicts in a section are identical
            code_anchors[section] = next(iter(verdicts))
    for req in checklist:
        req_id = req.get('id') or 'req'
        section = req.get('section') or ''
        question = req.get('question') or ''
        rtype = req.get('type')
        if rtype == 'human':
            escalate.append((req, 'review decision for you'))
            continue
        if rtype == 'code':
            grades.append({'id': req_id, 'section': section, 'question': question,
                           'type': 'code', 'verdict': _grade_code_requirement(req, qp),
                           'confidence': 1.0})
        elif rtype == 'jev':
            if jev_graded >= MAX_CHECKLIST_JEV_GRADES:
                grades.append({'id': req_id, 'section': section, 'question': question,
                               'type': 'jev', 'verdict': GRADE_UNSURE,
                               'confidence': None, 'skipped': True})
                escalate.append((req, 'not graded this round (checklist spend cap)'))
                continue
            jev_graded += 1
            verdict, confidence = _grade_jev_requirement(req, review)
            grades.append({'id': req_id, 'section': section, 'question': question,
                           'type': 'jev', 'verdict': verdict, 'confidence': confidence})
            # Record the anchored sample: only definite verdicts count (an
            # UNSURE grade is the judge declining, not a wrong answer), and
            # only when the section has a unanimous mechanical anchor.
            if verdict in (GRADE_MEETS, GRADE_FAILS) and section in code_anchors:
                _serve._insert_review_calibration_sample(
                    section, verdict, confidence, code_anchors[section])
            if verdict == GRADE_UNSURE:
                escalate.append((req, f'uncertain review requirement (Jev confidence {confidence:.2f})'
                                      if confidence else 'uncertain review requirement (Jev call failed)'))
        else:
            # Unknown type -- never auto-decide on it either.
            escalate.append((req, 'review requirement with unrecognized type'))
    return {'grades': grades, 'escalate': escalate}


def _record_classified_failures(agent_id, kind, checklist_grades, escalate, review_text):
    """The 'sort' step's durable write: every verified-failed checklist
    requirement and every escalated review requirement is classified into the
    four-bucket taxonomy (factual error / client preference / missing
    information / style) and appended to the failure ledger in serve.py, where
    the weekly rule-mining pass turns recurring patterns into operator rule
    proposals. Best-effort: recording must never break the review itself, so a
    ledger failure is swallowed (the review result is unchanged)."""
    recorded = 0
    for g in (checklist_grades or []):
        if g.get('verdict') != GRADE_FAILS:
            continue
        question = g.get('question') or g.get('id') or 'requirement'
        section = g.get('section') or ''
        try:
            ftype = _serve._classify_failure(f'{question} {section}')
            _serve._record_failure(ftype, question, rule_hint=question, section=section,
                                   input_text=review_text[:800], agent_id=agent_id)
            recorded += 1
        except Exception:
            pass
    for req, _reason in (escalate or [])[:MAX_CHECKLIST_ESCALATIONS]:
        question = req.get('question') or 'requirement'
        section = req.get('section') or ''
        try:
            ftype = _serve._classify_failure(f'{question} {section}')
            _serve._record_failure(ftype, question, rule_hint=question, section=section,
                                   input_text=review_text[:800], agent_id=agent_id)
            recorded += 1
        except Exception:
            pass
    return recorded


def _perception_block(base_ctx):
    """The situational-awareness block (sim's base_ctx, see sim._build_agent_
    perception) folded into an executor's prompt. '' when the sim supplied
    nothing -- keeps prompts byte-identical for the unit tests that call
    executors bare (base_ctx=None)."""
    return f'\n\nContext from the sim:\n{base_ctx}' if base_ctx else ''


def _run_review_content(snapshot, agent_id, task, base_ctx=None):
    """Port of tasks.js runReviewTask: a real review/QA pass -- gatherUnifiedContext,
    a skeptical critique call (probe-driven), a real screenshot visual pass, a Jev
    actionable/clean verdict, and on 'actionable' a follow-up fix task returned via
    'queueFix' (which sim._apply_content_result enqueues through the single
    read-modify-write -- never a direct DB write here)."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    is_qa = task.get('taskType') == 'qa'
    project_label = task.get('projectLabel') or ''
    backlog = task.get('title') or ''
    instructions = task.get('instructions')
    backlog_item = f'{backlog} -- {instructions}' if instructions else backlog
    name = _agent_name(snapshot, agent_id)
    context = _gather_unified_context(snapshot, base, key, agent_id, WORKROOM_SANDBOX_ID, backlog)

    # Cut 4 -- objective pipeline evidence, run by the sandbox, NOT by reviewer
    # opinion. The approval gate in sim.py refuses to count a clean vote unless
    # this is green; here we surface it to the reviewer and force actionable on
    # a red pipeline no matter what the review text says.
    qp = _run_quality_pipeline(base, key, agent_id, WORKROOM_SANDBOX_ID,
                               f'Quality gate for {("QA" if is_qa else "review")} of: {backlog_item}')

    tier_slug = _serve._coding_tier_slug() or _serve._mid_tier_slug() or _serve._low_tier_slug()
    if not tier_slug:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Tried to {("QA-test" if is_qa else "review")} "{backlog}", but no model tier is configured yet.',
                                           'ok': False})
        return
    kind = 'QA' if is_qa else 'review'
    if is_qa:
        lead = (f'You are {name}, QA-testing a teammate\'s real work for a project: {project_label}. '
                f'Current code/state:\n{context}\n\nTask: {backlog_item}\n\n'
                'Assess, as a real playtester/QA would, whether this is actually usable end to end for its intended purpose -- '
                'not just "a file that sounds related exists." Call out anything broken, missing, or incomplete, as specifically as you can. '
                'Be honest either way.')
    else:
        lead = (f'You are {name}, reviewing a teammate\'s real code for a project: {project_label}. '
                f'Be a genuine, skeptical reviewer, not a rubber stamp. Current code:\n{context}\n\nTask: {backlog_item}\n\n'
                'List real, specific problems you actually see (bugs, broken logic, missing pieces), or say plainly if it genuinely '
                'looks solid. Be honest either way.')
    pipeline_clause = (f'Quality pipeline status (run live in the sandbox just now): {qp["note"]}. '
                       'This is a HARD standard the code must meet -- if it is red, that is a real problem you must flag, even if the '
                       'code otherwise looks fine. It is not optional.' if not qp['ok']
                       else f'Quality pipeline status (run live in the sandbox just now): {qp["note"]}. '
                            'This is the bar the code must clear -- taking the pipeline result as given.')
    system_prompt = (lead +
                     _perception_block(base_ctx) +
                     f'\n\n{pipeline_clause}' +
                     ' Before answering, you may check real facts about how the ACTUAL running page behaves right now -- what a button '
                     'click or keypress actually does -- instead of guessing from the source alone. To do this, respond with ONLY a JSON '
                     'object, no markdown fences, no explanation, in exactly this shape: '
                     '{"probeRequest": {"path": "index.html", "actions": [{"type":"click","selector":"text=Some Button"}, '
                     '{"type":"keydown","key":"a"}], "probes": ["document.body.className", "typeof window.SomeGlobal"]}}\n'
                     'Action types are click ({selector}), keydown ({key}), wait ({ms}), eval ({code}). '
                     f'You can do this up to {MAX_REVIEW_PROBE_ROUNDS} times if you genuinely need to. '
                     # Item 6: the reviewer's final answer carries a structured
                     # verdict -- approve/send_back + the WHY (summary/checks/risks)
                     # -- so the gate decision is the reviewer's own explicit call,
                     # not a Jev guess at what the prose implies. The verdict JSON
                     # is the base; a red pipeline and verified checklist failures
                     # still override it (see below). On a probe round the verdict
                     # is requested again with the same wording.
                     'When ready, respond with your final assessment as plain text, then END with a single JSON object '
                     '(no markdown fence) in exactly this shape: '
                     '{"verdict": "approve" or "send_back", "summary": "<one short sentence>", '
                     '"checks": ["<what you actually verified>"], "risks": ["<what remains a risk>"]}. '
                     '"approve" only if the work genuinely looks solid and the quality pipeline passed; '
                     '"send_back" if you found any real, specific problem that should be fixed.')
    messages = [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': 'Give your assessment.'}]
    review = None
    probe_rounds = 0
    budget_hit = None
    while True:
        r = _serve._http_json('POST', base, '/api/chat',
                       {'model': tier_slug, 'messages': messages,
                        'max_tokens': _REVIEW_MAX_TOKENS, 'agentId': agent_id,
                        'service': project_label or task.get('productId') or '__general__',
                        'taskId': task.get('id')}, key)
        if not isinstance(r, dict) or r.get('error') or not r.get('reply'):
            budget_hit = _chat_error_result(r, f'Tried to {("QA-test" if is_qa else "review")} "{backlog}", but this task used its full model-spend budget and the review call was paused.')
            break
        reply = r['reply'].strip()
        probe_req = _parse_probe_request(reply) if probe_rounds < MAX_REVIEW_PROBE_ROUNDS else None
        if probe_req:
            probe_rounds += 1
            messages.append({'role': 'assistant', 'content': reply})
            probe_data = _serve._http_json('POST', base, '/api/page-probe',
                                    {'agentId': agent_id, 'sandboxId': WORKROOM_SANDBOX_ID,
                                     'path': probe_req['path'], 'actions': probe_req['actions'],
                                     'probes': probe_req['probes']}, key)
            feedback = _format_page_probe_result(probe_data)
            if probe_rounds >= MAX_REVIEW_PROBE_ROUNDS:
                feedback += f'\n\nYou have used all {MAX_REVIEW_PROBE_ROUNDS} probe rounds. Respond now with your final assessment as plain text, then the JSON verdict object as instructed.'
            messages.append({'role': 'user', 'content': feedback})
            continue
        review = reply
        break
    if not review:
        if budget_hit:
            _sim_module._store_content_result(task.get('id'), budget_hit)
            return
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Tried to {("QA-test" if is_qa else "review")} "{backlog}", but the model call didn\'t produce anything usable.',
                                           'ok': False})
        return

    visual = _review_screenshot(base, key, agent_id, WORKROOM_SANDBOX_ID, 'index.html',
                                f'You\'re reviewing a real screenshot of this project\'s main page. Task: {backlog_item}. '
                                'Describe what you actually see, and call out anything that looks visually broken -- elements in the '
                                'wrong place, overlapping, cut off, or missing.',
                                task_id=task.get('id'))
    visual_budget_hit = bool(visual.get('budgetExhausted'))
    full_review = (f'{review}\n\n## Visual check (real screenshot)\n\n{visual["review"]}' if visual['ok']
                   else f'{review}\n\n## Visual check\n\n(could not complete: {visual["note"]})')

    # File the review into the Library (fire-and-forget, never blocks).
    _serve._http_json('POST', base, '/api/library/file',
               {'agentId': agent_id,
                'path': f"archive/{int(time.time() * 1000)}-{task.get('taskType') or 'review'}-{task.get('id') or 'adhoc'}.md",
                'content': f'# {backlog}\n\nProject: {project_label}\nBy: {name} ({kind})\n\n{full_review}\n',
                'source': 'firsthand'}, key)

    # Item 6: the reviewer's own structured verdict is the base vote. The review
    # ends with {"verdict":"approve"|"send_back", ...}; a clean parse gives the
    # reviewer's EXPLICIT call (send_back -> actionable, approve -> clean) as the
    # primary signal. Jev still runs as the prose cross-check + the hard
    # overrides below (red pipeline, verified checklist failures) still outvote
    # it -- fail-closed stays fail-closed.
    structured_verdict, peer_summary, peer_checks, peer_risks = _parse_review_verdict(review)
    structured_actionable = structured_verdict == 'send_back'

    # Jev verdict: actionable -> queue a follow-up fix; clean -> nothing to do.
    verdict = 'clean'
    try:
        decision = _serve._call_openrouter_decision_sync(
            _serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice',
                        'instructions': f'A {kind} just wrote this about "{project_label}": "{full_review[:1500]}" '
                                        f'\n\nQuality pipeline (run live): {qp["note"]}'
                                        f'\nDoes this identify at least one concrete, real, fixable problem (a bug, missing feature, broken integration '
                                        f'or a failed quality pipeline)? '
                                        f'Or does it say things genuinely look solid with nothing actionable?',
                        'criteria': {'actionable': 'Yes -- it names at least one real, specific problem that should be fixed '
                                                   '(including a red flake8/mypy/bandit/pytest-cov pipeline).',
                                     'clean': 'No -- it says things look solid, the quality pipeline is green, and there is nothing '
                                              'a developer needs to act on.'}}})
        verdict, _, _ = _serve._jev_choice(decision)
    except Exception:
        verdict = 'clean'  # deterministic safe fallback: don't queue a fix we can't justify
    # The reviewer's own explicit send_back outvotes a Jev read of the prose; an
    # approve (or a failed JSON parse) leaves Jev's decision as the base.
    if structured_actionable:
        verdict = 'actionable'

    # Cut 4 hard gate: a red quality pipeline can NEVER read as approval. Force
    # the vote to actionable so the story goes back for a fix even if the review
    # text or the Jev call brushed the pipeline failure aside.
    if not qp['ok']:
        verdict = 'actionable'

    # CS329A takeaway #2 -- Weaver-style verifier ensemble: grade the project's
    # checklist requirements with focused per-requirement checks (mechanical for
    # 'code', a confidence-gated Jev call for 'jev'), and let a VERIFIED failure
    # outvote the holistic verdict -- a requirement the verifier caught is a
    # real defect even if the holistic call read clean. Unsure or 'human'
    # requirements surface to the player, never auto-decided.
    checklist_grades = []
    escalated_reqs = []
    checklist = task.get('checklist') or []
    if checklist:
        graded = _grade_review_checklist(checklist, full_review, qp, agent_id)
        checklist_grades = graded['grades']
        if any(g['verdict'] == GRADE_FAILS for g in checklist_grades):
            verdict = 'actionable'
        what_checked = f'Reviewed {len(checklist)} checklist requirements during this {kind} pass'
        for req, reason in graded['escalate'][:MAX_CHECKLIST_ESCALATIONS]:
            try:
                _serve.create_escalation(reason,
                                         f'{req.get("question", "")} ({req.get("section", "") or "deliverable"})',
                                         what_checked=what_checked,
                                         look_first=req.get('question', '') or reason)
                escalated_reqs.append(req.get('id') or 'req')
            except Exception:
                pass
        # The sort step: verified failures + escalated requirements land in the
        # failure ledger (classified into the four buckets) for the weekly
        # rule-mining pass. Best-effort -- never changes the review outcome.
        _record_classified_failures(agent_id, kind, checklist_grades,
                                    graded.get('escalate', []), full_review)

    queue_fix = None
    suffix = 'nothing actionable found'
    is_gate_review = bool(task.get('reviewOf'))
    if verdict == 'actionable':
        queue_fix = {'title': f'Fix issues found in {kind} testing of "{backlog}"',
                     'room': 'pressoffice',
                     'instructions': (f'A {kind} pass of "{project_label}" found real problems -- see the Library entry just filed for '
                                      f'"{backlog}" -- fix them. Build on the existing files.'
                                      + (f'\n\nQuality pipeline is red: {qp["note"]}' if not qp['ok'] else '')),
                     'goal': project_label,
                     # Bug: this never carried
                     # productId, so every fix cycle after a rejection fell
                     # through _run_workroom_content's dispatcher (which checks
                     # productId FIRST) into the generic _run_coding_content --
                     # which is hardcoded to WORKROOM_SANDBOX_ID regardless of
                     # the product's own configured sandbox. A product with its
                     # own dedicated sandbox (e.g. per-agent-sprites) therefore
                     # only ever got its FIRST attempt built there; every
                     # rejection afterward silently started building in the
                     # shared sandbox instead, contaminating it while the real
                     # project sandbox stayed empty.
                     'productId': task.get('productId')}
        if is_gate_review:
            # A gate review that rejects: the fix must go back to the SAME author
            # and re-open the SAME story (carry reviewOf), not a fresh free task.
            # `task` here is the REVIEW SUBTASK (its assignedTo is the reviewer);
            # the author is threaded in as reviewAuthorId so the fix lands on the
            # worker who BUILT the story, not on the reviewer.
            queue_fix['assignedTo'] = task.get('reviewAuthorId') or task.get('assignedTo')
            queue_fix['reviewOf'] = task.get('reviewOf')
        # The ensemble's verified FAILS are the sharpest signal the fix has --
        # name the top ones explicitly (bounded) so the fix task is steered at
        # the actual requirement it missed, not just pointed at the Library.
        failed_reqs = [g for g in checklist_grades if g.get('verdict') == GRADE_FAILS]
        if failed_reqs:
            reqs_text = '\n'.join(
                f'- {g.get("question") or g.get("id") or "requirement"}'
                + (f' (section: {g.get("section")})' if g.get('section') else '')
                for g in failed_reqs[:MAX_QUEUE_FIX_REQUIREMENTS])
            queue_fix['instructions'] += (
                f'\n\nVerified failing requirements from the review checklist:\n{reqs_text}\n'
                'Fix the work so each of these is met.')
        suffix = 'queued a fix'
    # SWiRL: a review that VERIFIED a checklist requirement failed leaves a
    # process-trace lesson in pending_review/skills/ (bounded + deduped by the
    # helper) so the think tank's skill-review sweep can promote it into reference
    # material -- the review writes its own durable trace, no separate crawl.
    process_trace_count = 0
    if verdict == 'actionable' and any(g.get('verdict') == GRADE_FAILS for g in checklist_grades):
        try:
            process_trace_count = _record_review_process_trace(
                base, key, agent_id, project_label, backlog, kind, name,
                checklist_grades, full_review)
        except Exception:
            process_trace_count = 0
    result = {'note': f'Filed a {kind} on "{backlog}" (text + visual), {suffix}',
              'ok': True,
              'pipelineOk': qp['ok'],
              'pipelineSummary': qp['note'],
              # Item 7: the objective quality-pipeline output rides the result so
              # the evidence-based done gate can verify the code was ACTUALLY
              # exercised by the standard gate -- never just "a reviewer said it
              # looked ok". Empty when the pipeline couldn't run.
              'evidence': _quality_pipeline_evidence(qp)}
    if checklist_grades:
        result['checklistGrades'] = checklist_grades
    if escalated_reqs:
        result['checklistEscalated'] = escalated_reqs
    if is_gate_review:
        # Relay the vote so _apply_content_result can count it on the parent.
        result['peerVerdict'] = verdict
    # Item 6: carry the reviewer's structured verdict (its own explicit call +
    # the WHY -- summary / verified checks / remaining risks) through the result
    # channel so _apply_content_result can fold them onto the parent task and
    # the author/player can see the basis of the vote, not just the binary.
    if peer_summary or peer_checks or peer_risks:
        result['peerSummary'] = peer_summary
        result['peerChecks'] = peer_checks
        result['peerRisks'] = peer_risks
    if visual_budget_hit:
        # The vision pass was refused because the task's model-spend budget is
        # exhausted -- this is NOT a review that found work needing a fix, and
        # queueing a stray fix task here would start ANOTHER unpaid lane. Drop
        # it and fail the card closed so the sim's budget branch (notify +
        # fail-closed) owns the shutdown.
        queue_fix = None
        result['budgetExhausted'] = True
        result['taskSpendUsd'] = visual.get('taskSpendUsd')
        result['taskSpendAttempts'] = visual.get('taskSpendAttempts')
        result['note'] = (f'Review text was filed, but the visual screenshot check was paused because this task used its '
                          f'full model-spend budget ({result.get("note", "")}).')
    if process_trace_count:
        result['processTraceCount'] = process_trace_count
    if queue_fix:
        result['queueFix'] = queue_fix
    _sim_module._store_content_result(task.get('id'), result)


def _wiki_context_for_task(state, task, agent_id=None):
    """Read-before-act: fold the wiki pages relevant to a task into a context
    block (markdown) the executor prepends to its model prompt, scoped to the
    task's agent's VILLAGE so a worker only ever sees their own village's wiki.
    Empty string when the wiki has nothing for this task's room -- the executor
    still runs, just with a blank knowledge context (the pre-existing
    behavior)."""
    try:
        import sim as _sim
        if agent_id:
            village_id = _sim.village_of_agent(state, agent_id)
            return _sim.inject_wiki_context(state, task, village_id=village_id)
        return _sim.inject_wiki_context(state, task)
    except Exception:
        return ''


def _design_context_for_task(state, task, agent_id=None):
    """Read-before-act: the folded design taste doc (+ raw inspiration brief
    pointers) for the project this task targets, scoped to the task's agent's
    VILLAGE. Required context for design work -- a product build or a labeled
    workroom task whose project has a taste.md must reason over it. Empty
    string when the project has no design vocabulary (the executor still runs,
    just without a design reference)."""
    try:
        project = task.get('productId') or task.get('projectLabel')
        if not project:
            return ''
        if agent_id:
            village = _sim_village_of_agent(state, agent_id)
            return _serve._design_context_for_project(project, village=village)
        return _serve._design_context_for_project(project)
    except Exception:
        return ''


def _sim_village_of_agent(state, agent_id):
    """Resolve an agent's village, defaulting to the main village when the sim
    helper or the agent is unavailable."""
    try:
        import sim as _sim
        return _sim.village_of_agent(state, agent_id) if agent_id else _sim.DEFAULT_VILLAGE
    except Exception:
        return 'main'


def _run_product_build_content(snapshot, agent_id, task, base_ctx=None):
    """Phase E: a pressoffice task targeting a PRODUCT (task['productId']).
    Reads the product's catalog record + sandbox, injects the wiki context so
    the build reasons over think tank knowledge first, runs the real coding
    pipeline against the product's sandbox, and on success RELEASES a frozen
    revision (snapshot -> library/projects/<id>/v<N>/ + passport entry). Uses
    the SWE-bench coding tier, exactly like _run_coding_content; passes through
    an optional Phase D capability handle from the product's `handles`."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    product_id = task.get('productId')
    record = (snapshot.get('products') or {}).get(product_id)
    if not record:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Tried to build product "{product_id}", but it is not in the catalog.',
                                           'ok': False})
        return
    sandbox_id = record.get('sandboxId')
    if not sandbox_id or not os.path.isdir(os.path.join(_serve.SANDBOXES_DIR, sandbox_id)):
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Tried to build "{record.get("name")}", but its sandbox ({sandbox_id}) is missing.',
                                           'ok': False})
        return
    project_label = record.get('name') or product_id
    backlog = f'Build work on {project_label} toward its next release -- spec: {record.get("spec") or "(none)"}'
    task_title = task.get('title') or backlog
    instructions = task.get('instructions')
    backlog_item = f'{task_title} -- {instructions}' if instructions else task_title
    context_summary = base_ctx or ''
    if not context_summary:
        ls = _serve._http_json('POST', _serve.SELF_BASE_URL, '/api/execute',
                        {'agentId': agent_id, 'sandboxId': sandbox_id, 'command': 'ls -la'},
                        key)
        if isinstance(ls, dict) and ls.get('allowed') and ls.get('stdout'):
            context_summary = f'Current files in the {project_label} sandbox:\n{ls["stdout"]}'
    # Read-before-act: think tank knowledge for this task's room, scoped to the
    # agent's village.
    wiki_ctx = _wiki_context_for_task(snapshot, task, agent_id)
    if wiki_ctx:
        context_summary = f'{wiki_ctx}\n\n{context_summary}'
    # Read-before-act: the folded design taste doc for this product's project
    # (required context for design work -- build against the player's taste,
    # never against a guessed "modern/clean" default), scoped to the agent's
    # village.
    design_ctx = _design_context_for_task(snapshot, task, agent_id)
    if design_ctx:
        context_summary = f'{design_ctx}\n\n{context_summary}'
    ok = _run_coding_content(snapshot, agent_id,
                             {**task, 'projectLabel': project_label,
                              'title': backlog_item, 'instructions': None,
                              'productId': product_id, 'sandboxId': sandbox_id},
                             base_ctx=context_summary)
    # On a real build success (the coding executor reports ok=True), release a
    # frozen revision. The executor's stored content-result is still consumed by
    # _task_cycle via _apply_content_result -- we do NOT take it here.
    if ok:
        _release_product_from_build(product_id, agent_id, base, key)


def _release_product_from_build(product_id, releasing_agent, base, key):
    """Freeze a released revision of a product from inside the live build flow.
    Snapshots the product's sandbox into library/projects/<id>/v<N>/ + a
    RELEASE.md, flips status -> released, chains product_released into the
    passport. This is the same release the endpoint performs, driven by the
    build itself having succeeded. Always best-effort (never raises)."""
    try:
        state = _serve.get_state_from_db()
        if not state:
            return
        import sim as _sim
        record = (state.get('products') or {}).get(product_id)
        if not record:
            return
        sandbox_id = record.get('sandboxId')
        src = os.path.join(_serve.SANDBOXES_DIR, sandbox_id) if sandbox_id else None
        if not src or not os.path.isdir(src):
            return
        revision_no, _rel = _sim.next_product_revision(state, product_id)
        dest_dir = os.path.join(_serve._product_projects_dir(product_id), f'v{revision_no}')
        os.makedirs(dest_dir, exist_ok=True)
        _serve._copy_release_snapshot(src, dest_dir)
        _serve._write_file(os.path.join(dest_dir, 'RELEASE.md'),
                    f'# {record.get("name")} -- v{revision_no}\n\n'
                    f'Released: {datetime.datetime.utcnow().isoformat()}Z\n'
                    f'By: {releasing_agent or "unknown"}\n\nBuilt and frozen from the {sandbox_id} sandbox.\n')
        _sim.product_release_record(state, product_id, revision_no,
                                    releasing_agent or 'player',
                                    'Auto-released by a successful build.',
                                    target_path=f'v{revision_no}')
        _serve.save_state_to_db(state)
        _serve.log_action(releasing_agent or 'player', 'product_released',
                   {'id': product_id, 'revision': revision_no, 'source': 'build'}, authorized=True)
        _serve._append_passport_decision('product_released', releasing_agent or 'player',
                                  {'id': product_id, 'revision': revision_no,
                                   'path': f'v{revision_no}', 'source': 'build'})
    except Exception:
        # Release on a build is best-effort; never break the task cycle over it.
        return


def _plain_completion(model, messages, max_tokens, service='spike', task_id=None,
                      village_id=None):
    """One non-tool completion call, with the same spend accrual _http_json
    self-loopback calls get from serve.py's own endpoints -- used for the
    plan/synthesize bookends of a spike, which need real (possibly extended-
    reasoning) deliberation but no tool access of their own. Returns stripped
    text, or '' on any failure (never raises -- a spike must always still
    complete via SOME path, per the notifyPlayer-on-every-outcome rule)."""
    # Item 4 gate: the spike lane accrues per-task like every other lane, so a
    # spike over its task budget must not keep paying for plan/synthesize.
    if task_id and _serve._task_budget_exhausted(task_id):
        return ''
    try:
        data = _serve._call_openrouter_sync(model, messages, max_tokens)
    except Exception as e:
        print(f'[spike] plain completion failed: {e}', flush=True)
        return ''
    if not isinstance(data, dict):
        return ''
    cost = (data.get('usage') or {}).get('cost', 0.0)
    if isinstance(cost, (int, float)) and cost:
        _serve._accrue_spend(service, cost, village_id=village_id)
        if task_id:
            _serve._accrue_task_spend(task_id, cost)
    try:
        return (data['choices'][0]['message']['content'] or '').strip()
    except (KeyError, IndexError, TypeError):
        return ''


def _parse_reflection(text):
    """Parse a reflection self-check's JSON reply, ported (structure, not
    code) from the user's own MAGI framework's ReflectionEngine.parse_
    reflection -- same shape: tolerate a bare JSON object or one embedded in
    surrounding prose, fall back to a safe default on anything else.
    Confidence defaults to 1.0 (not 0.0 or 0.5) on parse failure -- a
    reflection call that itself failed is not evidence the investigation is
    going badly, and shouldn't inject a confusing nudge on top of a shaky
    signal."""
    text = (text or '').strip()
    data = None
    try:
        if text.startswith('{'):
            data = json.loads(text)
        else:
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                data = json.loads(m.group())
    except (json.JSONDecodeError, TypeError):
        data = None
    if not isinstance(data, dict):
        return 1.0, ''
    confidence = data.get('confidence')
    confidence = float(confidence) if isinstance(confidence, (int, float)) and 0.0 <= confidence <= 1.0 else 1.0
    note = data.get('note') if isinstance(data.get('note'), str) else ''
    return confidence, note


def _parse_review_verdict(text):
    """Item 6: parse the structured verdict block a review ends with --
    {"verdict": "approve"|"send_back", "summary": "...", "checks": [...],
    "risks": [...]}. Returns (verdict_or_None, summary, checks, risks); the
    verdict is the ONLY signal that decides (approve->clean, send_back->
    actionable), the rest is diagnostic carried to the author/player. Tolerates
    the JSON embedded in surrounding prose or wrapped in a code fence; on any
    parse failure returns (None, '', [], []) so the caller falls back to the
    Jev decision exactly as before."""
    text = (text or '').strip()
    data = None
    try:
        if text.startswith('{'):
            data = json.loads(text)
        else:
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                data = json.loads(m.group())
    except (json.JSONDecodeError, TypeError):
        data = None
    if not isinstance(data, dict):
        return None, '', [], []
    verdict = data.get('verdict')
    verdict = verdict if verdict in ('approve', 'send_back') else None
    summary = data.get('summary') if isinstance(data.get('summary'), str) else ''
    checks = data.get('checks') if isinstance(data.get('checks'), list) else []
    checks = [c for c in checks if isinstance(c, str)][:12]
    risks = data.get('risks') if isinstance(data.get('risks'), list) else []
    risks = [r for r in risks if isinstance(r, str)][:12]
    return verdict, summary, checks, risks


def _plan_requires_verification_basis(plan_text):
    """Best-effort detection of whether the PLAN decided a basis (verified/
    estimated) column was needed for a judgment CSV -- used as the trigger
    for the post-hoc disclaimer below, since the plan saying the right thing
    is not the same as the final report actually containing it (real gap
    ."""
    t = (plan_text or '').lower()
    return 'basis' in t and ('verified' in t or 'estimated' in t)


def _finding_shows_basis_column(finding_text):
    t = (finding_text or '').lower()
    return 'basis' in t or ('verified' in t and 'estimated' in t)


_INTERNAL_REVIEW_MARKERS = (
    'our own', 'internal', 'prior research', 'prior investigation', 'prior work',
    'prior spike', 'already found', 'already know', 'already investigated',
    'existing research', 'existing findings', 'existing work', 'previous spike',
    'previous investigation', 'previously found', 'another team', 'other team',
    'a different team', 'before investigating anything new', "what we've",
    "what have we", 'what do we already know', 'built by', 'done by',
)


def _spike_wants_internal_review(backlog, instructions):
    """Best-effort keyword detection of an internal-prior-art-flavored spike
    (e.g. "review Team A's approach before we build something similar") --
    same 'don't reach for a heavier tool than the problem needs' style as
    the other plan-phase detectors above. Used to FORCE search_library as
    the first tool call, not just ask the PLAN prompt to suggest it: real
    gap -- a real spike asked to "review the
    think tank's own prior research on DreyX.com" went straight to browse_page
    and reported "no existing records" despite 7+ real matching entries
    already in the Library, because the PLAN prompt's own "search_library
    first" instruction was never reliably followed. This is the exact same
    failure class already fixed once for search_web (force_first_
    tool=True alone always still reached for browse_page) -- the SAME
    proven fix: force the specific tool by name, don't just ask nicely."""
    text = f'{backlog or ""} {instructions or ""}'.lower()
    return any(marker in text for marker in _INTERNAL_REVIEW_MARKERS)


def _spike_wants_x_trending(backlog, instructions):
    """Same forced-tool-choice reasoning as _spike_wants_internal_review,
    applied on day one instead of after a live miss: 'trending' alone is too
    generic (could mean any platform), so also require a real X/Twitter
    mention -- a bare 'x' is checked as a whole word to avoid matching it
    inside ordinary words (e.g. 'flexible')."""
    text = f'{backlog or ""} {instructions or ""}'.lower()
    mentions_x = bool(re.search(r'\bx\b', text)) or 'twitter' in text or 'x.com' in text
    return 'trending' in text and mentions_x


def _spike_wants_linkedin_search(backlog, instructions):
    text = f'{backlog or ""} {instructions or ""}'.lower()
    return 'linkedin' in text


def _spike_wants_github(backlog, instructions):
    """Same forced-tool-choice reasoning as the other _spike_wants_*
    detectors, applied preemptively (day one) for the GitHub read tools: a
    spike about a real project -- "how does cpython handle X", "check the
    open issues for a repo", "what does the standard library actually
    contain" -- should reach for github_get_repo / github_list_issues first,
    not browse_page (which would hit bot walls or a docs page) and not
    search_web (which is ungrounded for repo internals). Only fires when
    GITHUB_TOKEN is set, mirroring how search_web only fires when Tavily is."""
    text = f'{backlog or ""} {instructions or ""}'.lower()
    if not _serve.GITHUB_TOKEN:
        return False
    markers = ('github', 'open issues', 'issue list', 'pull request', 'the repo', 'the repository',
               'this project on github', 'how does', 'how do they', 'in the real', 'standard library',
               'a real project', 'that repo', 'the cpython', 'in python', 'how is it implemented')
    return any(marker in text for marker in markers)


def _extract_execute_script_outputs(transcript):
    """Pull the stdout of every execute_script tool call out of a real
    _call_agent_tool_loop transcript (matching each tool_call_id to its
    result). Used as a deterministic safety net: confirmed
    live that a spike can cat a real produced file into its own transcript
    and then still summarize it in prose during synthesis instead of
    including it verbatim -- the real data existed, it just never made it
    into the final report. Same philosophy as the basis-column check above:
    catch the miss after the fact rather than trust another round of
    prompting to prevent it."""
    script_call_ids = set()
    for m in (transcript or []):
        if m.get('role') == 'assistant':
            for call in (m.get('tool_calls') or []):
                if (call.get('function') or {}).get('name') == 'execute_script':
                    script_call_ids.add(call.get('id'))
    outputs = []
    for m in (transcript or []):
        if m.get('role') == 'tool' and m.get('tool_call_id') in script_call_ids:
            match = re.search(r'stdout:\n(.*?)(?:\n\nstderr:|\Z)', m.get('content') or '', re.DOTALL)
            if match:
                stdout = match.group(1).strip()
                # Strip the external-data injection wrapper (the END marker the
                # executor appends) so the deterministic raw-output safety net
                # still recovers the real stdout, not the boundary decoration.
                stdout = re.sub(r'\n<<<END_EXTERNAL_DATA.*?\Z', '', stdout, flags=re.DOTALL).strip()
                if stdout:
                    outputs.append(stdout)
    return outputs


# Reflection/replan, ported (design, not code) from the user's
# own MAGI framework's react_engine.py ReflectionEngine -- a real gap: the
# EXECUTE tool loop ran as ONE flat call for its whole iteration budget,
# with no mid-run check on whether it was actually still on track. This
# chunks the SAME total iteration budget into rounds, with a cheap
# confidence/replan self-check between rounds -- never more total tool
# calls than before, just a chance to course-correct WITHIN that budget
# instead of only finding out it went sideways after it's already over.
_REFLECTION_CHUNK_SIZE = 4
_REFLECTION_CONFIDENCE_FLOOR = 0.5


def _run_spike_tool_loop_with_reflection(tier_slug, reasoning_slug, messages, tools, execute_tool,
                                         total_iterations, max_tokens, force_first_tool, task_id=None,
                                         village_id=None):
    remaining = total_iterations
    current_messages = list(messages)
    first_round = True
    execute_text = None
    while remaining > 0:  # pragma: no cover -- the budget-exhausted break below always fires before the condition can re-evaluate False
        this_round = min(_REFLECTION_CHUNK_SIZE, remaining)
        before_len = len(current_messages)
        execute_text, current_messages = _serve._call_agent_tool_loop(
            tier_slug, current_messages, tools, execute_tool,
            max_iterations=this_round, max_tokens=max_tokens, service='spike',
            force_first_tool=(force_first_tool if first_round else False), return_transcript=True,
            task_id=task_id, village_id=village_id)
        remaining -= this_round
        if len(current_messages) == before_len:
            # No progress at all this round (no tool call, no settling text
            # -- shouldn't normally happen once force_first_tool covers
            # round one, but a genuinely stalled round is not worth
            # reflecting on or burning more rounds over).
            break
        first_round = False
        if execute_text is not None or remaining <= 0:
            # Settled on its own, or the budget is spent either way -- no
            # value in reflecting on a round that won't be followed by
            # another one.
            break
        reflection = _plain_completion(reasoning_slug, current_messages + [
            {'role': 'user', 'content': (
                'Self-check, before you continue (this question and your answer to it are NOT part '
                'of your final report): given everything above, respond with ONLY a JSON object: '
                '{"confidence": <0.0-1.0, how likely you are to produce a real, complete answer with '
                'what has actually been gathered so far>, "note": "<one short sentence: what to do '
                'differently, or \'on track\' if the current approach is working>"}')},
        ], max_tokens=150, task_id=task_id, village_id=village_id)
        confidence, note = _parse_reflection(reflection)
        if confidence < _REFLECTION_CONFIDENCE_FLOOR and note and note.strip().lower() != 'on track':
            current_messages.append({'role': 'user', 'content': f'Self-check before continuing: {note}'})
    return execute_text, current_messages


# "No committed deliverable" (a spike's actual
# defining property -- it never opens a peer gate, see _peer_gated_lane)
# got conflated with "no code execution." Sometimes the honest answer to an
# investigation needs to DO something with what was found -- write a CSV of
# the sources, parse/transform collected data, compute a real number instead
# of estimating one -- not just describe it in prose. This gives the
# EXECUTE phase the same real sandboxed execution pressoffice coding tasks
# already use (/api/execute -- classified before it runs, isolated,
# resource-capped), scoped to a PER-SPIKE sandbox (not the shared
# workroom-shared/research-shared ones) so nothing written here is ever
# reachable by a later, unrelated task -- the same class of risk a real
# security audit flagged this same evening for the shared sandboxes.
_SPIKE_SANDBOX_TOOL = {
    'type': 'function',
    'function': {
        'name': 'execute_script',
        'description': (
            'Run a real shell command in an isolated, resource-capped sandbox scoped to THIS '
            'investigation only -- e.g. write a small Python/shell script with a heredoc, then run '
            'it. Use this when the investigation needs to actually DO something with what you '
            'found, not just describe it -- write a CSV of the sources you found, parse/transform '
            'collected data, compute a real number instead of estimating one. If your plan calls for '
            'building a CSV or other structured file, this is how you actually build it -- writing '
            'a description of what the file would contain, in your own answer text, does NOT count '
            'as completing that step; only an actual execute_script call that writes the file does. '
            'The sandbox can install packages (pip/npm) and reach any domain already on the '
            'player-vetted browse allowlist, but has no other internet access. Files you write '
            'persist across calls within this investigation (not across different investigations) -- '
            'cat any file worth keeping out in a later call so its real contents can be included in '
            'your final report.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'command': {'type': 'string',
                           'description': 'A real shell command, e.g. writing a file with a heredoc and then running it.'},
                'purpose': {'type': 'string', 'description': 'One short sentence: why this command is needed.'},
            },
            'required': ['command', 'purpose'],
        },
    },
}


def _make_spike_sandbox_executor(agent_id, agent_key, sandbox_id, struck_tools=None):
    """`struck_tools`, if given a set, gets 'execute_script' added the moment
    a command is BLOCKED (a real policy denial) -- same one-strike shape
    _make_web_tools_executor uses, ported from the user's own MAGI
    framework. A timed-out or failed-but-approved command is transient, not
    a strike -- it may be worth one retry with a different approach.
    Self-checks `struck_tools` too (not just records into it) -- defense in
    depth so a caller that passes the set but forgets to check it itself
    doesn't silently get a no-op."""
    def execute_tool(name, args):
        if struck_tools is not None and name in struck_tools:
            return ('execute_script was already blocked once this investigation (one-strike) -- '
                    'do not call it again, use a different tool or approach instead.')
        if name != 'execute_script':
            raise ValueError(f'unknown tool: {name}')
        command = (args or {}).get('command') or ''
        purpose = (args or {}).get('purpose') or 'spike investigation'
        result = _serve._http_json('POST', _serve.SELF_BASE_URL, '/api/execute', {
            'agentId': agent_id, 'command': command, 'purpose': purpose, 'sandboxId': sandbox_id,
        }, agent_key, timeout=60)
        if not isinstance(result, dict):
            return 'Could not run that command (unexpected response).'
        if result.get('allowed') is False:
            if struck_tools is not None:
                struck_tools.add('execute_script')
            return (f"Command blocked: {result.get('reason', 'not approved')} "
                    "[ONE-STRIKE: this was a policy denial -- do not retry execute_script with a "
                    "similar command, try a genuinely different approach]")
        if result.get('error'):
            return f"Command failed: {result['error']} [this may be transient -- may retry once]"
        stdout = (result.get('stdout') or '').strip()
        stderr = (result.get('stderr') or '').strip()
        exit_code = result.get('exitCode')
        parts = [f'exit code: {exit_code}']
        if stdout:
            parts.append(f'stdout:\n{stdout}')
        if stderr:
            parts.append(f'stderr:\n{stderr}')
        if result.get('timedOut'):
            parts.append('(command timed out -- this may be transient, may retry once with a shorter/simpler command)')
        # Injection boundary: sandbox command output is UNTRUSTED code output
        # (the agent writes it, but the code it runs may download/print arbitrary
        # content). Wrap it exactly like fetched web pages so any instructions
        # embedded in the output are read as data, never obeyed as directives.
        raw = '\n\n'.join(parts)
        wrapped, _nonce, _tag, instruction = _serve.wrap_external_content(
            raw, 'the output of a command run in the sandbox')
        return f'{instruction}\n\n{wrapped}'
    return execute_tool


# Agent work the host machine cannot do -- CUDA/torch GPU
# jobs, fine-tuning experiments, heavy numeric work -- now runs on a real
# Google Colab T4 runtime, provisioned on demand by the spike toolchain and
# budgeted as compute units in the Bank. run_on_colab mirrors execute_script
# (a spike agent hands real code, gets real stdout back) but for a remote GPU
# session instead of the tiny local sandbox. Budget-gated only by serve's
# optional COLAB_MONTHLY_UNITS convention cap -- free tier T4 usage is
# unlimited, runtime just isn't guaranteed (see _colab_budget_exceeded);
# only offered when the colab CLI is actually installed on the machine (see
# COLAB_CLI_AVAILABLE).
_COLAB_RUN_TOOL = {
    'type': 'function',
    'function': {
        'name': 'run_on_colab',
        'description': (
            "Run real Python on a Google Colab runtime -- for computation this "
            "think tank's own machine cannot do: CUDA/torch GPU work, fine-tuning experiments, "
            "large matrix/ML or numeric jobs. The think tank provisions runtime(s) on demand, "
            "executes your code, and returns exactly what each printed -- so your code MUST print "
            "everything you need to see. Use this when a plan step genuinely requires real "
            "computation (not for browsing/text questions, and not for anything the tiny local "
            "sandbox can already do). runtime=\"gpu\" rents a T4 (needed for CUDA/torch GPU work); "
            "runtime=\"cpu\" rents a CPU runtime -- prefer cpu for large tasks that are purely "
            "CPU-bound (big numeric/data/parsing jobs), which should not rent a GPU: CPU slots are "
            "more likely granted on the free tier. It runs on the player's real Colab account, "
            "which is the FREE tier: usage is UNLIMITED (no monthly meter/wallet -- the only hard "
            "cap is the operator-set COLAB_MONTHLY_UNITS convention), but a runtime is NOT "
            "guaranteed -- provisioning can be refused (availability/cooldown), so keep runs small "
            "and retry later if a slot is refused. Provisioning is auto-retried server-side a few "
            "times with backoff before a refusal is reported, so do not re-call immediately on a "
            "refusal. "
            "Sessions get torn down after idle, GPU slots are "
            "not guaranteed. To shard ONE computation across MULTIPLE runtimes (chain runtimes for a "
            "single task), pass runtimes>1: the same code then runs on each granted runtime with "
            "COLAB_SHARD_INDEX (0-based) and COLAB_SHARD_COUNT env vars so it can split work and "
            "print its slice; the account grants whatever it grants (may be fewer than requested, "
            "reported honestly back), extra runtimes are torn down after. SHARDING POLICY: if "
            "Jev's decisions model is degraded (fallback active), sharding is refused -- the "
            "server will tell you and you should re-run with runtimes=1 or wait for recovery. "
            "The server independently caps your count with its own Jev gate: a sharded request is "
            "approved only up to the band the computation's stated purpose earns (single=1, "
            "double=2, shard=up to 5), so request runtimes>1 ONLY when the work genuinely "
            "parallelizes and accept a reduced count if the gate judges it smaller. "
            "HARD LIMIT: the code runs "
            "as the player's identity, so it may ONLY touch compute -- never Google Drive, GCS/cloud "
            "APIs, credentials, mining/bulk-media/torrents, offensive-security tooling, or any exfil "
            "site; and every literal URL the code references must clear the same allowlist/JEV "
            "gate as local browsing (off-limits hosts are refused up front, before any run)."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'code': {'type': 'string',
                         'description': 'Complete, self-contained Python source to run on the GPU runtime. It must print its results.'},
                'purpose': {'type': 'string',
                            'description': 'Short statement of what this computation is for (attribution, like execute_script purpose).'},
                'packages': {'type': 'array', 'items': {'type': 'string'},
                             'description': 'Optional pip package names to install before running (e.g. ["transformers", "sentencepiece"]). torch/cuda come preinstalled.'},
                'timeout_seconds': {'type': 'integer',
                                    'description': 'Optional execution timeout in seconds (default 300, hard max 900).'},
                'runtimes': {'type': 'integer',
                             'description': 'Optional number of runtimes to shard this ONE computation across (default 1; max 5). Each granted runtime runs the same code with COLAB_SHARD_INDEX and COLAB_SHARD_COUNT env vars so it can split the work and print its slice. The account grants whatever it grants -- fewer than requested is reported back, not an error; extra runtimes are torn down after.'},
                'runtime': {'type': 'string', 'enum': ['gpu', 'cpu'],
                            'description': 'Optional runtime kind (default "gpu"). "gpu" rents a T4 (required for CUDA/torch/GPU work). "cpu" rents a CPU runtime -- use it for large tasks that are purely CPU-bound (big numeric/data/parsing jobs), which should NOT rent a GPU: CPU slots are more likely granted on the free tier, so CPU sharding is often the more reliable path for big shardable work.'},
            },
            'required': ['code'],
        },
    },
}


def _make_colab_compute_executor(agent_id, struck_tools=None):
    """run_on_colab for a spike. In-process call into serve's Colab gateway
    (_colab_compute_run -- blocking, runs in this worker thread), formatted
    like execute_script so the model sees exit/units/stdout as one result.
    One-strike shape kept for consistency with the other executors (a budget-
    exhausted or provisioning failure is a transient/metered note, not a
    POLICY denial, so it does NOT strike -- retry/rephrase stays open)."""
    def execute_tool(name, args):
        if struck_tools is not None and name in struck_tools:
            return (f'{name} was already blocked once this investigation (one-strike) -- '
                    'do not call it again, use a different tool or approach instead.')
        if name != 'run_on_colab':
            raise ValueError(f'unknown tool: {name}')
        args = args or {}
        code = (args.get('code') or '').strip()
        if not code:
            return '__TOOL_ERROR__: run_on_colab requires the "code" argument'
        purpose = args.get('purpose') or 'spike computation'
        packages = [p for p in (args.get('packages') or [])
                    if isinstance(p, str) and p.strip()][:12]
        try:
            timeout = int(args.get('timeout_seconds') or 300)
        except (TypeError, ValueError):
            timeout = 300
        try:
            runtimes = int(args.get('runtimes') or 1)
        except (TypeError, ValueError):
            runtimes = 1
        kind = args.get('runtime') or 'gpu'
        try:
            result = _serve._colab_compute_run(agent_id, code, purpose, packages, timeout, runtimes, kind)
        except Exception as e:
            return f'__TOOL_ERROR__: Colab run crashed: {e}'
        if not isinstance(result, dict):
            return '__TOOL_ERROR__: unexpected Colab run response'
        if result.get('error'):
            return f'__TOOL_ERROR__: {result["error"]}'
        parts = [f'Colab GPU run OK (session: {result.get("session") or "colab"}, '
                 f'{result.get("elapsed_s", 0)}s, {result.get("units", 0)} compute units):']
        if result.get('runtimes'):
            parts.insert(0, f'Colab sharded GPU run OK across {result["runtimes"]} runtime(s) '
                            f'({result.get("elapsed_s", 0)}s, {result.get("units", 0)} compute units total):')
        stdout = (result.get('stdout') or '').strip()
        if stdout:
            parts.append(f'output:\n{stdout}')
        if not stdout:
            parts.append('(no output -- your code printed nothing)')
        # Injection boundary: Colab output is UNTRUSTED code output (remote GPU
        # code the agent wrote may print arbitrary content). Wrap it like any
        # other external data so embedded instructions are data, not directives.
        raw = '\n\n'.join(parts)
        wrapped, _nonce, _tag, instruction = _serve.wrap_external_content(
            raw, 'the output of a Colab GPU run')
        return f'{instruction}\n\n{wrapped}'
    return execute_tool


# The inverse of promote-spike's fix (a spike's
# real findings now flow FORWARD into a new story) is a spike whose job is
# to review work another team already did (a finished story, an earlier
# spike, a distilled wiki page) BEFORE this team builds something similar
# of its own. Until now a spike had zero way to actually read that prior
# work -- only search_web/browse_page (the outside internet) -- so a
# "review Team A's approach" spike could only guess at what Team A did, the
# same fabrication risk web tool access was built to fix, just aimed
# inward instead of outward. These wrap the same real, already-proven
# search (_library_search_matches -- also what the clarify router's
# knowledge-base-first lookup uses, so agents search the identical index a
# real player-facing feature already relies on) and read (_safe_library_path
# -- the same containment intent_promote_spike itself uses) the Library
# already holds, in-process, no self-loopback hop needed.
_LIBRARY_SEARCH_TOOL = {
    'type': 'function',
    'function': {
        'name': 'search_library',
        'description': (
            "Search the think tank's own Library -- real completed work from ANY team (finished "
            "stories, past spikes, research, peer reviews, wiki merges). Use this BEFORE "
            "search_web when the investigation is about reviewing, comparing against, or building "
            "on something already done inside this think tank (e.g. \"review Team A's approach before "
            "we build something similar\") -- guessing at another team's work instead of actually "
            "reading it produces a fabricated review. Returns matching file paths with a short real "
            "snippet around each hit; call read_library_file on a promising path for the full text."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string',
                          'description': 'Keywords likely to appear in the prior work, e.g. "login rate limiting" or a team/project name.'},
            },
            'required': ['query'],
        },
    },
}

_LIBRARY_READ_TOOL = {
    'type': 'function',
    'function': {
        'name': 'read_library_file',
        'description': (
            'Read the full real content of one Library file by its exact path (from '
            "search_library's results). Use this to actually read another team's finished work -- "
            'its findings, code notes, or CSV -- instead of relying on a search snippet alone.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'Exact Library-relative path, e.g. "archive/1234-spike-task-9.md".'},
            },
            'required': ['path'],
        },
    },
}


def _make_library_tools_executor(agent_id, struck_tools=None):
    """search_library / read_library_file for a spike reviewing another
    team's real prior work. Read-only, no ACL beyond what the Library
    already applies to everyone (the think tank is transparent about who did
    what). Self-checks `struck_tools` (defense in depth, same shape as the
    other two executors) even though a read-only lookup failing is unlikely
    to ever be a POLICY denial rather than "no matches" -- kept consistent
    so a future caller that adds a gate here for free gets one-strike too.

    Logs via log_action (added after a live burn-in): these two
    tools are in-process (no /api/browse-style HTTP hop), so unlike every
    other tool they had ZERO observability -- when a real live run's report
    claimed "no existing records" despite matching entries already in the
    Library, there was no way to directly confirm whether search_library
    was ever actually called at all versus just never finding a match. This
    closes that gap for good, not just for a one-off check."""
    def execute_tool(name, args):
        if struck_tools is not None and name in struck_tools:
            return (f'{name} was already blocked once this investigation (one-strike) -- '
                    'do not call it again, use a different tool or approach instead.')
        args = args or {}
        if name == 'search_library':
            query = (args.get('query') or '').strip()
            if not query:
                return 'query is required'
            matches = _serve._library_search_matches(query)[:20]
            _serve.log_action(agent_id, 'search_library',
                              {'query': query, 'matches': len(matches)}, authorized=True)
            if not matches:
                return f'No Library matches for "{query}" -- no internal record of this was found.'
            # Gap: the top-ranked match isn't
            # always the most substantial one -- a live run read the first
            # (most recent) result, which happened to be a prior attempt's
            # OWN thin, inconclusive finding, while richer earlier
            # investigations of the same question ranked lower. Real size
            # (not a prompt asking the model to "try harder") is a cheap,
            # mechanical signal of which match is actually worth reading.
            lines = [f'{m["path"]} ({m.get("size", "?")} bytes): {m["snippet"]}' for m in matches]
            hint = ''
            if len(matches) > 1:
                hint = ('\n\n(Multiple matches found -- if these look like repeated attempts at the '
                       'same question, the largest is not automatically the best, but a much smaller '
                       'one is a real signal it may be thin or inconclusive. Consider reading more '
                       'than one before concluding.)')
            return '\n'.join(lines) + hint
        if name == 'read_library_file':
            path = (args.get('path') or '').strip()
            target = _serve._safe_library_path(path)
            if not target or not os.path.isfile(target):
                _serve.log_action(agent_id, 'read_library_file',
                                  {'path': path, 'found': False}, authorized=True)
                return f'Not found: {path}'
            try:
                with open(target, 'r', errors='replace') as f:
                    content_text = f.read(20_000)
            except OSError:
                return f'Could not read {path}'
            # Trail reinforcement: a real, actual read of this
            # finding by another team's investigation is exactly the signal
            # that should make it rank higher in future search_library calls.
            _serve.record_library_read(path)
            _serve.log_action(agent_id, 'read_library_file',
                              {'path': path, 'found': True}, authorized=True)
            return content_text
        raise ValueError(f'unknown tool: {name}')
    return execute_tool


# Real, on-demand social/trend monitoring.
# Deliberately on-demand only, not a recurring cadence -- real
# per-call cost against Treg's own $10+ balance (separate from the
# OpenRouter spend cap), and the discipline established this same evening
# was "no more building beyond what's needed, watch spend closely." These
# are FIXED-purpose, narrow calls (a location's trends, a keyword search),
# not an open redirect to an arbitrary agent-chosen URL -- same reasoning
# weather_now is not Jev-gated: there is no domain/URL for Jev to judge,
# the target is fixed by the tool itself.
_TREG_X_TRENDING_TOOL = {
    'type': 'function',
    'function': {
        'name': 'x_trending_topics',
        'description': (
            "Get real, currently trending topics on X (Twitter) for a location, via Treg's real "
            "X API proxy (a real ~$0.01 call against the think tank's Treg balance). Use this when "
            "the investigation needs to know what is ACTUALLY trending right now -- never invent "
            "a plausible-sounding trend from training knowledge."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'woeid': {'type': 'integer',
                          'description': 'Where On Earth ID for the location; 1 = worldwide (default if omitted).'},
            },
            'required': [],
        },
    },
}

_TREG_LINKEDIN_SEARCH_TOOL = {
    'type': 'function',
    'function': {
        'name': 'search_linkedin_posts',
        'description': (
            "Search real, recent public LinkedIn posts by keyword, via Treg's real LinkedIn API "
            "proxy (a real ~$0.002 call against the think tank's Treg balance). Use this for a real, "
            "current view of what is actually being posted about a topic -- never invent a "
            "plausible-sounding LinkedIn post."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'The search query, e.g. "AI security".'},
                'date_posted': {'type': 'string',
                                'description': ('Recency filter: one of "last-hour", "last-day", '
                                                '"last-week", "last-month", "last-year". Defaults to "last-week".'),
                                'enum': ['last-hour', 'last-day', 'last-week', 'last-month', 'last-year']},
            },
            'required': ['query'],
        },
    },
}

# Real YouTube transcript extraction via the download-and-transcribe route:
# every video is treated the same (the player's call -- "force it to always
# download") -- the Apify youtube-link actor downloads the audio and
# faster-whisper transcribes it, both on the think tank's Colab runtime, so
# the extraction is metered against the Colab compute budget. The target host
# set is FIXED by the tool itself (youtube.com / youtu.be only, enforced by
# _serve._is_youtube_url), so there is no arbitrary URL for Jev to judge.
# Deliberately on-demand only: an agent fetches a transcript when an
# investigation genuinely needs a video's content -- never an invented-looking
# answer from training knowledge.
_YOUTUBE_TRANSCRIPT_TOOL = {
    'type': 'function',
    'function': {
        'name': 'youtube_transcript',
        'description': (
            "Fetch the real transcript of a YouTube video as plain text, by downloading the "
            "video's audio (via the Apify youtube-link actor) and transcribing it with whisper "
            "on the think tank's Colab runtime -- every video is treated the same, no captions "
            "shortcut (metered against the Colab compute budget). Give a youtube.com or youtu.be "
            "URL (you can paste the watch?v=, /shorts/, or youtu.be link). Returns the full "
            "transcript text (truncated at ~50k chars). Use this when an investigation needs what "
            "a video actually says -- e.g. to analyze a claimed method, pull quoted claims, or "
            "research a tutorial -- never invent a plausible-sounding video summary."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'url': {'type': 'string',
                        'description': 'The YouTube video URL, e.g. "https://youtu.be/NSuMfeTVHqY" or a youtube.com/watch?v= link.'},
                'lang': {'type': 'string',
                         'description': 'Subtitle language code (e.g. "en" for English). Defaults to "en".'},
            },
            'required': ['url'],
        },
    },
}


def _make_treg_tools_executor():
    """x_trending_topics / search_linkedin_posts -- thin
    wrappers over _serve._treg_call. No one-strike/struck_tools tracking
    here (unlike the web/sandbox executors): those track POLICY denials
    from a real gate (Jev, the sandbox classifier), and there is no such
    gate here to deny anything -- an error from Treg is a real API/network
    failure, not a policy decision, so it's always worth a caller retrying
    once rather than being refused locally on a second attempt."""
    def execute_tool(name, args):
        args = args or {}
        if name == 'x_trending_topics':
            woeid = args.get('woeid') or 1
            data, error = _serve._treg_call('x.x.get-trends-by-woeid', {'woeid': woeid}, method='GET')
            if error:
                return f'Could not get trending topics: {error}'
            _serve._accrue_spend('treg', _serve.TREG_ENDPOINT_COSTS['x.x.get-trends-by-woeid'])
            # Injection boundary: X posts are untrusted third-party text that can
            # embed instructions. Wrap like any fetched web page.
            raw = json.dumps(data)[:4000]
            wrapped, _nonce, _tag, instruction = _serve.wrap_external_content(
                raw, 'X trending topics')
            return f'{instruction}\n\n{wrapped}'
        if name == 'search_linkedin_posts':
            query = (args.get('query') or '').strip()
            if not query:
                return 'query is required'
            params = {'query': query, 'date_posted': args.get('date_posted') or 'last-week'}
            data, error = _serve._treg_call('scrapecreators.x.v1-linkedin-search-posts', params, method='GET')
            if error:
                return f'Could not search LinkedIn posts: {error}'
            _serve._accrue_spend('treg', _serve.TREG_ENDPOINT_COSTS['scrapecreators.x.v1-linkedin-search-posts'])
            # Injection boundary: LinkedIn post text is untrusted third-party
            # content that can embed instructions. Wrap like any fetched page.
            raw = json.dumps(data)[:4000]
            wrapped, _nonce, _tag, instruction = _serve.wrap_external_content(
                raw, 'LinkedIn posts')
            return f'{instruction}\n\n{wrapped}'
        raise ValueError(f'unknown tool: {name}')
    return execute_tool


def _make_youtube_transcript_executor(agent_id, agent_key):
    """youtube_transcript -- thin wrapper over the host's /api/youtube-transcript
    endpoint (always the download-and-transcribe route on the Colab runtime).
    Same shape as the Treg executor: no struck_tools tracking because there is
    no policy gate here to deny anything -- a failure is a real API/network/
    transcription issue, always worth a caller retrying once rather than being
    refused locally on a second attempt. Cost is metered against the Colab
    compute budget, never accrued against an agent spend balance."""
    def execute_tool(name, args):
        if name != 'youtube_transcript':
            raise ValueError(f'unknown tool: {name}')
        args = args or {}
        url = (args.get('url') or '').strip()
        if not url:
            return 'url is required: paste the YouTube video link to transcribe.'
        lang = (args.get('lang') or 'en').strip() or 'en'
        # Apify actor + whisper on a cold Colab runtime can take a few minutes;
        # 600s matches the Colab-side ceiling so a slow-but-healthy run is not
        # cut short by this loopback's own timeout.
        result = _serve._http_json('POST', _serve.SELF_BASE_URL, '/api/youtube-transcript', {
            'agentId': agent_id, 'url': url, 'lang': lang,
        }, agent_key, timeout=600)
        if not isinstance(result, dict):
            return 'Could not fetch that transcript (unexpected response).'
        if result.get('error'):
            return f'Could not fetch transcript: {result["error"]} [may be a transient network error -- may retry once]'
        text = result.get('transcript') or ''
        if not text:
            return 'Transcript fetched but empty (no captions on this video?).'
        filed = result.get('filed')
        where = (f'\n\nFiled to Library: media/transcripts/ (read it with '
                 f'read_library_file path={filed})' if filed else '')
        # Injection boundary: a transcript is untrusted third-party text that
        # can embed instructions. Wrap like any fetched page.
        wrapped, _nonce, _tag, instruction = _serve.wrap_external_content(
            text, 'a YouTube transcript')
        return f"Transcript of {result.get('url')} ({result.get('chars')} chars):\n\n{instruction}\n\n{wrapped}{where}"
    return execute_tool


# Real Apify scraping/automation, wired to the same budget chokepoint the
# Bank already reconciles against. Deliberately on-demand only (an agent
# calls these when an investigation genuinely needs real scraped data --
# never an invented-looking answer). Cost is accrued from the actor run's own
# reported usageTotalUsd (a real, Apify-verified number, matching the "don't
# fabricate a number, use a verified one" rule), and apify_run_actor refuses
# to START a run once _apify_budget_exceeded() -- the same fail-closed budget
# guard the Bank teller uses. Gated on APIFY_API_KEY: absent means the tools
# are simply not offered (same conditional-availability rule as search_web /
# the GitHub tools), so the surface never advertises an unusable tool.
_APIFY_RUN_ACTOR_TOOL = {
    'type': 'function',
    'function': {
        'name': 'apify_run_actor',
        'description': (
            "Start a real Apify actor (a web scraper/automation) with the think tank's real Apify "
            "account, sending it the given input. This is a real, metered run against the "
            "think tank's Apify FREE-plan budget (fail-closed: the run is refused if the monthly "
            "budget is spent). Use this when the investigation needs ACTUAL scraped data from a "
            "site -- never invent a plausible-sounding scrape result. Waits up to waitSeconds for "
            "the run to finish and returns the run's status, its dataset id, and the scraped items "
            "if it succeeded."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'actorId': {'type': 'string',
                            'description': 'The Apify actor id, e.g. "apify/website-content-crawler".'},
                'input': {'type': 'object',
                          'description': ('The actor\'s input JSON (startUrls, maxCrawledPages, etc.) '
                                          '-- varies per actor; check the actor\'s docs.')},
                'waitSeconds': {'type': 'integer',
                                'description': ('How long to wait (seconds) for the run to finish '
                                                'before returning its current status. Max 60.')},
            },
            'required': ['actorId', 'input'],
        },
    },
}

_APIFY_GET_DATASET_ITEMS_TOOL = {
    'type': 'function',
    'function': {
        'name': 'apify_get_dataset_items',
        'description': (
            "Fetch items from a real Apify dataset (the output of a previous apify_run_actor call), "
            "using the datasetId that call returned. Returns up to `limit` raw items."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'datasetId': {'type': 'string',
                              'description': 'The Apify dataset id returned by apify_run_actor.'},
                'limit': {'type': 'integer',
                          'description': 'Maximum number of items to fetch (default 10, max 50).'},
            },
            'required': ['datasetId'],
        },
    },
}

_APIFY_TOOL_NAMES = ('apify_run_actor', 'apify_get_dataset_items')


def _make_apify_tools_executor():
    """apify_run_actor / apify_get_dataset_items -- thin wrappers over
    _serve._apify_call. No one-strike/struck_tools tracking here (same
    reasoning as the treg/pixellab executors): those track POLICY denials
    from a real gate, and there is no such gate here -- an error from Apify
    is a real API/network failure or the budget gate, never a per-call
    policy decision, so it's always worth a caller retrying once. Budget
    enforcement happens BEFORE any real run starts (_apify_budget_exceeded
    fails closed), and real cost is accrued from the run's own usageTotalUsd
    when it settles."""
    def execute_tool(name, args):
        args = args or {}
        if name == 'apify_run_actor':
            actor_id = (args.get('actorId') or '').strip()
            if not actor_id:
                return 'actorId is required'
            if _serve._apify_budget_exceeded():
                return ('Apify monthly budget is exhausted -- refusing to start this actor run '
                        '(fail closed). Do not retry; say so in your findings.')
            inp = args.get('input') or {}
            wait = min(int(args.get('waitSeconds') or 30), 60)
            query = {'timeout': str(max(60, wait + 30))}
            data, error = _serve._apify_call(
                f'/actors/{actor_id}/runs', method='POST', body=inp,
                query=query, timeout=max(60, wait + 30))
            if error:
                return f'Could not start Apify actor: {error}'
            run = (data or {}).get('data') or {}
            run_id = run.get('id')
            dataset_id = run.get('defaultDatasetId')
            status = run.get('status') or 'RUNNING'
            elapsed = 0.0
            while status in ('READY', 'RUNNING') and elapsed < wait:
                time.sleep(3)
                elapsed += 3
                data, error = _serve._apify_call(f'/actor-runs/{run_id}', timeout=20)
                if error:
                    break
                run = (data or {}).get('data') or {}
                status = run.get('status') or 'RUNNING'
                dataset_id = run.get('defaultDatasetId') or dataset_id
            cost = run.get('usageTotalUsd') or 0.0
            if cost:
                _serve._accrue_apify_spend(cost)
            result = {'runId': run_id, 'datasetId': dataset_id, 'status': status,
                      'usageTotalUsd': cost}
            if status == 'SUCCEEDED' and dataset_id:
                items, error = _serve._apify_call(
                    f'/datasets/{dataset_id}/items',
                    query={'format': 'json', 'clean': '1', 'limit': '10'}, timeout=20)
                if error:
                    result['itemsError'] = error
                else:
                    result['items'] = items
            # Injection boundary: scraped web items are untrusted third-party
            # content. Wrap before returning to the model.
            raw = json.dumps(result)[:6000]
            wrapped, _nonce, _tag, instruction = _serve.wrap_external_content(
                raw, 'scraped Apify actor results')
            return f'{instruction}\n\n{wrapped}'
        if name == 'apify_get_dataset_items':
            dataset_id = (args.get('datasetId') or '').strip()
            if not dataset_id:
                return 'datasetId is required'
            limit = max(1, min(int(args.get('limit') or 10), 50))
            data, error = _serve._apify_call(
                f'/datasets/{dataset_id}/items',
                query={'format': 'json', 'clean': '1', 'limit': str(limit)}, timeout=30)
            if error:
                return f'Could not fetch Apify dataset items: {error}'
            # Injection boundary: dataset items are untrusted third-party content.
            raw = json.dumps(data)[:6000]
            wrapped, _nonce, _tag, instruction = _serve.wrap_external_content(
                raw, 'Apify dataset items')
            return f'{instruction}\n\n{wrapped}'
        raise ValueError(f'unknown tool: {name}')
    return execute_tool


# Real character-sprite generation,
# to wire up the remaining documented-but-unused APIs. Follows the SAME
# call shape as the think tank's own already-tested spike script
# (scripts/pixellab_spike.py), not a fresh guess at the public API. Real
# cost tracked via a before/after balance delta (PixelLab has no per-call
# price list the way Treg's catalog does -- see _pixellab_account_balance's
# own docstring for why a forced, uncached read is required on both sides
# of the call), matching the "don't fabricate a number, use a verified one"
# rule already applied to every other real integration.
_PIXELLAB_CHARACTER_TOOL = {
    'type': 'function',
    'function': {
        'name': 'generate_pixel_character',
        'description': (
            "Generate a real 4-direction pixel-art game character sprite from a text description, "
            "via the think tank's real PixelLab account. This is a real, metered generation (billed "
            "against the think tank's PixelLab balance or subscription allotment) and can take up to "
            "~90 real seconds -- only call this when the investigation genuinely needs a real "
            "generated sprite, not a description of what one might look like."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'description': {'type': 'string',
                                'description': ('What the character should look like, e.g. "young office '
                                                'worker in casual attire, SNES-style top-down RPG sprite".')},
                'view': {'type': 'string', 'description': 'Camera angle, e.g. "high top-down" (the default).'},
            },
            'required': ['description'],
        },
    },
}


def _make_pixellab_tools_executor():
    def execute_tool(name, args):
        args = args or {}
        if name != 'generate_pixel_character':
            raise ValueError(f'unknown tool: {name}')
        description = (args.get('description') or '').strip()
        if not description:
            return 'description is required'
        balance_before = _serve._pixellab_account_balance(force=True)
        resp, error = _serve._pixellab_call('POST', '/create-character-with-4-directions', {
            'description': description,
            'image_size': {'width': 48, 'height': 48},
            'view': args.get('view') or 'high top-down',
            'template_id': 'mannequin',
        })
        if error:
            return f'Could not generate character: {error}'
        char_id = (resp or {}).get('character_id')
        job_id = (resp or {}).get('background_job_id')
        if not char_id or not job_id:
            return f'Unexpected response from PixelLab: {json.dumps(resp)[:500]}'
        _job, error = _serve._pixellab_poll_job(job_id)
        if error:
            return f'Could not generate character: {error}'
        char, error = _serve._pixellab_call('GET', f'/characters/{char_id}')
        if error:
            return f'Could not fetch the generated character: {error}'
        rotation_urls = (char or {}).get('rotation_urls') or {}
        balance_after = _serve._pixellab_account_balance(force=True)
        if isinstance(balance_before, (int, float)) and isinstance(balance_after, (int, float)):
            # Spending REDUCES the balance -- cost is before minus after, not
            # the other way around (real bug caught by this file's own test:
            # a balance drop from 7.41 to 7.35 must accrue $0.06, not $0.00).
            real_cost = max(0.0, balance_before - balance_after)
            if real_cost > 0:
                _serve._accrue_spend('pixellab', real_cost)
        return json.dumps({'character_id': char_id, 'rotation_urls': rotation_urls})
    return execute_tool


# Google Sheets/Calendar. Both APIs
# are free today (quota, not cost -- see library/skills/google-sheets-
# calendar.md), so unlike Treg/PixelLab there's no spend to accrue; the
# real constraint here is quota, and the skill doc's own stated policy is
# "prefer read-only / low-frequency use... over any write-heavy or high-
# frequency automation" -- these tools stay simple, single-call operations,
# never a batch/high-frequency loop.
# Google API quota awareness: folded into every spike's system prompt so the
# agent paces its Google calls and never trips the shared per-project limits
# (the same account is used for Sheets/Calendar/Gmail/Docs, so one agent's loop
# hurts every other agent's real Google work). Numbers are the current public
# Google Workspace API defaults:
#   Calendar: 10,000 req/min/project, 600 req/min/user, 1,000,000 req/day/project.
#   Sheets:   300 reads/min/project + 60 reads/min/user, 300 writes/min/project
#             + 60 writes/min/user; 2MB max payload; 180s max processing time.
#   Gmail:    1,200,000 units/min/project, 6,000 units/min/user, 80M units/day.
#             Per-method: messages.get=20, messages.list=5, drafts.create=10.
#   Docs:     3,000 reads/min/project + 300 reads/min/user, 600 writes/min/project
#             + 60 writes/min/user.
# Gmail is READ + DRAFT ONLY -- the account has no send capability and the agent
# must never try to send email.
_GOOGLE_QUOTA_AWARENESS_BLOCK = (
    '\n\nGoogle APIs (Sheets/Calendar/Gmail/Docs) share ONE Google account and one set of per-project '
    'quota limits, so every call an agent makes competes with every other agent\'s real Google work. '
    'Pace yourself: batch reads, never loop a Google tool, and prefer the cheapest tool that answers '
    'the question. Sheet reads cost ~300/min/project and writes ~60/min/user; Calendar runs '
    '~600/min/user; Docs reads ~300/min/user and writes ~60/min/user; Gmail is metered in units '
    '(a message read is 20, a search 5, a draft 10). Treat a Google call that returns a quota/429 '
    'error as a stop signal for that tool this investigation, not a reason to retry it in a loop. '
    'The think tank also enforces a HARD shared rate cap per API (Sheets ~40 calls/min, Gmail ~80, '
    'Docs ~80, Calendar ~400, spaced across all agents); if a Google tool returns the rate-budget '
    'message, that budget is genuinely spent -- stop using that API this investigation. '
    'Gmail is READ + DRAFT ONLY: you may search, read, and create drafts for the player to send, '
    'but you have NO ability to send email and must never attempt to.'
)
_GOOGLE_SHEETS_READ_TOOL = {
    'type': 'function',
    'function': {
        'name': 'read_google_sheet',
        'description': (
            "Read a real range of cells from a real Google Sheet, via the think tank's own Google "
            "account. Use this to check real, current spreadsheet data -- never invent plausible-"
            "looking cell values. Respect Google's Sheets API quota: 300 reads/min/project, 60/"
            "min/user (shared across all the think tank's Google calls) -- read a bounded range, "
            "do not page through whole spreadsheets in a loop."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'spreadsheet_id': {'type': 'string', 'description': 'The spreadsheet ID from its URL (the long id between /d/ and /edit).'},
                'range': {'type': 'string', 'description': 'A1-notation range, e.g. "Sheet1!A1:D20".'},
            },
            'required': ['spreadsheet_id', 'range'],
        },
    },
}

_GOOGLE_SHEETS_APPEND_TOOL = {
    'type': 'function',
    'function': {
        'name': 'append_google_sheet_row',
        'description': (
            "Append one real row to a real Google Sheet, via the think tank's own Google account. "
            "Use this sparingly (occasional syncs, not a high-frequency loop) -- e.g. adding a "
            "finding to a shared roadmap sheet. Respect Google's Sheets API write quota: 300 writes/"
            "min/project, 60/min/user (shared across all the think tank's Google calls) -- never "
            "loop appends in a tight loop."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'spreadsheet_id': {'type': 'string', 'description': 'The spreadsheet ID from its URL.'},
                'range': {'type': 'string', 'description': 'A1-notation range identifying the sheet/table to append to, e.g. "Sheet1!A1".'},
                'values': {'type': 'array', 'items': {'type': 'string'}, 'description': "The row's real values, in column order."},
            },
            'required': ['spreadsheet_id', 'range', 'values'],
        },
    },
}

_GOOGLE_CALENDAR_LIST_TOOL = {
    'type': 'function',
    'function': {
        'name': 'list_calendar_events',
        'description': (
            "List real, real upcoming events on the think tank's own Google Calendar. Use this to "
            "check what's actually scheduled -- never invent a plausible-sounding event. Respect "
            "Google's Calendar API quota: 10,000 requests/min/project, 600/min/user (shared across "
            "all the think tank's Google calls) -- a single bounded list is fine, do not loop it."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'max_results': {'type': 'integer', 'description': 'Maximum events to return (default 10).'},
            },
            'required': [],
        },
    },
}

_GOOGLE_CALENDAR_CREATE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'create_calendar_event',
        'description': (
            "Create one real event on the think tank's own Google Calendar -- e.g. for a real "
            "ceremony. Use this sparingly (a handful of real events, not a high-frequency loop). "
            "Respect Google's Calendar API quota: 10,000 requests/min/project, 600/min/user (shared "
            "across all the think tank's Google calls) -- creating a few real events is fine, never "
            "loop event creation."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'summary': {'type': 'string', 'description': 'The event title.'},
                'start_datetime': {'type': 'string', 'description': 'ISO 8601 start, e.g. "2026-10-01T14:00:00-04:00".'},
                'end_datetime': {'type': 'string', 'description': 'ISO 8601 end, e.g. "2026-10-01T15:00:00-04:00".'},
                'description': {'type': 'string', 'description': 'Optional event description.'},
            },
            'required': ['summary', 'start_datetime', 'end_datetime'],
        },
    },
}

# Gmail read tools + draft creation (READ + DRAFT ONLY -- NEVER SEND).
# The think tank's Google account is read/write for Sheets/Calendar/Docs, but
# email is deliberately NOT send-capable: an agent may search/read messages and
# create DRAFTS for the player to review, but there is NO send tool and no
# path to messages.send / drafts.send. A persuasive agent (or a prompt-injected
# email) must not be able to mail on the player's behalf. Quota (Gmail's
# per-method unit model): 1,200,000 units/min/project, 6,000/min/user, 80M/day.
_GMAIL_SEARCH_TOOL = {
    'type': 'function',
    'function': {
        'name': 'search_gmail_messages',
        'description': (
            "Search the think tank's own Gmail inbox for messages matching a query (sender, subject, "
            "text), using Gmail's search syntax e.g. \"from:someone subject:invoice\". Read-only. "
            "Returns a bounded list of message metadata (id, sender, subject, snippet) -- use "
            "read_gmail_message on a specific id to read the full text. NEVER send email: there is "
            "no send capability on this account. Respect Gmail API quota (messages.list costs 5 "
            "units; 6,000 units/min/user shared across all Google calls) -- a handful of searches "
            "is fine, never loop."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': "Gmail search query, e.g. 'from:someone subject:invoice'."},
                'max_results': {'type': 'integer', 'description': 'Maximum messages to return (default 10, max 25).'},
            },
            'required': ['query'],
        },
    },
}

_GMAIL_READ_TOOL = {
    'type': 'function',
    'function': {
        'name': 'read_gmail_message',
        'description': (
            "Read the full text of one real Gmail message by id (returned by search_gmail_messages). "
            "Read-only. NEVER send email: there is no send capability on this account. Respect Gmail "
            "API quota (messages.get costs 20 units; 6,000 units/min/user shared across all Google "
            "calls) -- read a few specific messages, never page through a whole inbox."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'message_id': {'type': 'string', 'description': 'The Gmail message id to read.'},
            },
            'required': ['message_id'],
        },
    },
}

_GMAIL_CREATE_DRAFT_TOOL = {
    'type': 'function',
    'function': {
        'name': 'create_gmail_draft',
        'description': (
            "Create a DRAFT email in the think tank's own Gmail for the player to review and send -- "
            "drafts are never auto-sent, and there is NO send capability on this account, so the "
            "player must press send themselves. Use this to prepare a real reply or outreach an "
            "agent was asked to draft. Respect Gmail API quota (drafts.create costs 10 units; 6,000 "
            "units/min/user shared across all Google calls) -- draft a handful, never loop."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'to': {'type': 'string', 'description': 'Recipient email address(es), comma-separated.'},
                'subject': {'type': 'string', 'description': 'The draft subject line.'},
                'body': {'type': 'string', 'description': 'The draft plain-text body.'},
            },
            'required': ['to', 'subject', 'body'],
        },
    },
}

# Google Docs tools: read + create. Read quota 3,000/min/project, 300/min/user;
# write quota 600/min/project, 60/min/user (shared across all Google calls).
_GOOGLE_DOCS_READ_TOOL = {
    'type': 'function',
    'function': {
        'name': 'read_google_doc',
        'description': (
            "Read the full text of one real Google Doc by id. Use this to check real, current "
            "document content -- never invent what a doc says. Respect Google Docs API quota: 3,000 "
            "reads/min/project, 300/min/user (shared across all the think tank's Google calls) -- "
            "read a bounded set of docs, never loop."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'document_id': {'type': 'string', 'description': 'The Google Doc id from its URL.'},
            },
            'required': ['document_id'],
        },
    },
}

_GOOGLE_DOCS_CREATE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'create_google_doc',
        'description': (
            "Create a new real Google Doc with a title and optional body text. Use this sparingly "
            "(a real deliverable that belongs in Docs, not a high-frequency loop). Respect Google "
            "Docs API write quota: 600 writes/min/project, 60/min/user (shared across all the think "
            "tank's Google calls) -- creating a handful of real docs is fine, never loop."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string', 'description': 'The new document title.'},
                'body': {'type': 'string', 'description': 'Optional initial plain-text body content.'},
            },
            'required': ['title'],
        },
    },
}


def _make_google_tools_executor():
    def execute_tool(name, args):
        args = args or {}
        if name == 'read_google_sheet':
            spreadsheet_id = (args.get('spreadsheet_id') or '').strip()
            rng = (args.get('range') or '').strip()
            if not spreadsheet_id or not rng:
                return 'spreadsheet_id and range are required'
            url = (f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/'
                  f'{_serve.urllib.parse.quote(rng, safe="")}')
            data, error = _serve._google_call('GET', url)
            if error:
                return f'Could not read the sheet: {error}'
            raw = json.dumps(data)[:4000]
            wrapped, _nonce, _tag, instruction = _serve.wrap_external_content(
                raw, 'a Google Sheet range')
            return f"{instruction}\n\n{wrapped}"
        if name == 'append_google_sheet_row':
            spreadsheet_id = (args.get('spreadsheet_id') or '').strip()
            rng = (args.get('range') or '').strip()
            values = args.get('values')
            if not spreadsheet_id or not rng or not values:
                return 'spreadsheet_id, range, and values are required'
            url = (f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/'
                  f'{_serve.urllib.parse.quote(rng, safe="")}:append?valueInputOption=USER_ENTERED')
            data, error = _serve._google_call('POST', url, {'values': [values]})
            if error:
                return f'Could not append the row: {error}'
            return json.dumps(data)[:2000]
        if name == 'list_calendar_events':
            max_results = args.get('max_results') or 10
            now_iso = _serve.datetime.datetime.utcnow().isoformat() + 'Z'
            url = ('https://www.googleapis.com/calendar/v3/calendars/primary/events?'
                  f'maxResults={int(max_results)}&orderBy=startTime&singleEvents=true&timeMin={now_iso}')
            data, error = _serve._google_call('GET', url)
            if error:
                return f'Could not list calendar events: {error}'
            raw = json.dumps(data.get('items', []))[:4000]
            wrapped, _nonce, _tag, instruction = _serve.wrap_external_content(
                raw, 'a Google Calendar event list')
            return f"{instruction}\n\n{wrapped}"
        if name == 'create_calendar_event':
            summary = (args.get('summary') or '').strip()
            start = (args.get('start_datetime') or '').strip()
            end = (args.get('end_datetime') or '').strip()
            if not summary or not start or not end:
                return 'summary, start_datetime, and end_datetime are required'
            body = {'summary': summary, 'start': {'dateTime': start}, 'end': {'dateTime': end}}
            if args.get('description'):
                body['description'] = args['description']
            data, error = _serve._google_call(
                'POST', 'https://www.googleapis.com/calendar/v3/calendars/primary/events', body)
            if error:
                return f'Could not create the event: {error}'
            return json.dumps({'id': data.get('id'), 'htmlLink': data.get('htmlLink')})
        if name == 'search_gmail_messages':
            query = (args.get('query') or '').strip()
            if not query:
                return 'query is required'
            max_results = args.get('max_results') or 10
            url = ('https://gmail.googleapis.com/gmail/v1/users/me/messages?'
                   f'q={_serve.urllib.parse.quote(query)}&maxResults={int(max_results)}')
            data, error = _serve._google_call('GET', url)
            if error:
                return f'Could not search Gmail: {error}'
            items = data.get('messages') or []
            if not items:
                return 'No messages matched that Gmail query.'
            # messages.list returns only id+threadId; resolve each to a
            # readable snippet via a bounded read of a few messages.
            lines = []
            for m in items[:10]:
                mid = m.get('id')
                meta, merr = _serve._google_call(
                    'GET', f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}?format=metadata')
                if merr:
                    lines.append(f'- {mid}: (could not read: {merr})')
                    continue
                headers = {h.get('name'): h.get('value')
                           for h in ((meta.get('payload') or {}).get('headers') or [])}
                snippet = (meta.get('snippet') or '')[:160]
                lines.append(f"- {mid} | From: {headers.get('From', '?')} | Subject: "
                             f"{headers.get('Subject', '?')} | {snippet}")
            out = '\n'.join(lines)
            wrapped, _nonce, _tag, instruction = _serve.wrap_external_content(
                out, 'a Gmail search result')
            return f"{instruction}\n\n{wrapped}"
        if name == 'read_gmail_message':
            mid = (args.get('message_id') or '').strip()
            if not mid:
                return 'message_id is required'
            data, error = _serve._google_call(
                'GET', f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}?format=full')
            if error:
                return f'Could not read the message: {error}'
            headers = {h.get('name'): h.get('value')
                       for h in ((data.get('payload') or {}).get('headers') or [])}
            body_parts = []
            payload = data.get('payload') or {}
            for part in payload.get('parts') or []:
                body = part.get('body') or {}
                if part.get('mimeType') == 'text/plain' and body.get('data'):
                    body_parts.append(_gmail_base64url_decode(body['data']))
            if not body_parts and (payload.get('body') or {}).get('data'):
                body_parts.append(_gmail_base64url_decode(payload['body']['data']))
            out = (f"From: {headers.get('From', '?')}\nTo: {headers.get('To', '?')}\n"
                   f"Date: {headers.get('Date', '?')}\nSubject: {headers.get('Subject', '?')}\n\n"
                   + ('\n\n'.join(body_parts) or (data.get('snippet') or '')))
            out = out[:12000]
            wrapped, _nonce, _tag, instruction = _serve.wrap_external_content(
                out, 'a Gmail message')
            return f"{instruction}\n\n{wrapped}"
        if name == 'create_gmail_draft':
            to = (args.get('to') or '').strip()
            subject = (args.get('subject') or '').strip()
            body = (args.get('body') or '').strip()
            if not to or not subject:
                return 'to, subject, and body are required'
            import base64
            raw_msg = f"To: {to}\r\nSubject: {subject}\r\n\r\n{body}"
            data, error = _serve._google_call(
                'POST', 'https://gmail.googleapis.com/gmail/v1/users/me/drafts',
                {'message': {'raw': base64.urlsafe_b64encode(raw_msg.encode('utf-8')).decode()}})
            if error:
                return f'Could not create the draft: {error}'
            return (f"Draft created (id {data.get('id')}) -- a draft is NEVER auto-sent. "
                    "The player must open Gmail and press send.")
        if name == 'read_google_doc':
            doc_id = (args.get('document_id') or '').strip()
            if not doc_id:
                return 'document_id is required'
            data, error = _serve._google_call(
                'GET', f'https://docs.googleapis.com/v1/documents/{doc_id}')
            if error:
                return f'Could not read the doc: {error}'
            # Flatten the doc's structured body into plain text.
            texts = []
            for el in (data.get('body') or {}).get('content') or []:
                para = el.get('paragraph') or {}
                for run in para.get('elements') or []:
                    tr = run.get('textRun') or {}
                    if tr.get('content'):
                        texts.append(tr['content'])
            out = ''.join(texts)[:12000]
            wrapped, _nonce, _tag, instruction = _serve.wrap_external_content(
                out, 'a Google Doc')
            return f"{instruction}\n\n{wrapped}"
        if name == 'create_google_doc':
            title = (args.get('title') or '').strip()
            if not title:
                return 'title is required'
            body_text = (args.get('body') or '').strip()
            data, error = _serve._google_call(
                'POST', 'https://docs.googleapis.com/v1/documents', {'title': title})
            if error:
                return f'Could not create the doc: {error}'
            doc_id = data.get('documentId')
            if body_text and doc_id:
                _serve._google_call(
                    'POST', f'https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate',
                    {'requests': [{'insertText': {'location': {'index': 1}, 'text': body_text}}]})
            return json.dumps({'documentId': doc_id,
                               'url': f'https://docs.google.com/document/d/{doc_id}/edit'})
        raise ValueError(f'unknown tool: {name}')
    return execute_tool


def _gmail_base64url_decode(encoded):
    import base64
    try:
        return base64.urlsafe_b64decode(encoded).decode('utf-8', errors='replace')
    except Exception:
        return ''


# GitHub read tools: the think tank is an
# engineering org that already PUBLISHES to GitHub, but had no READ access --
# it could push real code yet could never ground its engineering work in real
# repos/issues/PRs (it would hallucinate plausible-looking ones instead). These
# are FIXED, narrow, READ-ONLY calls (a repo's metadata, a repo's open issues,
# a single issue's thread, a code search) -- no writes, no comments, no PR
# creation. Free: GitHub's public API has no per-call spend, only a per-hour
# rate limit (core API 5,000/hr, search 10/min), so unlike Treg/PixelLab there
# is nothing to accrue -- the real constraint is rate, and the skill stays
# low-frequency / single-call like the Google tools above.
_GITHUB_REPO_TOOL = {
    'type': 'function',
    'function': {
        'name': 'github_get_repo',
        'description': (
            "Fetch REAL metadata about a GitHub repository (owner/repo): description, stars, "
            "language, topics, default branch, open-issue count. Use this when engineering or "
            "research work references a real project -- read the real repo, never invent a "
            "plausible-sounding one from training knowledge."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string', 'description': 'The repo owner (user or org), e.g. "python".'},
                'repo': {'type': 'string', 'description': 'The repository name, e.g. "cpython".'},
            },
            'required': ['owner', 'repo'],
        },
    },
}

_GITHUB_ISSUES_TOOL = {
    'type': 'function',
    'function': {
        'name': 'github_list_issues',
        'description': (
            "List REAL open issues for a GitHub repository (owner/repo) -- number, title, state, "
            "labels. Use this when engineering work should track what a real project is actually "
            "dealing with (open bugs, feature requests), never invent a plausible-looking issue."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string', 'description': 'The repo owner (user or org).'},
                'repo': {'type': 'string', 'description': 'The repository name.'},
                'state': {'type': 'string', 'description': 'Issue state: "open" (default), "closed", or "all".', 'enum': ['open', 'closed', 'all']},
                'limit': {'type': 'integer', 'description': 'Max issues to return (default 10, max 30).'},
            },
            'required': ['owner', 'repo'],
        },
    },
}

_GITHUB_ISSUE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'github_get_issue',
        'description': (
            "Fetch ONE REAL GitHub issue by number (owner/repo#number) -- title, body, state, "
            "labels, and the top comments. Use this to read the actual discussion of a real "
            "issue before reasoning about it, never reconstruct it from memory."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string', 'description': 'The repo owner (user or org).'},
                'repo': {'type': 'string', 'description': 'The repository name.'},
                'issue_number': {'type': 'integer', 'description': 'The issue number, e.g. 1234.'},
            },
            'required': ['owner', 'repo', 'issue_number'],
        },
    },
}

_GITHUB_SEARCH_TOOL = {
    'type': 'function',
    'function': {
        'name': 'github_search_code',
        'description': (
            "Search REAL public GitHub code by query (e.g. a library name, a config pattern, a "
            "function). Returns matching repos/files with URLs. Use this to find how real "
            "projects actually do something before copying or referencing a pattern. Search API "
            "is rate-limited harder than the core API (10/min), so keep code searches few."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'A GitHub code-search query, e.g. "openrouter in:file language:python".'},
                'limit': {'type': 'integer', 'description': 'Max results to return (default 5, max 10).'},
            },
            'required': ['query'],
        },
    },
}

_GITHUB_TOOL_NAMES = ('github_get_repo', 'github_list_issues', 'github_get_issue', 'github_search_code')


def _make_github_tools_executor():
    """github_get_repo / github_list_issues / github_get_issue / github_search_code
    -- thin wrappers over _serve._github_call. No struck_tools / no spend
    accrual: read-only, rate-limited (not gated), and free like Google."""

    def _owner_repo(args):
        owner = (args.get('owner') or '').strip()
        repo = (args.get('repo') or '').strip()
        return owner, repo

    def execute_tool(name, args):
        args = args or {}
        if name == 'github_get_repo':
            owner, repo = _owner_repo(args)
            if not owner or not repo:
                return 'owner and repo are required'
            data, error = _serve._github_call(
                'GET', f'https://api.github.com/repos/{_serve.urllib.parse.quote(owner, safe="")}/{_serve.urllib.parse.quote(repo, safe="")}')
            if error:
                return f'Could not read the repo: {error}'
            return json.dumps({
                'full_name': data.get('full_name'),
                'description': data.get('description'),
                'stars': data.get('stargazers_count'),
                'forks': data.get('forks_count'),
                'language': data.get('language'),
                'topics': data.get('topics'),
                'default_branch': data.get('default_branch'),
                'open_issues': data.get('open_issues_count'),
                'pushed_at': data.get('pushed_at'),
                'html_url': data.get('html_url'),
            }, indent=1)[:4000]
        if name == 'github_list_issues':
            owner, repo = _owner_repo(args)
            if not owner or not repo:
                return 'owner and repo are required'
            state = (args.get('state') or 'open').strip()
            limit = min(int(args.get('limit') or 10), 30)
            data, error = _serve._github_call(
                'GET', f'https://api.github.com/repos/{_serve.urllib.parse.quote(owner, safe="")}/{_serve.urllib.parse.quote(repo, safe="")}/issues'
                       f'?state={state}&per_page={limit}')
            if error:
                return f'Could not list issues: {error}'
            items = data if isinstance(data, list) else []
            rows = [{'number': it.get('number'), 'title': it.get('title'),
                     'state': it.get('state'), 'labels': [l.get('name') for l in (it.get('labels') or [])],
                     'comments': it.get('comments'), 'html_url': it.get('html_url')}
                    for it in items if 'pull_request' not in it]
            return json.dumps(rows, indent=1)[:4000]
        if name == 'github_get_issue':
            owner, repo = _owner_repo(args)
            number = args.get('issue_number')
            if not owner or not repo or number is None:
                return 'owner, repo, and issue_number are required'
            base = f'https://api.github.com/repos/{_serve.urllib.parse.quote(owner, safe="")}/{_serve.urllib.parse.quote(repo, safe="")}'
            data, error = _serve._github_call('GET', f'{base}/issues/{int(number)}')
            if error:
                return f'Could not read the issue: {error}'
            if data.get('pull_request'):
                return f'#{number} is a pull request, not an issue -- use the issues list.'
            comments_data, comments_error = _serve._github_call('GET', f'{base}/issues/{int(number)}/comments?per_page=20')
            comments = []
            if not comments_error and isinstance(comments_data, list):
                comments = [{'user': (c.get('user') or {}).get('login'), 'body': c.get('body')[:1000]}
                            for c in comments_data]
            return json.dumps({
                'number': data.get('number'), 'title': data.get('title'),
                'state': data.get('state'), 'labels': [l.get('name') for l in (data.get('labels') or [])],
                'body': (data.get('body') or '')[:2000], 'html_url': data.get('html_url'),
                'top_comments': comments,
            }, indent=1)[:5000]
        if name == 'github_search_code':
            query = (args.get('query') or '').strip()
            if not query:
                return 'query is required'
            limit = min(int(args.get('limit') or 5), 10)
            data, error = _serve._github_call(
                'GET', 'https://api.github.com/search/code'
                       f'?q={_serve.urllib.parse.quote(query, safe="")}&per_page={limit}')
            if error:
                return f'Could not search code: {error}'
            items = (data or {}).get('items') or []
            rows = [{'repo': ((it.get('repository') or {}).get('full_name')),
                     'path': it.get('path'), 'html_url': it.get('html_url')} for it in items]
            return json.dumps({'total_count': data.get('total_count'), 'matches': rows}, indent=1)[:4000]
        raise ValueError(f'unknown tool: {name}')
    return execute_tool


_SPIKE_ISSUE_PROBLEM_SIGNALS = (
    'gap', 'problem', 'missing', 'broken', 'needs', 'fails', 'failed', 'unable',
    'should', 'recommend', 'issue', 'risk', 'limitation', 'error', 'bug',
    'outdated', 'stale', 'vulnerability', 'unmet', 'incomplete', 'crashed',
    'crash', 'not work', 'doesn\'t work', 'can\'t', 'inconsisten', 'incorrect',
    'defect', 'regression', 'worrying', 'concern', 'concerned', 'critical',
)


def _spike_file_issue_wish(finding, task, name, backlog):
    """W3: does this spike's finding identify a real, actionable gap worth
    filing as an issue, and if so, what should the wish look like?

    Workers have almost no autonomous self-proposal path (file_issue is
    HTTP-endpoint-only). A worker who spots a real gap mid-spike currently has
    no model-driven way to propose it. This is the model-driven probe: run in
    the executor thread against the snapshot, it returns a fileIssue WISH dict
    -- it NEVER files anything itself (file_issue mutates shared state and must
    only run inside the tick's single read-modify-write, in sim.py). The sim
    side resolves the owning team against LIVE state, dedups, and files.

    Cheap first: a deterministic problem-signal pre-filter on the finding
    text. No signal -> 'none' -> no Jev spend at all (most spikes are
    informational and should stay that way -- a spike worker who files on
    every investigation would be spamming the backlog). Only when a signal is
    present do we ask Jev for an explicit file/no-file verdict (file_bug /
    file_story / none)."""
    signals = _SPIKE_ISSUE_PROBLEM_SIGNALS
    if not (finding or '').strip():
        return None
    haystack = ' '.join((finding or '').split()).lower()
    if not any(sig in haystack for sig in signals):
        return None
    headline = next((line.strip() for line in (finding or '').splitlines() if line.strip()), None)
    if not headline:
        headline = (backlog or '').strip() or 'gap found during spike'  # pragma: no cover -- the empty-finding pre-filter above guarantees a non-blank first line
    prompt = (
        f'{name} just finished a time-boxed spike on "{backlog}" and reported this '
        f'finding:\n\n"{finding[:1500]}"\n\n'
        f'Does this finding identify at least one concrete, real, actionable gap '
        f'-- a bug, a missing capability, a broken integration, or a real '
        f'limitation worth filing as a backlog issue? Or is it just an '
        f'informational investigation into something that is actually fine?')
    decision = _serve._call_openrouter_decision_sync(
        _serve._jev_model(), {'messages': [], 'signals': {}},
        {'choice': {'type': 'choice', 'instructions': prompt,
                    'criteria': {
                        'file_bug': 'Yes -- it names a concrete defect or breakage that should be FIXED (something behaves wrongly or not at all).',
                        'file_story': 'Yes -- it names a real missing capability or gap worth BUILDING or adding (not a defect, but something that should exist).',
                        'none': 'No -- the finding is informational, says things are fine, or only surfaces expected limitations with nothing worth filing.'}}})
    choice, _, _ = _serve._jev_choice(decision)
    if choice not in ('file_bug', 'file_story'):
        return None
    feature = (task.get('projectLabel') or task.get('productId') or task.get('room') or 'pressoffice') or 'pressoffice'
    title = headline[:140]
    summary = f'{name} ({backlog[:120]}): {headline}'[:300]
    return {
        'issueType': 'bug' if choice == 'file_bug' else 'story',
        'summary': summary,
        'title': title,
        'feature': feature,
        'description': (finding or '')[:3000],
        'teamId': task.get('teamId'),
    }


def _run_spike_content(snapshot, agent_id, task, base_ctx=None):
    """Phase E2b: a SPIKE is a time-boxed investigation with no committed
    deliverable. Writes a concise findings artifact to the Library and stores
    a content result. A spike never opens a peer gate and never releases a
    product -- it answers a question, that's all.

    This used to be a single free-text /api/chat completion with
    NO tool access at all -- the model just guessed from training knowledge
    and called it "findings" (see the SECURITY_TEST_TOOLS comment in serve.py
    for the fabricated-report incident that exact pattern already caused
    once, for the security-test role). Then got real search_web/browse_page
    tool access via the shared _make_web_tools_executor -- fixed the
    fabrication, but a real, harder DreyX request ("list every source ever
    used, assess replicating each daily") showed the NEXT gap: a single
    non-reasoning model in one flat tool loop settles too early on genuinely
    open-ended, multi-step work, because it can't reliably judge "have I
    covered this exhaustively." Real, explicit fix, PLAN
    (reasoning tier, one call, a concrete checklist) -> EXECUTE (mid tier,
    the existing many-iteration tool loop, now following that checklist) ->
    SYNTHESIZE (reasoning tier, one call, given the FULL gathered transcript,
    write the complete report against the checklist). The reasoning tier's
    much higher per-token cost is paid twice per investigation this way, not
    once per tool call."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    name = _agent_name(snapshot, agent_id)
    backlog = task.get('title') or ''
    instructions = task.get('instructions')
    budget = task.get('budgetMs')
    # Item 4 gate up front: a spike that has already used its full model-spend
    # budget is failed closed BEFORE it resolves tiers or makes any call --
    # no further money is spent, and the sim sees budgetExhausted and runs its
    # notify + fail-closed branch. (The serve-side gate inside every /api/chat
    # and _plain_completion call backs this up; this check just fails fast and
    # stores the canonical result.)
    if _serve._task_budget_exhausted(task.get('id')):
        spent, attempts = _serve._task_budget_spent(task.get('id'))
        _sim_module._store_content_result(task.get('id'), {
            'ok': False,
            'budgetExhausted': True,
            'taskSpendUsd': spent,
            'taskSpendAttempts': attempts,
            'note': f'Spike "{backlog}" was paused before it started: this task already used its full model-spend budget (${spent:.4f} spent).',
            'notifyPlayer': {'kind': 'spike_done',
                             'subject': f'[AI Think Tank] Spike paused on budget: {backlog[:80]}',
                             'body': f'{name} tried to spike "{backlog}" but the task already used its full model-spend budget (${spent:.4f} spent, {attempts} model call(s)) -- no further calls were made.'},
        })
        return
    # A spike finishing used to generate no notice on either channel; the
    # think tank was reactive-only. notifyPlayer is the (safe, indirect --
    # _apply_content_result actually queues it) way any executor asks to be
    # notified on completion, success or failure alike, so silence never
    # reads as "still working" when it already gave up.
    tier_slug = _serve._resolve_model_tier(f'Run a time-boxed web investigation (spike): {backlog[:200]}')
    # The plan/synthesize bookends do the genuine reasoning of a spike. No
    # separate reasoning band needed: the JEV tier gate already
    # routes consequential work to mid/high, whose models are reasoning-
    # capable. Gate with allow_high so a hard investigation can spend up.
    reasoning_slug = _serve._resolve_model_tier(
        f'Plan and synthesize a time-boxed investigation, judging own completeness: {backlog[:200]}',
        allow_high=True)
    if not tier_slug:
        _sim_module._store_content_result(task.get('id'), {
            'note': f'Spiked "{backlog}", but no model tier is configured yet.', 'ok': False,
            'notifyPlayer': {'kind': 'spike_done',
                             'subject': f'[AI Think Tank] Spike stalled: {backlog[:80]}',
                             'body': f'{name} tried to spike "{backlog}" but no model tier is configured yet -- nothing was actually attempted.'},
        })
        return
    # Real multi-tool investigation (a browse_page render=true call launches
    # a real headless browser per fetch) can genuinely take longer than the
    # generic 120s content ceiling _dispatch_content_work installs -- widen
    # it here so a slow-but-real result isn't dropped by that timeout's
    # fallback-completion path before this thread finishes.
    task['workUntil'] = time.time() + 300

    # PLAN -- a short, concrete checklist of sub-goals, from the stronger
    # reasoning tier. Best-effort: an empty plan just means the execute step
    # falls back to its own generic instructions below, not a failure.
    plan_text = _plain_completion(reasoning_slug, [
        {'role': 'system', 'content': (
            'You are planning a real, tool-driven web investigation. Given the question below, '
            'write a short numbered checklist (3-7 items) of concrete sub-goals needed to answer it '
            'thoroughly and honestly -- e.g. which pages/categories to visit, what to extract from '
            'each, and what a real answer must cover. The FIRST item must always be a search_web '
            'query for what OTHER sites say about the subject -- a subject\'s own pages never '            'disclose everything about it (methodology, reputation, who else covers it), and a '
            'JS-heavy site may not even be readable by a plain fetch. If the subject has a large '
            'list of items (a directory, a catalog), include a step to sample a few individual item '
            'pages, not just the top-level listing.\n\n'
            'INTERNAL PRIOR ART -- if this question is about reviewing, comparing against, or '
            'building on work already done inside this think tank (another team\'s finished story, an '
            'earlier spike, prior research), the FIRST item must instead be a search_library query '
            '(not search_web) -- read what was actually done internally before looking externally, '
            'and follow a promising hit with read_library_file for the full content. If '
            'search_library finds nothing relevant, say so explicitly in the plan rather than '
            'assuming what the other team probably did.\n\n'
            'DELIVERABLE DECISION -- make this call explicitly: does answering this question well '
            'mean listing/comparing multiple items with attributes (sources, tools, options, prices, '
            'anything enumerable), or computing/transforming something from what gets found? If yes, '
            'the plan MUST include a MANDATORY step, worded exactly like this shape: "Use '
            'execute_script to write <filename>.csv with columns: <col1>, <col2>, ... -- one row per '
            '<item>." -- with REAL column names and a REAL filename you choose for this question, not '
            'a placeholder, and REAL columns for what the answer needs to be a real answer rather than '
            'a page description (e.g. a source/feasibility question needs columns like '
            'source_name, url, retrieval_method, api_or_rss_available -- not just source_name). Make '
            'this the LAST step, run only after the earlier research steps found real data to put in '
            'it. Say explicitly that describing the CSV in prose instead of actually building it does '
            'NOT satisfy this step. If the question is a simple yes/no or single-fact lookup with '
            'nothing to enumerate, skip this and say so -- do not force a CSV that doesn\'t fit.\n\n'
            'VERIFICATION HONESTY -- real gap caught live (2026-09-26): a CSV with a per-item judgment '
            'column (feasibility, difficulty, recommendation, anything requiring assessment, not just a '
            'fact copied from a page) looks equally authoritative whether each row was actually checked '
            'or just guessed from general knowledge -- there was no way to tell which from the output. '
            'If the CSV has a judgment column like this, the plan MUST require: (1) a status column '
            '(e.g. "basis") on EVERY row stating either "verified" (actually checked via a tool this '
            'run) or "estimated" (general knowledge, not directly checked this run) -- never leave this '
            'ambiguous; (2) an explicit step to ACTUALLY VERIFY a small sample (3-5 representative '
            'rows, or all of them if there are fewer than 6) via search_web/browse_page/execute_script '
            'before finalizing, so the CSV isn\'t 100% estimated -- pick rows that matter most to the '
            'question, not arbitrary ones. This does not apply to columns that are plain facts already '
            'confirmed during research (a name, a URL actually seen) -- only to judgment/assessment '
            'columns.\n\n'
            'This is a plan for another agent who will actually browse_page/search_web/execute_script '
            '-- do not answer the question yourself, do not invent facts, just plan the investigation.')},
        {'role': 'user', 'content': f'Question: {backlog}'
                                     f'{(" Method/constraints: " + instructions) if instructions else ""}'},
    ], max_tokens=500, task_id=task.get('id'))

    # EXECUTE -- the existing many-iteration real tool loop (mid tier: cheap
    # enough to spend on up to 18 round trips), now following the plan above
    # when one was produced.
    system = (
        f'You are {name}, running a time-boxed SPIKE (~{(budget or 60_000) / 1000:.0f}s). '
        f'Question: {backlog}.'
        f'{" Method/constraints: " + instructions if instructions else ""} '
        + (f'Follow this plan:\n{plan_text}\n\n' if plan_text else '')
        + 'This is REAL investigative work, not a guess from memory -- if the question depends on the '
        'actual current content of a specific website or any other live/current fact you do not '
        'already know for certain, you MUST use the search_web/browse_page tools to actually look it '
        'up before answering. If the question is about reviewing or building on work another team '
        'inside this think tank already did, use search_library/read_library_file to actually read '
        'their real findings first -- never describe another team\'s work from a guess; if '
        'search_library turns up nothing, say plainly that no internal record was found. Never '
        'invent specifics (numbers, names, sources, quotes) you did not '
        'actually get back from a tool -- if you can\'t find something real, say so instead of '
        'guessing. Many sites load their real content via JavaScript/AJAX -- if a plain browse_page '
        'fetch comes back as an empty shell or just navigation/boilerplate with no real content, '
        'retry the SAME url with render=true to get the real rendered page. If the question needs '
        'looking at multiple items (several posts/articles on a site, not just the front page), '
        'follow real links you found on the page and browse_page the individual pages too -- do not '
        'stop at the homepage. If a page lists a large catalog/directory of items, browse_page at '
        'least 2-3 of the individual item pages too, not just the top-level listing -- a category '
        'name is not the same as what that specific item actually is. Only ever follow a URL you '
        'actually saw returned by search_web or in a page\'s real links list -- never invent or guess '
        'a URL path. '
        + ("If a plan step or the question genuinely needs real computation this machine cannot do "
            "-- CUDA/GPU work, a fine-tuning experiment, a heavy numeric job -- use run_on_colab to "
            "run complete Python on a real Colab GPU and read its output, rather than skipping it or "
            "claiming the think tank can't do it. It is a metered cost against the think tank's Colab "
            "compute budget shown in the Bank; the Colab here is the FREE tier, so keep runs small "
            "-- a run must finish within its own timeout, an idle session gets recycled, and GPU "
            "slots are not guaranteed. IT RUNS AS THE PLAYER'S IDENTITY: you must NEVER use it for "
            "Google Drive, GCS/cloud APIs, credentials, mining/bulk media/torrents, or any data "
            "exfiltration site -- only raw compute. " if _serve.COLAB_CLI_AVAILABLE else "")
        + 'A spike never opens a peer-review gate, but it CAN still produce a real '
        'artifact -- if your plan has an execute_script/CSV step, that step is MANDATORY, not '
        'optional: you must actually call execute_script and write the real file. Describing what '
        'the CSV would contain, in prose, is NOT the same as building it and does NOT satisfy that '
        'plan item -- if you catch yourself writing "the CSV would include..." instead of actually '
        'writing csv rows to a real file, stop and go build it for real. Only build it from data you '
        'actually collected above -- if research fell short of what a row needs, use "unknown" for '
        'that cell rather than inventing a plausible-looking value. If your plan requires a "basis" '
        'column and a verification sample, that is ALSO mandatory: mark each row "verified" only if '
        'you actually checked it this run (a real search_web/browse_page/execute_script result), '
        '"estimated" otherwise -- do not mark a row "verified" just because you\'re confident about it '
        'from general knowledge. Work through every item in your '
        'plan before concluding -- if you genuinely cannot complete one, say so explicitly rather than '
        'skipping it silently. When you are done investigating, summarize what you actually found -- '
        'a later step will turn this into the final report, so completeness here matters more than '
        'polish. If you produced a file, cat its full contents out before you finish so the final '
        'report can include it verbatim.'
    )
    if _serve._google_is_configured():
        system += _GOOGLE_QUOTA_AWARENESS_BLOCK
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': 'Go ahead.'}]
    # One-strike-per-tool (ported from the user's own MAGI framework's
    # ReflectionEngine): a shared set across BOTH sub-executors, so a policy
    # denial on either browse_page or execute_script is remembered and the
    # SAME tool is refused (no network call at all) if the model tries it
    # again this investigation -- it can still try a genuinely different tool.
    struck_tools = set()
    web_tool = _serve._make_web_tools_executor(agent_id, key, default_query=backlog, struck_tools=struck_tools)
    sandbox_id = f"spike-{task.get('id') or 'adhoc'}"
    sandbox_tool = _make_spike_sandbox_executor(agent_id, key, sandbox_id, struck_tools=struck_tools)
    library_tool = _make_library_tools_executor(agent_id, struck_tools=struck_tools)
    treg_tool = _make_treg_tools_executor()
    pixellab_tool = _make_pixellab_tools_executor()
    google_tool = _make_google_tools_executor()
    github_tool = _make_github_tools_executor()
    apify_tool = _make_apify_tools_executor()
    youtube_tool = _make_youtube_transcript_executor(agent_id, key)
    # Colab agent compute (run_on_colab) -- offered only when the colab CLI
    # is actually on this machine; absent means the tool is simply absent.
    colab_compute_tool = None
    if _serve.COLAB_CLI_AVAILABLE:
        colab_compute_tool = _make_colab_compute_executor(agent_id, struck_tools=struck_tools)
    spike_tools = _serve.AGENT_ASK_TOOLS + [_SPIKE_SANDBOX_TOOL, _LIBRARY_SEARCH_TOOL, _LIBRARY_READ_TOOL,
                                            _TREG_X_TRENDING_TOOL, _TREG_LINKEDIN_SEARCH_TOOL,
                                            _PIXELLAB_CHARACTER_TOOL,
                                            _GOOGLE_SHEETS_READ_TOOL, _GOOGLE_SHEETS_APPEND_TOOL,
                                            _GOOGLE_CALENDAR_LIST_TOOL, _GOOGLE_CALENDAR_CREATE_TOOL,
                                            _GMAIL_SEARCH_TOOL, _GMAIL_READ_TOOL, _GMAIL_CREATE_DRAFT_TOOL,
                                            _GOOGLE_DOCS_READ_TOOL, _GOOGLE_DOCS_CREATE_TOOL]
    # YouTube transcripts: offered only when APIFY_API_KEY is set -- the route
    # ALWAYS downloads the audio via the Apify actor on the Colab runtime (no
    # captions fast-path), so unset = tool simply absent, same
    # conditional-availability rule as the Apify scraping tools below.
    if _serve.APIFY_API_KEY:
        spike_tools += [_YOUTUBE_TRANSCRIPT_TOOL]
    # GitHub read tools: offered only when GITHUB_TOKEN is set --
    # same conditional-availability rule as search_web (unset = tool simply
    # absent, so the surface never advertises something that would fail).
    if _serve.GITHUB_TOKEN:
        spike_tools += [_GITHUB_REPO_TOOL, _GITHUB_ISSUES_TOOL, _GITHUB_ISSUE_TOOL, _GITHUB_SEARCH_TOOL]
    # Apify scraping tools: offered only when APIFY_API_KEY is set --
    # same conditional-availability rule (unset = tool simply absent).
    if _serve.APIFY_API_KEY:
        spike_tools += [_APIFY_RUN_ACTOR_TOOL, _APIFY_GET_DATASET_ITEMS_TOOL]
    # Colab agent compute: offered only when the colab CLI is
    # actually installed on this machine -- same conditional-availability rule.
    if _serve.COLAB_CLI_AVAILABLE:
        spike_tools += [_COLAB_RUN_TOOL]
    _GOOGLE_TOOL_NAMES = ('read_google_sheet', 'append_google_sheet_row',
                          'list_calendar_events', 'create_calendar_event',
                          'search_gmail_messages', 'read_gmail_message', 'create_gmail_draft',
                          'read_google_doc', 'create_google_doc')

    def execute_tool(tool_name, args):
        if tool_name in struck_tools:
            return (f'{tool_name} was already blocked once this investigation (one-strike) -- do '
                     'not call it again, use a different tool or approach instead.')
        if tool_name == 'execute_script':
            return sandbox_tool(tool_name, args)
        if tool_name in ('search_library', 'read_library_file'):
            return library_tool(tool_name, args)
        if tool_name in ('x_trending_topics', 'search_linkedin_posts'):
            return treg_tool(tool_name, args)
        if tool_name == 'youtube_transcript':
            return youtube_tool(tool_name, args)
        if tool_name == 'generate_pixel_character':
            return pixellab_tool(tool_name, args)
        if tool_name in _GOOGLE_TOOL_NAMES:
            return google_tool(tool_name, args)
        if tool_name in _GITHUB_TOOL_NAMES:
            return github_tool(tool_name, args)
        if tool_name in _APIFY_TOOL_NAMES:
            return apify_tool(tool_name, args)
        if tool_name == 'run_on_colab' and colab_compute_tool is not None:
            return colab_compute_tool(tool_name, args)
        return web_tool(tool_name, args)

    # 18/900 (was 10/600): a "list every X across the whole site" question
    # (real request, "a full list of sources ever used on
    # DreyX") needs many more browse_page round trips than a single-fact
    # lookup. This turn's own text no longer has to BE the final report
    # (synthesize does that from the full transcript below), so its token
    # budget stays modest -- it only needs room for a working summary plus
    # each tool call's own arguments.
    # Forcing search_web specifically (not just force_first_tool=True's
    # "any tool") -- gap: force_first_tool=True alone still
    # ALWAYS reached for browse_page on the target's own pages and never
    # called search_web at all, missing facts that only live in OTHER sites'
    # coverage of the target (a manual search surfaced DreyX's named upstream
    # sources that 3 rounds of browsing dreyx.com itself never found).
    #
    # Same fix, second application: an internal-prior-art
    # question needs search_library forced first for the identical reason --
    # a PLAN-prompt instruction to "search the library first" was NOT
    # reliably followed in a real live run (see _spike_wants_internal_review's
    # own docstring for the exact incident). Checked before the search_web
    # default so an internal-review spike doesn't reach for the outside web
    # before it has even looked at what the think tank already knows.
    #
    # Same fix, third application: a real X-trending or
    # LinkedIn-search question needs its own specific tool forced first for
    # the identical reason -- built preemptively this time, on day one,
    # rather than after a live miss, now that the pattern is proven twice.
    if _spike_wants_internal_review(backlog, instructions):
        first_tool = 'search_library'
    elif _spike_wants_x_trending(backlog, instructions):
        first_tool = 'x_trending_topics'
    elif _spike_wants_linkedin_search(backlog, instructions):
        first_tool = 'search_linkedin_posts'
    elif _spike_wants_github(backlog, instructions):
        first_tool = 'github_get_repo'
    else:
        first_tool = 'search_web' if _serve.TAVILY_API_KEY else True
    # Chunked into rounds of 4 with a reflection self-check between them
    # (see _run_spike_tool_loop_with_reflection) -- same 50-call total
    # budget (raised 18->30->50, just spent with a chance to
    # course-correct partway through instead of only finding out it went
    # sideways at the end.
    try:
        execute_text, transcript = _run_spike_tool_loop_with_reflection(
            tier_slug, reasoning_slug, messages, spike_tools, execute_tool,
            total_iterations=50, max_tokens=900, force_first_tool=first_tool,
            task_id=task.get('id'),
            village_id=_sim_village_of_agent(snapshot, agent_id))
    except Exception as e:
        # A model-call failure (circuit-breaker RuntimeError, a 4xx, a network
        # blip) must never crash the spike executor or leave a half-baked
        # result -- record an honest not-ok like the empty-investigation path
        # below does, and bail. (without this, a tripped circuit
        # breaker propagated out of the tool loop and blew up the whole task.)
        _sim_module._store_content_result(task.get('id'), {
            'note': f'Spiked "{backlog}", but the investigation could not run (model call failed): {e}', 'ok': False,
            'notifyPlayer': {'kind': 'spike_done',
                             'subject': f'[AI Think Tank] Spike came up empty: {backlog[:80]}',
                             'body': f'{name} tried to spike "{backlog}" but the model call failed: {e}'},
        })
        return
    # Ground truth from the transcript itself (not a side-channel counter) --
    # true whenever execute_tool actually ran at least once.
    investigated = any(m.get('role') == 'tool' for m in (transcript or []))
    if not investigated:
        # force_first_tool=True should make this unreachable in practice --
        # kept as an explicit, honest failure rather than synthesizing a
        # report from a transcript that never actually investigated anything.
        _sim_module._store_content_result(task.get('id'), {
            'note': f'Spiked "{backlog}", but the model call returned nothing usable.', 'ok': False,
            'notifyPlayer': {'kind': 'spike_done',
                             'subject': f'[AI Think Tank] Spike came up empty: {backlog[:80]}',
                             'body': f'{name} looked into "{backlog}" but the model call returned nothing usable. You may want to ask again or rephrase it.'},
        })
        return

    # SYNTHESIZE -- the stronger reasoning tier, given the FULL transcript
    # (every real page fetched and every real search result), writes the
    # actual final report against the plan. Falls back to the execute step's
    # own closing text if synthesis itself comes back empty.
    synth_prompt = (
        'Based on everything above -- the plan, and everything actually found via the tools -- write '
        'the complete final findings report. If the question asked for a full/exhaustive/complete list '
        'of something, state every distinct item actually found, not a sample. Only report facts that '
        'were actually returned by a tool above; if something could not be determined, say so plainly '
        'rather than guessing. If different tool results above disagree on a fact (e.g. two different '
        'counts or conflicting claims), state the discrepancy explicitly instead of silently picking '
        'one. If any item from the plan was never actually completed, list it under a short "Not '
        'covered" section rather than omitting it. If an execute_script call above produced a real '
        'artifact (a CSV, a data file, a script) and its contents were cat\'d out, include the FULL '
        'contents verbatim in a fenced code block in your report, EXACTLY as produced (including any '
        '"basis"/verified-vs-estimated column) -- this is the actual deliverable, not something to '
        'summarize or clean up. If the CSV has a basis column, briefly note in your own summary how '
        'many rows were actually verified vs estimated, so that distinction isn\'t buried in a table '
        'nobody reads closely. Organize the answer clearly (a short list or numbered sections is '
        'fine). No filler, no release notes.'
    )
    finding = _plain_completion(
        reasoning_slug, transcript + [{'role': 'user', 'content': synth_prompt}], max_tokens=1800,
        task_id=task.get('id'))
    if not finding:
        finding = (execute_text or '').strip()
    if not finding:
        _sim_module._store_content_result(task.get('id'), {
            'note': f'Spiked "{backlog}", but the model call returned nothing usable.', 'ok': False,
            'notifyPlayer': {'kind': 'spike_done',
                             'subject': f'[AI Think Tank] Spike came up empty: {backlog[:80]}',
                             'body': f'{name} looked into "{backlog}" but the model call returned nothing usable. You may want to ask again or rephrase it.'},
        })
        return
    # Deterministic safety net, not a prompt: confirmed
    # that the model doesn't reliably follow through on the VERIFICATION
    # HONESTY plan requirement above even when the plan itself calls for
    # it -- a per-item judgment CSV can still come back with no basis
    # column, silently looking equally confident whether checked or
    # guessed. Rather than trying to block execute_script from writing a
    # non-compliant CSV (fragile -- too many ways to write a CSV to
    # reliably detect in a shell command), catch it AFTER the fact and
    # disclose it, so the player is never silently left without knowing.
    if _plan_requires_verification_basis(plan_text) and not _finding_shows_basis_column(finding):
        finding = (
            'NOTE (added automatically): this investigation\'s plan called for a "basis" column '
            '(verified vs estimated) on judgment/assessment data, but the report below does not '
            'appear to include one. Treat any feasibility/quality/recommendation-style assessments '
            'below as UNVERIFIED -- general knowledge, not independently checked this run -- unless '
            'stated otherwise.\n\n' + finding
        )
    # Second deterministic safety net: confirmed in the
    # SAME investigation -- a real file got cat'd into the transcript (a
    # genuine execute_script result, real data), but synthesis summarized it
    # in prose instead of including it verbatim, so the actual produced
    # artifact never reached the report at all. Append the raw output
    # whenever it exists and isn't already substantially present, so a real
    # produced file is never silently lost to a lossy summary.
    for output in _extract_execute_script_outputs(transcript):
        if len(output) > 40 and output[:80] not in finding:
            finding += (f'\n\n---\n\nRaw output from execute_script (added automatically -- not '
                       f'already included in the report above):\n\n```\n{output[:4000]}\n```')
    # W3: a spike worker who spots a real gap now has a model-driven path to
    # propose it. This is only a WISH -- the executor thread runs against a
    # snapshot and must never mutate shared state, so _spike_file_issue_wish
    # returns a fileIssue wish dict (or None) and the sim side (inside the
    # tick's single read-modify-write) does the real filing. Most spikes are
    # informational, so the deterministic pre-filter + Jev verdict means no
    # wish (and no Jev spend) for the common "investigated, all fine" case.
    file_issue_wish = None
    try:
        file_issue_wish = _spike_file_issue_wish(finding, task, name, backlog)
    except Exception:
        file_issue_wish = None
    library_path = f"archive/{int(time.time() * 1000)}-spike-{task.get('id') or 'adhoc'}.md"
    _serve._http_json('POST', base, '/api/library/file',
               {'agentId': agent_id,
                'path': library_path,
                'content': f'# Spike: {backlog}\n\nBy: {name}\n\n{finding}\n',
                'source': 'firsthand'}, key)
    # Data-minimization audit finding (Hermes Town comparison):
    # this was the ONE content executor (of 13) that duplicated the FULL
    # finding text into the task note -- which _apply_content_result (sim.py)
    # copies verbatim onto the durable task record AND the agent's own
    # profile.notes, so the same content ended up stored three times (the
    # library file above, task.note in the hot kv_state blob serialized on
    # every save, and the agent's profile). Every other executor already uses
    # a short summary here; the full finding's one real home is the library
    # file just written. The notification email/Telegram push below is a
    # THIRD, deliberate exception to that same rule -- the player asking to
    # be told what was found is a genuinely different need than the durable
    # task record staying lean, not a regression of the same bug.
    _sim_module._store_content_result(task.get('id'), {
        'note': f'{name} spiked "{backlog}" -- see the Library entry just filed.', 'ok': True,
        # Gap: /api/intent/spike/{id}/promote
        # (turning a spike into a real followup story) only ever had the
        # short note above to work with -- recording the exact path lets it
        # pull the REAL findings (source lists, CSVs, feasibility data)
        # forward instead of a vague pointer.
        'libraryPath': library_path,
        'fileIssue': file_issue_wish,
        'notifyPlayer': {'kind': 'spike_done',
                         'subject': f'[AI Think Tank] Spike done: {backlog[:80]}',
                         # 3600 (was 1500): a real list-style answer (e.g. "every
                         # source used") needs more room than a 2-4 sentence
                         # summary; kept under Telegram's 4096-char hard message
                         # cap once the subject/prefix overhead is added.
                         'body': f'{name} looked into "{backlog}":\n\n{finding[:3600]}'},
    })


def _run_workroom_content(snapshot, agent_id, task, base_ctx=None):
    """Port of tasks.js runWorkroomTask: the Press Office's dispatcher. A real
    review/QA taskType goes to the review executor; a projectLabel (real project)
    goes to the coding executor (with a real listing of the sandbox's current
    files as context instead of a guessed one); a productId routes to the Phase E
    product build/release executor; a bare ambient task falls back to the fixed
    shared-tooling health-check."""
    if task.get('productId'):
        _run_product_build_content(snapshot, agent_id, task, base_ctx)
        return
    project_label = task.get('projectLabel')
    if project_label and (task.get('taskType') == 'review' or task.get('taskType') == 'qa'):
        _run_review_content(snapshot, agent_id, task, base_ctx)
        return
    if project_label:
        backlog = f"{task.get('title') or ''} -- {task.get('instructions')}" if task.get('instructions') else (task.get('title') or '')
        context_summary = ''
        ls = _serve._http_json('POST', _serve.SELF_BASE_URL, '/api/execute',
                        {'agentId': agent_id, 'sandboxId': WORKROOM_SANDBOX_ID, 'command': 'ls -la'},
                        _serve.get_or_create_agent_key(agent_id))
        if isinstance(ls, dict) and ls.get('allowed') and ls.get('stdout'):
            context_summary = f'Current files in the shared Work Room sandbox:\n{ls["stdout"]}'
        # Item 1: the sim's perception (base_ctx) must survive this path -- the
        # file listing is extra context, not a replacement for who/where/why.
        if base_ctx:
            context_summary = '\n\n'.join(p for p in (base_ctx, context_summary) if p)
        # Read-before-act: the folded design taste doc for this project's
        # design work, so a labeled build reasons over the player's taste,
        # scoped to the agent's village.
        design_ctx = _design_context_for_task(snapshot, task, agent_id)
        if design_ctx:
            context_summary = f'{design_ctx}\n\n{context_summary}'
        _run_coding_content(snapshot, agent_id,
                            {**task, 'projectLabel': project_label,
                             'title': backlog or task.get('title', ''),
                             'instructions': None}, base_ctx=context_summary)
        return
    # Bare ambient task -> the fixed shared-tooling health check.
    import sim as _sim_module
    r = _serve._http_json('POST', _serve.SELF_BASE_URL, '/api/pipeline',
                   {'agentId': agent_id, 'sandboxId': WORKROOM_SANDBOX_ID,
                    'steps': [
                        {'name': 'update helper script', 'command': 'echo \'print("helper script checked and working")\' > helper.py'},
                        {'name': 'run it', 'command': 'python3 helper.py'}]},
                   _serve.get_or_create_agent_key(agent_id))
    note = (f'Worked in the shared sandbox -- hit a failure at "{r.get("failedStep")}".'
            if isinstance(r, dict) and r.get('failedStep')
            else 'Checked and ran the shared tooling in the Work Room -- all good.')
    _sim_module._store_content_result(task.get('id'), {'note': note})
