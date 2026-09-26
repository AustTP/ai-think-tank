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

    # 4. Persist as server authority (chains the passport; survives autosave).
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


def _run_spike_content(snapshot, agent_id, task, base_ctx=None):
    """Phase E2b: a SPIKE is a time-boxed investigation with no committed
    deliverable. The executor keeps it to a SINGLE short model call (honoring
    the spike's budgetMs), writes a concise findings artifact to the Library,
    and stores a content result. A spike never opens a peer gate and never
    releases a product -- it answers a question, that's all."""
    import sim as _sim_module
    base = _serve.SELF_BASE_URL
    key = _serve.get_or_create_agent_key(agent_id)
    name = _agent_name(snapshot, agent_id)
    backlog = task.get('title') or ''
    instructions = task.get('instructions')
    budget = task.get('budgetMs')
    prompt = (f'You are {name}, running a time-boxed SPIKE (~{(budget or 60_000) / 1000:.0f}s). '
              f'Question: {backlog}.'
              f'{" Method/constraints: " + instructions if instructions else ""}. '
              'Write 2-4 concrete sentences of real findings -- what was tried, '
              'what was learned, and a recommendation. No filler, no release notes.')
    tier_slug = _serve._mid_tier_slug()
    if not tier_slug:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Spiked "{backlog}", but no model tier is configured yet.', 'ok': False})
        return
    r = _serve._http_json('POST', base, '/api/chat',
                   {'model': tier_slug,
                    'messages': [{'role': 'system', 'content': prompt},
                                 {'role': 'user', 'content': 'Go ahead.'}],
                    'agentId': agent_id}, key)
    finding = (r.get('reply') or r.get('content') or '').strip() if isinstance(r, dict) else ''
    if not finding:
        _sim_module._store_content_result(task.get('id'),
                                          {'note': f'Spiked "{backlog}", but the model call returned nothing usable.', 'ok': False})
        return
    _serve._http_json('POST', base, '/api/library/file',
               {'agentId': agent_id,
                'path': f"archive/{int(time.time() * 1000)}-spike-{task.get('id') or 'adhoc'}.md",
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
    # file just written.
    _sim_module._store_content_result(task.get('id'),
                                      {'note': f'{name} spiked "{backlog}" -- see the Library entry just filed.', 'ok': True})


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
