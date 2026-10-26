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


@app.get('/api/service-pricing/drift')
async def service_pricing_drift(days: int = 30, threshold: float = 0.20):
    return JSONResponse({'findings': service_price_drift_report(
        days=max(1, min(int(days), 365)), threshold=max(0.01, min(float(threshold), 5.0)))})
