# parts: integrations/bank_parts
"""Ordered source parts for :mod:`integrations.bank`."""
import os as _os
_here = globals().get('__file__') or __file__
_base = _os.path.dirname(_os.path.abspath(_here))
_parts = _os.path.join(_base, 'integrations', 'bank_parts')
if not _os.path.isdir(_parts):
    _parts = _os.path.join(_os.path.dirname(_base), 'integrations', 'bank_parts')
for _fn in sorted(f for f in _os.listdir(_parts) if f.endswith('.py')):
    _target = _os.path.join(_parts, _fn)
    with open(_target, encoding='utf-8') as _fh:
        exec(compile(_fh.read(), _target, 'exec'))
