"""Pull the Nous Portal model catalog and upsert every model's prices into
model_catalog (per-1M-token USD, promo prices captured as-is, one-shot;
the catalog API lists 400+ models). Run: python3 world/tools/nous_catalog_sync.py"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve  # noqa: E402

KEY = serve._load_env().get('NOUS_API_KEY') or ''
req = urllib.request.Request(
    'https://inference-api.nousresearch.com/v1/models',
    headers={'Authorization': 'Bearer ' + KEY})
with urllib.request.urlopen(req, timeout=30) as r:
    data = json.load(r)
models = data.get('data') or []
print('nous catalog models:', len(models))

now = time.time()
written = 0
with serve._db() as conn:
    for m in models:
        mid = m.get('id') or ''
        pricing = m.get('pricing') or {}
        try:
            pin = float(pricing.get('prompt') or 0) * 1_000_000
            cout = float(pricing.get('completion') or 0) * 1_000_000
        except (TypeError, ValueError):
            continue
        name = m.get('name') or mid
        providers = 'nous'
        conn.execute(
            "INSERT INTO model_catalog (model_id, name, prompt_price, completion_price, price_per_m, first_seen, last_seen, providers) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (model_id) DO UPDATE SET "
            "prompt_price=excluded.prompt_price, completion_price=excluded.completion_price, "
            "price_per_m=excluded.price_per_m, last_seen=excluded.last_seen, "
            "providers=CASE WHEN model_catalog.providers LIKE '%nous%' THEN model_catalog.providers ELSE model_catalog.providers || ',nous' END",
            (mid, name, pin, cout, (pin + cout) / 2, now, now, providers))
        written += 1
print('catalog rows upserted:', written)
