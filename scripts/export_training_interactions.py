"""Inspect, review and export recorded conversations without fabricating evidence."""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'world'))
import training_capture as capture


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def jsonl(path, rows):
    with path.open('w', encoding='utf-8') as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')


def export(conn, out, fraction=0.1, max_chars=40000):
    out.mkdir(parents=True, exist_ok=True)
    candidates, accepted, skipped = [], [], collections.Counter()
    outcomes = {r['task_id']: json.loads(r['payload']) for r in conn.execute('SELECT * FROM task_outcomes')}
    for row in conn.execute('SELECT * FROM interactions ORDER BY ts,id'):
        payload = json.loads(row['payload'])
        flags = json.loads(row['flags'])
        request, response = payload.get('request') or {}, payload.get('response') or {}
        choices = response.get('choices') or []
        message = (choices[0].get('message') or {}) if choices else {}
        if not row['ok'] or not message or not (message.get('content') or message.get('tool_calls')):
            skipped['failed_or_empty'] += 1
            continue
        messages = request.get('messages')
        if not isinstance(messages, list) or not all(isinstance(m, dict) and m.get('role') for m in messages):
            skipped['invalid_messages'] += 1
            continue
        target = {k: message[k] for k in ('role', 'content', 'tool_calls') if k in message}
        target['role'] = 'assistant'
        conversation = {'messages': messages + [target]}
        if request.get('tools'):
            conversation['tools'] = request['tools']
        # Redact again against credentials currently configured, including keys
        # added after recording. Any altered/omitted example is withheld.
        conversation, extra_flags = capture.sanitize(conversation)
        if '[REDACTED]' in json.dumps(conversation):
            extra_flags.append('existing_redaction')
        flags = sorted(set(flags + extra_flags))
        candidate = {'id': row['id'], 'group_id': row['group_id'], 'agent_id': row['agent_id'],
            'task_id': row['task_id'], 'trace_id': row['trace_id'], 'model': row['model'],
            'provider': row['provider'], 'seed': row['seed'], 'status': row['review_status'],
            'review_note': row['review_note'], 'flags': flags,
            'outcome': outcomes.get(row['task_id']), 'conversation': conversation}
        candidates.append(candidate)
        if flags:
            skipped['redacted_or_omitted'] += 1
        elif row['review_status'] != 'approved':
            skipped['not_approved'] += 1
        elif len(json.dumps(conversation, ensure_ascii=False)) > max_chars:
            skipped['over_character_limit'] += 1
        elif message.get('refusal') or choices[0].get('finish_reason') in ('length', 'error', 'content_filter'):
            skipped['incomplete_or_refused'] += 1
        else:
            accepted.append((candidate, conversation))

    # Keep requests/root stories and shared identical inputs/outputs together.
    parent, seen = {}, {}
    def find(x):
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]
    for row, conversation in accepted:
        group = row['group_id']
        for key in (('input', sha(conversation['messages'][:-1])), ('output', sha(conversation['messages'][-1]))):
            if key in seen:
                a, b = find(group), find(seen[key])
                parent[max(a, b)] = min(a, b)
            seen[key] = group
    groups = sorted({find(row['group_id']) for row, _ in accepted}, key=sha)
    n_eval = min(len(groups)-1, max(1, round(len(groups)*fraction))) if len(groups)>1 else 0
    eval_groups = set(groups[:n_eval])
    splits, manifest, fingerprints = {'train': [], 'eval': []}, [], set()
    for row, conversation in accepted:
        fingerprint = sha(conversation)
        if fingerprint in fingerprints:
            skipped['duplicate'] += 1
            continue
        fingerprints.add(fingerprint)
        split = 'eval' if find(row['group_id']) in eval_groups else 'train'
        splits[split].append(conversation)
        manifest.append({'id': row['id'], 'group_id': row['group_id'], 'split': split, 'sha256': fingerprint})
    jsonl(out / 'candidates.jsonl', candidates)
    jsonl(out / 'manifest.jsonl', manifest)
    for split, rows in splits.items():
        jsonl(out / (split + '.jsonl'), rows)
    summary = {'candidates': len(candidates), 'train': len(splits['train']), 'eval': len(splits['eval']),
               'groups': len(groups), 'skipped': dict(skipped)}
    (out / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path(capture.config().get('TRAINING_CAPTURE_PATH', ROOT / 'datasets/local/interactions/recordings.sqlite3')))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('stats')
    listing = sub.add_parser('list')
    listing.add_argument('--limit', type=int, default=20)
    show = sub.add_parser('show')
    show.add_argument('id')
    review = sub.add_parser('review')
    review.add_argument('id')
    review.add_argument('--status', choices=['approved','rejected','pending'], required=True)
    review.add_argument('--note', required=True)
    output = sub.add_parser('export')
    output.add_argument('--out', type=Path, default=ROOT / 'datasets/local/interactions/export')
    output.add_argument('--eval-fraction', type=float, default=0.1)
    output.add_argument('--max-chars', type=int, default=40000)
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error('Recording database does not exist yet. Enable recording and run a model interaction first.')
    # Review updates are explicit; other commands open SQLite read-only, including WAL.
    conn = sqlite3.connect(args.db if args.command == 'review' else args.db.resolve().as_uri()+'?mode=ro', uri=args.command != 'review')
    conn.row_factory = sqlite3.Row
    try:
        if args.command == 'stats':
            print(json.dumps({'interactions': conn.execute('SELECT COUNT(*) FROM interactions').fetchone()[0],
                'events': conn.execute('SELECT COUNT(*) FROM events').fetchone()[0],
                'reviews': dict(conn.execute('SELECT review_status,COUNT(*) FROM interactions GROUP BY review_status').fetchall())}, indent=2))
        elif args.command == 'list':
            for row in conn.execute('SELECT id,agent_id,task_id,provider,model,ok,review_status FROM interactions ORDER BY ts DESC LIMIT ?', (args.limit,)):
                print(json.dumps(dict(row)))
        elif args.command == 'show':
            row = conn.execute('SELECT * FROM interactions WHERE id=?', (args.id,)).fetchone()
            if row is None:
                parser.error('Unknown interaction ID')
            print(json.dumps(json.loads(row['payload']), indent=2, ensure_ascii=False))
            outcome = conn.execute('SELECT payload FROM task_outcomes WHERE task_id=?', (row['task_id'],)).fetchone()
            if outcome:
                print('Recorded outcome:', outcome['payload'])
        elif args.command == 'review':
            row = conn.execute('SELECT ok,flags FROM interactions WHERE id=?', (args.id,)).fetchone()
            if row is None:
                parser.error('Unknown interaction ID')
            if args.status == 'approved' and (not row['ok'] or json.loads(row['flags'])):
                parser.error('Cannot approve a failed, redacted, or omitted interaction')
            note, _ = capture.sanitize(args.note)
            conn.execute('UPDATE interactions SET review_status=?,review_note=? WHERE id=?', (args.status, note, args.id))
            conn.execute('INSERT INTO events(ts,kind,payload) VALUES (?,?,?)', (__import__('time').time(), 'training_review', json.dumps({'id':args.id,'status':args.status,'note':note})))
            conn.commit()
            print('Review saved.')
        else:
            if not 0 < args.eval_fraction < 1 or args.max_chars < 1:
                parser.error('Invalid evaluation fraction or character limit')
            print(json.dumps(export(conn, args.out, args.eval_fraction, args.max_chars), indent=2))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
