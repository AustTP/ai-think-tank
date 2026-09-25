# read-only. python3 health.py
import sqlite3, json, time, collections
s = json.loads(sqlite3.connect('village.db').execute(
    'select blob from kv_state where id=1').fetchone()[0])
t = s.get('tasks') or {}
inflight = [v for v in t.values() if v.get('status') not in ('done',)]
titles = collections.Counter(v.get('title') for v in inflight)
print('last tick   :', time.strftime('%m-%d %H:%M', time.gmtime(s['sim']['lastTickEpochS'])), '| tick', s['sim']['tick'])
print('statuses    :', collections.Counter(v.get('status') for v in t.values()))
print('queue depth :', len(s.get('workQueue') or []))
print('top title   :', titles.most_common(1),
      f'= {100*titles.most_common(1)[0][1]//max(1,len(inflight))}% of in-flight' if inflight else '')
print('roles       :', collections.Counter((v.get('role') or 'seed') for v in (s.get('agents') or {}).values()))
print('pendingOnb  :', len(s.get('_pendingOnboard') or {}))
