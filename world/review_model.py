"""Persisted model selection for independent review and remediation advice."""
import json

SETTING = 'review_model_policy'


def policy(serve):
    read = getattr(serve, '_get_setting', lambda *args: None)
    saved = read(SETTING)
    if isinstance(saved, str):
        try:
            saved = json.loads(saved)
        except ValueError:
            saved = {}
    return saved if isinstance(saved, dict) else {}


def choose(serve):
    saved = policy(serve)
    model = saved.get('model')
    if isinstance(model, str) and model.strip():
        return model.strip()
    # Reviews never silently fall back to the low tier.
    return serve._mid_tier_slug() or serve._coding_tier_slug()
