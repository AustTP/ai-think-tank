"""Host/village resource policy and lease governor."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.resources as resources  # noqa: E402


def _config(light_max=1, media_max=1, browser_max=2):
    cfg = resources.load_deployment()
    cfg['resources']['classes']['light']['maxConcurrent'] = light_max
    cfg['resources']['classes']['browser']['maxConcurrent'] = browser_max
    cfg['resources']['classes']['media']['maxConcurrent'] = media_max
    cfg['resources']['villageDefaults']['maxConcurrent'] = 2
    cfg['resources']['villageDefaults']['maxHeavy'] = 1
    return cfg


def _state(policy=None):
    return {'villages': [{'id': 'main',
                          'resourcePolicy': policy or {}}]}


def test_resource_class_prefers_explicit_card_value():
    assert resources.resource_class_for(
        {'resourceClass': 'media', 'title': 'note'}) == 'media'


def test_resource_class_infers_heavy_commands_and_cards():
    assert resources.resource_class_for(
        {'title': 'edit the launch video'}) == 'media'
    assert resources.resource_class_for(
        {'title': 'note'}, 'python train.py --cuda') == 'gpu'
    assert resources.resource_class_for({'title': 'note'}) == 'light'


def test_global_class_limit_and_release():
    gov = resources.ResourceGovernor(_config(light_max=1))
    lease = gov.acquire('main', 'light', state=_state())
    try:
        try:
            gov.acquire('main', 'light', state=_state())
        except resources.ResourceDenied as exc:
            assert 'capacity' in exc.reason
        else:
            raise AssertionError('second lease should be denied')
    finally:
        gov.release(lease['id'])
    again = gov.acquire('main', 'light', state=_state())
    gov.release(again['id'])


def test_village_concurrency_limit():
    gov = resources.ResourceGovernor(_config(light_max=4))
    state = _state({'maxConcurrent': 1, 'maxHeavy': 1, 'hold': []})
    lease = gov.acquire('main', 'light', state=state)
    try:
        try:
            gov.acquire('main', 'light', state=state)
        except resources.ResourceDenied as exc:
            assert 'concurrency' in exc.reason
        else:
            raise AssertionError('village limit should deny the second lease')
    finally:
        gov.release(lease['id'])


def test_village_hold_blocks_media_and_heavy_alias():
    gov = resources.ResourceGovernor(_config())
    state = _state({'maxConcurrent': 2, 'maxHeavy': 1,
                    'hold': ['media'], 'reason': 'laptop busy'})
    try:
        gov.acquire('main', 'media', state=state)
    except resources.ResourceDenied as exc:
        assert 'laptop busy' in exc.reason
    else:
        raise AssertionError('media should be held')
    state = _state({'maxConcurrent': 2, 'maxHeavy': 1, 'hold': ['heavy']})
    try:
        gov.acquire('main', 'gpu', state=state)
    except resources.ResourceDenied:
        pass
    else:
        raise AssertionError('heavy alias should hold gpu')


def test_village_heavy_limit():
    gov = resources.ResourceGovernor(_config(media_max=2))
    state = _state({'maxConcurrent': 4, 'maxHeavy': 1, 'hold': []})
    lease = gov.acquire('main', 'media', state=state)
    try:
        try:
            gov.acquire('main', 'media', state=state)
        except resources.ResourceDenied as exc:
            assert 'heavy-work' in exc.reason
        else:
            raise AssertionError('heavy limit should deny the second lease')
    finally:
        gov.release(lease['id'])


def test_snapshot_counts_by_village_and_class():
    gov = resources.ResourceGovernor(_config(light_max=4))
    state = _state({'maxConcurrent': 4, 'maxHeavy': 1, 'hold': []})
    lease = gov.acquire('main', 'light', state=state)
    snap = gov.snapshot()
    assert snap['global']['light'] == 1
    assert snap['villages']['main']['total'] == 1
    gov.release(lease['id'])
    assert gov.snapshot()['villages'] == {}
