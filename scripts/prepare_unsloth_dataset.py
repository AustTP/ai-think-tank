"""Export local research artifacts for review, then build conversational SFT data.

Uses a read-only SQLite snapshot; never imports or starts the application.
"""
import argparse
import base64
import collections
import hashlib
import json
from pathlib import Path
import re
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
SYSTEM = (
    "Write a research report that addresses the task and acceptance criteria. "
    "Ground factual claims in the supplied evidence and cite its sources. "
    "Distinguish observations from estimates and state limitations. "
    "Do not invent sources, tool executions, verification, or results."
)
SECRET_PATTERNS = [
    re.compile(r"(?<![A-Za-z0-9_])(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]{20,}|sk-(?:proj-|or-v1-)?[A-Za-z0-9_-]{16,})"),
    re.compile(r"(?i)(?:Bearer\s+)[A-Za-z0-9._~-]{12,}"),
    re.compile(r"(?im)(?:api[_-]?key|access[_-]?token|secret|password)\s*[=:]\s*[\"']?[^\s\"',;]{8,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
FAILURE = re.compile(
    r"configuration error|no web search was conducted|no verification was possible|"
    r"no (?:CSV|report|deliverable) (?:file )?was produced|"
    r"unable to|could not|couldn't|UNVERIFIED|placeholder|not completed", re.I
)


def secret_values():
    """Read local env values for redaction without displaying or exporting them."""
    values = []
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            key, sep, value = line.strip().partition('=')
            value = value.strip().strip('\"\'')
            if sep and re.search(r'KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL', key, re.I) and not key.startswith('#') and len(value) >= 8:
                values.append(value)
    return sorted(set(values), key=len, reverse=True)


def redact(text, values):
    for value in values:
        text = text.replace(value, '[REDACTED]')
    for pattern in SECRET_PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    return text


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def write_jsonl(path, rows):
    with path.open('w', encoding='utf-8', newline='\n') as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')


def candidates(db, max_chars):
    values = secret_values()
    skips = collections.Counter()
    # backup() obtains a consistent view including the live WAL, without writes.
    with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(':memory:') as snapshot:
            source.backup(snapshot)
            state = json.loads(snapshot.execute('SELECT blob FROM kv_state WHERE id=1').fetchone()[0])
    tasks = state.get('tasks', {})
    tasks = list(tasks.values()) if isinstance(tasks, dict) else tasks
    rows = []
    library = (ROOT / 'library').resolve()
    for task in tasks:
        if task.get('status') != 'done' or task.get('taskType') not in ('research', 'spike'):
            continue
        relative = task.get('libraryPath')
        if not relative:
            skips['no_artifact_path'] += 1
            continue
        path = (library / relative).resolve()
        if not path.is_relative_to(library) or not path.is_file():
            skips['missing_or_unsafe_artifact'] += 1
            continue
        try:
            response = path.read_text(encoding='utf-8-sig')
        except (UnicodeError, OSError):
            skips['unreadable_or_non_utf8_artifact'] += 1
            continue
        # Remove the materialized author's wrapper, not the report's limitations.
        response = re.sub(r'\A# Spike: [^\n]*\n\s*By: [^\n]*\n\s*', '', response).strip()
        fields = {key: task.get(key) for key in ('title', 'instructions', 'userStory', 'acceptanceCriteria', 'checklist') if task.get(key)}
        prompt = json.dumps(fields, ensure_ascii=False, indent=2)
        if not response or len(prompt) + len(response) > max_chars:
            skips['empty_or_over_character_limit'] += 1
            continue
        prompt, response = redact(prompt, values), redact(response, values)
        flags = ['reconstructed_prompt', 'source_evidence_not_logged', 'not_human_verified']
        if FAILURE.search(response):
            flags.append('possible_failure_or_unverified_claims')
        if '[REDACTED]' in prompt + response:
            flags.append('credentials_redacted')
        if task.get('playerAcceptance') or task.get('failNote') or task.get('_peerRejectedBy'):
            flags.append('task_has_exception_or_failure_metadata')
        rows.append({
            'id': task['id'],
            'group_id': str(task.get('requestId') or task.get('rootStoryId') or task['id']),
            'source_path': relative,
            'task_type': task.get('taskType'),
            'flags': flags,
            'status': 'pending',
            'instruction': prompt,
            'evidence': '',
            'response': response,
            'review_notes': '',
        })
    return rows, dict(skips)


def build(review_path, out, eval_fraction, max_chars):
    values = secret_values()
    rows = [json.loads(line) for line in review_path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    accepted, skipped = [], collections.Counter()
    for row in rows:
        if row.get('status') != 'approved':
            skipped['not_approved'] += 1
            continue
        if not all(isinstance(row.get(k), str) and row[k].strip() for k in ('instruction', 'evidence', 'response', 'group_id')):
            raise ValueError(f"Approved example {row.get('id')} needs instruction, evidence, response, and group_id")
        messages = [
            {'role': 'system', 'content': SYSTEM},
            {'role': 'user', 'content': 'Task:\n' + row['instruction'] + '\n\nSource evidence:\n' + row['evidence']},
            {'role': 'assistant', 'content': row['response']},
        ]
        for message in messages:
            message['content'] = redact(message['content'], values)
        if any('[REDACTED]' in m['content'] for m in messages):
            skipped['redacted_example_requires_repair'] += 1
            continue
        if sum(len(m['content']) for m in messages) > max_chars:
            skipped['over_character_limit'] += 1
            continue
        accepted.append((row, messages))

    # Union stories/requests and exact duplicate inputs OR outputs before split.
    # Thus duplicate artifacts cannot connect training and evaluation groups.
    parents = {}
    def find(key):
        parents.setdefault(key, key)
        if parents[key] != key:
            parents[key] = find(parents[key])
        return parents[key]
    seen = {}
    for row, messages in accepted:
        group = row['group_id']
        for key in (('input', digest(messages[1]['content'])), ('output', digest(messages[2]['content']))):
            if key in seen:
                a, b = find(group), find(seen[key])
                parents[max(a, b)] = min(a, b)
            seen[key] = group
    groups = sorted({find(row['group_id']) for row, _ in accepted}, key=lambda g: digest('unsloth-v1:' + g))
    n_eval = min(len(groups) - 1, max(1, round(len(groups) * eval_fraction))) if len(groups) > 1 else 0
    eval_groups = set(groups[:n_eval])
    splits = {'train': [], 'eval': []}
    manifest, seen_pairs = [], set()
    for row, messages in accepted:
        fingerprint = digest(json.dumps(messages, sort_keys=True))
        if fingerprint in seen_pairs:
            skipped['exact_duplicate'] += 1
            continue
        seen_pairs.add(fingerprint)
        split = 'eval' if find(row['group_id']) in eval_groups else 'train'
        splits[split].append({'messages': messages})
        manifest.append({'id': row['id'], 'group_id': row['group_id'], 'split': split, 'sha256': fingerprint})
    for split, data in splits.items():
        write_jsonl(out / (split + '.jsonl'), data)
    write_jsonl(out / 'manifest.jsonl', manifest)
    return {'train': len(splits['train']), 'eval': len(splits['eval']), 'groups': len(groups), 'skipped': dict(skipped)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / 'think_tank.db')
    parser.add_argument('--out', type=Path, default=ROOT / 'datasets/local/research')
    parser.add_argument('--reviewed', type=Path, help='Build train/eval from the downloaded review JSONL')
    parser.add_argument('--eval-fraction', type=float, default=0.1)
    parser.add_argument('--max-chars', type=int, default=40000, help='Exclude oversized examples; never truncate. This is not a token limit.')
    args = parser.parse_args()
    if not 0 < args.eval_fraction < 1 or args.max_chars < 1:
        parser.error('eval-fraction must be between 0 and 1; max-chars must be positive')
    args.out.mkdir(parents=True, exist_ok=True)
    if args.reviewed:
        summary = build(args.reviewed, args.out, args.eval_fraction, args.max_chars)
    else:
        if (args.out / 'review.html').exists():
            parser.error('review.html already exists; use a new --out to preserve browser review progress')
        rows, skipped = candidates(args.db, args.max_chars)
        write_jsonl(args.out / 'candidates.jsonl', rows)
        template = (ROOT / 'scripts/dataset_review.html').read_text(encoding='utf-8')
        encoded = base64.b64encode(json.dumps(rows, ensure_ascii=False).encode()).decode()
        (args.out / 'review.html').write_text(template.replace('__DATA_BASE64__', encoded), encoding='utf-8')
        summary = {'candidates': len(rows), 'skipped': skipped, 'training_ready': False}
    (args.out / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
