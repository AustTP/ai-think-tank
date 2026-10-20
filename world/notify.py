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
import threading
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

# Serialize escalation read/modify/write. The director loop, the human
# approve/deny link, and the request endpoints all touch the same JSON file;
# an unlocked write can lose a decision or leave a half-written file.
_ESCALATIONS_LOCK = threading.Lock()


def _load_escalations():
    import serve as _serve
    with _ESCALATIONS_LOCK:
        if os.path.exists(_serve.ESCALATIONS_PATH):
            try:
                with open(_serve.ESCALATIONS_PATH) as f:
                    return json.load(f)
            except (OSError, ValueError) as e:
                print(f'[escalation] could not read {_serve.ESCALATIONS_PATH}: {e}')
                return {}
        return {}


def _save_escalations(data, replace_missing=False):
    import serve as _serve
    with _ESCALATIONS_LOCK:
        path = _serve.ESCALATIONS_PATH
        # Merge a concurrent writer's decisions before replacing the file.
        # The director loop and the human approve/deny link each load, change
        # one row, and save; without this merge the loop's pending copy could
        # overwrite a decision the human just made.
        try:
            with open(path) as f:
                disk = json.load(f) or {}
        except (OSError, ValueError):
            disk = {}
        for eid, esc in disk.items():
            current = data.get(eid)
            if current is None and not replace_missing:
                data[eid] = esc
            elif (esc.get('status') in ('approved', 'denied')
                  and current.get('status') == 'pending'):
                data[eid] = esc
        tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass


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


_ESCALATION_KIND_GUIDANCE = {
    # kind -> (impact, suggested action). Added for the live director
    # complaint that gates/notifications "arrive without enough context to
    # tell whether a block matters... I can't always tell whether to wait or
    # push" (Nora). Every escalation message now says what is actually
    # blocked by this decision and what a sensible call looks like.
    'allowlist request': (
        'Agent research on this host is waiting on this decision. Meanwhile the host still works '
        'through the normal per-request Jev gate (each visit judged individually) -- work is slowed, '
        'not stopped. Approving grants standing read+write reachability for sandbox scripts too.',
        'If the domain is an ordinary public source that fits the stated purpose, approve; deny '
        'otherwise. Denying is safe -- agents keep using the per-request gate.'),
    'allowlist candidate': (
        'This host earned confident Jev approvals repeatedly; approving makes that standing '
        'permanent (full read+write reach for sandbox scripts). Until then every visit re-runs Jev.',
        'Approve for routine public sources with a clean history; deny if the host\'s standing '
        'has changed.'),
    'blocked command': (
        'The command did NOT run -- the assigned task may be stalled waiting on it.',
        'Approve only if you recognize this exact command and want it run; otherwise deny and the '
        'worker should find another approach.'),
    'blocked pipeline step': (
        'The pipeline step did NOT run -- the delivery it belongs to is held up.',
        'Approve only if the step is the one you expect from this card; otherwise deny.'),
    'blocked browser action': (
        'The browser action was refused -- the investigation either adapted or stalled on it.',
        'Approve only if the page/action is clearly in scope; otherwise deny.'),
    'unsure safety decision': (
        'A worker paused on a genuinely unclear safety call and is waiting on yours.',
        'Decide from the question above; deny if the case for safety is not clearly made.'),
    'unresolved review requirement': (
        'A review requirement could not be resolved by the team -- the card is waiting.',
        'Approve the resolution described if it genuinely addresses the requirement; else deny '
        'so it goes back for real work.'),
    'personnel_strike': (
        'A firing strike is on record for this agent -- no personnel action was taken '
        'automatically.',
        'Read the cited reports first; this is informational unless you act on it.'),
}


def notify_human_of_escalation(esc_id, esc, context=''):
    """Send the approve/deny email + Telegram for one escalation. Called at
    creation for HUMAN-ONLY kinds (risk floor >= 1.0 -- the director loop is
    barred from them, so the human link is the only path), and by the director
    loop when it GIVES UP on a delegable kind (ask cap, judge-drift circuit).
    Delegable kinds are silent at creation on purpose: the director resolves
    them on its next pass, and the human only hears about the ones that
    actually need the human."""
    import serve as _serve
    esc_id = esc.get('id') or esc_id
    token = esc.get('token') or ''
    kind = esc.get('kind') or 'unknown'
    question = esc.get('question') or ''
    prefix = f'{context}\n\n' if context else ''
    impact, action = _ESCALATION_KIND_GUIDANCE.get(
        kind,
        ('Something an agent asked for is waiting on this decision; nothing has been applied yet.',
         'Approve only if the request above is clearly safe and in scope; otherwise deny -- '
         'denying is always the safe default.'))
    approve_url = f'{_serve.ESCALATION_BASE_URL}/api/escalation/resolve?id={esc_id}&token={token}&decision=approve'
    deny_url = f'{_serve.ESCALATION_BASE_URL}/api/escalation/resolve?id={esc_id}&token={token}&decision=deny'
    body = (f'{prefix}{question}\n\n'
            f'What is blocked: {impact}\n'
            f'Suggested call: {action}\n'
            f'What I checked: {esc.get("whatChecked") or "(see question)"}\n'
            f'Look here first: {esc.get("lookFirst") or "the question above"}\n\n'
            f'Approve: {approve_url}\n\n'
            f'Deny: {deny_url}')
    _serve._send_escalation_email_sync(f'[AI Think Tank] Needs your call: {kind}', body)
    # Telegram is the channel that actually reaches the player when SMTP
    # isn't configured -- same approve/deny links, fail-closed best-effort
    # like every other optional push. Telegram caps message text at 4096
    # chars; truncate the free-text middle (question/checked/look) but NEVER
    # the resolve URLs -- a truncated Approve link is a dead escalation.
    _tg_head = f'Needs your call: {kind}\n\n{(prefix + question)[:1000]}'
    _tg_tail = (f'\n\nWhat is blocked: {impact[:300]}\n'
                f'Suggested call: {action[:300]}\n'
                f'What I checked: {(esc.get("whatChecked") or "(see question)")[:300]}\n'
                f'Look here first: {(esc.get("lookFirst") or "the question above")[:300]}\n\n'
                f'Approve: {approve_url}\n\nDeny: {deny_url}')
    _serve.send_player_telegram_sync('', (_tg_head + _tg_tail)[:4096])


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

    # Human-only kinds (floor >= 1.0) notify the player immediately -- the
    # director loop is barred from them, so the email link IS the only path.
    # Delegable kinds (floor < 1.0) stay SILENT here: the director loop
    # resolves them on its next pass; the human is notified only if the
    # director gives up (ask cap / judge-drift circuit, see the loop).
    if _serve._escalation_floor(kind) >= 1.0:
        notify_human_of_escalation(esc_id, escalations[esc_id])
    return esc_id