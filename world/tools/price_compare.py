#!/usr/bin/env python3
"""Price comparison across providers, with vendor-id aliasing.

Provider catalogs identify the same underlying model differently:
Cheaper Inference uses bare ids (gpt-5.6-sol), OpenRouter and Straitly use
vendor-prefixed ids (openai/gpt-5.6-sol), sometimes with a variant suffix.
The catalog NEVER renames an id -- model_id is what the provider's API
expects -- so aliasing lives ONLY here, in the comparison layer:

  alias_key(id) strips the vendor prefix (and returns the variant-stripped
  base as a fallback candidate); two rows alias when an alias_key matches
  UNIQUELY in both directions. Ambiguity is reported, never merged: two
  different vendors can legitimately ship a same-named model.

Usage:
  python3 price_compare.py CI_CATALOG.json OR_CATALOG.json [DB.tsv]

  CI catalog: {data: [{id, type:'text', pricing:{input_per_million,
              output_per_million}}]}  (per-million strings)
  OR catalog: {data: [{id, pricing:{prompt, completion}}]}  (per-token)
  DB.tsv:     model_id \t prompt$/M \t completion$/M \t providers (the
              tank's model_catalog projection, for straitly-tagged rows)

Prices are compared as a 1:1 blend (in+out)/2 per million tokens.
"""
import json
import sys


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def alias_keys(model_id):
    """Candidate identity keys for a model id, strongest first: the bare
    suffix after the last '/', then the variant-stripped base, then the id
    itself. Aliasing requires an EXACT key equality with exactly one
    candidate on the other side."""
    base = model_id.rsplit('/', 1)[-1]
    keys = [base]
    variant = base.split(':', 1)[0]
    if variant != base:
        keys.append(variant)
    if model_id != base:
        keys.append(model_id)
    return keys


def alias_match(source_id, target_rows):
    """The unique aliased id in target_rows for source_id, or None.

    Variant classes must AGREE exactly: 'name:batch' is a different tier
    (batch pricing) from 'name', so a no-variant id never merges into a
    :batch row and vice versa. Within the same variant class the stripped
    bases must be equal, and multiple candidates = ambiguous = no match
    (reported by the caller, never merged)."""
    sbase = source_id.rsplit('/', 1)[-1]
    svar = sbase.split(':', 1)[1] if ':' in sbase else None
    spine = sbase.split(':', 1)[0]
    matches = []
    for rid in target_rows:
        rbase = rid.rsplit('/', 1)[-1]
        rvar = rbase.split(':', 1)[1] if ':' in rbase else None
        if rvar != svar:
            continue
        if rbase.split(':', 1)[0] == spine:
            matches.append(rid)
    return matches[0] if len(matches) == 1 else None


def load_ci(path):
    rows = {}
    for m in json.load(open(path))['data']:
        if m.get('type') != 'text':
            continue
        p = m.get('pricing') or {}
        pin, pout = _f(p.get('input_per_million')), _f(p.get('output_per_million'))
        if pin is None or pout is None:
            continue
        rows[m['id']] = {'in': pin, 'out': pout, 'blend': (pin + pout) / 2}
    return rows


def load_openrouter(path):
    rows = {}
    for m in json.load(open(path))['data']:
        p = m.get('pricing') or {}
        pin, pout = _f(p.get('prompt')), _f(p.get('completion'))
        if pin is None or pout is None:
            continue
        rows[m['id']] = {'in': pin * 1e6, 'out': pout * 1e6,
                         'blend': (pin + pout) / 2 * 1e6}
    return rows


def load_db_projection(path):
    rows = {}
    for line in open(path):
        parts = line.rstrip('\n').split('\t')
        if len(parts) < 4:
            continue
        mid, pin, pout, provs = parts[0], _f(parts[1]), _f(parts[2]), parts[3]
        if pin is None or pout is None:
            continue
        rows[mid] = {'in': pin, 'out': pout, 'blend': (pin + pout) / 2,
                     'provs': provs}
    return rows


def cross_compare(left_rows, right_rows, left_tag, right_tag):
    """Alias left against right; print matches, ambiguity, and the
    significant-savings table. Ambiguous = an id matching >1 candidate
    within its variant class: reported, never merged."""
    matched, ambiguous = [], []
    for lid in left_rows:
        hit = alias_match(lid, right_rows)
        if hit:
            matched.append((lid, hit))
        else:
            # Distinguish a true miss from an ambiguity worth reporting.
            sbase = lid.rsplit('/', 1)[-1]
            svar = sbase.split(':', 1)[1] if ':' in sbase else None
            spine = sbase.split(':', 1)[0]
            cands = [rid for rid in right_rows
                     if (rid.rsplit('/', 1)[-1].split(':', 1)[1]
                         if ':' in rid.rsplit('/', 1)[-1] else None) == svar
                     and rid.rsplit('/', 1)[-1].split(':', 1)[0] == spine]
            if len(cands) > 1:
                ambiguous.append((lid, cands))
    sig = []
    for lid, rid in matched:
        l, r = left_rows[lid], right_rows[rid]
        if l['blend'] < r['blend'] and (1 - l['blend'] / r['blend']) >= 0.25:
            sig.append({'lid': lid, 'rid': rid,
                        'l_in': l['in'], 'l_out': l['out'],
                        'r_in': r['in'], 'r_out': r['out'],
                        'save_pct': 1 - l['blend'] / r['blend'],
                        'save_blend': r['blend'] - l['blend']})
    sig.sort(key=lambda m: -m['save_blend'])
    print(f'{left_tag} priced: {len(left_rows)}  {right_tag} priced: '
          f'{len(right_rows)}  aliased: {len(matched)}  '
          f'ambiguous (not merged): {len(ambiguous)}')
    for lid, cands in ambiguous[:8]:
        print(f'  ambiguous: {lid} -> {cands}')
    cheaper = sum(1 for lid, rid in matched
                  if left_rows[lid]['blend'] < right_rows[rid]['blend'])
    print(f'{left_tag} cheaper on {cheaper} of {len(matched)}; '
          f'>=25% blend cheaper on {len(sig)}')
    if sig:
        print(f"\n{'model':52} {left_tag+' in/out $/M':>17} "
              f"{right_tag+' in/out $/M':>17} {'save%':>6} {'save$/M':>8}")
        for m in sig[:25]:
            print(f"{m['rid'][:52]:52} {m['l_in']:8.3f}/{m['l_out']:7.3f} "
                  f"{m['r_in']:8.3f}/{m['r_out']:7.3f} "
                  f"{100 * m['save_pct']:5.0f}% {m['save_blend']:8.2f}")
    return sig


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    ci = load_ci(argv[1])
    orr = load_openrouter(argv[2])
    cross_compare(ci, orr, 'CI', 'OR')
    if len(argv) > 3:
        print()
        db = load_db_projection(argv[3])
        straitly = {k: v for k, v in db.items()
                    if 'straitly' in v.get('provs', '')}
        cross_compare(ci, straitly, 'CI', 'straitly')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
