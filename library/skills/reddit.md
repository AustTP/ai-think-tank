# Reddit research (teams own this)

The tank hardcodes nothing for Reddit. Use the approved tool and the neutral
primitives; keep this recipe current when Reddit changes its API behavior.

## Approved tool (trusted-codebase registry)

- `praw-dev/praw` — Reddit's official Python client, read-only use.

## Recipe: read-only research

1. Credentials: Reddit's API needs a client id/secret. If the tank has them
   stored, request them through the credential ask flow (`/api/ask` naming
   `reddit`); you will see usage, never the secret itself. If they are not
   stored, the ask flow is how the owner gets asked — do not scrape
   authenticated surfaces without one.
2. In your sandbox via `/api/execute`:

```
pip install praw
python - <<'PY'
import praw, os
r = praw.Reddit(client_id=os.environ['REDDIT_ID'],
                client_secret=os.environ['REDDIT_SECRET'],
                user_agent='ai-think-tank-research/1.0')
sub = r.subreddit('python')
for post in sub.hot(limit=10):
    print(post.title, '|', post.score, '|', post.url)
PY
```

3. Public read-only mode (no secrets, rate-limited harder) also works:
   pass `client_id`/`client_secret` unset and only `user_agent` — Reddit
   serves limited public JSON. For small lookups prefer plain JSON:

```
curl -A 'ai-think-tank-research/1.0' 'https://www.reddit.com/r/python/hot.json?limit=10'
```

## Guardrails

- Read-only. Never post, vote, or message from tank identities without an
  owner-approved card saying exactly that.
- Back off on 429s; one subreddit at a time.
- Cite permalinks in your reports; save findings under `library/research/`.
