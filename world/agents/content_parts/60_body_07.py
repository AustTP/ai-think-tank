
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
            "Fetch the real transcript of a YouTube video as plain text. Uses the FREE route "
            "first (YouTube's own captions via yt-dlp) and only falls back to the metered "
            "download-and-transcribe path (Apify youtube-link actor + whisper on the Colab "
            "runtime) when the video has no usable captions. Give a youtube.com or youtu.be URL "
            "(you can paste the watch?v=, /shorts/, or youtu.be link). Returns the full "
            "transcript text (truncated at ~50k chars) and files a copy to "
            "media/transcripts/<video_id>.txt. Use this when an investigation needs what a video "
            "actually says -- e.g. to analyze a claimed method, pull quoted claims, or research a "
            "tutorial -- never invent a plausible-sounding video summary. For a video the free "
            "route can't cover, this is the fallback; you may also read a transcript from "
            "youtubetotranscript.com yourself if it is reachable, but that site blocks automated "
            "fetches."
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
            try:
                from pulse import observe as _pulse_observe
                _pulse_observe('x', 'trends', {'data': data},
                               dedupe_key=json.dumps(data, sort_keys=True, default=str))
            except Exception:
                pass
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
            try:
                from pulse import observe as _pulse_observe
                _pulse_observe('linkedin', query, {'data': data},
                               dedupe_key=json.dumps(data, sort_keys=True, default=str))
            except Exception:
                pass
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
                'requestId': {'type': 'string',
                              'description': ('Optional stable idempotency key (use the task id or a UUID). '
                                              'A retry with the same requestId returns the original run '
                                              'instead of starting a duplicate meter-billed run.')},
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


def _make_apify_tools_executor(agent_id, key):
    """apify_run_actor / apify_get_dataset_items -- thin wrappers over the
    shared _serve._api_execute chokepoint (registry base URL + credential
    injection + budget gate + spend accrual from the run's usageTotalUsd).
    No one-strike/struck_tools tracking here (same
    reasoning as the treg/pixellab executors): those track POLICY denials
    from a real gate, and there is no such gate here -- an error from Apify
    is a real API/network failure or the budget gate, never a per-call
    policy decision, so it's always worth a caller retrying once. Budget
    enforcement happens BEFORE any real run starts (_apify_budget_exceeded
    fails closed inside _api_execute), and real cost is accrued from the
    run's own usageTotalUsd when it settles -- the executor no longer accrues
    itself, so spend is recorded exactly once."""

    def _call(path, method='GET', body=None, query=None, purpose='apify'):
        url = _serve._api_base_url('api.apify.com') + path
        if query:
            url += '?' + _serve.urllib.parse.urlencode(query)
        out = _serve._api_execute(agent_id, key, url, method, body, {}, purpose)
        if out.get('ok') is not True:
            return None, out.get('reason') or out.get('error') or 'failed'
        return out.get('data'), None

    def execute_tool(name, args):
        args = args or {}
        if name == 'apify_run_actor':
            actor_id = (args.get('actorId') or '').strip()
            if not actor_id:
                return 'actorId is required'
            inp = args.get('input') or {}
            wait = min(int(args.get('waitSeconds') or 30), 60)
            request_id = (args.get('requestId') or '').strip()
            if not request_id:
                request_id = _serve.external_write_key(
                    actor_id, json.dumps(inp, sort_keys=True, default=str))[:32]
            cache_key = f'{agent_id}:{actor_id}:{request_id}'

            def _run():
                if _serve._apify_budget_exceeded():
                    return None, ('Apify monthly budget is exhausted -- refusing to start this '
                                  'actor run (fail closed). Do not retry; say so in your findings.')
                query = {'timeout': str(max(60, wait + 30))}
                data, error = _call(f'/actors/{actor_id}/runs', method='POST', body=inp,
                                    query=query, purpose='run an Apify actor')
                if error:
                    return None, f'Could not start Apify actor: {error}'
                run = (data or {}).get('data') or {}
                run_id = run.get('id')
                dataset_id = run.get('defaultDatasetId')
                status = run.get('status') or 'RUNNING'
                elapsed = 0.0
                while status in ('READY', 'RUNNING') and elapsed < wait:
                    time.sleep(3)
                    elapsed += 3
                    data, error = _call(f'/actor-runs/{run_id}',
                                        purpose='poll an Apify actor run')
                    if error:
                        break
                    run = (data or {}).get('data') or {}
                    status = run.get('status') or 'RUNNING'
                    dataset_id = run.get('defaultDatasetId') or dataset_id
                cost = run.get('usageTotalUsd') or 0.0
                result = {'runId': run_id, 'datasetId': dataset_id, 'status': status,
                          'usageTotalUsd': cost}
                if status == 'SUCCEEDED' and dataset_id:
                    items, error = _call(f'/datasets/{dataset_id}/items',
                                         query={'format': 'json', 'clean': '1', 'limit': '10'},
                                         purpose='fetch Apify dataset items')
                    if error:
                        result['itemsError'] = error
                    else:
                        result['items'] = items
                return result, None

            result, error, replayed = _serve.external_write_once(
                'apify_run_actor', cache_key, _run, ttl_s=3600)
            if error:
                return error
            if replayed:
                result['replayed'] = True
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
            data, error = _call(f'/datasets/{dataset_id}/items',
                                query={'format': 'json', 'clean': '1', 'limit': str(limit)},
                                purpose='fetch Apify dataset items')
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


def _make_pixellab_tools_executor(agent_id, key):
    def execute_tool(name, args):
        args = args or {}
        if name != 'generate_pixel_character':
            raise ValueError(f'unknown tool: {name}')
        description = (args.get('description') or '').strip()
        if not description:
            return 'description is required'
        body = {
            'description': description,
            'image_size': {'width': 48, 'height': 48},
            'view': args.get('view') or 'high top-down',
            'template_id': 'mannequin',
        }
        # Runs through the generic workflow chokepoint: submit
        # /create-character-with-4-directions, poll the background job,
        # fetch the final character, and accrue the real balance delta -- all
        # driven by the registry's pixellab workflow spec (no bespoke
        # orchestration here, and no hardcoded base URL).
        base = _serve._api_base_url('api.pixellab.ai')
        out = _serve._api_execute(agent_id, key, base, 'POST', body, {},
                                  'generate a pixel character sprite', workflow=True)
        if out.get('ok') is not True:
            return f'Could not generate character: {out.get("reason") or out.get("error") or "failed"}'
        char = out.get('data') or {}
        char_id = out.get('ids') or {}
        char_id = char_id.get('character_id')
        rotation_urls = char.get('rotation_urls') or {}
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
                'requestId': {'type': 'string',
                              'description': ('Optional stable idempotency key (use the task id or a UUID). '
                                              'A retry with the same requestId returns the original event '
                                              'instead of creating a duplicate.')},
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


def _make_google_tools_executor(agent_id=None):
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
            request_id = (args.get('requestId') or '').strip()
            if not request_id:
                request_id = _serve.external_write_key(
                    summary, start, end, args.get('description') or '')[:32]
            cache_key = f'{agent_id or "tank"}:{request_id}'
            # Google Calendar accepts a caller-supplied event id (base32hex,
            # lowercase a-v/0-9). A deterministic id makes a retry after a
            # crash hit the same event instead of creating a second one.
            event_id = 'tt' + _serve.external_write_key(cache_key)[:30]
            body = {'summary': summary, 'start': {'dateTime': start}, 'end': {'dateTime': end},
                    'id': event_id}
            if args.get('description'):
                body['description'] = args['description']

            def _create():
                data, error = _serve._google_call(
                    'POST', 'https://www.googleapis.com/calendar/v3/calendars/primary/events',
                    body)
                if error:
                    if '409' in str(error) or 'already exists' in str(error).lower():
                        existing, get_error = _serve._google_call(
                            'GET', 'https://www.googleapis.com/calendar/v3/calendars/primary/events/'
                                   + event_id)
                        if not get_error:
                            return {'id': existing.get('id') or event_id,
                                    'htmlLink': existing.get('htmlLink')}, None
                    return None, f'Could not create the event: {error}'
                return {'id': data.get('id') or event_id,
                        'htmlLink': data.get('htmlLink')}, None

            result, error, replayed = _serve.external_write_once(
                'calendar_event', cache_key, _create, ttl_s=86400 * 30)
            if error:
                return error
            if replayed:
                result['replayed'] = True
            return json.dumps(result)
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
