"""Dynamic transport allowances; agent scheduling limits remain soft."""
import threading

_LOCK = threading.Lock()
_OBSERVED = {}


def transport(path, body=None):
    body = body or {}
    if path == '/api/chat':
        estimate = 60 + max(1, int(body.get('max_tokens') or 150)) / 12
    elif path == '/api/pipeline':
        estimate = 30 + 90 * len(body.get('steps') or [])
    else:
        estimate = 60
    with _LOCK:
        return max(estimate, _OBSERVED.get(path, 0) * 2 + 30)


def observe(path, elapsed):
    with _LOCK:
        _OBSERVED[path] = max(float(elapsed), _OBSERVED.get(path, 0) * .9)
