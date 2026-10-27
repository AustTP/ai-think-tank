"""Machine and village resource policy.

The global class limits in ``world/deployment.json`` are the host ceiling. A
village's directors can lower its share and hold classes through
``/api/villages/{id}/resources``. This module is deliberately state-free for
the config and uses an in-process lease table for live usage; the server is one
process, so that is the same scope the Docker commands run in.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOYMENT_PATH = os.path.join(_HERE, 'deployment.json')

DEFAULT_CONFIG = {
    'resources': {
        'classes': {
            'light': {'maxConcurrent': 4, 'cpus': 1.0, 'memoryMb': 512,
                      'pidsLimit': 128, 'timeoutS': 120},
            'browser': {'maxConcurrent': 2, 'cpus': 1.0, 'memoryMb': 1024,
                        'pidsLimit': 256, 'timeoutS': 180},
            'media': {'maxConcurrent': 1, 'cpus': 2.0, 'memoryMb': 2048,
                      'pidsLimit': 128, 'timeoutS': 900},
            'gpu': {'maxConcurrent': 1, 'cpus': 4.0, 'memoryMb': 8192,
                    'pidsLimit': 128, 'timeoutS': 1800},
        },
        'villageDefaults': {'maxConcurrent': 4, 'maxHeavy': 1,
                            'hold': [], 'reason': ''},
    }
}
CLASS_NAMES = ('light', 'browser', 'media', 'gpu')
HEAVY_CLASSES = ('media', 'gpu')
_HEAVY_RE = re.compile(
    r'\b(ffmpeg|blender|maya|houdini|unreal|render(?:ing)?|animation|'
    r'3d|video|audio|nvidia|cuda|torch|tensorflow)\b',
    re.IGNORECASE)
_GPU_RE = re.compile(r'\b(nvidia|cuda|torch|tensorflow)\b', re.IGNORECASE)


def _merge(base, incoming):
    out = copy.deepcopy(base)
    if not isinstance(incoming, dict):
        return out
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_deployment(path=None):
    path = path or DEPLOYMENT_PATH
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return copy.deepcopy(DEFAULT_CONFIG)
    merged = _merge(DEFAULT_CONFIG, data)
    classes = merged.setdefault('resources', {}).setdefault('classes', {})
    for name in CLASS_NAMES:
        cfg = classes.get(name) or {}
        base = DEFAULT_CONFIG['resources']['classes'][name]
        for key, value in base.items():
            if not isinstance(cfg.get(key), (int, float)):
                cfg[key] = value
        classes[name] = cfg
    return merged


CONFIG = load_deployment()
CLASS_LIMITS = CONFIG['resources']['classes']
VILLAGE_DEFAULTS = CONFIG['resources']['villageDefaults']


def reload_deployment(path=None):
    global CONFIG, CLASS_LIMITS, VILLAGE_DEFAULTS
    CONFIG = load_deployment(path)
    CLASS_LIMITS = CONFIG['resources']['classes']
    VILLAGE_DEFAULTS = CONFIG['resources']['villageDefaults']
    GOVERNOR.reload(CONFIG)
    return CONFIG


def limits_for(resource_class):
    return dict(CLASS_LIMITS.get(resource_class) or CLASS_LIMITS['light'])


def resource_class_for(task, command=''):
    """Classify a card/command. Explicit ``resourceClass`` wins; otherwise a
    heavy command or media-shaped card is media/gpu, and everything else is
    light."""
    task = task or {}
    explicit = str(task.get('resourceClass') or '').strip().lower()
    if explicit in CLASS_NAMES:
        return explicit
    text = ' '.join(str(task.get(k) or '') for k in
                    ('title', 'instructions', 'goal', 'projectLabel'))
    text = (text + ' ' + str(command or '')).lower()
    if _GPU_RE.search(text):
        return 'gpu'
    if _HEAVY_RE.search(text):
        return 'media'
    if str(task.get('deliverable') or '').lower() in ('note', 'report'):
        return 'light'
    return 'light'


def village_policy(state, village_id):
    default = dict(VILLAGE_DEFAULTS)
    for village in (state or {}).get('villages') or []:
        if village.get('id') == village_id:
            policy = village.get('resourcePolicy') or {}
            if isinstance(policy, dict):
                for key in ('maxConcurrent', 'maxHeavy', 'hold', 'reason'):
                    if key in policy:
                        default[key] = policy[key]
            break
    try:
        default['maxConcurrent'] = max(1, int(default.get('maxConcurrent') or 1))
    except (TypeError, ValueError):
        default['maxConcurrent'] = 2
    try:
        default['maxHeavy'] = max(0, int(default.get('maxHeavy') or 0))
    except (TypeError, ValueError):
        default['maxHeavy'] = 1
    hold = default.get('hold') or []
    default['hold'] = [str(x).strip().lower() for x in hold
                       if str(x).strip()] if isinstance(hold, list) else []
    default['reason'] = str(default.get('reason') or '')
    return default


class ResourceDenied(Exception):
    def __init__(self, reason, retry_after_s=30):
        super().__init__(reason)
        self.reason = reason
        self.retry_after_s = retry_after_s


class ResourceGovernor:
    def __init__(self, config=None):
        self._lock = threading.Condition()
        self._leases = {}
        self._next_id = 0
        self.reload(config or CONFIG)

    def reload(self, config=None):
        self.config = config or load_deployment()
        self.classes = self.config['resources']['classes']
        self.defaults = self.config['resources']['villageDefaults']

    def _sweep(self):
        now = time.time()
        for lease_id, lease in list(self._leases.items()):
            if now - lease['startedAt'] > lease['timeoutS'] + 60:
                self._leases.pop(lease_id, None)

    def _snapshot_locked(self):
        out = {'global': {name: 0 for name in CLASS_NAMES}, 'villages': {}}
        for lease in self._leases.values():
            out['global'][lease['resourceClass']] = \
                out['global'].get(lease['resourceClass'], 0) + 1
            village = out['villages'].setdefault(
                lease['villageId'],
                {'total': 0, 'heavy': 0,
                 'classes': {name: 0 for name in CLASS_NAMES}})
            village['total'] += 1
            village['classes'][lease['resourceClass']] = \
                village['classes'].get(lease['resourceClass'], 0) + 1
            if lease['resourceClass'] in HEAVY_CLASSES:
                village['heavy'] += 1
        return out

    def snapshot(self):
        with self._lock:
            self._sweep()
            return self._snapshot_locked()

    def acquire(self, village_id, resource_class, task_id='', agent_id='',
                state=None, wait_s=0):
        village_id = village_id or 'main'
        resource_class = resource_class if resource_class in CLASS_NAMES else 'light'
        limit = dict(self.classes.get(resource_class) or self.classes['light'])
        timeout_s = max(1, int(limit.get('timeoutS') or 120))
        policy = village_policy(state, village_id)
        deadline = time.time() + max(0, float(wait_s or 0))
        with self._lock:
            while True:
                self._sweep()
                held = policy['hold']
                if (resource_class in held
                        or ('heavy' in held and resource_class in HEAVY_CLASSES)):
                    reason = policy.get('reason') or 'held by village policy'
                    raise ResourceDenied(
                        f'village resource hold: {reason}', retry_after_s=60)
                usage = self._snapshot_locked()
                village = usage['villages'].get(
                    village_id, {'total': 0, 'heavy': 0})
                if usage['global'].get(resource_class, 0) >= int(
                        limit.get('maxConcurrent') or 1):
                    message = f'host {resource_class} capacity is busy'
                elif village['total'] >= policy['maxConcurrent']:
                    message = f'village {village_id} concurrency limit reached'
                elif (resource_class in HEAVY_CLASSES
                      and village['heavy'] >= policy['maxHeavy']):
                    message = f'village {village_id} heavy-work limit reached'
                else:
                    self._next_id += 1
                    lease_id = f'lease-{self._next_id}'
                    lease = {
                        'id': lease_id,
                        'villageId': village_id,
                        'resourceClass': resource_class,
                        'taskId': task_id or '',
                        'agentId': agent_id or '',
                        'startedAt': time.time(),
                        'timeoutS': timeout_s,
                        'cpus': limit.get('cpus') or 1.0,
                        'memoryMb': limit.get('memoryMb') or 512,
                        'pidsLimit': limit.get('pidsLimit') or 128,
                        'timeout': timeout_s,
                    }
                    self._leases[lease_id] = lease
                    return dict(lease)
                if time.time() >= deadline:
                    raise ResourceDenied(message)
                self._lock.wait(timeout=min(1.0, max(0.05, deadline - time.time())))

    def release(self, lease_id):
        with self._lock:
            self._leases.pop(lease_id, None)
            self._lock.notify_all()


GOVERNOR = ResourceGovernor(CONFIG)
