# GitHub research and publishing (teams own this)

The tank keeps GitHub interaction generic: a neutral authenticated-call
primitive plus standard git. This file is the recipe book.

## What the tank provides

- `_github_api_call`-backed credential store: a stored GitHub token is used
  through the credential ask flow; you name the service, never see the
  secret.
- Standard `git` in your sandbox (clone, commit, push to any remote the
  card names).
- The trusted-codebase registry for anything beyond REST + git.

## Recipe 1: read public data (no credentials)

```
curl -s 'https://api.github.com/repos/OWNER/REPO/issues?per_page=10'
curl -s 'https://api.github.com/repos/OWNER/REPO/actions/runs?per_page=5'
```

Unauthenticated REST is rate-limited (60/hr per IP) — batch your questions.

## Recipe 2: authenticated calls

Request the GitHub credential via the ask flow, then call the REST API with
the injected token (the ask flow injects it into your sandbox env; you never
see it):

```
curl -s -H "Authorization: Bearer $GITHUB_TOKEN" \
     'https://api.github.com/repos/OWNER/REPO/issues?per_page=10'
```

5000/hr. Use it for issue triage, releases, CI status at scale.

## Recipe 3: publish a deliverable

The publish staging flow (cards that end in a GitHub push) already runs
`git init → add -A → commit → push` against the card's approved remote.
For a NEW repo or org, that is an owner decision — file an escalation, do
not create repos unilaterally.

## Recipe 4: file a change request as a GitHub issue (2026-11-25 rollout)

`main` is protected: direct pushes are refused, the `gate` check must pass
on a PR. Change work goes through issues, not a direct commit:

```
# 1. Open the issue (choose a short kebab issueKey, e.g. retried-ingest):
curl -s -X POST -H "Authorization: Bearer $GITHUB_TOKEN" \
     'https://api.github.com/repos/OWNER/REPO/issues' \
     -d '{"title":"<what and why>","body":"Problem: ...\nRequest: ...\nCard: <card id>"}'
# 2. A director approval spawns the card FROM the issue; the card records
#    the issue with a `Refs: issue #N` line in its instructions (Recipe 5).
# 3. The coding agent works on a feature branch named agent/<cardId>
#    (suffix -<slug> only when one card genuinely needs two PRs).
# 4. Push the branch and open the PR against main, with
#    "Closes #<issue number>" in the body and the card id cited.
curl -s -X POST -H "Authorization: Bearer $GITHUB_TOKEN" \
     'https://api.github.com/repos/OWNER/REPO/pulls' \
     -d '{"title":"...","head":"agent/<cardId>","base":"main","body":"Closes #<n>\nCard: <card id>\n<pickaxe/merge-risk summary>"}'
# 5. When gate is green, merge the PR (squash) and delete the branch:
curl -s -X PUT -H "Authorization: Bearer $GITHUB_TOKEN" \
     'https://api.github.com/repos/OWNER/REPO/pulls/<n>/merge' \
     -d '{"merge_method":"squash"}'
```

Never bypass: no `git push` to `main`, no SST-livedirect commits from a
sandbox. If the gate fails, fix and push to the SAME branch; the PR updates.

## Recipe 5: issue → card wiring (directors)

Sim cards already support an external reference. When filing a change
request that an agent should implement, record the GitHub issue number in
the card itself: a `Refs: issue #N` line in the card's instructions (the
coding prompt reads it back when opening the PR, and the branch name
`agent/<cardId>` traces the PR to the card in one hop). The work request
room reads it back when the PR opens so the tracker closes the loop.
Regional VPs file issues for their domain; the owner's escalation path
stays for money, credentials, and irreversible actions.

## Policy for this think tank

- Work on the tank's own repo (AustTP/ai-think-tank) is delivered as a
  branch → pull request, never a direct push to `main` (branch protection
  requires the `gate` check, 2026-11-25). Branch name: `agent/<cardId>`
  (suffix `-<slug>` only when one card genuinely needs two PRs). The card
  carries `Refs: issue #N` so the branch → card → issue chain is
  traceable in one hop. Open the PR with
  "Closes #<issue number>" plus the card id, merge SQUASH when the gate is
  green, and delete the branch. Never bypass or force-push; failing gate
  means fix and push to the SAME branch.
- Change requests that an agent should implement are filed as GitHub
  issues first (REST, Recipe 2 shape) with the card id in the body; the
  card records `Refs: issue #N` in its instructions. Regional VPs file
  issues for their domain's change requests; money, credentials, and
  irreversible actions still go through the owner escalation path.

## Guardrails

- Never commit secrets; the repo scanner rejects them anyway.
- Issues/PRs you open must cite the card id in the body.
- Public API only from sandboxes; web scraping of github.com HTML is
  unnecessary — the REST API is the interface.
- Feature branches only (`agent/<cardId>`); `main` is PR-only since
  2026-11-25.
