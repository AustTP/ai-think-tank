import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serve
import local_work_model


def test_loopback_does_not_expire_before_local_model_load_and_generation():
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"reply":"completed"}'
    with patch.object(serve, '_load_env', return_value={}), \
         patch.object(serve, 'get_state_from_db', return_value={}), \
         patch.object(local_work_model, 'route', return_value={'timeout': 600}), \
         patch.object(serve.urllib.request, 'urlopen', return_value=response) as opened:
        result = serve._http_json('POST', 'http://127.0.0.1:8936', '/api/chat',
                                  {'agentId': 'eva', 'max_tokens': 3500}, timeout=120)
    assert result == {'reply': 'completed'}
    assert opened.call_args.kwargs['timeout'] >= 1230
