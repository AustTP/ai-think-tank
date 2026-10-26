"""Detect when a provider's real charge stops matching its published price.

Every api_call that returns a billed amount records (estimate, actual). The
drift audit compares recent actuals with the estimates quoted for those same
calls. A sustained difference is a price change, even when the provider's
pricing page has not been re-read.

The audit never edits the catalog. Prices are operator policy; it logs a
drift finding and the operator updates world/api_services.json.
"""
import statistics

SERVICE_PRICE_DRIFT_THRESHOLD = 0.20
SERVICE_PRICE_DRIFT_MIN_SAMPLES = 3
SERVICE_PRICE_DRIFT_WINDOW_DAYS = 30
SERVICE_PRICE_DRIFT_INTERVAL_S = 6 * 3600


def _api_service_id(service):
    if not isinstance(service, dict):
        return None
    if service.get('id'):
        return service['id']
    for sid, spec in (_API_SERVICES or {}).items():
        if spec is service:
            return sid
    return None


def _api_actual_usd(parsed_json, workflow_result=None):
    """The provider's billed amount for a call, when it reports one. Never
    guesses: a missing field stays None and the call is not an observation."""
    if isinstance(workflow_result, dict):
        for key in ('actualUsd', 'usd'):
            value = workflow_result.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                return float(value)
    if isinstance(parsed_json, dict):
        usage = parsed_json.get('usage')
        if isinstance(usage, dict):
            for key in ('usd', 'cost', 'total_cost'):
                value = usage.get(key)
                if isinstance(value, (int, float)) and value >= 0:
                    return float(value)
    return None


def record_service_price_observation(service, endpoint, estimate_usd, actual_usd,
                                     basis='', source='', agent_id=None):
    """Best-effort: accounting must never break the real call."""
    if not service or actual_usd is None:
        return
    try:
        with _db() as conn:
            conn.execute(
                'INSERT INTO service_price_observations '
                '(ts, service, endpoint, estimate_usd, actual_usd, basis, source, agent_id) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (time.time(), service, endpoint, estimate_usd, actual_usd,
                 basis, source, agent_id))
    except Exception:
        pass


def service_price_drift_report(days=30, min_samples=3, threshold=0.20):
    """Recent (service, endpoint) groups whose billed median differs from the
    quoted median by at least `threshold`. Uses the last 20 observations per
    group so an operator's catalog update is reflected as soon as new calls
    are made, instead of being diluted by old estimates."""
    since = time.time() - max(1, int(days)) * 86400
    try:
        with _db() as conn:
            rows = [{'service': r[0], 'endpoint': r[1], 'ts': r[2],
                     'estimate_usd': r[3], 'actual_usd': r[4]}
                    for r in conn.execute(
                        'SELECT service, endpoint, ts, estimate_usd, actual_usd '
                        'FROM service_price_observations '
                        'WHERE ts >= ? AND actual_usd IS NOT NULL AND estimate_usd IS NOT NULL '
                        'AND estimate_usd > 0 ORDER BY ts DESC', (since,))]
    except Exception:
        return []
    groups = {}
    for row in rows:
        key = (row['service'], row.get('endpoint') or '')
        groups.setdefault(key, []).append(row)
    findings = []
    for (service, endpoint), items in groups.items():
        items = items[:20]
        if len(items) < max(1, int(min_samples)):
            continue
        estimates = [float(r['estimate_usd']) for r in items]
        actuals = [float(r['actual_usd']) for r in items]
        estimate_med = statistics.median(estimates)
        actual_med = statistics.median(actuals)
        if estimate_med <= 0:
            continue
        delta = (actual_med - estimate_med) / estimate_med
        if abs(delta) < float(threshold):
            continue
        findings.append({
            'service': service,
            'endpoint': endpoint or None,
            'samples': len(items),
            'medianEstimateUsd': round(estimate_med, 6),
            'medianActualUsd': round(actual_med, 6),
            'deltaPct': round(delta * 100, 1),
            'lastAt': items[0]['ts'],
        })
    return sorted(findings, key=lambda r: abs(r['deltaPct']), reverse=True)


def log_service_price_drift():
    """One audit pass. Logs each finding at most once per day per endpoint."""
    today = time.strftime('%Y-%m-%d', time.gmtime())
    logged = 0
    for finding in service_price_drift_report():
        key = f"service_price_drift:{finding['service']}:{finding['endpoint'] or ''}"
        if _get_setting(key) == today:
            continue
        log_action(None, 'service_price_drift', {**finding, 'date': today}, authorized=False)
        _set_setting(key, today)
        logged += 1
        try:
            if TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS:
                endpoint = finding['endpoint'] or finding['service']
                _telegram_send_text_sync(
                    f"Price drift: {endpoint} billed median "
                    f"${finding['medianActualUsd']:.4f} vs quoted "
                    f"${finding['medianEstimateUsd']:.4f} over {finding['samples']} calls "
                    f"({finding['deltaPct']:+.1f}%). Review world/api_services.json.")
        except Exception:
            pass
    return logged


async def _service_price_drift_loop():
    while True:
        try:
            await asyncio.to_thread(log_service_price_drift)
        except Exception:
            pass
        await asyncio.sleep(SERVICE_PRICE_DRIFT_INTERVAL_S)
