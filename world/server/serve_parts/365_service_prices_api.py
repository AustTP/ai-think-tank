"""Read-only HTTP surface for the service price catalog."""


@app.get('/api/service-pricing')
async def service_pricing_list():
    return JSONResponse(service_price_report())


@app.post('/api/service-pricing')
async def service_pricing_quote(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return JSONResponse(service_price_report(
        service=body.get('service'), path=body.get('path'),
        body=body.get('body') if isinstance(body.get('body'), dict) else {}))
