"""Read-only view of the scrum masters' learned card templates."""


@app.get('/api/workflow-templates')
def list_workflow_templates():
    state = get_state_from_db() or {}
    templates = list((state.get('workflowTemplates') or {}).values())
    order = {'active': 0, 'candidate': 1, 'retired': 2}
    templates.sort(key=lambda t: (
        order.get(t.get('status'), 3),
        -(int(t.get('successCount') or 0) + int(t.get('crossTeamSuccess') or 0)),
        t.get('name') or ''))
    return JSONResponse({
        'count': len(templates),
        'active': sum(1 for t in templates if t.get('status') == 'active'),
        'candidate': sum(1 for t in templates if t.get('status') == 'candidate'),
        'retired': sum(1 for t in templates if t.get('status') == 'retired'),
        'templates': templates,
    })


@app.get('/api/workflow-templates/{template_id}')
def get_workflow_template(template_id: str):
    state = get_state_from_db() or {}
    template = (state.get('workflowTemplates') or {}).get(template_id)
    if not template:
        return JSONResponse({'error': 'unknown workflow template'}, status_code=404)
    return JSONResponse(template)
