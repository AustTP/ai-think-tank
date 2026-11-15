# Optional allocation diagnostics: THINK_TANK_TRACEDUMP=<path> starts
# tracemalloc at boot; SIGUSR1 writes the top retained-allocation sites to
# that path. Landed 2026-11-15 to find the VM tank's OOM balloon (RSS
# climbed ~2 GB/min to a 23 GB kill and py-spy CPU samples could not say
# WHAT retained the memory). No-op unless the env var is set; tests never
# set it.
import os as _os
import signal as _signal
import tracemalloc as _tracemalloc

_TRACE_DUMP_PATH = _os.environ.get('THINK_TANK_TRACEDUMP')
if _TRACE_DUMP_PATH:
    _tracemalloc.start(5)

    def _trace_dump(_signum, _frame):
        try:
            snap = _tracemalloc.take_snapshot()
            tops = snap.statistics('lineno')[:25]
            with open(_TRACE_DUMP_PATH, 'w') as f:
                f.write(f'pid={_os.getpid()} sites={len(tops)}\n')
                for stat in tops:
                    f.write(f'{stat.size / 1048576:10.1f} MB '
                            f'{stat.count:>9} blocks  {stat.traceback}\n')
        except Exception:
            pass

    _signal.signal(_signal.SIGUSR1, _trace_dump)
