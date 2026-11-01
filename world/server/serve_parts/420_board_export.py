"""Board Markdown export (2026-11-01): GET /api/board.md.

The kanzo idea worth stealing, the version that fits: a one-request dump of
the board as Markdown, so the operator can paste live world state into any
external model session (Claude Code, a scratch chat, a planning doc) without
opening the House UI. Read-only, player-session-guarded, no model calls.

Sections mirror the House board: in progress, awaiting peer review, parked
(parking-lot cards -- never auto-assigned), the next queued cards, recently
completed (with the worker's note), and pending large requests. Values are
newline-flattened so one messy field can't break the Markdown structure.
"""
from __future__ import annotations

import time

from fastapi.responses import PlainTextResponse


def _md_cell(value, cap=120):
    """One Markdown-safe table/bullet cell: no newlines, no unbounded text."""
    text = str(value or '').replace('\r', ' ').replace('\n', ' / ').strip()
    return text[:cap] + ('…' if len(text) > cap else '')


def _who(state, agent_id):
    agent = (state.get('agents') or {}).get(agent_id) or {}
    return agent.get('name') or agent_id or ''


def _task_line(state, task, with_note=False):
    who = _who(state, task.get('assignedTo'))
    room = task.get('room') or ''
    phase = task.get('phase') or ''
    bits = [f"**{_md_cell(task.get('title'))}**"]
    if who:
        bits.append(who)
    if room:
        bits.append(room)
    if task.get('teamId'):
        bits.append(f"team {task.get('teamId')}")
    if phase:
        bits.append(f"phase {phase}")
    if task.get('taskType') and task.get('taskType') != 'code':
        bits.append(task.get('taskType'))
    line = '- ' + ' -- '.join(bits)
    if with_note:
        note = _md_cell(task.get('note'), cap=200)
        if note:
            line += f'\n  > {note}'
    return line


def _board_markdown(state):
    now_ms = int(time.time() * 1000)
    tasks = state.get('tasks') or {}
    queue = state.get('workQueue') or []

    in_flight, review, parked_tasks, done = [], [], [], []
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        st = str(task.get('status') or '')
        if st == 'done':
            done.append(task)
        elif st == 'needs_review':
            review.append(task)
        elif st in ('walking', 'working', 'repairing'):
            in_flight.append(task)
        elif st and st not in ('cancelled', 'abandoned'):
            parked_tasks.append(task)
    parked_queue = [i for i in queue if (i.get('lane') or '') == 'parking-lot']
    queued = [i for i in queue if (i.get('lane') or '') != 'parking-lot'
              and int(i.get('notBefore') or 0) <= now_ms]
    done.sort(key=lambda t: int(t.get('updatedAt') or t.get('createdAt') or 0), reverse=True)

    out = ['# Think tank board', '',
           f'_Live world state as of {time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())} '
           f'-- {len(in_flight)} in progress, {len(review)} awaiting review, '
           f'{len(parked_tasks) + len(parked_queue)} parked, {len(queued)} queued, '
           f'{len(done)} completed._', '']

    out.append(f'## In progress ({len(in_flight)})')
    if in_flight:
        out.extend(_task_line(state, t) for t in in_flight[:40])
    else:
        out.append('_Nothing in progress._')
    out.append('')

    if review:
        out.append(f'## Awaiting peer review ({len(review)})')
        out.extend(_task_line(state, t) for t in review[:20])
        out.append('')

    parked_total = parked_tasks + parked_queue
    if parked_total:
        out.append(f'## Parked ({len(parked_total)}) -- never auto-assigned')
        out.extend(_task_line(state, t) for t in parked_tasks[:20])
        out.extend(f"- **{_md_cell(i.get('title'))}** -- waiting in the parking lot"
                   for i in parked_queue[:20])
        out.append('')

    if queued:
        out.append(f'## Queued ({len(queued)} due now)')
        for i in queued[:25]:
            bits = [f"**{_md_cell(i.get('title'))}**"]
            if i.get('taskType') and i.get('taskType') != 'code':
                bits.append(i.get('taskType'))
            if i.get('phase'):
                bits.append(f"phase {i.get('phase')}")
            if i.get('sequence') is not None:
                bits.append(f"serial #{i.get('sequence')}")
            if i.get('dependsOn'):
                bits.append(f"depends on {i.get('dependsOn')}")
            if i.get('priority'):
                bits.append(f"priority {i.get('priority')}")
            out.append('- ' + ' -- '.join(bits))
        out.append('')

    out.append(f'## Recently completed ({len(done)} total, latest 12)')
    if done:
        out.extend(_task_line(state, t, with_note=True) for t in done[:12])
    else:
        out.append('_Nothing completed yet._')
    out.append('')

    pending = [r for r in (state.get('backlogRequests') or [])
               if isinstance(r, dict) and r.get('status') == 'pending']
    if pending:
        out.append(f'## Pending large requests ({len(pending)})')
        for r in pending[:15]:
            out.append(f"- **{_md_cell(r.get('goal'), cap=140)}**"
                       f" -- filed by {_who(state, r.get('filedBy'))}"
                       + (f" -- team {r.get('teamId')}" if r.get('teamId') else ''))
        out.append('')

    return '\n'.join(out).rstrip() + '\n'


@app.get('/api/board.md')
async def api_board_markdown(request: Request):
    if not _require_player_session(request):
        return PlainTextResponse('player session required', status_code=401)
    state = get_state_from_db() or {}
    return PlainTextResponse(_board_markdown(state), media_type='text/markdown; charset=utf-8')
