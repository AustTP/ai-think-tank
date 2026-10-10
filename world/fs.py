"""Scoped, capability-gated access to the player's own folders (Desktop,
Downloads, ...) and iCloud Notes, for the think tank's agents.

This is the think tank's answer to "give the app access to organize my
Desktop": agents never hold macOS paths or Notes credentials directly -- they
get a `local_file` tool whose every call is checked against a player-issued
grant (a scope root + capability set) stored in state. The trust root is the
player's explicit grant, and three hard rules are enforced regardless of any
grant:

1. CONTAINMENT: every path is realpath-resolved and must stay inside the
   granted scope root. `..`, symlink escapes, and absolute paths outside the
   scope are refused.
2. TRASH RULE: `~/.Trash` is never a valid target. Deletes move a file INTO
   the trash (reversible) -- they never permanently unlink, and they can never
   EMPTY the trash. `emptyTrash` is a capability that is never granted.
3. NOTES OWNERSHIP: existing notes are read-only. An agent may create a new
   note or modify/delete a note it created (tracked in a `noteOwnership`
   registry); it can never touch a note someone else made.

The module is dependency-free (stdlib only). Grants live in state under
`fileGrants`; the ownership registry under `noteOwnership`. Follows the
extracted-module pattern: lazy `import serve as _serve`, call-time
`_serve.X`, `with _serve._db()` for state reads.
"""
import os
import shutil
import subprocess
import time

_TRASH_RULE = 'the trash is off-limits: deletes move to trash and never empty it'


def _state():
    import serve as _serve
    return _serve.get_state_from_db()


def _trash_dir():
    return os.path.expanduser(os.environ.get('TRASH_DIR', '~/.Trash'))


def _grants(state):
    return state.get('fileGrants') or []


def _grant_for(state, scope):
    root = os.path.realpath(os.path.expanduser(scope))
    for g in _grants(state):
        if os.path.realpath(os.path.expanduser(g.get('scope') or '')) == root:
            return g
    return None


def _resolve(grant, rel_path):
    """Resolve a relative path inside a grant's scope, enforcing containment.
    Returns the absolute path, or raises PermissionError."""
    scope = os.path.realpath(os.path.expanduser(grant.get('scope') or ''))
    if not rel_path:
        return scope
    if rel_path.startswith('/'):
        raise PermissionError('path must be relative to the granted scope')
    joined = os.path.realpath(os.path.join(scope, rel_path))
    if joined != scope and not joined.startswith(scope + os.sep):
        raise PermissionError('path escapes the granted scope')
    trash = os.path.realpath(_trash_dir())
    if joined == trash or joined.startswith(trash + os.sep):
        raise PermissionError(_TRASH_RULE)
    return joined


def _require_cap(grant, cap):
    if not (grant.get('caps') or {}).get(cap):
        raise PermissionError(f'grant does not allow {cap}')


def list_scope(scope, rel_path=''):
    """List a directory inside the granted scope (default: the scope root).
    Returns [{name, type, size}]."""
    state = _state()
    grant = _grant_for(state, scope)
    if not grant:
        raise PermissionError('no grant for this scope')
    _require_cap(grant, 'read')
    root = os.path.realpath(os.path.expanduser(scope))
    p = _resolve(grant, rel_path)
    if not os.path.isdir(p):
        raise FileNotFoundError(f'not a directory: {rel_path}')
    entries = []
    for name in sorted(os.listdir(p)):
        fp = os.path.join(p, name)
        entries.append({'name': name,
                        'type': 'dir' if os.path.isdir(fp) else 'file',
                        'size': os.path.getsize(fp) if os.path.isfile(fp) else None})
    return entries


def read_file(scope, rel_path, max_bytes=200000):
    """Read a file inside a granted scope. Returns text (utf-8, errors=replace),
    truncated to max_bytes."""
    state = _state()
    grant = _grant_for(state, scope)
    if not grant:
        raise PermissionError('no grant for this scope')
    _require_cap(grant, 'read')
    p = _resolve(grant, rel_path)
    if not os.path.isfile(p):
        raise FileNotFoundError(f'not a file: {rel_path}')
    with open(p, 'r', encoding='utf-8', errors='replace') as f:
        return f.read(max_bytes)


def write_file(scope, rel_path, content):
    """Create or overwrite a file inside a granted scope (utf-8)."""
    state = _state()
    grant = _grant_for(state, scope)
    if not grant:
        raise PermissionError('no grant for this scope')
    _require_cap(grant, 'write')
    p = _resolve(grant, rel_path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(content)
    return p


def move_file(scope, src_rel, dst_rel):
    """Move/rename a file inside a granted scope (organizing the desktop)."""
    state = _state()
    grant = _grant_for(state, scope)
    if not grant:
        raise PermissionError('no grant for this scope')
    _require_cap(grant, 'write')
    src = _resolve(grant, src_rel)
    dst = _resolve(grant, dst_rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    return dst


def delete_to_trash(scope, rel_path):
    """Move a file/folder inside a granted scope to the trash (reversible).
    Never empties the trash and never touches the trash itself."""
    state = _state()
    grant = _grant_for(state, scope)
    if not grant:
        raise PermissionError('no grant for this scope')
    _require_cap(grant, 'delete')
    p = _resolve(grant, rel_path)
    if not os.path.exists(p):
        raise FileNotFoundError(f'not found: {rel_path}')
    trash = os.path.realpath(_trash_dir())
    os.makedirs(trash, exist_ok=True)
    base = os.path.basename(p)
    dest = os.path.join(trash, base)
    n = 1
    while os.path.exists(dest):
        stem, ext = os.path.splitext(base)
        dest = os.path.join(trash, f'{stem}-{n}{ext}')
        n += 1
    shutil.move(p, dest)
    return dest


# ---------------------------------------------------------------------------
# iCloud Notes: read existing (always), create new / modify-own (ownership).
# Backed by AppleScript against the Notes app; requires macOS automation
# permission (TCC) for the calling process. Best-effort -- a Notes failure is
# reported, never fatal.
# ---------------------------------------------------------------------------

def _osascript(script):
    try:
        proc = subprocess.run(
            ['osascript', '-e', script], capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            return None, (proc.stderr or '').strip()
        return proc.stdout.strip(), None
    except Exception as e:
        return None, str(e)


def notes_list():
    """Read all notes (title + body). Always allowed for existing notes."""
    out, err = _osascript(
        'tell application "Notes"\n'
        '  set out to ""\n'
        '  repeat with n in notes\n'
        '    set out to out & (name of n) & "\\t" & (body of n) & "\\n"\n'
        '  end repeat\n'
        '  return out\n'
        'end tell')
    if err:
        raise PermissionError(f'Notes unavailable: {err}')
    rows = []
    for line in out.splitlines():
        if '\t' in line:
            title, body = line.split('\t', 1)
            rows.append({'title': title, 'body': body})
    return rows


def notes_create(agent_id, title, body):
    """Create a new note and record the creating agent's ownership."""
    out, err = _osascript(
        'tell application "Notes"\n'
        f'  set n to make new note with properties {{name:"{_esc(title)}", body:"{_esc(body)}"}}\n'
        f'  return id of n\n'
        'end tell')
    if err:
        raise PermissionError(f'Notes create failed: {err}')
    note_id = out or f'note-{int(time.time() * 1000)}'
    state = _state()
    state.setdefault('noteOwnership', {})[note_id] = {
        'agentId': agent_id, 'createdAt': time.time()}
    import serve as _serve
    _serve.save_state_to_db(state)
    return note_id


def notes_modify(agent_id, note_id, body):
    """Modify a note the agent created. Ownership enforced."""
    state = _state()
    owner = (state.get('noteOwnership') or {}).get(note_id)
    if not owner or owner.get('agentId') != agent_id:
        raise PermissionError('can only modify a note this agent created')
    out, err = _osascript(
        'tell application "Notes"\n'
        f'  set body of note id "{_esc(note_id)}" to "{_esc(body)}"\n'
        'end tell')
    if err:
        raise PermissionError(f'Notes modify failed: {err}')
    return True


def notes_delete(agent_id, note_id):
    """Delete a note the agent created. Ownership enforced."""
    state = _state()
    owner = (state.get('noteOwnership') or {}).get(note_id)
    if not owner or owner.get('agentId') != agent_id:
        raise PermissionError('can only delete a note this agent created')
    out, err = _osascript(
        'tell application "Notes"\n'
        f'  delete note id "{_esc(note_id)}"\n'
        'end tell')
    if err:
        raise PermissionError(f'Notes delete failed: {err}')
    state.get('noteOwnership', {}).pop(note_id, None)
    import serve as _serve
    _serve.save_state_to_db(state)
    return True


def _esc(s):
    return str(s or '').replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')