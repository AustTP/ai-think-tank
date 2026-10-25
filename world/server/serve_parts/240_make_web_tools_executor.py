

def _make_web_tools_executor(agent_id, agent_key, default_location=None, default_query=None, struck_tools=None, work_context='', work_context_trusted=False, task_id=None):
    """Shared search_web/browse_page tool executor for any
    AGENT_ASK_TOOLS-driven tool-calling loop. Extracted so the
    spike content executor can reuse the exact same gated fetch/wrap logic
    _ask_core uses, instead of the single free-text completion with NO tool
    access it used before -- see the SECURITY_TEST_TOOLS comment above about
    that exact pattern already producing fabricated "test reports" once.

    `work_context` is the player-filed task this agent is currently executing
    (title -- instructions), threaded into every browser-like endpoint so its
    Jev gate judges URLs against the work instead of in isolation. It comes
    from the assignment, never from what the agent claims, so an agent cannot
    manufacture its own bypass by lying about its work.

    `work_context_trusted` is True only when the STORED task was player-authored
    (`task.playerAuthored`, read server-side). When it is, this factory mints a
    one-time work-trust nonce (see _mint_work_trust_nonce) and sends THAT to the
    endpoints instead of the bare boolean -- the bypass is now anchored to a
    server-minted credential, so an agent cannot set `workContextTrusted` in its
    own request and launder self-authored text past Jev.

    `struck_tools`, if given a set, gets a tool name added to it the moment
    that tool is BLOCKED (a real policy denial -- Jev said no -- not a
    transient fetch error, which may still be worth one retry). The caller
    is expected to check this set itself before calling in again; this
    function only records strikes, on the ask lane's request it never
    enforces them (None here -- the ask lane's plain Q&A doesn't need this;
    spikes opt in by passing a real set). The rule is one-strike-per-
    service: never retry a tool that just told you no, don't burn budget
    hoping for a different answer. Self-checks `struck_tools` too (not just
    records into it) --
    defense in depth so a caller that passes the set but forgets to check
    it itself doesn't silently get a no-op."""
    # Track every URL the tools actually returned. browse_page may only
    # follow a path that search_web or a previous page really exposed, so a
    # model cannot invent a plausible news/post URL and cite it as evidence.
    from core.url_evidence import DiscoveredURLs
    discovered = DiscoveredURLs((default_query or '') + '\n' + (work_context or ''))
    # Server-minted, single-use proof that the work-context host bypass is
    # backed by a stored player-authored task. Minted HERE (server code, at
    # factory time, once per tool set) -- never from an agent's request body.
    work_trust_nonce = _mint_work_trust_nonce() if work_context_trusted else ''
    def execute_tool(name, args):
        if struck_tools is not None and name in struck_tools:
            return (f'{name} was already blocked once this investigation (one-strike) -- do not '
                    'call it again, use a different tool or approach instead.')
        if name in ('mcp_list_servers', 'mcp_list_tools', 'mcp_read', 'mcp_call'):
            return _mcp_tool_text(name, args, agent_id=agent_id)
        if name in ('list_event_streams', 'read_event_stream', 'ack_event_stream'):
            return _durable_tool_text(name, args, agent_id=agent_id)
        if name in ('list_object_stores', 'read_object_store', 'write_object_store'):
            return _object_tool_text(name, args, agent_id=agent_id)
        if name in ('list_cli_tools', 'read_cli', 'run_cli'):
            return _cli_tool_text(name, args, agent_id=agent_id)
        if name == 'browser_act':
            from content import _make_browser_act_executor
            return _make_browser_act_executor(
                agent_id, agent_key, f'sandbox-ask-{agent_id}',
                struck_tools=struck_tools, work_context=work_context,
                work_context_trusted=work_context_trusted,
                task_id=task_id)(name, args)
        if name == 'solve_captcha':
            from content import _make_captcha_solve_executor
            return _make_captcha_solve_executor(agent_id, agent_key)(name, args)
        if name == 'list_capability_handles':
            from content import _make_list_handles_executor
            return _make_list_handles_executor(agent_id, agent_key)(name, args)
        if name == 'team_status':
            # Read-only live queue/assignment view (DB-only, no state change) --
            # offered to spikes too, so a spike can see what the team is doing
            # before it decides how to fit its findings in.
            return _team_status_text()
        if name == 'allowlist_status':
            # Read-only allowlist pending-queue / host-tier view -- the
            # observability half of request_allowlist.
            return _allowlist_status_text((args or {}).get('host') or '')
        if name == 'read_mailbox':
            # Read-only inbox view. Own mailbox for anyone; another agent's
            # only for the Post Office / a director / the chief of staff (enforced in
            # _read_mailbox_tool_text, server-side, not by prompt).
            return _read_mailbox_tool_text(agent_id, args)
        if name == 'send_mail':
            # Agent-to-agent mail that does NOT wake the recipient.
            # The ENDPOINT binds the sender to this agent's key,
            # validates the recipient, and appends through the pure
            # mailbox writer -- no wake marker, no dispatch. The
            # executor never decides authority itself; it forwards
            # the caller's own key so the server can refuse.
            to_id = ((args or {}).get('toAgentId') or '').strip()
            text = (args or {}).get('text')
            if not to_id or not isinstance(text, str) or not text.strip():
                return 'send_mail requires a toAgentId and non-empty text.'
            result = _http_json('POST', SELF_BASE_URL, '/api/mail/send',
                                {'toId': to_id, 'text': text.strip()[:2000]},
                                agent_key)
            if isinstance(result, dict) and result.get('ok'):
                return (f'Mail sent to {to_id}: it landed unread '
                        '(it does not wake the recipient).')
            err = (result.get('error') if isinstance(result, dict) else None) or result
            return f'Could not send the mail: {err}'
        if name == 'mark_mailbox_read':
            # The explicit "mark all read" -- a real mutation, unlike
            # read_mailbox's unreadOnly. Same permission gate as
            # read_mailbox, enforced server-side by the endpoint and
            # _mark_mailbox_read; the executor just forwards the key.
            target = ((args or {}).get('agentId') or '').strip()
            body = {'agentId': target} if target else {}
            result = _http_json('POST', SELF_BASE_URL, '/api/mail/read',
                                body, agent_key)
            if isinstance(result, dict) and result.get('ok'):
                return (f'Marked {result.get("marked")} unread message(s) '
                        f'in {result.get("agentId")}\'s mailbox as read.')
            err = (result.get('error') if isinstance(result, dict) else None) or result
            return f'Could not mark the mailbox read: {err}'
        if name == 'request_allowlist':
            result = _http_json('POST', SELF_BASE_URL, '/api/allowlist/request', {
                'agentId': agent_id,
                'host': (args or {}).get('host') or '',
                'purpose': (args or {}).get('purpose') or '',
            }, agent_key)
            if isinstance(result, dict) and not result.get('error'):
                msg = result.get('message') or ''
                return (f'{msg} [do not call request_allowlist again for this host -- one request is '
                        f'pending director review]' if result.get('requested')
                        else f'{msg} [no further request needed]')
            return f'Could not file the allowlist request: {result}'
        if name == 'request_credential_access':
            service = ((args or {}).get('service') or '').strip().lower()
            requested_task = ((args or {}).get('taskId') or '').strip() or (task_id or '')
            purpose = ((args or {}).get('purpose') or '').strip()
            if not requested_task:
                return ('request_credential_access needs the active taskId; it is '
                        'available only while you are working a task.')
            result = _http_json('POST', SELF_BASE_URL,
                                '/api/credentials/request-access', {
                                    'agentId': agent_id,
                                    'service': service,
                                    'taskId': requested_task,
                                    'purpose': purpose,
                                }, agent_key, timeout=30)
            if isinstance(result, dict) and result.get('ok'):
                return (f"Granted read-only {result.get('service')} access for this task. "
                        f"handle={result.get('handle')} hosts={', '.join(result.get('hosts') or [])} "
                        f"expiresInSec={result.get('expiresInSec')}. Use it with browser_act "
                        "op=login on the service login page. Fill/click/submit are refused on "
                        "social hosts outside that login.")
            err = (result.get('error') if isinstance(result, dict) else None) or result
            return (f'Credential access refused: {err} [do not retry unchanged; use a '
                    'different source or explain the task need]')
        if name in ('grant_allowlist', 'revoke_allowlist'):
            # Director/admin-only allowlist mutation. The ENDPOINT enforces the
            # role gate via _operator_gate (session or a director/admin agent
            # key) -- this executor never decides authority itself, it just
            # forwards the call with the caller's own key so the server can
            # refuse an ordinary agent. No tool description lies about access:
            # a non-director's attempt comes back as the server's own refusal.
            path = '/api/allowlist/grant' if name == 'grant_allowlist' else '/api/allowlist/revoke'
            result = _http_json('POST', SELF_BASE_URL, path, {
                'agentId': agent_id,
                'host': (args or {}).get('host') or '',
                'purpose': (args or {}).get('purpose') or '',
            }, agent_key)
            if isinstance(result, dict):
                if result.get('error'):
                    return f'Could not {name.split("_")[0]} the allowlist host: {result["error"]}'
                msg = result.get('message') or ''
                verb = 'granted' if name == 'grant_allowlist' else 'revoked'
                return f'{msg} [this host is now {verb} -- no further {name} call needed for it]'
            return f'Could not apply the allowlist change (unexpected response): {result}'
        if name in ('generate_image', 'generate_video'):
            # Real, metered Higgsfield generation -- estimated, submitted,
            # polled, and accrued inside the single choke point.
            return _higgsfield_generate('image' if name == 'generate_image' else 'video',
                                        args, agent_id, agent_key)
        if name == 'search_web':
            query = (args or {}).get('query') or default_query or ''
            # Runs through the generic chokepoint -- Tavily auth (body_field
            # api_key) injected server-side from the registry, page-request
            # spend accrued.
            out = _api_execute(agent_id, agent_key, _api_base_url('api.tavily.com') + '/search',
                               'POST', {'query': query}, {}, 'web search')
            discovered.observe(out)
            if out.get('ok') is not True:
                return f'Could not search the web: {out.get("reason") or out.get("error") or "failed"}'
            return f"{out.get('modelInstruction', '')}\n\n{out['textForModel']}"
        if name == 'read_ais_feed':
            # Live AIS vessel positions from the AISStream collector (WebSocket
            # -- NOT reachable via api_call). The feed is a registered ws_feed
            # driven by the generic collector; this tool reads its ring buffer.
            max_msgs = max(1, min(int((args or {}).get('max') or 20), 100))
            recent = _ws_recent('aisstream', max_items=max_msgs)
            status = _ws_status('aisstream')
            parts = [f"connection={'live' if status.get('connected') else 'down'}",
                     f"frames in buffer window={len(recent)}"]
            if not status.get('connected') and status.get('last_error'):
                parts.append(f"collector error: {status['last_error']}")
            if recent:
                rows = []
                for m in recent:
                    lat = m.get('lat') or m.get('latitude')
                    lon = m.get('lon') or m.get('longitude')
                    rows.append(f"- {m.get('topic') or 'frame'} @{lat},{lon}")
                parts.append('recent frames:\n' + '\n'.join(rows[-max_msgs:]))
            raw = ' | '.join(parts)
            wrapped, _nonce, _tag, instruction = wrap_external_content(
                raw, 'live AIS ship position data from AISStream')
            return f'{instruction}\n\n{wrapped}'
        if name == 'read_rss_feed':
            # Curated RSS/ATOM feeds (current events, space, hazards, research),
            # polled and buffered by the aggregator -- NOT fetched on demand.
            feed_id = ((args or {}).get('feed') or '').strip().lower()
            max_items = max(1, min(int((args or {}).get('max') or 8), 30))
            if feed_id and feed_id not in _RSS_FEEDS:
                avail = ', '.join(sorted(_RSS_FEEDS))
                return f'Unknown feed "{feed_id}". Available feeds: {avail}'
            feeds = [feed_id] if feed_id else sorted(_RSS_FEEDS)
            parts = []
            for fid in feeds:
                st = _RSS_STATUS.get(fid) or {}
                items = _rss_recent(fid, max_items=max_items)
                head = f"{fid} ({_RSS_FEEDS[fid]['name']}): status={'ok' if st.get('ok') else 'stale'}"
                if not st.get('ok') and st.get('last_error'):
                    head += f" last_error={st['last_error']}"
                if not items:
                    parts.append(f'{head} | no items buffered yet (first poll may not have run)')
                    continue
                rows = []
                for it in items:
                    line = f"- {it['title']}"
                    if it['link']:
                        line += f" | {it['link']}"
                    if it.get('matchedTopics'):
                        line += f" [watchlist: {', '.join(it['matchedTopics'])}]"
                    rows.append(line)
                parts.append(f'{head} | {len(items)} recent items\n' + '\n'.join(rows))
            raw = '\n\n'.join(parts)
            discovered.observe(raw)
            wrapped, _nonce, _tag, instruction = wrap_external_content(
                raw, 'curated RSS/ATOM feed items polled by the think tank')
            return f'{instruction}\n\n{wrapped}'
        if name == 'read_market_feed':
            # Live crypto quotes from the Kraken public ticker (no key). Latest
            # per pair only; history is a short in-process ring.
            pair = _kraken_normalize_pair((args or {}).get('pair'))
            quotes = _ws_latest('kraken', [pair] if pair else None)
            status = _ws_status('kraken')
            parts = [f"connection={'live' if status.get('connected') else 'down'}"]
            if not status.get('connected') and status.get('last_error'):
                parts.append(f"collector error: {status['last_error']}")
            if not quotes:
                parts.append('no quotes buffered yet (collector still connecting)')
            for p in sorted(quotes):
                q = quotes[p]
                parts.append(f"{p}: {q['price']} bid={q['bid']} ask={q['ask']} "
                             f"ts={q['ts']}")
            raw = ' | '.join(parts)
            wrapped, _nonce, _tag, instruction = wrap_external_content(
                raw, 'live crypto market data from the Kraken public ticker')
            return f'{instruction}\n\n{wrapped}'
        if name == 'read_live_feed':
            # Generic read of any registered real-time feed's buffer -- new
            # feeds added by operators via /api/ws/* become readable here with
            # zero code changes.
            service = ((args or {}).get('service') or '').strip().lower()
            if service and service not in _WS_FEEDS:
                avail = ', '.join(sorted(_WS_FEEDS)) or 'none registered'
                return f'Unknown feed service "{service}". Registered feeds: {avail}'
            topic = ((args or {}).get('topic') or '').strip()
            max_items = max(1, min(int((args or {}).get('max') or 10), 50))
            services = [service] if service else sorted(_WS_FEEDS)
            parts = []
            for sid in services:
                st = _ws_status(sid)
                items = _ws_recent(sid, max_items=max_items, topic=topic or None)
                head = f"{sid}: connection={'live' if st.get('connected') else 'down'}, {len(items)} recent records"
                if not st.get('connected') and st.get('last_error'):
                    head += f" last_error={st['last_error']}"
                if not items:
                    parts.append(f'{head} | no records buffered yet (collector may still be connecting)')
                    continue
                rows = []
                for it in items:
                    fields = {k: v for k, v in it.items() if k not in ('ts', 'topic', 'raw')}
                    rows.append(f"- {it['topic']}: " + ', '.join(f'{k}={v}' for k, v in fields.items()))
                parts.append(f'{head}\n' + '\n'.join(rows))
            raw = '\n\n'.join(parts)
            wrapped, _nonce, _tag, instruction = wrap_external_content(
                raw, 'live real-time feed records buffered by the think tank')
            return f'{instruction}\n\n{wrapped}'
        if name == 'read_live_coverage':
            # The vision desk's running transcript. Any agent may read it;
            # observations are model output about an external video, so they
            # cross the same external-content boundary as a fetched page.
            run_id = str((args or {}).get('run_id') or '').strip()
            try:
                max_items = int((args or {}).get('max') or 30)
            except (TypeError, ValueError):
                max_items = 30
            max_items = max(1, min(max_items, 200))
            query = f'?limit={max_items}'
            if run_id:
                query = ('?run_id=' + urllib.parse.quote(run_id) +
                         f'&limit={max_items}')
            result = _http_json('GET', SELF_BASE_URL,
                                '/api/live-coverage/status' + query,
                                None, agent_key)
            if not isinstance(result, dict) or result.get('error'):
                return ('Could not read live coverage: '
                        + str((result or {}).get('error') or 'no response'))
            run = result.get('run')
            if not run:
                return ('No live coverage runs yet. A director can start one with '
                        'start_live_coverage using a public stream URL.')
            lines = [
                f"run {run.get('id')} [{run.get('status')}] label={run.get('label')}",
                f"stream={run.get('stream')} frames={run.get('frames')}/{run.get('maxFrames')} "
                f"spent=${float(run.get('spentUsd') or 0):.4f}",
                f"stopReason={run.get('stopReason') or 'none'} "
                f"libraryPath={run.get('libraryPath') or 'pending'}",
            ]
            observations = run.get('observations') or []
            if observations:
                lines.append('recent observations:')
                for obs in observations:
                    line = (f"- +{obs.get('offsetS')}s frame {obs.get('frame')} "
                            f"(confidence {float(obs.get('confidence') or 0):.2f}): "
                            f"{obs.get('observation')}")
                    if obs.get('changes'):
                        line += f" | change: {obs.get('changes')}"
                    if obs.get('entities'):
                        line += f" | entities: {', '.join(obs.get('entities'))}"
                    lines.append(line)
            else:
                lines.append('no observations yet')
            raw = '\n'.join(lines)
            wrapped, _nonce, _tag, instruction = wrap_external_content(
                raw, 'live video coverage observations from the vision desk')
            return f'{instruction}\n\n{wrapped}'
        if name == 'start_live_coverage':
            payload = {
                'url': (args or {}).get('url') or '',
                'label': (args or {}).get('label') or '',
                'intervalS': (args or {}).get('interval_s'),
                'maxSeconds': (args or {}).get('max_seconds'),
                'maxFrames': (args or {}).get('max_frames'),
                'maxUsd': (args or {}).get('max_usd'),
            }
            result = _http_json('POST', SELF_BASE_URL,
                                '/api/live-coverage/start', payload,
                                agent_key, timeout=30)
            if isinstance(result, dict) and result.get('ok') and result.get('run'):
                run = result['run']
                return (f"Live coverage started: run {run.get('id')} is sampling "
                        f"every {run.get('intervalS') or '?'}s for up to "
                        f"{run.get('maxSeconds')}s / {run.get('maxFrames')} frames / "
                        f"${run.get('maxUsd')}. Other agents can call "
                        f"read_live_coverage while it runs.")
            return ('Could not start live coverage: '
                    + str((result or {}).get('error') or 'no response'))
        if name == 'stop_live_coverage':
            run_id = str((args or {}).get('run_id') or '').strip()
            if not run_id:
                return 'stop_live_coverage needs a run_id.'
            result = _http_json('POST', SELF_BASE_URL,
                                '/api/live-coverage/stop', {'runId': run_id},
                                agent_key, timeout=30)
            if isinstance(result, dict) and result.get('ok'):
                return f'Live coverage {run_id} is stopping.'
            return ('Could not stop live coverage: '
                    + str((result or {}).get('error') or 'no response'))
        if name == 'browse_page':
            country = ((args or {}).get('country') or '').strip().lower()
            exit_name = ((args or {}).get('exit') or '').strip()
            from urllib.parse import urlsplit
            url = (args or {}).get('url') or ''
            if not discovered.allows(url):
                valid_url = discovered.valid(url)
                log_action(agent_id, 'browse_url_rejected', {
                    'url': url,
                    'reason': 'not_discovered' if valid_url else 'malformed',
                }, authorized=False)
                if not valid_url:
                    return ('__TOOL_ERROR__: URL syntax is invalid. No page fetch was attempted. '
                            'Copy a complete exact URL from a search result or page link.')
                host = urlsplit(url).hostname
                choices = sorted(u for u in discovered.urls
                                 if urlsplit(u).hostname == host)[:8]
                return ('__TOOL_ERROR__: URL has valid syntax but this exact path was not discovered. '
                        'No page fetch was attempted. Use one of the actual discovered links below '
                        'or search for the exact page; do not invent paths.\n'
                        + '\n'.join(choices or sorted(discovered.urls)[:8]))
            result = _http_json('POST', SELF_BASE_URL, '/api/browse', {
                'agentId': agent_id,
                'url': url,
                'purpose': (args or {}).get('purpose') or 'research',
                'render': bool((args or {}).get('render')),
                'viaVpnCountry': country,
                'viaVpnExit': exit_name,
                'workContext': work_context,
                # Server-minted proof of player-authored provenance; the endpoint
                # redeems it and ignores the legacy boolean entirely.
                'workTrustNonce': work_trust_nonce,
            }, agent_key, timeout=60 if not (country or exit_name) else 60 + MULLVAD_CONNECT_TIMEOUT_S)
            if isinstance(result, dict) and result.get('allowed') and result.get('textForModel'):
                # Gap: /api/browse already
                # extracts every real <a href> on the page (_extract_links)
                # specifically so an agent can "follow a breadcrumb" to a
                # page it doesn't already know the URL for -- but this
                # executor was discarding that field entirely, so the model
                # had no real links to follow and had to GUESS the next URL
                # (confirmed: guessed /ai-news-feed on dreyx.com, got a
                # 404, gave up instead of picking a real link off the page
                # it just fetched). Surface a capped list of real
                # {text, url} pairs so multi-page investigation actually
                # works.
                links = result.get('links') or []
                discovered.observe(result)
                links_block = ''
                if links:
                    lines = [f"- {(l.get('text') or l.get('url'))[:80]} -> {l.get('url')}"
                             for l in links[:40] if l.get('url')]
                    links_block = ("\n\nReal links found on this page (use one of these EXACT urls "
                                   "to visit another page -- never invent a url):\n" + '\n'.join(lines))
                return f"{result.get('modelInstruction', '')}\n\n{result['textForModel']}{links_block}"
            if isinstance(result, dict) and result.get('allowed') is False:
                # A real policy denial -- retrying won't change Jev's mind.
                if struck_tools is not None:
                    struck_tools.add('browse_page')
                return (f"Could not visit that page: {result.get('reason', 'not approved')} "
                        "[ONE-STRIKE: this was a policy denial, not a technical error -- do not "
                        "retry browse_page, try search_web or a different real link instead] "
                        f"If you legitimately need this host for standing access, file ONE "
                        f"request_allowlist call for it (directors review it automatically; "
                        f"{_allowlist_wait_estimate()}) -- then move on with other sources "
                        f"while it is pending.")
            if isinstance(result, dict) and result.get('error'):
                # Transient (network/timeout) -- may be worth one retry, unlike a policy block.
                return (f"Could not visit that page: {result['error']} "
                        "[this may be a transient error -- you may retry ONCE, e.g. with "
                        "render=true, but don't loop on it]")
            return 'Could not visit that page (unexpected response).'
        if name == 'download_file':
            result = _http_json('POST', SELF_BASE_URL, '/api/download', {
                'agentId': agent_id,
                'url': (args or {}).get('url') or '',
                'filename': (args or {}).get('filename') or '',
                'purpose': (args or {}).get('purpose') or 'research',
                'maxBytes': (args or {}).get('maxBytes') or 0,
                'workContext': work_context,
                # Server-minted proof of player-authored provenance; the endpoint
                # redeems it and ignores the legacy boolean entirely.
                'workTrustNonce': work_trust_nonce,
            }, agent_key, timeout=90)
            if isinstance(result, dict) and result.get('ok'):
                return (f"Downloaded to your own filespace at agents/{agent_id}/downloads/{result.get('path', '').rsplit('/', 1)[-1]} "
                        f"({result.get('bytes')} bytes, sha256 {result.get('sha256', '')[:16]}...). "
                        "The file is saved under YOUR downloads directory; read it from there if a "
                        "later step needs its contents.")
            if isinstance(result, dict) and result.get('error'):
                err = result['error']
                if 'not approved' in err or 'capability handle' in err:
                    if struck_tools is not None:
                        struck_tools.add('download_file')
                    return (f"Could not download that file: {err} "
                            "[ONE-STRIKE: this was a policy denial, not a technical error -- do not "
                            "retry download_file, try browse_page or a different source instead]")
                return (f"Could not download that file: {err} "
                        "[this may be a transient error -- you may retry ONCE, but don't loop on it]")
            return 'Could not download that file (unexpected response).'
        if name == 'api_call':
            result = _http_json('POST', SELF_BASE_URL, '/api/api-call', {
                'agentId': agent_id,
                'url': (args or {}).get('url') or '',
                'method': (args or {}).get('method') or 'GET',
                'body': (args or {}).get('body'),
                'headers': (args or {}).get('headers') or {},
                'purpose': (args or {}).get('purpose') or 'research',
                'workContext': work_context,
                # Server-minted proof of player-authored provenance; the endpoint
                # redeems it and ignores the legacy boolean entirely.
                'workTrustNonce': work_trust_nonce,
            }, agent_key, timeout=API_CALL_TIMEOUT_S + 5)
            if isinstance(result, dict) and result.get('allowed') and result.get('textForModel'):
                return f"{result.get('modelInstruction', '')}\n\n{result['textForModel']}"
            if isinstance(result, dict) and result.get('allowed') is False:
                # A real policy or registry denial -- retrying won't change it.
                if struck_tools is not None:
                    struck_tools.add('api_call')
                return (f"Could not make that API call: {result.get('reason', 'not approved')} "
                        "[ONE-STRIKE: this was a policy or registry denial, not a technical "
                        "error -- do not retry the same api_call request] "
                        f"If you legitimately need this host for standing access, file ONE "
                        f"request_allowlist call for it (directors review it automatically; "
                        f"{_allowlist_wait_estimate()}) -- then move on with other sources "
                        f"while it is pending.")
            if isinstance(result, dict) and result.get('error'):
                return (f"Could not make that API call: {result['error']} "
                        "[this may be a transient error -- you may retry ONCE, but don't loop on it]")
            return 'Could not make that API call (unexpected response).'
        if name == 'propose_api_service':
            result = _http_json('POST', SELF_BASE_URL, '/api/api-service/request', {
                'agentId': agent_id,
                'service': (args or {}).get('service') or {},
                'purpose': (args or {}).get('purpose') or 'propose a new registered service',
            }, agent_key, timeout=API_CALL_TIMEOUT_S + 5)
            if isinstance(result, dict) and result.get('requested'):
                return (f"Filed a proposal for service '{result.get('serviceId', '?')}' (escalation "
                        f"{result.get('escalationId', '?')}). The player must approve it by email before "
                        "it goes live. Until then the host is only reachable read-only through the "
                        "normal Jev gate.")
            if isinstance(result, dict) and result.get('error'):
                return (f"Could not file the service proposal: {result['error']} "
                        "[do not keep retrying the same invalid proposal -- fix the spec or drop it]")
            return 'Could not file the service proposal (unexpected response).'
        if name == 'list_services':
            result = _http_json('GET', SELF_BASE_URL, '/api/api-service/list', None, agent_key,
                                timeout=API_CALL_TIMEOUT_S + 5)
            if isinstance(result, dict) and isinstance(result.get('services'), list):
                lines = ['Registered external services reachable via api_call (credentials are '
                         'injected server-side; you never supply or see a key):', '']
                for s in result['services']:
                    methods = ', '.join(s.get('methods') or [])
                    desc = (s.get('description') or '').strip()
                    lines.append(f"- {s.get('name')}  [id: {s.get('id')}]")
                    lines.append(f"    base_url: {s.get('baseUrl')}   methods: {methods}   "
                                 f"spend: {s.get('spend')}")
                    if desc:
                        lines.append(f"    when to use: {desc}")
                lines.append('')
                lines.append('Use api_call with a service\'s base_url + path to reach one. If a '
                             'service you need is missing, propose_api_service files a request for '
                             'the player to approve.')
                return '\n'.join(lines)
            if isinstance(result, dict) and result.get('error'):
                return (f"Could not list registered services: {result['error']} "
                        "[this is a read-only listing -- you may retry once if it looks transient]")
            return 'Could not list registered services (unexpected response).'
        if name == 'self_edit':
            # Code self-modification, gate #1 of the chain: the ENDPOINT owns
            # every real check (key, rate limit, anchor uniqueness, protected
            # paths, importer scan, JEV quorum, quarantine validator); the
            # executor only forwards this agent's identity and key -- same
            # shape as send_mail/request_allowlist above. The propose chain
            # BLOCKS on the quorum's sequential judge calls, hence the long
            # client timeout.
            path = ((args or {}).get('path') or '').strip()
            find_text = (args or {}).get('find')
            replace_text = (args or {}).get('replace')
            summary = ((args or {}).get('summary') or '').strip()
            if (not path or not isinstance(find_text, str) or not find_text
                    or not isinstance(replace_text, str)):
                return ('self_edit requires path, find (non-empty exact anchor) '
                        'and replace, plus a summary that says WHY.')
            result = _http_json('POST', SELF_BASE_URL, '/api/selfedit/propose', {
                'agentId': agent_id,
                'path': path,
                'find': find_text,
                'replace': replace_text,
                'summary': summary,
                'windowOnly': bool((args or {}).get('windowOnly')),
            }, agent_key, timeout=300)
            if isinstance(result, dict) and result.get('error'):
                return (f'self_edit not filed: {result["error"]} '
                        f'[do not keep re-proposing the same splice -- check '
                        f'self_edit_status for the recorded outcome]')
            if isinstance(result, dict) and result.get('status'):
                return (f'{result.get("message", "edit filed.")} '
                        f'[edit {result.get("id", "?")} status: {result.get("status")}]')
            return 'self_edit returned an unexpected response (nothing changed).'
        if name in ('self_edit_status', 'selfedit_status'):
            limit = (args or {}).get('limit')
            try:
                limit = max(1, min(int(limit or 15), 50))
            except (TypeError, ValueError):
                limit = 15
            result = _http_json('POST', SELF_BASE_URL, '/api/selfedit/status', {
                'agentId': agent_id, 'limit': limit,
            }, agent_key, timeout=30, extra_headers=_selfedit_fix_headers(agent_id))
            if isinstance(result, dict) and isinstance(result.get('edits'), list):
                edits = result['edits']
                if not edits:
                    return ('No self-edits have been proposed yet: nothing armed, '
                            'nothing pending, nothing applied.')
                lines = ['SELF-EDIT LEDGER (newest first; armed edits apply the '
                         'next time the tank is dormant):', '']
                for e in edits:
                    conf = (f' jev {e.get("jevConfidence")}'
                            if e.get('jevConfidence') is not None else '')
                    lines.append(f"- {e.get('id')} {(e.get('status') or '').upper()} "
                                 f"{e.get('path')} by {e.get('agent')}{conf}")
                    lines.append(f"    summary: {(e.get('summary') or '')[:160]}")
                    if e.get('validator'):
                        lines.append(f"    validator: {e.get('validator')[:160]}")
                    if e.get('note'):
                        lines.append(f"    note: {e.get('note')[:160]}")
                windows = result.get('windows') or []
                if windows:
                    lines.append('')
                    lines.append(f'DORMANCY WINDOWS ({len(windows)} newest, '
                                 'one-shot sleep slots for applying edits):')
                    for w in windows:
                        lines.append(
                            f"- {w.get('id')} {(w.get('status') or '').upper()} "
                            f"{w.get('date')} {w.get('start')} "
                            f"+{w.get('durationMin')}m by {w.get('agent')}")
                        if w.get('validator'):
                            lines.append(f"    validator: {w.get('validator')} "
                                         f"(status: {w.get('validatorStatus') or '-'})")
                        if w.get('pushStatus'):
                            push_extra = ''
                            if w.get('pushCategory'):
                                push_extra += f" / {w.get('pushCategory')}"
                            if w.get('pushSha'):
                                push_extra += f" {w.get('pushSha')}"
                            lines.append(f"    push: {w.get('pushStatus')}"
                                         f"{push_extra}")
                        lines.append(f"    reason: {(w.get('reason') or '')[:160]}")
                        if w.get('note'):
                            lines.append(f"    note: {w.get('note')[:160]}")
                return '\n'.join(lines)
            if isinstance(result, dict) and result.get('error'):
                return f'Could not read the self-edit ledger: {result["error"]}'
            return 'Could not read the self-edit ledger (unexpected response).'
        if name == 'schedule_dormant_apply':
            # Change request #1 of the dormancy-window chain: like self_edit
            # above, the executor only forwards identity and key; the
            # ENDPOINT runs the conflict scan, the supervisor JEV judgment,
            # and the player-link escalation.
            date = ((args or {}).get('date') or '').strip()
            start = ((args or {}).get('start') or '').strip()
            reason = ((args or {}).get('reason') or '').strip()
            validator = ((args or {}).get('validator') or '').strip()
            if not date or not start or not reason:
                return ('schedule_dormant_apply requires date (YYYY-MM-DD), '
                        'start (HH:MM) and a reason naming the edits that are '
                        'landing.')
            if not validator:
                return ('schedule_dormant_apply requires a validator: name a '
                        'DIFFERENT agent who will validate the applied result '
                        'before it is pushed to main.')
            body = {'agentId': agent_id, 'date': date, 'start': start,
                    'reason': reason, 'validator': validator}
            try:
                body['durationMin'] = int((args or {}).get('durationMin'))
            except (TypeError, ValueError):
                pass
            for arg_name, body_name in (
                    ('title', 'title'), ('description', 'description'),
                    ('changeType', 'changeType'), ('riskLevel', 'riskLevel'),
                    ('serviceMode', 'serviceMode'),
                    ('affectedServices', 'affectedServices'),
                    ('fileManifest', 'fileManifest'),
                    ('rollbackPlan', 'rollbackPlan'),
                    ('rollbackOwner', 'rollbackOwner'),
                    ('successCriteria', 'successCriteria'),
                    ('abortCriteria', 'abortCriteria'),
                    ('postApplyChecks', 'postApplyChecks'),
                    ('safeStatePlan', 'safeStatePlan'),
                    ('restorePlan', 'restorePlan'), ('onCall', 'onCall'),
                    ('acknowledgements', 'acknowledgements'),
                    ('blackoutWindows', 'blackoutWindows')):
                value = (args or {}).get(arg_name)
                if value is not None:
                    body[body_name] = value
            result = _http_json('POST', SELF_BASE_URL,
                                '/api/selfedit/schedule', body, agent_key,
                                timeout=1800)
            if isinstance(result, dict) and result.get('error'):
                lines = [f'schedule_dormant_apply refused: {result["error"]}']
                for c in (result.get('conflicts') or [])[:8]:
                    lines.append(f"- {c.get('kind')}: {c.get('name')}")
                return '\n'.join(lines)
            if isinstance(result, dict) and result.get('id'):
                return (f'{result.get("message", "window requested.")} '
                        f'[window {result.get("id")} status: '
                        f'{result.get("status")}]')
            return ('schedule_dormant_apply returned an unexpected response '
                    '(nothing scheduled).')
        if name == 'selfedit_fix':
            path = ((args or {}).get('path') or '').strip()
            find = (args or {}).get('find')
            replace = (args or {}).get('replace')
            summary = (args or {}).get('summary')
            if not path or not find or replace is None:
                return ('selfedit_fix requires path, find, replace and a '
                        'summary of what this correction changes.')
            body = {'agentId': agent_id, 'path': path, 'find': find,
                    'replace': replace, 'summary': summary}
            result = _http_json('POST', SELF_BASE_URL,
                                '/api/selfedit/fix/edit', body, agent_key,
                                timeout=300,
                                extra_headers=_selfedit_fix_headers(agent_id))
            if isinstance(result, dict) and result.get('error'):
                return (f'selfedit_fix not applied: {result["error"]} '
                        f'[fixes only work while a remediation session is '
                        f'open -- check self_edit_status]')
            if isinstance(result, dict) and result.get('id'):
                return (f'Correction applied inside the change window '
                        f'[{result.get("id")}] -- run selfedit_recheck next '
                        f'to test the tree.')
            return 'selfedit_fix returned an unexpected response (nothing changed).'
        if name == 'selfedit_recheck':
            result = _http_json('POST', SELF_BASE_URL,
                                '/api/selfedit/fix/checks',
                                {'agentId': agent_id}, agent_key, timeout=900,
                                extra_headers=_selfedit_fix_headers(agent_id))
            if isinstance(result, dict) and result.get('error'):
                return f'selfedit_recheck: {result["error"]}'
            if isinstance(result, dict):
                if result.get('tests') == 'pass':
                    return ('Repo test gate PASSES now: your failing edits '
                            'recovered to applied and the remediation '
                            'session is closed.')
                tail = (result.get('tail') or '')[-1200:]
                return ('Repo test gate still FAILING:\n' + tail)
            return 'selfedit_recheck returned an unexpected response.'
        if name == 'selfedit_read':
            path = ((args or {}).get('path') or '').strip()
            result = _http_json('POST', SELF_BASE_URL, '/api/selfedit/read',
                                {'agentId': agent_id, 'path': path,
                                 'query': (args or {}).get('query') or '',
                                 'context': (args or {}).get('context') or 40},
                                agent_key, timeout=60,
                                extra_headers=_selfedit_fix_headers(agent_id))
            if isinstance(result, dict) and result.get('error'):
                return f'selfedit_read: {result["error"]}'
            if isinstance(result, dict) and result.get('modules'):
                lines = [f'READABLE self-edit surface ({len(result["modules"])} '
                         'modules, name + size):']
                for m in result['modules'][:60]:
                    lines.append(f"- {m.get('path')} ({m.get('bytes')} bytes)")
                if len(result['modules']) > 60:
                    lines.append(f'... {len(result["modules"]) - 60} more; '
                                 'use selfedit_read with a path')
                return '\n'.join(lines)
            if isinstance(result, dict) and result.get('text'):
                note = (f"[sha256 {result.get('sha256')}, "
                        f"{result.get('totalBytes')} bytes total"
                        + (', truncated' if result.get('truncated') else '')
                        + ']')
                if result.get('matchLine'):
                    note += (f" [lines {result.get('startLine')}-"
                             f"{result.get('endLine')} of "
                             f"{result.get('totalLines')}; match at "
                             f"{result.get('matchLine')}]")
                return f'{result.get("path")} {note}\n{(result.get("text") or "")}'
            return 'selfedit_read returned an unexpected response.'
        if name == 'selfedit_validate':
            window_id = ((args or {}).get('windowId') or '').strip()
            if not window_id or 'approved' not in (args or {}):
                return ('selfedit_validate requires windowId and approved '
                        '(true pushes the validated bytes to main, false '
                        'rolls the window back).')
            body = {'agentId': agent_id, 'windowId': window_id,
                    'approved': bool(args.get('approved')),
                    'note': (args.get('note') or '')[:800],
                    'issues': bool(args.get('issues'))}
            result = _http_json('POST', SELF_BASE_URL,
                                '/api/selfedit/validate', body, agent_key,
                                timeout=120,
                                extra_headers=_selfedit_validate_headers(agent_id))
            if isinstance(result, dict) and result.get('error'):
                return (f'selfedit_validate refused: {result["error"]} '
                        f'[only the change request\'s validator may decide, '
                        f'and only while the window holds]')
            if isinstance(result, dict) and result.get('status'):
                detail = result.get('sha') and f' ({result.get("sha")})' or ''
                return (f'Change request {window_id}: {result.get("status")}'
                        f'{detail} -- {result.get("message", "")}')
            return 'selfedit_validate returned an unexpected response.'
        if name == 'veto_change_request':
            window_id = ((args or {}).get('windowId') or '').strip()
            reason = ((args or {}).get('reason') or '').strip()
            if not window_id or not reason:
                return ('veto_change_request requires windowId and a reason.')
            body = {'agentId': agent_id, 'windowId': window_id,
                    'reason': reason[:800]}
            result = _http_json('POST', SELF_BASE_URL,
                                '/api/selfedit/window/veto', body, agent_key,
                                timeout=120)
            if isinstance(result, dict) and result.get('error'):
                return (f'veto_change_request refused: {result["error"]} '
                        '[only the player session or the chief of staff may '
                        'veto]')
            if isinstance(result, dict) and result.get('status'):
                return (f'Change request {window_id}: {result.get("status")} '
                        f'-- {result.get("reason", "")}')
            return 'veto_change_request returned an unexpected response.'
        if name == 'policy_edit':
            key = ((args or {}).get('key') or '').strip()
            value = (args or {}).get('value')
            reason = (args or {}).get('reason')
            if not key or value is None:
                return ('policy_edit requires key and value.')
            if not (reason or '').strip():
                return ('policy_edit requires reason: say WHY the new value '
                        'helps the tank -- the reviewer judges the reason.')
            body = {'agentId': agent_id, 'key': key, 'value': value,
                    'reason': reason}
            result = _http_json('POST', SELF_BASE_URL,
                                '/api/policy/agent_edit', body, agent_key,
                                timeout=120)
            if isinstance(result, dict) and result.get('error'):
                return f'policy_edit: {result["error"]}'
            if isinstance(result, dict) and result.get('ok'):
                previous = result.get('previous')
                applied = result.get('value')
                return (f'Policy applied after review ({result.get("key")!r} '
                        f'{previous!r} -> {applied!r}); bound modules '
                        'rebind already, no restart needed.')
            return 'policy_edit returned an unexpected response.'
        raise ValueError(f'unknown tool: {name}')
    execute_tool.observe_urls = discovered.observe
    execute_tool.discovered = discovered
    return execute_tool
