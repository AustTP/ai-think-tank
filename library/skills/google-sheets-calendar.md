# Google Sheets & Calendar APIs

## Purpose
Reach for this before any real Sheets or Calendar work through the vault-held
Google OAuth credential (client id/secret + refresh token).

## Key facts
- **Both APIs are free today** for standard usage within quota. Google has
  announced overage billing is **planned for later in 2026** (exact pricing not
  yet published as of this writing) -- this is a real, near-term change, not a
  hypothetical.
- **Quotas are per-MINUTE, not per-day** (an earlier agent spike got this wrong):
  - Sheets: 300 requests/minute/project, 60/minute/user/project. No published
    daily cap beyond staying inside the per-minute limits.
  - Calendar: 10,000 requests/minute/project, 600/minute/user/project, plus a
    1,000,000 requests/day project-wide threshold before overage charges could
    apply.
- **Calendar's quotas are dramatically HIGHER than Sheets', not lower** (the
  earlier spike said the opposite). Per-project, per-minute: Calendar allows
  ~33x more requests than Sheets; per-user, ~10x more.
- Exceeding quota returns `429`/`403 usageLimits`; the documented fix is
  exponential backoff with randomization (Google's own recommendation), not an
  immediate retry.
- The refresh token is long-lived until explicitly revoked -- fine to hold in the
  vault indefinitely, same encrypted-credential + scoped-handle pattern as
  everything else external.

## Policy for this think tank
1. Prefer read-only / low-frequency use (a shared roadmap sheet synced
   occasionally, a handful of real calendar events for real ceremonies) over any
   write-heavy or high-frequency automation -- there's no real need to get near
   even Sheets' tighter per-minute limit for anything this think tank would
   plausibly do.
2. Once 2026's overage pricing is published, re-check this file before assuming
   continued free usage.
3. Same vault pattern: agent gets a scoped capability handle, never the raw
   client secret or refresh token.

## Sources
- https://developers.google.com/sheets/api/limits (Sheets quotas + 2026 pricing
  note)
- https://developers.google.com/calendar/api/guides/quota (Calendar quotas +
  daily threshold + 2026 pricing note)
- Verified by the developer with real web search, correcting an
  earlier agent spike (see Lessons learned below).

## Lessons learned
- An earlier agent "spike" claimed Sheets/Calendar used a "daily
  project-level" quota and that Calendar's quota was lower than Sheets'. Both
  claims were wrong once checked against Google's real docs: the quotas are
  per-minute, and Calendar's is far HIGHER than Sheets', not lower. Same root
  cause as the Treg and DigitalOcean skill files: a spike has no real internet
  access and answers from a plausible-sounding guess, not a lookup.
