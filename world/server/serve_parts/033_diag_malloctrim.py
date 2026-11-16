# Periodic glibc heap trim: THINK_TANK_MALLOC_TRIM_S=<seconds> runs
# malloc_trim(0) on a daemon thread. Landed 2026-11-16 for the VM tank's
# RSS sawtooth: every state op parses the full ~400 MB merged state, and
# that churn arrives as ~23k sub-mmap-threshold blobs, so freed pages stay
# in glibc arenas and RSS crept toward the cgroup kill even with the
# static MALLOC_MMAP_THRESHOLD_ guard (which only catches allocations
# >= 128 KB). malloc_trim returns free arena pages to the OS; the RSS
# then oscillates around one transient's worth instead of ratcheting.
# No-op unless the env var is set (0/absent = off); tests never set it.
import ctypes as _ctypes
import os as _os
import threading as _threading
import time as _time

_TRIM_S = int(_os.environ.get('THINK_TANK_MALLOC_TRIM_S') or 0)
if _TRIM_S > 0:
    def _malloc_trim_loop():
        try:
            libc = _ctypes.CDLL('libc.so.6')
        except OSError:
            return  # not glibc (e.g. macOS); trimming is a no-op there
        while True:
            _time.sleep(_TRIM_S)
            try:
                freed = libc.malloc_trim(0)
            except Exception:
                freed = 0
            if freed:
                print('[malloc-trim] free arena pages returned to the OS',
                      flush=True)

    _threading.Thread(target=_malloc_trim_loop, daemon=True,
                      name='malloc-trim').start()
