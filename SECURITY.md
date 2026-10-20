# Security policy

## Reporting a vulnerability

Do not open a public issue, pull request, or discussion for a security problem.

Use GitHub's private vulnerability reporting for this repository: open the
**Security** tab and choose **Report a vulnerability**. That keeps the report
visible only to maintainers.

Include what is affected, how to reproduce it, the impact you see, and the
commit or version you tested. Do not test against someone else's instance or
run load tests.

## What happens next

- We acknowledge the report.
- We confirm the issue and agree on a fix window with you.
- We fix it, then agree on a disclosure date.
- Reporters are credited if they want to be.

## Supported versions

Security fixes land on `main` and in the latest release. Older copies do not
receive separate fixes.

## Scope

This policy covers the code in this repository. A running think tank holds
prompts, agent memory, credentials, and personal data, so reports that involve
cross-agent access, credential handles, file grants, browser sessions, or the
confirmation ledger are in scope.

## Deliberate choices a scanner may flag

- **Sandbox execution.** `/api/execute` runs agent-authored commands inside the
  configured sandbox. The command is classified before it runs, the sandbox is
  the isolation boundary, and the host filesystem is not mounted.
- **Local browser profiles.** Social runs may copy a consented Safari cookie
  seed into a `0600` profile. The seed is limited to the target social domain.
  Consent is explicit in `.env`.
- **Model-generated SQL-free state.** SQLite statements use parameters for
  values. Schema and column names are fixed internal strings.
- **Auto-generated credentials.** `SERVER_SECRET`, admin credentials, and
  `DEVICE_API_KEY` can be generated on first boot and printed once. They are
  never committed; `.env` is gitignored.

## Secrets

- Never commit `.env`, cookies, API keys, tokens, or real user data.
- Use `.env.template` for documentation. Leave secret-shaped values blank.
- Rotate a leaked credential immediately. `DEVICE_API_KEY` and
  `PHOTO_REVIEW_KEY` have rotation endpoints; other service keys are rotated at
  the provider and then updated in `.env`.
