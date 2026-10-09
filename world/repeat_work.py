"""Stop unchanged failures across fresh subtask IDs without losing evidence."""
import hashlib
import json
import os
import re


def evidence_digest(state, parent):
    import serve, work_reuse, code_workspaces
    h = hashlib.sha256()
    path = serve._safe_library_path(parent.get('libraryPath')) if parent.get('libraryPath') else None
    if path and os.path.isfile(path):
        with open(path, 'rb') as stream:
            h.update(stream.read(200000))
    workspace = os.path.join(serve.SANDBOXES_DIR, code_workspaces.sandbox_for(state, parent))
    h.update(str(work_reuse.tree_digest(workspace) or 'no-code').encode())
    return h.hexdigest()


def record(state, task, result, now):
    import story_tracking
    parent = story_tracking.root(state, task)
    if not parent.get('id') or parent.get('status') in ('done', 'cancelled', 'abandoned'):
        return False
    failure = str(result.get('peerSummary') or result.get('note') or 'Failed acceptance check')
    normalized = re.sub(r'\btask-\d+\b|\b\d{10,}\b|:\d+:\d+:', '<id>', failure.lower())
    normalized = ' '.join(normalized.split())[:2500]
    plan = (parent.get('interventionHistory') or [{}])[-1].get('guidance') or ''
    missing = re.findall(r'Cited artifact is missing from Library: ([A-Za-z0-9_./-]+)', failure)
    if missing:
        import serve
        proof = []
        for name in sorted(set(missing)):
            path = serve._safe_library_path(name)
            proof.append((name, os.path.isfile(path) if path else False))
        evidence = json.dumps(proof)
    else:
        evidence = evidence_digest(state, parent)
    signature = hashlib.sha256(json.dumps([normalized, evidence, plan]).encode()).hexdigest()
    records = parent.setdefault('repeatWorkFailures', [])
    attempt = str((parent.get('_peerGate') or {}).get('enteredMs') or task.get('id')) if task.get('taskType') in ('review','qa') else str(task.get('id'))
    if any(r['signature'] == signature and r['attempt'] == attempt for r in records):
        return False
    records.append({'signature':signature, 'attempt':attempt, 'at':now, 'taskId':task.get('id'), 'failure':failure[:2000]})
    parent['repeatWorkFailures'] = records[-30:]
    matches = sum(r['signature'] == signature for r in records)
    if matches < 2:
        return False
    parent.update(status='blocked', manualAttention=True, blockedReason='unchanged_failure',
                  failNote='The same failure recurred with unchanged evidence and repair plan. Supply a new input, change the plan, or accept the known failure. Latest: ' + failure[:1800])
    parent['_peerGate'] = dict(parent.get('_peerGate') or {})
    parent['_peerGate']['escalated'] = True
    parent['repeatWorkStop'] = {'signature':signature, 'at':now, 'attempts':matches}
    return True
