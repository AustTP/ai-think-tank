"""Shared helpers for the split test_cov_gap_serve.py modules."""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402


_NO_GRANT = object()
_SENTINEL = object()


def req_with(body):
    req = mock.Mock()
    req.json = mock.AsyncMock(return_value=body)
    req.headers = {'X-Agent-Key': 'k'}
    return req


def decision_cache_report_defaults():
    return {'ttl_s': serve.DECISION_CACHE_TTL_S, 'cached_keys': 0,
            'hits': 0, 'requests': 0, 'hit_pct': 0.0}
