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

import governance

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
    'jev': {'serve.py'},
    'env_reload': {'serve.py'},
    'rss': {'serve.py'},
    'ws': {'serve.py'},
    'webhooks': {'serve.py'},
    'apis': {'serve.py'},
    'auth': {'serve.py'},
    'bank': {'serve.py'},
    'apple_photos': {'serve.py'},
    'sublow': {'jev_grants.py'},
    'config': {'content.py', 'sim.py', 'sublow.py', 'serve.py'},
}
# Reload chain, extras refreshed after the target module itself, in order.
# The order matters only for readability here: importers of importers are
# refreshed through their own module-dict attribute lookups, not reloads.
# Modules that hold a DB-backed in-memory registry: reloading the module
# re-executes its body and empties that registry, so the loader must run again
# before the next request. Read off serve by name at apply time.
POST_RELOAD_LOADERS = {
    'apis': ('_load_api_services',),
    'rss': ('_load_rss_feeds',),
    'ws': ('_load_ws_feeds',),
}
RELOAD_IMPORTERS_AFTER = {
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
_REVIEW_CRITERIA = (
    'A short summary describing intent (given).\n'
    'The exact find/replace splice being made (given).\n'
    'Reject destructive, self-privileging, or unexplained edits.\n'
    'Only CONSTITUTION.md and constitution.py are immutable. serve.py is a '
    'kernel edit with a separate preflight, restart, and watchdog; '
    'BUSINESS-PROCESSES.md is ordinary editable process documentation.'
)


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
    root_real = os.path.realpath(_repo_root())
    if os.path.commonpath([root_real, real]) != root_real:
        return None, 'path escapes the repository'
    return real, os.path.relpath(real, root_real)


def _importers_of(modname):
    """World modules whose TOP-LEVEL code does `import <modname>` or
    `from <modname> import ...` -- bindings that would go stale across a
    reload. Function-local `import <modname>` does not count (call-time
    resolution is reload-safe); only column-0 statements are read."""
    importers = []
    for fn in sorted(os.listdir(_world())):
        if not fn.endswith('.py') or fn == f'{modname}.py':
            continue
        try:
            with open(os.path.join(_world(), fn), 'r', encoding='utf-8') as fh:
                src = fh.read()
        except OSError:
            continue
        for line in src.splitlines():
            if not line or line[0] in (' ', '\t', '#'):
                continue
            stripped = line.strip()
            if stripped.startswith('import '):
                if stripped.split()[1].split('.')[0] == modname:
                    importers.append(fn)
                    break
            elif stripped.startswith('from '):
                module = stripped.split()[1].split('.')[0]
                if module == modname:
                    importers.append(fn)
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


def _compile_error(text):
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
    syntax_err = _compile_error(candidate)
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
        with open(resolved, 'rb') as fh:
            data = fh.read()
        text = data.decode('utf-8')
    except (OSError, UnicodeDecodeError) as e:
        return False, {'error': f'cannot read target: {e}'}
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
    try:
        decision, confidence, cost = serve._jev_quorum_choice_sync(
            _REVIEW_INSTRUCTIONS, _REVIEW_CRITERIA + '\n\n' + question)
    except Exception as e:  # transport/shape failure is not consent
        decision, confidence, cost = None, 1.0, 0.0
        serve.log_action(edit_id, 'self_edit_review_failed',
                         {'error': str(e)[:200]}, authorized=False)
    _update(edit_id, jev_confidence=round(float(confidence or 0.0), 4),
            jev_cost=round(float(cost or 0.0), 6),
            note=f'jev: {decision!r} @ {round(float(confidence or 0.0), 2)}')
    if decision == 'approve':
        return True, {'review': 'approved', 'confidence': confidence}
    _update(edit_id, status='rejected' if decision == 'reject' else 'refused')
    serve.log_action(edit_id,
                     'self_edit_rejected' if decision == 'reject'
                     else 'self_edit_refused',
                     {'decision': decision, 'confidence': confidence},
                     authorized=True)
    return False, {
        'error': (f'the edits reviewer said {decision!r} at '
                  f'{round(float(confidence or 0.0), 2)} confidence -- '
                  'nothing was changed. Re-propose with a clearer summary, '
                  'a smaller edit, or a rewritten splice.'),
        'status': 'rejected' if decision == 'reject' else 'refused'}


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
                            '.selfedit_backups'),
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
        if is_kernel(resolved):
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


def apply_pass():
    """Apply every armed edit while the tank is DORMANT, then TEST the live
    tree with the repo's own gate script (change-window semantics). Thread-
    safe from the serve loop (called through asyncio.to_thread).

    Returns the number of edits landed. On test failure the edits either
    enter the window's remediation session (fixable until the window ends,
    else automatic rollback) or, outside a window, roll back immediately;
    every proposer is mailed either way."""
    serve = _s()
    if not serve._dormant():
        return 0
    window_id = _active_window_id()
    with serve._db() as conn:
        _ensure_table(conn)
        if window_id:
            # inside a change window, ONLY the window's reserved edits apply
            # (two-agent contract: the validator reviews exactly this
            # change request's bytes, not bystander edits)
            rows = conn.execute(
                "SELECT id, path, sha_before FROM self_edit_log "
                "WHERE status = 'armed' AND reserve_window = ? "
                'ORDER BY created', (window_id,)).fetchall()
        else:
            # the idle path applies unreserved armed edits; it runs the same
            # post-apply tests but never pushes (no change request owns it)
            rows = conn.execute(
                "SELECT id, path, sha_before FROM self_edit_log "
                "WHERE status = 'armed' AND reserve_window IS NULL "
                "AND COALESCE(apply_policy, 'idle_or_window') != 'window_only' "
                'ORDER BY created').fetchall()
    if not rows:
        return 0
    kernel_rows = [r for r in rows if is_kernel(_target_from_display(r[1]))]
    if kernel_rows:
        # A kernel edit is applied alone: the restart replaces the process, so
        # mixing it with reload-based edits in one pass has no coherent order.
        row_id, display, sha_before = kernel_rows[0]
        return _apply_kernel(row_id, display, sha_before)
    applied = 0
    landed = []
    for row_id, display, sha_before in rows:
        try:
            if _apply_one(row_id, display, sha_before, window_id):
                applied += 1
                landed.append(row_id)
        except Exception as e:  # one bad edit must never stall the loop
            _update(row_id, status='failed', note=str(e)[:200])
            serve.log_action(row_id, 'self_edit_apply_failed',
                             {'error': str(e)[:200]}, authorized=False)
    if window_id and not landed:
        # every reserved edit conflicted or failed at apply time: the change
        # request produced nothing to validate or push
        note = 'no reserved edits applied (all conflicted or failed at apply time)'
        _update_window(window_id, push_status='failed',
                       push_category='unsuccessful', push_note=note)
        _unreserve_window_edits(window_id)
        serve.log_action(window_id, 'repo_push',
                         {'category': 'unsuccessful', 'reason': note},
                         authorized=True)
        _telegram_change_request(window_id, 'unsuccessful', note)
        return 0
    if not landed:
        return 0
    ok, tail = _run_post_apply_tests()
    serve.log_action('selfedit', 'self_edit_post_apply_tests',
                     {'result': 'pass' if ok else 'fail',
                      'edits': applied, 'window': window_id,
                      'tail': tail[:POST_APPLY_TAIL_LEN]}, authorized=True)
    if ok:
        if window_id:
            _hold_for_validator(window_id, remediation_used=False, tail=tail)
        return applied
    if window_id:
        _mark_remediation(window_id, landed, tail)
        return applied
    _rollback_rows(landed,
                   'post-apply tests failed and no change window was open',
                   'no-window')
    return applied


def _run_test_gate(cwd, label, env=None):
    """Run the repo's own gate script in cwd and return (ok, tail)."""
    serve = _s()
    try:
        done = subprocess.run(
            POST_APPLY_TEST_CMD, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=POST_APPLY_TEST_TIMEOUT_S)
        raw = (done.stdout or b'').decode('utf-8', errors='replace')
        ok = done.returncode == 0
    except subprocess.TimeoutExpired:
        raw = f'{label} test run timed out'
        ok = False
    except Exception as exc:  # a broken test harness fails closed
        raw = f'{label} test run could not start: {exc}'
        ok = False
    tail = raw.strip()[-POST_APPLY_TAIL_LEN:]
    if ok:
        return True, tail
    serve.log_action('selfedit', f'self_edit_{label}_tests_failed',
                     {'tail': tail[:POST_APPLY_TAIL_LEN]}, authorized=True)
    return False, tail


def _run_post_apply_tests():
    """Test the edited tree after applying: run the repo's own gate script
    from the repo root and return (ok, tail). The command is a module
    constant so tests can substitute a fast fake."""
    repo_root = os.path.dirname(os.path.abspath(_world()))
    return _run_test_gate(repo_root, 'post_apply')


PRE_APPLY_IGNORE = (
    '.git', '.github', 'agents', 'sandboxes', 'library', 'datasets',
    'think-tank-db-backups', 'design-references', 'tinyprobe_test',
    '__pycache__', '.pytest_cache', '.mypy_cache', 'node_modules',
    '.selfedit_staging', '.selfedit_backups', '.selfedit_failed',
    'think_tank.db', '*.db', '*.db.bak*', '.env*', '.youtube-cookies.txt',
)

# The pre-apply copy must not carry real credentials. Keys whose names look
# secret get a placeholder value; everything else (numbers, booleans, URLs,
# roster configuration) keeps its real value so the gate sees the same
# feature switches as the live server.
PREAPPLY_SECRET_BITS = ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'COOKIE',
                        'ACCOUNT', 'REFRESH', 'HASH', 'SALT', 'NUMBER')
PREAPPLY_ENV_PLACEHOLDER = 'preapply'


def _write_preapply_env(repo_root, tmp):
    env_path = os.path.join(repo_root, '.env')
    if not os.path.isfile(env_path):
        return
    lines = []
    try:
        with open(env_path, encoding='utf-8') as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                if any(bit in key.upper() for bit in PREAPPLY_SECRET_BITS):
                    value = PREAPPLY_ENV_PLACEHOLDER
                lines.append(f'{key}={value}\n')
    except OSError:
        return
    dest = os.path.join(tmp, '.env')
    with open(dest, 'w', encoding='utf-8') as fh:
        fh.writelines(sorted(lines))
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass


def _init_preapply_db(tmp):
    db_path = os.path.join(tmp, 'think_tank.db')
    env = {**os.environ, 'THINK_TANK_DB': db_path}
    try:
        done = subprocess.run(
            [sys.executable, '-c', 'import serve; serve.init_db()'],
            cwd=os.path.join(tmp, 'world'), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=180)
    except Exception as exc:
        return None, f'pre-apply database init failed: {exc}'
    if done.returncode != 0:
        tail = (done.stdout or b'').decode('utf-8', errors='replace')[-600:]
        return None, 'pre-apply database init failed: ' + tail
    return db_path, None


def _run_pre_apply_tests(rows):
    """Run the repo gate against a quarantine copy with every reserved
    candidate applied. This is the full pre-apply bar; it is used only for
    high-risk change requests because it copies and tests the whole tree.

    Returns (ok, tail). Any harness failure fails closed."""
    serve = _s()
    repo_root = os.path.dirname(os.path.abspath(_world()))
    staging_root = os.path.join(_world(), '.selfedit_staging')
    os.makedirs(staging_root, exist_ok=True)
    tmp = os.path.join(staging_root, f'pre-{secrets.token_hex(6)}')
    try:
        shutil.copytree(repo_root, tmp,
                        ignore=shutil.ignore_patterns(*PRE_APPLY_IGNORE),
                        dirs_exist_ok=True)
        _write_preapply_env(repo_root, tmp)
        db_path, err = _init_preapply_db(tmp)
        if err:
            return False, err
        root_real = os.path.realpath(tmp)
        for row in rows:
            candidate = _staging_candidate(row['id'])
            if not os.path.isfile(candidate):
                return False, f"candidate for {row.get('id')} is missing"
            dest = os.path.realpath(os.path.join(tmp, row['path']))
            if os.path.commonpath([root_real, dest]) != root_real:
                return False, f"candidate path escapes the pre-apply copy: {row['path']}"
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(candidate, 'rb') as src:
                data = src.read()
            with open(dest, 'wb') as out:
                out.write(data)
        ok, tail = _run_test_gate(
            tmp, 'pre_apply',
            env={**os.environ, 'THINK_TANK_DB': db_path})
        serve.log_action('selfedit', 'self_edit_pre_apply_tests',
                         {'result': 'pass' if ok else 'fail',
                          'edits': len(rows),
                          'tail': tail[:POST_APPLY_TAIL_LEN]},
                         authorized=True)
        return ok, tail
    except Exception as exc:
        return False, f'pre-apply gate could not run: {exc}'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _active_window_id():
    """The fired change window covering NOW, or None (idle-path applies
    get no window linkage)."""
    import math
    now = time.time()
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        row = conn.execute(
            "SELECT id FROM dormancy_windows WHERE status = 'fired' "
            'AND start_ts <= ? AND end_ts >= ? '
            'ORDER BY start_ts DESC LIMIT 1',
            (now, math.floor(now))).fetchone()
    return row[0] if row else None


def remediation_window():
    """The open remediation session for serve's wake exemption: a fired
    window still inside its slot with failing edits left. None otherwise.
    Called per request by the middleware -- cheap by design (two small
    indexed queries)."""
    import math
    serve = _s()
    now = time.time()
    with serve._db() as conn:
        _ensure_windows_table(conn)
        window = conn.execute(
            "SELECT id FROM dormancy_windows WHERE status = 'fired' "
            'AND end_ts >= ? ORDER BY start_ts DESC LIMIT 1',
            (math.floor(now),)).fetchone()
        if not window:
            return None
        failing = conn.execute(
            "SELECT COUNT(*) FROM self_edit_log WHERE window_id = ? "
            "AND status = 'failed'", (window[0],)).fetchone()[0]
    return window[0] if failing else None


def _window_participants(window_id, status):
    with _s()._db() as conn:
        _ensure_table(conn)
        return [r[0] for r in conn.execute(
            'SELECT id FROM self_edit_log WHERE window_id = ? '
            'AND status = ?', (window_id, status)).fetchall()]


def _mark_remediation(window_id, landed_ids, tail):
    """Open the remediation session: failed rows keep the latest test tail,
    and every responsible proposer is mailed with what to do until the
    window ends."""
    serve = _s()
    for row_id in landed_ids:
        _update(row_id, status='failed',
                note=f'change window {window_id}: repo tests failed; '
                     f'latest tail: {tail[-200:]}')
    serve.log_action(window_id, 'selfedit_remediation_opened',
                     {'edits': landed_ids}, authorized=True)
    for row_id in landed_ids:
        row = _row_meta(row_id)
        if row:
            agent_id, display = row['agent'], row['path']
            _notify_agent(agent_id, (
                f'Your self-edit {row_id} ({display}) applied inside change '
                f'window {window_id}, but the repo test gate FAILED right '
                f'after. You have until the window ends to fix it: use the '
                f'selfedit_fix tool to splice a correction into the current '
                f'file, then selfedit_recheck to rerun the tests. If the '
                f'issues are not resolved by the end of the window the '
                f'window rolls back automatically, your candidate is '
                f'saved under .selfedit_failed/{window_id}/, and you will '
                f'need to fix the code off-window and schedule again.\n'
                f'Test tail:\n{tail[-POST_APPLY_TAIL_LEN:]}'),
                about=f'selfedit:{row_id}')


def _row_meta(row_id):
    with _s()._db() as conn:
        _ensure_table(conn)
        row = conn.execute(
            'SELECT agent, path FROM self_edit_log '
            'WHERE id = ?', (row_id,)).fetchone()
    return {'agent': row[0], 'path': row[1]} if row else None


def _rollback_rows(row_ids, reason, subfolder):
    """Revert each edit to its pre-edit bytes, save the (rejected)
    candidate under ANOTHER FILE NAME, and re-load the modules. The
    candidate copy is what the responsible agent fixes off-window."""
    serve = _s()
    saved = []
    for row_id in row_ids:
        meta = _row_meta(row_id)
        if not meta:
            continue
        display = meta['path']
        target = _target_from_display(display)
        backups = os.path.join(_world(), '.selfedit_backups')
        pre_bytes = os.path.join(backups, row_id)
        cand_path = os.path.join(backups, f'{row_id}.cand')
        try:
            failed_dir = os.path.join(_world(), '.selfedit_failed', subfolder)
            os.makedirs(failed_dir, exist_ok=True)
            module_name = os.path.splitext(os.path.basename(display))[0]
            new_name = f'{row_id}-{module_name}.py'
            if os.path.isfile(cand_path):
                shutil.copyfile(cand_path, os.path.join(failed_dir, new_name))
                saved.append(new_name)
            if os.path.isfile(pre_bytes):
                shutil.copyfile(pre_bytes, target)
                _reload(target)
            _update(row_id, status='rolled_back',
                    note=f'{reason}; candidate saved as '
                         f'.selfedit_failed/{subfolder}/{new_name}')
            serve.log_action(row_id, 'self_edit_rolled_back',
                             {'reason': reason[:200],
                              'savedAs': (new_name if os.path.isfile(cand_path)
                                          else '')[:160]},
                             authorized=True)
        except Exception as exc:
            _update(row_id, status='failed',
                    note=f'rollback itself failed: {str(exc)[:200]}')
            serve.log_action(row_id, 'self_edit_rollback_failed',
                             {'error': str(exc)[:200]}, authorized=False)
        if meta.get('agent'):
            _notify_agent(meta['agent'], (
                f'Your self-edit {row_id} ({display}) was REVERTED: '
                f'{reason}. The rejected candidate is saved as '
                f'.selfedit_failed/{subfolder}/{new_name} -- use it, '
                f'resolve the issues OFF-window, then file a fresh '
                f'self_edit proposal and schedule a new change window '
                f'with schedule_dormant_apply. nothing changed server-side '
                f'otherwise; the tank is back to the way it was.'),
                about=f'selfedit:{row_id}')
    return saved


def run_remediation_checks(agent_id):
    """Rerun the repo gate NOW during an open remediation session. On pass,
    failing rows recover to applied and the session closes."""
    serve = _s()
    window_id = remediation_window()
    if not window_id:
        return False, {'error': 'no remediation session is open right now '
                                '(the tests pass, or no window is firing)'}
    ok, tail = _run_post_apply_tests()
    serve.log_action(window_id, 'selfedit_remediation_checks',
                     {'by': agent_id or 'unknown',
                      'result': 'pass' if ok else 'fail',
                      'tail': tail[:POST_APPLY_TAIL_LEN]}, authorized=True)
    if ok:
        for row_id in _window_participants(window_id, 'failed'):
            meta = _row_meta(row_id)
            _update(row_id, status='applied',
                    note='tests pass after in-window fixes; recovered to '
                         'applied')
            if meta and meta.get('agent'):
                _notify_agent(meta['agent'], (
                    f'Change window {window_id} checks PASS after the '
                    f'in-window fixes. Your edit is back to applied and '
                    f'clean; the validator now reviews the result before '
                    f'any push.'), about=f'selfedit:{row_id}')
        _update_window(window_id, remediation_used=1)
        _hold_for_validator(window_id, remediation_used=True,
                            tail='tests pass after in-window fixes')
        serve.log_action(window_id, 'selfedit_remediation_cleared', {},
                         authorized=True)
        return True, {'ok': True, 'tests': 'pass',
                      'message': 'the repo gate passes now -- failing '
                                 'edits recovered to applied'}
    return True, {'ok': False, 'tests': 'fail',
                  'tail': tail[-POST_APPLY_TAIL_LEN:]}


def _target_from_display(display):
    """display is repo-root-relative ('world/sim.py'); self-edits only ever
    target world modules, so the real file is basename under world/."""
    return os.path.join(_world(), os.path.basename(display))


# ---------------------------------------------------------------------------
# Kernel apply: serve.py cannot be reloaded, so an edit is written, preflighted
# against the full repo gate, then applied by an in-place os.execv. A detached
# watchdog waits for the new process to answer a public endpoint; if it never
# does, it restores the pre-edit bytes and relaunches the previous kernel.
# ---------------------------------------------------------------------------
_KERNEL_WATCHDOG_SOURCE = """import json, os, shutil, subprocess, sys, time, urllib.request

backup, target, url, cwd, argv_json, timeout = sys.argv[1:7]
argv = json.loads(argv_json)
deadline = time.time() + float(timeout)
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            if resp.status == 200:
                sys.exit(0)
    except Exception:
        pass
    time.sleep(2)
try:
    shutil.copyfile(backup, target)
except Exception:
    pass
try:
    subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
except Exception:
    pass
"""


def _kernel_argv():
    """The exact command line this process was launched with, read from ps so
    the restart reproduces it (host, port, flags) with no guessing. None when
    the command cannot be read -- the caller then refuses to swap the file."""
    try:
        done = subprocess.run(['ps', '-o', 'command=', '-p', str(os.getpid())],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=10)
        command = (done.stdout or b'').decode('utf-8', errors='replace').strip()
    except Exception:
        return None
    if not command:
        return None
    argv = shlex.split(command)
    return argv or None


def _kernel_health_url(argv):
    """Public readiness URL for the relaunched process: the same host/port
    from the launch command, with wildcard binds mapped to loopback. /api/rooms
    is used because /api/health is session-gated and the watchdog has no key."""
    host, port = '127.0.0.1', '8000'
    for i, arg in enumerate(argv):
        if arg == '--host' and i + 1 < len(argv):
            host = argv[i + 1]
        elif arg.startswith('--host='):
            host = arg.split('=', 1)[1]
        elif arg == '--port' and i + 1 < len(argv):
            port = argv[i + 1]
        elif arg.startswith('--port='):
            port = arg.split('=', 1)[1]
    if host in ('0.0.0.0', '::', ''):
        host = '127.0.0.1'
    return f'http://{host}:{port}/api/rooms'


def _spawn_kernel_watchdog(argv, backup, target, health_url):
    path = os.path.join(_world(), KERNEL_WATCHDOG_NAME)
    try:
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(_KERNEL_WATCHDOG_SOURCE)
        subprocess.Popen(
            [sys.executable, path, backup, target, health_url, _world(),
             json.dumps(argv), str(KERNEL_HEALTH_TIMEOUT_S)],
            cwd=_world(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        return True
    except Exception:
        return False


def _run_kernel_preflight():
    """Run the repo gate against the on-disk candidate. The running process
    still holds the old code, so a failing candidate is restored and nothing
    restarts."""
    repo_root = os.path.dirname(os.path.abspath(_world()))
    try:
        done = subprocess.run(
            POST_APPLY_TEST_CMD, cwd=repo_root,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=POST_APPLY_TEST_TIMEOUT_S)
        raw = (done.stdout or b'').decode('utf-8', errors='replace')
        return done.returncode == 0, raw.strip()[-POST_APPLY_TAIL_LEN:]
    except subprocess.TimeoutExpired:
        return False, 'kernel preflight timed out'
    except Exception as exc:
        return False, f'kernel preflight could not start: {exc}'


def _apply_kernel(row_id, display, sha_before):
    """Apply one armed serve.py edit: verify bytes, write, preflight, restart.
    Returns 1 when the file was applied (restart disabled in tests) and 0 when
    the edit failed and the original bytes were restored."""
    serve = _s()
    target = _target_from_display(display)
    if not os.path.isfile(target):
        _update(row_id, status='conflict', note='target missing at apply time')
        return 0
    with open(target, 'r', encoding='utf-8') as fh:
        original = fh.read()
    if hashlib.sha256(original.encode('utf-8')).hexdigest() != sha_before:
        _update(row_id, status='conflict',
                note='target changed after arming -- re-propose against the '
                     'current bytes')
        return 0
    with open(_staging_candidate(row_id), 'r', encoding='utf-8') as fh:
        candidate = fh.read()
    backups = os.path.join(_world(), '.selfedit_backups')
    os.makedirs(backups, exist_ok=True)
    backup = os.path.join(backups, row_id)
    with open(backup, 'w', encoding='utf-8') as fh:
        fh.write(original)
    with open(target, 'w', encoding='utf-8') as fh:
        fh.write(candidate)

    ok, tail = _run_kernel_preflight()
    serve.log_action(row_id, 'self_edit_kernel_preflight',
                     {'path': display, 'result': 'pass' if ok else 'fail',
                      'tail': tail[:POST_APPLY_TAIL_LEN]}, authorized=True)
    argv = _kernel_argv() if ok else None
    if not ok or not argv:
        shutil.copyfile(backup, target)
        note = ('kernel preflight failed' if not ok
                else 'launch command unavailable; refusing to restart')
        _update(row_id, status='failed', note=note)
        serve.log_action(row_id, 'self_edit_kernel_apply_failed',
                         {'path': display, 'note': note}, authorized=False)
        _notify_agent(_row_agent(row_id),
                      f'Your kernel edit to {display} was not applied: {note}. '
                      f'The previous serve.py bytes were restored.\n'
                      f'Test tail:\n{tail[-POST_APPLY_TAIL_LEN:]}',
                      about='selfedit')
        return 0

    _update(row_id, status='applied', applied_at=time.time(),
            note='kernel edit: applied by process restart')
    serve.log_action(row_id, 'self_edit_kernel_applied',
                     {'path': display, 'restart': bool(KERNEL_RESTART_ENABLED)},
                     authorized=True)
    if not KERNEL_RESTART_ENABLED:
        return 1

    health_url = _kernel_health_url(argv)
    watchdog = _spawn_kernel_watchdog(argv, backup, target, health_url)
    if not watchdog:
        # Without the rollback net, do not swap the process.
        shutil.copyfile(backup, target)
        _update(row_id, status='failed', note='watchdog could not start')
        serve.log_action(row_id, 'self_edit_kernel_apply_failed',
                         {'path': display, 'note': 'watchdog could not start'},
                         authorized=False)
        return 0
    try:
        os.chdir(_world())
        os.execv(argv[0], argv)
    except Exception as exc:
        shutil.copyfile(backup, target)
        _update(row_id, status='failed', note=f'restart failed: {exc}'[:200])
        serve.log_action(row_id, 'self_edit_kernel_apply_failed',
                         {'path': display, 'note': f'restart failed: {exc}'[:200]},
                         authorized=False)
        return 0
    return 1


def _row_agent(row_id):
    try:
        with _s()._db() as conn:
            _ensure_table(conn)
            row = conn.execute('SELECT agent FROM self_edit_log WHERE id = ?',
                               (row_id,)).fetchone()
        return (row[0] if row else '') or 'unknown'
    except Exception:
        return 'unknown'


def _schedule_kernel_restart(delay=1.5):
    """Restart after the current response flushes (used by revert). A daemon
    thread is enough: exec replaces the process, and the thread dies with it."""
    import threading

    def _go():
        time.sleep(delay)
        argv = _kernel_argv()
        if not argv:
            return
        try:
            os.chdir(_world())
            os.execv(argv[0], argv)
        except Exception:
            pass

    threading.Thread(target=_go, daemon=True).start()


def _apply_one(row_id, display, sha_before, window_id=None):
    serve = _s()
    target = _target_from_display(display)
    if not os.path.isfile(target):
        _update(row_id, status='conflict', note='target missing at apply time')
        return 0
    with open(target, 'r', encoding='utf-8') as fh:
        original = fh.read()
    if hashlib.sha256(original.encode('utf-8')).hexdigest() != sha_before:
        _update(row_id, status='conflict',
                note='target changed after arming -- re-propose against the '
                     'current bytes')
        return 0
    with open(_staging_candidate(row_id), 'r', encoding='utf-8') as fh:
        candidate = fh.read()
    backups = os.path.join(_world(), '.selfedit_backups')
    os.makedirs(backups, exist_ok=True)
    with open(os.path.join(backups, row_id), 'w', encoding='utf-8') as fh:
        fh.write(original)
    shutil.copyfile(_staging_candidate(row_id),
                    os.path.join(backups, f'{row_id}.cand'))
    with open(target, 'w', encoding='utf-8') as fh:
        fh.write(candidate)
    _reload(target)
    _update(row_id, status='applied', applied_at=time.time(),
            window_id=window_id)
    serve.log_action(row_id, 'self_edit_applied',
                     {'path': display, 'dormant': True,
                      'window': window_id}, authorized=True)
    return 1


def submit_remediation_fix(agent_id, path, find, replace, summary):
    """One corrective splice DURING an open remediation session (inside a
    live change window). The player's window approval already covers
    in-window corrections, so there is no second JEV review: the gates here
    are the mechanical ones (real path, protected refusal, unique anchor,
    compiled splice) and the changes are TESTED by the next recheck. The
    fix itself is what the change window exists to enable."""
    serve = _s()
    agent_id = (agent_id or '').strip() or 'unknown'
    window_id = remediation_window()
    if not window_id:
        return False, {'error': 'no remediation session is open -- submit '
                                'a normal self_edit proposal instead, or '
                                'schedule a change window'}
    summary = (summary or '').strip()[:MAX_SUMMARY_LEN]
    find = find if isinstance(find, str) else ''
    replace = replace if isinstance(replace, str) else ''
    if not find or len(find) > MAX_FIND_LEN:
        return False, {'error': f'find text is required (max {MAX_FIND_LEN} chars)'}
    if len(replace) > MAX_REPLACE_LEN:
        return False, {'error': f'replacement too long (max {MAX_REPLACE_LEN} chars)'}
    if find == replace:
        return False, {'error': 'this fix changes nothing'}
    if not summary:
        return False, {'error': 'a summary is required -- say WHAT broke '
                                'and WHAT this correction changes'}
    resolved, display = _resolve_path(path)
    if resolved is None:
        return False, {'error': display}
    refusal = _path_refusal(resolved, display)
    if refusal:
        return False, {'error': refusal}
    if is_kernel(resolved):
        return False, {'error': 'serve.py is the kernel: an in-window fix '
                                'cannot reload it. Propose a normal kernel '
                                'edit (player signature, preflight, restart).'}
    try:
        with open(resolved, 'r', encoding='utf-8') as fh:
            original = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        return False, {'error': f'cannot read target: {e}'}
    occurrences = original.count(find)
    if occurrences != 1:
        where = 'appears 0 times' if occurrences == 0 else \
            f'appears {occurrences} times'
        return False, {'error': f'the find text must appear exactly once; it '
                                f'{where}. Include more surrounding context '
                                'to make the anchor unique'}
    candidate = original.replace(find, replace, 1)
    syntax_err = _compile_error(candidate)
    if syntax_err:
        return False, {'error': f'fix does not compile: {syntax_err} -- '
                                'correct the splice and resubmit'}
    fix_id = f'fix-{secrets.token_hex(6)}'
    backups = os.path.join(_world(), '.selfedit_backups')
    os.makedirs(backups, exist_ok=True)
    with open(os.path.join(backups, fix_id), 'w', encoding='utf-8') as fh:
        fh.write(original)
    with open(resolved, 'w', encoding='utf-8') as fh:
        fh.write(candidate)
    _reload(resolved)
    _record(fix_id, time.time(), agent_id, display, summary,
            hashlib.sha256(original.encode('utf-8')).hexdigest(),
            hashlib.sha256(candidate.encode('utf-8')).hexdigest(), 'applied')
    _update(fix_id, status='applied', window_id=window_id,
            note=f'in-window fix; pre-fix bytes under backup {fix_id}')
    serve.log_action(fix_id, 'selfedit_fix_applied',
                     {'agent': agent_id, 'path': display,
                      'window': window_id}, authorized=True)
    return True, {'id': fix_id, 'status': 'applied', 'window': window_id,
                  'message': 'fix applied in-window -- run selfedit_recheck '
                             'to test the tree now'}


def _revert_one_from_backup(row_id):
    """Restore one edit's pre-edit bytes from its backup and re-load the
    module. Used by the end-of-window rollback."""
    meta = _row_meta(row_id)
    if not meta:
        return False
    target = _target_from_display(meta['path'])
    backup = os.path.join(_world(), '.selfedit_backups', row_id)
    if not os.path.isfile(backup):
        return False
    shutil.copyfile(backup, target)
    if is_kernel(target):
        _schedule_kernel_restart()
        return True
    try:
        _reload(target)
    except Exception:
        pass
    return True


def _reload(target):
    import importlib
    modname = os.path.splitext(os.path.basename(target))[0]
    mod = sys.modules.get(modname)
    if mod:
        importlib.reload(mod)
    if modname == 'jev':
        _refresh_serve_jev_exports()
    if modname == 'config':
        _refresh_serve_config_exports()
    if modname == 'env_reload':
        _refresh_serve_env_reload_exports()
    _refresh_serve_imports(modname)
    serve = _s()
    for loader_name in POST_RELOAD_LOADERS.get(modname, ()):
        loader = getattr(serve, loader_name, None)
        if callable(loader):
            try:
                loader()
            except Exception:
                pass  # a loader failure leaves the registry empty, never a boot failure
    for extra in RELOAD_IMPORTERS_AFTER.get(modname, ()):
        extra_mod = sys.modules.get(extra)
        if extra_mod:
            importlib.reload(extra_mod)


def _refresh_serve_jev_exports():
    """serve.py's `from jev import (...)` names must point at the reloaded
    jev module's objects after a jev self-edit, or every serve-side call
    site would keep running pre-edit functions (the jev.py apply contract)."""
    import jev
    serve = _s()
    for name in ('JEV_SAFETY_CONFIDENCE', 'QUORUM_SAMPLE_SIZE',
                 'QUORUM_MIN_AGREEING', 'ESCALATION_KIND_RISK',
                 'ESCALATION_DEFAULT_FLOOR', 'ESCALATION_ERROR_PENALTY_STEP',
                 'ESCALATION_ERROR_PENALTY_CAP', '_jev_choice',
                 '_jev_quorum_choice_sync', '_jev_quorum_decision'):
        setattr(serve, name, getattr(jev, name))


_SERVE_IMPORT_NAMES: dict = {}


def _refresh_serve_imports(modname):
    """Rebind every name serve.py imports from modname after a reload.

    The name list is read from serve.py's own `from <mod> import (...)`
    statements with ast, so adding an export later needs no second edit here
    and cannot drift. Function-local imports are irrelevant (call-time
    resolution). A name the reloaded module no longer defines is skipped
    rather than left pointing at the pre-edit object by accident."""
    import ast
    mod = sys.modules.get(modname)
    if mod is None:
        return []
    if modname not in _SERVE_IMPORT_NAMES:
        names = []
        try:
            with open(_s().__file__, 'r', encoding='utf-8') as fh:
                tree = ast.parse(fh.read())
        except (OSError, SyntaxError):
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == modname:
                    for alias in node.names:
                        names.append((alias.asname or alias.name, alias.name))
        _SERVE_IMPORT_NAMES[modname] = tuple(names)
    serve = _s()
    rebound = []
    for attr, source_name in _SERVE_IMPORT_NAMES[modname]:
        if hasattr(mod, source_name):
            setattr(serve, attr, getattr(mod, source_name))
            rebound.append(attr)
    return rebound


def _refresh_serve_env_reload_exports():
    """serve.py binds the .env reload table and its flag helper by name, so
    a self-edit to env_reload.py must rebind both or the pre-edit table keeps
    running until a restart."""
    import env_reload
    serve = _s()
    serve._ENV_RELOAD_FIELDS = env_reload.ENV_RELOAD_FIELDS
    serve._env_flag = env_reload._env_flag


def _refresh_serve_config_exports():
    """serve.py's `from config import (...)` block (line ~222) must point at
    the reloaded config module after a config.py self-edit, and sim/content/
    sublow are reloaded by the RELOAD_IMPORTERS_AFTER chain -- their
    `from config import` bindings re-execute against the updated module.
    The reload only ever runs while the tank is dormant, so it lands between
    simulation ticks."""
    import config
    serve = _s()
    for name in ('ALLOW_WATCHLIST_WRITE', 'BROWSE_BLOCK_CATEGORIES',
                 'PLAIN_WRITING_BAN_LIST', 'RESEARCH_WATCHLIST', 'ROOT',
                 'bind_policy', 'get_grants_config', 'get_operator_policy',
                 'note_watchlist_topic', 'reload_policy_config'):
        setattr(serve, name, getattr(config, name))


def revert(edit_id, by='operator'):
    """Restore an applied edit from its backup and reload. Player or admin
    only; the endpoint enforces that."""
    serve = _s()
    row = _get_row(edit_id)
    if not row:
        return False, {'error': 'unknown edit id'}
    display, status = row[0], row[1]
    if status != 'applied':
        return False, {'error': f'edit is {status!r}, not applied -- '
                                'nothing to revert'}
    backup = os.path.join(_world(), '.selfedit_backups', edit_id)
    if not os.path.isfile(backup):
        return False, {'error': 'backup missing; recover the file from git '
                                'instead (`git checkout -- world/...`)'}
    backup_candidate = os.path.join(_world(), '.selfedit_backups',
                                    f'{edit_id}.cand')
    target = _target_from_display(display)
    shutil.copyfile(backup, target)
    if is_kernel(target):
        _schedule_kernel_restart()
    else:
        _reload(target)
    _update(edit_id, status='reverted', reverted_at=time.time())
    serve.log_action(edit_id, 'self_edit_reverted', {'by': by}, authorized=True)
    return True, {'ok': True, 'status': 'reverted',
                  'backup_candidate_kept': os.path.isfile(backup_candidate)}


def status_snapshot(limit=25):
    """Bounded, newest-first view for the status tool and endpoint."""
    with _s()._db() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            'SELECT id, created, agent, path, summary, status, note, '
            'jev_confidence, validator_note, applied_at, window_id, '
            'apply_policy FROM self_edit_log '
            'ORDER BY created DESC LIMIT ?', (int(limit),)).fetchall()
    return [{'id': r[0], 'created': r[1], 'agent': r[2], 'path': r[3],
             'summary': r[4], 'status': r[5], 'note': r[6],
             'jevConfidence': r[7], 'validator': r[8], 'appliedAt': r[9],
             'windowId': r[10], 'applyPolicy': r[11] or 'idle_or_window'}
            for r in rows]


# ---------------------------------------------------------------------------
# One-shot dormancy windows (the change-request process for applying edits).
# See the docstring contract above: conflict scan -> supervisor JEV judgment
# -> player-only floor-1.0 escalation link. A window owns a wall-clock slot;
# the firing loop lives in serve (_dormancy_window_loop) and calls
# window_tick() through asyncio.to_thread.
# ---------------------------------------------------------------------------

WINDOW_MIN_DURATION_MIN = 5
WINDOW_MAX_DURATION_MIN = 240
WINDOW_DEFAULT_DURATION_MIN = 15
WINDOW_MIN_LEAD_S = 5 * 60
WINDOW_MAX_LEAD_S = 30 * 24 * 60 * 60

# Validator turn (operator decision, 2026-10-19). The tank is dormant while a
# change window holds for validation, so the sim tick cannot run and the
# validator cannot be reached through the normal mailbox -> task path. The
# dormancy loop therefore runs ONE narrow decision turn for the named
# validator only. It is the same two-agent contract, not a new approver: the
# decision still goes through validate_window(), and only the window's own
# validator identity may record it.
VALIDATOR_TURN_MAX_ATTEMPTS = 3
VALIDATOR_TURN_RETRY_S = 60
VALIDATOR_TURN_MAX_DIFF_CHARS = 16000
VALIDATOR_TURN_MAX_TAIL_CHARS = 6000
_VALIDATION_LOCK = threading.Lock()
MAX_WINDOW_REASON_LEN = 500
MAX_WINDOW_TITLE_LEN = 200
MAX_WINDOW_TEXT_LEN = 4000
MAX_MANIFEST_ITEMS = 100
MAX_BLACKOUT_ITEMS = 50

WINDOW_CHANGE_TYPES = ('standard', 'normal', 'emergency')
WINDOW_RISK_LEVELS = ('low', 'medium', 'high', 'critical')
WINDOW_SERVICE_MODES = ('idle_ok', 'batch', 'event_driven',
                        'always_on_trading')
HIGH_RISK_LEVELS = ('high', 'critical')
HIGH_RISK_SERVICE_MODES = ('event_driven', 'always_on_trading')
MAX_TRADING_WINDOW_MIN = 5
MAX_EVENT_WINDOW_MIN = 15


def _norm_choice(value, allowed, default):
    """Return the normalized choice, the default when omitted, or None for
    an invalid value. None must be rejected by the caller: silently mapping a
    typo like 'hgih' to 'low' would bypass the high-risk gate."""
    if value is None or not str(value).strip():
        return default
    text = str(value).strip().lower()
    return text if text in allowed else None


def _is_high_risk(risk_level, service_mode):
    return (risk_level in HIGH_RISK_LEVELS
            or service_mode in HIGH_RISK_SERVICE_MODES)


def _manifest_paths(items):
    """Normalize a declared file manifest to a list of repo paths."""
    if items is None:
        return None, None
    if not isinstance(items, (list, tuple)):
        return None, 'fileManifest must be a list of paths or {path} objects'
    out = []
    for item in items:
        if isinstance(item, str):
            path = item.strip()
        elif isinstance(item, dict):
            path = str(item.get('path') or '').strip()
        else:
            path = ''
        if not path:
            return None, 'fileManifest entries need a non-empty path'
        if path not in out:
            out.append(path)
        if len(out) > MAX_MANIFEST_ITEMS:
            return None, f'fileManifest is limited to {MAX_MANIFEST_ITEMS} paths'
    return out, None


def _ack_agents(items):
    """Normalize acknowledgements to a list of {agent, at} records."""
    if items is None:
        return None, None
    if not isinstance(items, (list, tuple)):
        return None, 'acknowledgements must be a list of agent ids'
    out = []
    now = time.time()
    for item in items:
        if isinstance(item, str):
            agent = item.strip()
            at = now
        elif isinstance(item, dict):
            agent = str(item.get('agent') or item.get('id') or '').strip()
            try:
                at = float(item.get('at') or now)
            except (TypeError, ValueError):
                at = now
        else:
            agent = ''
            at = now
        if not agent:
            return None, 'each acknowledgement needs a non-empty agent id'
        if not any(r['agent'] == agent for r in out):
            out.append({'agent': agent, 'at': at})
        if len(out) > MAX_MANIFEST_ITEMS:
            return None, 'too many acknowledgements'
    return out, None


def _parse_clock(value, fmt):
    import datetime as _dt
    try:
        return _dt.datetime.strptime((value or '').strip(), fmt)
    except ValueError:
        return None


def _parse_blackout_windows(items):
    """Normalize blackout windows.

    Each item is either an absolute interval
    {"start": "YYYY-MM-DDTHH:MM", "end": "...", "label": "..."}
    or a daily recurrence
    {"start": "HH:MM", "end": "HH:MM", "days": [0..6], "label": "..."}.
    Monday is 0. Local tank time is used for both forms.
    """
    if items is None:
        return None, None
    if not isinstance(items, (list, tuple)):
        return None, 'blackoutWindows must be a list of intervals'
    out = []
    for item in items:
        if not isinstance(item, dict):
            return None, 'each blackout window must be an object'
        start_raw = str(item.get('start') or '').strip()
        end_raw = str(item.get('end') or '').strip()
        label = str(item.get('label') or '').strip()[:120]
        if not start_raw or not end_raw:
            return None, 'each blackout window needs start and end'
        absolute = False
        start_dt = end_dt = None
        for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M',
                    '%Y-%m-%dT%H:%M:%S'):
            start_dt = _parse_clock(start_raw, fmt)
            end_dt = _parse_clock(end_raw, fmt)
            if start_dt is not None and end_dt is not None:
                absolute = True
                break
        if absolute:
            if end_dt <= start_dt:
                return None, f'blackout {label or start_raw} must end after it starts'
            out.append({'kind': 'absolute', 'label': label,
                        'start': start_dt.strftime('%Y-%m-%dT%H:%M'),
                        'end': end_dt.strftime('%Y-%m-%dT%H:%M')})
        else:
            start_clock = _parse_clock(start_raw, '%H:%M')
            end_clock = _parse_clock(end_raw, '%H:%M')
            if start_clock is None or end_clock is None:
                return None, ('blackout windows need absolute '
                              'YYYY-MM-DDTHH:MM times or HH:MM daily times')
            if end_clock <= start_clock:
                return None, f'blackout {label or start_raw} must end after it starts'
            days = item.get('days')
            if days is None:
                days = list(range(7))
            if not isinstance(days, (list, tuple)):
                return None, 'blackout days must be a list of 0..6'
            try:
                days = sorted({int(d) for d in days})
            except (TypeError, ValueError):
                return None, 'blackout days must be a list of 0..6'
            if any(d < 0 or d > 6 for d in days):
                return None, 'blackout days must be 0..6 (Monday is 0)'
            out.append({'kind': 'daily', 'label': label,
                        'start': start_clock.strftime('%H:%M'),
                        'end': end_clock.strftime('%H:%M'),
                        'days': days})
        if len(out) > MAX_BLACKOUT_ITEMS:
            return None, f'blackoutWindows is limited to {MAX_BLACKOUT_ITEMS}'
    return out, None


def _blackout_conflicts(start_ts, end_ts, windows):
    """Return the blackout labels covering any minute of the slot."""
    import datetime as _dt
    if not windows:
        return []
    hits = []
    step = 60
    cursor = start_ts
    while cursor < end_ts:
        moment = _dt.datetime.fromtimestamp(cursor)
        minute = moment.hour * 60 + moment.minute
        for window in windows:
            if window.get('kind') == 'absolute':
                w_start = _parse_clock(window.get('start'), '%Y-%m-%dT%H:%M')
                w_end = _parse_clock(window.get('end'), '%Y-%m-%dT%H:%M')
                if not w_start or not w_end:
                    continue
                if w_start.timestamp() <= cursor < w_end.timestamp():
                    hits.append(window)
            else:
                if moment.weekday() not in (window.get('days') or []):
                    continue
                w_start = _parse_clock(window.get('start'), '%H:%M')
                w_end = _parse_clock(window.get('end'), '%H:%M')
                if not w_start or not w_end:
                    continue
                start_min = w_start.hour * 60 + w_start.minute
                end_min = w_end.hour * 60 + w_end.minute
                if start_min <= minute < end_min:
                    hits.append(window)
        if hits:
            break
        cursor += step
    out = []
    for window in hits:
        label = window.get('label') or 'blackout window'
        if label not in out:
            out.append(label)
    return out


def _reserved_edit_rows(agent_id):
    with _s()._db() as conn:
        _ensure_table(conn)
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT id, path, apply_policy FROM self_edit_log "
            "WHERE status = 'armed' AND agent = ? ORDER BY created",
            (agent_id,)).fetchall()]


# The post-apply gate: the change window tests the LIVE tree after edits
# land, using the repo's own test gate (the same bar every contributors'
# change must meet). A long window is not needed for tests to finish, but a
# window shorter than the test script would roll back half-seen work -- keep
# the default 15 minutes and the gate capped so a stuck runner cannot eat a
# whole window.
POST_APPLY_TEST_CMD = ['bash', 'world/tests/run_all.sh']
POST_APPLY_TEST_TIMEOUT_S = 900
POST_APPLY_TAIL_LEN = 2000

# Push contract (operator decision, 2026-10-18). A windowed change request
# ends in a push to origin main ONLY after the repo gate passed on the live
# tree AND a second agent (the validator, never one of the proposers)
# approved the result. Outcomes are categorized; successful_with_issues
# cannot land without a note. Git failures and protocol failures roll the
# window back exactly like a failed gate, so local main never sits ahead of
# origin with unpushed validated bytes.
PUSH_CATEGORIES = ('successful', 'successful_with_issues', 'unsuccessful')
MAX_PUSH_NOTE_LEN = 800
GIT_STEP_TIMEOUT_S = 120

_WINDOW_JEV_KIND = 'dormancy window request'

_WINDOW_INSTRUCTIONS = (
    'You are the supervisor of an AI think tank agent that wants to schedule '
    'a short window during which the WHOLE think tank goes dormant (the '
    'simulation pauses) so its already-reviewed code self-edits can be '
    'applied and reloaded. Sleeping the tank interrupts every working agent '
    'for the length of the window. Approve only when the stated reason is a '
    'real maintenance need, the slot is a reasonable off-hours or quiet time '
    'rather than the middle of active work, and the duration is modest. '
    'Reject when the reason is vague, the slot would collide with normal '
    'working hours without justification, or the window looks like an excuse '
    'to pause oversight. The player still has to approve the window '
    'afterwards; this judgment is the supervisory pre-clearance.'
)
_WINDOW_CRITERIA = (
    'Date, start time and duration for the sleep window (given).\n'
    'A stated maintenance reason (given).\n'
    'Reject vague reasons, suspicious slots (prime working hours without '
    'justification), and oversized durations.\n'
    'The player approves or denies the final link separately.'
)


def _ensure_windows_table(conn):
    conn.execute(
        'CREATE TABLE IF NOT EXISTS dormancy_windows ('
        'id TEXT PRIMARY KEY, created REAL NOT NULL, agent TEXT NOT NULL, '
        'date TEXT NOT NULL, start TEXT NOT NULL, start_ts REAL NOT NULL, '
        'end_ts REAL NOT NULL, duration_min INTEGER NOT NULL, '
        'reason TEXT NOT NULL, status TEXT NOT NULL, note TEXT, '
        'escalation_id TEXT, fired_at REAL)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_dormancy_windows_status '
                 'ON dormancy_windows (status, start_ts)')
    # Push contract (operator decision, 2026-10-18): a change request is
    # completed by TWO agents -- the implementer requests the window, a
    # DIFFERENT agent validates the applied result, and only then may the
    # validated bytes be pushed to origin main. Every push outcome carries a
    # category (successful / successful_with_issues / unsuccessful); a
    # successful_with_issues push REQUIRES a non-empty note.
    for col, decl in (
            ('validator', 'TEXT'),
            ('validator_status', 'TEXT'),
            ('validator_note', 'TEXT'),
            ('validated_at', 'REAL'),
            ('remediation_used', 'INTEGER'),
            ('gate_tail', 'TEXT'),
            ('validator_attempt_at', 'REAL'),
            ('validator_attempts', 'INTEGER'),
            ('push_status', 'TEXT'),
            ('push_category', 'TEXT'),
            ('push_note', 'TEXT'),
            ('push_sha', 'TEXT'),
            ('pushed_at', 'REAL'),
            ('admin_status', 'TEXT'),
            ('admin_note', 'TEXT'),
            ('admin_approved_at', 'REAL'),
            ('admin_agent', 'TEXT'),
            ('supervisor', 'TEXT'),
            # Change-request detail (2026-10-22): enough context to review
            # the change, not just the slot. High-risk modes require these.
            ('title', 'TEXT'),
            ('description', 'TEXT'),
            ('change_type', 'TEXT'),
            ('risk_level', 'TEXT'),
            ('service_mode', 'TEXT'),
            ('affected_services', 'TEXT'),
            ('file_manifest', 'TEXT'),
            ('rollback_plan', 'TEXT'),
            ('rollback_owner', 'TEXT'),
            ('success_criteria', 'TEXT'),
            ('abort_criteria', 'TEXT'),
            ('post_apply_checks', 'TEXT'),
            ('safe_state_plan', 'TEXT'),
            ('restore_plan', 'TEXT'),
            ('on_call', 'TEXT'),
            ('acknowledgements', 'TEXT'),
            ('blackout_windows', 'TEXT'),
            ('pre_apply_result', 'TEXT'),
            ('pre_apply_tail', 'TEXT'),
            ('fire_recheck', 'TEXT'),
            ('deferred_reason', 'TEXT'),
            ('vetoed_by', 'TEXT'),
            ('vetoed_at', 'REAL'),
            ('veto_reason', 'TEXT'),
            ('veto_token', 'TEXT')):
        try:
            conn.execute(f'ALTER TABLE dormancy_windows ADD COLUMN {col} {decl}')
        except sqlite3.OperationalError:
            pass  # column already exists


def _notify_agent(agent_id, text, about=''):
    """File one action-needed mail item for an agent (the sim mailbox is
    how server-originated directions reach them). Reads and saves the full
    state around the mailbox mutation, mirroring sim's own callers."""
    serve = _s()
    try:
        import sim as _sim
        state = serve.get_state_from_db() or {}
        payload = {'text': text}
        if about:
            payload['about'] = about
        dispatched = _sim._deliver_mail(state, agent_id, 'selfedit', payload)
        if dispatched:
            serve.save_state_to_db(state)
        return dispatched
    except Exception as exc:
        serve.log_action('selfedit', 'selfedit_mail_failed',
                         {'agent': agent_id, 'error': str(exc)[:200]},
                         authorized=False)
        return False


def _window_record(window_id, created, agent, date, start, start_ts, end_ts,
                   duration_min, reason, status):
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        conn.execute(
            'INSERT INTO dormancy_windows (id, created, agent, date, start, '
            'start_ts, end_ts, duration_min, reason, status) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (window_id, created, agent, date, start, start_ts, end_ts,
             duration_min, reason, status))


def _update_window(window_id, **fields):
    if not fields:
        return
    cols = ', '.join(f'{k} = ?' for k in fields)
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        conn.execute(f'UPDATE dormancy_windows SET {cols} WHERE id = ?',
                     (*fields.values(), window_id))


def _occupied_window_conflicts(start_ts, end_ts, exclude_window_id=None):
    """Pending/approved/fired dormancy windows that overlap the slot."""
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        rows = conn.execute(
            "SELECT id, date, start, duration_min, agent FROM "
            "dormancy_windows WHERE status IN ('pending', 'approved', "
            "'fired') AND start_ts < ? AND end_ts > ? ORDER BY start_ts",
            (end_ts, start_ts)).fetchall()
    return [{'kind': 'dormancy window', 'name': r[0],
             'date': r[1], 'start': r[2], 'durationMin': r[3],
             'agent': r[4]} for r in rows
            if not exclude_window_id or r[0] != exclude_window_id]


def _standing_task_conflicts(start_ts, end_ts):
    """Standing-task schedule windows covering any minute of the slot.

    Mirrors the declaration -> schedule.StandingTask conversion the sim
    sweep uses, and reads the scheduler's live runtime state so hours-mode
    anchors are honored instead of treated as always-on. One sample per
    minute: a 4-hour window is 240 samples of a pure predicate."""
    import datetime as _dt
    import schedule as _schedule
    import sim as _sim
    state = _s().get_state_from_db() or {}
    runtime = {}
    scheduler = getattr(_sim, '_STANDING_SCHEDULER', None)
    if scheduler is not None:
        runtime = dict(getattr(scheduler, 'states', {}) or {})
    conflicts = []
    now_iter = start_ts
    seen_tasks = set()
    while now_iter < end_ts:
        now_dt = _dt.datetime.fromtimestamp(now_iter)
        for declaration in (state.get('standingTasks') or []):
            if not isinstance(declaration, dict):
                continue
            task_id = declaration.get('taskId')
            if task_id in seen_tasks:
                continue
            try:
                task = _schedule.StandingTask(
                    task_id=task_id,
                    name=declaration.get('name') or declaration.get('taskId') or task_id,
                    mode=declaration.get('mode') or _schedule.ALWAYS,
                    agent_count=declaration.get('agentCount'),
                    duration_hours=declaration.get('durationHours'),
                    start_time=declaration.get('startTime'),
                    end_time=declaration.get('endTime'),
                    days_of_week=set(declaration.get('daysOfWeek') or ()),
                    day_of_month=declaration.get('dayOfMonth'),
                    yearly_date=(tuple(declaration['yearlyDate'])
                                 if declaration.get('yearlyDate') else None))
            except (TypeError, ValueError):
                continue
            state_rt = runtime.get(task_id) or _schedule.TaskState()
            if _schedule.StandingScheduler().is_active(task, state_rt, now_dt):
                seen_tasks.add(task_id)
                conflicts.append({
                    'kind': 'standing task', 'name': task.name,
                    'taskId': task_id,
                    'mode': task.mode,
                    'startTime': task.start_time or '',
                    'endTime': task.end_time or ''})
                break
        now_iter += 60
    return conflicts


def _window_conflicts(start_ts, end_ts, exclude_window_id=None):
    return (_standing_task_conflicts(start_ts, end_ts)
            + _occupied_window_conflicts(start_ts, end_ts,
                                         exclude_window_id=exclude_window_id))


def _parse_window_target(date_text, start_text, now):
    """Parse 'YYYY-MM-DD' + 'HH:MM' into a local timestamp. Returns
    (start_ts, end_of_day_ts, None) or (None, None, error)."""
    import calendar
    import datetime as _dt
    try:
        parsed_date = time.strptime((date_text or '').strip(), '%Y-%m-%d')
    except ValueError:
        return None, None, 'date must be YYYY-MM-DD'
    try:
        parsed_time = time.strptime((start_text or '').strip(), '%H:%M')
    except ValueError:
        return None, None, 'start must be HH:MM (24h)'
    y, m, d = parsed_date.tm_year, parsed_date.tm_mon, parsed_date.tm_mday
    hh, mm = parsed_time.tm_hour, parsed_time.tm_min
    try:
        start_ts = time.mktime((y, m, d, hh, mm, 0, 0, 0, -1))
    except (OverflowError, ValueError) as e:
        return None, None, f'invalid window start: {e}'
    last_day = calendar.monthrange(y, m)[1]
    day_end = time.mktime((y, m, min(d, last_day), 23, 59, 0, 0, 0, -1))
    _dt.datetime.fromtimestamp(start_ts)  # raises on impossible times (DST gap)
    return start_ts, day_end, None


def request_window(agent_id, date, start, duration_min, reason, validator=None,
                   title=None, description=None, change_type='normal',
                   risk_level='low', service_mode='idle_ok',
                   affected_services=None, file_manifest=None,
                   rollback_plan=None, rollback_owner=None,
                   success_criteria=None, abort_criteria=None,
                   post_apply_checks=None, safe_state_plan=None,
                   restore_plan=None, on_call=None,
                   acknowledgements=None, blackout_windows=None,
                   notify_created=False):
    """Run the change-request chain for one dormancy slot.

    Returns (ok, payload); ok=False carries the refusal (with 'conflicts'
    when the slot was blocked by the schedule). ok=True means the supervisor
    judgment passed and the window is approved or waiting on the player link.

    Two-agent contract (operator decision, 2026-10-18): the requester is the
    IMPLEMENTER and must name a DIFFERENT agent as validator; the validator
    reviews the applied result before anything is pushed to main. The
    implementer's currently armed edits are RESERVED for this window -- they
    apply inside the slot and nowhere else, so the validator reviews exactly
    the change request's bytes.

    High-risk changes (risk high/critical, or an always-on service mode)
    must declare the files, the rollback plan, success and abort criteria,
    post-apply checks, an on-call owner, acknowledgements, and window-only
    edits. Trading and event modes also declare a safe-state plan, a restore
    plan, and blackout windows. Those requests run the full repo gate on a
    quarantine copy before they are scheduled.
    """
    serve = _s()
    agent_id = (agent_id or '').strip() or 'unknown'
    reason = (reason or '').strip()[:MAX_WINDOW_REASON_LEN]
    if not reason:
        return False, {'error': 'a reason is required -- say WHY the tank '
                                'should sleep then (emergency fix, risky '
                                'reload, quiet hours maintenance)'}
    validator = (validator or '').strip()
    change_type = _norm_choice(change_type, WINDOW_CHANGE_TYPES, 'normal')
    if change_type is None:
        return False, {'error': 'changeType must be one of: '
                                + ', '.join(WINDOW_CHANGE_TYPES)}
    risk_level = _norm_choice(risk_level, WINDOW_RISK_LEVELS, 'low')
    if risk_level is None:
        return False, {'error': 'riskLevel must be one of: '
                                + ', '.join(WINDOW_RISK_LEVELS)}
    service_mode = _norm_choice(service_mode, WINDOW_SERVICE_MODES, 'idle_ok')
    if service_mode is None:
        return False, {'error': 'serviceMode must be one of: '
                                + ', '.join(WINDOW_SERVICE_MODES)}
    title = (title or '').strip()[:MAX_WINDOW_TITLE_LEN]
    description = (description or '').strip()[:MAX_WINDOW_TEXT_LEN]
    rollback_plan = (rollback_plan or '').strip()[:MAX_WINDOW_TEXT_LEN]
    rollback_owner = (rollback_owner or '').strip()[:200]
    success_criteria = (success_criteria or '').strip()[:MAX_WINDOW_TEXT_LEN]
    abort_criteria = (abort_criteria or '').strip()[:MAX_WINDOW_TEXT_LEN]
    post_apply_checks = (post_apply_checks or '').strip()[:MAX_WINDOW_TEXT_LEN]
    safe_state_plan = (safe_state_plan or '').strip()[:MAX_WINDOW_TEXT_LEN]
    restore_plan = (restore_plan or '').strip()[:MAX_WINDOW_TEXT_LEN]
    on_call = (on_call or '').strip()[:200]
    try:
        duration_min = int(duration_min or WINDOW_DEFAULT_DURATION_MIN)
    except (TypeError, ValueError):
        duration_min = WINDOW_DEFAULT_DURATION_MIN
    duration_min = max(WINDOW_MIN_DURATION_MIN,
                       min(WINDOW_MAX_DURATION_MIN, duration_min))
    if service_mode == 'always_on_trading':
        duration_min = min(duration_min, MAX_TRADING_WINDOW_MIN)
    elif service_mode == 'event_driven':
        duration_min = min(duration_min, MAX_EVENT_WINDOW_MIN)
    now = time.time()
    start_ts, _day_end, err = _parse_window_target(date, start, now)
    if err:
        return False, {'error': err}
    if start_ts < now + WINDOW_MIN_LEAD_S:
        return False, {'error': 'the window must start at least 5 minutes '
                                'from now -- pick a future slot'}
    if start_ts > now + WINDOW_MAX_LEAD_S:
        return False, {'error': 'the window may not be more than 30 days out'}
    end_ts = start_ts + duration_min * 60
    conflicts = _window_conflicts(start_ts, end_ts)
    if conflicts:
        listed = '; '.join(
            f"{c['kind']} {c['name']}"
            + (f" ({c.get('startTime') or c['date']} "
               f"{c.get('start', '')})" if (c.get('startTime') or c['date'])
               else '')
            for c in conflicts)
        serve.log_action(agent_id, 'dormancy_window_conflict',
                         {'date': date, 'start': start, 'conflicts': listed[:300]},
                         authorized=True)
        return False, {'error': 'the requested slot collides with already '
                                'scheduled items -- pick another time: '
                                f'{listed}', 'conflicts': conflicts}
    # two-agent change requests (operator decision, 2026-10-18): the
    # implementer names a DIFFERENT validator, and the request only proceeds
    # if the implementer actually has armed edits to reserve for the slot
    if not validator:
        return False, {'error': 'a validator is required: name a DIFFERENT '
                                'agent who will review the applied result '
                                'before it is pushed to GitHub'}
    if validator == agent_id:
        return False, {'error': 'the validator must be a different agent than '
                                'the implementer -- change requests complete '
                                'with two agents (implementer + validator)'}
    state = serve.get_state_from_db() or {}
    roster = state.get('agents') or {}
    names = (set(roster.keys()) if isinstance(roster, dict)
             else {a.get('id') for a in roster if isinstance(a, dict)})
    if names and validator not in names:
        return False, {'error': f'{validator} is not a known agent -- name a '
                                'real validator from the tank roster'}
    admin_mode = governance.admin_change_request_enabled(state)
    if admin_mode and names and not serve._is_director(state, validator):
        return False, {'error': 'the validator must be a director -- change '
                                'requests need a director approval, not a '
                                'peer worker'}
    if admin_mode and validator == serve._admin_agent_id(state):
        return False, {'error': 'name a director other than the admin; the '
                                'admin approves the push as the second, '
                                'independent approver'}
    reserved_rows = _reserved_edit_rows(agent_id)
    if not reserved_rows:
        return False, {'error': 'you have no armed edits to schedule a window '
                                'for -- propose and arm your changes first '
                                '(self_edit), then schedule the window'}
    armed_ids = [r['id'] for r in reserved_rows]
    reserved_paths = [r['path'] for r in reserved_rows]
    high_risk = _is_high_risk(risk_level, service_mode)
    if high_risk:
        unsafe = [r['path'] for r in reserved_rows
                  if (r.get('apply_policy') or 'idle_or_window') != 'window_only']
        if unsafe:
            return False, {'error': (
                'high-risk change requests require window-only edits; '
                're-propose the following paths with windowOnly=true so '
                'they cannot apply on the idle path: '
                + ', '.join(sorted(set(unsafe))))}
    manifest, err = _manifest_paths(file_manifest)
    if err:
        return False, {'error': err}
    if high_risk and manifest is None:
        return False, {'error': 'fileManifest is required for a high-risk or '
                                'always-on change request: list every path '
                                'the window will modify'}
    if manifest is None:
        manifest = list(dict.fromkeys(reserved_paths))
    if sorted(manifest) != sorted(set(reserved_paths)):
        return False, {'error': 'fileManifest does not match the reserved '
                                'edits -- declared: '
                                f'{sorted(manifest)}; reserved: '
                                f'{sorted(set(reserved_paths))}'}
    acks, err = _ack_agents(acknowledgements)
    if err:
        return False, {'error': err}
    if high_risk:
        missing = [name for name, value in (
            ('title', title), ('description', description),
            ('rollbackPlan', rollback_plan), ('rollbackOwner', rollback_owner),
            ('successCriteria', success_criteria),
            ('abortCriteria', abort_criteria),
            ('postApplyChecks', post_apply_checks), ('onCall', on_call),
        ) if not value]
        if missing:
            return False, {'error': 'high-risk change requests require: '
                                    + ', '.join(missing)}
        if not acks:
            return False, {'error': 'high-risk change requests require at '
                                    'least one acknowledgement from the '
                                    'affected team or risk owner'}
        if service_mode in HIGH_RISK_SERVICE_MODES:
            trading_missing = [name for name, value in (
                ('safeStatePlan', safe_state_plan),
                ('restorePlan', restore_plan),
            ) if not value]
            if trading_missing:
                return False, {'error': 'trading and event modes require: '
                                        + ', '.join(trading_missing)}
    if names:
        unknown = [a['agent'] for a in (acks or []) if a['agent'] not in names]
        if unknown:
            return False, {'error': 'unknown acknowledgement agent(s): '
                                    + ', '.join(unknown)}
    blackouts, err = _parse_blackout_windows(blackout_windows)
    if err:
        return False, {'error': err}
    if service_mode in HIGH_RISK_SERVICE_MODES and not blackouts:
        return False, {'error': 'trading and event modes require '
                                'blackoutWindows: declare the market, event, '
                                'settlement, or funding windows the change '
                                'must not overlap'}
    blackout_hits = _blackout_conflicts(start_ts, end_ts, blackouts)
    if blackout_hits:
        return False, {'error': 'the requested slot overlaps a declared '
                                'blackout window: ' + '; '.join(blackout_hits)}
    pre_apply_result = None
    pre_apply_tail = ''
    if high_risk:
        ok_tests, pre_apply_tail = _run_pre_apply_tests(reserved_rows)
        if not ok_tests:
            serve.log_action(agent_id, 'dormancy_window_pre_apply_failed',
                             {'tail': pre_apply_tail[:400]}, authorized=True)
            return False, {'error': 'the pre-apply repo gate failed against '
                                    'the candidate tree -- nothing was '
                                    'scheduled', 'detail': pre_apply_tail[-2000:]}
        pre_apply_result = 'pass'
    affected = (affected_services if isinstance(affected_services, list)
                else ([affected_services] if affected_services else []))
    affected = [str(a).strip()[:120] for a in affected if str(a).strip()]
    question = (f'Agent {agent_id} requests a dormancy window to apply '
                f'reviewed code self-edits:\n'
                f'Date: {date}  Start: {start} (tank local time)  '
                f'Duration: {duration_min} min\n'
                f'Reason: {reason}\n'
                f'Change type: {change_type}  Risk: {risk_level}  '
                f'Service mode: {service_mode}\n'
                f'Files: {", ".join(manifest)}\n'
                f'Rollback: {rollback_plan[:300] or "(not declared)"}\n\n'
                f'Approving sets the tank dormant for that slot so armed '
                f'self-edits apply. The supervisor pre-cleared this request '
                f'through the JEV quorum.')
    decision, confidence, cost = serve._jev_quorum_choice_sync(
        _WINDOW_INSTRUCTIONS, _WINDOW_CRITERIA + '\n\n' + question)
    if decision != 'approve':
        serve.log_action(agent_id, 'dormancy_window_refused',
                         {'decision': decision, 'confidence': confidence},
                         authorized=True)
        return False, {'error': f'the supervisor review said {decision!r} '
                                f'at {round(float(confidence or 0.0), 2)} '
                                'confidence -- nothing was scheduled',
                       'status': 'refused'}
    window_id = f'win-{secrets.token_hex(5)}'
    veto_token = secrets.token_urlsafe(24)
    if admin_mode:
        esc_id = None  # no player link in the two-agent chain
        _window_record(window_id, now, agent_id, date, start, start_ts, end_ts,
                       duration_min, reason, 'approved')
    else:
        esc_id = serve.create_escalation(_WINDOW_JEV_KIND, question,
                                         on_approve_note=window_id)
        _window_record(window_id, now, agent_id, date, start, start_ts, end_ts,
                       duration_min, reason, 'pending')
    _update_window(
        window_id, escalation_id=esc_id, validator=validator,
        validator_status='pending', supervisor=serve._supervisor_of(state, agent_id),
        title=title or None, description=description or None,
        change_type=change_type, risk_level=risk_level,
        service_mode=service_mode,
        affected_services=json.dumps(affected) if affected else None,
        file_manifest=json.dumps(manifest),
        rollback_plan=rollback_plan or None,
        rollback_owner=rollback_owner or None,
        success_criteria=success_criteria or None,
        abort_criteria=abort_criteria or None,
        post_apply_checks=post_apply_checks or None,
        safe_state_plan=safe_state_plan or None,
        restore_plan=restore_plan or None,
        on_call=on_call or None,
        acknowledgements=json.dumps(acks or []),
        blackout_windows=json.dumps(blackouts or []),
        pre_apply_result=pre_apply_result,
        pre_apply_tail=pre_apply_tail[-POST_APPLY_TAIL_LEN:] or None,
        veto_token=veto_token,
        **({'admin_status': 'pending'} if admin_mode else {}))
    with serve._db() as conn:
        _ensure_table(conn)
        for row_id in armed_ids:
            conn.execute('UPDATE self_edit_log SET reserve_window = ? '
                         'WHERE id = ?', (window_id, row_id))
    for ack in (acks or []):
        _notify_agent(ack['agent'], (
            f'Change request {window_id} is scheduled by {agent_id} for '
            f'{date} {start} local, {duration_min} min. You are listed as an '
            f'acknowledged party. Risk {risk_level}; service mode '
            f'{service_mode}. Files: {", ".join(manifest)}.'),
            about=f'selfedit:{window_id}')
    serve.log_action(window_id, 'dormancy_window_requested',
                     {'agent': agent_id, 'date': date, 'start': start,
                      'durationMin': duration_min, 'escalationId': esc_id,
                      'supervisorDecision': 'approved', 'validator': validator,
                      'reservedEdits': armed_ids, 'riskLevel': risk_level,
                      'serviceMode': service_mode, 'changeType': change_type,
                      'files': manifest, 'highRisk': high_risk,
                      'preApply': pre_apply_result},
                     authorized=True)
    if notify_created:
        _telegram_change_request_created(window_id)
    return True, {'id': window_id,
                  'status': 'approved' if admin_mode else 'pending',
                  'escalationId': esc_id,
                  'durationMin': duration_min, 'validator': validator,
                  'reservedEdits': armed_ids,
                  'fileManifest': manifest,
                  'riskLevel': risk_level,
                  'serviceMode': service_mode,
                  'changeType': change_type,
                  'acknowledgements': acks or [],
                  'preApplyResult': pre_apply_result,
                  'message': (f'window approved by the supervisor; '
                              f'{len(armed_ids)} armed edit(s) are reserved. '
                              f'{validator} must approve the applied result '
                              f'and the admin must approve the push.'
                              if admin_mode else
                              f'window requested -- the supervisor review '
                              f'approved it; {len(armed_ids)} armed edit(s) '
                              f'are reserved and {validator} will validate '
                              f'the applied result. It now waits on the '
                              f'player approval link.')}


def resolve_window_escalation(esc_id, approved):
    """Side effect for the escalation resolve endpoint: the player's link
    decision lands on the window row. Only a PENDING window changes."""
    serve = _s()
    with serve._db() as conn:
        _ensure_windows_table(conn)
        row = conn.execute(
            'SELECT id, status FROM dormancy_windows '
            'WHERE escalation_id = ?', (esc_id,)).fetchone()
    if not row:
        return False
    window_id, status = row[0], row[1]
    if status != 'pending':
        return False
    if approved:
        _update_window(window_id, status='approved', note='player approved')
    else:
        # denied change requests release their reserved edits back to the
        # idle pool -- they were never applied and stay armed
        _unreserve_window_edits(window_id)
        _update_window(window_id, status='refused',
                       note='player denied')
    serve.log_action(window_id, 'dormancy_window_resolved',
                     {'by': 'player', 'approved': approved},
                     authorized=True)
    return True


def _window_fire_blockers(window):
    """Re-check a due window against live state before it fires.

    The request-time scan is not enough: standing tasks, other windows,
    market/event blackouts, the roster, and the reserved edit set can all
    change after approval. Any change defers the window instead of applying
    bytes into a state that was never reviewed.
    """
    if not window:
        return ['unknown window']
    blockers = []
    try:
        conflicts = _window_conflicts(float(window['start_ts']),
                                      float(window['end_ts']),
                                      exclude_window_id=window['id'])
    except (KeyError, TypeError, ValueError) as exc:
        return [f'conflict scan failed: {exc}']
    for c in conflicts:
        blockers.append(f"{c.get('kind')} {c.get('name')}")
    try:
        blackouts = json.loads(window.get('blackout_windows') or '[]')
    except (TypeError, ValueError):
        blackouts = []
    for label in _blackout_conflicts(float(window['start_ts']),
                                     float(window['end_ts']), blackouts):
        blockers.append(f'blackout {label}')
    # Bare windows in the ledger predate the detailed change-request
    # contract. They still get the schedule and blackout recheck; the
    # validator, manifest, and window-only checks apply once a window carries
    # request metadata (every real request does).
    has_request_meta = bool(window.get('validator')
                            or window.get('file_manifest')
                            or window.get('risk_level')
                            or window.get('service_mode'))
    if not has_request_meta:
        return blockers
    state = _s().get_state_from_db() or {}
    roster = state.get('agents') or {}
    names = (set(roster.keys()) if isinstance(roster, dict)
             else {a.get('id') for a in roster if isinstance(a, dict)})
    validator = (window.get('validator') or '').strip()
    if not validator:
        blockers.append('no validator on record')
    elif names and validator not in names:
        blockers.append(f'validator {validator} is no longer on the roster')
    elif governance.admin_change_request_enabled(state):
        if not _s()._is_director(state, validator):
            blockers.append(f'validator {validator} is no longer a director')
        if validator == _s()._admin_agent_id(state):
            blockers.append('validator is the admin, not an independent director')
    with _s()._db() as conn:
        _ensure_table(conn)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT id, path, apply_policy FROM self_edit_log "
            "WHERE reserve_window = ? AND status = 'armed' "
            'ORDER BY created', (window['id'],)).fetchall()]
    if not rows:
        blockers.append('no reserved edits are still armed')
    try:
        manifest = json.loads(window.get('file_manifest') or '[]')
    except (TypeError, ValueError):
        manifest = []
    if rows and manifest and sorted(set(manifest)) != sorted({r['path'] for r in rows}):
        blockers.append('file manifest no longer matches the reserved edits')
    service_mode = window.get('service_mode') or 'idle_ok'
    risk_level = window.get('risk_level') or 'low'
    if _is_high_risk(risk_level, service_mode):
        unsafe = [r['path'] for r in rows
                  if (r.get('apply_policy') or 'idle_or_window') != 'window_only']
        if unsafe:
            blockers.append('window-only edits required for: '
                            + ', '.join(sorted(set(unsafe))))
        if window.get('pre_apply_result') not in (None, 'pass'):
            blockers.append('pre-apply gate did not pass')
    return blockers


def _defer_window(window_id, blockers):
    """Release a window that cannot safely fire, notify the owner, and keep
    the reservation from stranding the edits."""
    serve = _s()
    note = '; '.join(blockers)[:800]
    _update_window(window_id, status='deferred_conflict',
                   note='deferred at fire time: ' + note,
                   deferred_reason=note)
    _unreserve_window_edits(window_id)
    row = _window_row(window_id) or {}
    if row.get('agent'):
        _notify_agent(row['agent'], (
            f'Change request {window_id} was DEFERRED at fire time and did '
            f'not apply. Reason: {note}. Your edits remain armed and '
            f'unreserved. Re-check the schedule and file a fresh request.'),
            about=f'selfedit:{window_id}')
    serve.log_action(window_id, 'dormancy_window_deferred',
                     {'reason': note}, authorized=True)
    _notify_player_telegram('[AI Think Tank] Change request deferred',
                            f'Change request {window_id} did not fire. {note}')
    return window_id


def window_tick():
    """Serve-loop tick for dormancy windows: sweep missed slots, fire due
    ones by forcing the tank dormant, and keep it dormant through the end
    of the slot. Returns the list of window ids newly fired."""
    serve = _s()
    now = time.time()
    with serve._db() as conn:
        _ensure_windows_table(conn)
        missed = conn.execute(
            "SELECT id FROM dormancy_windows WHERE status = 'approved' "
            'AND end_ts < ?', (now,)).fetchall()
    for (row_id,) in missed:
        _update_window(row_id, status='missed',
                       note='window passed without firing')
        _unreserve_window_edits(row_id)
        serve.log_action(row_id, 'dormancy_window_missed', {},
                         authorized=False)
    with serve._db() as conn:
        _ensure_windows_table(conn)
        due = conn.execute(
            "SELECT id FROM dormancy_windows WHERE status = 'approved' "
            'AND start_ts <= ? ORDER BY start_ts', (now,)).fetchall()
        holding = conn.execute(
            "SELECT id, end_ts FROM dormancy_windows WHERE status = 'fired' "
            'ORDER BY start_ts').fetchall()
    # END OF WINDOW: a fired window whose slot has passed either finished
    # healthy (done) or still has failing edits -- per the change-window
    # contract, rolling back automatically. The responsible agents are
    # mailed to fix off-window and schedule again.
    for row_id, end_ts in holding:
        if end_ts > now:
            continue
        failing = _window_participants(row_id, 'failed')
        if failing:
            _rollback_rows(failing,
                           f'change window {row_id} ended with failing tests',
                           row_id)
            _update_window(row_id, status='rolled_back',
                           note='ended with unresolved test failures; '
                                'candidates saved and edits reverted',
                           push_status='failed',
                           push_category='unsuccessful',
                           push_note='repo tests still failing at the end '
                                     'of the slot')
            serve.log_action(row_id, 'dormancy_window_rolled_back',
                             {'edits': failing}, authorized=True)
            _telegram_change_request(row_id, 'unsuccessful',
                                     'repo tests still failing at the end '
                                     'of the slot')
        else:
            push_status = _window_field(row_id, 'push_status')
            if push_status == 'pushed':
                _update_window(row_id, status='done')
                serve.log_action(row_id, 'dormancy_window_done', {},
                                 authorized=True)
            elif push_status == 'awaiting_validator':
                applied_rows = _window_participants(row_id, 'applied')
                _rollback_rows(applied_rows,
                               f'change window {row_id} ended before the '
                               'validator approved the push', row_id)
                _update_window(row_id, status='rolled_back',
                               note='ended awaiting validation; edits '
                                    'reverted, candidates saved',
                               push_status='failed',
                               push_category='unsuccessful',
                               push_note='window ended before the validator '
                                         'approved the push')
                serve.log_action(row_id, 'dormancy_window_rolled_back',
                                 {'edits': applied_rows,
                                  'reason': 'awaiting validator at slot end'},
                                 authorized=True)
                _telegram_change_request(
                    row_id, 'unsuccessful',
                    'window ended before the validator approved the push')
            else:
                _update_window(row_id, status='unsuccessful',
                               push_status='failed',
                               push_category='unsuccessful',
                               push_note='window ended without a validated, '
                                         'pushed result')
                serve.log_action(row_id, 'dormancy_window_unsuccessful', {},
                                 authorized=True)
                _telegram_change_request(row_id, 'unsuccessful',
                                         'window ended without a validated, '
                                         'pushed result')
        _unreserve_window_edits(row_id)
    fired = []
    for (row_id,) in due:
        window = _window_row(row_id)
        blockers = _window_fire_blockers(window)
        if blockers:
            _defer_window(row_id, blockers)
            continue
        _update_window(row_id, status='fired', fired_at=now)
        serve.log_action(row_id, 'dormancy_window_fired',
                         {'dormant': True}, authorized=True)
        fired.append(row_id)
    if fired or any(end_ts > now for _w, end_ts in holding):
        if not serve._dormant():
            serve._set_dormant(True)
            serve.log_action('dormancy_window_loop', 'dormancy_forced',
                             {'windows': fired + [w for w, _e in holding]},
                             authorized=True)
            print('[selfedit] dormancy window active -- tank set dormant', flush=True)
    return fired


def cancel_window(window_id, by='player'):
    """Operator-side undo before the slot fires."""
    serve = _s()
    with serve._db() as conn:
        _ensure_windows_table(conn)
        row = conn.execute(
            'SELECT status FROM dormancy_windows WHERE id = ?',
            (window_id,)).fetchone()
    if not row:
        return False, {'error': 'unknown window id'}
    status = row[0]
    if status not in ('pending', 'approved'):
        return False, {'error': f'window is {status!r}, nothing to cancel'}
    _update_window(window_id, status='cancelled',
                   note=f'cancelled by {by}')
    _unreserve_window_edits(window_id)
    serve.log_action(window_id, 'dormancy_window_cancelled',
                     {'by': by}, authorized=True)
    return True, {'ok': True, 'status': 'cancelled'}


def veto_window(window_id, by='player', reason=''):
    """Final operator veto for a change request.

    A veto before the slot fires cancels the window and releases the
    reservations. A veto after the slot fired but before the push rolls
    back every applied edit and prevents the push. A pushed window cannot
    be vetoed; it is already on main and needs a fresh change request.
    """
    serve = _s()
    reason = (reason or '').strip()[:MAX_PUSH_NOTE_LEN]
    if not reason:
        return False, {'error': 'a reason is required for a veto'}
    row = _window_row(window_id)
    if not row:
        return False, {'error': 'unknown window id'}
    status = row.get('status')
    if status == 'vetoed':
        return False, {'error': 'this change request is already vetoed'}
    if status in ('pending', 'approved'):
        _update_window(window_id, status='vetoed', note=f'vetoed by {by}: {reason}',
                       vetoed_by=by, vetoed_at=time.time(), veto_reason=reason)
        _unreserve_window_edits(window_id)
    elif status == 'deferred_conflict':
        _update_window(window_id, status='vetoed', note=f'vetoed by {by}: {reason}',
                       vetoed_by=by, vetoed_at=time.time(), veto_reason=reason)
    elif status == 'fired':
        if row.get('push_status') == 'pushed':
            return False, {'error': 'the change request was already pushed; '
                                    'veto cannot undo a push'}
        applied = _window_participants(window_id, 'applied')
        failed = _window_participants(window_id, 'failed')
        if applied:
            _rollback_rows(applied, f'change request vetoed by {by}: {reason}',
                           window_id)
        _update_window(window_id, status='vetoed',
                       note=f'vetoed by {by}: {reason}',
                       push_status='failed',
                       push_category='unsuccessful',
                       push_note=f'vetoed by {by}: {reason}',
                       vetoed_by=by, vetoed_at=time.time(),
                       veto_reason=reason)
        _unreserve_window_edits(window_id)
        if failed:
            _update_window(window_id, note=f'vetoed by {by}: {reason}; '
                                           f'{len(failed)} failed edit(s) kept '
                                           'for review')
    else:
        return False, {'error': f'window is {status!r}, nothing to veto'}
    if row.get('agent'):
        _notify_agent(row['agent'], (
            f'Change request {window_id} was VETOED by {by}. Reason: {reason}. '
            f'Nothing was pushed. Your edits remain armed unless they were '
            f'already applied inside the window.'),
            about=f'selfedit:{window_id}')
    validator = row.get('validator')
    if validator:
        _notify_agent(validator, (
            f'Change request {window_id} was VETOED by {by}. Reason: {reason}. '
            f'Do not approve or push it.'),
            about=f'selfedit:{window_id}')
    serve.log_action(window_id, 'dormancy_window_vetoed',
                     {'by': by, 'reason': reason, 'previousStatus': status},
                     authorized=True)
    _notify_player_telegram('[AI Think Tank] Change request vetoed',
                            f'Change request {window_id} was vetoed by {by}. '
                            f'Reason: {reason}')
    return True, {'ok': True, 'status': 'vetoed', 'windowId': window_id,
                  'vetoedBy': by, 'reason': reason}


def windows_snapshot(limit=25):
    """Bounded, newest-first view of the dormancy-window ledger."""
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT id, created, agent, date, start, duration_min, reason, '
            'status, note, escalation_id, fired_at, validator, '
            'validator_status, validator_note, push_status, push_category, '
            'push_note, push_sha, pushed_at, admin_status, admin_note, '
            'admin_agent, supervisor, title, description, change_type, '
            'risk_level, service_mode, affected_services, file_manifest, '
            'rollback_plan, rollback_owner, success_criteria, abort_criteria, '
            'post_apply_checks, safe_state_plan, restore_plan, on_call, '
            'acknowledgements, blackout_windows, pre_apply_result, '
            'pre_apply_tail, fire_recheck, deferred_reason, vetoed_by, '
            'vetoed_at, veto_reason '
            'FROM dormancy_windows ORDER BY created DESC LIMIT ?',
            (int(limit),)).fetchall()
    out = []
    for r in rows:
        item = {'id': r['id'], 'created': r['created'], 'agent': r['agent'],
                'date': r['date'], 'start': r['start'],
                'durationMin': r['duration_min'], 'reason': r['reason'],
                'status': r['status'], 'note': r['note'],
                'escalationId': r['escalation_id'],
                'firedAt': r['fired_at'], 'validator': r['validator'],
                'validatorStatus': r['validator_status'],
                'validatorNote': r['validator_note'],
                'pushStatus': r['push_status'],
                'pushCategory': r['push_category'],
                'pushNote': r['push_note'], 'pushSha': r['push_sha'],
                'pushedAt': r['pushed_at'],
                'adminStatus': r['admin_status'],
                'adminNote': r['admin_note'],
                'adminAgent': r['admin_agent'],
                'supervisor': r['supervisor']}
        json_lists = {'affectedServices', 'fileManifest',
                      'acknowledgements', 'blackoutWindows'}
        for key, col in (
                ('title', 'title'), ('description', 'description'),
                ('changeType', 'change_type'), ('riskLevel', 'risk_level'),
                ('serviceMode', 'service_mode'),
                ('affectedServices', 'affected_services'),
                ('fileManifest', 'file_manifest'),
                ('rollbackPlan', 'rollback_plan'),
                ('rollbackOwner', 'rollback_owner'),
                ('successCriteria', 'success_criteria'),
                ('abortCriteria', 'abort_criteria'),
                ('postApplyChecks', 'post_apply_checks'),
                ('safeStatePlan', 'safe_state_plan'),
                ('restorePlan', 'restore_plan'), ('onCall', 'on_call'),
                ('acknowledgements', 'acknowledgements'),
                ('blackoutWindows', 'blackout_windows'),
                ('preApplyResult', 'pre_apply_result'),
                ('preApplyTail', 'pre_apply_tail'),
                ('fireRecheck', 'fire_recheck'),
                ('deferredReason', 'deferred_reason'),
                ('vetoedBy', 'vetoed_by'), ('vetoedAt', 'vetoed_at'),
                ('vetoReason', 'veto_reason')):
            value = r[col]
            if key in json_lists:
                try:
                    value = json.loads(value or '[]')
                except (TypeError, ValueError):
                    value = []
            item[key] = value
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Two-agent change requests -> push to origin main (operator decision,
# 2026-10-18). The implementer requests the window and names a validator (a
# DIFFERENT agent). The window reserves the implementer's armed edits, applies
# exactly those inside the slot, and runs the repo gate on the live tree. Gate
# green holds the window for the validator's explicit approval; only an
# approved result is committed and pushed. Every final outcome is categorized
# (successful / successful_with_issues / unsuccessful), logged as repo_push,
# and pushed to the player's Telegram. Failures roll back like a failed gate:
# local main never sits ahead of origin with unpushed validated bytes.
# ---------------------------------------------------------------------------


def _git_run(args):
    """One git command against the repo root. Returns (rc, stdout, stderr);
    every failure is a plain return code -- the caller fails closed."""
    try:
        done = subprocess.run(['git', '-C', _repo_root(), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=GIT_STEP_TIMEOUT_S)
        return (done.returncode,
                (done.stdout or b'').decode('utf-8', errors='replace'),
                (done.stderr or b'').decode('utf-8', errors='replace'))
    except subprocess.TimeoutExpired:
        return 124, '', f'git {args[0]} timed out after {GIT_STEP_TIMEOUT_S}s'
    except Exception as exc:
        return 1, '', f'git {args[0]} could not start: {exc}'


def _notify_player_telegram(subject, body):
    """Best-effort player Telegram push for change-request outcomes; never
    raises (an unreachable Telegram must not break a finished push)."""
    try:
        from notify import send_player_telegram_sync
        return send_player_telegram_sync(subject, body)
    except Exception as exc:
        try:
            _s().log_action('selfedit', 'selfedit_telegram_failed',
                            {'error': str(exc)[:200]}, authorized=False)
        except Exception:
            pass
        return False


def _window_row(window_id):
    with _s()._db() as conn:
        conn.row_factory = sqlite3.Row
        _ensure_windows_table(conn)
        row = conn.execute(
            'SELECT * FROM dormancy_windows WHERE id = ?',
            (window_id,)).fetchone()
    return dict(row) if row else None


def _window_field(window_id, col):
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        row = conn.execute(
            f'SELECT {col} FROM dormancy_windows WHERE id = ?',
            (window_id,)).fetchone()
    return row[0] if row else None


def _unreserve_window_edits(window_id):
    """Release every edit still reserved for a window back to the idle pool
    (reservation only pays off if the window actually fires)."""
    with _s()._db() as conn:
        _ensure_table(conn)
        conn.execute(
            'UPDATE self_edit_log SET reserve_window = NULL '
            'WHERE reserve_window = ?', (window_id,))


def _window_applied_paths(window_id):
    with _s()._db() as conn:
        _ensure_table(conn)
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT path FROM self_edit_log WHERE window_id = ? "
            "AND status = 'applied'", (window_id,)).fetchall()]


def _window_edit_summaries(window_id):
    with _s()._db() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT id, agent, path, summary FROM self_edit_log "
            "WHERE window_id = ? AND status = 'applied' ORDER BY created",
            (window_id,)).fetchall()
    return [{'id': r[0], 'agent': r[1], 'path': r[2], 'summary': r[3]}
            for r in rows]


def _window_unlanded(window_id):
    """Reserved edits of this window that did NOT end applied (conflicts,
    apply failures). Each one is a change-request issue the validator must
    see -- and the note requirement exists for exactly this."""
    with _s()._db() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT id, status FROM self_edit_log WHERE reserve_window = ? "
            "AND status != 'applied'", (window_id,)).fetchall()
    return [{'id': r[0], 'status': r[1]} for r in rows]


def validation_window():
    """The fired window currently holding for its validator's push decision
    (still inside its slot). Drives serve's wake exemption so validator
    traffic reaches the server without waking the tank mid-window."""
    import math
    now = time.time()
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        row = conn.execute(
            "SELECT id FROM dormancy_windows WHERE status = 'fired' "
            "AND push_status = 'awaiting_validator' AND end_ts >= ? "
            'ORDER BY start_ts DESC LIMIT 1', (math.floor(now),)).fetchone()
    return row[0] if row else None


def window_validator(window_id):
    return _window_field(window_id, 'validator')


def _telegram_change_request_created(window_id):
    """Player Telegram message when a change request is created, with the
    details needed to judge it and a one-click veto link. The link is
    token-gated and needs no login. Telegram caps a message at 4096 chars."""
    row = _window_row(window_id) or {}
    token = row.get('veto_token') or ''
    base = _s().ESCALATION_BASE_URL.rstrip('/')
    veto_url = (f'{base}/api/selfedit/window/veto-link'
                f'?windowId={window_id}&token={token}')
    try:
        files = json.loads(row.get('file_manifest') or '[]')
    except (TypeError, ValueError):
        files = []
    title = row.get('title') or row.get('reason') or '(untitled change)'
    body = (
        f'Change request {window_id}: {title}\n'
        f'Status: {row.get("status") or "?"}\n'
        f'Implementer: {row.get("agent") or "?"}\n'
        f'Validator: {row.get("validator") or "?"}\n'
        f'Window: {row.get("date") or "?"} {row.get("start") or "?"} '
        f'({row.get("duration_min") or "?"} min)\n'
        f'Risk: {row.get("risk_level") or "low"}; '
        f'mode: {row.get("service_mode") or "idle_ok"}\n'
        f'Files: {", ".join(str(f) for f in files) or "(none)"}\n'
        f'Reason: {(row.get("reason") or "")[:500]}\n\n'
        f'Veto this change request: {veto_url}'
    )
    return _notify_player_telegram('[AI Think Tank] Change request created',
                                   body[:4096])


def veto_window_by_link(window_id, token, reason='Vetoed from Telegram link'):
    """Veto a change request from the tokenized Telegram link. The token is
    the authentication; no player session is needed. Fails closed on a
    missing/mismatched token and delegates every status rule to veto_window."""
    row = _window_row(window_id)
    if not row or not token:
        return False, {'error': 'invalid or expired veto link'}
    if not secrets.compare_digest(str(row.get('veto_token') or ''), str(token)):
        return False, {'error': 'invalid or expired veto link'}
    return veto_window(window_id, 'player',
                       (reason or 'Vetoed from Telegram link')[:300])


def _telegram_change_request(window_id, category, note, sha=None, n_edits=None):
    """One player Telegram message per final change-request outcome."""
    row = _window_row(window_id) or {}
    validator = row.get('validator') or '?'
    if category == 'successful':
        head = (f'Change request {window_id}: SUCCESSFUL -- {n_edits or "?"} '
                f'edit(s) passed the repo gate and validator {validator}, '
                'pushed to main' + (f' ({sha})' if sha else ''))
    elif category == 'successful_with_issues':
        head = (f'Change request {window_id}: SUCCESSFUL WITH ISSUES -- '
                f'pushed to main' + (f' ({sha})' if sha else '')
                + f' (validated by {validator})')
    else:
        head = (f'Change request {window_id}: UNSUCCESSFUL -- nothing was '
                'pushed to main')
    body = head
    if note:
        body += f'\n\nNote: {note}'
    return _notify_player_telegram('[AI Think Tank] Change request', body)


def _hold_for_validator(window_id, remediation_used, tail):
    """Gate-green handoff: the change request waits for the validator's
    explicit approval before anything is pushed. Idempotent per window."""
    serve = _s()
    row = _window_row(window_id)
    if not row or row['status'] != 'fired' or row.get('push_status'):
        return
    _update_window(window_id, push_status='awaiting_validator',
                   remediation_used=1 if remediation_used else 0,
                   gate_tail=(tail or '')[-POST_APPLY_TAIL_LEN:])
    edits = _window_edit_summaries(window_id)
    serve.log_action(window_id, 'selfedit_validation_requested',
                     {'edits': [e['id'] for e in edits],
                      'remediationUsed': bool(remediation_used)},
                     authorized=True)
    validator = row.get('validator')
    if not validator:
        return
    lines = [
        f'Change window {window_id} applied {len(edits)} edit(s) and the repo '
        'test gate PASSED on the live tree. You are the validator for this '
        'change request: review the applied result, then approve or deny the '
        'push with the selfedit_validate tool.',
        'Edits in this change request:',
    ]
    for e in edits:
        lines.append(f"- {e['path']} ({e['agent']}): {(e['summary'] or '')[:200]}")
    if remediation_used:
        lines.append('NOTE: in-window remediation fixes were needed before '
                     'the gate passed -- approving WITHOUT a note is refused; '
                     'your note becomes the change-request record.')
    lines.append('Approving pushes straight to main (a note is required when '
                 'there are issues). Denying rolls the window back like a '
                 'failed gate -- nothing is pushed.')
    deadline = time.strftime('%H:%M', time.localtime(row['end_ts']))
    lines.append(f'Window deadline: {deadline} tank time. If the slot ends '
                 'before you decide, the window rolls back automatically.')
    lines.append('Gate tail:\n' + (tail or '')[-POST_APPLY_TAIL_LEN:])
    _notify_agent(validator, '\n'.join(lines), about=f'selfedit:{window_id}')


def _window_diff(window_id):
    """Bounded pre-edit/current diff for the validator turn. The applied rows
    are exactly the change request's bytes, so a unified diff is the smallest
    honest review surface; a missing backup degrades to a named limitation."""
    where = _world()
    with _s()._db() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            'SELECT id, agent, path FROM self_edit_log WHERE window_id = ? '
            "AND status = 'applied' ORDER BY created", (window_id,)).fetchall()
    parts = []
    for row_id, agent, display in rows:
        target = _target_from_display(display)
        backup = os.path.join(where, '.selfedit_backups', row_id)
        try:
            with open(backup, 'rb') as fh:
                before = fh.read().decode('utf-8', errors='replace')
            with open(target, 'rb') as fh:
                after = fh.read().decode('utf-8', errors='replace')
        except OSError as exc:
            parts.append(f'{display} ({agent}): diff unavailable: {exc}')
            continue
        diff = ''.join(difflib.unified_diff(
            before.splitlines(True), after.splitlines(True),
            fromfile=f'{display} (before)', tofile=f'{display} (after)',
            n=3))
        parts.append(f'{display} ({agent}):\n'
                     + (diff or '(no line diff; binary or line-ending change)'))
    text = '\n\n'.join(parts) or '(no applied edits to diff)'
    if len(text) > VALIDATOR_TURN_MAX_DIFF_CHARS:
        text = text[:VALIDATOR_TURN_MAX_DIFF_CHARS] + '\n[diff truncated]'
    return text


def _validator_brief(window_id):
    row = _window_row(window_id) or {}
    return {
        'window': row,
        'edits': _window_edit_summaries(window_id),
        'unlanded': _window_unlanded(window_id),
        'diff': _window_diff(window_id),
        'tail': (row.get('gate_tail') or '')[-VALIDATOR_TURN_MAX_TAIL_CHARS:],
    }


SELFEDIT_VALIDATE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'selfedit_validate',
        'description': ('Record the validator decision for one change request. '
                        'approve=true pushes the applied bytes to origin main; '
                        'approve=false rolls the window back with nothing '
                        'pushed. A non-empty note is required when remediation '
                        'was used, any reserved edit failed to apply, or you '
                        'flag issues.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'windowId': {'type': 'string',
                             'description': 'The change window id under review.'},
                'approved': {'type': 'boolean',
                             'description': 'true to push, false to roll back.'},
                'note': {'type': 'string',
                         'description': 'Why, and what issues you found.'},
                'issues': {'type': 'boolean',
                           'description': 'true when you have concerns beyond '
                                          'the automatic issue checks.'},
            },
            'required': ['windowId', 'approved'],
        },
    },
}


def _default_validator_decider(window_id, validator, brief):
    """One model turn as the named validator. Forced to the decision tool so a
    prose answer cannot be mistaken for consent. Evidence is wrapped as
    external data; the validator identity is the window's own."""
    serve = _s()
    row = brief['window']
    state = serve.get_state_from_db() or {}
    agents = state.get('agents') or {}
    agent = agents.get(validator) if isinstance(agents, dict) else {}
    if not isinstance(agent, dict):
        agent = {}
    name = agent.get('name') or validator
    role = agent.get('role') or 'agent'
    diff_block, _n, _t, diff_inst = serve.wrap_external_content(
        brief['diff'], 'the applied change diff')
    tail_block, _n2, _t2, tail_inst = serve.wrap_external_content(
        brief['tail'] or '(no gate output recorded)', 'the repo test gate output')
    edit_lines = [f"- {e['path']} by {e['agent']}: {(e['summary'] or '')[:240]}"
                  for e in brief['edits']]
    unlanded = [f"- {u['id']}: {u['status']}" for u in brief['unlanded']]
    system = (
        f'You are {name}, a {role} in an AI think tank. You are the named '
        'validator for one change request. The tank is dormant while you '
        'decide, and your decision is the only gate before the applied bytes '
        'are pushed to the shared main branch. Review the diff, the applied '
        'edit summaries, and the repo test gate output. Approve only when the '
        'change matches the stated reason, is safe to ship, and the gate '
        'output actually supports it. Deny when the change is unrelated, '
        'incomplete, unsafe, or the evidence is insufficient. If in-window '
        'remediation was used, any reserved edit did not land, or you have '
        'any concern, set issues=true and write a non-empty note. Return '
        'exactly one selfedit_validate tool call. Do not return prose.'
    )
    user = ('## Change request\n'
            f"Window: {window_id}\n"
            f"Implementer: {row.get('agent') or '?'}\n"
            f"Reason: {row.get('reason') or '(none)'}\n"
            f"Remediation used: {'yes' if row.get('remediation_used') else 'no'}\n\n"
            '## Applied edits\n' + ('\n'.join(edit_lines) or '(none)') + '\n\n'
            '## Reserved edits that did not land\n'
            + ('\n'.join(unlanded) or '(none)') + '\n\n'
            '## Diff instruction\n' + diff_inst + '\n' + diff_block + '\n\n'
            '## Gate instruction\n' + tail_inst + '\n' + tail_block)
    model = serve._resolve_model_tier(
        f'Review applied self-edits for change request {window_id}',
        task_type='review')
    data = serve._post_openrouter_raw(
        model, [{'role': 'system', 'content': system},
                {'role': 'user', 'content': user}],
        tools=[SELFEDIT_VALIDATE_TOOL], max_tokens=800,
        tool_choice={'type': 'function',
                     'function': {'name': 'selfedit_validate'}})
    usage = (data or {}).get('usage') or {}
    cost = usage.get('cost') or 0
    if cost:
        serve._accrue_spend('selfedit_validation', cost, agent_id=validator)
    message = (((data or {}).get('choices') or [{}])[0].get('message') or {})
    calls = message.get('tool_calls') or []
    if not calls:
        raise RuntimeError('validator returned no selfedit_validate tool call')
    raw = ((calls[0].get('function') or {}).get('arguments')) or '{}'
    try:
        args = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'validator tool arguments were not JSON: {exc}')
    if (args.get('windowId') or '').strip() != window_id:
        raise RuntimeError('validator tool call named a different window')
    if 'approved' not in args:
        raise RuntimeError('validator tool call omitted approved')
    return {'approved': bool(args.get('approved')),
            'note': (args.get('note') or '').strip(),
            'issues': bool(args.get('issues')),
            'model': model, 'cost': cost}


def pending_validator_windows(now=None, limit=5):
    """Fired windows that still need a validator turn. `validator_status`
    gates retries: a failed transport wait VALIDATOR_TURN_RETRY_S, and a turn
    that has run out of attempts stops consuming model budget."""
    now = time.time() if now is None else now
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        rows = conn.execute(
            "SELECT id FROM dormancy_windows WHERE status = 'fired' "
            "AND push_status = 'awaiting_validator' AND end_ts >= ? "
            "AND COALESCE(validator_attempts, 0) < ? "
            "AND (validator_status IS NULL "
            "     OR ((validator_status = 'pending' "
            "          OR validator_status = 'deciding') "
            "         AND COALESCE(validator_attempt_at, 0) <= ?)) "
            'ORDER BY start_ts LIMIT ?',
            (now, VALIDATOR_TURN_MAX_ATTEMPTS,
             now - VALIDATOR_TURN_RETRY_S, limit)).fetchall()
    return [r[0] for r in rows]


def run_validator_review(window_id, decider=None, now=None):
    """Run ONE narrow validator decision turn while the tank is dormant.
    Returns (ok, payload). The decision still goes through validate_window(),
    so only the window's named validator can record it and every push/rollback
    path is unchanged."""
    serve = _s()
    now = time.time() if now is None else now
    row = _window_row(window_id)
    if not row:
        return False, {'error': 'unknown window id'}
    if row.get('status') != 'fired' or row.get('push_status') != 'awaiting_validator':
        return False, {'error': 'window is not awaiting validation'}
    if row['end_ts'] < now:
        return False, {'error': 'window slot already ended'}
    validator = (row.get('validator') or '').strip()
    if not validator:
        return False, {'error': 'window has no validator'}
    attempts = int(row.get('validator_attempts') or 0)
    if attempts >= VALIDATOR_TURN_MAX_ATTEMPTS:
        _update_window(window_id, validator_status='failed')
        return False, {'error': 'validator turn already used all attempts'}
    with serve._db() as conn:
        _ensure_windows_table(conn)
        cur = conn.execute(
            "UPDATE dormancy_windows SET validator_status = 'deciding', "
            'validator_attempt_at = ?, '
            'validator_attempts = COALESCE(validator_attempts, 0) + 1 '
            "WHERE id = ? AND status = 'fired' "
            "AND push_status = 'awaiting_validator' AND end_ts >= ? "
            "AND COALESCE(validator_attempts, 0) < ? "
            "AND (validator_status IS NULL "
            "     OR ((validator_status = 'pending' "
            "          OR validator_status = 'deciding') "
            "         AND COALESCE(validator_attempt_at, 0) <= ?))",
            (now, window_id, now, VALIDATOR_TURN_MAX_ATTEMPTS,
             now - VALIDATOR_TURN_RETRY_S))
    if cur.rowcount != 1:
        return False, {'error': 'another validator turn owns this window right now'}
    attempts += 1
    serve.log_action(window_id, 'selfedit_validator_turn_started',
                     {'validator': validator, 'attempt': attempts},
                     authorized=True)
    try:
        brief = _validator_brief(window_id)
        decider = decider or _default_validator_decider
        verdict = decider(window_id, validator, brief)
    except Exception as exc:  # transport/shape failure is not consent
        status = ('failed' if attempts >= VALIDATOR_TURN_MAX_ATTEMPTS
                  else 'pending')
        _update_window(window_id, validator_status=status)
        serve.log_action(window_id, 'selfedit_validator_turn_failed',
                         {'validator': validator, 'attempt': attempts,
                          'error': str(exc)[:300]}, authorized=False)
        return False, {'error': f'validator turn failed: {exc}'}
    approved = bool((verdict or {}).get('approved'))
    note = ((verdict or {}).get('note') or '').strip()
    issues = bool((verdict or {}).get('issues'))
    ok, payload = validate_window(validator, window_id, approved, note, issues)
    if ok:
        _update_window(window_id,
                       validator_status='approved' if approved else 'denied',
                       validated_at=time.time())
        serve.log_action(window_id, 'selfedit_validator_decided',
                         {'validator': validator, 'approved': approved,
                          'issues': issues,
                          'model': (verdict or {}).get('model'),
                          'attempt': attempts}, authorized=True)
    else:
        status = ('failed' if attempts >= VALIDATOR_TURN_MAX_ATTEMPTS
                  else 'pending')
        _update_window(window_id, validator_status=status)
        serve.log_action(window_id, 'selfedit_validator_decision_refused',
                         {'validator': validator, 'attempt': attempts,
                          'error': str((payload or {}).get('error'))[:300]},
                         authorized=False)
    return ok, payload


def _fail_window_push(window_id, reason, by=None):
    """Roll the window back like a failed gate and record an unsuccessful
    outcome: candidates saved under .selfedit_failed/<window>/, tree restored,
    Telegram + action log tell the player."""
    serve = _s()
    applied = _window_participants(window_id, 'applied')
    _rollback_rows(applied, reason, window_id)
    _update_window(window_id, push_status='failed',
                   push_category='unsuccessful',
                   push_note=reason[:MAX_PUSH_NOTE_LEN])
    serve.log_action(window_id, 'repo_push',
                     {'category': 'unsuccessful', 'reason': reason[:300],
                      'by': by or 'system', 'edits': applied},
                     authorized=True)
    _telegram_change_request(window_id, 'unsuccessful', reason)
    return True, {'ok': True, 'status': 'unsuccessful',
                  'message': f'{reason} -- the window rolled back; nothing '
                             'was pushed'}


def _agent_display_name(agent_id):
    """The human-facing name for an agent id, read from the live roster. Falls
    back to the id when the roster cannot be read, so a push never loses the
    attribution line (the id is still a correct, stable reference)."""
    if not agent_id:
        return '?'
    if agent_id == 'player':
        return 'player'
    try:
        state = _s().get_state_from_db() or {}
    except Exception:
        return str(agent_id)
    for row in state.get('agentRoster') or []:
        if isinstance(row, dict) and row.get('id') == agent_id:
            return row.get('name') or agent_id
    return str(agent_id)


def _commit_message(window_id, row, edits):
    """Commit text for a validated change request. Attribution is part of the
    contract: the approving supervisor, the chief of staff, the implementer,
    and the independent validator are all named, so `git log` answers who
    approved and who checked the work without reading the tank's database."""
    implementer = _agent_display_name(row.get('agent'))
    validator = _agent_display_name(row.get('validator'))
    supervisor = _agent_display_name(row.get('supervisor'))
    chief = _agent_display_name(row.get('admin_agent'))
    lines = [f'selfedit: change window {window_id}', '',
             (row.get('reason') or '').strip() or '(no reason recorded)', '',
             f'Implemented by {implementer}; validated by {validator}.', '',
             'Change request:',
             f'Approving supervisor: {supervisor}',
             f'Chief of staff: {chief}',
             f'Implementer: {implementer}',
             f'Validator: {validator}', '']
    for e in edits[:20]:
        lines.append(f"- {e['path']}: {(e['summary'] or '')[:160]}")
    return '\n'.join(lines)


def _push_window(window_id, category, note):
    """Commit exactly this window's applied files and push origin main.
    The gate already ran on these exact bytes while the tank was dormant;
    the status check guards against unrelated tracked changes sneaking in."""
    serve = _s()
    row = _window_row(window_id)
    paths = _window_applied_paths(window_id)
    if not row or not paths:
        return _fail_window_push(window_id,
                                 'no applied edits to push (unexpected state)')
    rc, out, err = _git_run(['status', '--porcelain'])
    if rc != 0:
        return _fail_window_push(window_id,
                                 f'git status failed: {(err or out)[-300:]}')
    expected = set(paths)
    for line in out.splitlines():
        if not line.strip():
            continue
        st, path = line[:2], line[3:].strip()
        if st.strip() in ('??', '!!'):
            continue  # untracked/ignored noise is not part of the push
        if ' -> ' in path:
            path = path.split(' -> ', 1)[1]
        if path not in expected:
            return _fail_window_push(
                window_id,
                f'unexpected tracked change in the tree ({path}) -- only '
                "this change request's files may land")
    edits = _window_edit_summaries(window_id)
    message = _commit_message(window_id, row, edits)
    for args in (['add', *paths], ['commit', '-m', message],
                 ['push', 'origin', 'main']):
        rc, out, err = _git_run(args)
        if rc != 0:
            return _fail_window_push(
                window_id, f'git {args[0]} failed: {(err or out)[-300:]}')
    rc, sha_out, _err = _git_run(['rev-parse', 'HEAD'])
    sha = sha_out.strip()[:12] if rc == 0 and sha_out.strip() else ''
    _update_window(window_id, push_status='pushed', push_category=category,
                   push_note=(note or None), push_sha=sha,
                   pushed_at=time.time())
    serve.log_action(window_id, 'repo_push',
                     {'category': category, 'sha': sha, 'edits': len(edits),
                      'agent': row.get('agent'),
                      'validator': row.get('validator'),
                      'note': (note or '')[:300]},
                     authorized=True)
    _telegram_change_request(window_id, category, note, sha=sha,
                             n_edits=len(edits))
    _notify_agent(row.get('agent'), (
        f'Change window {window_id} was validated by {row.get("validator")} '
        f'and PUSHED to main ({sha or "sha unavailable"}), category '
        f'{category}.' + (f' Note: {note}' if note else '')),
        about=f'selfedit:{window_id}')
    return True, {'ok': True, 'status': category, 'sha': sha,
                  'edits': len(edits),
                  'message': f'pushed to origin main ({sha or "sha unavailable"}), '
                             f'category {category}'}


def admin_approve_window(agent_id, window_id, approved, note=''):
    """The admin's push approval for a change request. The director validator
    approves the applied bytes; this is the second, independent approval."""
    serve = _s()
    state = serve.get_state_from_db() or {}
    if not serve._is_admin(state, agent_id):
        return False, {'error': 'only the admin may approve a change request push'}
    row = _window_row(window_id)
    if not row:
        return False, {'error': 'unknown window id'}
    if row.get('status') != 'fired' or row.get('push_status') != 'awaiting_validator':
        return False, {'error': 'nothing is waiting for admin approval right now'}
    note = (note or '').strip()[:MAX_PUSH_NOTE_LEN]
    if not approved:
        _update_window(window_id, admin_status='denied', admin_note=note,
                       admin_approved_at=time.time(), admin_agent=agent_id)
        _fail_window_push(window_id, f'admin {agent_id} denied the push', by=agent_id)
        return True, {'ok': True, 'status': 'unsuccessful',
                      'message': 'admin denied; the window rolled back'}
    _update_window(window_id, admin_status='approved', admin_note=note,
                   admin_approved_at=time.time(), admin_agent=agent_id)
    if row.get('validator_status') != 'approved':
        return True, {'ok': True, 'status': 'awaiting_director',
                      'message': 'admin approved; the director validator must '
                                 'still approve the applied bytes'}
    has_issues = bool(row.get('remediation_used') or _window_unlanded(window_id)
                      or row.get('validator_note'))
    return _push_window(window_id,
                        'successful_with_issues' if has_issues else 'successful',
                        note)


def validate_window(agent_id, window_id, approved, note, issues=False):
    """The validator's push decision for a held change request. Only the
    window's validator may call this, only while the slot is open and the
    window holds. Deny = rollback (nothing pushed). Approve = push, with a
    REQUIRED note whenever the outcome has issues (remediation was used,
    reserved edits failed to apply, or the validator flags concerns)."""
    with _VALIDATION_LOCK:
        return _validate_window_unlocked(agent_id, window_id, approved, note,
                                         issues)


def _validate_window_unlocked(agent_id, window_id, approved, note,
                              issues=False):
    agent_id = (agent_id or '').strip()
    note = (note or '').strip()[:MAX_PUSH_NOTE_LEN]
    row = _window_row(window_id)
    if not row:
        return False, {'error': 'unknown window id'}
    if row.get('validator') != agent_id:
        state = _s().get_state_from_db() or {}
        if (governance.admin_change_request_enabled(state)
                and _s()._is_admin(state, agent_id)):
            return admin_approve_window(agent_id, window_id, approved, note)
        return False, {'error': 'only the change request\'s validator may '
                                'validate it (you are not on record as the '
                                'validator)'}
    if row['status'] != 'fired' or row.get('push_status') != 'awaiting_validator':
        return False, {'error': f"window {window_id} is {row['status']!r} / "
                                f"push {row.get('push_status')!r} -- nothing "
                                'is waiting for validation right now'}
    # A validator turn that started before the slot ended may finish after it:
    # the alternative is losing a real decision to a slow model call. The
    # end-of-slot sweep still rolls back an unanswered window on the next tick.
    grace = bool(row.get('validator_status') == 'deciding'
                 and row.get('validator_attempt_at')
                 and float(row['validator_attempt_at']) <= float(row['end_ts']))
    if row['end_ts'] < time.time() and not grace:
        return False, {'error': 'the window slot has ended -- the sweep will '
                                'roll the edits back; schedule a fresh window'}
    if not approved:
        _fail_window_push(window_id,
                          f'validator {agent_id} denied the push', by=agent_id)
        _update_window(window_id, validator_status='denied',
                       validated_at=time.time())
        return True, {'ok': True, 'status': 'unsuccessful',
                      'message': 'denial recorded -- the window rolled back '
                                 'and nothing was pushed'}
    remediation_used = bool(row.get('remediation_used'))
    unlanded = _window_unlanded(window_id)
    has_issues = bool(remediation_used or unlanded or issues)
    if has_issues and not note:
        why = []
        if remediation_used:
            why.append('in-window remediation was needed')
        if unlanded:
            why.append(f'{len(unlanded)} reserved edit(s) did not apply')
        if issues:
            why.append('you flagged issues')
        return False, {'error': 'a note is REQUIRED for a change request '
                                'with issues (' + '; '.join(why) + ') -- '
                                'describe them for the record, then approve '
                                'again'}
    _update_window(window_id, validator_status='approved',
                   validator_note=note, validated_at=time.time())
    state = _s().get_state_from_db() or {}
    if (governance.admin_change_request_enabled(state)
            and row.get('admin_status') != 'approved'):
        return True, {'ok': True, 'status': 'awaiting_admin',
                      'message': 'director approved; the admin must approve '
                                 'the push before anything lands'}
    category = 'successful_with_issues' if has_issues else 'successful'
    return _push_window(window_id, category, note)
