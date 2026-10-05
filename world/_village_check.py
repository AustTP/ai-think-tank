import sys
import time
import json
sys.path.insert(0, 'world')
import serve  # noqa: E402

now = time.time()

print('=== server process ===')
import subprocess  # noqa: E402
r = subprocess.run(['pgrep', '-f', 'python3 serve.py 8936'], capture_output=True, text=True)
print('alive:', r.returncode == 0, r.stdout.strip())

with serve._db() as conn:
    print('=== tables ===')
    tabs = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    print(tabs)

    print()
    print('=== action_log last 20 min (non-player) ===')
    for ts, a, act, det in conn.execute(
            "SELECT ts, agent_id, action, substr(coalesce(details,''),1,110) FROM action_log "
            "WHERE ts > ? AND agent_id != 'player' ORDER BY ts DESC LIMIT 40", (now - 1200,)):
        print(time.strftime('%H:%M:%S', time.localtime(ts)), a, '|', act, '|', det)

    print()
    print('=== decision_tape last 30 min ===')
    for ts, ok, m, k, ch in conn.execute(
            "SELECT ts, ok, model, coalesce(kind,''), substr(coalesce(choice,''),1,30) FROM decision_tape "
            "WHERE ts > ? ORDER BY ts DESC LIMIT 25", (now - 1800,)):
        print(time.strftime('%H:%M:%S', time.localtime(ts)), 'ok=', ok, k, ch, m)

print()
print('=== health alert rows ===')
with serve._db() as conn:
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(health_alerts)").fetchall()]
        for row in conn.execute("SELECT * FROM health_alerts ORDER BY rowid DESC LIMIT 5"):
            d = dict(zip(cols, row))
            print(json.dumps(d, default=str)[:200])
    except Exception as e:
        print('err', e)