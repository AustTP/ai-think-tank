# Access-control lattice: one structure that answers "can actor X do action Y
# in domain Z" instead of a per-endpoint bag of conditionals. Levels come from
# the org chart (the tank's own director definition is "has direct reports"),
# titles are tiebreakers, and the policy table maps (domain, action) to a
# minimum level. Unknown rows and unknown actors DENY (fail closed); new
# governance is a new row, never another scattered if.
#
# Levels (low -> high): external < assistant < supervisor < director < chief
# < player. The player is a human with a session, ranked above every agent on
# every domain; agents never outrank the operator.

_ACCESS_LEVELS = ('external', 'assistant', 'supervisor', 'director', 'chief', 'player')
_ACCESS_RANK = {name: i for i, name in enumerate(_ACCESS_LEVELS)}

_ACCESS_MIN_LEVEL = {
    # exec: the registry/review-execute gate. Matches today's _operator_gate
    # rule exactly: a player session, or any agent with direct reports.
    # The pre-lattice operator gate admitted player, director/admin, and any
    # agent with direct reports (the canonical director predicate) -- the
    # supervisor floor preserves exactly that. The lattice structures
    # authority; it does not silently widen or narrow it.
    ('exec', 'run'): 'supervisor',
    ('exec', 'manage'): 'chief',          # registry mutations, key management
    ('spend', 'view'): 'assistant',
    ('spend', 'change'): 'chief',         # caps, budgets, policy switches
    ('credentials', 'read'): 'chief',
    ('credentials', 'write'): 'chief',
    ('library_write', 'commit'): 'supervisor',
    ('allowlist', 'request'): 'assistant',
    ('allowlist', 'approve'): 'chief',
    ('policy', 'write_global'): 'chief',  # e.g. the free-pool global switches
    ('policy', 'write_team'): 'director',  # a director may govern their own team
    ('state_write', 'full'): 'player',    # the whole-blob autosave: player-only
}


def _access_level_of(actor, state=None):
    if actor == 'player':
        return 'player'
    if not actor:
        return 'external'
    if state is None:
        try:
            state = get_state_from_db() or {}
        except Exception:
            state = {}
    roster = state.get('agentRoster') or []
    entry = next((d for d in roster if d.get('id') == actor), None)
    if entry:
        if entry.get('isAdmin'):
            return 'chief'
        role = (entry.get('role') or '').lower()
        if 'chief' in role:
            return 'chief'
        if 'director' in role:
            return 'director'
    # The tank's own canonical director predicate ("has direct reports") is
    # the supervisor floor -- one definition, injectable in tests. Only an
    # agent the canonical predicate vouches for rises above assistant; a
    # plain worker never does.
    if _is_director_or_admin(state, actor):
        return 'supervisor'
    return 'assistant' if entry else 'external'


def _access_authorize(actor, domain, action, state=None):
    """(ok, level, required_level). Unknown domain/action and unknown actors
    deny. The org chart decides rank; this table decides policy."""
    lvl = _access_level_of(actor, state)
    need = _ACCESS_MIN_LEVEL.get((domain, action))
    if need is None:
        return False, lvl, None
    return _ACCESS_RANK[lvl] >= _ACCESS_RANK[need], lvl, need
