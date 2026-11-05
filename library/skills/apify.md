# Apify (DEPRECATED)

**Status: deprecated 2026-11-05.** The owner's teams-own-external-services
directive removed the tank's reliance on Apify actors. The account key and
the budget ledger still exist for legacy reconciliation, but no new work
should start an actor run.

## Why it was retired

- The account is on the FREE plan, which refuses every paid actor -- the
  lanes that mattered (X/Twitter, YouTube audio) were already dead in
  practice.
- X moved to `twscrape` against the tank's own session pool (see
  `library/skills/x.md`).
- YouTube moved to captions-first via `youtube-transcript-api`, with
  yt-dlp + faster-whisper in the agent's own sandbox for captionless
  videos (see `library/skills/youtube.md`).

## What to use instead

| Old actor use case | Replacement |
| --- | --- |
| X/Twitter search, timelines | `library/skills/x.md` (twscrape pool) |
| YouTube transcripts | `library/skills/youtube.md` recipe 1 |
| YouTube audio transcription | `library/skills/youtube.md` recipe 2 |
| Reddit reads | `library/skills/reddit.md` (praw / public JSON) |
| Generic structured scraping | your sandbox + the approved tool list (trusted-codebase registry); browser work via `/api/browse` |

## If you genuinely need an actor

Only the owner can approve a paid-plan upgrade or a new actor. File an
escalation naming the exact actor, the data you need, and why the sandbox
recipes cannot get it. Do not spend budget on actor runs on your own.
