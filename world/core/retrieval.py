"""Small, dependency-free chunking and lexical retrieval for Library reads."""

import re
from collections import Counter

_WORD = re.compile(r"[a-z0-9']+")


def _terms(text):
    return Counter(_WORD.findall((text or '').lower()))


def chunk_text(text, max_chars=1200, overlap=150):
    text = text or ''
    if len(text) <= max_chars:
        return [text] if text else []
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            cut = text.rfind('\n\n', start, end)
            if cut > start + max_chars // 2:
                end = cut
            else:
                cut = text.rfind('\n', start, end)
                if cut > start + max_chars // 2:
                    end = cut
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def rank_chunks(query, chunks, top_k=4):
    q = _terms(query)
    if not q:
        return []
    scored = []
    for i, chunk in enumerate(chunks):
        terms = _terms(chunk)
        score = sum(min(count, terms.get(term, 0)) for term, count in q.items())
        if score:
            scored.append((score, i, chunk))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored[:max(1, top_k)]


def excerpts(text, query, top_k=4, max_chars=1200):
    chunks = chunk_text(text, max_chars=max_chars)
    hits = rank_chunks(query, chunks, top_k=top_k)
    if not hits:
        return ''
    out = []
    for score, i, chunk in hits:
        out.append(f'--- excerpt {i + 1} (match {score}) ---\n{chunk.strip()}')
    return '\n\n'.join(out)
