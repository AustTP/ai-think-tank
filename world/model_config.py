"""Validate the model-band configuration without importing serve.py."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

BANDS = ('low', 'sublow', 'mid', 'coding', 'high', 'vision')


def load_bands(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def validate(config: dict, db_path: Path | None = None) -> tuple[list[str], list[dict]]:
    problems: list[str] = []
    price_bands = config.get('price_bands') or {}
    purpose = config.get('band_purpose') or {}
    benchmarks = config.get('band_benchmark') or {}
    gaps = config.get('quality_floor_gap') or {}
    criteria = config.get('tier_gate_criteria') or {}

    for band in ('low', 'mid', 'high'):
        if band not in price_bands:
            problems.append(f'{band}: missing price_bands entry')
    for band in ('low', 'sublow', 'mid', 'coding', 'high'):
        if band not in purpose:
            problems.append(f'{band}: missing band_purpose entry')
    for band in BANDS:
        if not benchmarks.get(band):
            problems.append(f'{band}: missing band_benchmark entry')
    for band, bounds in price_bands.items():
        if band not in BANDS:
            problems.append(f'{band}: unknown band in price_bands')
        if not isinstance(bounds, list) or len(bounds) != 2:
            problems.append(f'{band}: price_bands must be [low, high]')
            continue
        try:
            low, high = float(bounds[0]), float(bounds[1])
        except (TypeError, ValueError):
            problems.append(f'{band}: price_bands must be numeric')
            continue
        if low < 0 or high <= low:
            problems.append(f'{band}: invalid price range {bounds}')
    valid_ranges = []
    for k, v in price_bands.items():
        if not isinstance(v, list) or len(v) != 2:
            continue
        try:
            lo, hi = float(v[0]), float(v[1])
        except (TypeError, ValueError):
            continue
        if hi > lo:
            valid_ranges.append((lo, hi, k))
    ordered = sorted(valid_ranges, key=lambda row: row[0])
    for (lo1, hi1, name1), (lo2, hi2, name2) in zip(ordered, ordered[1:]):
        if lo2 < hi1:
            problems.append(f'price bands overlap: {name1} and {name2}')
    if not gaps:
        problems.append('quality_floor_gap is empty')
    for band, gap in gaps.items():
        try:
            if float(gap) < 0:
                problems.append(f'{band}: quality gap must be non-negative')
        except (TypeError, ValueError):
            problems.append(f'{band}: quality gap must be numeric')
    if 'default' not in gaps:
        problems.append('quality_floor_gap.default is missing')
    for band in ('low', 'mid', 'high'):
        if band not in criteria:
            problems.append(f'{band}: missing tier_gate_criteria entry')

    rows = []
    if db_path and Path(db_path).exists():
        con = sqlite3.connect(f'file:{Path(db_path)}?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in con.execute(
                'SELECT * FROM model_tiers ORDER BY band')]
            seen = {r['band'] for r in rows}
            for band in BANDS:
                if band not in seen:
                    problems.append(f'{band}: no selected model in model_tiers')
            for row in rows:
                if row['band'] not in BANDS:
                    problems.append(f"{row['band']}: unknown band in model_tiers")
                if not row.get('slug'):
                    problems.append(f"{row['band']}: empty model slug")
                try:
                    if float(row.get('price_per_m') or 0) < 0:
                        problems.append(f"{row['band']}: negative price")
                except (TypeError, ValueError):
                    problems.append(f"{row['band']}: non-numeric price")
            fallbacks: dict[str, list[dict]] = {}
            for row in con.execute(
                    'SELECT band, rank, slug FROM model_tier_fallbacks '
                    'ORDER BY band, rank'):
                fallbacks.setdefault(row['band'], []).append(row)
            for band, items in fallbacks.items():
                ranks = [int(r['rank']) for r in items]
                if ranks != list(range(1, len(ranks) + 1)):
                    problems.append(f'{band}: fallback ranks are not 1..N')
                if any(not r['slug'] for r in items):
                    problems.append(f'{band}: empty fallback slug')
        finally:
            con.close()
    return problems, rows


def table(rows: list[dict]) -> str:
    lines = ['band      price/M   model']
    for row in rows:
        lines.append(f"{row['band']:<9} ${float(row['price_per_m']):<7.3f} "
                     f"{row['slug']}")
    return '\n'.join(lines)
