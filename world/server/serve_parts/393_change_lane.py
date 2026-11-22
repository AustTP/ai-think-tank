# Change lane (operator, 2026-11-22): the tank's own merge-request pipeline.
# A worker finishes a deliverable=code card (codePublished), the lane turns
# that workspace's HEAD into a tank/<taskId> branch, opens a PR, waits for
# CI, then routes by risk class:
#   docs/tests-only  -> auto-merge on green CI (approval record as a PR
#                       comment; GitHub forbids the author approving formally)
#   code             -> Jev quorum review of the diff. Confident approve
#                       merges; confident deny closes the PR with the reason;
#                       unsure escalates through _director_second_opinion,
#                       which ends in an owner escalation (the existing
#                       token-gated email link) and leaves the PR open.
# Deliberately NOT in v1: branch protection on main (the tank still lands
# hotfixes by direct push; revisit once this lane is the norm). Rollback of
# any merged change is a revert PR through the same lane.
#
# The worker lane that produced tonight's failed loop stays untouched -- this
# lane only lifts VALIDATED completions (codePublished), it never authors.
# Every gate is fail-closed: any error marks the job failed in action_log and
# the sim keeps running.

import asyncio
import json
import re
import subprocess
import time
import urllib.request
from collections import deque


_CHANGE_LANE_ENABLED = str(
    (_load_env().get('CHANGE_LANE_ENABLED') or '1')).lower() not in ('0', 'false', 'no')
_CHANGE_LANE_MAX_PER_DAY = int(_load_env().get('CHANGE_LANE_MAX_PER_DAY') or 6)
_CHANGE_LANE_CI_TIMEOUT_S = int(_load_env().get('CHANGE_LANE_CI_TIMEOUT_S') or 1800)
# Review resilience (operator direction 2026-11-22): the free model pool
# wobbles; a change request must survive that. Jev retries on HARD failures
# (exception or malformed verdict); a None/unsure verdict is a VALID outcome
# that routes to the director chain unretried. If the review cannot convene
# after all attempts, the PR stays open and the owner is escalated directly.
_CHANGE_LANE_REVIEW_ATTEMPTS = 3
_CHANGE_LANE_RETRY_BACKOFF_S = 60
_CHANGE_LANE_REPO = (_load_env().get('GITHUB_WATCH_REPO') or '').strip()
_CHANGE_LANE_TOKEN = (_load_env().get('GITHUB_TOKEN') or '').strip()

_CHANGE_LANE_QUEUE = deque()
_CHANGE_LANE_ENQ_LOCK = None  # bound lazily; serve_parts share one namespace
_CHANGE_LANE_DAY = ''
_CHANGE_LANE_COUNT = 0

# Paths that classify a diff as docs/tests-only (auto-merge tier).
_CHANGE_LANE_DOCS = ('README.md', 'CHANGELOG.md', 'DESIGN.md',
                     'BUSINESS-PROCESSES.md', 'SECURITY.md', 'CONTRIBUTING.md',
                     'CONSTITUTION.md', '.env.template', 'docs/')
_CHANGE_LANE_TESTS = ('world/tests/', 'evals/')


def _change_lane_scrub(text):
    if _CHANGE_LANE_TOKEN:
        text = text.replace(_CHANGE_LANE_TOKEN, '***')
    return re.sub(r'x-access-token:[^@]+@', 'x-access-token:***@', text or '')


def _change_lane_run(args, timeout=300, cwd=None):
    """One git subprocess inside a sandbox clone. Never raises; the captured
    output is token-scrubbed. Returns (rc, scrubbed_stdout)."""
    try:
        proc = subprocess.run(
            args, capture_output=True, timeout=timeout, cwd=cwd,
            env={'GIT_TERMINAL_PROMPT': '0',
                 'PATH': '/usr/bin:/bin:/usr/local/bin'})
        return proc.returncode, _change_lane_scrub(proc.stdout.decode(errors='replace'))
    except Exception as e:
        return 1, f'git failed: {e}'


def _change_lane_api(method, path, body=None):
    """One GitHub REST call as the tank's own identity. Never raises.
    Returns (status, data) with data=None on transport failure."""
    if not _CHANGE_LANE_REPO or not _CHANGE_LANE_TOKEN:
        return 0, None
    url = f'https://api.github.com/repos/{_CHANGE_LANE_REPO}{path}'
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': f'token {_CHANGE_LANE_TOKEN}',
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'think-tank-change-lane',
        'Content-Type': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b'{}')
        except Exception:
            payload = None
        return e.code, payload
    except Exception:
        return 0, None


def change_lane_classify(paths):
    """docs-tier iff EVERY changed path is a doc or test file."""
    if not paths:
        return 'code'
    for path in paths:
        if path.startswith(_CHANGE_LANE_TESTS) or path in _CHANGE_LANE_DOCS \
                or any(path.startswith(p) for p in _CHANGE_LANE_DOCS):
            continue
        return 'code'
    return 'docs'


def _change_lane_sandbox_for(task):
    task_id = task.get('taskId') or task.get('id') or ''
    digits = re.sub(r'\D', '', str(task_id))[-7:]
    base = SANDBOXES_DIR  # absolute (00_header), CWD-independent
    candidates = []
    if task.get('sandboxId'):
        candidates.append(os.path.join(base, task['sandboxId']))
    for prefix in ('story-task-', 'spike-task-', 'story-', 'task-'):
        if digits:
            candidates.append(os.path.join(base, f'{prefix}{digits}'))
    for cand in candidates:
        rc, _ = _change_lane_run(['git', 'rev-parse', '--git-dir'],
                                 timeout=15, cwd=cand)
        if rc == 0:
            return cand
    return None


def _change_lane_url():
    if not _CHANGE_LANE_REPO or not _CHANGE_LANE_TOKEN:
        return None
    return (f'https://x-access-token:{_CHANGE_LANE_TOKEN}'
            f'@github.com/{_CHANGE_LANE_REPO}.git')


def _change_lane_on_task_completed(task, agent_id):
    global _CHANGE_LANE_ENQ_LOCK
    if not _CHANGE_LANE_ENABLED:
        return None
    task = task or {}
    if task.get('deliverable') != 'code' or not task.get('codePublished'):
        return None
    if not _CHANGE_LANE_REPO or not _CHANGE_LANE_TOKEN:
        log_action(agent_id or 'system', 'change_lane_skipped',
                   {'taskId': task.get('id'),
                    'reason': 'GITHUB_WATCH_REPO or GITHUB_TOKEN unset'},
                   authorized=True)
        return None
    job = {'taskId': task.get('id') or task.get('taskId'),
           'title': (task.get('title') or '')[:160],
           'agent': agent_id or 'unknown',
           'queuedAt': time.time()}
    if _CHANGE_LANE_ENQ_LOCK is None:
        import threading
        _CHANGE_LANE_ENQ_LOCK = threading.Lock()
    with _CHANGE_LANE_ENQ_LOCK:
        _CHANGE_LANE_QUEUE.append(job)
    log_action(agent_id or 'system', 'change_lane_enqueued',
               {'taskId': job['taskId'], 'title': job['title']}, authorized=True)
    return job


def _change_lane_wait_ci(head_sha):
    """Poll Actions runs for the head sha until every run completed, then
    green iff all conclusions are success. Returns 'green' | 'red' | 'timeout'
    | 'none'."""
    deadline = time.time() + _CHANGE_LANE_CI_TIMEOUT_S
    time.sleep(60)  # workflow registration lag
    while time.time() < deadline:
        status, data = _change_lane_api(
            'GET', f'/actions/runs?head_sha={head_sha}&per_page=20')
        if status == 200 and isinstance(data, dict):
            runs = data.get('workflow_runs') or []
            if runs:
                if all((r.get('status') == 'completed') for r in runs):
                    if all((r.get('conclusion') == 'success') for r in runs):
                        return 'green'
                    return 'red'
            else:
                if time.time() > deadline - _CHANGE_LANE_CI_TIMEOUT_S + 300:
                    return 'none'
        time.sleep(30)
    return 'timeout'


def _change_lane_pr_comment(number, text):
    return _change_lane_api('POST', f'/issues/{number}/comments',
                            {'body': text[:8000]})


def _change_lane_merge(number, note, job):
    status, data = _change_lane_api(
        'PUT', f'/pulls/{number}/merge',
        {'merge_method': 'squash',
         'commit_title': f"{job['title']} (tank lane, {job['taskId']})"})
    if status == 200:
        _change_lane_pr_comment(number, note[:4000])
        log_action('system', 'change_lane_merged',
                   {'taskId': job['taskId'], 'pr': number}, authorized=True)
        return True
    log_action('system', 'change_lane_failed',
               {'taskId': job['taskId'], 'pr': number, 'stage': 'merge',
                'status': status,
                'reason': str((data or {}).get('message'))[:200]},
               authorized=True)
    return False


def _change_lane_review_with_retry(job, instructions, criteria):
    """Jev quorum with lane-level resilience. Retries only on hard failures
    (exception, malformed verdict); a None/unsure verdict is a VALID outcome
    and routes to the director chain without retry. Returns
    (decision, confidence, available)."""
    for attempt in range(_CHANGE_LANE_REVIEW_ATTEMPTS):
        if attempt:
            time.sleep(_CHANGE_LANE_RETRY_BACKOFF_S)
        try:
            raw, confidence, _cost = _jev_quorum_choice_sync(
                instructions, criteria)
        except Exception as e:  # noqa: BLE001
            log_action('system', 'change_lane_review_retry',
                       {**job, 'attempt': attempt + 1,
                        'reason': str(e)[:160]}, authorized=True)
            continue
        if raw in ('approve', 'deny', 'unsure') or raw is None:
            return raw, confidence, True
        log_action('system', 'change_lane_review_retry',
                   {**job, 'attempt': attempt + 1,
                    'reason': f'malformed verdict {str(raw)[:60]}'},
                   authorized=True)
    return None, None, False


def _change_lane_process(job):
    """Failure containment boundary: a lane job dies with a log row, never a
    traceback that could take a caller with it (the async loop's to_thread
    catches too, but direct callers -- a future admin re-run endpoint --
    deserve the same guarantee)."""
    try:
        _change_lane_process_inner(job)
    except Exception as e:  # noqa: BLE001
        try:
            log_action('system', 'change_lane_failed',
                       {**(job or {}), 'stage': 'worker',
                        'reason': str(e)[:200]}, authorized=True)
        except Exception:
            pass


def _change_lane_process_inner(job):
    global _CHANGE_LANE_DAY, _CHANGE_LANE_COUNT
    today = time.strftime('%Y-%m-%d')
    if _CHANGE_LANE_DAY != today:
        _CHANGE_LANE_DAY = today
        _CHANGE_LANE_COUNT = 0
    if _CHANGE_LANE_COUNT >= _CHANGE_LANE_MAX_PER_DAY:
        log_action('system', 'change_lane_skipped',
                   {**job, 'reason': f'daily cap {_CHANGE_LANE_MAX_PER_DAY}'},
                   authorized=True)
        return
    sandbox = _change_lane_sandbox_for(job)
    if not sandbox:
        log_action('system', 'change_lane_skipped',
                   {**job, 'reason': 'no preserved sandbox workspace found'},
                   authorized=True)
        return
    url = _change_lane_url()
    rc, out = _change_lane_run(['git', '-C', sandbox, 'fetch', '--no-tags',
                                url, 'main:refs/remotes/origin/main'], timeout=120)
    if rc != 0:
        log_action('system', 'change_lane_failed',
                   {**job, 'stage': 'fetch', 'reason': out[-300:]}, authorized=True)
        return
    rc, out = _change_lane_run(['git', '-C', sandbox, 'rev-list', '--count',
                                'origin/main..HEAD'])
    ahead = int(out.strip() or '0') if rc == 0 else -1
    if ahead <= 0:
        log_action('system', 'change_lane_skipped',
                   {**job, 'reason': f'no commits ahead of main ({ahead})'},
                   authorized=True)
        return
    rc_head, head_sha = _change_lane_run(['git', '-C', sandbox, 'rev-parse', 'HEAD'])
    head_sha = head_sha.strip()
    branch = f"tank/{re.sub(r'[^A-Za-z0-9._-]', '-', str(job['taskId']))}"
    rc, out = _change_lane_run(['git', '-C', sandbox, 'push', url,
                                f'HEAD:refs/heads/{branch}'], timeout=300)
    if rc != 0:
        log_action('system', 'change_lane_failed',
                   {**job, 'stage': 'push', 'reason': out[-300:]}, authorized=True)
        return
    rc, diff_text = _change_lane_run(
        ['git', '-C', sandbox, 'diff', '--name-only', 'origin/main...HEAD'])
    paths = [p for p in diff_text.splitlines() if p.strip()]
    risk = change_lane_classify(paths)
    status, pr = _change_lane_api('POST', '/pulls', {
        'title': f"{job['title']} [{job['taskId']}]",
        'head': branch, 'base': 'main',
        'body': (f'Change lane PR from card {job["taskId"]} (agent '
                 f'{job["agent"]}).\n\nDeliverable validated by the coding lane '
                 f'(codePublished). Risk class: **{risk}** '
                 f'({len(paths)} files). CI gates this branch; merge policy '
                 'docs/tests auto-merge, code = Jev + director review.')})
    if status not in (200, 201):
        log_action('system', 'change_lane_failed',
                   {**job, 'stage': 'pr', 'status': status,
                    'reason': str((pr or {}).get('message'))[:200]},
                   authorized=True)
        return
    number = pr.get('number')
    _CHANGE_LANE_COUNT += 1
    log_action('system', 'change_lane_pr_opened',
               {'taskId': job['taskId'], 'pr': number, 'risk': risk,
                'files': len(paths)}, authorized=True)
    verdict_ci = _change_lane_wait_ci(head_sha)
    if verdict_ci != 'green':
        _change_lane_pr_comment(
            number, f'CI verdict: **{verdict_ci}** -- merge withheld by lane '
                    f'policy; the card owner can push a fix.')
        log_action('system', 'change_lane_withheld',
                   {'taskId': job['taskId'], 'pr': number, 'ci': verdict_ci},
                   authorized=True)
        return
    if risk == 'docs':
        _change_lane_merge(number, 'AUTO-MERGE (docs/tests tier): green CI, '
                                   'docs/tests-only diff, lane policy tier 1. '
                                   'Approval record per operator direction '
                                   '2026-11-22.', job)
        return
    # Code tier: Jev quorum over the diff excerpt.
    rc, full_diff = _change_lane_run(
        ['git', '-C', sandbox, 'diff', 'origin/main...HEAD'], timeout=120)
    excerpt = full_diff[:6000]
    instructions = (
        'You are reviewing a pull request the tank produced against its own '
        'repository. Judge whether the change is correct, safe, scoped to its '
        f'stated purpose, and free of secrets. Stated purpose: {job["title"]}. '
        'The diff follows.\n\n' + excerpt)
    criteria = {'approve': 'Correct, safe, scoped; no secrets or credential '
                           'material in the diff.',
                'deny': 'Wrong, unsafe, out of scope, leaks secrets, or '
                        'gaming the tests.'}
    decision, confidence, review_available = \
        _change_lane_review_with_retry(job, instructions, criteria)
    if not review_available:
        _change_lane_pr_comment(
            number, 'The review could not convene after '
                    f'{_CHANGE_LANE_REVIEW_ATTEMPTS} attempts (model pool '
                    'unavailable). The PR stays OPEN; an owner decision has '
                    'been requested through the escalation flow.')
        log_action('system', 'change_lane_review_unavailable',
                   {'taskId': job['taskId'], 'pr': number}, authorized=True)
        create_escalation(
            'change lane review unavailable',
            f'PR #{number} (card {job["taskId"]}) could not be reviewed: the '
            f'Jev quorum could not convene after '
            f'{_CHANGE_LANE_REVIEW_ATTEMPTS} attempts. CI is green; the '
            'change waits on a human or a recovered model pool. The PR '
            'stays open.',
            what_checked='Jev quorum: 3 attempts with 60s backoff, all hard-failed',
            look_first=f'PR #{number}',
            agent_id=job.get('agent') or 'system')
        return
    threshold = _effective_safety_confidence()
    if decision == 'approve' and confidence >= threshold:
        _change_lane_merge(number, f'Jev approved at confidence '
                                   f'{confidence:.2f} (>= {threshold:.2f}); '
                                   f'CI green. Code-tier lane policy.', job)
        return
    if decision == 'deny' and confidence >= threshold:
        _change_lane_api('PATCH', f'/pulls/{number}', {'state': 'closed'})
        _change_lane_pr_comment(number, f'Jev denied at confidence '
                                        f'{confidence:.2f}: the change does not '
                                        f'meet the review bar. PR closed by '
                                        f'lane policy.')
        log_action('system', 'change_lane_closed',
                   {'taskId': job['taskId'], 'pr': number,
                    'jevConfidence': confidence}, authorized=True)
        return
    # Unsure: the director chain owns it; its exhaustion path already
    # escalates to the owner (token-gated link) and returns False.
    ok = _director_second_opinion(
        'system', 'pr_merge', 'pull request',
        f'PR #{number} on {_CHANGE_LANE_REPO}: {job["title"]}',
        f'Tank-authored change; Jev was unsure ({decision} at '
        f'{confidence:.2f}). Diff files: {", ".join(paths[:12])}',
        float(confidence or 0.0))
    if ok:
        _change_lane_merge(number, 'Director chain approved after Jev was '
                                   'unsure; CI green. Code-tier lane policy.',
                           job)
        return
    _change_lane_pr_comment(
        number, 'Neither Jev nor the director chain reached a confident '
                'verdict; an owner decision has been requested through the '
                'escalation flow. The PR stays OPEN pending that decision.')
    log_action('system', 'change_lane_escalated',
               {'taskId': job['taskId'], 'pr': number}, authorized=True)


async def _change_lane_loop():
    while True:
        try:
            job = None
            if _CHANGE_LANE_ENQ_LOCK is not None:
                with _CHANGE_LANE_ENQ_LOCK:
                    if _CHANGE_LANE_QUEUE:
                        job = _CHANGE_LANE_QUEUE.popleft()
            if job:
                try:
                    await asyncio.to_thread(_change_lane_process, job)
                except Exception as e:  # noqa: BLE001
                    log_action('system', 'change_lane_failed',
                               {**job, 'stage': 'worker',
                                'reason': str(e)[:200]}, authorized=True)
        except Exception as e:  # noqa: BLE001
            print(f'[change-lane] loop error: {e}', flush=True)
        await asyncio.sleep(20)
