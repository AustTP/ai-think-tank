"""Evidence-based completion write-ups, persisted before email delivery."""
import time
from datetime import datetime, timezone
from reporting_standard import SOURCE_URL


def is_repair_task(task):
    """Internal follow-ups report through their original story, not separately."""
    return bool(task and (any(task.get(k) for k in ('reviewOf', 'investigationOf', 'recoveryOf'))
        or task.get('taskType') == 'code_preflight'
        or str(task.get('title') or '').lower().startswith(('fix:', 'repair:', 'code check:'))))


def queue(state, task):
    if (not task or task.get('status') != 'done' or task.get('completionWriteup')
            or is_repair_task(task)
            or any(task.get(k) for k in ('reviewOf', 'investigationOf', 'recoveryOf',
                                         'shadowDoneAt', 'distill', 'skillReview'))):
        return None
    import sim, serve
    tid = task.get('id')
    if not tid:
        return None
    title = str(task.get('title') or tid)
    gate = task.get('_peerGate') or {}
    notes = str(task.get('note') or 'No implementation summary was recorded.')[:5000]
    artifacts = [str(task[k]) for k in ('libraryPath', 'validatedArtifactPath', 'artifactPath') if task.get(k)]
    repairs = [t for t in (state.get('tasks') or {}).values() if t.get('reviewOf') == tid
               and t.get('status') == 'done' and t.get('taskType') not in ('review', 'qa')]
    if repairs:
        notes += '\n\nCompleted repair/validation records:\n' + '\n'.join(
            str(t.get('title') or t.get('id')) + ': ' + str(t.get('note') or 'No work summary recorded.')
            for t in sorted(repairs, key=lambda t: t.get('completedAt') or 0)[-3:])[:5000]
        artifacts += [str(t['libraryPath']) for t in repairs if t.get('libraryPath')]
    evidence = []
    for review in task.get('peerReviews') or []:
        if not isinstance(review, dict):
            continue
        assessment = review.get('acceptanceAssessment') or {}
        if not isinstance(assessment, dict):
            continue
        if review.get('verdict') == 'clean' and assessment.get('verified') is True:
            for check in assessment.get('checks') or []:
                evidence.append(str(check.get('criterion') or check.get('criterionId')) + ': '
                                + str(check.get('status')) + '; ' + '; '.join(map(str, check.get('evidence') or [])))
    validation = ('Independent acceptance evidence recorded; approvals: ' + str(gate.get('approvals', 0))
                  if gate.get('acceptanceVerified') is True else
                  'Independent acceptance evidence is not recorded on this task; recorded done status alone is not proof of acceptance.')
    body = ('# Completed work: ' + title + '\nAuthor: ' + str(task.get('originalAssignedTo') or task.get('assignedTo') or 'Not recorded')
            + '\nCompleted (UTC): ' + (datetime.fromtimestamp(task['completedAt']/1000, timezone.utc).isoformat()
                                       if task.get('completedAt') else 'Not recorded')
            + '\n\n## Executive summary\n'
            + 'Task ' + tid + ' is recorded complete. ' + validation
            + '\n\n## Scope\n' + str(task.get('acceptanceCriteria') or 'No acceptance criteria recorded.')[:4000]
            + '\n\n## Work completed\n' + notes
            + '\n\n## Validation and method\n' + ('\n'.join(evidence)[:7000] or validation)
            + '\n\n## Saved results\n' + ('\n'.join(artifacts) or 'No saved artifact path is recorded; inspect the task workspace before relying on its output.')
            + '\n\n## Delivery metrics\nElapsed seconds: ' + str(round((task['completedAt']-task['createdAt'])/1000,1)
                if task.get('completedAt') and task.get('createdAt') else 'Not recorded')
            + '\nStory model spend (USD): ' + str(task.get('storySpendUsd', 'Not recorded'))
            + '\nRepair attempts: ' + str((task.get('recoveryLedger') or {}).get('attempts', 0))
            + '\n\n## Conclusion and next steps\n'
            + 'Use the recorded results for dependent work. Missing evidence and artifact references remain limitations; this write-up does not re-approve historical work.'
            + '\n\n## Sources\nPersisted task ' + tid + ', its completion record and independent review evidence. Format: ' + SOURCE_URL)
    body = serve._redact_secrets(body)
    entry = sim._queue_player_email(state, 'story_done:' + tid,
        '[AI Think Tank] Completed work: ' + title[:80], body)
    if not entry:
        return None
    entry.update(completionTaskId=tid, emailOnly=True)
    task['completionWriteup'] = {'body': body, 'queuedAt': int(time.time()*1000), 'emailStatus': 'queued'}
    return entry
