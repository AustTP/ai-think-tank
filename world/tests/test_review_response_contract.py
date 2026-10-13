import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serve


def test_json_reviews_forward_format_and_reserve_budget_for_visible_verdict():
    captured = []
    def response(request):
        captured.append(json.loads(request.data))
        return {'choices':[{'message':{'content':'{"verdict":"approve","summary":"checked","checks":[],"risks":[],"repairStories":[]}'}}]}
    with patch.object(serve, '_think_tank_spend_cap_exceeded', return_value=False), \
         patch.object(serve, 'is_model_circuit_broken', return_value=False), \
         patch.object(serve, 'record_model_result'), \
         patch.object(serve, '_read_openrouter_response', side_effect=response):
        serve._call_openrouter_sync('review-model',[{'role':'user','content':'Review and return JSON'}],2400,
                                    response_format={'type':'json_object'})
    assert captured[0]['response_format']=={'type':'json_object'}
    assert captured[0]['reasoning']=={'enabled':False}
    assert captured[0]['max_tokens']==2400
