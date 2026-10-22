"""Tool replay and trust-tier metadata.

The Pi Durable review's takeaway applied to this think tank's own surface:
every tool is labelled with whether it is safe to replay and how much trust it
needs. Read-only tools may be repeated. Commit-tier tools need an explicit
per-conversation approval. Unknown tools default to unsafe/commit so a new tool
cannot silently become replayable or low-trust.
"""

_READ_ONLY_TOOLS = frozenset({
    'search_library', 'read_library_file', 'search_web', 'browse_page',
    'github_get_repo', 'github_search_code', 'read_ais_feed', 'read_market_feed',
    'read_live_feed', 'read_rss_feed', 'read_weather', 'read_google_sheet',
    'list_calendar_events', 'search_gmail_messages', 'read_gmail_message',
    'read_google_doc', 'list_services', 'apify_get_dataset_items',
    'db_records', 'state_records', 'supervisor_db_records',
    'supervisor_state_records', 'supervisor_report', 'live_read', 'runbook',
    'read_mailbox', 'read_peer_reviews', 'team_digest', 'team_status',
    'allowlist_status', 'team_overview', 'treasury_status', 'x_trending_topics',
    'search_linkedin_posts', 'self_edit_status', 'selfedit_recheck',
    'selfedit_read', 'selfedit_validate',
})
_PREPARE_TOOLS = frozenset({
    'create_gmail_draft', 'create_google_doc', 'write_spike_report',
    'write_report', 'draft_email', 'save_note', 'execute_script',
    'download_file', 'mark_mailbox_read',
})
_COMMIT_TOOLS = frozenset({
    'apify_run_actor', 'create_calendar_event', 'append_google_sheet_row',
    'generate_pixel_character', 'generate_agent_avatar', 'generate_image',
    'generate_video', 'assign_task', 'direct_backlog', 'delegate', 'schedules',
    'supervisor_grant_budget', 'propose_api_service', 'request_allowlist',
    'grant_allowlist', 'revoke_allowlist', 'request_capability_handle',
    'adversarial_village', 'local_file', 'app_script', 'send_email',
    'send_sms', 'send_telegram', 'publish_skill', 'send_mail', 'api_call',
    'self_edit', 'schedule_dormant_apply', 'selfedit_fix', 'policy_edit',
    'request_credential_access',
})


def tool_replay_safety(name):
    """'safe' when repeating the call cannot create a second side effect."""
    name = str(name or '')
    if name in _READ_ONLY_TOOLS:
        return 'safe'
    if name.startswith(('read_', 'search_', 'list_', 'get_')):
        return 'safe'
    return 'unsafe'


def tool_trust_tier(name):
    """research | prepare | commit. Unknown tools are commit (fail closed)."""
    name = str(name or '')
    if name in _COMMIT_TOOLS:
        return 'commit'
    if tool_replay_safety(name) == 'safe':
        return 'research'
    if name in _PREPARE_TOOLS:
        return 'prepare'
    return 'commit'


def tool_policy(name):
    return {'replay': tool_replay_safety(name), 'tier': tool_trust_tier(name)}


def is_known_tool(name):
    """True when the name is in the declared surface. Unknown names stay
    unwrapped so their real runtime error reaches the caller instead of being
    swallowed by the idempotency ledger."""
    name = str(name or '')
    return (name in _READ_ONLY_TOOLS or name in _PREPARE_TOOLS
            or name in _COMMIT_TOOLS
            or name.startswith(('read_', 'search_', 'list_', 'get_')))


def task_tool_policy(task):
    """Build the per-conversation allowlist for a task.

    Research and prepare tools are always allowed. A commit tool is allowed
    only when the task carries `allowCommit` or lists it in `approvedTools`.
    Code/review/QA pipeline tasks are trusted to use commit tools because their
    job is to change the repository; research, report, supervisor, and ask
    conversations are not.
    """
    task = task or {}
    approved = {str(t) for t in (task.get('approvedTools') or []) if t}
    trusted_pipeline = task.get('taskType') in ('code', 'review', 'qa')
    allow_all = bool(task.get('allowCommit')) or trusted_pipeline

    def policy(name):
        if tool_trust_tier(name) != 'commit':
            return True
        return allow_all or name in approved

    return policy
