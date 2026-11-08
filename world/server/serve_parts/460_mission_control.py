# Tank Mission Control: sealed live-query realtime over the tank's own
# Postgres. Reverse-engineered from @neon/realtime (Apache-2.0): the
# server seals allowlisted queries into short-lived AES-GCM capabilities,
# the browser subscribes over one WebSocket, and pg_notify-driven re-runs
# push fresh rows -- no panel ever polls. Read-only toward the tank: the
# projection (mc_projection) is fed after committed saves, fails silently
# and is counted, and never raises into the tank's save path.

_MC_MODULE_READY = False

try:
    import server.mc_projection as _mc_projection  # noqa: E402
    import server.mc_realtime as _mc_realtime  # noqa: E402
    _MC_MODULE_READY = True
except Exception as _mc_import_exc:  # pragma: no cover - layout guard
    print(f'[mc-realtime] module import failed: '
          f'{type(_mc_import_exc).__name__}: {_mc_import_exc}', flush=True)
    _mc_projection = None
    _mc_realtime = None

if _MC_MODULE_READY:
    try:
        _mc_realtime.load_queries()
    except FileNotFoundError:
        print('[mc-realtime] no mc_queries dir yet; panels disabled',
              flush=True)

_MC_DEBOUNCE_S = 0.25
_MC_LISTENER = {'thread': None, 'lock': __import__('threading').Lock(),
                'stop': False}
_MC_SUBS = {'lock': __import__('threading').Lock(),
            # fingerprint -> set of websocket connections
            'by_fp': {},
            # the serve event loop; pushes from the listener thread go
            # through call_soon_threadsafe (a bare ensure_future from a
            # worker thread raises 'no running event loop' and the push
            # silently dies)
            'loop': None}
_MC_FAILURES = {'seal': 0, 'ws': 0, 'notify': 0}


def _mc_ensure_schema():
    """Idempotent projection schema + the action_log notify trigger.

    The trigger is the ONLY thing Mission Control adds to a primary
    table: INSERT-only, one pg_notify per insert, negligible at the
    action log's write rate. Postgres-only; skipped on SQLite.
    """
    import db_backend
    conn = db_backend.connect()
    try:
        _mc_projection.ensure_schema(conn)
        if db_backend.is_postgres():
            conn.execute('''
                CREATE OR REPLACE FUNCTION mc_notify_action_log() RETURNS
                trigger AS $$
                BEGIN
                  PERFORM pg_notify('mc_changed',
                    '{"tables":["action_log"]}');
                  RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
            ''')
            conn.execute('''
                DROP TRIGGER IF EXISTS mc_action_log_notify ON action_log
            ''')
            conn.execute('''
                CREATE TRIGGER mc_action_log_notify AFTER INSERT ON
                action_log FOR EACH STATEMENT EXECUTE FUNCTION
                mc_notify_action_log()
            ''')
            conn.execute('''
                CREATE OR REPLACE FUNCTION mc_notify_generic() RETURNS
                trigger AS $$
                BEGIN
                  PERFORM pg_notify('mc_changed',
                    '{"tables":["' || TG_TABLE_NAME || '"]}');
                  RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
            ''')
            for _t in ('decision_archive', 'confirmation_proposals'):
                conn.execute(f'''
                    DROP TRIGGER IF EXISTS mc_{_t}_notify ON {_t}
                ''')
                conn.execute(f'''
                    CREATE TRIGGER mc_{_t}_notify AFTER INSERT ON {_t}
                    FOR EACH STATEMENT EXECUTE FUNCTION mc_notify_generic()
                ''')
        conn.commit()
    finally:
        conn.close()


def _mc_changed_tables(payload_text):
    try:
        tables = (json.loads(payload_text) or {}).get('tables') or []
        return {str(t) for t in tables}
    except Exception:
        return set()


def _mc_listener_loop():
    """One dedicated Postgres connection: LISTEN mc_changed, debounce, and
    re-run every subscribed live query whose declared tables intersect the
    change. The tank's writers only ever pg_notify -- they never know
    about websockets."""
    import select
    import psycopg2
    import psycopg2.extensions
    import db_backend
    dsn = db_backend._dsn() or ''
    if not dsn:
        return
    while not _MC_LISTENER['stop']:
        conn = None
        try:
            conn = psycopg2.connect(dsn, connect_timeout=10)
            conn.set_isolation_level(
                psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            cur = conn.cursor()
            cur.execute('LISTEN mc_changed')
            pending = {}
            last_sent = {}
            while not _MC_LISTENER['stop']:
                # Drain timeouts too: the debounce must fire even when no
                # notify arrives in the window (a select timeout used to
                # skip the drain and freeze pending tables forever).
                ready = select.select([conn], [], [], 0.25)
                if ready[0]:
                    conn.poll()
                    while conn.notifies:
                        n = conn.notifies.pop(0)
                        tables = _mc_changed_tables(n.payload)
                        for t in tables:
                            pending[t] = time.time()
                now = time.time()
                due = [t for t in list(pending)
                       if now - pending[t] >= _MC_DEBOUNCE_S]
                if not due:
                    continue
                for t in due:
                    del pending[t]
                _mc_push_changed(due, last_sent)
        except Exception as exc:
            _MC_FAILURES['notify'] += 1
            if not _MC_LISTENER['stop']:
                print(f'[mc-realtime] listener retrying: '
                      f'{type(exc).__name__}: {exc}', flush=True)
                time.sleep(3.0)
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass


def _mc_push_changed(tables, last_sent):
    """Re-run and push every subscribed query touching any of `tables`."""
    with _MC_SUBS['lock']:
        targets = [(fp, name, params, sockets)
                   for fp, subs in list(_MC_SUBS['by_fp'].items())
                   for (name, params, sockets) in list(subs.values())]
    if not targets:
        return
    now = time.time()
    for fp, name, params, sockets in targets:
        if not (_mc_realtime.query_tables(name) & set(tables)):
            continue
        if now - float(last_sent.get(fp) or 0) < _MC_DEBOUNCE_S:
            continue
        last_sent[fp] = now
        try:
            rows = _mc_realtime.run_query(name, params)
        except Exception as exc:
            _MC_FAILURES['ws'] += 1
            print(f'[mc-realtime] re-run {name} failed: '
                  f'{type(exc).__name__}: {exc}', flush=True)
            continue
        payload = json.dumps({'type': 'update', 'fp': fp, 'rows': rows})
        loop = _MC_SUBS['loop']
        for ws in list(sockets):
            if loop is None or loop.is_closed():
                _MC_FAILURES['ws'] += 1
                continue
            try:
                loop.call_soon_threadsafe(
                    asyncio.ensure_future, ws.send_text(payload))
            except Exception:
                _MC_FAILURES['ws'] += 1


def _mc_register(fp, name, params, ws):
    with _MC_SUBS['lock']:
        _MC_SUBS['by_fp'].setdefault(fp, {})[id(ws)] = (name, params, [ws])


def _mc_unregister(fp, ws):
    with _MC_SUBS['lock']:
        subs = _MC_SUBS['by_fp'].get(fp)
        if subs:
            subs.pop(id(ws), None)
            if not subs:
                del _MC_SUBS['by_fp'][fp]


def _mc_ensure_listener():
    with _MC_LISTENER['lock']:
        if _MC_LISTENER['thread'] is not None:
            return
        import db_backend
        if not db_backend.is_postgres():
            return
        t = threading.Thread(target=_mc_listener_loop, daemon=True,
                             name='mc-realtime-listener')
        _MC_LISTENER['thread'] = t
        t.start()


_mc_orig_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _mc_lifespan(app_):
    if _MC_MODULE_READY and not READ_ONLY_MODE:
        try:
            _mc_ensure_schema()
            _mc_ensure_listener()
            print('[mc-realtime] listener started', flush=True)
        except Exception as exc:
            print(f'[mc-realtime] startup deferred: '
                  f'{type(exc).__name__}: {exc}', flush=True)
    _MC_SUBS['loop'] = asyncio.get_running_loop()
    async with _mc_orig_lifespan(app_):
        yield


app.router.lifespan_context = _mc_lifespan


@app.post('/api/mc/seal')
async def _mc_seal(request: Request):
    """Seal an allowlisted live query for the caller. Requires a player
    session, like every other write-adjacent lane."""
    if not verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return PlainTextResponse('player session required', status_code=401)
    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse('json body required', status_code=400)
    name = body.get('query')
    params = body.get('params') or []
    if not _MC_MODULE_READY:
        return PlainTextResponse('mission control disabled', status_code=503)
    try:
        sealed = _mc_realtime.seal(name, params)
    except KeyError:
        _MC_FAILURES['seal'] += 1
        return PlainTextResponse(f'unknown live query: {name}',
                                 status_code=404)
    except (TypeError, ValueError) as exc:
        _MC_FAILURES['seal'] += 1
        return PlainTextResponse(str(exc), status_code=400)
    return sealed


@app.websocket('/api/mc/ws')
async def _mc_ws(websocket: WebSocket):
    if not _MC_MODULE_READY:
        await websocket.close(code=1011)
        return
    if not verify_session(websocket.cookies.get(SESSION_COOKIE_NAME)):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    try:
        while True:
            msg = await websocket.receive_json()
            cap = str(msg.get('capability') or '')
            try:
                name, params, fp = _mc_realtime.unseal(cap)
            except ValueError as exc:
                _MC_FAILURES['ws'] += 1
                await websocket.send_json({'type': 'error',
                                           'detail': str(exc)})
                continue
            rows = await asyncio.to_thread(
                _mc_realtime.run_query, name, params)
            await websocket.send_json({'type': 'snapshot', 'fp': fp,
                                       'query': name, 'rows': rows})
            _mc_register(fp, name, params, websocket)
    except WebSocketDisconnect:
        pass
    finally:
        with _MC_SUBS['lock']:
            for subs in _MC_SUBS['by_fp'].values():
                subs.pop(id(websocket), None)
            for fp in [fp for fp, subs in _MC_SUBS['by_fp'].items()
                       if not subs]:
                del _MC_SUBS['by_fp'][fp]


_MC_PAGE_CACHE = {'mtime': None, 'html': None}


@app.get('/mc')
async def _mc_page():
    """The Mission Control console (same login gate as the main UI)."""
    path = os.path.join(ROOT, 'mc', 'index.html')
    try:
        mtime = os.path.getmtime(path)
        if _MC_PAGE_CACHE['mtime'] != mtime:
            with open(path, encoding='utf-8') as fh:
                _MC_PAGE_CACHE['html'] = fh.read()
            _MC_PAGE_CACHE['mtime'] = mtime
        return Response(_MC_PAGE_CACHE['html'],
                        media_type='text/html; charset=utf-8')
    except OSError:
        raise HTTPException(status_code=404, detail='mission control UI '
                            'not found')


@app.get('/api/mc/status')
async def _mc_status():
    return {
        'enabled': _MC_MODULE_READY,
        'queries': sorted((_mc_realtime._queries() or {}).keys())
        if _MC_MODULE_READY else [],
        'failures': dict(_MC_FAILURES),
        'subscriptions': sum(len(s) for s in _MC_SUBS['by_fp'].values()),
    }



if _MC_MODULE_READY:
    _mc_orig_save = _save_state_to_db_sync

    def _mc_save_and_project(data):
        result = _mc_orig_save(data)
        try:
            _mc_projection.sync(data if isinstance(data, dict) else {})
        except Exception:
            _mc_projection.count_failure()
        return result

    _save_state_to_db_sync = _mc_save_and_project



_MC_CI = {'at': 0.0, 'data': None}


@app.get('/api/mc/ci')
async def _mc_ci():
    """Recent GitHub Actions runs for the tank repo, via the local gh CLI.
    60 s cache; the gh token never reaches the browser."""
    import subprocess
    if time.time() - float(_MC_CI['at'] or 0) < 60 and _MC_CI['data']:
        return _MC_CI['data']
    try:
        out = await asyncio.to_thread(
            subprocess.run,
            ['gh', 'run', 'list', '--repo', 'AustTP/ai-think-tank',
             '--limit', '8', '--json',
             'databaseId,status,conclusion,displayTitle,createdAt,workflowName'],
            capture_output=True, text=True, timeout=15)
        runs = json.loads(out.stdout) if out.returncode == 0 else []
        _MC_CI['data'] = {'ok': out.returncode == 0, 'runs': runs}
    except Exception as exc:
        _MC_CI['data'] = {'ok': False, 'runs': [], 'error': str(exc)}
    _MC_CI['at'] = time.time()
    return _MC_CI['data']
