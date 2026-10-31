"""generate_village: the SENIOR-MOST DIRECTORS' village-founding tool.

The operator fixed each village at 12 top-level teams. When a village is at
its capacity, team growth has nowhere to go INSIDE that village -- the
sanctioned next step is founding another schematic-native village: pure data
(boundary/collision grid, door markers, room labels, gate, spawn), no baked
artwork. This tool runs the same generator the operator used for the south
village and registers the result in world/scenes.json, which the client
picks up on its next boot.

The tool refuses to run while the current village still has team capacity --
villages are for overflow, not sprawl. Population (agents living in the new
village) is a later sim slice; this tool creates the place, honestly scoped.
"""
import json
import os
import re
import sys

VILLAGE_NAME_RE = re.compile(r'^[a-z][a-z0-9-]{1,19}$')
RESERVED_NAMES = {'main', 'winter', 'south'}

VILLAGE_GEN_TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'generate_village',
            'description': 'Found a NEW schematic-native village (data only: '
                           'boundary, collision grid, door markers, room '
                           'labels, gate, spawn -- no artwork). Senior-most '
                           'directors only (directors reporting directly to '
                           'the chief of staff), and only when the current '
                           'village has reached its maxTeamsPerVillage '
                           'capacity: villages exist to absorb overflow, '
                           'not sprawl, and founding one is the senior '
                           'directors\' responsibility. Supply a short '
                           'lowercase name (e.g. "east"). The village is '
                           'registered in scenes.json and becomes available '
                           'to the client on its next boot; agents do not '
                           'relocate to it until the multi-village sim slice '
                           'lands.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string',
                             'description': 'Short lowercase village name '
                                            '(2-20 chars: a-z, 0-9, dash).'},
                },
                'required': ['name'],
            },
        },
    },
]


def _world_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _team_capacity_gate():
    # This module is imported inside the serve process: `config` is
    # core.config (imported by serve's header) and `governance` is
    # core.governance. Import both explicitly -- this module's own namespace
    # does not inherit serve's.
    import core.config as config  # noqa: F401
    import core.governance as governance  # noqa: F401
    cap = int((config.OPERATOR_POLICY or {}).get('maxTeamsPerVillage') or 12)
    import serve
    state = serve.get_state_from_db()
    count = governance.village_team_count(state)
    return count, cap

def _is_senior_director(agent_id):
    """Senior-most directors: top-level directors reporting directly to the
    chief of staff (the admin). Founding a village is their responsibility --
    the operator fixed the senior layer and owns overflow through it."""
    import serve
    state = serve.get_state_from_db()
    roster = state.get('agentRoster') or []
    admin_id = next((d.get('id') for d in roster if d.get('isAdmin')), None)
    if not admin_id:
        return False
    if agent_id == admin_id:
        return True
    row = next((d for d in roster if d.get('id') == agent_id), None)
    return bool(row and row.get('isDirector')
                and row.get('director') == admin_id)


def execute_village_gen_tool(agent_id, name, args):
    _ = name
    if not _is_senior_director(agent_id):
        return json.dumps({
            'ok': False,
            'reason': 'village founding is the senior-most directors\' '
                      'responsibility -- only directors reporting directly '
                      'to the chief of staff may generate a village.'})
    count, cap = _team_capacity_gate()
    if count < cap:
        return json.dumps({
            'ok': False,
            'reason': f'the current village has {count} of {cap} team '
                      'capacity left -- villages are founded on overflow, '
                      'not sprawl. Create teams first.'})
    village = (args.get('name') or '').strip().lower()
    if not VILLAGE_NAME_RE.match(village) or village in RESERVED_NAMES:
        return json.dumps({
            'ok': False,
            'reason': 'village name must be 2-20 lowercase letters, digits '
                      'or dashes, and not a reserved village name.'})
    sys.path.insert(0, os.path.join(_world_dir(), 'tools'))
    try:
        import generate_schematic_village as gen
    finally:
        sys.path.pop(0)
    world_dir = _world_dir()
    grid_path = os.path.join(world_dir, f'collision_grid_{village}.json')
    doors_path = os.path.join(world_dir, f'door_triggers_{village}.json')
    if os.path.exists(grid_path) or os.path.exists(doors_path):
        return json.dumps({
            'ok': False,
            'reason': f'a village named "{village}" already has data on '
                      'disk; pick another name.'})
    grid_path, doors_path, reachable = gen.generate(village, world_dir)
    scenes_path = os.path.join(world_dir, 'scenes.json')
    import fcntl
    with open(scenes_path + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(scenes_path) as f:
                scenes = json.load(f)
            if village in scenes:
                os.remove(grid_path)
                os.remove(doors_path)
                return json.dumps({
                    'ok': False,
                    'reason': f'a scene named "{village}" is already '
                              'registered; pick another name.'})
            scenes[village] = {
                'gridSprite': f'/collision_grid_{village}.json',
                'doorsSprite': f'/door_triggers_{village}.json',
                # WORLD-px gate and spawn (the client scales native data by
                # SCALE at load; scenes.json coordinates are already world).
                'gate': {'x': gen.GATE['x'] * 2, 'y': gen.GATE['y'] * 2,
                         'w': gen.GATE['w'] * 2, 'h': gen.GATE['h'] * 2},
                'spawn': {'x': gen.SPAWN['x'] * 2, 'y': gen.SPAWN['y'] * 2},
            }
            tmp = scenes_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(scenes, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, scenes_path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    try:
        import serve
        serve.log_action(agent_id, 'generate_village',
                         {'village': village, 'teams': count, 'cap': cap},
                         authorized=True)
    except Exception:
        pass
    return json.dumps({
        'ok': True,
        'village': village,
        'walkableCells': reachable,
        'registered': True,
        'note': 'The village is schematic-native data: boundary, collision '
                'grid, door markers for the standard rooms, gate and spawn. '
                'It becomes walkable on the client\'s next boot; the player '
                'reaches it from an outlet, and agents relocate only when '
                'the multi-village sim slice lands.'})
