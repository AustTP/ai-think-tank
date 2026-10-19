"""Theo's role policy: what the think tank admin (Theo) will do for
the person talking to him.

The owner (the player, the human who owns the think tank), the
directors, and the workers all talk to Theo, but Theo's authority
is not the same for each. This module is that difference, as data:
a role per requester, a set of actions each role may ask Theo to
perform, the small set of actions that need the owner's explicit
approval first, and the per-requester rate limit and budget that
stop an agent from spending Theo's authority faster than a human
could notice. It is pure logic over the roster in `state` -- it
never imports serve.py, so it stays testable and free of the
request/auth machinery.

Roles are resolved from the roster, never from a self-claim: the
player's session is the owner, the admin agent is a director (Theo
himself, the top of the agent chain), an agent who supervises
others is a director, and every other agent is a worker. A key is
per agent already (serve.get_or_create_agent_key), so "a different
key for each agent Theo talks to" is the existing credential -- the
policy is what those keys are allowed to ask for.

Two lanes reach this policy. evaluate() resolves the requester from
the roster: a player session or an agent talking to Theo directly.
evaluate_key() resolves it from a granted key: an agent acting
through a credential the owner issued, which carries its own role
and its own rate limit and budget, so an agent cannot ask for more
through a key than the role the owner granted it.

Approval gates are the abuse safeguard: a director or worker asking
for spend, external communication, repo modification, a new agent,
or a new container does not get it. Theo records a pending approval
and tells the requester the owner must approve it first. Only the
owner approves.
"""

import hashlib
import json
import secrets
import time

ROLE_OWNER = 'owner'
ROLE_DIRECTOR = 'director'
ROLE_WORKER = 'worker'

ROLES = (ROLE_OWNER, ROLE_DIRECTOR, ROLE_WORKER)

# Only the owner may change the rules of the think tank itself.
OWNER_ONLY = {'change_policy', 'grant_key', 'revoke_key'}

# Actions that spend money, reach outside, or change the fleet. A
# director or worker asking for one of these gets a pending approval
# the owner must decide, never the action itself.
APPROVAL_GATES = {
    'spend',
    'external_comm',
    'modify_repo',
    'add_agent',
    'provision_container',
}

# Runs inside the sandbox proxy, never on the host.
SANDBOXED_ACTIONS = {'request_tool_call'}

# What a director may ask Theo to do: read and shape the work, run
# it, report on it, and talk to the team. Media handling is here
# because a director reviews artifacts, and it is separately guarded
# by the content-class rule in evaluate().
DIRECTOR_ACTIONS = {
    'read_backlog',
    'create_story',
    'assign_work',
    'start_run',
    'report_status',
    'read_artifact',
    'request_tool_call',
    'exec_container',
    'shutdown_container',
    'process_image',
    'process_video',
    'report_spend',
    'budget_review',
    'send_message',
    'read_message',
    'create_schedule',
    'cancel_schedule',
}

# A worker may report, read what is theirs, request a sandboxed tool
# call, and message the team. Everything else is above them.
WORKER_ACTIONS = {
    'report_status',
    'read_artifact',
    'request_tool_call',
    'send_message',
    'read_message',
}

# Talking to Theo is open to everyone -- the ask lane is a
# conversation, not a change to the think tank.
COMMON_ACTIONS = {'converse'}

MEDIA_ACTIONS = {'process_image', 'process_video'}

# What each role may ask Theo to do directly. The approval
# gates are deliberately NOT listed here -- they are handled
# by the gate inside evaluate(), so a director or worker
# asking for one gets a pending approval rather than a flat
# denial. The owner keeps them so an owner request passes
# the capability check straight through.
CAPABILITIES = {
    ROLE_OWNER: COMMON_ACTIONS | DIRECTOR_ACTIONS | OWNER_ONLY | APPROVAL_GATES,
    ROLE_DIRECTOR: COMMON_ACTIONS | DIRECTOR_ACTIONS,
    ROLE_WORKER: COMMON_ACTIONS | WORKER_ACTIONS,
}

ALL_ACTIONS = set().union(*CAPABILITIES.values())

# Abuse prevention defaults: a requester may not drive more than
# this many Theo actions per window, and a requester with a budget
# set may not charge more than that budget. No budget set (None)
# means unlimited -- distinct from a budget of 0, which allows
# nothing further to be charged.
DEFAULT_RATE_LIMIT = 100
RATE_WINDOW_SECONDS = 60.0


def role_for_requester(state, requester_id):
    """The role of whoever is talking to Theo, resolved from the
    roster rather than from any self-claimed field.

    None is the player's own session -- the owner. The admin agent
    (Theo) is a director: the top of the agent chain, but still an
    agent, never the owner. An agent the roster marks isDirector, or
    one that supervises at least one other agent (some roster entry
    names it as their director), is a director. Every other agent is
    a worker. An unknown id is a worker -- the least authority, so a
    stale or mistyped id can only under-serve, never over-grant."""
    if not requester_id:
        return ROLE_OWNER
    roster = (state or {}).get('agentRoster') or []
    entry = None
    has_reports = False
    for d in roster:
        if not isinstance(d, dict):
            continue
        if d.get('id') == requester_id:
            entry = d
        if d.get('director') == requester_id:
            has_reports = True
        if entry is not None and has_reports:
            break
    if entry is not None and entry.get('isAdmin'):
        return ROLE_DIRECTOR
    if entry is not None and (entry.get('isDirector') or has_reports):
        return ROLE_DIRECTOR
    return ROLE_WORKER


class ActionRequest(object):
    """One ask: the action the requester wants and the arguments
    for it. `approval_id` carries an approval the owner already
    granted, so a gated ask can present it instead of creating a
    new pending approval."""

    def __init__(self, action, payload=None, approval_id=None):
        self.action = action
        self.payload = dict(payload or {})
        self.approval_id = approval_id


class Decision(object):
    """The outcome of one ask: whether Theo will do it, and why not
    when he won't. `agent_id` names the acting agent on the key lane
    (None on the roster lane, where the requester already is the
    agent). `requires_approval` carries the pending approval id the
    owner must decide."""

    def __init__(self, allowed, role, action, agent_id=None,
                 requires_approval=False, approval_id=None,
                 reason=''):
        self.allowed = allowed
        self.agent_id = agent_id
        self.role = role
        self.action = action
        self.requires_approval = requires_approval
        self.approval_id = approval_id
        self.reason = reason

    def as_dict(self):
        return {
            'allowed': self.allowed,
            'agentId': self.agent_id,
            'role': self.role,
            'action': self.action,
            'requiresApproval': self.requires_approval,
            'approvalId': self.approval_id,
            'reason': self.reason,
        }


class _Usage(object):
    __slots__ = ('count', 'window_start')

    def __init__(self, count=0, window_start=0.0):
        self.count = count
        self.window_start = window_start


class UsageTracker(object):
    """Per-requester rate limit and budget, the two dials that keep
    an agent from burning Theo's authority. The clock is injectable so
    tests can pin time; production passes nothing and gets time.time."""

    def __init__(self, rate_limit=DEFAULT_RATE_LIMIT,
                 window_seconds=RATE_WINDOW_SECONDS, clock=time.time):
        self.rate_limit = rate_limit
        self.window_seconds = window_seconds
        self.clock = clock
        self._usage = {}
        self._budgets = {}
        self._rate_limits = {}

    def set_rate_limit(self, requester_id, rate_limit):
        """Override the requester's rate limit. A granted key
        carries its own limit (KeyRegistry.grant_key sets it
        here), so a key holder burns their allowance, not the
        whole agent's. None clears the override back to the
        policy-wide default."""
        if rate_limit is None:
            self._rate_limits.pop(requester_id, None)
        else:
            self._rate_limits[requester_id] = int(rate_limit)

    def _limit_for(self, requester_id):
        """The requester's effective rate limit: their override
        when a key set one, else the policy-wide default."""
        return self._rate_limits.get(requester_id, self.rate_limit)

    def set_budget(self, requester_id, amount):
        """Set the requester's spend budget. None clears the cap
        (unlimited); 0 blocks all further spending."""
        if amount is None:
            self._budgets.pop(requester_id, None)
        else:
            self._budgets[requester_id] = float(amount)

    def charge(self, requester_id, amount):
        """Spend `amount` of the requester's budget. Returns True
        when the charge fits (and deducts it), False when it would
        exceed a configured budget. A requester with no budget set
        is unlimited; a budget spent down to zero allows nothing
        further."""
        budget = self._budgets.get(requester_id)
        if budget is None:
            return True
        if amount > budget:
            return False
        self._budgets[requester_id] = budget - amount
        return True

    def allow(self, requester_id):
        """True while the requester is inside their rate limit. The
        window is fixed-size (not sliding): the first request in a
        window starts it, and the count resets when the window has
        fully elapsed."""
        now = self.clock()
        usage = self._usage.get(requester_id)
        limit = self._limit_for(requester_id)
        if usage is None or now - usage.window_start >= self.window_seconds:
            self._usage[requester_id] = _Usage(count=1, window_start=now)
            return True
        if usage.count >= limit:
            return False
        usage.count += 1
        return True


class AgentKey(object):
    """A granted credential: the raw secret (never stored,
    only its SHA-256 hash), the agent it belongs to, the
    role that agent holds through this key, and the key's
    own rate limit and budget. A key's limits are the key
    holder's allowance -- separate from the roster-lane
    limits the same agent would have talking to Theo
    directly."""

    __slots__ = ('key_id', 'agent_id', 'role', 'key_hash',
                 'created_at', 'expires_at', 'active',
                 'rate_limit', 'budget')

    def __init__(self, key_id, agent_id, role, key_hash,
                 created_at, expires_at=None, active=True,
                 rate_limit=DEFAULT_RATE_LIMIT, budget=None):
        self.key_id = key_id
        self.agent_id = agent_id
        self.role = role
        self.key_hash = key_hash
        self.created_at = created_at
        self.expires_at = expires_at
        self.active = active
        self.rate_limit = rate_limit
        self.budget = budget

    def as_dict(self):
        """The key's record without the secret: the hash is
        safe to list, the raw key never leaves grant_key."""
        return {
            'keyId': self.key_id,
            'agentId': self.agent_id,
            'role': self.role,
            'createdAt': self.created_at,
            'expiresAt': self.expires_at,
            'active': self.active,
            'rateLimit': self.rate_limit,
            'budget': self.budget,
        }


class KeyRegistry(object):
    """The owner's grants of Theo's authority to agents: who
    holds a key, as which role, with what limits. Only the
    owner grants and revokes. The raw key is shown once at
    grant time and stored nowhere -- only its hash is kept,
    so a stolen registry copy cannot be replayed. Grants
    persist through save()/load(), so a restart keeps what
    the owner decided."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self._by_id = {}
        self._by_hash = {}

    def grant_key(self, requester_role, agent_id, role,
                  rate_limit=DEFAULT_RATE_LIMIT, budget=None,
                  ttl=None):
        """Grant `agent_id` a key carrying `role`. Only the
        owner may grant, and the role must be a real one --
        anything else returns None and grants nothing. The
        raw key is returned exactly once, paired with its
        AgentKey record."""
        if requester_role != ROLE_OWNER or role not in ROLES:
            return None
        raw = secrets.token_urlsafe(32)
        now = self.clock()
        key = AgentKey(
            'key-%s' % secrets.token_hex(8),
            agent_id, role,
            hashlib.sha256(raw.encode('utf-8')).hexdigest(),
            now,
            expires_at=(now + ttl) if ttl is not None else None,
            rate_limit=rate_limit,
            budget=budget)
        self._by_id[key.key_id] = key
        self._by_hash[key.key_hash] = key.key_id
        return raw, key

    def verify(self, raw_key):
        """Resolve a presented raw key to its AgentKey. None
        when the key was never granted, has been revoked, or
        has passed its expiry -- all three look the same to
        the holder, so a revoked key cannot be probed."""
        if not raw_key:
            return None
        key_id = self._by_hash.get(
            hashlib.sha256(raw_key.encode('utf-8')).hexdigest())
        if key_id is None:
            return None
        key = self._by_id[key_id]
        if not key.active:
            return None
        if key.expires_at is not None \
                and self.clock() > key.expires_at:
            return None
        return key

    def revoke(self, requester_role, key_id):
        """Deactivate a key. Only the owner may revoke;
        anyone else gets False and the key keeps working."""
        if requester_role != ROLE_OWNER:
            return False
        key = self._by_id.get(key_id)
        if key is None:
            return False
        key.active = False
        return True

    def list_keys(self):
        """Every granted key's record, secret-free, in grant
        order."""
        return [key.as_dict() for key in self._by_id.values()]

    def save(self, path):
        """Persist the registry (hashes only) to `path` as
        JSON, so a restart keeps the owner's grants."""
        entries = []
        for key in self._by_id.values():
            entries.append({
                'key_id': key.key_id,
                'agent_id': key.agent_id,
                'role': key.role,
                'key_hash': key.key_hash,
                'created_at': key.created_at,
                'expires_at': key.expires_at,
                'active': key.active,
                'rate_limit': key.rate_limit,
                'budget': key.budget,
            })
        with open(path, 'w') as handle:
            json.dump(entries, handle)

    @classmethod
    def load(cls, path, clock=time.time):
        """Rebuild a registry from save()'s JSON. Hashes only
        -- raw keys were never stored, so nothing here can
        replay a grant."""
        registry = cls(clock=clock)
        with open(path) as handle:
            entries = json.load(handle)
        for entry in entries:
            key = AgentKey(
                entry['key_id'], entry['agent_id'],
                entry['role'], entry['key_hash'],
                entry['created_at'],
                expires_at=entry['expires_at'],
                active=entry['active'],
                rate_limit=entry['rate_limit'],
                budget=entry['budget'])
            registry._by_id[key.key_id] = key
            registry._by_hash[key.key_hash] = key.key_id
        return registry


class PendingApproval(object):
    __slots__ = ('approval_id', 'requester_id', 'role', 'action',
                 'payload', 'created_at', 'status', 'decided_by')

    def __init__(self, approval_id, requester_id, role, action,
                 payload, created_at):
        self.approval_id = approval_id
        self.requester_id = requester_id
        self.role = role
        self.action = action
        self.payload = payload
        self.created_at = created_at
        self.status = 'pending'
        self.decided_by = None


class AuditEntry(object):
    __slots__ = ('timestamp', 'requester_id', 'role', 'action',
                 'allowed', 'requires_approval', 'approval_id', 'reason')

    def __init__(self, timestamp, requester_id, role, action, allowed,
                 requires_approval=False, approval_id=None, reason=''):
        self.timestamp = timestamp
        self.requester_id = requester_id
        self.role = role
        self.action = action
        self.allowed = allowed
        self.requires_approval = requires_approval
        self.approval_id = approval_id
        self.reason = reason


class AuditLog(object):
    """The record of every decision Theo made, so the owner can see
    who asked for what and whether Theo let it through. In-memory in
    this module; serve.py's log_action is the durable copy the
    routing layer writes alongside it."""

    def __init__(self):
        self._entries = []

    def record(self, entry):
        self._entries.append(entry)

    def entries(self):
        return list(self._entries)

    def for_requester(self, requester_id):
        return [e for e in self._entries if e.requester_id == requester_id]


class TheoPolicy(object):
    """The policy engine. Construct once, injectable clock for
    tests. evaluate() is the roster lane the routing layer
    uses; evaluate_key() is the granted-key lane, which needs
    a KeyRegistry passed at construction.

    Pending approvals are DURABLE when a storage_path is set: the
    owner may answer an `apr-...` id hours later, or after a restart,
    and it must still resolve. Without persistence the pending object
    lived only in memory and a restart silently dropped it."""

    DECIDED_RETENTION_S = 7 * 24 * 3600

    def __init__(self, rate_limit=DEFAULT_RATE_LIMIT,
                 window_seconds=RATE_WINDOW_SECONDS,
                 clock=time.time, registry=None, storage_path=None):
        self.clock = clock
        self.usage = UsageTracker(rate_limit=rate_limit,
                                  window_seconds=window_seconds,
                                  clock=clock)
        self.audit = AuditLog()
        self.registry = registry
        self._approvals = {}
        self._approved = set()
        self.storage_path = storage_path
        if storage_path:
            self.load_persisted()

    def _persist(self):
        if not self.storage_path:
            return
        now = self.clock()
        # Decide-and-forget approvals are kept a week for audit, then dropped.
        for aid, approval in list(self._approvals.items()):
            if (approval.status != 'pending'
                    and now - float(approval.created_at or 0) > self.DECIDED_RETENTION_S):
                self._approvals.pop(aid, None)
                self._approved.discard(aid)
        data = {'approvals': [
            {'approval_id': a.approval_id, 'requester_id': a.requester_id,
             'role': a.role, 'action': a.action, 'payload': a.payload,
             'created_at': a.created_at, 'status': a.status,
             'decided_by': a.decided_by}
            for a in self._approvals.values()
        ]}
        tmp = self.storage_path + '.tmp'
        try:
            import os
            os.makedirs(os.path.dirname(self.storage_path) or '.', exist_ok=True)
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.storage_path)
        except (OSError, ValueError):
            try:
                import os
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def load_persisted(self):
        if not self.storage_path:
            return
        try:
            with open(self.storage_path) as f:
                data = json.load(f) or {}
        except (OSError, ValueError):
            return
        for row in data.get('approvals') or []:
            try:
                approval = PendingApproval(
                    row['approval_id'], row['requester_id'], row['role'],
                    row['action'], row.get('payload') or {},
                    row.get('created_at') or self.clock())
            except (KeyError, TypeError):
                continue
            approval.status = row.get('status') or 'pending'
            approval.decided_by = row.get('decided_by')
            self._approvals[approval.approval_id] = approval
            if approval.status == 'approved':
                self._approved.add(approval.approval_id)

    def evaluate(self, state, requester_id, action, payload=None):
        """Decide whether Theo does `action` for `requester_id`.
        `state` supplies the roster (role resolution); `payload` is
        the action's arguments, kept on the decision and any approval
        so the owner can see exactly what was asked. Never raises:
        every outcome is a Decision."""
        role = role_for_requester(state, requester_id)
        payload = dict(payload or {})
        if not self.usage.allow(requester_id):
            return self._record(requester_id, role, action,
                                Decision(False, role, action,
                                         reason='rate limit exceeded'),
                                payload)
        if action not in ALL_ACTIONS:
            return self._record(requester_id, role, action,
                                Decision(False, role, action,
                                         reason='unknown action'),
                                payload)
        # The approval gates come before the capability check:
        # a gated action is one anyone may ASK for, but only
        # the owner may get without an approval -- a director
        # or worker leaves with a pending approval, not a
        # denial.
        if action in APPROVAL_GATES and role != ROLE_OWNER:
            approval = self._match_approval(requester_id, action)
            if approval is not None:
                return self._record(requester_id, role, action,
                                    Decision(True, role, action,
                                             approval_id=approval.approval_id,
                                             reason='approved by owner'),
                                    payload)
            pending = self._create_approval(requester_id, role, action, payload)
            return self._record(requester_id, role, action,
                                Decision(False, role, action,
                                         requires_approval=True,
                                         approval_id=pending.approval_id,
                                         reason='owner approval required'),
                                payload)
        if action not in CAPABILITIES[role]:
            return self._record(requester_id, role, action,
                                Decision(False, role, action,
                                         reason='role %s may not request %s'
                                         % (role, action)),
                                payload)
        if action in MEDIA_ACTIONS and payload.get('content_class') != 'data':
            return self._record(requester_id, role, action,
                                Decision(False, role, action,
                                         reason='media content must be '
                                               'classified as data'),
                                payload)
        return self._record(requester_id, role, action,
                            Decision(True, role, action), payload)

    def evaluate_key(self, request, raw_key):
        """Decide an ask made with a granted key instead
        of a roster identity. The key names the agent and
        its role, so the capability check runs against the
        key's role -- an agent cannot ask for more through
        a key than the role the owner granted it. The key
        burns its own rate allowance (per key, not per
        agent), and the approval gates and the media
        content-class rule apply exactly as they do for
        roster asks. Never raises: every outcome is a
        Decision."""
        if self.registry is None:
            return self._record(
                'unknown', 'unknown', request.action,
                Decision(False, 'unknown', request.action,
                         reason='key registry not configured'),
                request.payload)
        key = self.registry.verify(raw_key)
        if key is None:
            return self._record(
                'unknown', 'unknown', request.action,
                Decision(False, 'unknown', request.action,
                         reason='invalid, revoked, or '
                                'expired key'),
                request.payload)
        self.usage.set_rate_limit(key.key_id, key.rate_limit)
        if not self.usage.allow(key.key_id):
            return self._record(
                key.agent_id, key.role, request.action,
                Decision(False, key.role, request.action,
                         agent_id=key.agent_id,
                         reason='rate limit exceeded'),
                request.payload)
        if request.action not in ALL_ACTIONS:
            return self._record(
                key.agent_id, key.role, request.action,
                Decision(False, key.role, request.action,
                         agent_id=key.agent_id,
                         reason='unknown action'),
                request.payload)
        # Same gate order as evaluate(): approvals before
        # the capability check, so a director or worker
        # asking for a gated action gets a pending
        # approval, not a flat denial.
        if request.action in APPROVAL_GATES \
                and key.role != ROLE_OWNER:
            approval = self._match_approval(key.agent_id,
                                             request.action)
            if approval is not None:
                return self._record(
                    key.agent_id, key.role, request.action,
                    Decision(True, key.role, request.action,
                             agent_id=key.agent_id,
                             approval_id=approval.approval_id,
                             reason='approved by owner'),
                    request.payload)
            pending = self._create_approval(
                key.agent_id, key.role, request.action,
                request.payload)
            return self._record(
                key.agent_id, key.role, request.action,
                Decision(False, key.role, request.action,
                         agent_id=key.agent_id,
                         requires_approval=True,
                         approval_id=pending.approval_id,
                         reason='owner approval required'),
                request.payload)
        if request.action not in CAPABILITIES[key.role]:
            return self._record(
                key.agent_id, key.role, request.action,
                Decision(False, key.role, request.action,
                         agent_id=key.agent_id,
                         reason='role %s may not request %s'
                                % (key.role, request.action)),
                request.payload)
        if request.action in MEDIA_ACTIONS \
                and request.payload.get('content_class') != 'data':
            return self._record(
                key.agent_id, key.role, request.action,
                Decision(False, key.role, request.action,
                         agent_id=key.agent_id,
                         reason='media content must be '
                                'classified as data'),
                request.payload)
        return self._record(
            key.agent_id, key.role, request.action,
            Decision(True, key.role, request.action,
                     agent_id=key.agent_id),
            request.payload)

    def approve(self, state, owner_requester_id, approval_id):
        """The owner approves a pending request. Only the owner role
        may decide; a director or worker calling this is a no-op that
        returns False. `state` supplies the roster so the owner is
        resolved the same way evaluate() resolves everyone. Returns
        True when a pending approval was approved."""
        if role_for_requester(state, owner_requester_id) != ROLE_OWNER:
            return False
        approval = self._approvals.get(approval_id)
        if approval is None or approval.status != 'pending':
            return False
        approval.status = 'approved'
        approval.decided_by = owner_requester_id
        self._approved.add(approval_id)
        self._persist()
        self.audit.record(AuditEntry(
            self.clock(), owner_requester_id, ROLE_OWNER,
            'approve:%s' % approval_id, True, approval_id=approval_id))
        return True

    def deny_approval(self, state, owner_requester_id, approval_id):
        """The owner rejects a pending request. Same owner-only gate as
        approve(). Returns True when a pending approval was denied."""
        if role_for_requester(state, owner_requester_id) != ROLE_OWNER:
            return False
        approval = self._approvals.get(approval_id)
        if approval is None or approval.status != 'pending':
            return False
        approval.status = 'denied'
        approval.decided_by = owner_requester_id
        self._persist()
        self.audit.record(AuditEntry(
            self.clock(), owner_requester_id, ROLE_OWNER,
            'deny:%s' % approval_id, False, approval_id=approval_id))
        return True

    def _match_approval(self, requester_id, action):
        for approval_id in self._approved:
            approval = self._approvals.get(approval_id)
            if approval is None or approval.status != 'approved':
                continue
            if approval.requester_id == requester_id and approval.action == action:
                return approval
        return None

    def _create_approval(self, requester_id, role, action, payload):
        approval = PendingApproval(
            'apr-%s' % secrets.token_hex(8),
            requester_id, role, action, payload, self.clock())
        self._approvals[approval.approval_id] = approval
        self._persist()
        return approval

    def _record(self, requester_id, role, action, decision, payload=None):
        self.audit.record(AuditEntry(
            self.clock(), requester_id, role, action, decision.allowed,
            requires_approval=decision.requires_approval,
            approval_id=decision.approval_id, reason=decision.reason))
        decision.payload = dict(payload or {})
        return decision


def requires_sandbox(action):
    """True when the action must run inside the sandbox proxy, never
    on the host. The routing layer uses this to pick the sandboxed
    execution path for a worker's tool call."""
    return action in SANDBOXED_ACTIONS
