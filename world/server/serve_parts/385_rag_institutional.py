
# ---------------------------------------------------------------------------
# Retrieval over the think tank's institutional memory (RAG, zero-dep).
#
# The tank's context assembly is keyword-shaped: library search is a literal
# substring scan, the working-guide is read wholesale, and nothing surfaces
# SEMANTICALLY related prior work. So a task about rate limiting never sees
# the retrospective about 429 storms unless the words overlap. This part
# builds a small embedding index over the two highest-value corpora -- the
# Library (which already contains the wiki bodies, they are files under
# library/wiki/) and the archived deliverables in task_archive -- and feeds
# retrieved anchors into three consumers: the library search agents already
# call for every task's context, the escalation prompt, and the grader.
#
# Embeddings are hashed sparse vectors: word unigrams plus character 3-grams,
# sublinear tf weighting, L2-normalized, hashed with md5 so the index is
# stable across restarts and CI. No numpy, no model download, no spend. The
# honest limit is lexical: no synonymy. The interface is the upgrade path --
# swapping _rag_embed for a real embedder changes nothing else.
#
# Search NEVER touches kv_state (the hot blob): the index lives in its own
# table and is refreshed incrementally by a bounded background pass, so a
# retrieval is one table scan plus python dot products.
# ---------------------------------------------------------------------------

_RAG_CHAR_NGRAM = 3
_RAG_MAX_DOC_CHARS = 24000
_RAG_MAX_FILE_BYTES = 262144
_RAG_DELIVERABLE_WINDOW = 300
_RAG_MIN_SCORE = 0.12


def _rag_stable_hash(token):
    import hashlib as _hashlib
    return int.from_bytes(
        _hashlib.md5(token.encode('utf-8', errors='replace')).digest()[:4],
        'big')


def _rag_tokens(text):
    """Lowercased word unigrams (len >= 2) plus character 3-grams, as a
    count vector over stable feature ids."""
    import re as _re
    counts = {}
    if not text:
        return counts
    text = str(text).lower()
    for tok in _re.findall(r'[a-z0-9]{2,}', text):
        key = _rag_stable_hash('w:' + tok)
        counts[key] = counts.get(key, 0) + 1
    compact = _re.sub(r'[^a-z0-9]+', ' ', text)
    for i in range(len(compact) - _RAG_CHAR_NGRAM + 1):
        gram = compact[i:i + _RAG_CHAR_NGRAM]
        if gram.strip() != gram or len(gram.strip()) < 2:
            continue
        key = _rag_stable_hash('c:' + gram)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _rag_embed(text, cap=400):
    """Sparse L2-normalized embedding: sublinear tf (1 + log count) over the
    `cap` strongest features."""
    counts = _rag_tokens(text)
    if not counts:
        return {}
    kept = sorted(counts.items(), key=lambda kv: -kv[1])[:cap]
    vec = {k: 1.0 + math.log(c) for k, c in kept}
    norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
    return {k: w / norm for k, w in vec.items()}


# Village scoping (2026-11-01): every indexed doc carries the village it
# belongs to, so one village's institutional memory never surfaces in
# another's retrieval. Derivation follows the same path rules the library
# search already enforces (_library_path_village, 280): villages/ trees are
# their village, the flat legacy tree is 'main', and archived deliverables
# carry their own villageId. Rows with a NULL village (craft shared by
# design, like skill files, or legacy rows not yet backfilled) stay visible
# to every village -- shared knowledge, not another village's memory.


def _rag_ensure_table():
    with _db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS rag_index (
            doc_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            ref TEXT NOT NULL,
            title TEXT,
            room TEXT,
            text TEXT,
            vec TEXT,
            grade REAL,
            outcome TEXT,
            content_hash TEXT,
            updated_at REAL NOT NULL
        )''')
        # Village scoping, added after the table shipped: an existing index
        # migrates in place (NULL = shared/unknown until the pass backfills).
        # Check-then-alter, the memory.py idiom: a swallowed failed ALTER
        # would poison the whole Postgres transaction.
        existing_cols = {r[1] for r in conn.execute(
            'PRAGMA table_info(rag_index)')}
        if 'village_id' not in existing_cols:
            conn.execute('ALTER TABLE rag_index ADD COLUMN village_id TEXT')
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_rag_source ON rag_index (source)')


def _rag_read_file_text(full):
    """Library file content for indexing: text-decodable, bounded, images and
    dotfiles excluded (the same exclusions _library_search_matches keeps --
    one policy, not two)."""
    if os.path.splitext(full)[1].lower() in INGEST_IMAGE_EXTENSIONS:
        return None
    try:
        if os.path.getsize(full) > _RAG_MAX_FILE_BYTES:
            return None
        with open(full, 'r', errors='replace') as f:
            return f.read(_RAG_MAX_DOC_CHARS)
    except OSError:
        return None


def _rag_candidates():
    """The corpus, as (doc_id, source, ref, title, room, village, text)
    tuples: every text file under the Library (wiki bodies included -- they
    are files under library/wiki/) and the most recent archived
    deliverables. Village derivation follows _library_path_village (280),
    the same rules the library search enforces at read time; a deliverable
    carries its own villageId."""
    out = []
    for root, _dirs, filenames in os.walk(LIBRARY_DIR):
        for fn in filenames:
            if fn.startswith('.'):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, LIBRARY_DIR)
            body = _rag_read_file_text(full)
            if not body or not body.strip():
                continue
            out.append(('library:' + rel, 'library', rel,
                        os.path.splitext(fn)[0], '',
                        _library_path_village(rel), body))
    try:
        with _db() as conn:
            rows = conn.execute(
                'SELECT id, blob FROM task_archive '
                'ORDER BY updated_at DESC LIMIT ?',
                (_RAG_DELIVERABLE_WINDOW,)).fetchall()
    except Exception:
        rows = []
    for task_id, blob in rows:
        try:
            task = json.loads(blob)
        except Exception:
            continue
        parts = []
        for field in ('deliverableText', 'instructions', 'note'):
            body = (task.get(field) or '').strip()
            if body:
                parts.append(body)
        text = '\n\n'.join(parts)[:_RAG_MAX_DOC_CHARS]
        if not text:
            continue
        out.append(('deliverable:' + task_id, 'deliverable', task_id,
                    (task.get('title') or '')[:200],
                    (task.get('room') or '')[:40],
                    (task.get('villageId') or 'main'), text))
    return out


def _rag_row_outcome(task_ref):
    """The grade_outcomes verdict for a deliverable's task ref, when the
    calibration ledger has classified it."""
    try:
        with _db() as conn:
            row = conn.execute(
                'SELECT outcome FROM grade_outcomes WHERE task_ref = ? '
                'ORDER BY grade_ts DESC LIMIT 1', (task_ref,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _rag_index_pass(budget=60):
    """Incrementally index stale or missing documents, newest-first, at most
    `budget` per pass. Unchanged content only bumps its timestamp. Legacy
    rows with a NULL village get theirs backfilled without re-embedding.
    Returns {'indexed': n, 'skipped': n, 'pending': n}."""
    _rag_ensure_table()
    now = time.time()
    indexed = skipped = 0
    try:
        with _db() as conn:
            known = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
                'SELECT doc_id, content_hash, updated_at, village_id '
                'FROM rag_index')}
    except Exception:
        known = {}
    pending = []
    for doc_id, source, ref, title, room, village, text in _rag_candidates():
        digest = hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()
        entry = known.get(doc_id)
        if entry and entry[0] == digest:
            if not entry[2]:
                # Legacy row: stamp the village in place (no re-embed).
                try:
                    with _db() as conn:
                        conn.execute('UPDATE rag_index SET village_id = ? '
                                     'WHERE doc_id = ?', (village, doc_id))
                    indexed += 1
                except Exception:
                    pass
            if entry[1] < now - 7 * 86400:
                pending.append((0.0, now, doc_id, source, ref, title, room,
                                village, text, digest))
            skipped += 1
            continue
        pending.append((1.0, 0.0, doc_id, source, ref, title, room, village,
                        text, digest))
    pending.sort(key=lambda item: (-item[0], item[1]))
    done = 0
    for _stale_rank, _age, doc_id, source, ref, title, room, village, text, digest in pending:
        if done >= budget:
            break
        grade = None
        outcome = None
        if source == 'deliverable':
            try:
                with _db() as conn:
                    blob = conn.execute(
                        'SELECT blob FROM task_archive WHERE id = ?',
                        (ref,)).fetchone()
                task = json.loads(blob[0]) if blob else {}
                grade = task.get('grade') if isinstance(task, dict) else None
            except Exception:
                grade = None
            outcome = _rag_row_outcome(ref)
        vec = json.dumps(_rag_embed(text))
        with _db() as conn:
            conn.execute(
                'INSERT INTO rag_index (doc_id, source, ref, title, room, '
                'village_id, text, vec, grade, outcome, content_hash, '
                'updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(doc_id) DO UPDATE SET source = excluded.source, '
                'ref = excluded.ref, title = excluded.title, room = excluded.room, '
                'village_id = excluded.village_id, '
                'text = excluded.text, vec = excluded.vec, grade = excluded.grade, '
                'outcome = excluded.outcome, content_hash = excluded.content_hash, '
                'updated_at = excluded.updated_at',
                (doc_id, source, ref, title, room, village, text, vec, grade,
                 outcome, digest, now))
        done += 1
        indexed += 1
    return {'indexed': indexed, 'skipped': skipped,
            'pending': max(0, len(pending) - done)}


def _rag_search(query, k=5, min_score=_RAG_MIN_SCORE, sources=None,
                village=None):
    """Ranked semantic hits over the index: cosine similarity (the vectors
    are L2-normalized, so a dot product), min_score floor to keep noise out,
    optional source filter. `village` scopes the retrieval the way the wiki
    already scopes reads: docs stamped with another village are excluded;
    NULL-village docs (shared craft, legacy rows) stay visible to all.
    Empty index or empty query -> []. DB-only: no model calls, safe on
    decision paths."""
    qvec = _rag_embed(query, cap=200)
    if not qvec:
        return []
    try:
        with _db() as conn:
            where, args = [], []
            if sources:
                marks = ','.join('?' for _ in sources)
                where.append(f'source IN ({marks})')
                args.extend(sources)
            if village:
                where.append('(village_id IS NULL OR village_id = ?)')
                args.append(village)
            sql = ('SELECT source, ref, title, room, text, vec FROM rag_index'
                   + (f' WHERE {" AND ".join(where)}' if where else ''))
            rows = conn.execute(sql, tuple(args)).fetchall()
    except Exception:
        return []
    hits = []
    for source, ref, title, room, text, vec_raw in rows:
        try:
            # json.dumps stringifies dict keys; the embeddings were ints.
            dvec = {int(k): v for k, v in (json.loads(vec_raw) if vec_raw else {}).items()}
        except Exception:
            continue
        if not dvec:
            continue
        if len(qvec) < len(dvec):
            score = sum(w * dvec.get(key, 0.0) for key, w in qvec.items())
        else:
            score = sum(w * qvec.get(key, 0.0) for key, w in dvec.items())
        if score >= min_score:
            hits.append({'source': source, 'ref': ref, 'title': title,
                         'room': room, 'score': round(score, 4),
                         'snippet': (text or '')[:240]})
    hits.sort(key=lambda h: -h['score'])
    return hits[:k]


def _rag_room_anchors(room, exclude_ref=None, k=2, village=None):
    """Calibration anchors for the grader: recent comparable deliverables in
    the SAME room, with their grade and -- when the outcome ledger has
    classified them -- what happened next. A judge that sees what an 8 looked
    like, and that one apparently-strong deliverable later came back for
    rework, grades against the room's real bar instead of its own reflex.
    `village` keeps the bar the grader sees inside its own village (NULL
    village rows stay visible -- shared craft). Best-effort: [] on any
    failure."""
    try:
        _rag_ensure_table()
        with _db() as conn:
            where = ("source = 'deliverable' AND room = ? AND grade IS NOT NULL")
            args = [room]
            if village:
                where += ' AND (village_id IS NULL OR village_id = ?)'
                args.append(village)
            rows = conn.execute(
                f'SELECT ref, title, grade FROM rag_index WHERE {where} '
                'ORDER BY updated_at DESC LIMIT 12', tuple(args)).fetchall()
    except Exception:
        return []
    anchors = []
    for ref, title, grade in rows:
        if exclude_ref and ref == exclude_ref:
            continue
        # Outcome read LIVE, not from the index row: verdicts mature after
        # indexing (a held deliverable can later flip to rework), and the
        # grader should never quote a stale verdict.
        outcome = _rag_row_outcome(ref)
        verdict = ''
        if outcome == 'held':
            verdict = ' and it held up'
        elif outcome == 'rework':
            verdict = ' and it later came back for rework'
        try:
            grade_text = f'{float(grade):.0f}'
        except (TypeError, ValueError):
            grade_text = '?'
        anchors.append(
            f'Comparable prior deliverable in this room, "{title}" ({ref}), '
            f'was graded {grade_text}/10{verdict}.')
        if len(anchors) >= k:
            break
    return anchors


def _rag_status():
    """Operator visibility: what is indexed, how fresh, is the pass keeping
    up. DB-only."""
    _rag_ensure_table()
    out = {'sources': {}, 'pendingEstimate': None}
    try:
        with _db() as conn:
            for source, n, newest in conn.execute(
                    'SELECT source, COUNT(*), MAX(updated_at) FROM rag_index '
                    'GROUP BY source'):
                out['sources'][source] = {
                    'docs': n,
                    'newestAgeS': round(time.time() - newest, 1) if newest else None}
            out['pendingEstimate'] = max(
                0, len(_rag_candidates()) - sum(
                    s['docs'] for s in out['sources'].values()))
    except Exception:
        pass
    return out
