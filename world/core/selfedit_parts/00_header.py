"""Self-edit pipeline: agents edit self-owned code through review gates.

The self-modification contract (operator decisions, locked 2026-10-18):
  * Editable surface: everything under world/ and BUSINESS-PROCESSES.md,
    EXCEPT CONSTITUTION.md and constitution.py. serve.py is editable through
    the signed kernel path (preflight, restart, watchdog); BUSINESS-PROCESSES.md
    is ordinary self-editable process documentation now.
  * Reload binding contract (why consumer scanning exists): after a file is
    replaced and its module reloaded, only code that reaches that module
    through its OWN globals or module attributes at CALL time sees new
    objects. config.POLICY_BINDINGS and serve's jev.py re-exports already
    follow that pattern; jev reloads are followed by a serve-name refresh,
    and sublow reloads are followed by reloading its one importer
    (jev_grants.py). Any OTHER module holding a top-level `from <target>
    import ...` binding would go stale, so a target with outside importers
    beyond the registered cases is REFUSED in v1.
  * config.py is NOT protected but is refused in v1 for the same reason: it
    has direct importers (content.py, sublow.py, serve.py) and a topological
    reload is not yet built. The operator edits config.py directly; Phase 2
    opens it.
  * Review bar: one JEV decisions review (the quorum sampler), then a
    subprocess quarantine validation (compileall over the whole world tree
    with the candidate in place + flake8 on the changed file + an import
    smoke of the changed module). No human approval step in v1; every gate
    outcome is logged in the self_edit_log table for audit and revert.
  * Apply window: DORMANT ONLY. apply_pass() returns without doing anything
    unless serve._dormant() is true; a single always-running serve loop
    (_dormancy_window_loop) owns applies on every poll while dormant. The
    tank reaches dormancy through TWO paths: the idle watcher (operator's
    --max-idle-minutes) and approved one-shot DORMANCY WINDOWS (below).
  * Change-request process for scheduled applies (operator decision,
    2026-10-18): an agent may request a one-shot dormancy window -- a date +
    start time + duration -- to sleep the tank and apply armed edits. The
    chain: (1) conflict scan against the standing-task schedule and every
    other pending/approved/fired window; any overlap REFUSES with the
    colliding items listed; (2) the agent's supervisor (serve._supervisor_of)
    passes JEV judgment on the request; (3) the request escalates as a
    floor-1.0 kind ('dormancy window request' in jev.Escalation floors) whose
    email/telegram link is the PLAYER's mandatory final signature. A window
    applies whatever armed edits exist when it fires (each edit already
    passed its own quorum + validator), then consumes itself; the tank
    normally re-awakes on the next request, exactly like the idle path.
  * VALIDATOR TURN (operator decision, 2026-10-19): the sim tick is paused
    while a window holds for validation, so the validator cannot be reached
    through the normal mailbox -> task path. serve._dormancy_window_loop runs
    ONE narrow decision turn for the window's named validator only. The turn
    reviews the applied diff plus the gate tail and still records its verdict
    through validate_window(); no other agent runs and the village stays
    dormant. A transport failure retries with backoff up to a hard attempt
    cap, then fails closed so the slot rollback still applies.
  * CHANGE WINDOW semantics (operator decision, 2026-10-18): edits landed in
    a window are TESTED after they apply (the repo's own gate script runs
    inside the window). If tests fail, an in-window remediation session
    opens: every proposer is mailed the failing output and, until the
    window's end, may submit corrective fixes and rerun the checks through
    remediation traffic that does NOT wake the tank (selfedit_* request
    paths only). If the failures are not resolved BY THE END of the window,
    the window ROLLS BACK automatically: each candidate that did not
    survive is copied to world/.selfedit_failed/<window>/<new-name>, every
    edit applied in that window is reverted to its pre-edit bytes, modules
    re-load, and each responsible agent is mailed to fix the issues
    OFF-window and schedule a fresh window. Applies outside any window (the
    idle path) get the same post-apply tests; with no window to remediate
    in, a failure there rolls back immediately and mails the agents.
  * AUDIT TIERS (operator decision, 2026-10-18):
      Tier A: the repo test gate itself -- world/tests/, conftest.py, run
        scripts -- is refused outright for edits and in-window fixes. An
        edit that could rewrite its own acceptance bar proves nothing.
      Tier B: oversight-enforcement files (jev, selfedit, notify,
        theo_policy, sublow, jev_grants, bank, budget) AND the operator
        policy data that steers other agents or weakens a safety net
        (tool_descriptions, redaction, actions, model_bands, service_costs,
        browse_policy, rooms, seed_profiles, endpoint_policy -- the world/
        *.json files) stay editable but need the PLAYER's emailed signature
        (OVERSIGHT_KIND, floor 1.0, the same human-in-the-loop as dormancy
        windows). The row waits as pending_signature and arms only after
        resolve_oversight_signature.
      READS mirror the write rule minus a smaller deliberate core
        (read_module): test expectations, gate internals, and oversight
        files are also UNREADABLE, since those are exactly what gaming
        would read first.
  * Failure posture: every gate fails closed. A refused, rejected, or
    unverifiable proposal never touches the file tree.
"""

import difflib
import hashlib
import json
import os
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

import core.governance as governance

# NEVER self-editable: the owner's constitution only. BUSINESS-PROCESSES.md
# is now self-editable, so the immutable "must not" rules live in
# CONSTITUTION.md and constitution.py instead. serve.py is a signed kernel
# edit (KERNEL_BASENAMES) with its own preflight and restart.
PROTECTED_BASENAMES = ('CONSTITUTION.md', 'constitution.py')

# Board movement is owner-protected for now: pathfinding, collision, and
# step/slide geometry stay frozen even though the rest of sim.py is editable.
# The markers make the guarded span explicit; a missing marker pair fails
# closed so an edit cannot remove the fence and then walk through it.
PHYSICS_BASENAMES = ('sim.py',)
PHYSICS_BEGIN = '# BEGIN MOVEMENT PHYSICS'
PHYSICS_END = '# END MOVEMENT PHYSICS'

# The HTTP kernel. It cannot be importlib-reloaded (uvicorn owns `app`), so a
# kernel edit is staged, preflighted against the full test suite, then applied
# by an in-place os.execv with a watchdog that restores the previous bytes if
# the new process never answers /api/health.
KERNEL_BASENAMES = ('serve.py',)
KERNEL_RESTART_ENABLED = True          # tests patch this off
KERNEL_HEALTH_TIMEOUT_S = 120
KERNEL_WATCHDOG_NAME = '.kernel_watchdog.py'

# Tier A of the 2026-10-18 audit: the post-apply rollback gate must be
# beyond proposer reach. A broken edit that could rewrite the tests (or the
# runner) would let the change-window contract "prove" its own success --
# so world/tests/ and the bare gate helpers are refused outright.
GATE_DIRS = ('tests',)
GATE_BASENAMES = ('conftest.py',)

# Tier B: oversight-enforcement files. They stay editable so the tank can
# improve its own judgment, but only through a mandatory PLAYER signature
# (floor-1.0 escalation kind, like 'dormancy window request'). One quorum
# review is not enough authority for the files that govern the reviews.
PLAYER_SIGNATURE_BASENAMES = ('jev.py', 'selfedit.py', 'notify.py',
                              'theo_policy.py', 'sublow.py', 'jev_grants.py',
                              'bank.py', 'budget.py',
                              # Policy data that shapes other agents' prompts,
                              # spend/routing facts, dormancy telemetry, or the
                              # log redaction net. Same tier as the code that
                              # consumes it: editable, player-signed.
                              'tool_descriptions.json', 'redaction.json',
                              'actions.json', 'model_bands.json',
                              'service_costs.json', 'browse_policy.json',
                              'rooms.json', 'seed_profiles.json',
                              'endpoint_policy.json')
OVERSIGHT_KIND = 'oversight self-edit'

# Read access mirrors the write rule: an agent may read exactly what it may
# edit (audit Q3), MINUS a smaller deliberate core. Also UNREADABLE on top
# of uneditable: test expectations and gate/oversight internals are the two
# things a gaming attempt would read first.
UNREADABLE_BASENAMES = ()
READ_TEXT_CAP = 48000

# Loading contract, target module -> importer basenames that ARE tolerated,
# because apply-time refresh/reload handles their specific binding shape:
#   jev    -> serve's `from jev import (...)` names are refreshed explicitly.
#   sublow -> jev_grants.py imports sublow at top level and is reloaded after.
#   config -> content/sim/sublow get a `from config import ...` binding that
#             importlib.reload re-executes, and serve's import block is
#             refreshed explicitly; apply re-applies every registered
#             policy binding through config.apply_policy_bindings().
#   extracted modules (rss, ws, webhooks, apis, auth, bank) -> serve's
#             `from <mod> import (...)` names are rebound by the generic
#             ast-driven refresh, and the modules with DB-backed registries
#             re-run their loader after the reload (POST_RELOAD_LOADERS).
#             Long-running loops keep running: they resolve module globals
#             per iteration, and the module dict is updated in place.
# Everything else tolerates ZERO outside importers in v1.
TOLERATED_IMPORTERS = {
    # Canonical package paths (current layout).
    'core.jev': {'serve.py'},
    'core.env_reload': {'serve.py'},
    'integrations.rss': {'serve.py'},
    'server.ws': {'serve.py'},
    'server.webhooks': {'serve.py'},
    'integrations.apis': {'serve.py'},
    'core.auth': {'serve.py'},
    'integrations.bank': {'serve.py'},
    'integrations.apple_photos': {'serve.py'},
    'core.sublow': {'serve.py', 'core/jev_grants.py'},
    'core.config': {'content.py', 'sim.py', 'core/sublow.py', 'serve.py'},
    # Legacy flat paths (temp-repo fixtures and old checkouts).
    'jev': {'serve.py'},
    'env_reload': {'serve.py'},
    'rss': {'serve.py'},
    'ws': {'serve.py'},
    'webhooks': {'serve.py'},
    'apis': {'serve.py'},
    'auth': {'serve.py'},
    'bank': {'serve.py'},
    'apple_photos': {'serve.py'},
    'sublow': {'serve.py', 'jev_grants.py'},
    'config': {'content.py', 'sim.py', 'sublow.py', 'serve.py'},
}
# Reload chain, extras refreshed after the target module itself, in order.
# The order matters only for readability here: importers of importers are
# refreshed through their own module-dict attribute lookups, not reloads.
# Modules that hold a DB-backed in-memory registry: reloading the module
# re-executes its body and empties that registry, so the loader must run again
# before the next request. Read off serve by name at apply time.
POST_RELOAD_LOADERS = {
    'integrations.apis': ('_load_api_services',),
    'integrations.rss': ('_load_rss_feeds',),
    'server.ws': ('_load_ws_feeds',),
    'apis': ('_load_api_services',),
    'rss': ('_load_rss_feeds',),
    'ws': ('_load_ws_feeds',),
}
RELOAD_IMPORTERS_AFTER = {
    'core.sublow': ('core.jev_grants',),
    'core.config': ('core.sublow', 'content', 'sim'),
    'sublow': ('jev_grants',),
    'config': ('sublow', 'content', 'sim'),
}

MAX_TARGET_FILE_BYTES = 4 * 1024 * 1024
MAX_FIND_LEN = 80_000
MAX_REPLACE_LEN = 200_000
MAX_SUMMARY_LEN = 500

# JEV review contract for code edits. The choice space maps onto the existing
# deciders: approve / reject / escalate / None(fail-closed).
_REVIEW_INSTRUCTIONS = (
    'You are reviewing a proposed code edit to an AI think tank codebase. '
    'The proposing agent must state WHAT it is changing and WHY this improves '
    'the tank. Reject when: the summary is missing, vague, or conflicts with '
    'the edit; the replacement looks destructive (wide deletions, removing '
    'fail-closed checks, hardcoding credentials, disabling gates, raising '
    'the proposing agent\'s own privileges); or the change exceeds what the '
    'summary describes. Approve only when purpose and scope are coherent '
    'and safe. Escalate when you are unsure.'
)
_REVIEW_CRITERIA = [
    {'id': 'approve',
     'description': ('Approve only when the stated purpose and the exact '
                     'find/replace splice are coherent, scoped, and safe.')},
    {'id': 'reject',
     'description': ('Reject destructive, self-privileging, unexplained, or '
                     'scope-exceeding edits. Only CONSTITUTION.md and '
                     'constitution.py are immutable; serve.py needs its own '
                     'preflight, restart, and watchdog.')},
]


def _s():
    import serve
    return serve


def _world():
    return _s().ROOT


def _repo_root():
    return os.path.dirname(_world())


def is_protected(resolved_path):
    """True for the immutable owner rules. BUSINESS-PROCESSES.md is no
    longer protected; the tank may edit its own process."""
    return os.path.basename(resolved_path) in PROTECTED_BASENAMES


def _physics_refusal(resolved_path, original, find):
    """Refuse a splice that overlaps sim.py's owner-protected movement
    physics. Markers are required; their absence fails closed."""
    if os.path.basename(resolved_path) not in PHYSICS_BASENAMES:
        return None
    begin = original.find(PHYSICS_BEGIN)
    end = original.find(PHYSICS_END)
    if begin < 0 or end < begin:
        return ('sim.py movement physics guard markers are missing or '
                'damaged; the file is refused until the owner restores them')
    region_end = end + len(PHYSICS_END)
    start = original.find(find)
    if start < 0:
        return None
    if start < region_end and start + len(find) > begin:
        return ('sim.py movement physics is owner-protected: pathfinding, '
                'collision, and step/slide geometry may not be changed '
                'without the owner. Edit other parts of sim.py instead')
    return None


def is_gate_file(resolved_path):
    """Tier A: the repo's own test gate -- tests/ trees, the run scripts,
    and bare gate helpers. Refused outright for edits AND fixes."""
    real = os.path.realpath(resolved_path)
    world_real = os.path.realpath(_world())
    rel = os.path.relpath(real, world_real)
    head = rel.split(os.sep)[0]
    if head in GATE_DIRS:
        return True
    return os.path.basename(real) in GATE_BASENAMES


def is_kernel(resolved_path):
    """True for the HTTP kernel: signed, preflighted, applied by restart."""
    name = os.path.basename(os.path.realpath(resolved_path))
    return name in KERNEL_BASENAMES


def is_player_signature(resolved_path):
    """Tier B: oversight-enforcement files need the player's signature
    before they can arm, and their contents stay unreadable."""
    name = os.path.basename(os.path.realpath(resolved_path))
    return name in PLAYER_SIGNATURE_BASENAMES


def is_unreadable(resolved_path):
    name = os.path.basename(os.path.realpath(resolved_path))
    if name in KERNEL_BASENAMES:
        # The kernel is signed, not hidden: an agent cannot propose a safe
        # find/replace against bytes it is not allowed to read.
        return False
    if name in UNREADABLE_BASENAMES or name in GATE_BASENAMES:
        return True
    if name in PLAYER_SIGNATURE_BASENAMES:
        return True
    return is_gate_file(resolved_path)


def _path_refusal(resolved_path, display):
    """Shared edit/fix gate for the audit tiers. Returns an error string or
    None when the path is acceptable."""
    if is_protected(resolved_path):
        return (f'{display} is PROTECTED: self-edits may never touch '
                'CONSTITUTION.md or constitution.py, even with approval')
    if is_gate_file(resolved_path):
        return (f'{display} is part of the repo test gate (tests/, '
                'conftest.py, run scripts): refused. An edit that could '
                'rewrite its own acceptance bar proves nothing')
    return None


def _facade_redirect(real):
    """Follow a `# facade: package.module` compatibility stub to the real file.

    The world tree is being split into domain packages while the historical
    top-level module names stay importable. A proposal against the stub must
    edit the implementation, not the one-line alias."""
    if not real.endswith('.py'):
        return None
    try:
        with open(real, 'r', encoding='utf-8') as fh:
            first = fh.readline().strip()
    except (OSError, UnicodeDecodeError):
        return None
    prefix = '# facade: '
    if not first.startswith(prefix):
        return None
    mod = first[len(prefix):].strip()
    if not mod or '..' in mod or '/' in mod or '\\' in mod:
        return None
    candidate = os.path.join(_world(), *mod.split('.')) + '.py'
    return candidate if os.path.isfile(candidate) else None


def _parts_dir(path):
    """Directory for a `# parts: rel/dir` source split, else None."""
    if not path or not path.endswith('.py'):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            first = fh.readline().strip()
    except (OSError, UnicodeDecodeError):
        return None
    prefix = '# parts: '
    if not first.startswith(prefix):
        return None
    rel = first[len(prefix):].strip().replace('\\', '/')
    if not rel or rel.startswith('/') or '..' in rel.split('/'):
        return None
    parts = os.path.join(_world(), *rel.split('/'))
    return parts if os.path.isdir(parts) else None


def _read_source(path):
    """Read one module, transparently stitching a `# parts:` source split."""
    parts = _parts_dir(path)
    if not parts:
        with open(path, 'r', encoding='utf-8') as fh:
            return fh.read()
    chunks = []
    for fn in sorted(f for f in os.listdir(parts) if f.endswith('.py')):
        with open(os.path.join(parts, fn), 'r', encoding='utf-8') as fh:
            chunks.append(fh.read())
    return ''.join(chunks)


def _resolve_path(path):
    """Resolve a proposal's target to (realpath, display_path) where display
    is repo-root-relative (e.g. 'world/sim.py'). Accepted forms: a bare
    world-module file name ('sim.py'), a repo-root-relative path
    ('world/sim.py'), or an absolute path inside the repo. Everything else
    -- and every nonexistent file or symlink escape -- errors out."""
    raw = (path or '').strip().replace('\\', '/')
    if not raw:
        return None, 'path is required'
    if os.path.isabs(raw):
        real = os.path.realpath(raw)
    elif raw.startswith('world/'):
        real = os.path.realpath(os.path.join(_repo_root(), raw))
    elif '/' not in raw and raw.endswith('.py'):
        real = os.path.realpath(os.path.join(_world(), raw))
    elif '/' not in raw and raw.endswith('.md'):
        real = os.path.realpath(os.path.join(_repo_root(), raw))
    else:
        return None, ('path must be a world module file (sim.py), a '
                      "repo-root relative path (world/sim.py), a root "
                      'markdown file (BUSINESS-PROCESSES.md), or an absolute '
                      'path inside this repository')
    if not os.path.isfile(real):
        return None, 'no such file'
    real = _facade_redirect(real) or real
    root_real = os.path.realpath(_repo_root())
    if os.path.commonpath([root_real, real]) != root_real:
        return None, 'path escapes the repository'
    return real, os.path.relpath(real, root_real)


def _module_name_from_path(path):
    """Canonical dotted module name for a file under world/, or its legacy
    top-level name when the file sits directly in world/."""
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(_world()))
    if rel.endswith('.py'):
        rel = rel[:-3]
    return rel.replace(os.sep, '.')


def _importers_of(modname):
    """World modules whose TOP-LEVEL code imports ``modname``.

    Walks the package tree, stitches ``# parts:`` loaders back into their
    logical module, and ignores function-local imports. Importer names are
    world-relative paths (``serve.py``, ``core/sublow.py``)."""
    importers = []
    short = modname.rsplit('.', 1)[-1]
    world = _world()
    for root, dirs, files in os.walk(world):
        dirs[:] = sorted(
            d for d in dirs
            if d not in ('__pycache__', '.selfedit_staging',
                         '.selfedit_backups', 'tests')
            and not d.endswith('_parts'))
        for fn in sorted(files):
            if not fn.endswith('.py'):
                continue
            full = os.path.join(root, fn)
            if _module_name_from_path(full) == modname:
                continue
            try:
                src = _read_source(full)
            except (OSError, UnicodeDecodeError):
                continue
            display = os.path.relpath(full, world).replace(os.sep, '/')
            for line in src.splitlines():
                if not line or line[0] in (' ', '\t', '#'):
                    continue
                stripped = line.strip()
                imported = ''
                if stripped.startswith('import '):
                    token = stripped[len('import '):].split(',')[0]
                    imported = token.split()[0].split(' as ')[0]
                elif stripped.startswith('from '):
                    imported = stripped.split()[1]
                if not imported:
                    continue
                if imported == modname or ('.' not in imported and imported == short):
                    importers.append(display)
                    break
    return importers


def _ensure_table(conn):
    conn.execute(
        'CREATE TABLE IF NOT EXISTS self_edit_log ('
        'id TEXT PRIMARY KEY, created REAL NOT NULL, agent TEXT NOT NULL, '
        'path TEXT NOT NULL, summary TEXT NOT NULL, '
        'sha_before TEXT NOT NULL, sha_after TEXT NOT NULL, '
        'status TEXT NOT NULL, note TEXT, jev_confidence REAL, jev_cost REAL, '
        'validator_note TEXT, armed_at REAL, applied_at REAL, reverted_at REAL)')
    try:  # change-window linkage landed after the v1 table
        conn.execute('ALTER TABLE self_edit_log ADD COLUMN window_id TEXT')
    except sqlite3.OperationalError:
        pass  # column already exists
    try:  # Tier B (audit 2026-10-18): mandatory player signature linkage
        conn.execute('ALTER TABLE self_edit_log ADD COLUMN signature_esc TEXT')
    except sqlite3.OperationalError:
        pass  # column already exists
    try:  # two-agent change requests: armed edits reserved for one window
        conn.execute('ALTER TABLE self_edit_log ADD COLUMN reserve_window TEXT')
    except sqlite3.OperationalError:
        pass  # column already exists
    try:  # high-risk changes: never apply on the idle path
        conn.execute("ALTER TABLE self_edit_log ADD COLUMN apply_policy TEXT "
                     "DEFAULT 'idle_or_window'")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute('CREATE INDEX IF NOT EXISTS idx_self_edit_status '
                 'ON self_edit_log (status, created)')


def _record(edit_id, created, agent, display, summary, sha_before, sha_after,
            status, apply_policy='idle_or_window'):
    with _s()._db() as conn:
        _ensure_table(conn)
        conn.execute(
            'INSERT INTO self_edit_log (id, created, agent, path, summary, '
            'sha_before, sha_after, status, apply_policy) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (edit_id, created, agent, display, summary, sha_before, sha_after,
             status, apply_policy))


def _update(edit_id, **fields):
    if not fields:
        return
    cols = ', '.join(f'{k} = ?' for k in fields)
    with _s()._db() as conn:
        _ensure_table(conn)
        conn.execute(f'UPDATE self_edit_log SET {cols} WHERE id = ?',
                     (*fields.values(), edit_id))


def _get_row(edit_id):
    with _s()._db() as conn:
        _ensure_table(conn)
        return conn.execute(
            'SELECT path, status FROM self_edit_log WHERE id = ?',
            (edit_id,)).fetchone()


def _staging_candidate(edit_id):
    return os.path.join(_world(), '.selfedit_staging', f'{edit_id}.cand')


def _compile_error(text, path=''):
    # Python's compiler only applies to Python modules. HTML, JS, CSS, and
    # other assets are valid self-edit targets; compiling them as Python
    # produced false "invalid decimal literal" failures and blocked the
    # pressoffice artifact change. The repo gate still tests the tree.
    if path and not str(path).endswith('.py'):
        return None
    try:
        compile(text, '<selfedit-candidate>', 'exec')
    except SyntaxError as e:
        return str(e)
    return None


def propose(agent_id, path, find, replace, summary, window_only=False):
    """Run the full gate chain for one splice edit. Returns (ok, payload);
    ok=False means nothing was armed and payload carries an 'error' for the
    proposing agent.

    window_only marks the edit as change-window-only: the idle dormancy path
    will never apply it. High-risk requests require this on every reserved
    edit so an always-on service cannot be changed by an idle apply."""
    agent_id = (agent_id or '').strip() or 'unknown'
    summary = (summary or '').strip()[:MAX_SUMMARY_LEN]
    apply_policy = 'window_only' if window_only else 'idle_or_window'
    find = find if isinstance(find, str) else ''
    replace = replace if isinstance(replace, str) else ''
    if not find or len(find) > MAX_FIND_LEN:
        return False, {'error': f'find text is required (max {MAX_FIND_LEN} chars)'}
    if len(replace) > MAX_REPLACE_LEN:
        return False, {'error': f'replacement too long (max {MAX_REPLACE_LEN} chars)'}
    if find == replace:
        return False, {'error': 'this edit changes nothing'}
    if not summary:
        return False, {'error': 'a summary is required -- say WHAT you are '
                                'changing and WHY it helps the tank'}
    state = _s().get_state_from_db() or {}
    if state and not governance.may_self_edit(state, agent_id):
        return False, {'error': 'directors and the admin may not edit or '
                                'create code; file a story, spike, or bug for '
                                'a worker instead'}
    resolved, display = _resolve_path(path)
    if resolved is None:
        return False, {'error': display}
    refusal = _path_refusal(resolved, display)
    if refusal:
        return False, {'error': refusal}
    try:
        with open(resolved, 'r', encoding='utf-8') as fh:
            original = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        return False, {'error': f'cannot read target: {e}'}
    refusal = _physics_refusal(resolved, original, find)
    if refusal:
        return False, {'error': refusal}
    if len(original.encode('utf-8')) > MAX_TARGET_FILE_BYTES:
        return False, {'error': 'target file too large for a self-edit'}
    occurrences = original.count(find)
    if occurrences != 1:
        where = 'appears 0 times' if occurrences == 0 else \
            f'appears {occurrences} times'
        return False, {'error': f'the find text must appear exactly once; it '
                                f'{where}. Include more surrounding context '
                                'to make the anchor unique'}
    candidate = original.replace(find, replace, 1)
    syntax_err = _compile_error(candidate, resolved)
    if syntax_err:
        return False, {'error': f'candidate does not compile: {syntax_err} -- '
                                'fix the splice before proposing'}
    modname = os.path.splitext(os.path.basename(resolved))[0]
    kernel = is_kernel(resolved)
    if not kernel:
        # A kernel edit is a process replacement, so every binding is rebuilt
        # at boot and the importer scan does not apply to it.
        importers = _importers_of(modname)
        tolerated = TOLERATED_IMPORTERS.get(modname, set())
        hostile = [i for i in importers if i not in tolerated]
        if hostile:
            return False, {'error': f'{display} is top-level imported by '
                                    f'{", ".join(hostile)}; those bindings '
                                    'would go stale after the post-apply '
                                    'reload. Propose edits to the consumer '
                                    'module instead (v1 binding contract).'}
    sha_before = hashlib.sha256(original.encode('utf-8')).hexdigest()
    sha_after = hashlib.sha256(candidate.encode('utf-8')).hexdigest()
    edit_id = f'ed-{secrets.token_hex(6)}'
    _record(edit_id, time.time(), agent_id, display, summary, sha_before,
            sha_after, 'proposed', apply_policy=apply_policy)
    _s().log_action(agent_id, 'self_edit_proposed',
                    {'id': edit_id, 'path': display,
                     'delta_bytes': len(candidate) - len(original)},
                    authorized=True)
    ok, payload = _review(edit_id, display, find, replace, summary)
    if not ok:
        return False, payload
    ok, payload = _validate(edit_id, resolved, candidate)
    if not ok:
        return False, payload
    os.makedirs(os.path.dirname(_staging_candidate(edit_id)), exist_ok=True)
    with open(_staging_candidate(edit_id), 'w', encoding='utf-8') as fh:
        fh.write(candidate)
    if is_player_signature(resolved):
        # Tier B (audit 2026-10-18): oversight-enforcement files arm only
        # after the player's emailed signature. The quorum review and the
        # validator already passed; the human signs (or refuses) a fully
        # vetted artifact.
        serve = _s()
        apply_text = ('Approving ARMS this KERNEL edit: on the next dormant '
                      'window it is preflighted against the full test suite '
                      'and then applied by restarting the server in place.'
                      if kernel else
                      'Approving ARMS this edit to apply the next time the '
                      'tank goes dormant.')
        question = (f'Signature requested for a player-signature self-edit '
                    f'of {display} by {agent_id}.\n'
                    f'Summary: {summary}\n'
                    f'{apply_text} Denying refuses it.')
        esc_id = serve.create_escalation(
            OVERSIGHT_KIND, question, on_approve_note=edit_id,
            what_checked='JEV quorum review passed; quarantine validation '
                         'passed')
        _update(edit_id, status='pending_signature', signature_esc=esc_id)
        serve.log_action(edit_id, 'self_edit_signature_pending',
                         {'path': display, 'escalation': esc_id},
                         authorized=True)
        return True, {'id': edit_id, 'status': 'pending_signature',
                      'escalationId': esc_id,
                      'message': f'{display} governs oversight, so this edit '
                                 f'needs the player signature: an approval '
                                 f'link was emailed and the edit arms only '
                                 f'after it is signed'}
    _update(edit_id, status='armed', armed_at=time.time())
    _s().log_action(edit_id, 'self_edit_armed',
                    {'path': display, 'apply': 'dormant-only via idle loop'},
                    authorized=True)
    return True, {'id': edit_id, 'status': 'armed',
                  'message': 'edit armed -- it applies the next time the tank '
                             'goes dormant; check self_edit_status'}


def resolve_oversight_signature(esc_id, approved):
    """Player link driver for OVERSIGHT_KIND escalations: approves arm the
    waiting row, denies refuse it. Returns False for unknown/already-set."""
    serve = _s()
    with serve._db() as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT id FROM self_edit_log WHERE signature_esc = ? "
            "AND status = 'pending_signature'", (esc_id,)).fetchone()
    if not row:
        return False
    edit_id = row[0]
    if approved:
        _update(edit_id, status='armed', armed_at=time.time())
        serve.log_action(edit_id, 'self_edit_signed',
                         {'escalation': esc_id}, authorized=True)
    else:
        _update(edit_id, status='refused',
                note='player signature denied (oversight self-edit)')
        serve.log_action(edit_id, 'self_edit_signature_denied',
                         {'escalation': esc_id}, authorized=True)
    return True


def read_module(path='', query='', context=40):
    """Audit Q3: bounded READ access to the editable surface, mirroring the
    write rule. Empty path lists the readable world modules. A path returns
    the file's head with its sha256 and total size; `query` instead returns a
    window of `context` lines around the first exact match, so a large file
    like serve.py can be inspected at the anchor a proposal needs without
    loading it whole. BUSINESS-PROCESSES.md, tests/, and the Tier B oversight
    files stay closed; the kernel is readable because it is signed."""
    if not (path or '').strip():
        modules = []
        world = _world()
        for fn in sorted(os.listdir(world)):
            full = os.path.join(world, fn)
            if not os.path.isfile(full):
                continue
            display = f'world/{fn}'
            if is_unreadable(full):
                continue
            modules.append({'path': display,
                            'bytes': os.path.getsize(full)})
        return True, {'modules': modules}
    resolved, display = _resolve_path(path)
    if resolved is None:
        return False, {'error': display}
    if is_unreadable(resolved):
        return False, {'error': f'{display} is outside the readable '
                                'self-edit surface (oversight internals and '
                                'the repo test gate stay closed)'}
    try:
        text = _read_source(resolved)
    except (OSError, UnicodeDecodeError) as e:
        return False, {'error': f'cannot read target: {e}'}
    data = text.encode('utf-8')
    base = {
        'path': display,
        'sha256': hashlib.sha256(data).hexdigest(),
        'totalBytes': len(data),
        'totalLines': text.count('\n') + 1,
    }
    query = (query or '').strip()
    if query:
        idx = text.find(query)
        if idx < 0:
            return False, {'error': f'the query was not found in {display}; '
                                    'omit it to read the head, then pick a '
                                    'shorter exact anchor'}
        try:
            context_n = max(1, min(int(context or 40), 200))
        except (TypeError, ValueError):
            context_n = 40
        line_no = text.count('\n', 0, idx) + 1
        lines = text.splitlines()
        start = max(1, line_no - context_n)
        end = min(len(lines), line_no + context_n)
        return True, {**base,
                      'text': '\n'.join(lines[start - 1:end]),
                      'truncated': False,
                      'matchLine': line_no,
                      'startLine': start,
                      'endLine': end,
                      'note': 'window around the query; an anchor must still '
                              'appear exactly once in the whole file'}
    head = text[:READ_TEXT_CAP]
    return True, {**base,
                  'text': head,
                  'truncated': len(text) > READ_TEXT_CAP,
                  'note': 'read-only context for proposing exact anchors; '
                          'the file may change between this read and an apply. '
                          'Pass query=... to jump to a specific anchor.'}


def _review(edit_id, display, find, replace, summary):
    """One quorum-sampled JEV decisions review; every outcome fails closed
    unless it is a confident enough approval."""
    serve = _s()
    question = (f'Proposed self-edit of {display} by agent.\n'
                f'Summary: {summary}\n'
                f'FIND (exact splice anchor):\n{find[:4000]}\n'
                f'REPLACE WITH:\n{replace[:8000]}')
    decision = confidence = None
    cost = 0.0
    for _attempt in range(2):
        try:
            decision, confidence, cost = serve._jev_quorum_choice_sync(
                _REVIEW_INSTRUCTIONS + '\n\n' + question,
                [dict(c) for c in _REVIEW_CRITERIA])
        except Exception as e:  # transport/shape failure is not consent
            decision, confidence, cost = None, 0.0, 0.0
            serve.log_action(edit_id, 'self_edit_review_failed',
                             {'error': str(e)[:200]}, authorized=False)
        if decision in ('approve', 'reject'):
            break
    _update(edit_id, jev_confidence=round(float(confidence or 0.0), 4),
            jev_cost=round(float(cost or 0.0), 6),
            note=f'jev: {decision!r} @ {round(float(confidence or 0.0), 2)}')
    if decision == 'approve':
        return True, {'review': 'approved', 'confidence': confidence}
    if decision not in ('approve', 'reject'):
        # A missing/invalid reviewer decision is an outage, not a judgment.
        # Fail closed (nothing is armed) but tell the proposer to retry instead
        # of reporting a substantive rejection that never happened.
        _update(edit_id, status='refused', note='reviewer unavailable')
        serve.log_action(edit_id, 'self_edit_review_unavailable',
                         {'decision': decision, 'confidence': confidence},
                         authorized=False)
        return False, {
            'error': ('the edits reviewer returned no valid decision '
                      '(reviewer unavailable, not a rejection) -- nothing was '
                      'changed; retry the same proposal shortly.'),
            'status': 'unavailable',
            'retryable': True,
        }
    _update(edit_id, status='rejected')
    serve.log_action(edit_id, 'self_edit_rejected',
                     {'decision': decision, 'confidence': confidence},
                     authorized=True)
    return False, {
        'error': (f'the edits reviewer said {decision!r} at '
                  f'{round(float(confidence or 0.0), 2)} confidence -- '
                  'nothing was changed. Re-propose with a clearer summary, '
                  'a smaller edit, or a rewritten splice.'),
        'status': 'rejected'}


def _validate(edit_id, resolved, candidate):
    """Subprocess quarantine: copy the whole world tree, drop the candidate
    in, compileall, flake8 the changed file, import-smoke the changed
    module. Any failure refuses the edit."""
    serve = _s()
    staging_root = os.path.join(_world(), '.selfedit_staging')
    os.makedirs(staging_root, exist_ok=True)
    tmp = os.path.join(staging_root, f'q-{secrets.token_hex(6)}')
    checks, errors = [], []
    try:
        shutil.copytree(_world(), tmp,
                        ignore=shutil.ignore_patterns(
                            '__pycache__', '.selfedit_staging',
                            '.selfedit_backups', '.selfedit_failed'),
                        dirs_exist_ok=True)
        with open(os.path.join(tmp, os.path.basename(resolved)), 'w',
                  encoding='utf-8') as fh:
            fh.write(candidate)
        py = sys.executable
        step = subprocess.run([py, '-m', 'compileall', '-q', tmp],
                              capture_output=True, text=True, timeout=180)
        if step.returncode != 0:
            errors.append('compileall: ' + (step.stdout or step.stderr)[-400:])
        else:
            checks.append('compileall ok')
        is_python = str(resolved).endswith('.py')
        if not is_python:
            # HTML/JS/CSS assets are valid change-request targets. Python's
            # flake8 and import smoke do not apply; the repo gate still runs
            # on the live tree after apply.
            checks.append('non-Python asset: flake8/import smoke skipped')
        elif is_kernel(resolved):
            # serve.py deliberately imports after config/root setup (E402) and
            # predates this gate; the full preflight suite is its real bar.
            checks.append('flake8 skipped for the kernel')
        else:
            flake = subprocess.run(
                [py, '-m', 'flake8',
                 os.path.join(tmp, os.path.basename(resolved)),
                 '--max-line-length=100'],
                capture_output=True, text=True, timeout=180)
            if flake.returncode != 0:
                errors.append('flake8: ' + (flake.stdout or flake.stderr)[-400:])
            else:
                checks.append('flake8 ok')
        if is_python and not is_kernel(resolved):
            modname = os.path.splitext(os.path.basename(resolved))[0]
            smoke_src = (f'import sys; sys.path.insert(0, {tmp!r}); '
                         f'import {modname}')
            smoke = subprocess.run(
                [py, '-c', smoke_src],
                capture_output=True, text=True, timeout=180, cwd=tmp,
                env={**os.environ, 'PYTHONPATH': tmp})
            if smoke.returncode != 0:
                errors.append('import smoke: ' + (smoke.stderr or smoke.stdout)[-600:])
            else:
                checks.append('import smoke ok')
    except subprocess.TimeoutExpired:
        errors.append('validator timed out')
    except Exception as e:
        errors.append(f'validator failure: {e}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    note = (' | '.join(checks) if not errors
            else 'FAIL: ' + ' | '.join(errors))
    _update(edit_id, validator_note=note[:400])
    if errors:
        serve.log_action(edit_id, 'self_edit_validator_rejected',
                         {'note': note[:200]}, authorized=True)
        return False, {'error': 'quarantine validation failed -- the '
                                'candidate never left staging',
                       'detail': note[:400], 'status': 'validator-rejected'}
    return True, {'validator': note[:400]}
