from types import SimpleNamespace
from unittest.mock import Mock, patch

import review_model
import serve


def server(saved=None, mid='medium-model', coding='coding-model'):
    return SimpleNamespace(_get_setting=Mock(return_value=saved),
                           _mid_tier_slug=Mock(return_value=mid),
                           _coding_tier_slug=Mock(return_value=coding))


def test_review_defaults_to_medium():
    configured = server()
    assert review_model.choose(configured) == 'medium-model'
    configured._coding_tier_slug.assert_not_called()


def test_persisted_review_selection_survives_general_tier_changes():
    configured = server({'model':'openai/gpt-5.4-mini','tier':'mid'})
    assert review_model.choose(configured) == 'openai/gpt-5.4-mini'
    assert review_model.choose(server('{"model":"openai/gpt-5.4-mini"}')) == 'openai/gpt-5.4-mini'
    configured._mid_tier_slug.return_value='different-automatic-pick'
    assert review_model.choose(configured) == 'openai/gpt-5.4-mini'


def test_missing_medium_uses_coding_never_low():
    assert review_model.choose(server(mid=None)) == 'coding-model'
    assert review_model.choose(server(mid=None,coding=None)) is None


def test_review_and_qa_routing_use_review_selection_without_changing_code():
    with patch.object(serve,'_review_tier_slug',return_value='review-model'), \
            patch.object(serve,'_coding_tier_slug',return_value='coding-model'):
        for kind in ('review','qa'):
            assert serve._resolve_model_tier('Inspect source and recommend repairs',kind)=='review-model'
        assert serve._resolve_model_tier('Implement changes','code')=='coding-model'
