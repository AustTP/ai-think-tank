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

## Guardrails

- Never commit secrets; the repo scanner rejects them anyway.
- Issues/PRs you open must cite the card id in the body.
- Public API only from sandboxes; web scraping of github.com HTML is
  unnecessary — the REST API is the interface.
