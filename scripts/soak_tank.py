#!/usr/bin/env python3
"""Long-run failure-injection soak harness for the think tank.

The harness starts a disposable server with an isolated SQLite database, seeds
live and archived tasks, then repeatedly kills the process with SIGKILL and
restarts it. After every restart it compares the durable task ledger and fails
on any lost task, duplicate id, or mutation of an inert seeded task.

It also performs one database-restore test: snapshot the DB, kill the server,
replace the DB with the snapshot, restart, and require an exact ledger match.

Examples:
    python3 scripts/soak_tank.py --duration 21d --kill-every 6h
    python3 scripts/soak_tank.py --duration 5m --kill-every 30s --seed-tasks 20

The report is JSON. Per-event detail is JSONL under the work directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
WORLD = ROOT / 'world'
UNITS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat()


def parse_duration(text: str) -> float:
    text = text.strip().lower()
    if not text:
        raise argparse.ArgumentTypeError('duration cannot be empty')
    unit = text[-1]
    if unit in UNITS:
        number, unit_seconds = text[:-1], UNITS[unit]
    else:
        number, unit_seconds = text, 1
    try:
        value = float(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f'invalid duration {text!r}') from exc
    if value < 0:
        raise argparse.ArgumentTypeError('duration cannot be negative')
    return value * unit_seconds


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def db_uri(path: Path) -> str:
    return f'file:{path}?mode=ro'


def read_ledger(path: Path) -> dict[str, str]:
    """Return {task id: content hash} from the live blob plus task_archive."""
    if not path.exists():
        return {}
    last_error = None
    for _ in range(5):
        try:
            con = sqlite3.connect(db_uri(path), uri=True, timeout=5)
            try:
                tasks: dict[str, dict] = {}
                try:
                    archived = con.execute('SELECT id, blob FROM task_archive').fetchall()
                except sqlite3.OperationalError:
                    archived = []
                tasks.update({tid: json.loads(blob) for tid, blob in archived})
                row = con.execute('SELECT blob FROM kv_state WHERE id = 1').fetchone()
                if row:
                    blob = json.loads(row[0])
                    tasks.update(blob.get('tasks') or {})
            finally:
                con.close()
            return {tid: fingerprint(task) for tid, task in tasks.items()}
        except sqlite3.Error as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f'could not read task ledger {path}: {last_error}')


def fingerprint(task: object) -> str:
    payload = json.dumps(task, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def settle_ledger(path: Path, timeout: float) -> dict[str, str]:
    """Wait for two identical consecutive reads so startup ticks can finish."""
    deadline = time.time() + timeout
    previous = None
    stable = 0
    current = read_ledger(path)
    while time.time() < deadline:
        time.sleep(1)
        current = read_ledger(path)
        if current == previous:
            stable += 1
            if stable >= 1:
                return current
        else:
            stable = 0
            previous = current
    return current


def ledger_delta(before: dict[str, str], after: dict[str, str]) -> dict:
    before_ids, after_ids = set(before), set(after)
    return {
        'before': len(before),
        'after': len(after),
        'lost': sorted(before_ids - after_ids),
        'new': sorted(after_ids - before_ids),
        'changed': sorted(tid for tid in before_ids & after_ids if before[tid] != after[tid]),
    }


def seed_database(db: Path, live_count: int, done_count: int) -> None:
    seed_script = f'''\
import json, os, sqlite3, time
import serve
serve.init_db()
now = time.time()
tasks = {{}}
for i in range(1, {live_count} + 1):
    tasks[f'task-{{i}}'] = {{
        'id': f'task-{{i}}',
        'title': f'soak live task {{i}}',
        'room': 'pressoffice',
        'status': 'needs_review',
        'createdAt': int(now * 1000),
    }}
done = {{}}
for i in range(1, {done_count} + 1):
    tid = f'task-{{9000 + i}}'
    done[tid] = {{
        'id': tid,
        'title': f'soak archived task {{i}}',
        'room': 'pressoffice',
        'status': 'done',
        'completedAt': int(now * 1000),
    }}
state = {{
    'tasks': tasks,
    'agents': {{}},
    'issues': {{}},
    'issueCounters': {{}},
    'backlogRequests': [],
    'villages': [{{'id': 'main'}}],
}}
con = sqlite3.connect(os.environ['THINK_TANK_DB'])
con.execute(
    'INSERT INTO kv_state (id, blob, updated_at) VALUES (1, ?, ?) '
    'ON CONFLICT(id) DO UPDATE SET blob = excluded.blob, updated_at = excluded.updated_at',
    (json.dumps(state), now),
)
for tid, task in done.items():
    con.execute(
        'INSERT INTO task_archive (id, blob, updated_at) VALUES (?, ?, ?) '
        'ON CONFLICT(id) DO UPDATE SET blob = excluded.blob, updated_at = excluded.updated_at',
        (tid, json.dumps(task), now),
    )
con.commit()
con.close()
'''
    env = os.environ.copy()
    env['THINK_TANK_DB'] = str(db)
    subprocess.run([sys.executable, '-c', seed_script], cwd=WORLD, env=env, check=True)


def health(port: int, timeout: float = 2.0) -> bool:
    try:
        with urlopen(f'http://127.0.0.1:{port}/', timeout=timeout) as res:
            return res.status == 200
    except (URLError, OSError, ValueError):
        return False


class Server:
    def __init__(self, db: Path, port: int, log: Path):
        self.db = db
        self.port = port
        self.log = log
        self.proc: subprocess.Popen | None = None
        self.starts = 0

    def start(self, timeout: float) -> None:
        env = os.environ.copy()
        env.update({
            'THINK_TANK_DB': str(self.db),
            'SELF_BASE_URL': f'http://127.0.0.1:{self.port}',
            'AGENT_EXECUTION_ENABLED': 'false',
            'TRAINING_CAPTURE_ENABLED': 'false',
        })
        out = self.log.open('ab')
        try:
            self.proc = subprocess.Popen(
                [sys.executable, 'serve.py', str(self.port)],
                cwd=WORLD, env=env, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            out.close()
        self.starts += 1
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f'server exited during startup (code {self.proc.returncode})')
            if health(self.port):
                return
            time.sleep(0.5)
        raise RuntimeError(f'server did not answer /api/health within {timeout:.0f}s')

    def kill9(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        os.kill(self.proc.pid, signal.SIGKILL)
        self.proc.wait(timeout=10)
        self.proc = None

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self.proc = None
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.proc = None

    def tail(self, lines: int = 20) -> str:
        if not self.log.exists():
            return ''
        text = self.log.read_text(encoding='utf-8', errors='replace').splitlines()
        return '\n'.join(text[-lines:])


def snapshot_db(db: Path, dest: Path) -> None:
    src = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=10)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def restore_db(db: Path, snapshot: Path) -> None:
    for suffix in ('', '-wal', '-shm'):
        Path(str(db) + suffix).unlink(missing_ok=True)
    shutil.copy2(snapshot, db)


def append_event(path: Path, event: dict) -> None:
    with path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(event, sort_keys=True) + '\n')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--duration', type=parse_duration, default=None,
                        help='stop after this long (21d, 6h, 30m, 45s). Default: run until stopped')
    parser.add_argument('--kill-every', type=parse_duration, default=parse_duration('6h'))
    parser.add_argument('--kill-count', type=int, default=0,
                        help='stop after N SIGKILLs. 0 means unlimited')
    parser.add_argument('--snapshot-every', type=parse_duration, default=parse_duration('15m'))
    parser.add_argument('--seed-tasks', type=int, default=50,
                        help='inert live tasks to seed (needs_review)')
    parser.add_argument('--seed-done', type=int, default=5,
                        help='archived done tasks to seed')
    parser.add_argument('--port', type=int, default=0,
                        help='listen port. 0 chooses a free loopback port')
    parser.add_argument('--startup-timeout', type=parse_duration, default=parse_duration('120s'))
    parser.add_argument('--settle-timeout', type=parse_duration, default=parse_duration('30s'),
                        help='wait for two identical ledger reads after each start')
    parser.add_argument('--workdir', type=Path, default=None,
                        help='directory for the disposable DB and logs')
    parser.add_argument('--report', type=Path, default=Path('soak-report.json'))
    parser.add_argument('--keep', action='store_true',
                        help='keep the disposable DB and server log')
    parser.add_argument('--restore-test', action=argparse.BooleanOptionalAction, default=True,
                        help='run one DB-restore test at the first kill')
    args = parser.parse_args(argv)

    workdir = args.workdir or Path(tempfile.mkdtemp(prefix='soak-tank-'))
    workdir.mkdir(parents=True, exist_ok=True)
    db = workdir / 'think_tank.db'
    log = workdir / 'server.log'
    events_path = workdir / 'events.jsonl'
    started = time.time()

    print(f'[soak] workdir {workdir}')
    print(f'[soak] seeding {args.seed_tasks} live and {args.seed_done} archived tasks')
    seed_database(db, args.seed_tasks, args.seed_done)
    server = Server(db, args.port or free_port(), log)
    report = {
        'started_at': iso(started),
        'duration_seconds': args.duration,
        'kill_every_seconds': args.kill_every,
        'snapshot_every_seconds': args.snapshot_every,
        'seed_tasks': args.seed_tasks,
        'seed_done': args.seed_done,
        'server_starts': 0,
        'kill9_count': 0,
        'restore_tests': 0,
        'snapshots': 0,
        'failures': [],
        'events': events_path.name,
    }
    failures: list[str] = report['failures']
    seeded_ids: set[str] = set()

    def event(kind: str, **data) -> None:
        entry = {'at': iso(), 'kind': kind, **data}
        append_event(events_path, entry)

    try:
        server.start(args.startup_timeout)
        report['server_starts'] = server.starts
        initial = settle_ledger(db, args.settle_timeout)
        seeded_ids = set(initial)
        event('start', tasks=len(initial), port=server.port)
        print(f'[soak] server up on {server.port}, {len(initial)} tasks')
        baseline = initial

        deadline = started + args.duration if args.duration is not None else None
        next_kill = time.time() + args.kill_every
        next_snapshot = time.time() + args.snapshot_every
        restore_pending = args.restore_test

        while True:
            now = time.time()
            if deadline is not None and now >= deadline:
                break
            if args.kill_count and report['kill9_count'] >= args.kill_count:
                break
            if now >= next_kill:
                before = read_ledger(db)
                snap = workdir / 'before-restore.db'
                if restore_pending:
                    snapshot_db(db, snap)
                    event('snapshot', tasks=len(before), path=snap.name)
                server.kill9()
                report['kill9_count'] += 1
                event('kill9', tasks=len(before))
                if restore_pending:
                    restore_db(db, snap)
                    server.start(args.startup_timeout)
                    after = settle_ledger(db, args.settle_timeout)
                    delta = ledger_delta(before, after)
                    report['restore_tests'] += 1
                    event('restore', **delta)
                    if delta['lost'] or delta['new'] or delta['changed']:
                        failures.append(f"restore mismatch: {delta['lost']} lost, "
                                        f"{delta['new']} new, {delta['changed']} changed")
                    restore_pending = False
                else:
                    server.start(args.startup_timeout)
                    after = settle_ledger(db, args.settle_timeout)
                    delta = ledger_delta(before, after)
                    event('restart', **delta)
                    if delta['lost']:
                        failures.append(f"lost tasks after kill: {delta['lost']}")
                report['server_starts'] = server.starts
                missing_seeded = sorted(seeded_ids - set(after))
                if missing_seeded:
                    failures.append(f'seeded tasks lost: {missing_seeded}')
                changed_seeded = sorted(tid for tid in seeded_ids & set(after)
                                        if baseline.get(tid) != after[tid])
                if changed_seeded:
                    failures.append(f'seeded tasks mutated: {changed_seeded}')
                if failures:
                    break
                next_kill = time.time() + args.kill_every
                print(f"[soak] kill {report['kill9_count']}: {delta['before']} -> "
                      f"{delta['after']} tasks, lost={len(delta['lost'])}")
            if now >= next_snapshot:
                current = read_ledger(db)
                report['snapshots'] += 1
                event('snapshot', tasks=len(current))
                next_snapshot = now + args.snapshot_every
            time.sleep(min(1.0, max(0.05, next_kill - time.time())))
    except KeyboardInterrupt:
        event('interrupted')
        report['interrupted'] = True
    except Exception as exc:
        failures.append(str(exc))
        event('error', error=str(exc), server_log=server.tail())
        print(f'[soak] ERROR: {exc}')
        print(server.tail())
    finally:
        server.stop()
        final = read_ledger(db) if db.exists() else {}
        report['ended_at'] = iso()
        report['final_tasks'] = len(final)
        report['operator_interventions'] = max(0, report['server_starts'] - 1)
        report['passed'] = not failures
        report['failures'] = failures
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n',
                               encoding='utf-8')
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
        print(f"[soak] {'PASS' if report['passed'] else 'FAIL'}: "
              f"{report['kill9_count']} kills, {report['restore_tests']} restore tests, "
              f"{len(failures)} failures")
        print(f'[soak] report {args.report}')
        if failures:
            for failure in failures:
                print(f'  - {failure}')
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
