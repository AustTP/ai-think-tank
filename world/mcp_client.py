# facade: integrations.mcp_client
"""Compatibility loader for :mod:`integrations.mcp_client`."""
import os as _os
import sys as _sys
_here = globals().get('__file__')
if _here:
    _pkg_root = _os.path.dirname(_os.path.abspath(_here))
else:
    _pkg_root = next((p for p in _sys.path if _os.path.isfile(_os.path.join(p, 'integrations', 'mcp_client.py'))), '')
_target = _os.path.join(_pkg_root, 'integrations', 'mcp_client.py')
_facade_file = globals().get('__file__')
globals()['__file__'] = _os.path.join(_pkg_root, '_facade_mcp_client.py')
with open(_target, encoding='utf-8') as _fh:
    exec(compile(_fh.read(), _target, 'exec'))
globals()['__file__'] = _facade_file
