

# ---------------------------------------------------------------------------
# Draft desk (Tier-2, 2026-10-30): the tank PREPARES short local-area posts
# for the operator's personal review; the OPERATOR decides each one from
# Telegram with "post <id>" / "skip <id>". "post" only marks the LEDGER --
# nothing is ever posted anywhere by the tank (the Facebook hard rule: the
# operator's personal account is never touched by the tank). The default
# when nothing clears the quality bar is NO draft at all.
# ---------------------------------------------------------------------------

_DRAFT_COMMAND_RE = re.compile(r'^\s*(drafts?|post|skip)(?:\s+([A-Za-z0-9_.-]+))?\s*$', re.I)
_DRAFT_MAX_CHARS = 280
_DRAFT_MAX_PER_RUN = 2
_DRAFT_LINE_RE = re.compile(r'^DRAFT:\s*(.+)$', re.M)


def _ensure_draft_desk_schema():
    with _db() as conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS draft_desk ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT,'
            'draft_id TEXT UNIQUE,'
            'text TEXT NOT NULL,'
            'status TEXT NOT NULL DEFAULT \'pending\','
            'source TEXT,'
            'created_at REAL NOT NULL,'
            'decided_at REAL,'
            'decided_by TEXT)')


def _draft_desk_create(texts, source):
    """Insert generated drafts as pending rows. Returns the created
    {'draftId', 'text'} dicts. Overlong or empty candidates are refused
    (never silently truncated -- a mangled draft is a bad draft)."""
    _ensure_draft_desk_schema()
    created = []
    with _db() as conn:
        for text in texts:
            text = str(text or '').strip()
            if not text or len(text) > _DRAFT_MAX_CHARS:
                continue
            if len(created) >= _DRAFT_MAX_PER_RUN:
                break
            cur = conn.execute(
                'INSERT INTO draft_desk (draft_id, text, status, source, created_at) '
                'VALUES (NULL, ?, \'pending\', ?, ?)',
                (text, source, time.time()))
            draft_id = f'dft_{cur.lastrowid}'
            conn.execute('UPDATE draft_desk SET draft_id=? WHERE id=?',
                         (draft_id, cur.lastrowid))
            created.append({'draftId': draft_id, 'text': text})
    if created:
        log_action('system', 'draft_desk_generated',
                   {'ids': [c['draftId'] for c in created],
                    'source': source, 'count': len(created)},
                   authorized=True)
    return created


def _draft_desk_pending():
    _ensure_draft_desk_schema()
    with _db() as conn:
        rows = conn.execute(
            "SELECT draft_id, text, created_at FROM draft_desk "
            "WHERE status='pending' ORDER BY id DESC LIMIT 10").fetchall()
    return [{'draftId': r[0], 'text': r[1], 'ageHours':
             round((time.time() - r[2]) / 3600, 1)} for r in rows]


def _draft_desk_decide(draft_id, verb, by):
    """Mark one draft posted or skipped. 'posted' is a LEDGER entry only --
    the operator posts it personally from their own account; the tank never
    touches any social platform. Returns (ok, message)."""
    _ensure_draft_desk_schema()
    draft_id = (draft_id or '').strip()
    with _db() as conn:
        row = conn.execute('SELECT status FROM draft_desk WHERE draft_id=?',
                           (draft_id,)).fetchone()
        if not row:
            return False, f'No draft {draft_id} exists.'
        if row[0] != 'pending':
            return False, f'Draft {draft_id} was already {row[0]}.'
        conn.execute(
            'UPDATE draft_desk SET status=?, decided_at=?, decided_by=? '
            'WHERE draft_id=?', (verb, time.time(), by or 'player', draft_id))
    log_action('player', 'draft_desk_decision',
               {'draftId': draft_id, 'verb': verb,
                'note': 'ledger only -- tank never posts anywhere'},
               authorized=True)
    verdict = 'POSTED' if verb == 'post' else 'SKIPPED'
    return True, f'Draft {draft_id} marked {verdict} (ledger only -- nothing was posted anywhere).'


def _parse_draft_command(text):
    """Return ('drafts'|'draft', None) or ('post'|'skip', draft_id) when
    `text` is a draft-desk owner command, else None."""
    match = _DRAFT_COMMAND_RE.match(text or '')
    if not match:
        return None
    verb = match.group(1).lower()
    target = (match.group(2) or '').strip()
    if verb == 'draft' and not target:
        return ('draft', None)
    if verb == 'drafts' and not target:
        return ('drafts', None)
    if verb in ('post', 'skip') and target:
        return (verb, target)
    return None


_DRAFT_GENERATION_PROMPT = (
    'Draft-desk run: propose ZERO to {max_n} short local-area posts for my personal '
    'review. Area: the Charlotte / Albemarle NC region. '
    'GROUNDING RULE: use your tools first (read_rss_feed for local news, list_services '
    'then api_call for live local data such as events or prices) -- every draft must be '
    'grounded in something you actually read in THIS session, with its source named inline '
    '(feed or service). A draft with no real source is a rejected draft. '
    'QUALITY BAR: numbers over vibes -- a concrete number, date, price, or event beats '
    'generalities; practical value for someone deciding where to go or what things cost. '
    'PREFERENCE: cost/price and practical-living content beats event announcements -- an '
    'event draft is only interesting if it carries a price or value angle (a plain '
    '"this event exists" listing was already skipped by the operator once). '
    'Each draft is ONE message of at most {max_chars} characters, no hashtags, no links '
    'to anything we own, nothing about this think tank. '
    'If nothing clears the bar, output NO draft lines at all and say why in one sentence. '
    'Output format -- drafts, then stop:\nDRAFT: <text>')


async def _draft_desk_generate(state, session_id=None):
    """Run the draft-generation ask and turn its DRAFT: lines into pending
    rows. Returns the reply dict for the operator chat."""
    prompt = _DRAFT_GENERATION_PROMPT.format(
        max_n=_DRAFT_MAX_PER_RUN, max_chars=_DRAFT_MAX_CHARS)
    result = await _ask_core(state, prompt, session_id=session_id,
                             max_tokens=600)
    if result.get('error'):
        return {'error': result['error'], 'status': result.get('status', 500)}
    reply = str(result.get('reply') or '')
    candidates = [m.strip() for m in _DRAFT_LINE_RE.findall(reply)]
    candidates = [c for c in candidates if len(c) <= _DRAFT_MAX_CHARS]
    created = _draft_desk_create(candidates, 'telegram_draft_command')
    if not created:
        return {'reply':
                'No draft cleared the bar this run -- nothing created. '
                '(The desk stays empty by default; ask again later.)'}
    lines = [f"{c['draftId']}: {c['text']}" for c in created]
    lines.append("Reply 'post <id>' to mark one POSTED (ledger only -- you post "
                 "it yourself) or 'skip <id>' to discard it.")
    return {'reply': '\n\n'.join(lines)}


async def _draft_desk_handle_owner_command(state, text, session_id=None):
    """Pre-lane owner-command entry. Returns a reply dict when `text` is a
    draft-desk command (so it is never mistaken for a conversational
    request), else None."""
    parsed = _parse_draft_command(text)
    if not parsed:
        return None
    verb, target = parsed
    if verb == 'draft':
        return await _draft_desk_generate(state, session_id)
    if verb == 'drafts':
        pending = _draft_desk_pending()
        if not pending:
            return {'reply': 'Draft desk is empty -- nothing pending.'}
        lines = [f"{p['draftId']} ({p['ageHours']}h): {p['text']}"
                 for p in pending]
        lines.append("Reply 'post <id>' or 'skip <id>'.")
        return {'reply': '\n'.join(lines)}
    ok, message = _draft_desk_decide(target, verb, 'player')
    return {'reply': message}
