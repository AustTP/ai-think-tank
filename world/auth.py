"""Player authentication and sessions.

Extracted from serve.py. The one-time "first boot" credential bootstrap
(_get_or_create_server_secret / _get_or_create_admin_credentials /
_get_or_create_device_key) auto-generates the server's HMAC secret, the admin
password (salted PBKDF2 hash, plaintext printed once), and the device bearer
key, writing them into the repo-root .env -- exactly the shape the pre-existing
module-level calls in serve.py expect. The runtime surface (sessions, the
login rate limit, password hashing) lives here too.

Everything that needs serve.py state is reached through `_serve.X` at call
time (the same lazy pattern sim.py and content.py use), so:
  - the env-derived constants and `_db()` that serve.py owns resolve fresh on
    every call, and
  - a test patching `serve.create_session` / `serve.verify_session` /
    `serve.SESSION_COOKIE_NAME` on the serve module is honored by the callers
    that route through `_serve.X`.

Imported back into serve.py as `from auth import ...`; no call site changes.
"""

import hashlib
import os
import secrets
import time

# Session cookie + lifetime, and the PBKDF2 work factor. The server holds the
# session id only in an HttpOnly cookie; page JS (and so an XSS bug) can't
# read it at all.
SESSION_COOKIE_NAME = 'ai_think_tank_session'
SESSION_LIFETIME_S = 7 * 24 * 3600
PBKDF2_ITERATIONS = 200_000

_LOGIN_ATTEMPT_LIMIT_WINDOW_S = 300
_LOGIN_ATTEMPT_LIMIT = 10
_login_attempts: dict[str, list[float]] = {}  # ip -> [timestamps within the current window]


def _hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()
    return salt, digest


def _get_or_create_server_secret():
    # NOT the access gate anymore -- this is now purely an internal
    # cryptographic secret (HMAC key for the boundary markers,
    # _BOUNDARY_SECRET). Kept under its own name so it's not confused with
    # something a browser or an API caller should ever hold. Runs during
    # serve.py's module import (SERVER_ACCESS_KEY assignment), so serve is
    # partially loaded -- only _load_env and THINK_TANK_DIR (both already
    # defined) are touched.
    import serve as _serve
    env = _serve._load_env()
    if env.get('SERVER_SECRET'):
        return env['SERVER_SECRET']
    key = secrets.token_hex(32)
    with open(os.path.join(_serve.THINK_TANK_DIR, '.env'), 'a') as f:
        f.write(f'\nSERVER_SECRET={key}\n')
    return key


def _get_or_create_admin_credentials():
    # First run: generate a real random password (not a placeholder you'd
    # forget to change), store only its salted hash, and print the
    # PLAINTEXT once -- the only time it's ever available in the clear.
    # Same "auto-generate, persist, surface once" shape as every other
    # secret the server creates, applied to something that now actually
    # gates a real login instead of being embedded in every page load.
    import serve as _serve
    env = _serve._load_env()
    username = env.get('ADMIN_USERNAME', 'admin')
    if env.get('ADMIN_PASSWORD_SALT') and env.get('ADMIN_PASSWORD_HASH'):
        return username, env['ADMIN_PASSWORD_SALT'], env['ADMIN_PASSWORD_HASH'], None
    password = secrets.token_urlsafe(12)
    salt, digest = _hash_password(password)
    with open(os.path.join(_serve.THINK_TANK_DIR, '.env'), 'a') as f:
        f.write(f'\nADMIN_USERNAME={username}\nADMIN_PASSWORD_SALT={salt}\nADMIN_PASSWORD_HASH={digest}\n')
    return username, salt, digest, password


def _get_or_create_device_key():
    # A device (an iOS Shortcut, to start) that
    # isn't a player browser session (no cookie support) and isn't an AI
    # agent (agent_keys are per-agent, not per-device) needs its own bearer
    # credential. One player, one phone today -- a single token, not a
    # per-device table; that's real complexity for a need that doesn't exist
    # yet. Same "auto-generate, persist, surface once" shape as the admin
    # password above, but compared directly (secrets.compare_digest) like
    # agent_keys/TELEGRAM_BOT_TOKEN, not hashed -- this is a bearer token
    # presented on every request, not a human-typed password.
    import serve as _serve
    env = _serve._load_env()
    key = env.get('DEVICE_API_KEY')
    if key:
        return key, None
    key = secrets.token_urlsafe(24)
    with open(os.path.join(_serve.THINK_TANK_DIR, '.env'), 'a') as f:
        f.write(f'\nDEVICE_API_KEY={key}\n')
    return key, key  # second value set only when freshly generated -- print it once


def create_session():
    import serve as _serve
    session_id = secrets.token_urlsafe(32)
    now = time.time()
    with _serve._db() as conn:
        conn.execute('INSERT INTO sessions (session_id, created_at, expires_at) VALUES (?, ?, ?)',
                     (session_id, now, now + SESSION_LIFETIME_S))
    return session_id


def verify_session(session_id):
    import serve as _serve
    if not session_id:
        return False
    with _serve._db() as conn:
        row = conn.execute('SELECT expires_at FROM sessions WHERE session_id = ?', (session_id,)).fetchone()
    return bool(row and row[0] > time.time())


def _require_player_session(request):
    """Player-only gate for the /api/intent/* write surface (and the team
    prefix endpoint). The AUTH_PROTECTED_PREFIXES middleware accepts EITHER a
    real session OR a valid agent key (server content executors loopback with
    only a key), but these handlers are the PLAYER's controls -- they hardcode
    actor 'player', and a valid agent key must not let an agent create or close
    a sprint, trigger a publish push, release a product, veto a story, promote
    a spike, or file new work as the player. A session is the only credential
    that satisfies them. Fail closed (False) on anything else."""
    import serve as _serve
    return _serve.verify_session(request.cookies.get(_serve.SESSION_COOKIE_NAME))


def destroy_session(session_id):
    import serve as _serve
    with _serve._db() as conn:
        conn.execute('DELETE FROM sessions WHERE session_id = ?', (session_id,))


def _check_login_rate_limit(ip):
    now = time.time()
    attempts = _login_attempts.setdefault(ip, [])
    attempts[:] = [t for t in attempts if now - t < _LOGIN_ATTEMPT_LIMIT_WINDOW_S]
    if len(attempts) >= _LOGIN_ATTEMPT_LIMIT:
        return False
    attempts.append(now)
    return True