"""One-time title backfill (operator directive 2026-11-28).

Every assistant hire minted a role of "Assistant to {who they relieve}" and a
mission frozen at hire time. Org moves never refreshed either, so the roster
wears stale relationship badges (ruth: titled after nora, directed by maya,
mission citing the dead nora team). This boot migration retitles every agent
still carrying an "Assistant to X" role with the function title their recent
real work supports, and rewrites the mission to name the CURRENT director,
keeping the original hire as provenance. Implemented in sim
(_backfill_assistant_titles) so the derivation is unit-testable offline;
this wrapper only moves state in and out of the DB. Idempotent: retitled
agents no longer match the "Assistant to " prefix, so reruns are no-ops and
it is safe on every boot like the other _backfill_* migrations.
"""


def _backfill_assistant_titles_in_db():
    import sim
    state = get_state_from_db()
    if state is None:
        return
    changed = sim._backfill_assistant_titles(state)
    if changed:
        save_state_to_db(state)
        print(f'[titles] backfilled {changed} assistant role(s)', flush=True)
