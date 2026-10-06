"""Escalation records and the player-notification outbound channels.

Extracted from serve.py. Two responsibilities:

1. The escalation file (`escalations.json`, kept outside world/ so it is never
   statically served): load/save, record creation with an unguessable token
   per escalation, and the best-effort email that pushes a pending decision to
   the player.
2. Player notification email (one-way SMTP via a Gmail app-password held in
   the encrypted vault) and the proactive Telegram push. All sends are
   fail-closed and never raise -- they're called from the sim loop drain, so
   an exception would propagate into the think tank tick.

All serve.py state is reached through `_serve.X` at call time (the same lazy
pattern sim.py and content.py use), so the env-derived SMTP/Telegram constants
and the vault helpers resolve fresh on every call, and a test patching
`serve.PLAYER_EMAIL_ENABLED` (or any other name here) on the serve module is
honored by these functions.

Imported back into serve.py as `from notify import ...`; no call site changes.
"""

import email.mime.text
import json
import os
import secrets
import smtplib
import time

# One-way SMTP to the player's real address via a Gmail app-password held in
# the encrypted vault (credential name `gmail_smtp`), NOT a capability handle
# -- this is the think tank's own outbound channel, not an agent-delegated
# grant. The FROM/TO are the same player address; only the app-password is
# secret.
GMAIL_SMTP = os.environ.get('AI_THINK_TANK_GMAIL_SMTP_EMAIL') or 'austtp25@gmail.com'
GMAIL_SMTP_HOST = os.environ.get('AI_THINK_TANK_GMAIL_SMTP_HOST') or 'smtp.gmail.com'
GMAIL_SMTP_PORT = int(os.environ.get('AI_THINK_TANK_GMAIL_SMTP_PORT', '587'))
_GMAIL_CRED_NAME = 'gmail_smtp'


def _load_escalations():
    import serve as _serve
    if os.path.exists(_serve.ESCALATIONS_PATH):
        with open(_serve.ESCALATIONS_PATH) as f:
            return json.load(f)
    return {}


def _save_escalations(data):
    import serve as _serve
    with open(_serve.ESCALATIONS_PATH, 'w') as f:
        json.dump(data, f, indent=2)


def _send_escalation_email_sync(subject, body_text):
    import serve as _serve
    # Best-effort -- an admin decision this needs shouldn't hang forever
    # because email isn't configured yet. Logged either way so a missing
    # SMTP setup is visible in the server's own stdout, not silently
    # swallowed.
    if not (_serve.ESCALATION_EMAIL_TO and _serve.SMTP_HOST and _serve.SMTP_USER and _serve.SMTP_PASSWORD):
        print(f'[escalation] SMTP not configured -- would have sent: {subject}')
        return False
    msg = email.mime.text.MIMEText(body_text)
    msg['Subject'] = subject
    msg['From'] = _serve.SMTP_USER
    msg['To'] = _serve.ESCALATION_EMAIL_TO
    try:
        with smtplib.SMTP(_serve.SMTP_HOST, _serve.SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(_serve.SMTP_USER, _serve.SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f'[escalation] send failed: {e}')
        return False


def _credential_token(name):
    import serve as _serve
    with _serve._db() as conn:
        row = conn.execute('SELECT encrypted_value FROM external_credentials WHERE name = ?',
                           (name,)).fetchone()
        return row[0] if row else None


def _send_player_email_sync(subject, body_text):
    """Send one notification email to the player. Fail-closed and best-effort:
    returns True on success, False (after logging) when no credential is
    provisioned or SMTP fails. Must NEVER raise -- it's called from the sim loop
    drain and a raised exception would propagate into the think tank tick."""
    import serve as _serve
    if not _serve.PLAYER_EMAIL_ENABLED:
        return False
    token = None
    try:
        token = _serve._credential_token(_serve._GMAIL_CRED_NAME)
    except Exception as e:
        print(f'[email] credential lookup failed: {e}')
        return False
    if not token:
        print(f'[email] no {_serve._GMAIL_CRED_NAME} credential provisioned -- not sending: {subject}')
        return False
    app_password = _serve._open_secret(token)
    if not app_password:
        print(f'[email] {_serve._GMAIL_CRED_NAME} credential present but undecryptable -- not sending: {subject}')
        return False
    msg = email.mime.text.MIMEText((body_text or '').strip() or '(no body)')
    msg['Subject'] = subject
    msg['From'] = _serve.GMAIL_SMTP
    msg['To'] = _serve.GMAIL_SMTP
    try:
        with smtplib.SMTP(_serve.GMAIL_SMTP_HOST, _serve.GMAIL_SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(_serve.GMAIL_SMTP, app_password)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f'[email] send failed: {e}')
        return False


def send_player_email_sync(subject, body_text):
    """Public alias the pure sim loop drain calls. Sim.py late-imports this so
    the sim stays pure; serve owns all networking + credentials."""
    import serve as _serve
    return _serve._send_player_email_sync(subject, body_text)


def send_player_telegram_sync(subject, body_text):
    """The think tank was reactive-only on Telegram --
    it could reply to an incoming message but never push anything on its own,
    so a spike/story finishing generated no notice on either channel unless
    it happened to be one of the two existing email triggers (agent_ask,
    card_blocked). This is the proactive half: same outbox entry, same
    dedup/drain mechanism as send_player_email_sync (sim.py's
    _drain_email_outbox_sync calls both for every entry), just a second
    best-effort delivery channel. No-op (returns False, never raises) if the
    bridge isn't configured -- mirrors every other optional-integration gate
    in serve.py (AGENT_BROWSING_ENABLED, TAVILY_API_KEY). Telegram has no
    separate subject line, so it's folded into the message text."""
    import serve as _serve
    if not (_serve.TELEGRAM_BOT_TOKEN and _serve.TELEGRAM_ALLOWED_CHAT_IDS):
        return False
    text = f"{subject}\n\n{body_text}" if subject else body_text
    ok = False
    for chat_id in _serve.TELEGRAM_ALLOWED_CHAT_IDS:
        try:
            result = _serve._telegram_api_sync('sendMessage', {'chat_id': chat_id, 'text': text})
        except Exception as e:
            print(f'[telegram] player push failed for {chat_id}: {e}', flush=True)
            result = None
        ok = ok or (result is not None)
    return ok


def provision_player_email(app_password):
    """Admin endpoint body: validate + store the Gmail app-password in the vault,
    then fire a self-test so provisioning is verified, not assumed. Returns a
    dict {ok, test_ok, error?} -- never returns or logs the password."""
    import serve as _serve
    pw = (app_password or '').strip()
    if not _serve._looks_like_gmail_app_password(pw):
        return {'ok': False, 'error': 'not a valid Gmail app-password (16 chars, 4 groups of 4, no spaces)'}
    _serve._store_credential(_serve._GMAIL_CRED_NAME, 'Gmail SMTP (player notifications)', pw)
    test_ok = _serve._send_player_email_sync('[AI Think Tank] Email configured',
                                             'Your AI Think Tank is now emailing you on action-needed events.')
    return {'ok': True, 'test_ok': bool(test_ok)}


def create_escalation(kind, question, on_approve_note='', what_checked='', look_first=''):
    import serve as _serve
    # Escalation-storm guard: a pending escalation with the SAME kind and
    # question is reused instead of spawning a new record. Without this, a
    # retrying agent (the same blocked command, the same stuck story) creates
    # a new escalation per attempt -- the 3.8k-record pileup this guard
    # exists to prevent. Only PENDING records dedup; a resolved one lets a
    # fresh escalation be created if the same thing genuinely recurs later.
    escalations = _serve._load_escalations()
    for esc_id, existing in escalations.items():
        if existing.get('status') == 'pending' and existing.get('kind') == kind \
                and existing.get('question') == question:
            return esc_id
    # A random unguessable token per escalation, not just the record id --
    # the resolve link needs to not be trivially enumerable (id alone
    # would be sequential and guessable).
    esc_id = 'esc-' + secrets.token_hex(4)
    token = secrets.token_urlsafe(24)
    escalations[esc_id] = {'kind': kind, 'question': question, 'status': 'pending', 'token': token, 'ts': time.time(), 'note': on_approve_note, 'whatChecked': what_checked, 'lookFirst': look_first}
    _serve._save_escalations(escalations)

    approve_url = f'{_serve.ESCALATION_BASE_URL}/api/escalation/resolve?id={esc_id}&token={token}&decision=approve'
    deny_url = f'{_serve.ESCALATION_BASE_URL}/api/escalation/resolve?id={esc_id}&token={token}&decision=deny'
    body = (f'{question}\n\n'
            f'What I checked: {what_checked or "(see question)"}\n'
            f'Look here first: {look_first or "the question above"}\n\n'
            f'Approve: {approve_url}\n\n'
            f'Deny: {deny_url}')
    _serve._send_escalation_email_sync(f'[AI Think Tank] Needs your call: {kind}', body)
    return esc_id