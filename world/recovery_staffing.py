"""Scrum masters coordinate recovery; researchers execute investigations."""
def step(state):
    import sim
    masters = {t.get('scrumMasterId') for t in state.get('teams',[])}
    workers = __import__('task_master').workers(state)
    agents = state.get('agents',{})
    tasks = state.get('tasks',{})
    load = {aid:sum(t.get('assignedTo') == aid and t.get('status') in ('walking','working') for t in tasks.values())
                 +sum(q.get('assignedTo') == aid for q in state.get('workQueue',[])) for aid in workers}
    for task in list(tasks.values()):
        if task.get('status') != 'investigating' or task.get('investigationOf') or task.get('reviewOf'):
            continue
        master = task.get('recoveryOwner') or (task.get('assignedTo') if task.get('assignedTo') in masters else None)
        if not master: continue
        children = [t for t in tasks.values() if t.get('investigationOf') == task['id']]
        completed = [t for t in children if t.get('status') == 'done']
        if completed:
            latest = completed[-1]
            task.update(status='recovery_planned',recoveryOwner=master,
                assignedTo=task.get('originalAssignedTo') or latest.get('assignedTo'),
                investigationReport=latest.get('libraryPath'),
                note='Investigation completed by '+str(latest.get('assignedTo'))+'. Repair planning coordinated by '+master+'; original deliverable still requires review.')
            continue
        if any(t.get('status') in ('walking','working','needs_review') for t in children): continue
        queue = next((q for q in state.get('workQueue',[]) if q.get('investigationOf') == task['id']),None)
        if queue is None and task.get('recoveryStaffed'):
            continue
        team = next((t for t in state.get('teams',[]) if t.get('scrumMasterId') == master),{})
        candidates = [aid for aid in workers if aid in team.get('members',[]) and not agents[aid].get('busy')]
        if not candidates: candidates = [aid for aid in workers if not agents[aid].get('busy')]
        if not candidates: continue
        worker = min(candidates,key=lambda aid:load[aid])
        if queue is None:
            sim.queue_work(state,[{'title':'Investigate review blocker: '+task.get('title','story'),
                'taskType':'spike','room':'observatory','budgetMs':120000,'budgetUsd':0.30,
                'investigationOf':task['id'],'assignedTo':worker,'directRoute':True,'taskMasterWork':True,
                'teamId':team.get('id'),'priority':'high','projectLabel':'Blocker recovery',
                'instructions':'Investigate '+task['id']+' under scrum master '+master+'. Recorded failure: '
                    +str(task.get('failNote') or task.get('note') or '')+'\nRequired: '+str(task.get('acceptanceCriteria') or '')
                    +'\nRead Library '+str(task.get('libraryPath') or 'reports')+'. Save an evidence-based diagnosis and use file_issue to create concrete repair or research stories. Do not approve the original work.',
                'acceptanceCriteria':'Save the root cause, evidence, recovery owner and actionable repair/research stories with testable criteria.'}])
        else:
            queue.update(assignedTo=worker,directRoute=True,taskMasterWork=True)
        task.update(assignedTo=worker,recoveryOwner=master,recoveryStaffed=True,
                    note='Research investigation assigned to '+worker+'; scrum coordination: '+master+'.')
        load[worker] += 1
