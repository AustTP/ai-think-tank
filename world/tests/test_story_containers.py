import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import story_containers


def settings():
    return SimpleNamespace(PROXY_CONTAINER='proxy',PROXY_PORT=8899,SANDBOX_NETWORK='internal',
        SANDBOX_IMAGE='sandbox-image',SANDBOX_TIMEOUT_S=10)


def test_persistent_container_uses_only_its_workspace_and_keeps_internal_network(tmp_path):
    directory=tmp_path/'story-a'
    directory.mkdir()
    calls=[]
    containers=set()
    def run(args,timeout=30):
        calls.append(args)
        result=SimpleNamespace(returncode=0,stdout='',stderr='')
        if args[:2]==['docker','inspect']:
            if args[-1] not in containers:
                result.returncode=1
            else:
                result.stdout=json.dumps([{'Config':{'Labels':{'ai-think-tank.managed':'story'}},
                    'Mounts':[{'Destination':'/workspace','Type':'bind'}]}])
        if args[:3]==['docker','image','inspect']:
            result.returncode=1
        if args[:2]==['docker','create']:
            containers.add(args[args.index('--name')+1])
        return result
    with patch.object(story_containers,'_run',side_effect=run):
        story_containers.execute(directory,'python -m pip install example',settings())
        story_containers.execute(directory,'python -m pytest',settings())
    creates=[c for c in calls if c[:2]==['docker','create']]
    assert len(creates)==1
    assert '--privileged' not in creates[0]
    assert creates[0][creates[0].index('--network')+1]=='internal'
    assert any(str(directory.resolve()) in part and 'target=/workspace' in part for part in creates[0])
    assert sum(c[:2]==['docker','exec'] for c in calls)==2


def test_stage_and_parent_share_dependencies_but_other_stories_do_not(tmp_path):
    stage=tmp_path/'stage-example'
    stage.mkdir()
    (tmp_path/'stage-example.runtime.json').write_text(json.dumps({'workspace':'story-a'}))
    assert story_containers.identity(stage)[2]==story_containers.identity(tmp_path/'story-a')[2]
    assert story_containers.identity(stage)[2]!=story_containers.identity(tmp_path/'story-b')[2]


def test_unmanaged_container_is_never_removed(tmp_path):
    with patch.object(story_containers,'_run',return_value=SimpleNamespace(returncode=0,stdout='[{"Config":{"Labels":{}}}]')):
        with pytest.raises(ValueError,match='unmanaged'):
            story_containers.release(tmp_path/'story-a')
