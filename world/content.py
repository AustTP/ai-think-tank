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
            continue
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
        tier_slug = _serve._mid_tier_slug()
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
            try:
                r = _serve._http_json('POST', base, '/api/chat',
                               {'model': tier_slug,
                                'messages': [{'role': 'system', 'content': sys_msg},
                                             {'role': 'user', 'content': f'Freshly collected sources:\n\n{sources_text}'}],
                                'max_tokens': _serve.RESEARCH_SKILL_SYNTHESIS_TOKENS,
                                'agentId': agent_id}, key)
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
_WEATHER_REFERENCE_URL = 'https://en.wikipedia.org/wiki/Weather_forecasting'
_MEDIA_FEEDS_PATH = 'media/feeds.md'
_MEDIA_DIGEST_TOKENS = 200            # runMediaDigestTask /api/chat max_tokens
_SKILL_REVIEW_MAX_PER_SWEEP = 5       # tasks.js SKILL_REVIEW_MAX_PER_SWEEP
RENDER_FALLBACK_THRESHOLD_CHARS = 300  # world.js RENDER_FALLBACK_THRESHOLD_CHARS

# Hive-mind distillation limits. Bounded reads keep the synthesis call from
# being flooded: at most the newest archives since the last run, each excerpt
# capped, plus the current village wiki so the merge is incremental.
_DISTILL_MAX_ARCHIVES = 25
_DISTILL_ARCHIVE_EXCERPT_CHARS = 4000
_DISTILL_WIKI_EXCERPT_CHARS = 6000
_DISTILL_VILLAGE_PAGE_ID = 'state-of-knowledge'
_DISTILL_CSV_MIN_LINES = 3


def _extract_csv_like_blocks(text):
    """Best-effort detection of real CSV/tabular data inside an archive
    file's FULL body (not the truncated excerpt the synthesis prompt sees --
    this must catch data the model never even saw, not just data it saw and
    paraphrased away). Pure heuristic -- a fenced code block, or a run of
    3+ consecutive comma-bearing lines -- same 'don't reach for a heavier
    tool than the problem needs' reasoning as the Library's own plain
    substring search.

    Deterministic safety net (2026-09-26): the distill synthesis prompt
    explicitly tells the model to 'extract what the village NOW knows...
    do not just re-print the raw archive files' -- exactly the summarize-
    don't-preserve instruction that already, twice tonight, caused a
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
    # Real gap flagged (2026-09-26): the original check (>=1 comma, no
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
    """Port of tasks.js checkWeatherReference: browse one FIXED well-known
    reference page through the full /api/browse gate and note the headline.
    The Weather Station's whole identity is one external reference, so this is
    deliberately not content-aware -- same as the browser's version."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    purpose = 'Checking outside weather reference material for the village weather station.'
    data = _serve._http_json('POST', base, '/api/browse',
                      {'url': _WEATHER_REFERENCE_URL, 'agentId': agent_id, 'purpose': purpose}, key)
    if data.get('error'):
        note = 'Tried to check an outside reference, but the request failed.'
    elif data.get('allowed') and data.get('text'):
        note = f'Checked outside weather reference -- noted: "{data["text"][:140].strip()}..."'
    elif data.get('allowed'):
        note = 'Checked outside weather reference, but the page came back empty.'
    else:
        note = f"Tried to check an outside reference, but it wasn't approved: {data.get('reason') or 'no reason given'}"
    _sim_module._store_content_result(task.get('id'), {'note': note})


def _parse_feed_urls_after(text):
    return [l for l in (text or '').split('\n') if l.strip() and not l.strip().startswith('#')]


def _file_digest_url(url):
    return url.replace('https://', '').replace('http://', '').replace(' ', '-').strip('-')


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
    model_slug = _serve._mid_tier_slug()
    summary = None
    if model_slug:
        r = _serve._http_json('POST', base, '/api/chat',
                       {'model': model_slug,
                        'messages': [
                            {'role': 'system', 'content': 'Summarize the following page in 2-3 short, honest sentences for someone who has not read it. Only report what is actually in the text -- do not invent detail.'},
                            {'role': 'user', 'content': text[:6000]}],
                        'max_tokens': _MEDIA_DIGEST_TOKENS, 'agentId': agent_id}, key)
        if isinstance(r, dict) and not r.get('error') and r.get('reply'):
            summary = r['reply'].strip()
    if not summary:
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
                'typesafe/jev-1.13', {'messages': [], 'signals': {}},
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
    last distillation, LLM-synthesize them into an updated village wiki page,
    and write it back as server authority. This is what makes the village a
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

    # 2. Pull the current village wiki page so the merge is incremental, not a
    # from-scratch rewrite (what the village already "knows" is preserved).
    current_wiki = ''
    village_wiki_path = os.path.join(_serve.LIBRARY_DIR, 'wiki', 'village',
                                     f'{_DISTILL_VILLAGE_PAGE_ID}.md')
    try:
        with open(village_wiki_path, 'r', errors='replace') as f:
            current_wiki = f.read()
    except OSError:
        current_wiki = ''

    # Nothing new since the last distillation -> don't churn the wiki.
    if not archives:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': 'Distillation ran -- no new archive findings since the last pass, so the village knowledge is unchanged.', 'noop': True})
        return

    tier_slug = _serve._mid_tier_slug()
    if not tier_slug:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Found {len(archives)} new finding(s) to distill, but the synthesis call failed (no model tier).',
                                           'noop': True})
        return

    # 3. Synthesis call -- same shape as the research executor's /api/chat.
    sources_text = '\n\n'.join(
        f'### {fn}\n{body[:_DISTILL_ARCHIVE_EXCERPT_CHARS]}' for _t, fn, body in archives)
    sys_msg = (
        'You are the distillation step of a village-wide knowledge base. Merge the '
        'findings below -- and the current village knowledge, when provided -- into one '
        f'updated page ({_DISTILL_VILLAGE_PAGE_ID}). Rules:\n'
        '- Remove redundancy and contradiction; where findings disagree, keep the view '
        'with more supporting evidence and mark residual disagreement honestly.\n'
        '- Extract what the village NOW knows as a body; do not just re-print the raw '
        'archive files.\n'
        '- Preserve citations to the source files you folded in.\n'
        '- If a current page is given, keep what still holds and fold in what is new.\n'
        'Write concrete, actionable markdown, not filler.' +
        (f'\n\nCURRENT VILLAGE KNOWLEDGE (keep/merge):\n\n{current_wiki[:_DISTILL_WIKI_EXCERPT_CHARS]}'
         if current_wiki else '\n\nNo current village page yet -- write one from scratch.'))
    reply = None
    try:
        r = _serve._http_json('POST', base, '/api/chat',
                              {'model': tier_slug,
                               'messages': [{'role': 'system', 'content': sys_msg},
                                            {'role': 'user', 'content': f'Recent village findings:\n\n{sources_text}'}],
                               'max_tokens': _serve.RESEARCH_SKILL_SYNTHESIS_TOKENS,
                               'agentId': agent_id}, key)
        if isinstance(r, dict) and not r.get('error') and r.get('reply'):
            reply = r['reply'].strip()
    except Exception:
        reply = None
    if not reply:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Distilled {len(archives)} finding(s), but the synthesis call returned nothing usable.', 'noop': True})
        return

    # 4. Deterministic safety net (2026-09-26): the synthesis prompt above
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
        _DISTILL_VILLAGE_PAGE_ID, 'Village state of knowledge', 'village', reply)
    if record is None:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Distilled {len(archives)} finding(s), but the wiki write failed.', 'noop': True})
        return
    _sim_module._store_content_result(task.get('id'),
                                      {'note': f'Distilled {len(archives)} recent finding(s) into the village wiki ({record.get("title")}, v{record.get("version")}).',
                                       'distilled': len(archives), 'wikiPage': record.get('id')})


def _run_observatory_content(snapshot, agent_id, task, base_ctx=None):
    """Observatory's own executor choice branches on which flag the task
    carries, not a fixed 1:1 key like every other room -- pulled out of the
    dispatcher (2026-09-25, registry conversion) so it reads and tests the
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

    Registry conversion (2026-09-25): was a single if/elif chain: taskType
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
    if executor:
        executor(snapshot, agent_id, task, base_ctx)


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
    per your call (2026-09-24): the village should have hive-mind awareness of
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
            note = (f"{name} checked the bank: the village has used ${total_used:.2f} of "
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
        forecast = f", {row['daysLeft']:.1f} days until cap at current burn" if row['daysLeft'] is not None else ""
        lines.append(f"- {svc}: ${row['used']:.2f} / ${row['cap']:.2f} cap ({row['left']:.2f} left, {status}, {row['calls']} calls, {row['burnPerDay']:.2f}/day{forecast})")
    body = '; '.join(lines)
    over_any = any(v['over'] for v in view.values())
    note = (f"{name} (director) reviewed the bank. Cumulative: ${total_used:.2f} used of ${total_cap:.2f} "
            f"across {len(view)} services. Per service: {body}. "
            + ("WARNING: at least one service is over its cap - directors should re-allocate or raise a cap."
               if over_any else
               "All services are within cap; directors can coordinate to keep it that way."))
    # Per your call (2026-09-24): reconcile against the REAL OpenRouter account
    # balance too, not just the village's own internal ledger -- the two can
    # legitimately diverge (outside usage on the same key, manual top-ups).
    credits = _serve._openrouter_account_credits()
    if credits:
        note += (f" OpenRouter account (real): ${credits['totalCredits']:.2f} total credits, "
                 f"${credits['totalUsage']:.2f} used lifetime, ${credits['remaining']:.2f} remaining.")
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
    tier_slug = _serve._mid_tier_slug()
    finding = None
    if tier_slug:
        r = _serve._http_json('POST', base, '/api/chat',
                       {'model': tier_slug,
                        'messages': [
                            {'role': 'system', 'content': f'You are researching for a real project: {project}. Your specific task right now: {backlog}. Write 2-4 honest, concrete sentences of real findings or analysis -- no filler, an actual answer or set of concrete points.'},
                            {'role': 'user', 'content': 'Go ahead.'}],
                        'max_tokens': 400, 'agentId': agent_id,
                        'service': project or task.get('productId') or '__general__'}, key)
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
# tasks.js versions were written specifically for the village's own shared
# sandbox (WORKROOM_SANDBOX_ID), so these are a faithful pipeline: real
# /api/execute calls (probe-before-writing, heredoc-continuation, orphaned and
# phantom script detection) and /api/chat for the code itself.
WORKROOM_SANDBOX_ID = 'workroom-shared'      # index.html WORKROOM_SANDBOX_ID
CODE_CONTINUATION_ATTEMPTS = 2               # tasks.js
MAX_CODE_PROBE_ROUNDS = 3                    # tasks.js
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


def _review_screenshot(base, key, agent_id, sandbox_id, path, question):
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
                    'max_tokens': _VISION_MAX_TOKENS, 'agentId': agent_id}, key)
    if isinstance(r, dict) and not r.get('error') and r.get('reply'):
        return {'ok': True, 'review': r['reply'].strip()}
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


def _auto_link_js_files(base, key, agent_id, sandbox_id, unlinked_files, messages, tier_slug):
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
                        'max_tokens': 400, 'agentId': agent_id}, key)
        if not isinstance(r, dict) or r.get('error') or not r.get('reply'):
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
                     'When ready to write the actual fix, respond with ONLY a single shell command, no explanation, no markdown fences, '
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
    while attempts <= CODE_CONTINUATION_ATTEMPTS:
        reply = None
        r = _serve._http_json('POST', base, '/api/chat',
                       {'model': tier_slug, 'messages': messages,
                        'max_tokens': _CODE_MAX_TOKENS, 'agentId': agent_id,
                        'service': project_label or task.get('productId') or '__general__'}, key)
        if not isinstance(r, dict) or r.get('error') or not r.get('reply'):
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
        if attempts > CODE_CONTINUATION_ATTEMPTS:
            _sim_module._store_content_result(task.get('id'),
                                              {'note': f'generation still looked truncated after {CODE_CONTINUATION_ATTEMPTS} '
                                                       f'continuation attempt(s) ({_opens} heredoc(s) opened, {_closes} closed) -- not executed',
                                               'ok': False, 'tier': tier_slug})
            return False
        messages.append({'role': 'assistant', 'content': command})
        messages.append({'role': 'user',
                         'content': 'You were cut off before finishing. Continue EXACTLY where you left off -- do not repeat anything you '
                                    'already wrote, do not restart the heredoc or add a new one, just output the rest of the raw file content '
                                    'and the closing EOF line(s).'})

    exec_data = _serve._http_json('POST', base, '/api/execute',
                           {'agentId': agent_id, 'command': command,
                            'purpose': f'Coding task: {backlog_item}', 'sandboxId': WORKROOM_SANDBOX_ID}, key)
    if not isinstance(exec_data, dict) or not exec_data.get('allowed'):
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f"blocked: {exec_data.get('reason') if isinstance(exec_data, dict) else 'no response'}",
                                           'command': command, 'ok': False, 'tier': tier_slug})
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
            linked = _auto_link_js_files(base, key, agent_id, WORKROOM_SANDBOX_ID, unlinked, messages, tier_slug)
            if linked['ok']:
                result['note'] = f'auto-linked previously-orphaned file(s) into index.html: {", ".join(unlinked)}'
            else:
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

    tier_slug = _serve._mid_tier_slug()
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
                     f'\n\n{pipeline_clause}' +
                     ' Before answering, you may check real facts about how the ACTUAL running page behaves right now -- what a button '
                     'click or keypress actually does -- instead of guessing from the source alone. To do this, respond with ONLY a JSON '
                     'object, no markdown fences, no explanation, in exactly this shape: '
                     '{"probeRequest": {"path": "index.html", "actions": [{"type":"click","selector":"text=Some Button"}, '
                     '{"type":"keydown","key":"a"}], "probes": ["document.body.className", "typeof window.SomeGlobal"]}}\n'
                     'Action types are click ({selector}), keydown ({key}), wait ({ms}), eval ({code}). '
                     f'You can do this up to {MAX_REVIEW_PROBE_ROUNDS} times if you genuinely need to. '
                     'When ready, respond with your final assessment as plain text, not JSON.')
    messages = [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': 'Give your assessment.'}]
    review = None
    probe_rounds = 0
    while True:
        r = _serve._http_json('POST', base, '/api/chat',
                       {'model': tier_slug, 'messages': messages,
                        'max_tokens': _REVIEW_MAX_TOKENS, 'agentId': agent_id,
                        'service': project_label or task.get('productId') or '__general__'}, key)
        if not isinstance(r, dict) or r.get('error') or not r.get('reply'):
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
                feedback += f'\n\nYou have used all {MAX_REVIEW_PROBE_ROUNDS} probe rounds. Respond now with your final assessment as plain text.'
            messages.append({'role': 'user', 'content': feedback})
            continue
        review = reply
        break
    if not review:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Tried to {("QA-test" if is_qa else "review")} "{backlog}", but the model call didn\'t produce anything usable.',
                                           'ok': False})
        return

    visual = _review_screenshot(base, key, agent_id, WORKROOM_SANDBOX_ID, 'index.html',
                                f'You\'re reviewing a real screenshot of this project\'s main page. Task: {backlog_item}. '
                                'Describe what you actually see, and call out anything that looks visually broken -- elements in the '
                                'wrong place, overlapping, cut off, or missing.')
    full_review = (f'{review}\n\n## Visual check (real screenshot)\n\n{visual["review"]}' if visual['ok']
                   else f'{review}\n\n## Visual check\n\n(could not complete: {visual["note"]})')

    # File the review into the Library (fire-and-forget, never blocks).
    _serve._http_json('POST', base, '/api/library/file',
               {'agentId': agent_id,
                'path': f"archive/{int(time.time() * 1000)}-{task.get('taskType') or 'review'}-{task.get('id') or 'adhoc'}.md",
                'content': f'# {backlog}\n\nProject: {project_label}\nBy: {name} ({kind})\n\n{full_review}\n',
                'source': 'firsthand'}, key)

    # Jev verdict: actionable -> queue a follow-up fix; clean -> nothing to do.
    verdict = 'clean'
    try:
        decision = _serve._call_openrouter_decision_sync(
            'typesafe/jev-1.13', {'messages': [], 'signals': {}},
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

    # Cut 4 hard gate: a red quality pipeline can NEVER read as approval. Force
    # the vote to actionable so the story goes back for a fix even if the review
    # text or the Jev call brushed the pipeline failure aside.
    if not qp['ok']:
        verdict = 'actionable'

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
                     # Real bug caught live (2026-09-24): this never carried
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
        suffix = 'queued a fix'
    result = {'note': f'Filed a {kind} on "{backlog}" (text + visual), {suffix}',
              'ok': True,
              'pipelineOk': qp['ok'],
              'pipelineSummary': qp['note']}
    if is_gate_review:
        # Relay the vote so _apply_content_result can count it on the parent.
        result['peerVerdict'] = verdict
    if queue_fix:
        result['queueFix'] = queue_fix
    _sim_module._store_content_result(task.get('id'), result)


def _wiki_context_for_task(state, task):
    """Read-before-act: fold the wiki pages relevant to a task into a context
    block (markdown) the executor prepends to its model prompt. Empty string
    when the wiki has nothing for this task's room -- the executor still runs,
    just with a blank knowledge context (the pre-existing behavior)."""
    try:
        import sim as _sim
        return _sim.inject_wiki_context(state, task)
    except Exception:
        return ''


def _run_product_build_content(snapshot, agent_id, task, base_ctx=None):
    """Phase E: a pressoffice task targeting a PRODUCT (task['productId']).
    Reads the product's catalog record + sandbox, injects the wiki context so
    the build reasons over village knowledge first, runs the real coding
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
    # Read-before-act: village knowledge for this task's room.
    wiki_ctx = _wiki_context_for_task(snapshot, task)
    if wiki_ctx:
        context_summary = f'{wiki_ctx}\n\n{context_summary}'
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


def _plain_completion(model, messages, max_tokens, service='spike'):
    """One non-tool completion call, with the same spend accrual _http_json
    self-loopback calls get from serve.py's own endpoints -- used for the
    plan/synthesize bookends of a spike, which need real (possibly extended-
    reasoning) deliberation but no tool access of their own. Returns stripped
    text, or '' on any failure (never raises -- a spike must always still
    complete via SOME path, per the notifyPlayer-on-every-outcome rule)."""
    try:
        data = _serve._call_openrouter_sync(model, messages, max_tokens)
    except Exception as e:
        print(f'[spike] plain completion failed: {e}', flush=True)
        return ''
    cost = (data.get('usage') or {}).get('cost', 0.0)
    if isinstance(cost, (int, float)) and cost:
        _serve._accrue_spend(service, cost)
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


def _plan_requires_verification_basis(plan_text):
    """Best-effort detection of whether the PLAN decided a basis (verified/
    estimated) column was needed for a judgment CSV -- used as the trigger
    for the post-hoc disclaimer below, since the plan saying the right thing
    is not the same as the final report actually containing it (real gap
    caught live, 2026-09-26)."""
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
    gap caught LIVE (2026-09-26) -- a real spike asked to "review the
    village's own prior research on DreyX.com" went straight to browse_page
    and reported "no existing records" despite 7+ real matching entries
    already in the Library, because the PLAN prompt's own "search_library
    first" instruction was never reliably followed. This is the exact same
    failure class already fixed once tonight for search_web (force_first_
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


def _spike_wants_linkedin_jobs(backlog, instructions):
    """More specific than _spike_wants_linkedin_search -- must be checked
    BEFORE it in any first-tool chain (a job search also mentions
    'linkedin', so the generic post-search check would otherwise win first
    and force the wrong tool)."""
    text = f'{backlog or ""} {instructions or ""}'.lower()
    return 'linkedin' in text and ('job' in text or 'hiring' in text)


def _extract_execute_script_outputs(transcript):
    """Pull the stdout of every execute_script tool call out of a real
    _call_agent_tool_loop transcript (matching each tool_call_id to its
    result). Used as a deterministic safety net (2026-09-26): confirmed
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
                if stdout:
                    outputs.append(stdout)
    return outputs


# Reflection/replan (2026-09-26), ported (design, not code) from the user's
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
                                         total_iterations, max_tokens, force_first_tool):
    remaining = total_iterations
    current_messages = list(messages)
    first_round = True
    execute_text = None
    while remaining > 0:
        this_round = min(_REFLECTION_CHUNK_SIZE, remaining)
        before_len = len(current_messages)
        execute_text, current_messages = _serve._call_agent_tool_loop(
            tier_slug, current_messages, tools, execute_tool,
            max_iterations=this_round, max_tokens=max_tokens, service='spike',
            force_first_tool=(force_first_tool if first_round else False), return_transcript=True)
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
                'differently, or \'on track\' if the current approach is working>"}.')},
        ], max_tokens=150)
        confidence, note = _parse_reflection(reflection)
        if confidence < _REFLECTION_CONFIDENCE_FLOOR and note and note.strip().lower() != 'on track':
            current_messages.append({'role': 'user', 'content': f'Self-check before continuing: {note}'})
    return execute_text, current_messages


# Real request (2026-09-26): "no committed deliverable" (a spike's actual
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
        return '\n\n'.join(parts)
    return execute_tool


# Real request (2026-09-26): the inverse of promote-spike's fix (a spike's
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
            "Search the village's own Library -- real completed work from ANY team (finished "
            "stories, past spikes, research, peer reviews, wiki merges). Use this BEFORE "
            "search_web when the investigation is about reviewing, comparing against, or building "
            "on something already done inside this village (e.g. \"review Team A's approach before "
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
    already applies to everyone (the village is transparent about who did
    what). Self-checks `struck_tools` (defense in depth, same shape as the
    other two executors) even though a read-only lookup failing is unlikely
    to ever be a POLICY denial rather than "no matches" -- kept consistent
    so a future caller that adds a gate here for free gets one-strike too.

    Logs via log_action (2026-09-26, added after a live burn-in): these two
    tools are in-process (no /api/browse-style HTTP hop), so unlike every
    other tool they had ZERO observability -- when a real live run's report
    claimed "no existing records" despite matching entries already in the
    Library, there was no way to directly confirm whether search_library
    was ever actually called at all versus just never finding a match. This
    closes that gap for good, not just for tonight's one-off check."""
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
            # Real gap caught live (2026-09-26): the top-ranked match isn't
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
            # Trail reinforcement (2026-09-26): a real, actual read of this
            # finding by another team's investigation is exactly the signal
            # that should make it rank higher in future search_library calls.
            _serve.record_library_read(path)
            _serve.log_action(agent_id, 'read_library_file',
                              {'path': path, 'found': True}, authorized=True)
            return content_text
        raise ValueError(f'unknown tool: {name}')
    return execute_tool


# Real, on-demand social/trend monitoring (2026-09-26), per your explicit
# request. Deliberately on-demand only, not a recurring cadence -- real
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
            "X API proxy (a real ~$0.01 call against the village's Treg balance). Use this when "
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
            "proxy (a real ~$0.002 call against the village's Treg balance). Use this for a real, "
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

# Real LinkedIn job search + post-engagement endpoints (2026-09-27), per your
# explicit request to expand beyond post-search. Treg's catalog page
# publishes only human-readable family names (e.g. "Search job postings by
# keyword"), never the actual callable endpoint id or its param schema --
# these THREE ids and shapes were discovered live via Treg's own
# self-documenting error text (404 "did you mean X", 400 "valid fields:
# [...]"), the same method already used for the two tools above, not
# guessed from docs. Two families the same request asked about --
# `linkedin.post.reposts` and the comment-level `linkedin.comment.reactions`
# / `linkedin.comment.replies` -- came back a confirmed "no endpoint in the
# catalog" for every provider tried (apify/anyapi/adyntel); they are not
# available today, not just unwired.
_TREG_LINKEDIN_JOB_SEARCH_TOOL = {
    'type': 'function',
    'function': {
        'name': 'search_linkedin_jobs',
        'description': (
            "Search real, current LinkedIn job postings by title/keyword, via Treg's real "
            "Apify-backed LinkedIn jobs API (a real ~$0.011 call against the village's Treg "
            "balance -- more expensive than the other Treg tools, only call this once per "
            "distinct search). Returns real postings (title, company, location, full "
            "description, apply link) -- never invent a plausible-sounding job listing."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'job_title': {'type': 'string', 'description': 'The job title or keyword to search for, e.g. "backend engineer".'},
            },
            'required': ['job_title'],
        },
    },
}

_TREG_LINKEDIN_POST_COMMENTS_TOOL = {
    'type': 'function',
    'function': {
        'name': 'get_linkedin_post_comments',
        'description': (
            "Get real comments on a specific LinkedIn post, via Treg's real LinkedIn API proxy "
            "(a real ~$0.005 call against the village's Treg balance). Requires a real, specific "
            "LinkedIn post URL -- e.g. one already found via search_linkedin_posts. Never invent "
            "a plausible-sounding comment."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'post_url': {'type': 'string', 'description': 'A real, specific LinkedIn post URL, e.g. one returned by search_linkedin_posts.'},
            },
            'required': ['post_url'],
        },
    },
}

_TREG_LINKEDIN_POST_REACTIONS_TOOL = {
    'type': 'function',
    'function': {
        'name': 'get_linkedin_post_reactions',
        'description': (
            "Get real reactions (likes, celebrates, etc.) on a specific LinkedIn post, via Treg's "
            "real LinkedIn API proxy (a real ~$0.005 call against the village's Treg balance). "
            "Requires a real, specific LinkedIn post URL -- e.g. one already found via "
            "search_linkedin_posts. Never invent a plausible-sounding reaction count."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'post_url': {'type': 'string', 'description': 'A real, specific LinkedIn post URL, e.g. one returned by search_linkedin_posts.'},
            },
            'required': ['post_url'],
        },
    },
}


def _make_treg_tools_executor():
    """x_trending_topics / search_linkedin_posts / search_linkedin_jobs /
    get_linkedin_post_comments / get_linkedin_post_reactions -- thin
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
            return json.dumps(data)[:4000]
        if name == 'search_linkedin_posts':
            query = (args.get('query') or '').strip()
            if not query:
                return 'query is required'
            params = {'query': query, 'date_posted': args.get('date_posted') or 'last-week'}
            data, error = _serve._treg_call('scrapecreators.x.v1-linkedin-search-posts', params, method='GET')
            if error:
                return f'Could not search LinkedIn posts: {error}'
            _serve._accrue_spend('treg', _serve.TREG_ENDPOINT_COSTS['scrapecreators.x.v1-linkedin-search-posts'])
            return json.dumps(data)[:4000]
        if name == 'search_linkedin_jobs':
            job_title = (args.get('job_title') or '').strip()
            if not job_title:
                return 'job_title is required'
            # maxTotalChargeUsd/timeout are real Apify-platform-level query
            # params Treg requires on top of the JSON body (confirmed live,
            # see _treg_call's `query` docstring) -- not agent-controllable
            # knobs, a fixed operational cap on this specific call.
            data, error = _serve._treg_call(
                'apify.linkedin.search.jobs', {'jobTitles': [job_title]}, method='POST',
                query={'maxTotalChargeUsd': 0.5, 'timeout': 60})
            if error:
                return f'Could not search LinkedIn jobs: {error}'
            _serve._accrue_spend('treg', _serve.TREG_ENDPOINT_COSTS['apify.linkedin.search.jobs'])
            return json.dumps(data)[:4000]
        if name in ('get_linkedin_post_comments', 'get_linkedin_post_reactions'):
            post_url = (args.get('post_url') or '').strip()
            if not post_url:
                return 'post_url is required'
            endpoint_id = ('anyapi.linkedin.post_comments' if name == 'get_linkedin_post_comments'
                            else 'anyapi.linkedin.post_reactions')
            data, error = _serve._treg_call(endpoint_id, {'url': post_url, 'limit': 20}, method='POST')
            if error:
                return f'Could not get LinkedIn post {"comments" if name == "get_linkedin_post_comments" else "reactions"}: {error}'
            _serve._accrue_spend('treg', _serve.TREG_ENDPOINT_COSTS[endpoint_id])
            return json.dumps(data)[:4000]
        raise ValueError(f'unknown tool: {name}')
    return execute_tool


# Real character-sprite generation (2026-09-26), per your explicit request
# to wire up the remaining documented-but-unused APIs. Follows the SAME
# call shape as the village's own already-tested spike script
# (scripts/pixellab_spike.py), not a fresh guess at the public API. Real
# cost tracked via a before/after balance delta (PixelLab has no per-call
# price list the way Treg's catalog does -- see _pixellab_account_balance's
# own docstring for why a forced, uncached read is required on both sides
# of the call), matching the "don't fabricate a number, use a verified one"
# rule already applied to every other real integration tonight.
_PIXELLAB_CHARACTER_TOOL = {
    'type': 'function',
    'function': {
        'name': 'generate_pixel_character',
        'description': (
            "Generate a real 4-direction pixel-art game character sprite from a text description, "
            "via the village's real PixelLab account. This is a real, metered generation (billed "
            "against the village's PixelLab balance or subscription allotment) and can take up to "
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


# Google Sheets/Calendar (2026-09-26), per your explicit request. Both APIs
# are free today (quota, not cost -- see library/skills/google-sheets-
# calendar.md), so unlike Treg/PixelLab there's no spend to accrue; the
# real constraint here is quota, and the skill doc's own stated policy is
# "prefer read-only / low-frequency use... over any write-heavy or high-
# frequency automation" -- these tools stay simple, single-call operations,
# never a batch/high-frequency loop.
_GOOGLE_SHEETS_READ_TOOL = {
    'type': 'function',
    'function': {
        'name': 'read_google_sheet',
        'description': (
            "Read a real range of cells from a real Google Sheet, via the village's own Google "
            "account. Use this to check real, current spreadsheet data -- never invent plausible-"
            "looking cell values."
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
            "Append one real row to a real Google Sheet, via the village's own Google account. "
            "Use this sparingly (occasional syncs, not a high-frequency loop) -- e.g. adding a "
            "finding to a shared roadmap sheet."
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
            "List real, real upcoming events on the village's own Google Calendar. Use this to "
            "check what's actually scheduled -- never invent a plausible-sounding event."
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
            "Create one real event on the village's own Google Calendar -- e.g. for a real "
            "ceremony. Use this sparingly (a handful of real events, not a high-frequency loop)."
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
            return json.dumps(data)[:4000]
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
            return json.dumps(data.get('items', []))[:4000]
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
        raise ValueError(f'unknown tool: {name}')
    return execute_tool


def _run_spike_content(snapshot, agent_id, task, base_ctx=None):
    """Phase E2b: a SPIKE is a time-boxed investigation with no committed
    deliverable. Writes a concise findings artifact to the Library and stores
    a content result. A spike never opens a peer gate and never releases a
    product -- it answers a question, that's all.

    2026-09-26: this used to be a single free-text /api/chat completion with
    NO tool access at all -- the model just guessed from training knowledge
    and called it "findings" (see the SECURITY_TEST_TOOLS comment in serve.py
    for the fabricated-report incident that exact pattern already caused
    once, for the security-test role). Then got real search_web/browse_page
    tool access via the shared _make_web_tools_executor -- fixed the
    fabrication, but a real, harder DreyX request ("list every source ever
    used, assess replicating each daily") showed the NEXT gap: a single
    non-reasoning model in one flat tool loop settles too early on genuinely
    open-ended, multi-step work, because it can't reliably judge "have I
    covered this exhaustively." Real, explicit fix, per your call: PLAN
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
    # Real request (2026-09-26): the player asked whether they'd ever hear
    # about a spike finishing, on either channel -- they wouldn't have; the
    # village was reactive-only. notifyPlayer is the (safe, indirect --
    # _apply_content_result actually queues it) way any executor asks to be
    # notified on completion, success or failure alike, so silence never
    # reads as "still working" when it already gave up.
    tier_slug = _serve._coding_tier_slug() or _serve._mid_tier_slug()
    reasoning_slug = _serve._reasoning_tier_slug() or tier_slug
    if not tier_slug:
        _sim_module._store_content_result(task.get('id'), {
            'note': f'Spiked "{backlog}", but no model tier is configured yet.', 'ok': False,
            'notifyPlayer': {'kind': 'spike_done',
                             'subject': f'[AI Village] Spike stalled: {backlog[:80]}',
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
            'query for what OTHER sites say about the subject -- a subject\'s own pages never '
            'disclose everything about it (methodology, reputation, who else covers it), and a '
            'JS-heavy site may not even be readable by a plain fetch. If the subject has a large '
            'list of items (a directory, a catalog), include a step to sample a few individual item '
            'pages, not just the top-level listing.\n\n'
            'INTERNAL PRIOR ART -- if this question is about reviewing, comparing against, or '
            'building on work already done inside this village (another team\'s finished story, an '
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
    ], max_tokens=500)

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
        'inside this village already did, use search_library/read_library_file to actually read '
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
        'a URL path. A spike never opens a peer-review gate, but it CAN still produce a real '
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
    spike_tools = _serve.AGENT_ASK_TOOLS + [_SPIKE_SANDBOX_TOOL, _LIBRARY_SEARCH_TOOL, _LIBRARY_READ_TOOL,
                                            _TREG_X_TRENDING_TOOL, _TREG_LINKEDIN_SEARCH_TOOL,
                                            _PIXELLAB_CHARACTER_TOOL,
                                            _GOOGLE_SHEETS_READ_TOOL, _GOOGLE_SHEETS_APPEND_TOOL,
                                            _GOOGLE_CALENDAR_LIST_TOOL, _GOOGLE_CALENDAR_CREATE_TOOL]
    _GOOGLE_TOOL_NAMES = ('read_google_sheet', 'append_google_sheet_row',
                          'list_calendar_events', 'create_calendar_event')

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
        if tool_name == 'generate_pixel_character':
            return pixellab_tool(tool_name, args)
        if tool_name in _GOOGLE_TOOL_NAMES:
            return google_tool(tool_name, args)
        return web_tool(tool_name, args)

    # 18/900 (was 10/600): a "list every X across the whole site" question
    # (real request, 2026-09-26 -- "a full list of sources ever used on
    # DreyX") needs many more browse_page round trips than a single-fact
    # lookup. This turn's own text no longer has to BE the final report
    # (synthesize does that from the full transcript below), so its token
    # budget stays modest -- it only needs room for a working summary plus
    # each tool call's own arguments.
    # Forcing search_web specifically (not just force_first_tool=True's
    # "any tool") -- real gap caught live: force_first_tool=True alone still
    # ALWAYS reached for browse_page on the target's own pages and never
    # called search_web at all, missing facts that only live in OTHER sites'
    # coverage of the target (a manual search surfaced DreyX's named upstream
    # sources that 3 rounds of browsing dreyx.com itself never found).
    #
    # Same fix, second application (2026-09-26): an internal-prior-art
    # question needs search_library forced first for the identical reason --
    # a PLAN-prompt instruction to "search the library first" was NOT
    # reliably followed in a real live run (see _spike_wants_internal_review's
    # own docstring for the exact incident). Checked before the search_web
    # default so an internal-review spike doesn't reach for the outside web
    # before it has even looked at what the village already knows.
    #
    # Same fix, third application (2026-09-26): a real X-trending or
    # LinkedIn-search question needs its own specific tool forced first for
    # the identical reason -- built preemptively this time, on day one,
    # rather than after a live miss, now that the pattern is proven twice.
    if _spike_wants_internal_review(backlog, instructions):
        first_tool = 'search_library'
    elif _spike_wants_x_trending(backlog, instructions):
        first_tool = 'x_trending_topics'
    elif _spike_wants_linkedin_search(backlog, instructions):
        first_tool = 'search_linkedin_posts'
    else:
        first_tool = 'search_web' if _serve.TAVILY_API_KEY else True
    # Chunked into rounds of 4 with a reflection self-check between them
    # (see _run_spike_tool_loop_with_reflection) -- same 50-call total
    # budget (raised 18->30->50, 2026-09-27), just spent with a chance to
    # course-correct partway through instead of only finding out it went
    # sideways at the end.
    try:
        execute_text, transcript = _run_spike_tool_loop_with_reflection(
            tier_slug, reasoning_slug, messages, spike_tools, execute_tool,
            total_iterations=50, max_tokens=900, force_first_tool=first_tool)
    except Exception as e:
        # A model-call failure (circuit-breaker RuntimeError, a 4xx, a network
        # blip) must never crash the spike executor or leave a half-baked
        # result -- record an honest not-ok like the empty-investigation path
        # below does, and bail. (2026-09-27: without this, a tripped circuit
        # breaker propagated out of the tool loop and blew up the whole task.)
        _sim_module._store_content_result(task.get('id'), {
            'note': f'Spiked "{backlog}", but the investigation could not run (model call failed): {e}', 'ok': False,
            'notifyPlayer': {'kind': 'spike_done',
                             'subject': f'[AI Village] Spike came up empty: {backlog[:80]}',
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
                             'subject': f'[AI Village] Spike came up empty: {backlog[:80]}',
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
        reasoning_slug, transcript + [{'role': 'user', 'content': synth_prompt}], max_tokens=1800)
    if not finding:
        finding = (execute_text or '').strip()
    if not finding:
        _sim_module._store_content_result(task.get('id'), {
            'note': f'Spiked "{backlog}", but the model call returned nothing usable.', 'ok': False,
            'notifyPlayer': {'kind': 'spike_done',
                             'subject': f'[AI Village] Spike came up empty: {backlog[:80]}',
                             'body': f'{name} looked into "{backlog}" but the model call returned nothing usable. You may want to ask again or rephrase it.'},
        })
        return
    # Deterministic safety net (2026-09-26), not a prompt: confirmed live
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
    # Second deterministic safety net (2026-09-26): confirmed live in the
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
    library_path = f"archive/{int(time.time() * 1000)}-spike-{task.get('id') or 'adhoc'}.md"
    _serve._http_json('POST', base, '/api/library/file',
               {'agentId': agent_id,
                'path': library_path,
                'content': f'# Spike: {backlog}\n\nBy: {name}\n\n{finding}\n',
                'source': 'firsthand'}, key)
    # Data-minimization audit finding (2026-09-24, Hermes Town comparison):
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
        # Real gap caught live (2026-09-26): /api/intent/spike/{id}/promote
        # (turning a spike into a real followup story) only ever had the
        # short note above to work with -- recording the exact path lets it
        # pull the REAL findings (source lists, CSVs, feasibility data)
        # forward instead of a vague pointer.
        'libraryPath': library_path,
        'notifyPlayer': {'kind': 'spike_done',
                         'subject': f'[AI Village] Spike done: {backlog[:80]}',
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
