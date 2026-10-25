

def _mcp_tool_text(name, args, agent_id=None):
    """Run one MCP tool and wrap its result as external data.

    The MCP layer owns protocol and per-tool policy. This function owns the
    boundary wrapping, so a tool description or payload can never arrive at a
    model as trusted instructions.
    """
    if mcp_client is None:
        return 'MCP support is not available on this server.'
    try:
        if name == 'mcp_list_servers':
            raw = mcp_client.servers_text()
            source = 'operator-registered MCP server metadata'
        elif name == 'mcp_list_tools':
            server = str((args or {}).get('server') or '')
            raw = mcp_client.list_tools_text(
                server, refresh=bool((args or {}).get('refresh')))
            source = f'MCP tool catalog for server {server or "?"}'
        else:
            server = str((args or {}).get('server') or '')
            tool = str((args or {}).get('tool') or '')
            out = mcp_client.run_tool(
                server, tool, (args or {}).get('arguments') or {},
                read_only=(name == 'mcp_read'),
                max_bytes=(args or {}).get('maxBytes'))
            try:
                log_action(agent_id or 'unknown', 'mcp_tool', {
                    'server': server,
                    'tool': tool,
                    'tier': out.get('tier'),
                    'ok': bool(out.get('ok')),
                    'policyDenied': bool(out.get('policy_denied')),
                }, authorized=not bool(out.get('policy_denied')))
            except Exception:
                pass
            source = f'MCP server {server} tool {tool}'
            if not out.get('ok'):
                reason = out.get('error') or out.get('text') or 'MCP call failed'
                if out.get('policy_denied'):
                    return (f'MCP call refused: {reason} '
                            '[policy denial -- do not retry the same call]')
                wrapped, _nonce, _tag, instruction = wrap_external_content(
                    reason, source)
                retry_note = ('[do not retry the same call]'
                              if out.get('retryable') is False
                              else '[this may be transient; retry at most once]')
                return f'MCP call failed. {instruction}\n\n{wrapped}\n{retry_note}'
            raw = out.get('text') or '(empty MCP result)'
    except Exception as exc:
        wrapped, _nonce, _tag, instruction = wrap_external_content(
            str(exc), 'MCP server or client error')
        return (f'MCP call failed. {instruction}\n\n{wrapped}\n'
                '[do not loop on the same call]')
    wrapped, _nonce, _tag, instruction = wrap_external_content(raw, source)
    return f'{instruction}\n\n{wrapped}'


def _wrap_external_text(raw, source):
    wrapped, _nonce, _tag, instruction = wrap_external_content(raw, source)
    return f'{instruction}\n\n{wrapped}'


def _durable_tool_text(name, args, agent_id=None):
    if durable_streams is None:
        return 'Durable event stream support is not available on this server.'
    args = args or {}
    try:
        if name == 'list_event_streams':
            raw = durable_streams.streams_text()
            source = 'operator-registered durable event stream metadata'
        elif name == 'read_event_stream':
            stream = str(args.get('stream') or '')
            out = durable_streams.read_stream(
                stream, count=args.get('max'),
                block_ms=args.get('blockMs'), wait_s=args.get('waitS'),
                max_bytes=args.get('maxBytes'))
            source = f'durable event stream {stream or "?"}'
            try:
                log_action(agent_id or 'unknown', 'event_stream_read', {
                    'stream': stream,
                    'count': len(out.get('messages') or []),
                    'ok': bool(out.get('ok')),
                }, authorized=bool(out.get('ok')))
            except Exception:
                pass
            if not out.get('ok'):
                return _wrap_external_text(
                    'Event stream read failed: ' + (out.get('error') or 'unknown error'),
                    source)
            raw = out.get('text') or '(no messages)'
        elif name == 'ack_event_stream':
            stream = str(args.get('stream') or '')
            out = durable_streams.ack_stream(stream, args.get('receipts') or [])
            source = f'durable event stream {stream or "?"}'
            try:
                log_action(agent_id or 'unknown', 'event_stream_ack', {
                    'stream': stream,
                    'acked': out.get('acked', out.get('deleted', 0)),
                    'ok': bool(out.get('ok')),
                }, authorized=bool(out.get('ok')))
            except Exception:
                pass
            if not out.get('ok'):
                reason = out.get('error') or out.get('text') or 'unknown error'
                if out.get('errors'):
                    reason += ' | ' + '; '.join(str(e) for e in out['errors'])
                return _wrap_external_text(
                    'Event stream ack failed: ' + reason, source)
            raw = out.get('text') or 'acknowledged'
        else:
            return 'Unknown durable event stream tool.'
    except Exception as exc:
        return _wrap_external_text(
            f'Event stream call failed: {exc}', 'durable event stream error')
    return _wrap_external_text(raw, source)


def _object_tool_text(name, args, agent_id=None):
    if object_store is None:
        return 'Object storage support is not available on this server.'
    args = args or {}
    try:
        if name == 'list_object_stores':
            raw = object_store.stores_text()
            source = 'operator-registered object store metadata'
        else:
            store = str(args.get('store') or '')
            action = str(args.get('action') or '')
            source = f'object store {store or "?"} action {action or "?"}'
            if name == 'read_object_store':
                out = object_store.read_store(store, action, args, agent_id)
                try:
                    log_action(agent_id or 'unknown', 'object_store_read', {
                        'store': store,
                        'action': action,
                        'key': str(args.get('key') or ''),
                        'ok': bool(out.get('ok')),
                    }, authorized=bool(out.get('ok')))
                except Exception:
                    pass
            elif name == 'write_object_store':
                out = object_store.write_store(store, action, args, agent_id)
                try:
                    log_action(agent_id or 'unknown', 'object_store_write', {
                        'store': store,
                        'action': action,
                        'key': str(args.get('key') or ''),
                        'ok': bool(out.get('ok')),
                    }, authorized=bool(out.get('ok')))
                except Exception:
                    pass
            else:
                return 'Unknown object store tool.'
            if not out.get('ok'):
                return _wrap_external_text(
                    'Object store call failed: ' + (out.get('error') or 'unknown error'),
                    source)
            raw = out.get('text') or '(empty object store result)'
    except Exception as exc:
        return _wrap_external_text(
            f'Object store call failed: {exc}', 'object store error')
    return _wrap_external_text(raw, source)


def _cli_tool_text(name, args, agent_id=None):
    if cli_tools is None:
        return 'CLI transport support is not available on this server.'
    args = args or {}
    try:
        if name == 'list_cli_tools':
            raw = cli_tools.tools_text()
            source = 'operator-registered CLI tool metadata'
        else:
            tool = str(args.get('tool') or '')
            source = f'CLI tool {tool or "?"}'
            out = cli_tools.run_tool(
                tool, args.get('args') or [],
                read_only=(name == 'read_cli'), max_bytes=args.get('maxBytes'))
            try:
                log_action(agent_id or 'unknown', 'cli_run', {
                    'tool': tool,
                    'exitCode': out.get('exitCode'),
                    'ok': bool(out.get('ok')),
                    'truncated': bool(out.get('truncated')),
                }, authorized=bool(out.get('ok')))
            except Exception:
                pass
            if not out.get('ok'):
                reason = out.get('text') or out.get('error') or 'CLI call failed'
                return _wrap_external_text('CLI call failed: ' + reason, source)
            raw = out.get('text') or '(empty CLI output)'
    except Exception as exc:
        return _wrap_external_text(f'CLI call failed: {exc}', 'CLI transport error')
    return _wrap_external_text(raw, source)
