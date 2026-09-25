# GitHub

## Purpose
Reach for this before any real GitHub work: the player-triggered publish flow,
or any future work that creates or manages a repository.

## Key facts
- The village's ONLY current GitHub integration is the player-triggered
  "Publish to GitHub" action from The House, which pushes the village's released
  work to a single pre-existing private repo named by `AI_VILLAGE_PUBLISH_REPO`
  (`owner/repo`) in `.env`, via `git push` authenticated through the `gh` CLI's
  own cached login (`gh auth token`) -- NOT the vault-held `GITHUB_TOKEN`. The
  target repo is not created by the app: make it private first, then set the var.
- Nothing leaves the machine unless the player explicitly clicks publish;
  agents never push on their own.
- There is currently **no code path that creates a new repository** anywhere in
  the village. This policy is for if/when that capability gets built.
- The vault-held `GITHUB_TOKEN` was checked live (2026-09-24) and its actual
  granted scopes are **very broad**: `admin:org, admin:org_hook,
  admin:public_key, admin:repo_hook, delete:packages, delete_repo, gist,
  notifications, project, repo, user, workflow, write:packages`. That includes
  full org administration and the ability to delete ANY repo the account can
  reach -- far more than pushing released work needs. Treat it as a
  high-blast-radius credential; scope any capability handle built on it as
  narrowly as the specific action actually requires.
- Recommendation (not yet acted on): if real repo-creation or repo-management
  work is ever built, prefer minting a fine-grained GitHub PAT scoped to only
  the specific repo(s) involved, rather than continuing to use this
  broadly-scoped classic PAT.

## Policy for this village
1. **Any repository an agent creates or pushes to MUST be private.** No
   exceptions without an explicit, separate player decision. This applies
   whether the repo already exists (verify before pushing) or gets created by
   a future capability (set `private: true` / equivalent at creation time,
   never rely on a default).
2. Before any future "create a repo" capability is built, re-scope the
   underlying token (fine-grained PAT, repo-restricted) rather than handing an
   agent a capability backed by the current admin-level classic PAT.
3. Same vault pattern as everything else external: an agent gets a scoped
   capability handle, never the raw token.

## Sources
- Verify a target repo's visibility with `gh repo view <owner>/<repo>` and
  inspect an actual token's granted scopes via `GET api.github.com/user` (the
  `X-OAuth-Scopes` response header).

## Lessons learned
- The publish path uses `gh`'s own logged-in session for push, never the
  vault-held token -- this keeps the broad-scoped token off the hot path.
  If a future capability mints real repository access, prefer a fine-grained,
  repo-restricted PAT over a broadly-scoped classic one.