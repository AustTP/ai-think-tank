# Tank process control: the chief of staff's stop switch.
#
# The owner can tell the chief of staff to stop any tank service, or all of
# them at once. This endpoint is the governed surface for that order: admin
# agent key required, services restricted to this allowlist (a typo can never
# bootout an unrelated launchd unit), and the serving process answers BEFORE
# it stops so the caller always sees the result.
#
# Services are started again with /api/tank/start (launchctl bootstrap), or by
# the operator: launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
_TANK_SERVICES = {
    'main': 'com.austtp.ai-think-tank',
    'read': 'com.austtp.ai-think-tank-read',
    'village-north': 'com.austtp.ai-think-tank-village-north',
    'sandbox-archive': 'com.austtp.sandbox-archive',
    'tank-consultant': 'com.austtp.tank-consultant',
    'tank-fast': 'com.austtp.tank-fast',
}
_TANK_LAUNCHAGENTS_DIR = os.path.expanduser('~/Library/LaunchAgents')


def _tank_requested_labels(body):
    """Resolve the request body to allowlisted launchd labels. Returns
    (labels, error_response). scope=all means every known tank service."""
    scope = body.get('scope')
    if scope == 'all':
        return sorted(_TANK_SERVICES.values()), None
    labels = []
    for wanted in (body.get('services') or []):
        label = _TANK_SERVICES.get(str(wanted))
        if not label and str(wanted) in _TANK_SERVICES.values():
            label = str(wanted)
        if label and label not in labels:
            labels.append(label)
    if not labels:
        return None, JSONResponse({
            'error': ('No known tank services requested. Use {"scope": "all"} '
                      'or services named from: '
                      + ', '.join(sorted(_TANK_SERVICES))),
        }, status_code=400)
    return labels, None


def _tank_admin_agent(request, body):
    """The caller's agent id when they present a valid ADMIN agent key, else
    None. Distinguishes bad-key (401) from non-admin (403) via the pair:
    returns (agent_id, 'ok' | 'bad-key' | 'not-admin')."""
    agent_id = body.get('agentId')
    if verify_agent_key(agent_id, request.headers.get('X-Agent-Key')) is not True:
        return None, 'bad-key'
    try:
        state = get_state_from_db() or {}
    except Exception:
        return None, 'bad-key'
    if _agent_is_admin(agent_id, state):
        return agent_id, 'ok'
    return agent_id, 'not-admin'


def _tank_auth_response(request, body):
    agent_id, reason = _tank_admin_agent(request, body)
    if reason == 'ok':
        return agent_id, None
    code = 401 if reason == 'bad-key' else 403
    return None, JSONResponse(
        {'error': ('Unauthorized -- valid admin agent key required'
                   if reason == 'bad-key'
                   else 'Admin agents only')}, status_code=code)


def _tank_run_ctl(verb, labels):
    """launchctl bootout|bootstrap each label. Returns per-label results.
    bootstrap takes the plist path; bootout takes the gui domain label."""
    results = []
    for label in labels:
        if verb == 'bootstrap':
            argv = ['/bin/launchctl', 'bootstrap',
                    f'gui/{os.getuid()}',
                    os.path.join(_TANK_LAUNCHAGENTS_DIR, f'{label}.plist')]
        else:
            argv = ['/bin/launchctl', 'bootout',
                    f'gui/{os.getuid()}/{label}']
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=15)
            ok = proc.returncode == 0
            detail = (proc.stderr or proc.stdout or '').strip()[:160]
        except Exception as e:  # noqa: BLE001 -- report, never raise
            ok, detail = False, str(e)[:160]
        results.append({'service': label, 'ok': ok, 'detail': detail})
        try:
            log_action('system', f'tank_{verb}',
                       {'service': label, 'ok': ok, 'detail': detail[:120]},
                       authorized=True)
        except Exception:
            pass
    return results


@app.get('/api/tank/status')
async def tank_status():
    """What is running right now, per allowlisted tank service. Read-only and
    unauthenticated-safe: reports launchctl state only, no state blob."""
    try:
        proc = subprocess.run(['/bin/launchctl', 'list'], capture_output=True,
                              text=True, timeout=15)
        listed = set()
        for line in (proc.stdout or '').splitlines()[1:]:
            parts = line.split('\t')
            if len(parts) >= 3:
                listed.add(parts[2].strip())
    except Exception as e:  # noqa: BLE001 -- status is best-effort
        return JSONResponse({'error': str(e)}, status_code=500)
    services = {}
    for name, label in sorted(_TANK_SERVICES.items()):
        services[name] = {
            'label': label,
            'running': label in listed,
        }
    return JSONResponse({'services': services})


@app.post('/api/tank/stop')
async def tank_stop(request: Request):
    body = await request.json()
    agent_id, err = _tank_auth_response(request, body)
    if err is not None:
        return err
    labels, err = _tank_requested_labels(body)
    if err is not None:
        return err
    # The main tank is THIS process: answer first, stop after a beat so the
    # caller always receives the acknowledgment.
    asyncio.get_event_loop().call_later(
        0.5, lambda: _tank_run_ctl('bootout', labels))
    return JSONResponse({'stopping': labels, 'requestedBy': agent_id})


@app.post('/api/tank/start')
async def tank_start(request: Request):
    body = await request.json()
    agent_id, err = _tank_auth_response(request, body)
    if err is not None:
        return err
    labels, err = _tank_requested_labels(body)
    if err is not None:
        return err
    return JSONResponse({'started': _tank_run_ctl('bootstrap', labels),
                         'requestedBy': agent_id})
