# Pipeline review ? October 6, 2026

## Confirmed failure

The final brief task failed at flake8 because an agent overwrote
`crypto_trading_brief.py` with an unfinished triple-quoted f-string and undefined
placeholder variables. The validated implementation was restored from Ada's
prototype mirror. It reads a supplied scored CSV and produces a brief without
inventing candidate rankings or prices.

## Current validation

- flake8: PASS (exit 0).
- mypy: PASS (exit 0).
- bandit: PASS (exit 0).
- pytest-cov: PASS (exit 0).

## Remaining input and workflow limitations

A current scored dataset with `symbol` and `score` columns is still required
for the final ranking/brief. Synthetic test fixtures are not market research.
External-source permissions and safety escalations must be resolved through
the app's existing approval flow. Historical failed tasks remain failed: passing
these checks does not retroactively approve or complete their deliverables.

## Reading complete documents

Open a file in the Library and choose **Expand document** to hide the sidebar
and editing controls and use most of the window for reading. **Collapse document**
returns to the normal Library view. The player document reader loads the full
file; the previous 200,000-character agent-context limit no longer cuts off the
player's reading view. Completed tasks and completed research spikes with a
saved Library path offer **Read document** on the Board.
