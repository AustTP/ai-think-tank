# Team-definable activation levels (2026-11-11): permanent work that sits
# OUTSIDE the sprint structure -- trading desks, bug-bounty lanes, on-call
# watches -- does not run on a scrum cadence, and the conditions that change
# how it operates are the team's own. A trading desk cares about drawdown;
# a bounty lane cares about a live exploit window; neither is expressible as
# a sprint. So the doctrine is DATA each team owns: named activation levels
# with the team's own trigger, staffing, autonomy, and notification
# semantics. The tank stores the levels and routes the notifications; it
# imposes NO shared severity vocabulary (a team may call them DEFCON 1..5,
# "green/amber/red", or anything else).
#
# Storage: team['doctrine'] on the team row in state (teams are already
# state records; doctrine rides along, retires with the team, and needs no
# second source of truth). Shape:
#   {'origin': 'template:<id>' | 'custom',
#    'levels': [{'id', 'name', 'trigger', 'staffing', 'autonomy',
#                'demote', 'notify'} ...],
#    'currentLevel': <level id>,
#    'changedAt': <epoch>, 'changedBy': <actor>,
#    'history': [{'at', 'level', 'by', 'reason'} ...]}   # capped
#
# Templates live in doctrine_templates.json (operator-authored,
# code-reviewed, same registry shape as api_services.json). A team ADOPTS a
# template -- the levels are COPIED onto the team's record as their live
# doctrine -- and then owns that copy: PUT edits it freely, re-adopting
# resets it. Template file changes never auto-propagate; teams that want the
# new wording re-adopt. The tank stores the levels and routes the
# notifications; it imposes NO shared severity vocabulary (a team may call
# them DEFCON 1..5, "green/amber/red", or anything else).
#
# Who defines it: the team's OWN people (its director, scrum master, or
# members) or the operator. Other teams and random agents cannot.

import json as _json
import os as _os

_DOCTRINE_MAX_LEVELS = 8
_DOCTRINE_HISTORY_CAP = 50
_DOCTRINE_TEXT_CAP = 300

DOCTRINE_TEMPLATES_PATH = _os.path.join(ROOT, 'doctrine_templates.json')


def _team_doctrine_normalize_levels(levels):
    """Validate a levels declaration into doctrine shape. Returns
    (levels, None) or (None, error). Ids and names are required, unique, and
    bounded; everything else is the team's own free text."""
    if not isinstance(levels, list) or not levels:
        return None, 'levels must be a non-empty array'
    if len(levels) > _DOCTRINE_MAX_LEVELS:
        return None, f'at most {_DOCTRINE_MAX_LEVELS} levels'
    out, seen = [], set()
    for raw in levels:
        if not isinstance(raw, dict):
            return None, 'each level must be an object'
        lid = str(raw.get('id') or '').strip()[:40]
        name = str(raw.get('name') or '').strip()[:60]
        if not lid or not name:
            return None, 'every level needs an id and a name'
        if lid in seen:
            return None, f'duplicate level id "{lid}"'
        seen.add(lid)
        out.append({
            'id': lid,
            'name': name,
            'trigger': str(raw.get('trigger') or '').strip()[:_DOCTRINE_TEXT_CAP],
            'staffing': str(raw.get('staffing') or '').strip()[:_DOCTRINE_TEXT_CAP],
            'autonomy': str(raw.get('autonomy') or '').strip()[:_DOCTRINE_TEXT_CAP],
            'demote': str(raw.get('demote') or '').strip()[:_DOCTRINE_TEXT_CAP],
            'notify': bool(raw.get('notify')),
        })
    return out, None


def _team_doctrine_member_actor(request, team):
    """Return the requesting agent id when the agent belongs to THIS team
    (its director, scrum master, or a member); None otherwise. Player/admin
    access is handled separately by the caller."""
    from serve import _agent_id_for_key, verify_agent_key
    key = request.headers.get('X-Agent-Key')
    agent_id = _agent_id_for_key(key)
    if not agent_id or verify_agent_key(agent_id, key) is not True:
        return None
    if team.get('directorId') == agent_id:
        return agent_id
    if team.get('scrumMasterId') == agent_id:
        return agent_id
    members = team.get('members') or []
    if isinstance(members, list) and agent_id in members:
        return agent_id
    return None


def _team_doctrine_authorized(request, team):
    """(authorized, actor): the team's own people or the operator
    (player session / director/admin agent, per _operator_gate)."""
    member = _team_doctrine_member_actor(request, team)
    if member:
        return True, member
    authed, actor = _operator_gate(request)
    if authed:
        return True, actor or 'operator'
    return False, None


def _team_doctrine_find(state, team_id):
    team_id = (team_id or '').strip()
    for t in state.get('teams') or []:
        if isinstance(t, dict) and t.get('id') == team_id:
            return t
    return None


def _doctrine_templates_load():
    """Read + validate every template in doctrine_templates.json. A template
    whose levels fail the same validation the PUT endpoint applies is
    skipped loudly (print), never served half-valid. Returns
    {template_id: {'name', 'description', 'levels'}} or {} on any error."""
    try:
        with open(DOCTRINE_TEMPLATES_PATH, encoding='utf-8') as fh:
            data = _json.load(fh)
    except (OSError, ValueError) as exc:
        print(f'[doctrine] templates unreadable: {exc}', flush=True)
        return {}
    templates = data.get('templates') if isinstance(data, dict) else None
    if not isinstance(templates, dict):
        return {}
    out = {}
    for tid, spec in templates.items():
        if not isinstance(spec, dict):
            continue
        levels, error = _team_doctrine_normalize_levels(spec.get('levels'))
        if error:
            print(f'[doctrine] template "{tid}" invalid: {error}', flush=True)
            continue
        out[str(tid).strip()[:40]] = {
            'name': str(spec.get('name') or tid)[:60],
            'description': str(spec.get('description') or '')[:500],
            'levels': levels,
        }
    return out


def _team_doctrine_get(team_id):
    """Read-only doctrine lookup for status views; {} when absent."""
    try:
        state = get_state_from_db(archive_merge='none') or {}
    except Exception:  # noqa: BLE001 -- status views must never raise
        return {}
    team = _team_doctrine_find(state, team_id)
    doctrine = (team or {}).get('doctrine')
    return doctrine if isinstance(doctrine, dict) else {}


@app.get('/api/team-doctrine/{team_id}')
async def team_doctrine_get(team_id: str, request: Request):
    """Read the team's own activation doctrine. Auth: the team's people or
    the operator -- other teams' doctrines are not everyone's business."""
    state = get_state_from_db(archive_merge='none') or {}
    team = _team_doctrine_find(state, team_id)
    if team is None:
        return JSONResponse({'error': f'no team "{team_id}"'}, status_code=404)
    authed, _actor = _team_doctrine_authorized(request, team)
    if not authed:
        return JSONResponse({'error': 'Unauthorized -- the team\'s own '
                             'members or the operator only'}, status_code=401)
    doctrine = team.get('doctrine')
    return JSONResponse({'teamId': team_id,
                         'teamName': team.get('name') or team_id,
                         'doctrine': doctrine if isinstance(doctrine, dict) else None})


@app.get('/api/doctrine-templates')
async def doctrine_templates_list(request: Request):
    """The reusable doctrine templates any team can adopt. Auth is the
    default middleware gate (session or agent key): picking a doctrine is a
    normal agent-visible capability, editing one stays team-gated."""
    templates = _doctrine_templates_load()
    return JSONResponse({'templates': [
        {'id': tid, 'name': spec['name'], 'description': spec['description'],
         'levels': spec['levels']}
        for tid, spec in sorted(templates.items())
    ]})


@app.post('/api/team-doctrine/{team_id}/adopt')
@_state_writer
async def team_doctrine_adopt(team_id: str, request: Request):
    """Adopt a template: its levels are COPIED onto the team's own record as
    the live doctrine (origin 'template:<id>'). The team then modifies its
    copy freely via PUT; re-adopting resets to the template. Template edits
    in the JSON file never auto-propagate."""
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    team = _team_doctrine_find(state, team_id)
    if team is None:
        return JSONResponse({'error': f'no team "{team_id}"'}, status_code=404)
    authed, actor = _team_doctrine_authorized(request, team)
    if not authed:
        return JSONResponse({'error': 'Unauthorized -- the team\'s own '
                             'members or the operator only'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    template_id = str((body or {}).get('template') or '').strip()[:40]
    templates = _doctrine_templates_load()
    template = templates.get(template_id)
    if template is None:
        known = ', '.join(sorted(templates)) or '(none loaded)'
        return JSONResponse({'error': f'unknown template "{template_id}" '
                             f'(available: {known})'}, status_code=400)
    team['doctrine'] = {
        'origin': f'template:{template_id}',
        'levels': template['levels'],
        'currentLevel': template['levels'][0]['id'],
        'changedAt': time.time(),
        'changedBy': actor,
        'history': [],
    }
    log_action(actor, 'team_doctrine_adopted',
               {'teamId': team_id, 'template': template_id,
                'levels': [l['id'] for l in template['levels']]},
               authorized=True)
    save_state_to_db(state)
    return JSONResponse({'ok': True, 'teamId': team_id,
                         'origin': f'template:{template_id}',
                         'doctrine': team['doctrine']})


@app.put('/api/team-doctrine/{team_id}')
@_state_writer
async def team_doctrine_define(team_id: str, request: Request):
    """Define/replace the team's activation levels. The tank stores the
    levels and routes notifications; the MEANING of each level belongs to
    the team (triggers, staffing floors, autonomy ceilings, demotion
    criteria are all the team's own words)."""
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    team = _team_doctrine_find(state, team_id)
    if team is None:
        return JSONResponse({'error': f'no team "{team_id}"'}, status_code=404)
    authed, actor = _team_doctrine_authorized(request, team)
    if not authed:
        return JSONResponse({'error': 'Unauthorized -- the team\'s own '
                             'members or the operator only'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    levels, error = _team_doctrine_normalize_levels((body or {}).get('levels'))
    if error:
        return JSONResponse({'error': error}, status_code=400)
    doctrine = team.setdefault('doctrine', {})
    if not isinstance(doctrine, dict):
        doctrine = team['doctrine'] = {}
    previous = doctrine.get('currentLevel')
    doctrine['levels'] = levels
    valid = {l['id'] for l in levels}
    if previous not in valid:
        doctrine['currentLevel'] = levels[0]['id']
    doctrine['changedAt'] = time.time()
    doctrine['changedBy'] = actor
    doctrine.setdefault('history', [])
    log_action(actor, 'team_doctrine_defined',
               {'teamId': team_id, 'levels': [l['id'] for l in levels]},
               authorized=True)
    save_state_to_db(state)
    return JSONResponse({'ok': True, 'teamId': team_id, 'doctrine': doctrine})


@app.post('/api/team-doctrine/{team_id}/level')
@_state_writer
async def team_doctrine_set_level(team_id: str, request: Request):
    """Change the team's current activation level. Level changes are
    logged actions with a reason; when the target level declares
    notify=true, the operator hears about it on the standing Telegram
    channel immediately."""
    state = _state_begin()
    if not state:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    team = _team_doctrine_find(state, team_id)
    if team is None:
        return JSONResponse({'error': f'no team "{team_id}"'}, status_code=404)
    authed, actor = _team_doctrine_authorized(request, team)
    if not authed:
        return JSONResponse({'error': 'Unauthorized -- the team\'s own '
                             'members or the operator only'}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a missing body is a bad request
        body = None
    wanted = str((body or {}).get('level') or '').strip()[:40]
    reason = str((body or {}).get('reason') or '').strip()[:_DOCTRINE_TEXT_CAP]
    doctrine = team.get('doctrine')
    levels = (doctrine or {}).get('levels') or []
    if not levels:
        return JSONResponse({'error': 'this team has not defined a doctrine '
                             '(PUT /api/team-doctrine/<teamId> first)'},
                            status_code=409)
    level = next((l for l in levels if l.get('id') == wanted
                  or l.get('name', '').lower() == wanted.lower()), None)
    if level is None:
        known = ', '.join(l.get('name') or l.get('id') for l in levels)
        return JSONResponse({'error': f'unknown level "{wanted}" '
                             f'(team levels: {known})'}, status_code=400)
    previous_id = (doctrine or {}).get('currentLevel')
    if level['id'] == previous_id:
        return JSONResponse({'ok': True, 'teamId': team_id,
                             'currentLevel': previous_id,
                             'unchanged': True})
    doctrine['currentLevel'] = level['id']
    doctrine['changedAt'] = time.time()
    doctrine['changedBy'] = actor
    history = doctrine.setdefault('history', [])
    history.append({'at': time.time(), 'level': level['id'],
                    'name': level.get('name') or level['id'],
                    'by': actor, 'reason': reason})
    del history[:-_DOCTRINE_HISTORY_CAP]
    log_action(actor, 'team_doctrine_level',
               {'teamId': team_id, 'from': previous_id, 'to': level['id'],
                'reason': reason}, authorized=True)
    if level.get('notify'):
        try:
            send_player_telegram_sync(
                f'{team.get("name") or team_id} activation: {level.get("name") or level["id"]}',
                f'{reason or "(no reason given)"}\n'
                f'Trigger for this level: {level.get("trigger") or "(team-defined, unstated)"}\n'
                f'Autonomy at this level: {level.get("autonomy") or "(team-defined, unstated)"}\n'
                f'Demote when: {level.get("demote") or "(team-defined, unstated)"}')
        except Exception:  # noqa: BLE001 -- notification is best-effort
            pass
    save_state_to_db(state)
    return JSONResponse({'ok': True, 'teamId': team_id,
                         'currentLevel': level['id'],
                         'levelName': level.get('name') or level['id'],
                         'previousLevel': previous_id})
