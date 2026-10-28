"""Free-pool service switches (Gemini / Groq / OpenRouter-free).

Two authorities, mirroring /api/supervision's caller discipline:
  * TEAM -- each director switches a service off for their own team's work
    (a team IS a director: team id == director id, so the team entry is
    keyed by the director's own id, exactly like directorSupervision).
  * GLOBAL -- the chief of staff (an admin agent) or the player switches a
    service off for the whole tank.
The global switch OVERRIDES the team switch: nothing a team does can
re-enable a globally blocked service, so an issue encountered in any of the
three providers can be silenced tank-wide in one move while teams keep
their own narrower, independent switches.

The fourth service, 'paid', is not a pool endpoint: it switches off the LOW
band's paid leg (the DB tier pick or a @provider:low pin) for the scope --
the test/emergency lever that proves the free pool carries low-band work on
its own. Mid/high/coding calls are untouched by it.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import core.governance as governance

router = APIRouter()

SERVICES = ('gemini', 'groq', 'openrouter', 'paid')


def _caller(request):
    import serve
    if serve.verify_session(request.cookies.get(serve.SESSION_COOKIE_NAME)):
        return 'player'
    requester = serve._resolve_requester(request)
    if requester and serve.verify_agent_key(
            requester, request.headers.get('X-Agent-Key')) is True:
        return requester
    return None


def _policy_of(state):
    raw = state.get('freePoolPolicy') or {}
    return {
        'globalDisabled': {s: bool((raw.get('globalDisabled') or {}).get(s))
                           for s in SERVICES},
        'teams': raw.get('teams') or {},
    }


@router.get('/api/free-pool-policy')
async def get_free_pool_policy(request: Request):
    import serve
    actor = _caller(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    state = serve.get_state_from_db() or {}
    policy = _policy_of(state)
    if actor != 'player' and not governance.is_admin(state, actor):
        # A director sees their own team entry only; everyone else sees no
        # team entries (they cannot write any either).
        policy['teams'] = ({actor: policy['teams'].get(actor) or {}}
                           if governance.is_director(state, actor) else {})
    return JSONResponse({'ok': True, 'services': list(SERVICES),
                         'globalDisabled': policy['globalDisabled'],
                         'teams': policy['teams'],
                         'status': serve._freepool_status()})


@router.put('/api/free-pool-policy')
async def put_free_pool_policy(request: Request):
    import serve
    actor = _caller(request)
    if not actor:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    service = (body.get('service') or '').strip()
    if service not in SERVICES:
        return JSONResponse({'error': f'service must be one of {", ".join(SERVICES)}'},
                            status_code=400)
    disabled = bool(body.get('disabled'))
    scope = (body.get('scope') or 'team').strip()
    if scope not in ('global', 'team'):
        return JSONResponse({'error': "scope must be 'global' or 'team'"},
                            status_code=400)
    state = serve.get_state_from_db() or {}
    policy = state.setdefault('freePoolPolicy', {})
    if scope == 'global':
        # The chief of staff (admin) -- or the player above them -- flips the
        # tank-wide switch. A plain director cannot.
        if actor != 'player' and not governance.is_admin(state, actor):
            return JSONResponse({'error': 'only the chief of staff or the player may set the global switch'},
                                status_code=403)
        policy.setdefault('globalDisabled', {})[service] = disabled
        target = {'scope': 'global'}
    else:
        # A team switch is the director's own team: a director may only write
        # their own entry; the player or an admin may write any team's.
        team_id = (body.get('teamId') or actor).strip()
        if actor != 'player' and not governance.is_admin(state, actor):
            team_id = actor
        if not governance.is_director(state, team_id):
            return JSONResponse({'error': f'{team_id} is not a director'},
                                status_code=400)
        policy.setdefault('teams', {}).setdefault(team_id, {})[service] = disabled
        target = {'scope': 'team', 'teamId': team_id}
    serve.save_state_to_db(state)
    # The enforcement cache must not outlive the write: the very next pick
    # sees the new switch.
    serve._invalidate_free_pool_policy_cache()
    serve.log_action(actor, 'free_pool_policy_set',
                     {'service': service, 'disabled': disabled, **target},
                     authorized=True)
    return JSONResponse({'ok': True, 'service': service, 'disabled': disabled,
                         **target})
