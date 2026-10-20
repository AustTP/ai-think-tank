"""Stable review contracts and deterministic checks of cited deliverables."""
import csv
import datetime
import hashlib
import io
import json
import os
import re

BLOCKING_FINDING = re.compile(r'\[(?:SOURCE_VERIFICATION|MISSING_ARTIFACT|FUNCTIONAL_DEFECT|TEST_FAILURE|'
                             r'QUALITY_CHECK|ACCEPTANCE_GAP|MISSING_INPUT|SERVICE_FAILURE)\]')

ACCEPTANCE_PROMPT = (
    'Approval requires EVERY supplied nonempty criterion to be met with inspected evidence. '
    'Include acceptanceChecks in the JSON: [{"criterionId":"C1","status":"met|unmet|unverified",'
    '"evidenceIds":["E1"],"explanation":"what the evidence proves"}]. '
    'Use one entry per criterion, with exact supplied evidence IDs. '
    'Also give completionPercent (0-100) per criterion as an evidence-based estimate of implemented work, '
    'and explain what remains. Use 100 only for met criteria; unverified criteria get 0. '
    'For an unmet criterion, partial credit requires inspected evidence of working portions. '
    'This estimate never changes the acceptance decision. '
    'Any unmet or unverified criterion, known blocking defect, failing test, or missing required artifact '
    'forbids approve. Return send_back with an evidence-anchored repair for a known defect. '
    'Never accept a known failure as an optional limitation, exception, future fix, or scope reduction. '
    'Request approval settings do not waive acceptance criteria or quality checks. '
)


def clean_vote_errors(result, require_acceptance=True):
    assessment = result.get('acceptanceAssessment') or {}
    errors = list(assessment.get('errors') or [])
    if require_acceptance:
        if assessment.get('verified') is not True or not assessment.get('checks'):
            errors.append('Approval lacks verified acceptance criteria.')
        if any(not isinstance(check, dict) or check.get('status') != 'met'
               or not (check.get('evidence') or check.get('evidenceIds'))
               for check in assessment.get('checks') or []):
            errors.append('An acceptance criterion is unmet or unverified.')
    if result.get('ok') is False:
        errors.append('The review or execution failed.')
    if result.get('pipelineOk') is not True:
        errors.append('Quality pipeline did not pass.')
    if result.get('peerRepairStories'):
        errors.append('Required repairs remain unresolved.')
    if any(BLOCKING_FINDING.search(str(finding)) for field in ('peerChecks', 'peerRisks')
           for finding in result.get(field) or []):
        errors.append('The reviewer recorded a blocking finding.')
    diagnostics = result.get('reviewDiagnostics') or {}
    artifact = diagnostics.get('artifactChecks') or {}
    if artifact.get('errors') or artifact.get('passed') is False:
        errors.append('Artifact inspection found failures.')
    if diagnostics.get('citationErrors'):
        errors.append('The final report cites URLs that were never discovered by a tool.')
    if result.get('checklistEscalated') or any(g.get('verdict') != 'meets_requirement'
                                              for g in result.get('checklistGrades') or []):
        errors.append('A checklist requirement failed or remains unverified.')
    return errors


def known_failure(result):
    diagnostics = result.get('reviewDiagnostics') or {}
    artifact = diagnostics.get('artifactChecks') or {}
    return (result.get('ok') is False or result.get('pipelineOk') is False
            or bool(result.get('peerRepairStories')) or bool(artifact.get('errors'))
            or artifact.get('passed') is False or bool(diagnostics.get('citationErrors'))
            or any(check.get('status') == 'unmet'
                   for check in (result.get('acceptanceAssessment') or {}).get('checks') or []
                   if isinstance(check, dict))
            or any(check.get('verdict') == 'fails_requirement' for check in result.get('checklistGrades') or []))


def contract_for(task):
    if isinstance(task.get('reviewContract'), dict):
        contract = dict(task['reviewContract'])
    else:
        contract = {'instructions': str(task.get('instructions') or ''),
                    'acceptanceCriteria': str(task.get('acceptanceCriteria') or ''), 'version': 1}
    instructions = contract.get('instructions') or ''
    # Old coaching was appended before contracts were frozen. A generated
    # failure notice is feedback, never an additional acceptance requirement.
    instructions = re.sub(r"\nYour deliverable '[^\n]*failed the quality pipeline\.[\s\S]*?Address this on your next pass\.", '', instructions)
    for marker in ('\nPeer review of ', '\n\nHuman guidance:', '\nHuman guidance:',
                   '\n\nRepair the original deliverable:', '\nPeer-review findings:'):
        instructions = instructions.split(marker, 1)[0]
    contract['instructions'] = instructions
    return contract


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def artifact_references(document, contract=None):
    """Required/delivered files, excluding explicit descriptions of absent output.

    A limitation is not a delivery claim. Named contractual outputs remain
    mandatory even when the report admits they were not produced.
    """
    pattern = r'[A-Za-z0-9_./-]+\.(?:csv|ipynb)'
    contract = contract or {}
    requirements = str(contract.get('instructions') or '').split('Story:', 1)[-1]
    requirements += '\n' + str(contract.get('acceptanceCriteria') or '')
    paths = re.findall(pattern, requirements)
    absent = re.compile(r'\b(?:unable|could not|not (?:created|saved|produced|generated|available)|'
                        r'never (?:created|saved|produced)|planned|proposed|intended|would (?:contain|include))\b', re.I)
    for line in document.splitlines():
        for path in re.findall(pattern, line):
            missing = re.search(r'\bmissing(?: artifact| file| document)?\s*:?\s*[`\'"]?' + re.escape(path), line, re.I)
            missing = missing or re.search(re.escape(path) + r'[`\'"]?(?:\s+(?:is|was|remains))?\s+(?:missing|unavailable)\b', line, re.I)
            if not absent.search(line.replace(path, '<artifact>')) and not missing:
                paths.append(path)
    return list(dict.fromkeys(paths))[:12]


def _date(value):
    try:
        number = float(value)
        if number > 1e12: number /= 1000
        return datetime.datetime.fromtimestamp(number, datetime.timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        try:
            parsed = datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return parsed.astimezone(datetime.timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return None


def artifact_checks(document, contract, resolve, execution=None):
    """Check actual Library files, never infer verification from prose claims."""
    instructions = contract['instructions']
    # Parent objectives describe context, not this story's deliverables.
    scope = instructions.split('Story:', 1)[-1]
    requirements = scope + '\n' + contract['acceptanceCriteria']
    mentions_history = bool(re.search(r'historical data|historical dataset|historical.*candles', requirements, re.I))
    documentation = bool(re.search(r'documentation|endpoints|API reference|parameters', requirements, re.I))
    delivers_dataset = bool(re.search(
        r'(?:save|download|fetch|collect|provide|produce|deliver|extract)\s+(?:a\s+|the\s+|actual\s+|dated\s+|validated\s+)*(?:historical|OHLCV|candles|dataset)',
        requirements, re.I))
    needs_data = mentions_history and (not documentation or delivers_dataset)
    needs_notebook = bool(re.search(r'notebook|\.ipynb', requirements, re.I))
    paths = artifact_references(document, contract)
    checks, errors = [], []
    has_csv = has_notebook = False
    verified_notebook = False
    for path in paths:
        target = resolve(path)
        if not target or not os.path.isfile(target):
            errors.append('Cited artifact is missing from Library: ' + path)
            continue
        if os.path.getsize(target) > 20 * 1024 * 1024:
            errors.append('Artifact exceeds the 20 MB review inspection limit: ' + path)
            continue
        try:
            with open(target, encoding='utf-8-sig') as stream: text = stream.read()
            if path.endswith('.csv'):
                rows = list(csv.DictReader(io.StringIO(text)))
                if any(None in row for row in rows):
                    errors.append('CSV has rows with more values than its header; fix quoting or column counts: ' + path)
                    continue
                fields = list(rows[0]) if rows else []
                duplicates = len(rows) - len({json.dumps(r, sort_keys=True) for r in rows})
                missing = sum(any(v is None or str(v).strip() == '' for v in row.values()) for row in rows)
                date_field = next((f for f in fields if f.lower() in ('timestamp','time','date','datetime','start','open_time')), None)
                dates = [_date(r.get(date_field)) for r in rows] if date_field else []
                dates = [d for d in dates if d]
                check = {'path':path, 'kind':'csv', 'rows':len(rows), 'duplicateRows':duplicates,
                         'rowsWithMissingValues':missing, 'dateColumn':date_field,
                         'start':min(dates).isoformat() if dates else None,
                         'end':max(dates).isoformat() if dates else None}
                checks.append(check)
                if not rows: errors.append('Dataset is empty: ' + path)
                if duplicates: errors.append('Dataset contains duplicate rows: ' + path)
                if missing: errors.append('Dataset contains missing values: ' + path)
                # Endpoint inventories and strategy tables are not candle data.
                historical_candidate = bool(date_field or {'open','high','low','close'}.issubset({f.lower() for f in fields}))
                if needs_data and historical_candidate:
                    if not dates or len(dates) != len(rows): errors.append('Historical dataset needs a valid date on every row: ' + path)
                    required_years = set(re.findall(r'\b20\d{2}\b', requirements))
                    observed_years = {str(d.year) for d in dates}
                    absent = required_years - observed_years
                    if absent: errors.append('Dataset lacks requested years ' + ', '.join(sorted(absent)) + ': ' + path)
                    ordered = sorted(set(d.timestamp() for d in dates))
                    deltas = sorted(b-a for a,b in zip(ordered,ordered[1:]))
                    interval = deltas[len(deltas)//2] if deltas else None
                    check['inferredIntervalSeconds'] = interval
                    check['intervalGaps'] = sum(d > interval * 1.5 for d in deltas) if interval else 0
                    if check['intervalGaps']: errors.append('Historical dataset contains interval gaps requiring explanation: ' + path)
                has_csv = has_csv or bool(historical_candidate and rows and dates and len(dates) == len(rows))
            else:
                notebook = json.loads(text)
                code = [c for c in notebook.get('cells',[]) if c.get('cell_type') == 'code' and c.get('source')]
                cell_errors = sum(any(o.get('output_type') == 'error' for o in c.get('outputs',[])) for c in code)
                unexecuted = sum(c.get('execution_count') is None for c in code)
                checks.append({'path':path, 'kind':'notebook', 'codeCells':len(code),
                               'errorCells':cell_errors,'unexecutedCells':unexecuted,
                               'limitation':'Saved cell outputs are not independent proof of an execution.'})
                if not code or cell_errors or unexecuted: errors.append('Notebook must contain executed code cells with no errors: ' + path)
                has_notebook = bool(code and not cell_errors and not unexecuted) or has_notebook
                verified_notebook = verified_notebook or bool(isinstance(execution, dict)
                    and execution.get('verified') is True and execution.get('exitCode') == 0
                    and execution.get('artifactSha256') == digest(text))
        except (ValueError, UnicodeError, OSError, TypeError) as exc:
            errors.append('Cannot inspect artifact ' + path + ': ' + str(exc)[:160])
    if needs_data and not has_csv: errors.append('No saved, dated historical CSV dataset was available for inspection.')
    if needs_notebook and not has_notebook: errors.append('No executed notebook artifact was available for inspection.')
    if needs_notebook and not verified_notebook:
        errors.append('Notebook requires recorded execution evidence, not only saved cell outputs.')
    return {'checks':checks, 'errors':errors, 'passed':not errors}
