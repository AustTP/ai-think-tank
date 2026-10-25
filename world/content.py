# parts: agents/content_parts
"""Ordered source parts for :mod:`content`."""
import os as _os
_here = globals().get('__file__') or __file__
_parts = _os.path.join(_os.path.dirname(_os.path.abspath(_here)), *['agents', 'content_parts'])
for _fn in sorted(f for f in _os.listdir(_parts) if f.endswith('.py')):
    _target = _os.path.join(_parts, _fn)
    with open(_target, encoding='utf-8') as _fh:
        exec(compile(_fh.read(), _target, 'exec'))
