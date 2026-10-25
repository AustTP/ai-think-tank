# parts: server/serve_parts
"""Ordered source parts for :mod:`serve`."""
import os as _os
_here = globals().get('__file__') or __file__
_parts = _os.path.join(_os.path.dirname(_os.path.abspath(_here)), 'server', 'serve_parts')
for _fn in sorted(f for f in _os.listdir(_parts) if f.endswith('.py')):
    _target = _os.path.join(_parts, _fn)
    with open(_target, encoding='utf-8') as _fh:
        exec(compile(_fh.read(), _target, 'exec'))

if __name__ == '__main__':
    from uvicorn import Server, Config
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8936
    # --max-idle-minutes N: exit the server after N minutes with no HTTP
    # request at all (any request, authed or not -- touched in middleware).
    # The enforced form of "kill the server when idle"; 0 = disabled.
    _idle_arg = [a for a in sys.argv if a.startswith('--max-idle-minutes')]
    if _idle_arg:
        try:
            _MAX_IDLE_MINUTES = float(_idle_arg[0].split('=')[1])
        except (IndexError, ValueError):
            print('[args] --max-idle-minutes=<minutes> expected (0 disables); ignoring', flush=True)
    # --host=<ip>: bind somewhere other than localhost, e.g. a Tailscale
    # address so the real (already-login-gated) UI is reachable from other
    # devices on your tailnet without opening anything to the public
    # internet. Defaults to 127.0.0.1 (unchanged) so nothing is exposed
    # unless this is passed explicitly. Prefer a specific Tailscale IP
    # (`tailscale ip -4`) over 0.0.0.0 -- the latter also binds your regular
    # LAN interface, not just the tailnet.
    _host_arg = [a for a in sys.argv if a.startswith('--host=')]
    bind_host = _host_arg[0].split('=', 1)[1] if _host_arg else '127.0.0.1'
    # SELF_BASE_URL must point at THIS process: every use is a self-loopback
    # (the research crawl's browse/evidence/save calls, chat follow-ups,
    # allowlist requests). The .env value is the operator's guess at boot, but
    # it drifts the moment the port or bind host changes -- and a dead
    # self-loopback is silent: the crawl's evidence POST degrades to
    # 'recorded' and the evidence ledger stays empty. Derive the URL from the
    # real bind instead, and warn when .env disagrees.
    _bound_base, _env_matches = _derive_self_base_url(bind_host, port)
    if not _env_matches:
        print(f'[env] SELF_BASE_URL in .env is {SELF_BASE_URL!r} but this process binds '
              f'{_bound_base}; self-loopback calls will use {_bound_base}', flush=True)
        SELF_BASE_URL = _bound_base
        # Escalation Approve/Deny links are tapped by the player; when we
        # didn't follow an explicit ESCALATION_BASE_URL, rebase it with
        # SELF_BASE_URL so the links hit this process, not a stale port.
        if not _load_env().get('ESCALATION_BASE_URL'):
            ESCALATION_BASE_URL = _bound_base
    init_db()
    # flush=True -- real bug hit while building this: stdout is fully
    # buffered (not line-buffered) once redirected to a file/pipe rather
    # than a real terminal, so a one-time secret like this could sit
    # invisible in the buffer indefinitely if the process kept running
    # (or got lost entirely on an unclean stop) instead of ever reaching
    # the log file.
    if _GENERATED_PASSWORD:
        print('=' * 60, flush=True)
        print('[auth] First run -- admin account created.', flush=True)
        print(f'[auth] username: {ADMIN_USERNAME}', flush=True)
        print(f'[auth] password: {_GENERATED_PASSWORD}  (shown once -- save it)', flush=True)
        print('=' * 60, flush=True)
    else:
        print(f'[auth] admin account: {ADMIN_USERNAME} (password already set -- not shown)', flush=True)
    if _GENERATED_DEVICE_KEY:
        print('=' * 60, flush=True)
        print('[device] First run -- device check-in key created.', flush=True)
        print(f'[device] X-Device-Key: {_GENERATED_DEVICE_KEY}  (shown once -- save it)', flush=True)
        print('=' * 60, flush=True)
    if EXECUTION_ENABLED:
        try:
            ensure_sandbox_networking()
        except Exception as e:
            print(f'[sandbox] networking setup failed (execution will error until Docker is available): {e}')
    print(f'World dev server (FastAPI, with /save + /api/state) on http://{bind_host}:{port}')
    if _MAX_IDLE_MINUTES:
        print(f'[idle] auto-sleep armed: think tank pauses after {_MAX_IDLE_MINUTES}m with no HTTP request; stays bound; any request wakes it', flush=True)
    server = Server(Config(app, host=bind_host, port=port, log_level='warning'))

    async def _arm_idle_and_run():
        if _MAX_IDLE_MINUTES:
            # Idle from the moment we're armed, not from the first request: a
            # server started and then abandoned (page never even loaded) must
            # still go dormant instead of running forever.
            global _LAST_REQUEST_TIME
            if _LAST_REQUEST_TIME is None:
                _LAST_REQUEST_TIME = time.time()
            asyncio.create_task(_idle_shutdown_loop())
        # The dormancy window loop runs whether or not idle-watching is
        # armed: it owns dormancy-window firing AND the dormant-only
        # apply_pass for armed self-edits.
        asyncio.create_task(_dormancy_window_loop())
        await server.serve()

    try:
        asyncio.run(_arm_idle_and_run())
    finally:
        # Clean shutdown: persist a fresh DB checkpoint so state survives the
        # shell exit (and any later crash of the OLD manual-copy-only backup).
        try:
            _backup_think_tank_db()
        except Exception as e:
            print(f'[backup] shutdown checkpoint failed: {e}', flush=True)


# ---------------------------------------------------------------------------
# Content-executor re-exports (phase-consolidation). The per-room executors + the
# router moved to content.py (see there for the "why"); they are re-exported
# here so `serve._run_*` / `serve._server_content_dispatcher` stay the stable
# public surface the tests (and the lifespan wiring above) call. `content` is
# imported lazily from here (bottom of module) rather than the top: content.py
# itself does `import serve as _serve`, so a top-level import of content from
# serve would be the classic import-time cycle. By the time this line runs the
# serve module is fully populated, and content's `_serve.X` lookups resolve at
# call time, never at import time -- no cycle.
# ---------------------------------------------------------------------------
_CONTENT_REEXPORTS = {
    '_server_content_dispatcher', '_run_research_content', '_run_media_content',
    '_run_skill_review_content', '_run_bank_content',
    '_run_research_project_content', '_run_research_bare_content',
    '_run_quality_pipeline', '_run_coding_content', '_run_review_content',
    '_run_product_build_content', '_run_spike_content', '_run_workroom_content',
    '_quality_pipeline_steps', 'CODING_STANDARDS_PROMPT',
}


def __getattr__(name):
    """Lazy content re-exports. content imports serve at module load, so eager
    re-exports here fail when a test imports content first. Resolve on first
    access after that import has completed."""
    if name in _CONTENT_REEXPORTS:
        import content as _content
        value = getattr(_content, name)
        globals()[name] = value
        return value
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
