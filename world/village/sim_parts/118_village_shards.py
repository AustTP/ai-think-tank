"""Village sharding seam (operator direction 2026-11-21): a durable
cross-village mailbox so villages communicate through a queue instead of
direct cross-village state writes, plus the roster-slice helpers the future
process-per-village split needs.

Today ONE read-modify-write pass advances the entire roster every 6s --
the fundamental O(N) cap on organization size. The path to many processes
is: (1) this mail boundary (DONE -- villages stop needing each other's
state to talk), (2) per-village tick slices behind the same relay (the
movement block must first be lifted into a callable), (3) one process per
village over a village-scoped store, with this queue as the ONLY coupling.

The relay runs inside the global tick's single read-modify-write
(`_village_mail_relay_pass`, wired in 010's pass), so delivery rides the
same save and can never be clobbered by a concurrent blob write.
"""
import time

VILLAGE_MAIL_KEY = 'villageMail'
VILLAGE_MAIL_MAX = 2000
VILLAGE_CLAIM_TIMEOUT_MS = 5 * 60 * 1000
# Mail the relay could not deliver (unknown recipient) expires after a day
# instead of retrying forever -- send_mail's unknown-id no-op, but durable.
VILLAGE_MAIL_TTL_MS = 24 * 60 * 60 * 1000


def _village_of(state, agent_id):
    """Pure helper: one agent's villageId, defaulting to 'main' (matches
    sim_helpers.village_of_agent; kept local so this part stays
    import-order independent)."""
    return (((state.get('agents') or {}).get(agent_id) or {})
            .get('villageId') or 'main')


def _agents_in_village(state, village_id):
    """The roster slice for one village. O(roster) per call -- the shard
    scan. A future process-level shard replaces this scan with its own
    store; the interface (village_id -> agent ids) stays the same."""
    out = []
    for aid, a in (state.get('agents') or {}).items():
        if isinstance(a, dict) and (a.get('villageId') or 'main') == village_id:
            out.append(aid)
    return out


def _village_mail_queue(state):
    """The durable cross-village mailbox:
    state['villageMail'] = [{id, fromVillage, toVillage, entry, status, at,
    attempts, claimedAt, outcome?, doneAt?}]. status: queued -> claimed ->
    done. Bounded by VILLAGE_MAIL_MAX (oldest done entries trimmed first,
    never live mail)."""
    queue = state.get(VILLAGE_MAIL_KEY)
    if not isinstance(queue, list):
        queue = []
        state[VILLAGE_MAIL_KEY] = queue
    return queue


def _village_mail_enqueue(state, from_village, to_village, entry, now_ms):
    """Queue one piece of cross-village mail. Returns the mail id, or None
    when the target village does not exist (fail closed: the sender learns
    immediately instead of queuing into the void)."""
    villages = {v.get('id') for v in (state.get('villages') or [])}
    if to_village not in villages:
        return None
    seq = int(state.get('_villageMailSeq') or 0) + 1
    state['_villageMailSeq'] = seq
    mail_id = f'vmail-{seq}'
    _village_mail_queue(state).append({
        'id': mail_id,
        'fromVillage': from_village,
        'toVillage': to_village,
        'entry': entry if isinstance(entry, dict) else {'text': str(entry)},
        'status': 'queued',
        'at': now_ms,
        'attempts': 0,
        'claimedAt': None,
    })
    _village_mail_trim(state)
    return mail_id


def _village_mail_trim(state):
    queue = _village_mail_queue(state)
    if len(queue) <= VILLAGE_MAIL_MAX:
        return
    excess = len(queue) - VILLAGE_MAIL_MAX
    kept = []
    for m in queue:
        if excess and m.get('status') == 'done':
            excess -= 1
            continue
        kept.append(m)
    queue[:] = kept[-VILLAGE_MAIL_MAX:]


def _village_mail_claim(state, village_id, now_ms):
    """Claim every queued mail addressed to one village (single consumer:
    that village's relay slice). Stamps claimedAt for stale recovery."""
    claimed = []
    for m in _village_mail_queue(state):
        if m.get('status') == 'queued' and m.get('toVillage') == village_id:
            m['status'] = 'claimed'
            m['claimedAt'] = now_ms
            m['attempts'] = int(m.get('attempts') or 0) + 1
            claimed.append(m)
    return claimed


def _village_mail_complete(state, mail_id, outcome, now_ms):
    for m in _village_mail_queue(state):
        if m.get('id') == mail_id and m.get('status') != 'done':
            m['status'] = 'done'
            m['outcome'] = outcome
            m['doneAt'] = now_ms
            m['claimedAt'] = None
            return True
    return False


def _village_mail_recover_stale(state, now_ms):
    """Requeue mail whose consumer claimed it and then died (claimed longer
    than VILLAGE_CLAIM_TIMEOUT_MS ago). Returns the count requeued."""
    n = 0
    for m in _village_mail_queue(state):
        if (m.get('status') == 'claimed'
                and now_ms - (m.get('claimedAt') or 0) > VILLAGE_CLAIM_TIMEOUT_MS):
            m['status'] = 'queued'
            m['claimedAt'] = None
            n += 1
    return n


def _village_mail_expire(state, now_ms):
    """Drop undeliverable mail past its TTL (unknown recipient ages out
    instead of retrying forever). Returns the count expired."""
    n = 0
    for m in _village_mail_queue(state):
        if (m.get('status') in ('queued', 'claimed')
                and now_ms - (m.get('at') or 0) > VILLAGE_MAIL_TTL_MS):
            m['status'] = 'done'
            m['outcome'] = 'expired'
            m['doneAt'] = now_ms
            m['claimedAt'] = None
            n += 1
    return n


def _village_mail_deliver(state, village_id, now_ms):
    """Deliver one village's claimed mail into recipient mailboxes and wake
    the recipients through the standard wake queue. Mail addressed to an
    unknown recipient (or one that has left the village) completes as
    'dropped'. Returns the count delivered."""
    claimed = _village_mail_claim(state, village_id, now_ms)
    delivered = 0
    agents = state.get('agents') or {}
    for m in claimed:
        entry = m.get('entry') or {}
        recipient = entry.get('toAgent') or ''
        agent = agents.get(recipient)
        if not isinstance(agent, dict) or (agent.get('villageId') or 'main') != village_id:
            _village_mail_complete(state, m['id'], 'dropped', now_ms)
            continue
        _append_mailbox(agent, entry.get('mail') if isinstance(entry.get('mail'), dict)
                        else dict(entry, fromVillage=m.get('fromVillage')))
        delivered += 1
        _village_mail_complete(state, m['id'], 'delivered', now_ms)
        try:
            _wake_enqueue(state, recipient,
                          entry.get('kind') or 'village_mail',
                          {'villageMailId': m['id'],
                           'about': entry.get('about') or '',
                           'fromVillage': m.get('fromVillage')}, now_ms)
        except Exception:
            pass  # wake is best-effort; the mailbox note is the durable part
    return delivered


def _village_mail_relay_pass(state, now_ms):
    """The global relay: recover stale claims, expire TTL'd mail, then
    deliver every village's queued mail. Runs INSIDE the global tick's
    read-modify-write (wired in 010's pass) so delivery rides the same
    save. Returns {villageId: deliveredCount}."""
    try:
        _village_mail_recover_stale(state, now_ms)
        _village_mail_expire(state, now_ms)
    except Exception:
        pass
    villages = {v.get('id') for v in (state.get('villages') or []) if v.get('id')}
    for a in (state.get('agents') or {}).values():
        if isinstance(a, dict):
            villages.add(a.get('villageId') or 'main')
    out = {}
    for village_id in sorted(villages):
        try:
            out[village_id] = _village_mail_deliver(state, village_id, now_ms)
        except Exception:
            out[village_id] = -1
    return out
