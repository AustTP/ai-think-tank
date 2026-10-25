# facade: village.schedule
"""Compatibility loader for :mod:`village.schedule`."""
import os as _os
import sys as _sys
_here = globals().get('__file__')
if _here:
    _pkg_root = _os.path.dirname(_os.path.abspath(_here))
else:
    _pkg_root = next((p for p in _sys.path if _os.path.isfile(_os.path.join(p, 'village', 'schedule.py'))), '')
_target = _os.path.join(_pkg_root, 'village', 'schedule.py')
_facade_file = globals().get('__file__')
globals()['__file__'] = _os.path.join(_pkg_root, '_facade_' + _os.path.basename(_target))
with open(_target, encoding='utf-8') as _fh:
    exec(compile(_fh.read(), _target, 'exec'))
globals()['__file__'] = _facade_file
