import transport_recovery


def fixture(preset='always'):
    parent={'id':'p','status':'repairing','budgetUnlimited':True,'budgetUsd':0}
    task={'id':'fix','reviewOf':'p','title':'Fix: story','taskType':'code','assignedTo':'leo'}
    return {'tasks':{'p':parent,'fix':task},'completionControl':{'approvalPreset':preset},'workQueue':[]}, task


def failure(**extra):
    return {'ok':False,'note':'Coding model request failed: Remote end closed connection without response Existing workspace preserved.',**extra}


def test_first_transport_retry_is_immediate_and_repeats_use_bounded_backoff():
    state,task=fixture()
    for attempt in range(1,12):
        state['workQueue']=[]
        assert transport_recovery.queue(state,task,failure(),100000)
        delay=state['workQueue'][0]['notBefore']-100000
        assert delay == 0 if attempt==1 else 0<delay<=900000
    assert state['tasks']['p']['status']=='repairing'
    assert not state['tasks']['p']['manualAttention']


def test_other_presets_budget_stops_and_real_code_failures_do_not_use_transport_retry():
    for preset in ('moderation','strict'):
        state,task=fixture(preset)
        assert not transport_recovery.queue(state,task,failure(),100000)
    state,task=fixture()
    state['tasks']['p']['budgetExhausted']=True
    assert not transport_recovery.queue(state,task,failure(),100000)
    state['tasks']['p']['budgetExhausted']=False
    assert not transport_recovery.queue(state,task,{'ok':False,'note':'pytest failed: assertion error'},100000)


def test_saved_stage_is_preserved_and_duplicate_retry_is_not_queued():
    state,task=fixture()
    assert transport_recovery.queue(state,task,failure(stagedWorkspace='stage-preserved'),100000)
    assert state['tasks']['p']['lastFailedWorkspace']=='stage-preserved'
    assert not transport_recovery.queue(state,task,failure(),100001)
    assert len(state['workQueue'])==1
