"""Stable review contracts and deterministic checks of cited deliverables."""
import csv
import datetime
import hashlib
import io
import json
import os
import re


def contract_for(task):
    if isinstance(task.get('reviewContract'), dict):
        return dict(task['reviewContract'])
    instructions = str(task.get('instructions') or '')
    for marker in ('\nPeer review of ', '\n\nHuman guidance:', '\nHuman guidance:',
                   '\n\nRepair the original deliverable:', '\nPeer-review findings:'):
        instructions = instructions.split(marker, 1)[0]
    contract = {'instructions': instructions, 'acceptanceCriteria': str(task.get('acceptanceCriteria') or ''),
                'version': 1}
    return contract


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


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
    paths = list(dict.fromkeys(re.findall(r'[A-Za-z0-9_./-]+\.(?:csv|ipynb)', document)))[:12]
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
