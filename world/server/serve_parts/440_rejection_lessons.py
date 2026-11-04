"""Rejection lessons: every quality-gate send-back drafts a durable skill so
the same mistake only happens once (pattern taken from an operator-shared
agent-company post, 2026-11-04). The red pipeline already catches failures;
what it lacked was INSTITUTIONAL MEMORY -- 8 retry spirals minted 29 task ids
in one 24h window because each retry rediscovered the same root cause.

Mechanical, no model calls: the gate's failure note already names the root
cause, so the lesson is composed deterministically and dropped into
library/skills/lesson-<slug>.md (the flat-markdown dir agents load by slug).
Dedup is a settings-row index (slug -> ts) with a 30-day window, so a
recurring class of failure keeps ONE file instead of spiraling. The author's
feedback buffer gets a pointer to the lesson, which lands in their next
dispatch perception -- that is what closes the loop."""
import re
import time

_LESSON_WINDOW_S = 30 * 86400
_LESSON_INDEX_SETTING = 'rejection_lesson_index'
_LESSON_MAX_FILES = 60


def _lesson_slug(note, title):
    """Deterministic slug for one failure class: the leading tool/error token
    of the gate note when it has one (flake8-F401, mypy, bandit, pytest),
    else a normalized fragment of the note itself."""
    raw = (note or '').strip() or (title or '').strip() or 'unspecified'
    m = re.match(r'\s*([A-Za-z]+[a-z]*)\b', raw)
    lead = (m.group(1).lower() if m else '')
    code = re.search(r'\b([A-Z]\d{3}|E\d{3}|W\d{3}|F\d{3})\b', raw)
    parts = [p for p in (lead, code.group(1).lower() if code else '') if p]
    base = '-'.join(parts) if parts else raw[:40]
    slug = re.sub(r'[^a-z0-9-]+', '-', base.lower()).strip('-')[:48]
    return f'lesson-{slug or "unspecified"}'


def _lesson_index():
    raw = _get_setting(_LESSON_INDEX_SETTING, '') or ''
    out = {}
    for entry in raw.split(','):
        if not entry.strip():
            continue
        bits = entry.split('@')
        if len(bits) == 2:
            try:
                out[bits[0]] = float(bits[1])
            except ValueError:
                continue
    return out


def _save_lesson_index(index):
    _set_setting(_LESSON_INDEX_SETTING, ','.join(
        f'{slug}@{ts}' for slug, ts in sorted(index.items())))


def _capture_rejection_lesson(state, task, note, escalated):
    """Draft one deduplicated lesson skill for a quality-gate send-back.
    Fire-and-forget: any failure here must never break the send-back path."""
    try:
        note = (note or '').strip()
        title = (task.get('title') or '').strip()
        author = task.get('assignedTo') or ''
        if not note and not title:
            return
        slug = _lesson_slug(note, title)
        now = time.time()
        index = _lesson_index()
        if slug in index and (now - index[slug]) < _LESSON_WINDOW_S:
            index[slug] = now  # recurring: keep one file, refresh the window
            _save_lesson_index(index)
            return
        index[slug] = now
        # Cap total lesson files: prune the oldest past the budget.
        while len(index) > _LESSON_MAX_FILES:
            oldest = min(index, key=lambda s: index[s])
            del index[oldest]
            try:
                (SKILLS_DIR / f'{oldest}.md').unlink()
            except OSError:
                pass
        _save_lesson_index(index)
        path = _lessons_dir() / f'{slug}.md'
        day = time.strftime('%Y-%m-%d', time.localtime(now))
        task_id = task.get('id') or ''
        tool = (re.match(r'\s*([A-Za-z]+)', note).group(1).lower()
                if re.match(r'\s*([A-Za-z]+)', note) else 'the quality gate')
        path.write_text(
            f'# Lesson: {slug[len("lesson-"):].replace("-", " ")}\n\n'
            f'## What went wrong\n'
            f'The quality gate sent back work on {day}'
            f'{f" (task {task_id})" if task_id else ""}: {note[:300]}\n\n'
            f'## Key facts\n'
            f'- Failing check: {tool}\n'
            f'- Gate note: {note[:200]}\n'
            f'- This class of failure was recorded {len(index)} times total.\n\n'
            f'## Policy for this think tank\n'
            f'- Run `{tool}` (or the equivalent check) on your own work BEFORE '
            f'marking the card done -- the pipeline is fail-closed and this '
            f'exact failure has been seen before.\n',
            encoding='utf-8')
        if author:
            try:
                _append_feedback(
                    state, author,
                    f"A lesson was recorded from a rejected deliverable: skill "
                    f"'{_LESSONS_SUBDIR}/{slug}'. Read it before you fix "
                    f"'{(title or '')[:80]}' so the same gate failure does not "
                    f'repeat.', source='lesson')
            except Exception:
                pass
    except Exception as e:
        try:
            from serve import log_action
            log_action(None, 'lesson_capture_failed',
                       {'error': str(e)[:160]}, authorized=True)
        except Exception:
            pass


def _lessons_dir():
    from agents.skill_prompts import SKILLS_DIR
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    return SKILLS_DIR