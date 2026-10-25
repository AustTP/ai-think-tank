"""Load operator-editable skill prompts from the shared skills tree.

Skills are reference documents first. A few stable task playbooks are also
executed prompts, so the executor needs a deterministic read instead of a
semantic library search. The file is cached by path, mtime, and size.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

_LOCK = threading.Lock()
_CACHE: dict = {}

_SKILLS_ENV = os.environ.get('THINK_TANK_SKILLS_DIR')
SKILLS_DIR = (Path(_SKILLS_ENV) if _SKILLS_ENV
              else Path(__file__).resolve().parents[2] / 'library' / 'skills')


def _safe_slug(name):
    slug = str(name or '').strip()
    if not slug or slug.startswith('.') or '/' in slug or '\\' in slug:
        return ''
    return slug


def load_skill(name, fallback='', skills_dir=None):
    """Return one skill file, or fallback when it is missing or unreadable."""
    slug = _safe_slug(name)
    if not slug:
        return fallback
    base = Path(skills_dir) if skills_dir else SKILLS_DIR
    path = base / f'{slug}.md'
    try:
        stat = path.stat()
    except OSError:
        return fallback
    key = str(path)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return fallback
    if not text.strip():
        return fallback
    with _LOCK:
        _CACHE[key] = (stat.st_mtime_ns, stat.st_size, text)
    return text


_PROMPT_SECTIONS = ('key facts', 'policy for this think tank')


def load_skill_prompt(name, fallback='', skills_dir=None):
    """Return only the instruction sections of a skill, not its citations."""
    text = load_skill(name, fallback='', skills_dir=skills_dir)
    if not text:
        return fallback
    parts = []
    keep = False
    for line in text.splitlines():
        if line.startswith('## '):
            heading = line[3:].strip()
            keep = heading.lower() in _PROMPT_SECTIONS
            if keep:
                parts.append(f'## {heading}')
            continue
        if line.startswith('# '):
            continue
        if keep:
            parts.append(line)
    cleaned = '\n'.join(parts).strip()
    return cleaned or fallback


def clear_cache():
    """Drop cached skill text. Used by tests and operator reloads."""
    with _LOCK:
        _CACHE.clear()
