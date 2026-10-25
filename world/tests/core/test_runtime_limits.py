import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import runtime_limits


def test_chat_allowance_scales_with_reply_budget():
    assert runtime_limits.transport('/api/chat', {'max_tokens': 1500}) >= 150
    assert runtime_limits.transport('/api/chat', {'max_tokens': 12000}) >= 1000


def test_pipeline_allowance_scales_with_step_count():
    assert runtime_limits.transport('/api/pipeline', {'steps': [1, 2, 3]}) >= 300


def test_observed_slow_call_raises_the_next_allowance():
    runtime_limits._OBSERVED.clear()
    before = runtime_limits.transport('/api/chat', {'max_tokens': 150})
    runtime_limits.observe('/api/chat', 300)
    assert runtime_limits.transport('/api/chat', {'max_tokens': 150}) > before
