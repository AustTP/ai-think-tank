"""Village-worker mode (2026-11-06).

`serve.py --village=<id>` runs this process as ONE village against its OWN
database (THINK_TANK_DB_URL), reporting to the parent control plane
(THINK_TANK_PARENT_URL) by heartbeat. The operator's architecture:

    parent DB  -- org level: chief of staff, regional VP, bank, governance,
                  the village registry, the operator console.
    village DB -- one per village: that village's agents, teams, tasks,
                  wiki, sim tick. A village process dying never touches
                  another village.

This part installs the village-mode behaviors and NOTHING ELSE when the
main deployment runs (VILLAGE_MODE false: zero effect).

Gates in village mode:
  * the operator console (/login, the player-session surface) and the
    admin endpoints are REFUSED -- the parent is the operator surface;
  * agent-key routes stay open (the village's own workers need them).

Heartbeat (every minute, fire-and-forget): task/agent counts, sim
liveness, and any UNRESOLVED health alerts, posted to the parent's
registry endpoint with the shared THINK_TANK_PARENT_TOKEN. The parent
drops alert lines into the chief of staff's mailbox -- the org intake a
village cannot reach on its own.
"""
import asyncio
import json
import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from serve import VILLAGE_MODE, VILLAGE_ID, PARENT_URL

_HEARTBEAT_INTERVAL_S = 60


def _village_refuse_operator_surface():
    """In village mode the player console and admin endpoints do not exist:
    the parent process is the single operator surface."""
    if not VILLAGE_MODE:
        return

    @app.middleware('http')
    async def _village_gate(request: Request, call_next):
        path = request.url.path
        if path == '/login' or path.startswith('/api/admin') \
                or path.startswith('/api/villages'):
            return JSONResponse(
                {'error': 'this is a village worker; the operator surface '
                          'lives on the parent process'}, status_code=404)
        return await call_next(request)


_village_refuse_operator_surface()


def _village_heartbeat_payload():
    """A compact liveness + pressure snapshot from this village's own DB."""
    try:
        with _db() as conn:
            row = conn.execute(
                'SELECT blob FROM kv_state WHERE id=1').fetchone()
            alerts = conn.execute(
                'SELECT count(*) FROM health_alerts WHERE ts > ?',
                (time.time() - 24 * 3600,)).fetchone()
        state = json.loads(row[0]) if row else {}
        open_tasks = sum(
            1 for t in (state.get('tasks') or {}).values()
            if isinstance(t, dict)
            and t.get('status') not in ('done', 'cancelled', 'abandoned'))
        return {
            'ok': True,
            'villageId': VILLAGE_ID,
            'ts': time.time(),
            'openTasks': open_tasks,
            'queued': len(state.get('workQueue') or []),
            'alerts24h': alerts[0] if alerts else 0,
        }
    except Exception as exc:  # noqa: BLE001 -- a heartbeat must never crash
        return {'ok': False, 'villageId': VILLAGE_ID, 'ts': time.time(),
                'error': str(exc)[:200]}


async def _village_heartbeat_loop():
    import urllib.request
    token = os.environ.get('THINK_TANK_PARENT_TOKEN') or ''
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
        if not PARENT_URL:
            continue
        try:
            body = json.dumps(_village_heartbeat_payload()).encode()
            req = urllib.request.Request(
                f'{PARENT_URL}/api/villages/{VILLAGE_ID}/heartbeat',
                data=body, method='POST',
                headers={'Content-Type': 'application/json',
                         'X-Parent-Token': token})
            await asyncio.to_thread(
                urllib.request.urlopen, req, None, 10)
        except Exception:
            pass  # the parent being down must never take the village down


if VILLAGE_MODE:
    @app.on_event('startup')
    async def _start_village_heartbeat():
        asyncio.get_event_loop().create_task(_village_heartbeat_loop())
