"""Judge calibration: join deliverable grades to downstream outcomes.

Three commitments from the operator's 2026-10-29 review of the Gamma /
Norona agent-harness interview (library/pending_review/
youtube-agent-harnesses-st61aKW8P5A.txt):

1. Grade -> outcome cohort tracking. Every grade decision in the decision
   tape is joined to the graded task's later life (a quality_gate_reject or
   task_review_requeued naming the task after the grade landed). A judge is
   only a measured instrument once its calls are checked against what
   actually happened next.
2. The north-star metric: the held-up rate -- the share of graded
   deliverables that never needed rework afterwards. This is the tank's
   owner-accepted-without-rework rate, the hard-to-fake number the
   interview argues every product needs.
3. A labeled dataset. The tape already stores prompt, choice and
   confidence; with outcomes attached it is the training set for a
   specialized judge model. The reflexive-grade failure that the
   evidence-rich prompt fixed is the symptom; this ledger is the measuring
   stick that will say whether the cure worked and, later, whether a
   distilled judge does better.

Rework signals v1 are deliberately strict -- only process-recorded events
count (quality_gate_reject, task_review_requeued). Follow-on "Fix: ..."
tasks whose titles reference the graded task are recorded as evidence but
NOT counted as rework: title-text matching is too loose to rate a judge on,
and a lenient signal would corrupt the north star it feeds.
"""
import json
import re
import time

# How long after a grade a rework event still counts against it.
_GRADE_FOLLOWUP_WINDOW_S = 14 * 86400
# A grade younger than this has not had time to prove itself either way.
_GRADE_MATURITY_S = 7 * 86400
# Grade prompts name the deliverable in prose; structured `_taskRef` tape
# metadata (added with this ledger) is authoritative, the regex is the
# historical fallback. The last match wins: later refs in a prompt are the
# deliverable's own id, earlier ones tend to be cross-references.
_TASK_REF_RE = re.compile(r'(?:task|spike-task)-\d+')
# `details LIKE '%ref%'` is only a prefilter: task-123 must not match
# task-1234. The boundary check rejects digit-adjacent collisions.
_ADVERSE_ACTIONS = ('quality_gate_reject', 'task_review_requeued')
# The deliverable-grade family: `kind='grade'` also tapes peer-REVIEW
# decisions ('actionable', 'allow', 'file_bug'), which judge a REVIEW, not a
# deliverable, and whose prompts mention task ids for other reasons. Only
# the 0-10 score buckets feed the judge-calibration ledger.
_SCORE_BUCKETS = frozenset({'0', '2', '4', '6', '8', '10'})


def _ensure_grade_outcomes():
    with _db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS grade_outcomes (
            tape_id INTEGER PRIMARY KEY,
            task_ref TEXT,
            grade_ts REAL NOT NULL,
            grade TEXT,
            confidence REAL,
            outcome TEXT NOT NULL,
            evidence TEXT,
            computed_at REAL NOT NULL
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_grade_outcomes_outcome '
                     'ON grade_outcomes(outcome)')


def _grade_task_ref(prompt, meta_raw):
    """The task a grade decision was about: structured `_taskRef` tape
    metadata when present, else the last task-id-shaped token in the prompt."""
    try:
        meta = json.loads(meta_raw) if meta_raw else None
    except Exception:  # noqa: BLE001 -- malformed metadata falls back to regex
        meta = None
    if isinstance(meta, dict):
        ref = meta.get('taskRef') or meta.get('_taskRef')
        if ref:
            return str(ref)
    hits = _TASK_REF_RE.findall(prompt or '')
    return hits[-1] if hits else None


def _ref_in_details(ref, details):
    return bool(re.search(r'(?<!\d)' + re.escape(ref) + r'(?!\d)', details or ''))


def _judge_outcome_reconcile(days=30):
    """Classify grade decisions in the window against downstream outcomes.
    Idempotent per tape row: UPSERT, because outcomes mature over time (a
    'held' can flip to 'rework' when a late reject lands, never back)."""
    cutoff = time.time() - max(1, int(days)) * 86400
    mature_before = time.time() - _GRADE_MATURITY_S
    _ensure_grade_outcomes()
    scanned = 0
    with _db() as conn:
        rows = conn.execute(
            'SELECT id, ts, prompt, choice, confidence, meta FROM decision_tape '
            "WHERE kind = 'grade' AND ok = 1 AND ts >= ?", (cutoff,)).fetchall()
        for tape_id, ts, prompt, choice, confidence, meta_raw in rows:
            if str(choice) not in _SCORE_BUCKETS:
                continue
            ref = _grade_task_ref(prompt, meta_raw)
            if not ref:
                continue
            scanned += 1
            outcome, evidence = 'held', None
            if ts > mature_before:
                outcome = 'pending'
            else:
                adverse = [row for row in conn.execute(
                    'SELECT id, action, details FROM action_log '
                    "WHERE action IN (?, ?) AND ts >= ? AND ts <= ? "
                    "AND details LIKE ?",
                    (*_ADVERSE_ACTIONS, ts, ts + _GRADE_FOLLOWUP_WINDOW_S,
                     f'%{ref}%')).fetchall()
                    if _ref_in_details(ref, row[2])]
                if adverse:
                    outcome = 'rework'
                    evidence = json.dumps(
                        [{'logId': i, 'action': a} for i, a, _d in adverse][:8])
            conn.execute(
                'INSERT INTO grade_outcomes (tape_id, task_ref, grade_ts, grade, '
                'confidence, outcome, evidence, computed_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(tape_id) DO UPDATE SET outcome = excluded.outcome, '
                'evidence = excluded.evidence, computed_at = excluded.computed_at',
                (tape_id, ref, ts, choice, confidence, outcome, evidence,
                 time.time()))
        # Self-healing: rows ledgered before the score-bucket filter existed
        # (review decisions mis-joined by regex) must not rate the judge.
        conn.execute(
            'DELETE FROM grade_outcomes WHERE grade IS NULL OR grade NOT IN '
            "('0', '2', '4', '6', '8', '10')")
    return scanned


def _grade_score(grade):
    try:
        return float(grade)
    except (TypeError, ValueError):
        return None


def _judge_calibration_report(days=30, now=None):
    """The judge-calibration report. `holdRate` is the north star: of graded
    deliverables old enough to have proven themselves, the share that never
    needed rework. Per-grade tables expose reflexive grading -- a '0' bucket
    whose decisions mostly held up, or a '10' bucket that keeps coming back,
    is an overconfident judge, not a bad workforce."""
    now = time.time() if now is None else now
    scanned = _judge_outcome_reconcile(days)
    cutoff = now - max(1, int(days)) * 86400
    with _db() as conn:
        rows = conn.execute(
            'SELECT g.grade, g.confidence, g.outcome FROM grade_outcomes g '
            'JOIN decision_tape t ON t.id = g.tape_id '
            "WHERE t.kind = 'grade' AND t.ok = 1 AND g.grade_ts >= ? "
            "AND g.outcome IN ('held', 'rework')", (cutoff,)).fetchall()
        pending = conn.execute(
            'SELECT COUNT(*) FROM grade_outcomes g '
            'JOIN decision_tape t ON t.id = g.tape_id '
            "WHERE t.kind = 'grade' AND t.ok = 1 AND g.grade_ts >= ? "
            "AND g.outcome = 'pending'", (cutoff,)).fetchone()[0]
    by_choice = {}
    for grade, confidence, outcome in rows:
        slot = by_choice.setdefault(str(grade), {'n': 0, 'held': 0, 'rework': 0,
                                                 'conf_sum': 0.0})
        slot['n'] += 1
        slot['held' if outcome == 'held' else 'rework'] += 1
        slot['conf_sum'] += float(confidence or 0.0)

    def _order(key):
        score = _grade_score(key)
        return (score if score is not None else -1.0, key)

    choices = []
    for grade, slot in sorted(by_choice.items(), key=lambda kv: _order(kv[0])):
        choices.append({
            'grade': grade, 'n': slot['n'], 'held': slot['held'],
            'rework': slot['rework'],
            'holdRate': round(slot['held'] / slot['n'], 4) if slot['n'] else None,
            'meanConfidence': round(slot['conf_sum'] / slot['n'], 4),
        })
    held = sum(1 for _g, _c, o in rows if o == 'held')
    overconfident = sum(
        1 for grade, confidence, outcome in rows
        if (confidence or 0) >= 0.95 and (
            (outcome == 'held' and (_grade_score(grade) or 0) <= 2) or
            (outcome == 'rework' and (_grade_score(grade) or 10) >= 8)))
    return {
        'windowDays': int(days),
        'decisions': scanned,
        'labeled': len(rows),
        'pending': pending,
        'holdRate': round(held / len(rows), 4) if rows else None,
        'byGrade': choices,
        'overconfident': overconfident,
        'datasetRows': len(rows),
        'note': ('holdRate is the owner-accepted-without-rework north star; '
                 'pending rows are grades too recent to classify '
                 f'(< {_GRADE_MATURITY_S // 86400}d maturity).'),
    }


def _judge_calibration_export(days=30, cap=5000):
    """The labeled training set: one JSON object per graded decision with its
    downstream outcome. Bounded; the prompt is included so the export is
    directly fine-tunable later."""
    cutoff = time.time() - max(1, int(days)) * 86400
    _ensure_grade_outcomes()
    lines = []
    with _db() as conn:
        rows = conn.execute(
            'SELECT g.tape_id, g.task_ref, g.grade_ts, g.grade, g.confidence, '
            'g.outcome, t.prompt, t.criteria FROM grade_outcomes g '
            'JOIN decision_tape t ON t.id = g.tape_id '
            "WHERE t.kind = 'grade' AND t.ok = 1 AND g.grade_ts >= ? "
            "AND g.outcome IN ('held', 'rework') "
            'ORDER BY g.grade_ts LIMIT ?', (cutoff, int(cap))).fetchall()
    for tape_id, ref, ts, grade, confidence, outcome, prompt, criteria in rows:
        lines.append(json.dumps({
            'tapeId': tape_id, 'taskRef': ref, 'gradeTs': ts,
            'grade': grade, 'confidence': confidence, 'outcome': outcome,
            'prompt': prompt, 'criteria': criteria,
        }, ensure_ascii=False))
    return '\n'.join(lines)


@app.get('/api/judge/calibration')
async def judge_calibration_endpoint(request: Request):
    # Operator-gated: judge quality ratings could be gamed by an agent
    # reading the calibration pressure and grading to fit.
    if not _require_player_or_admin(request):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        days = int(request.query_params.get('days') or 30)
    except (TypeError, ValueError):
        return JSONResponse({'error': 'days must be an integer'}, status_code=400)
    days = max(1, min(180, days))
    if request.query_params.get('export'):
        return PlainTextResponse(
            _judge_calibration_export(days), media_type='application/x-ndjson')
    return JSONResponse(_judge_calibration_report(days))
