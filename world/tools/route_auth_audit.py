"""Control-reachability audit (2026-11-18, adopted from the coherence
review): prove that every HTTP route in serve_parts has an explicit,
reviewable authorization decision, and fail the build when a new route
ships without one.

The tank's authorization is layered, and the audit models both layers:

1. DENY-BY-DEFAULT MIDDLEWARE (190_ingest_hf_open_llm_fallback.py): every
   path NOT matched by _is_public_path requires a player session, a valid
   agent key, a scoped Theo key, or an A2A bearer at the door. The audit
   parses AUTH_PUBLIC_PATHS / AUTH_PUBLIC_STATIC_EXTENSIONS from the same
   file (no drift: tests assert the parsed set equals the live tuple) and
   marks a middleware-covered route as authenticated.
2. IN-HANDLER GATES: routes the middleware deliberately exposes (public
   reads, self-guarding credentials) must reach an authorization gate in
   their own handler body -- one of the named gate helpers, or an inline
   X-Agent-Key check. A middleware-public route with NO body gate is a
   violation unless it is on this audit's PUBLIC_ALLOWLIST with a written
   reason. Adding to that list REQUIRES a reason; the audit prints it on
   every run so the list stays reviewed.

Static, AST-based, no app import (fast, side-effect free). CI runs this as
a blocking release gate.

Run: python3 tools/route_auth_audit.py [--verbose]
Exit 0 clean, 1 violations, 2 internal error.
"""
import ast
import os
import sys

PARTS_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'server', 'serve_parts')
MIDDLEWARE_PART = '190_ingest_hf_open_llm_fallback.py'

GATE_CALLS = {
    # Player/session gates
    '_operator_gate',
    '_require_player_or_admin',
    '_require_player_session',
    '_resolve_requester',
    '_access_authorize',
    'verify_session',
    # Agent-key registry
    'verify_agent_key',
    '_theo_policy',
    'evaluate_key',
    # Lane / role policy
    'check_rate_limit',
    # Grant-specific self-guards (the middleware-public routes' own
    # credential checks; each names its bearer credential explicitly)
    '_grant_actor',
    '_heartbeat_token_ok',
    '_village_token_ok',
    '_parent_token_ok',
    '_move_token_ok',
    '_visit_token_ok',
    '_visit_actor',
    '_habit_link_check',
    '_pair_key_ok',
    'validate_jev_grant',
    'validate_sublow_grant',
}

# Inline credential markers: a handler that reads one of these headers or
# does a constant-time secret compare is checking a bearer credential of
# its own.
CREDENTIAL_MARKERS = {
    'X-Agent-Key',
    'X-Webhook-Key',
    'X-Device-Key',
    'X-Parent-Token',
    'X-Habit-Link-Token',
    'X-SMS-Forward-Secret',
    'X-Jev-Grant',
    'X-Sublow-Grant',
    'Authorization',
}

HTTP_DECORATORS = {'get', 'post', 'patch', 'delete', 'put', 'head'}

# Paths the exact-match branch of _is_public_path treats as public (kept
# here in sync with the middleware's `if path in (...)` check).
EXACT_PUBLIC_PATHS = ('/', '/index.html')

# Middleware-public routes that legitimately serve WITHOUT an in-handler
# gate. Every entry needs a written reason; the audit prints the list on
# every run. A route here must be genuinely public or self-guarding -- if
# it touches operator data, fix the route, not this list.
PUBLIC_ALLOWLIST = {
    '/': 'static UI shell; all data calls ride the middleware gate',
    '/index.html': 'static UI shell',
    '/login': 'the login surface itself',
    '/logout': 'session teardown',
    '/mcp': 'MCP edge self-guards with a bearer token, fail-closed when '
            'THINK_TANK_MCP_TOKEN is unset (440_mcp_edge.py)',
    '/.well-known/agent-card.json': 'A2A discovery metadata, no secrets',
    '/.well-known/agent.json': 'A2A discovery metadata, no secrets',
    '/admin/enterprise': 'public HTML shell only; its data endpoints are '
                         'operator-gated in their own handlers',
    '/api/health': 'liveness read for process supervisors; no operator '
                   'data in the response',
    '/api/health/alerts': 'provider/budget alert booleans, no secrets',
    '/api/avatar': 'agent avatar images, public by design (static '
                   'extension branch of _is_public_path)',
    '/api/rooms': 'open UI read (deliberate, middleware list comment)',
    '/api/teams': 'open UI read (deliberate, middleware list comment)',
    '/api/backlog': 'open UI read (deliberate, middleware list comment)',
    '/api/templates': 'open read of the role-template library (deliberate: '
                      'only writes are director-gated, see in-code comment)',
    '/api/rule-proposals': 'open UI read of proposal list, no secrets',
    '/api/success-proposals': 'open UI read of proposal list, no secrets',
    '/api/escalation/resolve': 'self-guarded by an unguessable per-escalation '
                               'token carried in the emailed link; token '
                               'mismatch 404s (400_screenshot.py)',
}


def _parse_string_tuple(module_tree, name):
    """The constant strings of a module-level tuple assignment."""
    for node in module_tree.body:
        if isinstance(node, ast.Assign) \
                and any(isinstance(t, ast.Name) and t.id == name
                        for t in node.targets) \
                and isinstance(node.value, ast.Tuple):
            return [e.value for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def _parse_public_sets(parts_dir):
    path = os.path.join(parts_dir, MIDDLEWARE_PART)
    with open(path, encoding='utf-8') as fh:
        tree = ast.parse(fh.read(), filename=path)
    prefixes = _parse_string_tuple(tree, 'AUTH_PUBLIC_PATHS')
    extensions = _parse_string_tuple(tree, 'AUTH_PUBLIC_STATIC_EXTENSIONS')
    if not prefixes or not extensions:
        raise ValueError(f'{MIDDLEWARE_PART}: AUTH_PUBLIC_PATHS or '
                         'AUTH_PUBLIC_STATIC_EXTENSIONS not found')
    return prefixes, extensions


def _middleware_public(path, prefixes, extensions):
    """Same decision the middleware's _is_public_path makes: exact public
    paths, prefix entries, then static asset extensions."""
    if path in EXACT_PUBLIC_PATHS:
        return True
    if any(path.startswith(p) for p in prefixes):
        return True
    ext = os.path.splitext(path)[1].lower()
    return ext in extensions


def _route_path(decorator):
    """(method, path) from an @app.<method>('...') decorator, or None."""
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    if not (isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == 'app'
            and func.attr in HTTP_DECORATORS):
        return None
    if not decorator.args:
        return None
    arg = decorator.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return (func.attr.upper(), arg.value)
    return None


def _body_reaches_gate(func_node):
    """True when the handler body calls a named gate, checks a bearer
    credential inline, or does a constant-time secret compare."""
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in GATE_CALLS:
                return True
            if isinstance(func, ast.Attribute) \
                    and func.attr in GATE_CALLS:
                return True
            if isinstance(func, ast.Attribute) \
                    and func.attr == 'compare_digest':
                return True
        elif isinstance(node, ast.Constant) \
                and isinstance(node.value, str) \
                and node.value in CREDENTIAL_MARKERS:
            return True
    return False


def _load_functions(parts_dir):
    """{(method, route): (fn, file, lineno)} for every HTTP route. When a
    (method, route) pair is registered twice, FastAPI dispatches to the
    FIRST registration, so the audit must evaluate the first one too --
    auditing a handler that never runs would be a false pass."""
    routes = {}
    for fn in sorted(os.listdir(parts_dir)):
        if not fn.endswith('.py'):
            continue
        path = os.path.join(parts_dir, fn)
        with open(path, encoding='utf-8') as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                route = _route_path(dec)
                if route is not None and route not in routes:
                    routes[route] = (node, fn, node.lineno)
    return routes


def _allowlisted(route):
    """Exact entry, or under an entry's path prefix (parameterized routes
    like /api/avatar/{agent_id}/{filename})."""
    if route in PUBLIC_ALLOWLIST:
        return True
    return any(route.startswith(entry + '/')
               for entry in PUBLIC_ALLOWLIST)


def audit(parts_dir=PARTS_DIR):
    """Returns (counts, violations). violations: [(path, file, lineno,
    name, layer)] where layer explains which check the route failed."""
    prefixes, extensions = _parse_public_sets(parts_dir)
    routes = _load_functions(parts_dir)
    counts = {'routes': len(routes), 'middleware': 0, 'bodyGate': 0,
              'allowlisted': 0}
    violations = []
    for (method, route), (node, fn, lineno) in sorted(routes.items()):
        if _middleware_public(route, prefixes, extensions):
            if _body_reaches_gate(node):
                counts['bodyGate'] += 1
            elif _allowlisted(route):
                counts['allowlisted'] += 1
            else:
                violations.append((f'{method} {route}', fn, lineno, node.name,
                                   'public at the middleware with no '
                                   'in-handler gate and no allowlist reason'))
        else:
            counts['middleware'] += 1
    return counts, violations


def main():
    verbose = '--verbose' in sys.argv
    try:
        counts, violations = audit()
    except (OSError, SyntaxError, ValueError) as e:
        print(f'route_auth_audit: internal error: {e}')
        return 2
    print(f'route_auth_audit: {counts["routes"]} routes; '
          f'{counts["middleware"]} deny-by-default middleware; '
          f'{counts["bodyGate"]} public + in-handler gate; '
          f'{counts["allowlisted"]} public allowlisted; '
          f'{len(violations)} violations')
    for route, fn, lineno, name, why in sorted(violations):
        print(f'  UNGATED {route} ({fn}:{lineno} {name}): {why}')
    print('allowlist:')
    for route, reason in sorted(PUBLIC_ALLOWLIST.items()):
        print(f'  {route}: {reason}')
    if violations:
        print('FAIL: every route needs an explicit authorization decision '
              '-- gate the handler or justify it in PUBLIC_ALLOWLIST.')
        return 1
    if verbose:
        prefixes, extensions = _parse_public_sets(PARTS_DIR)
        for route in sorted(_load_functions(PARTS_DIR)):
            layer = ('public-middleware' if _middleware_public(
                route, prefixes, extensions) else 'deny-by-default')
            print(f'  [{layer}] {route}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
